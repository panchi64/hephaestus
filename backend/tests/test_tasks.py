"""The agent's task list: sealed per-conversation persistence, an event on every mutation
— including the bulk replace the harness's own stores leave silent — and the rename that
keeps the harness's word for it out of the model's catalog.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
    StreamFunctionDef,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.planning import PlanItem as TaskItem
from pydantic_ai_harness.planning import TaskStatus
from sqlalchemy import Engine
from sqlmodel import Session, select

from agent import build_chat_orchestrator
from agent.open_tasks import open_tasks_notice
from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.vault import Vault
from models.task_list import ConversationTaskList
from runs import EventBody, Run, RunRegistry, RunStatus, RunStream, TasksUpdated
from services.conversations import ConversationBinding
from services.subagents import FiledReport, SubagentLauncher
from services.task_list import ConversationTasks, ConversationTaskStore
from tools import RunDeps, build_agent_toolsets, harness_events_capability
from tools.report import report_toolset
from tools.tasks import tasks_toolset

OWNER = "operator"


@pytest.fixture
async def vault(tmp_path: Path) -> Vault:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("correct horse battery staple")
    return vault


@pytest.fixture
def engine() -> Engine:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return engine


@pytest.fixture
def tasks(engine: Engine, vault: Vault) -> ConversationTasks:
    return ConversationTasks(engine, vault)


def _store(tasks: ConversationTasks, run: Run | None = None) -> ConversationTaskStore:
    return ConversationTaskStore(tasks, owner_id=OWNER, conversation_id="conv-1", run=run)


def _items(*contents: str) -> list[TaskItem]:
    return [
        TaskItem(id=f"t{i}", content=c, status=TaskStatus.pending) for i, c in enumerate(contents)
    ]


def _bodies(run: Run) -> list[EventBody]:
    return [e.body for e in run.stream.replay()]


async def _toolset_ctx(
    tasks: ConversationTasks | None, conversation_id: str | None
) -> tuple[AbstractToolset[RunDeps], RunContext[RunDeps]]:
    """One built toolset plus a run context over it — the shape every tool test here
    needs, and built the way the app builds it (once, shared) rather than per call."""
    toolset = build_agent_toolsets({"tasks": tasks_toolset()})[0]
    run = Run(id=f"r-{conversation_id or 'solo'}", kind="chat", owner_id=OWNER, stream=RunStream())
    caps = ServiceContainer()
    if tasks is not None:
        caps.add(tasks, as_type=ConversationTasks)
    deps = RunDeps(run=run, owner_id=OWNER, caps=caps, conversation_id=conversation_id)
    # Two fields make this stand in for a tool call *inside* a run, both because the
    # harness's planning tools emit capability events: `emit` refuses a context with no
    # event stream to emit into (a plain list is the shape the library carries), and it
    # refuses an event it cannot attribute to a capability. A real run attributes through
    # `tool_manager`, which a synthetic context has none of, so name the owner directly.
    ctx = RunContext(
        deps=deps,
        model=TestModel(),
        usage=RunUsage(),
        _event_stream_buffer=[],
        _capability=harness_events_capability(),
    )
    return toolset, ctx


async def test_tasks_round_trip_across_stores(tasks: ConversationTasks):
    await _store(tasks).set_items(_items("read the file", "fix the bug"))

    # A second store object is what a later turn (or a reload) gets — the list has to
    # come back from storage, not from anything held in memory.
    reloaded = await _store(tasks).get_items()
    assert [i.content for i in reloaded] == ["read the file", "fix the bug"]


async def test_the_bulk_replace_emits_too(tasks: ConversationTasks):
    run = Run(id="r", kind="chat", owner_id=OWNER, stream=RunStream())
    await _store(tasks, run).set_items(_items("one", "two"))

    # `write` is the tool a model reaches for first and is event-silent in the harness's
    # own stores; a panel built on events alone would sit empty through it.
    events = [b for b in _bodies(run) if isinstance(b, TasksUpdated)]
    assert len(events) == 1
    assert [i["content"] for i in events[0].items] == ["one", "two"]


async def test_each_granular_mutation_emits_the_whole_list(tasks: ConversationTasks):
    run = Run(id="r", kind="chat", owner_id=OWNER, stream=RunStream())
    store = _store(tasks, run)
    await store.set_items(_items("one", "two"))
    _ = await store.update_item("t0", status=TaskStatus.completed)
    _ = await store.remove_item("t1")

    events = [b for b in _bodies(run) if isinstance(b, TasksUpdated)]
    assert len(events) == 3
    # Full state every time, so applying an event on SSE replay is idempotent.
    assert [i["status"] for i in events[1].items] == ["completed", "pending"]
    assert [i["content"] for i in events[2].items] == ["one"]


async def test_concurrent_updates_do_not_lose_one(tasks: ConversationTasks):
    store = _store(tasks)
    await store.set_items(_items("one", "two", "three"))

    # A model can emit several task calls in one response, which Pydantic AI runs
    # concurrently. Each is a read-modify-write of the whole list, so without
    # serialization the later writer overwrites the earlier one's change.
    _ = await asyncio.gather(
        store.update_item("t0", status=TaskStatus.completed),
        store.update_item("t1", status=TaskStatus.in_progress),
        store.update_item("t2", status=TaskStatus.cancelled),
    )

    final = {i.id: i.status for i in await store.get_items()}
    assert final == {
        "t0": TaskStatus.completed,
        "t1": TaskStatus.in_progress,
        "t2": TaskStatus.cancelled,
    }


async def test_seeding_replaces_the_list_and_announces_it(tasks: ConversationTasks):
    """An approved plan's steps become the task list. It replaces rather than appends —
    the plan supersedes whatever the thread was tracking — and it goes out on the same
    event as a written one, so the panel cannot tell the two apart."""
    run = Run(id="r-seed", kind="chat", owner_id=OWNER, stream=RunStream())
    await _store(tasks).set_items(_items("stale", "older still"))

    seeded = await tasks.seed(OWNER, "conv-1", ["first step", "second step"], run=run)

    assert [i.content for i in seeded] == ["first step", "second step"]
    assert all(i.status is TaskStatus.pending for i in seeded)
    assert [i.content for i in await _store(tasks).get_items()] == ["first step", "second step"]
    events = [b for b in _bodies(run) if isinstance(b, TasksUpdated)]
    assert len(events) == 1


async def test_one_shared_category_keeps_conversations_apart(tasks: ConversationTasks):
    """The category object is built **once** for the whole app and shared by every
    conversation, while each conversation owns its own list. The capability memoises its
    store on first use, so a single shared capability hands thread B thread A's tasks."""
    toolset, _ = await _toolset_ctx(tasks, "conv-a")

    async def write(conversation_id: str, task: str) -> None:
        _, ctx = await _toolset_ctx(tasks, conversation_id)
        tools = await toolset.get_tools(ctx)
        await toolset.call_tool(
            "tasks_write", {"items": [TaskItem(content=task)]}, ctx, tools["tasks_write"]
        )

    await write("conv-a", "alpha task")
    await write("conv-b", "beta task")

    a = await ConversationTaskStore(tasks, owner_id=OWNER, conversation_id="conv-a").get_items()
    b = await ConversationTaskStore(tasks, owner_id=OWNER, conversation_id="conv-b").get_items()
    assert [i.content for i in a] == ["alpha task"]
    assert [i.content for i in b] == ["beta task"]


