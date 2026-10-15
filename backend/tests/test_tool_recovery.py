"""A tool that raises leaves the run going: the model reads the failure and carries on."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import override

from pydantic_ai import Agent, DeferredToolRequests, ModelRetry, RunContext
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.toolsets import FunctionToolset, ToolsetTool

from agent import stream_agent_run
from core.diagnostics import is_platform_failure, render_tool_error, suggest
from core.exceptions import DegradedCapabilityError
from runs import Run, RunStream, ToolFailed
from services.conversation_view import project_tree
from tools import RunDeps, build_agent_toolsets
from tools.recover import describe_failure

from ._helpers import bodies_of

OWNER = "operator"


def _boom_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain
    async def explode() -> str:
        raise RuntimeError("the index is corrupt")

    @toolset.tool_plain
    async def degraded() -> str:
        raise DegradedCapabilityError("search is down", hint="answer from what you know")

    @toolset.tool_plain
    async def stall() -> str:
        await asyncio.sleep(5)
        return "late"

    @toolset.tool_plain
    async def picky() -> str:
        raise ModelRetry("pass a smaller window")

    @toolset.tool_plain
    async def distill() -> str:
        raise ModelHTTPError(400, "utility", body="context length exceeded")

    @toolset.tool_plain
    async def build(timeout_seconds: float) -> str:
        await asyncio.sleep(0.1)
        return f"built within {timeout_seconds}s"

    return toolset


class _Down(FunctionToolset[RunDeps]):
    """A category whose server went away: listing its tools fails."""

    @override
    async def get_tools(self, ctx: RunContext[RunDeps]) -> dict[str, ToolsetTool[RunDeps]]:
        raise ConnectionError("the MCP server is gone")


def _results(messages: list[ModelMessage]) -> list[ToolReturnPart | RetryPromptPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart | RetryPromptPart)
    ]


def _calls_then_answers(tool: str, args: dict[str, object]):
    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if _results(messages):
            yield "carried on"
        else:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args))}

    return stream_fn


async def _drive(
    tool: str,
    *,
    call_timeout_s: float | None = None,
    args: dict[str, object] | None = None,
    extra: dict[str, FunctionToolset[RunDeps]] | None = None,
) -> tuple[str, list[ModelMessage], Run]:
    categories = {"boom": _boom_toolset(), **(extra or {})}
    agent = Agent(
        FunctionModel(stream_function=_calls_then_answers(tool, args or {})),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets(categories, call_timeout_s=call_timeout_s),
        output_type=[str, DeferredToolRequests],
    )
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    # Yolo, so the gate offers these unclassified test tools instead of parking them.
    deps = RunDeps(run=run, owner_id=OWNER, permission="yolo")
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)
    assert agent_run.result is not None
    output = agent_run.result.output
    assert isinstance(output, str)
    return output, agent_run.result.all_messages(), run


async def test_an_unexpected_exception_reaches_the_model_and_the_turn_finishes() -> None:
    answer, messages, run = await _drive("boom_explode")
    assert answer == "carried on"
    [result] = _results(messages)
    assert isinstance(result, ToolReturnPart)
    assert result.outcome == "failed"
    text = str(result.content)
    assert text.startswith("error[internal]: boom_explode failed unexpectedly: RuntimeError")
    assert "the index is corrupt" in text
    [failed] = bodies_of(run, ToolFailed)
    assert failed.platform
    assert failed.name == "boom_explode"


async def test_a_reload_shows_the_same_failed_call_the_stream_did() -> None:
    _, messages, _ = await _drive("boom_explode")
    views = project_tree([(f"n{index}", message) for index, message in enumerate(messages)])
    [tool] = [tool for view in views for tool in view.tools]
    assert tool.status == "error"
    assert tool.platform
    assert tool.error is not None and "the index is corrupt" in tool.error


async def test_a_domain_error_is_the_environment_and_carries_its_hint() -> None:
    _, messages, run = await _drive("boom_degraded")
    [result] = _results(messages)
    text = str(result.content)
    assert text.startswith("error: boom_degraded could not finish: search is down")
    assert "help: answer from what you know" in text
    [failed] = bodies_of(run, ToolFailed)
    assert not failed.platform


async def test_a_call_past_its_timeout_is_stopped_and_reported() -> None:
    answer, messages, _ = await _drive("boom_stall", call_timeout_s=0.05)
    assert answer == "carried on"
    [result] = _results(messages)
    assert "boom_stall was stopped after 0s unfinished" in str(result.content)


async def test_a_sub_models_error_inside_a_tool_is_contained() -> None:
    # A utility model refusing a distill is not the chat model overflowing its window, and
    # read as one it would fold the operator's thread for nothing.
    answer, messages, _ = await _drive("boom_distill")
    assert answer == "carried on"
    [result] = _results(messages)
    assert "context length exceeded" in str(result.content)


async def test_a_call_asking_for_longer_is_given_it() -> None:
    answer, messages, _ = await _drive(
        "boom_build", call_timeout_s=0.05, args={"timeout_seconds": 1.0}
    )
    assert answer == "carried on"
    [result] = _results(messages)
    assert "built within 1.0s" in str(result.content)


async def test_a_category_that_cannot_list_its_tools_does_not_end_the_run() -> None:
    answer, _, _ = await _drive("boom_explode", extra={"down": _Down()})
    assert answer == "carried on"


async def test_a_retry_request_still_goes_back_as_a_retry() -> None:
    _, messages, _ = await _drive("boom_picky")
    assert any(isinstance(part, RetryPromptPart) for part in _results(messages))


def test_the_renderer_reads_like_a_compiler_diagnostic() -> None:
    text = render_tool_error(
        "no file named notes.md", cause="the path is relative", hints=["list ~ first"], code="x"
    )
    assert text == (
        "error[x]: no file named notes.md\n  cause: the path is relative\n  help: list ~ first"
    )
    assert not is_platform_failure(text)
    assert is_platform_failure(describe_failure("t", ValueError("bad")))


def test_suggest_names_the_closest_candidate_or_nothing() -> None:
    assert suggest("web_serch", ["web_search", "web_fetch"]) == "did you mean `web_search`?"
    assert suggest("calendar", ["web_search"]) is None
