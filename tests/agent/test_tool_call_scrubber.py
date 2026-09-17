"""Tests for StreamingToolCallScrubber (agent/tool_call_scrubber.py).

Contract, both ways: (1) for every covered tool-call XML shape the scrubber's chunk-split
output equals the final-response stripper's whole-string output — the stream is never
dirtier than the final answer. Where the final strips a line-anchored cut tail together
with the preceding ``newline + indentation``, the stream keeps that whitespace run (it was
already delivered before the anchor could be known) and suppresses the XML only; (2) no
delta consumer ever sees raw tool-call XML (the streaming repro from the review on
NousResearch/hermes-agent#114136). The scenarios map to the muse-spark / opencode-go
Responses-wire serialization: ``<atem:function_calls>…</atem:function_calls>`` blocks,
stray closers, and openers cut mid-serialization.
"""

from __future__ import annotations

from agent.agent_runtime_helpers import strip_think_blocks
from agent.tool_call_scrubber import StreamingToolCallScrubber

# The field block from the PR, and the cut-mid-serialization tail.
CLOSED_BLOCK = (
    "Checking the queue.\n"
    "<atem:function_calls>\n"
    '<atem:invoke name="default.terminal">\n'
    '<atem:parameter name="command">echo hi</atem:parameter>\n'
    "</atem:invoke>\n"
    "</atem:function_calls>"
)
CUT_TAIL = 'Waiting.\n<atem:function_calls>\n<atem:invoke name="default.terminal">'
REVIEWER_REPRO = (
    "redox=default.hermes_search_files Hollywood"
    "<atem:function_calls>...</atem:function_calls>"
)


def _drive(deltas: list[str]) -> str:
    """Feed a sequence of deltas and return the concatenated visible output."""
    s = StreamingToolCallScrubber()
    out = [s.feed(d) for d in deltas]
    out.append(s.flush())
    return "".join(out)


class TestCoveredShapes:
    def test_closed_block_single_delta(self):
        assert _drive([CLOSED_BLOCK]) == "Checking the queue.\n"

    def test_closed_block_split_across_deltas(self):
        deltas = [
            "Checking the queue.\n<atem:func",
            "tion_calls>\n<atem:invoke ",
            'name="x">y</atem:invoke></atem:func',
            "tion_calls>",
        ]
        assert _drive(deltas) == "Checking the queue.\n"

    def test_cut_mid_serialization_drops_the_line_anchored_tail(self):
        out = _drive([CUT_TAIL])
        assert "atem:" not in out
        assert out == "Waiting.\n"

    def test_stray_closer_is_suppressed(self):
        assert _drive(["done.</atem:function_calls> after"]) == "done.after"

    def test_plain_names_and_case_insensitivity(self):
        assert _drive(["a <Tool_Call>b</TOOL_CALL> c"]) == "a  c"

    def test_namespace_mismatch_between_open_and_close_still_closes(self):
        # the final pattern closes on any namespace prefix, keeping the space before '<'
        assert _drive(["x <tool_calls>y</atem:tool_calls>z"]) == "x z"

    def test_mid_line_open_without_closer_is_released_at_flush(self):
        # mirrors the final stripper exactly: no line anchor, no closer → kept
        assert _drive(["abc <tool_calls>xyz"]) == "abc <tool_calls>xyz"

    def test_prose_before_a_real_call_is_kept(self):
        assert _drive([REVIEWER_REPRO]) == "redox=default.hermes_search_files Hollywood"

    def test_non_tag_angle_text_survives(self):
        assert _drive(["a < b and 3 <4 stay"]) == "a < b and 3 <4 stay"


# Under every chunking the stream equals the final stripper byte for byte.
EXACT_CORPUS = [
    CLOSED_BLOCK,
    REVIEWER_REPRO,
    "abc <tool_calls>x</tool_calls> y",
    "x <tool_calls>y</tool_calls> z </tool_call> w",
    "mid</tool_calls>dle",
    "a < b and <tool_call>c</tool_call> d",
    "keep <this> and </that>",
    "<tool_calls foo='a>b'>x</tool_calls>y",
    "word<atem:funct",
    "plain text with no tags",
    "abc <tool_calls>xyz",
    "<TOOL_CALLS>x</tool_calls>after",
    "done.</atem:function_calls> after",
]

# Line-anchored unresolved openers: the final also removes the preceding newline +
# indentation; the stream has already delivered that whitespace run, so it may differ
# there only (asserted via rstrip below) — and never in XML.
ANCHORED_CORPUS = [
    CUT_TAIL,
    "abc\n  <tool_calls>xyz",
    "one\n\n  <tool_call>a\nb",
    "   <tool_call>lead at stream start",
]


class TestParityWithFinalStripper:
    """The invariant the review asked for: chunk-split stream == final stripper output."""

    def test_exact_equality_under_every_single_split(self):
        for text in EXACT_CORPUS:
            want = strip_think_blocks(None, text)
            for i in range(len(text) + 1):
                got = _drive([text[:i], text[i:]])
                assert got == want, (text, i, got, want)

    def test_exact_equality_char_by_char(self):
        for text in EXACT_CORPUS:
            assert _drive(list(text)) == strip_think_blocks(None, text), text

    def test_anchored_cut_differs_only_in_leading_whitespace(self):
        for text in ANCHORED_CORPUS:
            want = strip_think_blocks(None, text)
            half = len(text) // 2
            for chunks in ([text], list(text), [text[:half], text[half:]]):
                got = _drive(chunks)
                assert got.rstrip() == want.rstrip(), (text, chunks, got, want)
                assert "atem:" not in got, (text, chunks, got)

    def test_no_raw_markup_reaches_consumers(self):
        for text in (CLOSED_BLOCK, CUT_TAIL, REVIEWER_REPRO):
            out = _drive(list(text))
            assert "atem:" not in out
            assert "function_calls" not in out