async def test_a_run_without_a_conversation_keeps_one_list():
    """A run with no conversation still gets *one* list for its lifetime — a store rebuilt
    per call would hand every call an empty list and nothing would accumulate."""
    toolset, ctx = await _toolset_ctx(None, None)
    tools = await toolset.get_tools(ctx)

    await toolset.call_tool(
        "tasks_write", {"items": [TaskItem(content="only task")]}, ctx, tools["tasks_write"]
    )
    read = cast(object, await toolset.call_tool("tasks_read", {}, ctx, tools["tasks_read"]))
    assert "only task" in str(read)


async def test_a_duplicate_id_is_refused(tasks: ConversationTasks):
    store = _store(tasks)
    await store.set_items(_items("one"))
    # Two items sharing an id would shadow each other and make later updates land on
    # one of them at random — the protocol requires this to raise.
    with pytest.raises(ValueError):
        _ = await store.add_item(TaskItem(id="t0", content="again", status=TaskStatus.pending))


async def test_updating_a_missing_task_reports_rather_than_writing(tasks: ConversationTasks):
    store = _store(tasks)
    await store.set_items(_items("one"))
    assert await store.update_item("nope", status=TaskStatus.completed) is None
    assert await store.remove_item("nope") is False
    assert [i.content for i in await store.get_items()] == ["one"]


