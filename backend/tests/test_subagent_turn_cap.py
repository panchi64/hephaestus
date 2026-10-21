"""A spawned agent's request cap and the turn its file changes are filed under."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from agent.turn import TURN_LIMIT_DETAIL
from harness.manifests._ledger_prompt import ledger_prompt_of
from routes.deps import app_state
from runs import Run, RunEnded, RunStatus
from runs.run import StopCause
from services.agents import AgentDefinition
from services.home.backend import Mount
from services.home.changes import ThreadChanges
from services.home.paths import Place
from services.registry import ModelRegistry
from services.subagents import SubagentParent
from tools import RunDeps
from tools.home import change_log

from ._helpers import client_app, register_stub_provider, stub_resolution, swap_tool_catalog
from .test_subagents_launch import launcher_of, records_of, settle_run

OWNER = "operator"
TASK = "capped task"


async def _stored_agent(app: FastAPI, cap: int) -> AgentDefinition:
    """A published agent, since a wake reads its definition back from the catalog."""
    return await app_state(app).agent_store.create(
        OWNER,
        {"name": "capped", "description": "a capped worker", "max_requests": cap},
        published=True,
    )


def _toolset(root: Path) -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(metadata={"sensitivity": "read"})
    async def step(ctx: RunContext[RunDeps], n: int) -> str:
        log = change_log(ctx.deps)
        assert log is not None
        mount = Mount(Place("projects", "api"), root, writable=True, project_id="p1")
        await log.before(mount, ["made.txt"])
        _ = (root / "made.txt").write_text(f"step {n}")
        await log.after(mount, ["made.txt"])
        return f"step {n} done"

    return toolset


def _first_prompt(messages: list[ModelMessage]) -> str:
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


def _titled(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    """The background (title) requests are not streamed."""
    return ModelResponse(parts=[TextPart("ok")])


def _install_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capped agent works forever; every other thread answers in one request."""

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if _first_prompt(messages) == TASK:
            yield {0: DeltaToolCall(name="work_step", json_args=f'{{"n": {len(messages)}}}')}
        else:
            yield "ok"

    register_stub_provider(monkeypatch)

    async def resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(_titled, stream_function=stream_fn))

    monkeypatch.setattr(ModelRegistry, "resolve_detailed", resolve_detailed)


async def _quiet(app: FastAPI) -> None:
    """Every run, and the hooks that react to it, has finished."""
    for _ in range(200):
        runs = app_state(app).runs.list()
        if all(run.is_terminal or run.is_parked for run in runs):
            for run in runs:
                await settle_run(app, run.id)
            return
        await asyncio.sleep(0.02)
    live = [(r.kind, r.status.value) for r in app_state(app).runs.list() if not r.is_terminal]
    raise AssertionError(f"runs never settled: {live}")


def _ended(run: Run) -> RunEnded:
    body = run.stream.replay()[-1].body
    assert isinstance(body, RunEnded)
    return body


async def _parent(app: FastAPI) -> tuple[str, Run, asyncio.Event]:
    """A thread with a turn in flight, standing in for the model that launches."""
    conversation = await app_state(app).conversations.create_conversation(OWNER, title="parent")
    release = asyncio.Event()

    async def orchestrate(_run: Run) -> None:
        _ = await release.wait()

    run = app_state(app).runs.submit(
        kind="chat", owner_id=OWNER, orchestrator=orchestrate, conversation_id=conversation
    )
    await asyncio.sleep(0.05)
    run.prompt_message_id = "parent-prompt"
    return conversation, run, release


async def test_a_capped_child_ends_blocked_and_its_wake_is_capped_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_model(monkeypatch)
    async with client_app() as (_client, app):
        swap_tool_catalog(app, {"work": _toolset(tmp_path)})
        parent, parent_run, release = await _parent(app)

        started = await launcher_of(app).launch(
            OWNER,
            await _stored_agent(app, 2),
            TASK,
            handle="worker-one",
            parent=SubagentParent(conversation_id=parent, run_id=parent_run.id),
        )
        await settle_run(app, started.run_id)
        first = app_state(app).runs.get(started.run_id)
        assert first is not None
        assert first.status is RunStatus.blocked
        assert first.stop_cause is StopCause.turn_limit
        assert _ended(first).code == "turn_limit"
        assert first.detail == TURN_LIMIT_DETAIL

        # The parent is told it did not finish, and why.
        queued = app_state(app).runs.inbox(parent, OWNER).messages
        assert any("did not finish" in m.text and "request limit" in m.text for m in queued)
        release.set()
        await _quiet(app)

        # A message that wakes it starts another turn, capped the same way.
        _ = await launcher_of(app).message(OWNER, parent, "worker-one", "carry on")
        await _quiet(app)
        runs = [
            r for r in app_state(app).runs.list() if r.conversation_id == started.conversation_id
        ]
        assert len(runs) == 2
        woken = max(runs, key=lambda r: r.created_at)
        assert woken.id != started.run_id
        assert woken.status is RunStatus.blocked
        assert woken.stop_cause is StopCause.turn_limit
        assert _ended(woken).code == "turn_limit"


async def test_a_childs_file_edit_is_filed_under_the_launching_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_model(monkeypatch)
    root = tmp_path / "project"
    root.mkdir()
    async with client_app() as (_client, app):
        swap_tool_catalog(app, {"work": _toolset(root)})
        parent, parent_run, release = await _parent(app)

        started = await launcher_of(app).launch(
            OWNER,
            await _stored_agent(app, 1),
            TASK,
            handle="editor",
            parent=SubagentParent(conversation_id=parent, run_id=parent_run.id),
        )
        await settle_run(app, started.run_id)

        changes = app_state(app).capabilities.get_optional(ThreadChanges)
        assert changes is not None
        plan = await changes.rewind_plan(OWNER, parent, ["parent-prompt"], {"p1": root})
        assert [f.path for f in plan.delete] == ["made.txt"]
        child_run = app_state(app).runs.get(started.run_id)
        assert child_run is not None and child_run.prompt_message_id is not None
        own = await changes.rewind_plan(OWNER, parent, [child_run.prompt_message_id], {"p1": root})
        assert own.delete == []

        # A grandchild resolves through its parent to the same launching turn.
        assert (
            await ledger_prompt_of(
                app_state(app).runs, records_of(app), OWNER, started.conversation_id
            )
            == "parent-prompt"
        )
        release.set()
        await _quiet(app)
