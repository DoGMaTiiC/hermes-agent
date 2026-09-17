# Spec — PR #114136: namespace-prefixed text-channel tool-call XML (review P1 remediation)

Status: frozen 2026-09-17 · Target: update of NousResearch/hermes-agent#114136 (branch `fix/strip-namespace-text-channel-toolcalls`) · Owner: DoGMaTiiC

## Problem Statement

muse-spark (opencode-go, Responses wire) sometimes serializes its next native tool call onto the **text channel** as `<atem:function_calls>…</atem:function_calls>` instead of a `function_call` item. The shipped regex fix only cleans the completed response, and review (ehz0ah, exact head `4a99053`) left two blocking findings:

1. **Stream leak** — Responses deltas flow through `codex_runtime.py` into `stream_delivery.py`, where `StreamingThinkScrubber` handles only reasoning tags. CLI, gateway, TTS and stream-hook consumers receive and accumulate the raw XML block before any final cleanup.
2. **Prefix trap** — `redox=default.hermes_search_files Hollywood<atem:function_calls>…</atem:function_calls>` strips to a non-empty prefix, so the empty-response recovery never runs, neither continuation guard matches, and the turn ends `finish_reason=stop` without executing the intended tool.

## Solution

Namespace-aware, chunk-safe tool-call XML handling on both surfaces:

- A stateful **streaming scrubber** so no delta consumer ever sees tool-call XML, including tags split across deltas.
- A **tail-anchored turn-recovery arm**: a text stop whose content ends in tool-call XML is not a final answer — re-prompt, reusing the existing dropped-tool-call machinery.

## User Stories

1. As a CLI user, I want streamed output to never display raw `<atem:function_calls>` XML, so that partial rendering shows prose, not markup.
2. As a gateway (Discord/Telegram) user, I want the streamed preview to stay clean even when a tag is split across deltas, so that progressive edits never expose serialization noise.
3. As a TTS consumer, I want tool-call XML suppressed before the speech callback, so that the audio never reads XML aloud.
4. As a plugin hook consumer (`on_stream_delta`), I want deltas to be pre-cleaned, so that hooks don't have to reimplement scrubbing.
5. As an unattended cron user, I want a turn ending on "prefix + tool-call XML" to be re-prompted instead of reported as a completed answer, so that the intended tool actually executes.
6. As a maintainer, I want one canonical list of tool-call tag names shared by the final stripper and the streaming scrubber, so the two never drift.

## Implementation Decisions

1. **New module `agent/tool_call_scrubber.py`** owns `TOOL_CALL_TAG_NAMES` and `StreamingToolCallScrubber`; `agent/agent_runtime_helpers.py` imports the names (single-list invariant, mirroring `THINK_TAG_NAMES` ↔ `think_scrubber`).
2. **Streaming wiring** (single choke point, agent-side): register the scrubber in `agent_init._STREAM_STATE`; feed chain `think → tool-call → context` in `StreamDeliveryMixin._fire_stream_delta`; flush order in `_reset_stream_delivery_tracking`; add it to the per-turn scrubber reset in `turn_context`.
3. **Streaming semantics = parity with the final stripper** for the three shapes the PR covers: closed pair anywhere (with/without namespace prefix), stray closer, unterminated opener. At flush: a line-anchored unresolved opener is discarded; a mid-line unresolved opener is released (mirrors the final stripper). The prose-gated `<function name=…>` form is out of scope for the scrubber.
4. **Turn recovery**: extend the dropped-tool-call arm in `agent/turn_final_response.py` — it fires when there are no `tool_calls` and the raw content ends (trailing whitespace allowed) in tool-call XML; action reuses `_DROPPED_TOOLCALL_NUDGE_CONTENT`, the bounded `_dropped_toolcall_retries` counter (max 3) and the ephemeral scaffolding flags.
5. **Empty-after-strip** stays on the existing empty-response ladder (unchanged); the new arm covers only the non-empty-prefix shape.
6. **Rebase** onto current `origin/main` — clean, zero conflicts (verified, also green on `64ea66b` per independent forward-port).
7. **Coexistence with PR #111472** (open, same file): the new arm is additive; no reordering of existing guards.

## Testing Decisions

- **Streaming**: new tests for `StreamingToolCallScrubber` — closed block; namespace + plain names; case-insensitivity; opener/name/closer each split across deltas; stray closer; unterminated-at-flush discard; mid-line release; **parity invariant**: for covered shapes, `feed()`+`flush()` concatenation equals the final stripper's output for the same text under multiple chunkings. Prior art: `tests/agent/test_think_scrubber.py` (`_drive` pattern), `tests/agent/test_streaming_context_scrubber.py`.
- **Turn recovery**: a test driving `finish_text_response` with the review's exact shape, asserting the turn does **not** end (verdict `continue`, nudge + interim row appended, `final_response=None`). Prior art: `tests/agent/test_intent_ack_continuation.py`.
- **Red-on-base** recorded for both new test groups (repo requirement).
- Focused suites: `test_strip_reasoning_tags_cli.py`, `test_think_scrubber.py`, `test_streaming*.py`, `test_intent_ack_continuation.py`, plus `tests/agent/`.

## Out of Scope

- Streaming parity for prose-gated `<function name=…>` blocks.
- Gateway-side re-filter changes (`gateway/stream_consumer_think.py` receives already-scrubbed deltas; unchanged).
- PR #111472's degenerate-final / mid-task arms.
- Hosted-CI parity (the PR has no hosted CI run; the local canonical runner remains the evidence, as before).

## Further Notes

- Review: PR #114136 review by ehz0ah (2×P1, head `4a99053`); independent verifications by whyyagswhy and actualrat1984 (forward-port green on `64ea66b`; rebase requested).
- Communication: only the technical substance (both P1s fixed, how, test evidence, rebase) goes to the upstream PR — no process/meta, no extra files in the PR branch.
- This spec lives on the fork (`docs/pr-114136-spec`), not in the PR and not upstream.