async def test_the_task_list_is_sealed_at_rest(tasks: ConversationTasks, engine: Engine):
    await _store(tasks).set_items(_items("something private"))

    with Session(engine) as session:
        row = session.exec(select(ConversationTaskList)).one()
    # The list describes what the operator asked for, so it is content, not policy.
    assert "something private" not in row.items_enc


async def test_a_locked_vault_degrades_to_no_tasks(tasks: ConversationTasks, vault: Vault):
    vault.lock()
    store = _store(tasks)
    # The list aids the turn; it is not the turn. A run that cannot read it carries on
    # without one rather than failing.
    assert await store.get_items() == []
    await store.set_items(_items("dropped"))


async def test_the_offered_surface_is_three_tools_under_our_own_names():
    """The harness registers six under its own vocabulary; we offer three under ours. The
    narrowing is an allowlist the harness validates by name, so a rename upstream must fail
    loudly rather than quietly widening the surface again — and the renaming is what keeps
    the word "plan" out of the one place the model actually reads."""
    toolset, ctx = await _toolset_ctx(None, None)

    tools = await toolset.get_tools(ctx)
    assert set(tools) == {"tasks_write", "tasks_update_statuses", "tasks_read"}
    # The definition carries the new name too, not just the key: the model is offered
    # whatever `tool_def.name` says, and a key-only rename would be invisible to it.
    assert {t.tool_def.name for t in tools.values()} == set(tools)
    # Ours, not the harness's: its wording is written for the surface we just dropped and
    # cross-references tools this catalog never offers.
    descriptions = {name: tool.tool_def.description or "" for name, tool in tools.items()}
    assert "add_task" not in " ".join(descriptions.values())
    assert descriptions["tasks_read"].startswith("Returns your task list")


async def test_the_catalog_lists_the_same_names_the_model_is_offered(tasks: ConversationTasks):
    """The operator's settings list is derived from the toolset's static `tools` registry,
    and the enabled gate matches on the offered name. If the two spelled the category
    differently, switching a tool off in settings would withhold nothing."""
    from tools.tasks import ConversationTaskToolset, tasks_toolset

    toolset = tasks_toolset()
    assert isinstance(toolset, ConversationTaskToolset)
    _, ctx = await _toolset_ctx(tasks, "conv-catalog")
    offered = await toolset.get_tools(ctx)
    assert set(toolset.tools) == set(offered)


def test_the_rename_map_is_a_bijection_over_the_allowlist():
    """A description keyed to a tool we no longer offer would be silently ignored, and a
    tool offered without one would fall back to the harness's wording — so the sets are
    asserted equal rather than each being checked alone. The inverse map is checked too:
    a duplicate value there would make one of our names undispatchable."""
    from tools.tasks import DESCRIPTIONS, LOCAL, UPSTREAM

    assert set(DESCRIPTIONS) == set(UPSTREAM.values())
    assert len(LOCAL) == len(UPSTREAM)
    assert {LOCAL[upstream] for upstream in UPSTREAM.values()} == set(UPSTREAM)


class _Launcher:
    """Every thread has a report to come while ``out``; a filed report is accepted."""

    def __init__(self, *, out: bool) -> None:
        self.out: bool = out

    async def awaiting_reports(self, _owner_id: str, _conversation_id: str) -> bool:
        return self.out

    async def file_report(
        self, _owner_id: str, _conversation_id: str, _run_id: str, _report: FiledReport
    ) -> str:
        return "parent"


def _caps(tasks: ConversationTasks, launcher: _Launcher | None = None) -> ServiceContainer:
    caps = ServiceContainer()
    caps.add(tasks, as_type=ConversationTasks)
    if launcher is not None:
        caps.add(launcher, as_type=SubagentLauncher)
    return caps


async def test_open_steps_are_raised_once_per_open_set(tasks: ConversationTasks):
    items = _items("read the file", "fix the bug")
    items[0] = items[0].model_copy(update={"status": TaskStatus.completed})
    await _store(tasks).set_items(items)
    caps = _caps(tasks)

    notice = await open_tasks_notice(caps, OWNER, "conv-1")
    assert notice is not None
    assert "fix the bug (pending)" in notice
    assert "read the file" not in notice
    assert await open_tasks_notice(caps, OWNER, "conv-1") is None


