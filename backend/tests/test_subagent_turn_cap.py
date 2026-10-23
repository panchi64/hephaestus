"""A spawned agent's request cap and the turn its file changes are filed under."""

from __future__ import annotations

import asyncio
import itertools
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
from routes.deps import app_state
from runs import Run, RunEnded, RunStatus
from runs.run import StopCause
from services.agents import AgentDefinition
from services.home.backend import Mount
from services.home.changes import ThreadChanges
from services.home.paths import Place
from services.home.rewind import Rewinder
from services.registry import ModelRegistry
from services.subagents import SubagentParent
from tools import RunDeps
from tools.home import change_log, ledger_prompt

from ._helpers import client_app, register_stub_provider, stub_resolution, swap_tool_catalog
from .test_subagents_launch import launcher_of, records_of, settle_run

OWNER = "operator"
TASK = "capped task"


async def _stored_agent(app: FastAPI, cap: int) -> AgentDefinition:
    """A published agent, since a wake reads its definition back from the catalog."""
    store = app_state(app).agent_store
    existing = await store.get_by_name(OWNER, "capped")
    if existing is not None:
        return existing
    return await store.create(
        OWNER,
        {
            "name": "capped",
            "description": "a capped worker",
            "max_requests": cap,
            "can_delegate": True,
        },
        published=True,
    )


def _toolset(root: Path) -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    names = itertools.count()

    @toolset.tool(metadata={"sensitivity": "read"})
    async def step(ctx: RunContext[RunDeps], n: int) -> str:
        log = change_log(ctx.deps)
        assert log is not None
        mount = Mount(Place("projects", "api"), root, writable=True, project_id="p1")
        name = f"made-{next(names)}.txt"
        await log.before(mount, [name])
        _ = (root / name).write_text(f"step {n}")
        await log.after(mount, [name])
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
    run.prompt_message_id = "T1"
    return conversation, run, release


def _deps_of(run: Run) -> RunDeps:
    return RunDeps(run=run, owner_id=OWNER, conversation_id=run.conversation_id)


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

        queued = app_state(app).runs.inbox(parent, OWNER).messages
        assert any("did not finish" in m.text and "request limit" in m.text for m in queued)
        release.set()
        await _quiet(app)

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


async def _filed(app: FastAPI, root: Path, thread: str, prompt: str) -> set[str]:
    """The files a rewind of ``prompt`` in ``thread`` would delete."""
    changes = app_state(app).capabilities.get_optional(ThreadChanges)
    assert changes is not None
    plan = await Rewinder(changes).plan(OWNER, thread, [prompt], {"p1": root})
    return {f.path for f in plan.delete}


async def _spawn(app: FastAPI, parent: SubagentParent, handle: str, cap: int = 1):
    started = await launcher_of(app).launch(
        OWNER, await _stored_agent(app, cap), TASK, handle=handle, parent=parent
    )
    await settle_run(app, started.run_id)
    return started


async def test_a_childs_file_edit_is_filed_under_the_launching_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_model(monkeypatch)
    async with client_app() as (_client, app):
        swap_tool_catalog(app, {"work": _toolset(tmp_path)})
        parent, parent_run, release = await _parent(app)

        started = await _spawn(
            app,
            SubagentParent(
                conversation_id=parent,
                run_id=parent_run.id,
                ledger_prompt_message_id=ledger_prompt(_deps_of(parent_run)),
            ),
            "editor",
        )

        assert len(await _filed(app, tmp_path, parent, "T1")) == 1
        child_run = app_state(app).runs.get(started.run_id)
        assert child_run is not None and child_run.prompt_message_id is not None
        assert await _filed(app, tmp_path, parent, child_run.prompt_message_id) == set()
        row = await records_of(app).by_child(started.conversation_id)
        assert row is not None and row.ledger_prompt_message_id == "T1"
        release.set()
        await _quiet(app)


async def test_a_wake_files_under_the_senders_turn_or_else_the_launching_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_model(monkeypatch)
    async with client_app() as (_client, app):
        swap_tool_catalog(app, {"work": _toolset(tmp_path)})
        parent, parent_run, release = await _parent(app)
        started = await _spawn(
            app,
            SubagentParent(
                conversation_id=parent, run_id=parent_run.id, ledger_prompt_message_id="T1"
            ),
            "editor",
        )
        first = await _filed(app, tmp_path, parent, "T1")
        assert len(first) == 1

        # The root has moved on to an unrelated turn. A delivery that names no sending
        # turn does not file under it.
        parent_run.prompt_message_id = "T2"
        release.set()
        _ = await launcher_of(app).message(OWNER, parent, "editor", "again")
        await _quiet(app)
        second = await _filed(app, tmp_path, parent, "T1")
        assert len(second) == 2
        assert await _filed(app, tmp_path, parent, "T2") == set()

        _ = await launcher_of(app).message(
            OWNER,
            parent,
            "editor",
            "and again",
            ledger_prompt_message_id="T2",
            ledger_thread=parent,
        )
        await _quiet(app)
        assert len(await _filed(app, tmp_path, parent, "T2")) == 1
        assert await _filed(app, tmp_path, parent, "T1") == second
        assert started.conversation_id


