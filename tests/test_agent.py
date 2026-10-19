import copy
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from anthropic import Anthropic, DefaultHttpxClient
from anthropic.types.beta import (
    BetaMessage,
    BetaTextBlock,
    BetaThinkingBlock,
    BetaToolUseBlock,
    BetaUsage,
)

from food_facts_agent import agent
from food_facts_agent.tools import TOOLS

QUESTION = "Enthält Nutella Palmöl?"
UNKNOWN_BARCODE = "4099999999994"
OFF = object()


class FakeLLM:
    """Stands in for anthropic.Anthropic and replays prepared responses."""

    def __init__(self, *responses: BetaMessage) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> BetaMessage:
        # run_agent keeps appending to the same list, so store a snapshot.
        self.requests.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def reply(
    *content: Any, stop_reason: str = "end_turn", usage: BetaUsage | None = None, **fields: Any
) -> BetaMessage:
    return BetaMessage(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        content=list(content),
        stop_reason=stop_reason,
        usage=usage or BetaUsage(input_tokens=100, output_tokens=20),
        **fields,
    )


def text(value: str) -> BetaTextBlock:
    return BetaTextBlock(type="text", text=value)


def tool_use(block_id: str, name: str, **tool_input: Any) -> BetaToolUseBlock:
    return BetaToolUseBlock(type="tool_use", id=block_id, name=name, input=tool_input)