async def test_a_changed_open_set_is_raised_again(tasks: ConversationTasks):
    store = _store(tasks)
    await store.set_items(_items("one"))
    caps = _caps(tasks)
    assert await open_tasks_notice(caps, OWNER, "conv-1") is not None

    await store.set_items(_items("one", "two"))
    notice = await open_tasks_notice(caps, OWNER, "conv-1")
    assert notice is not None and "two (pending)" in notice


async def test_settled_steps_raise_nothing(tasks: ConversationTasks):
    items = [
        item.model_copy(update={"status": status})
        for item, status in zip(
            _items("read the file", "fix the bug"),
            (TaskStatus.completed, TaskStatus.cancelled),
            strict=True,
        )
    ]
    await _store(tasks).set_items(items)
    assert await open_tasks_notice(_caps(tasks), OWNER, "conv-1") is None


async def test_no_conversation_or_no_capability_raises_nothing(tasks: ConversationTasks):
    await _store(tasks).set_items(_items("one"))
    assert await open_tasks_notice(_caps(tasks), OWNER, None) is None
    assert await open_tasks_notice(ServiceContainer(), OWNER, "conv-1") is None


async def test_a_report_to_come_holds_the_notice_for_a_later_stop(tasks: ConversationTasks):
    await _store(tasks).set_items(_items("collect the reports"))
    launcher = _Launcher(out=True)
    caps = _caps(tasks, launcher)
    assert await open_tasks_notice(caps, OWNER, "conv-1") is None

    launcher.out = False
    notice = await open_tasks_notice(caps, OWNER, "conv-1")
    assert notice is not None and "collect the reports (pending)" in notice


type _Call = tuple[str, dict[str, object]]

_WRITE_LIST: _Call = ("tasks_write", {"items": [{"content": "ship it"}]})
_REPORT: _Call = ("subagent_report", {"status": "done", "report": "shipped"})


def _answer(*calls: _Call) -> StreamFunctionDef:
    """Makes ``calls`` one per model request, then answers."""

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        made = sum(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if made < len(calls):
            name, args = calls[made]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
        else:
            yield "done"

    return stream_fn


async def _run_with_list(
    tasks: ConversationTasks,
    *calls: _Call,
    launcher: _Launcher | None = None,
    spawned: bool = False,
) -> Run:
    orch = build_chat_orchestrator(
        "go",
        model=FunctionModel(stream_function=_answer(*calls)),
        categories={"tasks": tasks_toolset(), "subagent": report_toolset()},
        capabilities=_caps(tasks, launcher),
        conversation_id="conv-e2e",
        binding=ConversationBinding(spawned=spawned),
    )
    run = RunRegistry().submit(kind="chat", owner_id=OWNER, orchestrator=orch)
    await run.wait()
    return run


def _notices(run: Run) -> list[str]:
    return [b.type for b in _bodies(run) if b.type == "message.injected"]


async def test_the_turn_continues_once_for_open_steps_and_not_again(tasks: ConversationTasks):
    first = await _run_with_list(tasks, _WRITE_LIST)
    assert first.status is RunStatus.done
    assert _notices(first) == ["message.injected"]
    assert [b.type for b in _bodies(first)].count("step.started") == 3
    assert [b.type for b in _bodies(first)].count("run.ended") == 1

    second = await _run_with_list(tasks)
    assert second.status is RunStatus.done
    assert _notices(second) == []


async def test_a_parent_waiting_on_its_agents_ends_the_turn_unprompted(
    tasks: ConversationTasks,
):
    launcher = _Launcher(out=True)
    waiting = await _run_with_list(tasks, _WRITE_LIST, launcher=launcher)
    assert waiting.status is RunStatus.done
    assert _notices(waiting) == []

    launcher.out = False
    woken = await _run_with_list(tasks, launcher=launcher)
    assert _notices(woken) == ["message.injected"]


async def test_a_spawned_agent_that_reported_is_not_reopened(tasks: ConversationTasks):
    run = await _run_with_list(
        tasks, _WRITE_LIST, _REPORT, launcher=_Launcher(out=False), spawned=True
    )
    assert run.status is RunStatus.done
    assert _notices(run) == []
