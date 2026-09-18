"""Regression tests for dropped tool-call recovery.

Some providers (observed: claude-opus-4.8 / claude-sonnet-4.5 on GitHub
Copilot, ~2026-07) return ``finish_reason="tool_calls"`` while the parsed
``tool_calls`` array is empty — the model signalled it wanted to act but the
payload shipped no call. Before the fix, the conversation loop took the
no-tool-calls ``else`` branch, treated the turn's narration as the final
answer, and exited with the task unstarted. On an unattended multi-step job
(e.g. a scheduled PR reviewer) this silently did nothing.

The fix keys on the provider contract violation itself
(``finish_reason == "tool_calls"`` with zero ``tool_calls``) and re-prompts,
bounded to 3 consecutive stalls, with the budget resetting after any
successful tool round so it guards each stall rather than the whole run. A
genuine ``finish_reason="stop"`` text turn is unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_run_agent's fixture)
    so we can stage a dropped-tool-call response + continuation pair on
    ``.chat.completions.create``."""
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def _dropped_tool_call_response(content: str):
    """A response whose finish_reason claims a tool call, but tool_calls is
    empty — the provider contract violation this fix recovers from."""
    from tests.agent.test_run_agent import _mock_assistant_msg
    return SimpleNamespace(
        id="chatcmpl-dropped",
        model="test/model",
        choices=[SimpleNamespace(
            index=0,
            message=_mock_assistant_msg(content=content, tool_calls=None),
            finish_reason="tool_calls",
        )],
        usage=None,
    )


