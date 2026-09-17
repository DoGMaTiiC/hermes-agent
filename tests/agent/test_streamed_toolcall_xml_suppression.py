"""Streaming-pipeline regression: tool-call XML must never reach delta consumers.

Repro from the review on NousResearch/hermes-agent#114136: Responses-wire deltas flow
through ``agent/stream_delivery.py::_fire_stream_delta`` into ``stream_delta_callback`` /
``_stream_callback`` / the ``on_stream_delta`` hooks, where only reasoning tags were
scrubbed — so a serialized ``<atem:function_calls>`` block reached the CLI/gateway/TTS
consumers raw before final cleanup. This drives the real pipeline (real ``AIAgent``
defaults from ``agent_init``, real callback fan-out) rather than the scrubber unit, so it
fails if the wiring is missing even when the scrubber class is correct.
"""

from __future__ import annotations


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


BLOCK = (
    "Checking the queue.\n"
    "<atem:function_calls>\n"
    '<atem:invoke name="default.terminal">\n'
    '<atem:parameter name="command">echo hi</atem:parameter>\n'
    "</atem:invoke>\n"
    "</atem:function_calls>"
)


def _stream(agent, deltas) -> str:
    """Push deltas through the production stream path; return what consumers saw."""
    visible: list[str] = []
    agent.stream_delta_callback = visible.append
    for delta in deltas:
        agent._fire_stream_delta(delta)
    agent._reset_stream_delivery_tracking()  # end-of-stream flush
    return "".join(visible)


class TestStreamedToolCallXmlSuppression:
    def test_block_never_reaches_delta_consumers(self):
        out = _stream(_make_agent(), [BLOCK])
        assert "atem:" not in out
        assert "function_calls" not in out
        assert out == "Checking the queue.\n"

    def test_tags_split_across_deltas_never_leak(self):
        deltas = [
            "Checking the queue.\n<atem:function_",
            'calls>\n<atem:invoke name="default.terminal">\n',
            "<atem:parameter",
            ' name="command">echo hi</atem:parameter>\n</atem:invoke>\n',
            "</atem:function_calls>",
        ]
        out = _stream(_make_agent(), deltas)
        assert "atem:" not in out
        assert out == "Checking the queue.\n"

    def test_cut_serialization_drops_the_unterminated_tail(self):
        deltas = ['Waiting.\n<atem:function_calls>\n<atem:invoke name="x">']
        out = _stream(_make_agent(), deltas)
        assert "atem:" not in out
        assert out == "Waiting.\n"

    def test_plain_prose_is_untouched(self):
        out = _stream(_make_agent(), ["No tags ", "here, just ", "prose."])
        assert out == "No tags here, just prose."