async def test_a_grandchild_spawned_while_the_root_is_idle_files_under_the_first_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_model(monkeypatch)
    async with client_app() as (_client, app):
        swap_tool_catalog(app, {"work": _toolset(tmp_path)})
        parent, parent_run, release = await _parent(app)
        child = await _spawn(
            app,
            SubagentParent(
                conversation_id=parent, run_id=parent_run.id, ledger_prompt_message_id="T1"
            ),
            "child",
        )
        release.set()
        await _quiet(app)
        row = await records_of(app).by_child(child.conversation_id)
        child_run = app_state(app).runs.get(child.run_id)
        assert row is not None and child_run is not None
        deps = RunDeps(
            run=child_run,
            owner_id=OWNER,
            conversation_id=child.conversation_id,
            workspace_key=row.workspace_key or "",
            ledger_prompt_message_id=row.ledger_prompt_message_id,
        )

        _ = await _spawn(
            app,
            SubagentParent(
                conversation_id=child.conversation_id,
                workspace_key=row.workspace_key,
                ledger_prompt_message_id=ledger_prompt(deps),
            ),
            "grandchild",
            cap=1,
        )

        assert len(await _filed(app, tmp_path, parent, "T1")) == 2


async def test_a_message_from_an_own_workspace_agent_does_not_file_in_another_threads_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_model(monkeypatch)
    async with client_app() as (_client, app):
        swap_tool_catalog(app, {"work": _toolset(tmp_path)})
        parent, parent_run, release = await _parent(app)
        shared = SubagentParent(
            conversation_id=parent, run_id=parent_run.id, ledger_prompt_message_id="T1"
        )
        _ = await _spawn(app, shared, "editor")
        loner_def = await app_state(app).agent_store.create(
            OWNER,
            {"name": "loner", "description": "alone", "max_requests": 1, "workspace": "own"},
            published=True,
        )
        loner = await launcher_of(app).launch(OWNER, loner_def, TASK, handle="loner", parent=shared)
        await settle_run(app, loner.run_id)
        loner_run = app_state(app).runs.get(loner.run_id)
        assert loner_run is not None and loner_run.prompt_message_id
        release.set()
        await _quiet(app)
        before = await _filed(app, tmp_path, parent, "T1")

        # The loner files in its own thread's ledger, so its prompt means nothing in the
        # root's: the editor's wake files under the launching turn.
        _ = await launcher_of(app).message(
            OWNER,
            loner.conversation_id,
            "editor",
            "go on",
            ledger_prompt_message_id=loner_run.prompt_message_id,
            ledger_thread=loner.conversation_id,
        )
        await _quiet(app)

        assert len(await _filed(app, tmp_path, parent, "T1")) == len(before) + 1
        assert await _filed(app, tmp_path, parent, loner_run.prompt_message_id) == set()


async def test_an_own_workspace_child_files_under_its_own_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_model(monkeypatch)
    async with client_app() as (_client, app):
        swap_tool_catalog(app, {"work": _toolset(tmp_path)})
        parent, parent_run, release = await _parent(app)
        agent = await app_state(app).agent_store.create(
            OWNER,
            {"name": "loner", "description": "works alone", "max_requests": 1, "workspace": "own"},
            published=True,
        )
        started = await launcher_of(app).launch(
            OWNER,
            agent,
            TASK,
            handle="loner",
            parent=SubagentParent(
                conversation_id=parent, run_id=parent_run.id, ledger_prompt_message_id="T1"
            ),
        )
        await settle_run(app, started.run_id)

        child_run = app_state(app).runs.get(started.run_id)
        assert child_run is not None and child_run.prompt_message_id is not None
        assert await _filed(app, tmp_path, parent, "T1") == set()
        assert (
            len(await _filed(app, tmp_path, started.conversation_id, child_run.prompt_message_id))
            == 1
        )
        release.set()
        await _quiet(app)