class TestDroppedToolCallRecovery:
    def test_dropped_tool_call_reprompts_instead_of_exiting(self, loop_agent):
        """finish_reason=tool_calls with an empty tool_calls array must
        re-prompt the model to emit the call rather than exiting the loop
        with the narration as the final answer."""
        from tests.agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _dropped_tool_call_response("Let me verify the PR and gather evidence."),
            _mock_response(content="All checks pass. Approved.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("review the PR")

        assert loop_agent.client.chat.completions.create.call_count == 2, (
            "A dropped tool call must trigger a re-prompt (second API call), "
            "not exit the loop after one call."
        )

        # The loop must have injected a nudge user-message telling the model to
        # issue the actual tool call.
        second_call = loop_agent.client.chat.completions.create.call_args_list[1]
        msgs = second_call.kwargs.get("messages") or second_call.args[0].get("messages")
        last_user = next(
            (m for m in reversed(msgs) if m.get("role") == "user"), None,
        )
        assert last_user is not None
        assert "tool call" in (last_user.get("content") or "").lower(), (
            "The nudge must explicitly ask the model to issue the tool call."
        )
        assert "All checks pass" in result["final_response"]


    def test_clean_stop_text_turn_is_unaffected(self, loop_agent):
        """A genuine finish_reason=stop text response must exit normally — the
        recovery path must not fire on ordinary final answers."""
        from tests.agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content="Here is your answer.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("hello")

        assert loop_agent.client.chat.completions.create.call_count == 1, (
            "A clean finish_reason=stop turn must not trigger a re-prompt."
        )
        assert "Here is your answer." in result["final_response"]

    def test_persistent_dropped_tool_calls_are_bounded(self, loop_agent):
        """If the model never emits a call, the recovery must give up after a
        bounded number of consecutive stalls instead of looping forever."""
        from tests.agent.test_run_agent import _mock_response

        # Stage plenty of dropped-tool-call responses followed by a clean stop,
        # so that if the bound is respected the loop exits on its own well
        # before exhausting the staged responses (no StopIteration).
        loop_agent.client.chat.completions.create.side_effect = [
            _dropped_tool_call_response("Let me check.") for _ in range(9)
        ] + [_mock_response(content="done", finish_reason="stop")]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("review the PR")

        # 1 initial call + at most 3 bounded re-prompts = 4 total before the
        # guard stops firing. It must NOT consume all 9 staged stalls.
        assert loop_agent.client.chat.completions.create.call_count <= 4, (
            "Consecutive dropped tool calls must be bounded (no infinite loop)."
        )
        assert result is not None

    def test_nudge_pair_is_ephemeral_scaffolding(self, loop_agent):
        """The re-prompt pair (interim assistant turn + synthetic user nudge)
        must be flagged as ephemeral scaffolding so persistence never writes
        it to the durable transcript — a resumed session must not replay the
        internal "issue the actual tool call now" instruction as user-authored
        context (#69630 review follow-up)."""
        from agent.session_persistence import _is_ephemeral_scaffolding
        from tests.agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _dropped_tool_call_response("Let me verify the PR."),
            _mock_response(content="All checks pass. Approved.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("review the PR")

        assert result["completed"] is True
        # The finalization pop strips the answered pair from the live list —
        # no flagged scaffolding may survive into the returned transcript.
        leftover = [
            m for m in result["messages"]
            if isinstance(m, dict) and m.get("_dropped_toolcall_nudge")
        ]
        assert not leftover, (
            "The re-prompt pair must be stripped at finalization, not kept "
            "in the returned transcript."
        )
        # And the persistence filter must classify the flag as ephemeral so a
        # mid-turn flush can never write the pair to the durable store either.
        assert _is_ephemeral_scaffolding(
            {"role": "user", "content": "nudge", "_dropped_toolcall_nudge": True}
        ), (
            "_dropped_toolcall_nudge messages must be classified as "
            "ephemeral scaffolding so they are never persisted."
        )



def _text_channel_call_response(content: str):
    """A finish_reason=stop response whose content tail is serialized tool-call XML.

    The #103483 shape: the model's next native call was serialized onto the text
    channel instead of the tool_calls channel, so the parsed tool_calls array is
    empty and the raw XML sits in content."""
    from tests.agent.test_run_agent import _mock_assistant_msg
    return SimpleNamespace(
        id="chatcmpl-textchannel",
        model="test/model",
        choices=[SimpleNamespace(
            index=0,
            message=_mock_assistant_msg(content=content, tool_calls=None),
            finish_reason="stop",
        )],
        usage=None,
    )


# The field shape: a prefix the model meant as narration, then the serialized call.
# Stripping the XML leaves the prefix non-empty, which is why the empty-response
# ladder never fired and the turn reported success with no call run.
_TEXT_CHANNEL_SHAPE = (
    "redox=default.hermes_search_files Hollywood"
    "<atem:function_calls>\n"
    '<atem:invoke name="default.hermes_search_files">\n'
    '<atem:parameter name="pattern">Hollywood</atem:parameter>\n'
    "</atem:invoke>\n"
    "</atem:function_calls>"
)


class TestTextChannelToolCallRecovery:
    def test_xml_tail_reprompts_instead_of_exiting(self, loop_agent):
        """A stop whose content ends in tool-call XML must re-prompt (the call never
        became a function_call item), not deliver the leftover prefix as the answer."""
        from tests.agent.test_run_agent import _mock_response

        # Recovery needs a callable tool: with no tools a closer-like tail is prose.
        loop_agent.valid_tool_names = {"default.hermes_search_files"}
        loop_agent.client.chat.completions.create.side_effect = [
            _text_channel_call_response(_TEXT_CHANNEL_SHAPE),
            _mock_response(content="Searched; here are the results.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("find Hollywood references")

        assert loop_agent.client.chat.completions.create.call_count == 2, (
            "A text-channel tool-call serialization must trigger a re-prompt (second "
            "API call), not exit with the leftover prefix as the final answer."
        )
        second_call = loop_agent.client.chat.completions.create.call_args_list[1]
        msgs = second_call.kwargs.get("messages") or second_call.args[0].get("messages")
        last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        assert last_user is not None
        assert "tool call" in (last_user.get("content") or "").lower()
        assert "Searched; here are the results." in result["final_response"]

    def test_xml_tail_shape_is_bounded(self, loop_agent):
        """A model that keeps serializing the call must be re-prompted a bounded
        number of times, then the turn ends (no infinite loop)."""
        from tests.agent.test_run_agent import _mock_response

        # Recovery needs a callable tool: with no tools a closer-like tail is prose.
        loop_agent.valid_tool_names = {"default.hermes_search_files"}
        loop_agent.client.chat.completions.create.side_effect = [
            _text_channel_call_response(_TEXT_CHANNEL_SHAPE) for _ in range(9)
        ] + [_mock_response(content="done", finish_reason="stop")]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("find it")

        assert loop_agent.client.chat.completions.create.call_count <= 4
        assert result is not None

    def test_prose_ends_after_the_xml_is_unaffected(self, loop_agent):
        """XML mid-text followed by real closing prose is a genuine answer — the
        trigger is tail-anchored and must not fire."""
        from tests.agent.test_run_agent import _mock_response

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(
                content="The pattern <tool_calls> is what we strip. Rest of the answer.",
                finish_reason="stop",
            ),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("explain")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert "Rest of the answer." in result["final_response"]


# A cut outer block: the opener never gets its closer, so the tail patterns alone
# return false and the leftover prefix would be accepted as the final answer.
_TEXT_CHANNEL_NESTED_CUT = (
    "Waiting.\n<atem:function_calls>{}\n<atem:invoke>partial"
)
_TEXT_CHANNEL_CLOSER_CUT = (
    "Waiting.\n<atem:function_calls>{}\n</atem:function_"
)


class TestUnterminatedOuterBlockRecovery:
    """A stop cut after nested content, or inside the outer closer, still recovers the
    lost call — while a closed block followed by prose stays a genuine answer."""

    def test_cut_after_nested_content_reprompts(self, loop_agent):
        from tests.agent.test_run_agent import _mock_response

        loop_agent.valid_tool_names = {"default.hermes_search_files"}
        loop_agent.client.chat.completions.create.side_effect = [
            _text_channel_call_response(_TEXT_CHANNEL_NESTED_CUT),
            _mock_response(content="Searched; here are the results.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("find Hollywood references")

        assert loop_agent.client.chat.completions.create.call_count == 2, (
            "A cut outer block must trigger a re-prompt (second API call), "
            "not exit with the leftover prefix as the final answer."
        )
        assert "Searched; here are the results." in result["final_response"]

    def test_cut_inside_outer_closer_reprompts(self, loop_agent):
        from tests.agent.test_run_agent import _mock_response

        loop_agent.valid_tool_names = {"default.hermes_search_files"}
        loop_agent.client.chat.completions.create.side_effect = [
            _text_channel_call_response(_TEXT_CHANNEL_CLOSER_CUT),
            _mock_response(content="Searched; here are the results.", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("find Hollywood references")

        assert loop_agent.client.chat.completions.create.call_count == 2, (
            "A cut inside the outer closer must trigger a re-prompt (second API call), "
            "not exit with the leftover prefix as the final answer."
        )
        assert "Searched; here are the results." in result["final_response"]

    def test_closed_block_followed_by_prose_is_unaffected(self, loop_agent):
        from tests.agent.test_run_agent import _mock_response

        loop_agent.valid_tool_names = {"default.hermes_search_files"}
        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(
                content="<atem:function_calls>x</atem:function_calls> trailing prose here",
                finish_reason="stop",
            ),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("explain")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert "trailing prose here" in result["final_response"]


class TestRecoveryGatedOnCallableTools:
    """With no callable tools, closer-like prose is a genuine answer: one API call,
    no re-prompt, no truncation of the turn."""

    def test_closer_like_prose_without_tools_is_delivered(self, loop_agent):
        from tests.agent.test_run_agent import _mock_response

        assert not loop_agent.valid_tool_names
        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content="To close it, emit </function>", finish_reason="stop"),
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("explain")

        assert loop_agent.client.chat.completions.create.call_count == 1, (
            "With no callable tools the recovery must not fire on closer-like prose."
        )
        assert "To close it, emit" in result["final_response"]
