"""Attributing the events a lifted harness toolset emits.

Several `pydantic_ai_harness` capabilities emit `CapabilityEvent`s from inside their tool
functions, and this codebase registers those toolsets *directly* rather than registering the
capabilities that own them — so the tools stay inside the namespaced, operator-toggleable
catalog. Pydantic AI refuses a capability event it cannot attribute to a
registered capability, which makes that choice load-bearing in a way nothing announces: get
it wrong and the failure is not a boot error but a `UserError` mid-turn, on the first
mutation, in whichever category was lifted.

These are the three properties that keep it working.
"""

from __future__ import annotations

import pytest
from pydantic_ai.models.test import TestModel

from agent.factory import build_agent
from tools import build_agent_toolsets, core_categories, harness_events_capability


async def test_a_turn_that_mutates_the_task_list_does_not_die_attributing_its_events():
    """The end-to-end property, asserted the way it actually broke: a turn on the agent the
    engine really builds, calling the lifted planning tools, must not raise.

    `TestModel` calls every tool it is offered, so the task-list mutation — the thing that
    emits — happens for free. Attribution needs the owner registered on the agent *and* the
    namespaced name restated on the way into the harness; drop either and this turn ends in
    a `UserError` rather than an answer."""
    from agent import stream_agent_run
    from runs import Run, RunStream, ToolStarted
    from tools import RunDeps

    agent = build_agent(TestModel(custom_output_text="ok"), categories=core_categories())
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    async with agent.iter("go", deps=RunDeps(run=run, owner_id="operator")) as agent_run:
        await stream_agent_run(agent_run, run)

    started = [e.body.name for e in run.stream.replay() if isinstance(e.body, ToolStarted)]
    assert any(name.startswith("tasks_") for name in started), (
        "no task tool ran, so this turn never exercised the emit path it exists to cover"
    )


async def test_without_a_registered_owner_the_same_turn_fails():
    """The negative half, so the test above cannot pass for the wrong reason. Identical
    toolsets, identical model, no owner registered — and the emit the harness performs has
    nothing to attribute to, so the turn dies where it used to."""
    from pydantic_ai import Agent, DeferredToolRequests
    from pydantic_ai.exceptions import UserError

    from agent import stream_agent_run
    from runs import Run, RunStream
    from tools import RunDeps

    agent = Agent(
        TestModel(custom_output_text="ok"),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets(core_categories()),
        output_type=[str, DeferredToolRequests],
    )
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    with pytest.raises(UserError, match="Capability events belong to capabilities"):
        async with agent.iter("go", deps=RunDeps(run=run, owner_id="operator")) as agent_run:
            await stream_agent_run(agent_run, run)


def test_the_owner_never_defers_loading():
    """The stamp goes on *every* tool in the catalog, which is only safe while the owner
    does not defer: both places `capability_id` gates a tool's availability require
    `defer_loading is True`, so an owner that deferred would hide the whole catalog behind a
    capability the model is never told to load."""
    assert harness_events_capability().defer_loading is not True


async def test_every_offered_tool_names_an_owner():
    """A lifted toolset whose events nobody owns does not fail at assembly — it fails
    mid-turn, on the first mutation. Stamping the whole catalog is what makes that
    impossible to forget, so this asserts the stamp actually reaches every tool rather
    than only the ones someone remembered."""
    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import RunUsage

    from runs import Run, RunStream
    from tools import RunDeps

    toolset = build_agent_toolsets(core_categories())[0]
    run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(run=run, owner_id="operator")
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    tools = await toolset.get_tools(ctx)
    assert tools, "no tools resolved — the walk is broken, not clean"
    unowned = sorted(name for name, tool in tools.items() if tool.tool_def.capability_id is None)
    assert not unowned, f"these tools name no owning capability: {unowned}"