@pytest.fixture
def tool_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replaces run_tool in the agent module and records the calls."""
    runs: list[tuple[str, dict]] = []

    def fake_run_tool(off: object, name: str, tool_input: dict) -> tuple[str, bool]:
        assert off is OFF
        runs.append((name, tool_input))
        if tool_input.get("barcode") == UNKNOWN_BARCODE:
            return '{"error": "unknown barcode"}', True
        return f'{{"tool": "{name}"}}', False

    monkeypatch.setattr(agent, "run_tool", fake_run_tool)
    return runs


def ask(llm: FakeLLM | Anthropic, **kwargs: Any) -> agent.AgentResult:
    return agent.run_agent(
        QUESTION, llm=llm, off=OFF, model="claude-opus-5-5", system="Test prompt", **kwargs
    )


def test_answer_without_tools_sends_one_request(tool_runs: list) -> None:
    llm = FakeLLM(reply(text("Nutella enthält Palmöl.")))

    result = ask(llm)

    assert result == agent.AgentResult("Nutella enthält Palmöl.", "end_turn", [], 1)
    assert tool_runs == []
    assert llm.requests == [
        {
            "model": "claude-opus-5-5",
            "max_tokens": agent.MAX_TOKENS,
            "system": [
                {"type": "text", "text": "Test prompt", "cache_control": {"type": "ephemeral"}}
            ],
            "tools": TOOLS,
            "messages": [{"role": "user", "content": QUESTION}],
            "output_config": {"effort": "medium"},
            "cache_control": {"type": "ephemeral"},
            "fallbacks": "default",
            "betas": [agent.FALLBACK_BETA],
        }
    ]


def test_tool_result_goes_back_with_matching_id(tool_runs: list) -> None:
    first = reply(
        tool_use("toolu_1", "get_product", barcode="3017624010701"), stop_reason="tool_use"
    )
    llm = FakeLLM(first, reply(text("Ja.")))

    result = ask(llm)

    assert tool_runs == [("get_product", {"barcode": "3017624010701"})]
    assert llm.requests[1]["messages"][1:] == [
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"tool": "get_product"}',
                    "is_error": False,
                }
            ],
        },
    ]
    assert result.answer == "Ja."
    assert result.steps == 2
    assert result.tool_calls == [
        agent.ToolCall(
            "get_product", {"barcode": "3017624010701"}, '{"tool": "get_product"}', False
        )
    ]


def test_parallel_tool_calls_are_answered_in_one_message(tool_runs: list) -> None:
    first = reply(
        tool_use("toolu_1", "get_product", barcode="3017624010701"),
        tool_use("toolu_2", "get_product", barcode="4251680801722"),
        stop_reason="tool_use",
    )
    llm = FakeLLM(first, reply(text("Fertig.")))

    ask(llm)

    last_message = llm.requests[1]["messages"][-1]
    assert last_message["role"] == "user"
    assert [result["tool_use_id"] for result in last_message["content"]] == ["toolu_1", "toolu_2"]


def test_tool_errors_are_marked_for_the_model(tool_runs: list) -> None:
    first = reply(
        tool_use("toolu_1", "get_product", barcode=UNKNOWN_BARCODE), stop_reason="tool_use"
    )
    llm = FakeLLM(first, reply(text("Unbekannter Barcode.")))

    result = ask(llm)

    tool_result = llm.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert result.tool_calls[0].is_error is True


def test_history_is_only_appended_to(tool_runs: list) -> None:
    llm = FakeLLM(
        reply(tool_use("toolu_1", "search_products", query="Müsli"), stop_reason="tool_use"),
        reply(tool_use("toolu_2", "get_product", barcode="4260188784710"), stop_reason="tool_use"),
        reply(text("Fertig.")),
    )

    ask(llm)

    histories = [request["messages"] for request in llm.requests]
    for earlier, later in zip(histories, histories[1:], strict=False):
        assert later[: len(earlier)] == earlier
        assert len(later) == len(earlier) + 2


def test_answer_leaves_out_thinking_blocks(tool_runs: list) -> None:
    thinking = BetaThinkingBlock(type="thinking", thinking="", signature="sig")
    llm = FakeLLM(reply(thinking, text("Nutella "), text("enthält Palmöl.")))

    assert ask(llm).answer == "Nutella enthält Palmöl."


def test_stops_after_max_steps_without_running_the_last_tools(
    tool_runs: list, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLLM(
        *[
            reply(tool_use(f"toolu_{i}", "search_products", query="Müsli"), stop_reason="tool_use")
            for i in range(3)
        ]
    )

    result = ask(llm, max_steps=3)

    assert result.stop_reason == "max_steps"
    assert result.answer == "Stopped after 3 steps without a final answer."
    assert result.steps == 3
    assert len(llm.requests) == 3
    assert len(tool_runs) == len(result.tool_calls) == 2
    assert "No final answer after 3 steps" in caplog.text


def test_refusal_after_a_tool_round_keeps_tool_calls(
    tool_runs: list, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLLM(
        reply(tool_use("toolu_1", "get_product", barcode="3017624010701"), stop_reason="tool_use"),
        reply(
            text("Teilantwort"),
            stop_reason="refusal",
            stop_details={
                "type": "refusal",
                "category": "bio",
                "recommended_model": "claude-opus-4-8",
            },
        ),
    )

    result = ask(llm)

    assert result == agent.AgentResult(
        agent.REFUSAL_ANSWER,
        "refusal",
        [
            agent.ToolCall(
                "get_product", {"barcode": "3017624010701"}, '{"tool": "get_product"}', False
            )
        ],
        2,
    )
    assert "category bio, recommended model claude-opus-4-8" in caplog.text


def test_max_tokens_returns_partial_text_without_running_tools(tool_runs: list) -> None:
    llm = FakeLLM(
        reply(
            text("Nutella enthält"),
            tool_use("toolu_1", "get_product", barcode="3017624010701"),
            stop_reason="max_tokens",
        )
    )

    result = ask(llm)

    assert result.answer == "Nutella enthält"
    assert result.stop_reason == "max_tokens"
    assert tool_runs == []


def test_logs_each_step_and_tool_call(tool_runs: list, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="food_facts_agent.agent")
    first = reply(
        tool_use("toolu_1", "get_product", barcode="3017624010701"),
        tool_use("toolu_2", "get_product", barcode=UNKNOWN_BARCODE),
        stop_reason="tool_use",
        usage=BetaUsage(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=80,
            cache_creation_input_tokens=5,
        ),
    )
    llm = FakeLLM(first, reply(text("Ja.")))

    ask(llm)

    assert (
        "Step 1: tool_use, input tokens 100, cache read 80, cache write 5, output tokens 20"
        in caplog.text
    )
    assert 'Tool get_product {"barcode": "3017624010701"} -> ok' in caplog.text
    assert (
        f'Tool get_product {{"barcode": "{UNKNOWN_BARCODE}"}} -> {{"error": "unknown barcode"}}'
        in caplog.text
    )
    assert "Step 2: end_turn" in caplog.text


def api_reply(content: list[dict], stop_reason: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_sdk_builds_the_expected_http_requests(
    tool_runs: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs the real Anthropic client against a mocked transport instead of the API."""
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    replies = [
        api_reply(
            [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_product",
                    "input": {"barcode": "3017624010701"},
                },
            ],
            "tool_use",
        ),
        api_reply([{"type": "text", "text": "Ja."}], "end_turn"),
    ]
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=replies[len(requests) - 1])

    llm = Anthropic(
        api_key="test-key",
        base_url="https://api.anthropic.com",
        max_retries=0,
        http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )

    result = ask(llm)

    first, second = (json.loads(request.content) for request in requests)
    assert result.answer == "Ja."
    assert requests[0].url.path == "/v1/messages"
    assert requests[0].headers["anthropic-beta"] == agent.FALLBACK_BETA
    assert first["fallbacks"] == "default"
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["system"] == [
        {"type": "text", "text": "Test prompt", "cache_control": {"type": "ephemeral"}}
    ]
    assert first["output_config"] == {"effort": "medium"}
    assert [tool["strict"] for tool in first["tools"]] == [True, True]
    assert second["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig",
    }
    assert second["messages"][2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": '{"tool": "get_product"}',
            "is_error": False,
        }
    ]
