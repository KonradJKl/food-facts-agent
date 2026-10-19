import json
import logging
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaTextBlock,
    BetaToolResultBlockParam,
    BetaToolUseBlock,
)

from food_facts_agent.off_client import OFFClient
from food_facts_agent.tools import TOOLS, run_tool

logger = logging.getLogger(__name__)

MAX_STEPS = 8
MAX_TOKENS = 16000
EFFORT = "medium"
# Lets the API answer a request that a safety classifier declined with another model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
REFUSAL_ANSWER = "The model declined to answer this question."


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    content: str
    is_error: bool


@dataclass
class AgentResult:
    answer: str
    stop_reason: str | None
    tool_calls: list[ToolCall]
    steps: int


def run_agent(
    question: str,
    *,
    llm: Anthropic,
    off: OFFClient,
    model: str,
    system: str,
    max_steps: int = MAX_STEPS,
) -> AgentResult:
    messages: list[BetaMessageParam] = [{"role": "user", "content": question}]
    tool_calls: list[ToolCall] = []
    for step in range(1, max_steps + 1):
        response = llm.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},
            fallbacks="default",
            betas=[FALLBACK_BETA],
        )
        usage = response.usage
        logger.info(
            "Step %d: %s, input tokens %d, cache read %s, cache write %s, output tokens %d",
            step,
            response.stop_reason,
            usage.input_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
            usage.output_tokens,
        )
        if response.stop_reason == "refusal":
            # Either the fallback model declined too, or no fallback ran: it was busy, or
            # the category is never retried. recommended_model is set in the busy case.
            details = response.stop_details
            logger.warning(
                "Request declined, category %s, recommended model %s",
                details and details.category,
                details and details.recommended_model,
            )
            return AgentResult(REFUSAL_ANSWER, "refusal", tool_calls, step)

        # The history is only ever appended to: thinking blocks become invalid if an
        # earlier message, the system prompt or the tools change.
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return AgentResult(_text(response), response.stop_reason, tool_calls, step)
        if step == max_steps:
            # The model would never see these results; don't use up Open Food Facts requests.
            break

        results: list[BetaToolResultBlockParam] = []
        for block in response.content:
            if isinstance(block, BetaToolUseBlock):
                content, is_error = run_tool(off, block.name, block.input)
                logger.info(
                    "Tool %s %s -> %s",
                    block.name,
                    json.dumps(block.input, ensure_ascii=False),
                    content if is_error else f"ok, {len(content)} characters",
                )
                tool_calls.append(ToolCall(block.name, block.input, content, is_error))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
        messages.append({"role": "user", "content": results})

    logger.warning("No final answer after %d steps", max_steps)
    return AgentResult(
        f"Stopped after {max_steps} steps without a final answer.",
        "max_steps",
        tool_calls,
        max_steps,
    )


def _text(response: BetaMessage) -> str:
    # Only text blocks form the answer; thinking blocks are left out.
    return "".join(block.text for block in response.content if isinstance(block, BetaTextBlock))
