"""Stateful scrubber for tool-call XML that leaked onto the text channel of a stream.

Open models on the Responses wire (observed: muse-spark via opencode-go) occasionally serialize
their next native tool call into the content stream as ``<atem:function_calls>…</atem:function_calls>``
instead of a ``function_call`` item. The completed response is cleaned by the regex stripper in
``agent/agent_runtime_helpers.py``, but per-delta consumers (CLI display, gateway previews, TTS,
``on_stream_delta`` hooks) see the raw XML first, and a tag split across deltas defeats a
per-delta regex (the same failure class that ``StreamingThinkScrubber`` exists for).

This scrubber mirrors the final stripper's tool-call positions, chunk-safely:

* a closed ``<tag>…</tag>`` pair anywhere (either side may carry a namespace prefix),
* a stray ``</tag>`` closer (no matching opener; ``</function>`` included, mirroring
  ``_STRAY_TOOL_CALL_CLOSER_PATTERN``),
* an unterminated opener at a line boundary — the stream was cut mid-serialization
  (mirrors ``_UNTERMINATED_TOOL_CALL_PATTERN``).

The tag itself follows the final stripper's keep/strip decision: a mid-line opener whose closer
never arrives is released at ``flush()`` (the final stripper keeps it too), a line-anchored one
is dropped. One cosmetic difference, called out because it is the only one: the whitespace that
preceded a dropped opener has already been streamed by the time the anchor can be known (the
final regex also removes that ``newline + indentation`` run) — whitespace only, never XML.
Out of scope, same as the regexes: the prose-gated ``<function name=…>`` block and the GLM
``<arg_key>`` markup.

Safe text is emitted immediately per delta (no buffering of ordinary prose), so downstream
consumers see the same deltas as before except for the suppressed spans. The tag-name list
lives here and is imported by the final stripper, so a tag added here is covered on every
surface (the single-list contract ``think_scrubber`` established).
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

__all__ = ["TOOL_CALL_TAG_NAMES", "StreamingToolCallScrubber"]

# The one list of text-channel tool-call tag names: bound by the final-response stripper
# (agent/agent_runtime_helpers.py) and by this scrubber.
TOOL_CALL_TAG_NAMES: Tuple[str, ...] = (
    "tool_call",
    "tool_calls",
    "tool_result",
    "function_call",
    "function_calls",
)
# ``</function>`` closes nothing here, but the final stripper removes it as a stray closer.
_STRAY_CLOSER_NAMES: Tuple[str, ...] = TOOL_CALL_TAG_NAMES + ("function",)
_OPEN_NAMES = frozenset(TOOL_CALL_TAG_NAMES)
_CLOSE_NAMES = frozenset(_STRAY_CLOSER_NAMES)

_WHITESPACE = " \t\n\r\f\v"
# The run of characters a tag name portion may still be growing through (namespace allowed).
_RUN_RE = re.compile(r"[A-Za-z0-9_.:-]*")
# Optional ``ns:`` prefix + name word run; greedy, so a trailing ``\b``-equivalent is implicit.
_NAME_RE = re.compile(r"^(?:([\w.-]+):)?([A-Za-z0-9_]+)")
# A partial tag is abandoned past this many chars: real prefixes are short, and an unbounded
# hold would stall prose that merely contains '<'.
# ponytail: fixed ceiling — raise it if a provider ever streams longer tag prefixes.
_MAX_PARTIAL_TAG = 96


def _run_is_tag(run: str, names: frozenset) -> bool:
    """Whether the finished name portion *run* is exactly ``(ns:)?<recognized name>``."""
    if ":" in run:
        ns, local = run.split(":", 1)
        return bool(ns) and ":" not in local and local.lower() in names
    return run.lower() in names


def _run_may_grow(run: str, names: frozenset) -> bool:
    """Whether *run* (still at the buffer edge) could still grow into a recognized name."""
    if ":" in run:
        ns, local = run.split(":", 1)
        # one namespace separator at most; the local part must still be a name prefix
        return (
            bool(ns)
            and ":" not in local
            and (local == "" or any(n.startswith(local.lower()) for n in names))
        )
    # A run without ':' is either a growing name prefix or a growing namespace prefix
    # (any [\w.-] run may still take a ':' and become "<ns:name>").
    return True


def _is_partial_tag(s: str) -> bool:
    """Whether *s* (starting at ``<``, containing no ``>``) could still become a tool-call tag."""
    if len(s) > _MAX_PARTIAL_TAG:
        return False
    body = s[1:]
    closing = body.startswith("/")
    if closing:
        body = body[1:]
    if not body:
        return True
    m = _RUN_RE.match(body)
    run = m.group(0) if m else ""
    rest = body[len(run) :]
    names = _CLOSE_NAMES if closing else _OPEN_NAMES
    if rest:
        # a space or any other character ended the name portion; it is final now
        return _run_is_tag(run, names)
    return _run_may_grow(run, names)


def _classify_tag(tag: str) -> Tuple[Optional[str], Optional[str]]:
    """Classify one complete ``<…>`` span as ``("opener"|"closer"|None, name)``.

    Mirrors the final patterns: closers need ``>`` right after the name (no attributes);
    openers may carry attributes; the name word run is boundary-checked implicitly by the
    greedy match plus the membership test.
    """
    inner = tag[1:-1]
    closing = inner.startswith("/")
    body = inner[1:] if closing else inner
    m = _NAME_RE.match(body)
    if not m:
        return None, None
    ns, name = m.groups()
    name = name.lower()
    rest = body[m.end() :]
    if closing:
        if rest or name not in _CLOSE_NAMES:
            return None, None
        return "closer", name
    if name in _OPEN_NAMES:
        return "opener", name
    return None, None


class StreamingToolCallScrubber:
    """Chunk-safe suppression of text-channel tool-call XML (see the module docstring).

    ``feed()`` per delta; ``flush()`` at end of stream. Like the sibling scrubbers this is
    safe across intra-turn retries: ``flush()`` releases what is releasable and resets, so a
    later stream can feed again without ``reset()``.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Drop all state; called at the top of each turn by the agent."""
        self._pending: str = (
            ""  # held tail: a partial tag that may complete on the next feed
        )
        self._block_name: Optional[str] = (
            None  # name of the open block (None = not in a block)
        )
        self._block_raw: str = (
            ""  # raw span consumed inside the block (dropped on close)
        )
        self._block_anchored: bool = False  # opener sat at a line boundary
        self._skip_ws: bool = (
            False  # a stray closer went out: its trailing \s* is dropped
        )
        self._line_has_nonws: bool = False  # non-whitespace seen since the last newline

    # ── public API ─────────────────────────────────────────────────────────────

    def feed(self, text: str) -> str:
        """Return the visible portion of *text*; only a partial-tag tail is held back."""
        if not text:
            return ""
        buf = self._pending + text
        self._pending = ""
        out: List[str] = []
        while buf:
            if self._skip_ws:
                buf = buf.lstrip(_WHITESPACE)
                if not buf:
                    break
                self._skip_ws = False
            if self._block_name is not None:
                buf = self._advance_in_block(buf, out)
            else:
                buf = self._advance(buf, out)
        return "".join(out)

    def flush(self) -> str:
        """End of stream: release a mid-line unresolved opener verbatim (the final stripper keeps
        it too); a line-anchored one is dropped (mirrors the unterminated strip). Always resets,
        so an intra-turn retry's next stream can feed again."""
        out: List[str] = []
        if self._block_name is not None and not self._block_anchored:
            out.append(self._block_raw)
        out.append(self._pending)
        self.reset()
        return "".join(out)

    # ── internals ──────────────────────────────────────────────────────────────

    def _consume_line(self, text: str) -> None:
        """Track whether the original text has non-whitespace since its last newline (the
        line-boundary anchor). Everything consumed counts, including suppressed spans. Only
        space/tab keep a line 'blank' — the final pattern's ``[ \\t]*``."""
        for ch in text:
            if ch == "\n":
                self._line_has_nonws = False
            elif ch not in " \t":
                self._line_has_nonws = True

    def _advance(self, buf: str, out: List[str]) -> str:
        """One outside-block step. Returns the remaining buffer; a hold goes to ``_pending``
        and returns ''."""
        i = buf.find("<")
        if i == -1:
            self._consume_line(buf)
            out.append(buf)
            return ""
        s = buf[i:]
        gt = s.find(">")
        if gt == -1:
            if not _is_partial_tag(s):
                # a '<' that cannot become a tool-call tag: plain text, keep scanning after it
                self._consume_line(buf[: i + 1])
                out.append(buf[: i + 1])
                return buf[i + 1 :]
            if i:
                self._consume_line(buf[:i])
                out.append(buf[:i])
            self._pending = s
            return ""
        tag = s[: gt + 1]
        kind, name = _classify_tag(tag)
        if kind is None:
            # a '<' that cannot start a tool-call tag: plain text. A later '<' inside the span
            # may still start one, so emit through this character and keep scanning.
            self._consume_line(buf[: i + 1])
            out.append(buf[: i + 1])
            return buf[i + 1 :]
        if i:
            self._consume_line(buf[:i])
            out.append(buf[:i])
        if kind == "opener":
            self._block_anchored = not self._line_has_nonws
            self._block_name = name
            self._block_raw = tag
            self._consume_line(tag)
            return buf[i + gt + 1 :]
        # stray closer: the final stripper keeps whitespace ahead of it and drops the closer's
        # own trailing whitespace run (\s*)
        self._skip_ws = True
        self._consume_line(tag)
        return buf[i + gt + 1 :]

    def _advance_in_block(self, buf: str, out: List[str]) -> str:
        """Inside an open block: everything is held as raw until the closer of the open name is
        complete. The raw span is kept verbatim so a mid-line opener can be released at flush."""
        j = 0
        while True:
            j = buf.find("<", j)
            if j == -1:
                self._block_raw += buf
                self._consume_line(buf)
                return ""
            s = buf[j:]
            gt = s.find(">")
            if gt != -1:
                kind, name = _classify_tag(s[: gt + 1])
                if kind == "closer" and name == self._block_name:
                    self._block_raw += buf[:j]
                    self._consume_line(buf[:j])
                    self._consume_line(s[: gt + 1])
                    self._block_name = None
                    self._block_raw = ""
                    self._block_anchored = False
                    return buf[j + gt + 1 :]
                j += 1
                continue
            # no '>' at or after this '<': nothing here can close the block. Hold the earliest
            # tail that could still become the closer; anything before it is raw.
            k = j
            while k != -1:
                t = buf[k:]
                if _is_partial_tag(t):
                    self._block_raw += buf[:k]
                    self._consume_line(buf[:k])
                    self._pending = t
                    return ""
                k = buf.find("<", k + 1)
            self._block_raw += buf
            self._consume_line(buf)
            return ""
