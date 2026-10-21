"""Routines: schedules read in the operator's zone, agent drafts that wait on approval, the
`routines_*` tools, and each routine's one thread run by its own agent."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import ModelResponse, RunContext, ToolCallPart
from pydantic_ai.exceptions import ApprovalRequired, ModelRetry
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.db import init_db, make_engine
from core.exceptions import InvalidInputError, RoutineValidationError
from core.vault import Vault
from routes.deps import app_state
from runs import Run, RunStream
from services.agents import AgentCatalog
from services.routines import (
    RoutineStore,
    Schedule,
    ScheduleNaturalLanguage,
    format_schedule,
    narrows,
    parse_schedule,
    upcoming,
)
from services.scheduler import ScheduledTaskView, SchedulerService, TaskRunResult
from tools.deps import RunDeps
from tools.routines import routines_toolset

from ._agent_helpers import make_stores as make_agent_stores
from ._helpers import client_app, patch_model_resolution
from ._json import arr, json_body, obj, s

OWNER = "operator"
_RESULT = TypeAdapter(dict[str, JsonValue])
_TOOL_ARGS = TypeAdapter(dict[str, object])
NOW = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)  # a Wednesday
DAILY = Schedule(type="cron", cron="0 9 * * *", timezone="America/New_York")


@pytest.fixture
async def store(tmp_path: Path) -> RoutineStore:
    agents, _conversations = await make_agent_stores(tmp_path)
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "routines.json")
    await vault.setup("pw")
    return RoutineStore(engine, vault, AgentCatalog(agents))


def _values(**more: object) -> dict[str, object]:
    return {"title": "Morning brief", "prompt": "Summarize my inbox.", "schedule": DAILY, **more}


# --- schedules ------------------------------------------------------------------------


def test_a_cron_is_read_in_its_zone_across_daylight_saving():
    summer = upcoming(DAILY, NOW, 1)[0]
    winter = upcoming(DAILY, datetime(2026, 12, 10, 9, 0, tzinfo=UTC), 1)[0]
    assert (summer.hour, winter.hour) == (13, 14)


def test_an_interval_previews_its_next_fires():
    every_hour = Schedule(type="interval", every_seconds=3600)
    assert upcoming(every_hour, NOW) == [NOW + timedelta(hours=n) for n in (1, 2, 3)]


@pytest.mark.parametrize(
    "schedule",
    [
        Schedule(type="cron", cron="every morning"),
        Schedule(type="cron", cron="0 9 * * *", timezone="Mars/Olympus"),
        Schedule(type="once", run_at=datetime(2026, 6, 12, 9)),
        Schedule(type="interval", every_seconds=0),
    ],
)
async def test_a_bad_schedule_is_refused(store: RoutineStore, schedule: Schedule):
    with pytest.raises(RoutineValidationError) as raised:
        _ = await store.create(OWNER, _values(schedule=schedule), approved=True)
    assert raised.value.field == "schedule"


def _parser(**fields: str) -> ScheduleNaturalLanguage:
    async def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, fields)])

    async def resolve() -> tuple[Model, ModelSettings | None]:
        return FunctionModel(respond), None

    return ScheduleNaturalLanguage(resolve_model=resolve)


async def test_a_phrase_becomes_a_cron_in_the_operators_zone():
    parser = _parser(type="cron", cron="0 9 * * 1-5")
    schedule = await parser.parse("weekdays at 9", timezone="Europe/Madrid", now=NOW)
    assert (schedule.cron, schedule.timezone) == ("0 9 * * 1-5", "Europe/Madrid")


async def test_a_one_off_phrase_is_localized_and_must_lie_ahead():
    parser = _parser(type="once", run_at="2026-06-12T13:00:00")
    schedule = await parser.parse("Friday 1pm", timezone="Europe/Madrid", now=NOW)
    assert schedule.run_at == datetime(2026, 6, 12, 11, 0, tzinfo=UTC)

    with pytest.raises(InvalidInputError):
        _ = await _parser(type="once", run_at="2026-06-01T13:00:00").parse(
            "last week", timezone="UTC", now=NOW
        )


async def test_a_misread_cron_is_refused_rather_than_saved():
    with pytest.raises(InvalidInputError):
        _ = await _parser(type="cron", cron="whenever").parse("now and then", timezone="UTC")


# --- drafts and approval -----------------------------------------------------------------


async def test_an_agents_draft_never_fires_until_approved(store: RoutineStore):
    draft = await store.create(OWNER, _values(), approved=False)

    async def executor(_task: ScheduledTaskView) -> TaskRunResult:
        raise AssertionError("an unapproved draft must not execute")

    async def notify(_task: ScheduledTaskView) -> None:
        raise AssertionError("an unapproved draft must not notify")

    scheduler = SchedulerService(store.engine, store.vault, executor=executor, notify=notify)
    assert await scheduler.fire_now(draft.id) is None

    approved = await store.approve(OWNER, draft.id)
    assert approved.approved and approved.next_run_at is not None


async def test_a_routine_names_a_real_agent(store: RoutineStore):
    with pytest.raises(RoutineValidationError) as raised:
        _ = await store.create(OWNER, _values(agent_name="nobody"), approved=True)
    assert raised.value.field == "agent"
    routine = await store.create(OWNER, _values(agent_name="researcher"), approved=True)
    assert routine.agent_name == "researcher"


async def test_only_taking_reach_away_counts_as_narrowing(store: RoutineStore):
    routine = await store.create(
        OWNER, _values(pre_authorized=("web_fetch", "shell_run_command")), approved=True
    )
    assert narrows(routine, {"enabled": False, "pre_authorized": ("web_fetch",)})
    assert not narrows(routine, {"pre_authorized": ("web_fetch", "mail_send")})
    assert not narrows(routine, {"prompt": "Delete my inbox."})


# --- the agent's tools ---------------------------------------------------------------------


class _Scheduler:
    def __init__(self) -> None:
        self.fired: list[str] = []

    async def fire_now(self, routine_id: str) -> str:
        self.fired.append(routine_id)
        return "run-row"

    def wake(self) -> None:
        pass


def _ctx(
    store: RoutineStore, scheduler: _Scheduler, *, approved: bool = False
) -> RunContext[RunDeps]:
    caps = ServiceContainer()
    caps.add(store)
    caps.add(scheduler, as_type=SchedulerService)
    return RunContext(
        deps=RunDeps(
            run=Run(id="run-1", kind="chat", owner_id=OWNER, stream=RunStream()),
            owner_id=OWNER,
            caps=caps,
        ),
        model=TestModel(),
        usage=RunUsage(),
        tool_call_approved=approved,
    )


async def _call(
    ctx: RunContext[RunDeps], name: str, args: Mapping[str, object]
) -> dict[str, JsonValue]:
    toolset = routines_toolset()
    tool = (await toolset.get_tools(ctx))[name]
    return _RESULT.validate_python(
        await toolset.call_tool(
            name, _TOOL_ARGS.validate_python(tool.args_validator.validate_python(args)), ctx, tool
        )
    )


def test_the_compact_schedule_forms_read_and_round_trip():
    every = parse_schedule("every 30m", "UTC")
    assert every.every_seconds == 1800
    cron = parse_schedule("cron 0 9 * * 1-5 Europe/Madrid", "UTC")
    assert (cron.cron, cron.timezone) == ("0 9 * * 1-5", "Europe/Madrid")
    assert parse_schedule(format_schedule(cron), "UTC") == cron
    once = parse_schedule("once 2026-06-12T09:00+02:00", "UTC")
    assert once.run_at == datetime(2026, 6, 12, 7, 0, tzinfo=UTC)
    for bad in ("daily", "every often", "once tomorrow", "once 2026-06-12T09:00"):
        with pytest.raises(RoutineValidationError):
            _ = parse_schedule(bad, "UTC")


def _write(**more: object) -> dict[str, object]:
    return {
        "title": "Morning brief",
        "prompt": "Summarize my inbox.",
        "schedule": "cron 0 9 * * 1-5 UTC",
        **more,
    }


async def test_an_agent_drafts_a_routine_and_sees_when_it_would_fire(store: RoutineStore):
    ctx = _ctx(store, _Scheduler())
    result = await _call(ctx, "write", _write(agent="researcher"))
    assert (result["status"], result["agent"]) == ("draft", "researcher")
    assert all(s(run).endswith("T09:00:00+00:00") for run in arr(result["next_runs"]))

    started = await _call(ctx, "run_now", {"routine_id": result["id"]})
    assert started["ok"] is False


async def test_a_bad_schedule_is_sent_back_to_the_model(store: RoutineStore):
    with pytest.raises(ModelRetry):
        _ = await _call(_ctx(store, _Scheduler()), "write", _write(schedule="cron often"))


async def test_rewriting_an_approved_routine_asks_unless_it_only_narrows(store: RoutineStore):
    routine = await store.create(
        OWNER, _values(schedule=parse_schedule("cron 0 9 * * 1-5 UTC", "UTC")), approved=True
    )
    ctx = _ctx(store, _Scheduler())
    with pytest.raises(ApprovalRequired):
        _ = await _call(ctx, "write", _write(routine_id=routine.id, prompt="Something else."))
    with pytest.raises(ApprovalRequired):
        _ = await _call(ctx, "delete", {"routine_id": routine.id})

    off = await _call(ctx, "write", _write(routine_id=routine.id, enabled=False))
    assert off["status"] == "off"

    approved = _ctx(store, _Scheduler(), approved=True)
    changed = await _call(approved, "write", _write(routine_id=routine.id, prompt="Other."))
    assert changed["ok"] and (await store.get(OWNER, routine.id)).approved


async def test_a_routine_deleted_by_a_run_names_that_run_to_the_delete_hook(store: RoutineStore):
    routine = await store.create(
        OWNER, _values(schedule=parse_schedule("every 1h", "UTC")), approved=False
    )
    seen: list[tuple[str, str | None]] = []

    async def hook(routine_id: str, by_run: str | None) -> None:
        seen.append((routine_id, by_run))

    store.before_delete(hook)
    _ = await _call(_ctx(store, _Scheduler()), "delete", {"routine_id": routine.id})

    assert seen == [(routine.id, "run-1")]


async def test_running_an_approved_routine_fires_it(store: RoutineStore):
    routine = await store.create(OWNER, _values(), approved=True)
    scheduler = _Scheduler()
    result = await _call(_ctx(store, scheduler), "run_now", {"routine_id": routine.id})
    assert result["ok"] and scheduler.fired == [routine.id]


# --- the REST surface and the routine's thread -------------------------------------------


def _routine_body(**more: object) -> dict[str, object]:
    return {
        "kind": "agent",
        "title": "Morning brief",
        "prompt": "Summarize my inbox.",
        "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "UTC"},
        "output": "chat",
        **more,
    }


async def test_an_agents_draft_is_approved_by_the_operator():
    async with client_app() as (client, app):
        draft = await app_state(app).routines.create(
            OWNER,
            _values(pre_authorized=("not_a_tool",)),
            approved=False,
        )
        listed = obj(arr(json_body(await client.get("/tasks"))["items"])[0])
        assert (listed["approved"], listed["upcoming"]) == (False, [])
        assert (await client.post(f"/tasks/{draft.id}/run_now")).status_code == 409

        assert (await client.post(f"/tasks/{draft.id}/approve")).status_code == 422
        _ = await client.patch(f"/tasks/{draft.id}", json={"preAuthorized": []})
        approved = json_body(await client.post(f"/tasks/{draft.id}/approve"))
        assert approved["approved"] and len(arr(approved["upcoming"])) == 3


async def test_every_run_gets_a_fresh_thread_told_how_the_last_ended(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch, output_text="Inbox is quiet.")
    async with client_app() as (client, app):
        created = json_body(await client.post("/tasks", json=_routine_body(agentName="researcher")))
        first = await _run_and_wait(client, s(created["id"]))
        second = await _run_and_wait(client, s(created["id"]))

        assert first["conversationId"] != second["conversationId"]
        store = app_state(app).conversations
        binding = await store.binding(s(second["conversationId"]))
        assert binding is not None and binding.agent_name == "researcher"
        turns = await store.messages_view(s(second["conversationId"]))
        [prompt] = [turn for turn in turns if turn.role == "user"]
        assert "How the previous run ended" in prompt.content
        # The previous run's own words are quoted, so they ride as a part of their own.
        assert prompt.parts is not None
        assert any("Inbox is quiet." in part.text for part in prompt.parts)
        latest = obj(arr(json_body(await client.get("/tasks"))["items"])[0])["conversationId"]
        assert latest == second["conversationId"]


async def _run_and_wait(client: httpx.AsyncClient, routine_id: str) -> dict[str, JsonValue]:
    run_id = json_body(await client.post(f"/tasks/{routine_id}/run_now"))["taskRunId"]
    for _ in range(500):
        items = arr(json_body(await client.get(f"/tasks/{routine_id}/runs"))["items"])
        row = next((obj(r) for r in items if obj(r)["id"] == run_id), None)
        if row is not None and row["finishedAt"] is not None:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("the run never finished")


async def test_a_schedule_phrase_is_read_without_saving_anything(
    monkeypatch: pytest.MonkeyPatch,
):
    async with client_app() as (client, app):

        async def parse(_phrase: str, *, timezone: str, **_kwargs: object) -> Schedule:
            return Schedule(type="cron", cron="0 9 * * 1-5", timezone=timezone)

        monkeypatch.setattr(app_state(app).schedule_parser, "parse", parse)
        response = await client.post(
            "/tasks/parse-schedule", json={"phrase": "weekdays at 9", "timezone": "UTC"}
        )
        body = json_body(response)
        assert obj(body["schedule"])["cron"] == "0 9 * * 1-5"
        assert len(arr(body["upcoming"])) == 3
        assert json_body(await client.get("/tasks"))["items"] == []
