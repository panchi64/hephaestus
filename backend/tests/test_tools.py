"""The toolset access-policy stack: namespacing, the enabled gate, deps reach."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic_ai import Agent, DeferredToolRequests, FunctionToolset, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset

from agent import stream_agent_run
from runs import Run, RunStream
from tools import RunDeps, build_agent_toolsets, harness_events_capability


def _run() -> Run:
    return Run(id="t", kind="chat", owner_id="operator", stream=RunStream())


async def _run_agent(
    *,
    disabled: frozenset[str] | None = None,
    categories: Mapping[str, AbstractToolset[RunDeps]] | None = None,
) -> Run:
    agent = Agent(
        TestModel(custom_output_text="ok"),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets(categories),
        # The default catalog includes an approval-gated tool (host exec); accept
        # DeferredToolRequests like the real engine so it defers instead of erroring.
        output_type=[str, DeferredToolRequests],
        # Registered like the real engine does (`agent/factory.py`): the `tasks` category
        # carries the harness planning tools, which emit capability events, and the library
        # refuses one it cannot attribute to a registered capability.
        capabilities=[harness_events_capability()],
    )
    run = _run()
    deps = RunDeps(run=run, owner_id="operator", disabled_tools=disabled or frozenset())
    async with agent.iter("go", deps=deps) as agent_run:
        await stream_agent_run(agent_run, run)
    return run


def _tool_names(run: Run) -> list[str]:
    return [e.body.name for e in run.stream.replay() if e.body.type == "tool.started"]


async def test_builtin_tool_is_namespaced_and_invoked():
    names = _tool_names(await _run_agent())
    assert any("builtin" in n and "now" in n for n in names)


async def test_disabled_tool_is_not_offered():
    name = next(n for n in _tool_names(await _run_agent()) if "now" in n)
    run = await _run_agent(disabled=frozenset({name}))
    assert name not in _tool_names(run)


async def test_deps_reach_tools():
    seen: dict[str, str] = {}
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(metadata={"sensitivity": "read"})
    def whoami(ctx: RunContext[RunDeps]) -> str:
        seen["owner"] = ctx.deps.owner_id
        return ctx.deps.owner_id

    _ = await _run_agent(categories={"id": toolset})
    assert seen.get("owner") == "operator"
