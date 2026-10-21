"""The /tasks REST surface: CRUD + validation, manual run_now (incl. non-overlap and
outcome mapping), the inbound webhook trigger (+ its auth exemption), and the AE-3.5
park/approve/resume path for a task-driven agent run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue
from pydantic_ai.models.test import TestModel
from sqlmodel import Session, select

from agent import ParkedTurn
from core.db import in_session
from models.task import ScheduledTask, TaskRun
from routes.deps import app_state
from runs import Orchestrator, Run
from services.approval_grants import ApprovalGrantStore
from services.notifications import NotificationService
from services.registry import ModelRegistry
from services.scheduler import ScheduledTaskView, SchedulerService, TaskRunResult

from ._helpers import (
    client_app,
    patch_model_resolution,
    register_stub_provider,
    stub_resolution,
    swap_tool_catalog,
)
from ._json import arr, json_body, obj, s
from .test_approval_routes import danger_categories

_FAR_FUTURE = "2999-01-01T00:00:00Z"


async def create_task(
    client: httpx.AsyncClient,
    *,
    kind: str = "agent",
    title: str = "a task",
    prompt: str = "do the thing",
    schedule: dict[str, JsonValue] | None = None,
    output: str = "chat",
    pre_authorized: list[str] | None = None,
    enabled: bool = True,
) -> dict[str, JsonValue]:
    resp = await client.post(
        "/tasks",
        json={
            "kind": kind,
            "title": title,
            "prompt": prompt,
            "schedule": schedule or {"type": "once", "runAt": _FAR_FUTURE},
            "output": output,
            "preAuthorized": pre_authorized or [],
            "enabled": enabled,
        },
    )
    assert resp.status_code == 201, resp.text
    return json_body(resp)


def response_items(resp: httpx.Response) -> list[JsonValue]:
    return arr(json_body(resp)["items"])


async def await_task_run_finalized(
    client: httpx.AsyncClient, task_id: str, task_run_id: str, *, timeout: float = 2.0
) -> dict[str, JsonValue]:
    loop_budget = int(timeout / 0.01)
    for _ in range(loop_budget):
        items = response_items(await client.get(f"/tasks/{task_id}/runs"))
        row = next((obj(r) for r in items if obj(r)["id"] == task_run_id), None)
        if row is not None and row["finishedAt"] is not None:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("task run never finalized")


def parked_call_id(run: Run) -> str:
    """The tool-call id of the first approval a parked run is waiting on."""
    payload = run.parked_payload
    assert isinstance(payload, ParkedTurn)
    return payload.requests.approvals[0].tool_call_id


def _scheduler(app: FastAPI) -> SchedulerService:
    return app_state(app).scheduler


def _notifications(app: FastAPI) -> NotificationService:
    return app_state(app).notifications


def _grants(app: FastAPI) -> ApprovalGrantStore:
    return app_state(app).approval_grants


# --- CRUD + validation -------------------------------------------------------------


async def test_create_list_patch_delete_round_trip():
    async with client_app() as (client, _app):
        created = await create_task(client, title="Morning digest")
        task_id = s(created["id"])
        assert created["kind"] == "agent"
        assert created["title"] == "Morning digest"
        schedule = obj(created["schedule"])
        assert schedule["type"] == "once"
        assert s(schedule["runAt"]).startswith("2999-01-01T00:00:00")
        assert schedule["everySeconds"] is None
        assert schedule["cron"] is None
        assert created["enabled"] is True
        assert created["webhookUrl"] is None
        assert created["nextRunAt"] is not None  # computed at creation

        listed = response_items(await client.get("/tasks"))
        assert [obj(t)["id"] for t in listed] == [task_id]

        patched = await client.patch(f"/tasks/{task_id}", json={"title": "Evening digest"})
        assert patched.status_code == 200
        assert json_body(patched)["title"] == "Evening digest"
        assert json_body(patched)["prompt"] == "do the thing"  # unedited fields untouched

        assert (await client.delete(f"/tasks/{task_id}")).status_code == 204
        assert response_items(await client.get("/tasks")) == []
        assert (await client.patch(f"/tasks/{task_id}", json={"title": "x"})).status_code == 404
        assert (await client.delete(f"/tasks/{task_id}")).status_code == 404


async def test_create_rejects_unknown_kind_output_and_bad_schedule():
    async with client_app() as (client, _app):
        assert (await _post_task(client, kind="not-a-kind")).status_code == 422
        assert (await _post_task(client, output="not-an-output")).status_code == 422
        assert (await _post_task(client, schedule={"type": "once"})).status_code == 422
        assert (
            await _post_task(client, schedule={"type": "interval", "everySeconds": 0})
        ).status_code == 422
        assert (await _post_task(client, schedule={"type": "cron"})).status_code == 422
        assert (
            await _post_task(client, schedule={"type": "cron", "cron": "not a cron expr"})
        ).status_code == 422
        assert (await _post_task(client, title="   ")).status_code == 422
        assert (await _post_task(client, prompt="")).status_code == 422


async def _post_task(client: httpx.AsyncClient, **overrides: JsonValue) -> httpx.Response:
    body: dict[str, JsonValue] = {
        "kind": "agent",
        "title": "t",
        "prompt": "p",
        "schedule": {"type": "once", "runAt": _FAR_FUTURE},
        "output": "chat",
        "preAuthorized": [],
    }
    body.update(overrides)
    return await client.post("/tasks", json=body)


async def test_create_and_patch_reject_unknown_pre_authorized_scope():
    async with client_app() as (client, _app):
        resp = await _post_task(client, preAuthorized=["not_a_real_scope"])
        assert resp.status_code == 422

        created = await create_task(client, pre_authorized=["agents_write"])
        bad_patch = await client.patch(f"/tasks/{created['id']}", json={"preAuthorized": ["nope"]})
        assert bad_patch.status_code == 422

        good_patch = await client.patch(
            f"/tasks/{created['id']}", json={"preAuthorized": ["platform_search"]}
        )
        assert good_patch.status_code == 200
        assert json_body(good_patch)["preAuthorized"] == ["platform_search"]


async def test_interval_and_cron_schedules_compute_a_next_run_at():
    async with client_app() as (client, _app):
        interval = await create_task(client, schedule={"type": "interval", "everySeconds": 3600})
        assert interval["nextRunAt"] is not None

        cron = await create_task(client, schedule={"type": "cron", "cron": "0 * * * *"})
        assert cron["nextRunAt"] is not None


async def test_patch_schedule_change_recomputes_next_run_at():
    async with client_app() as (client, _app):
        created = await create_task(client, schedule={"type": "interval", "everySeconds": 3600})
        first_next = created["nextRunAt"]

        patched = await client.patch(
            f"/tasks/{created['id']}",
            json={"schedule": {"type": "interval", "everySeconds": 60}},
        )
        assert patched.status_code == 200
        assert json_body(patched)["nextRunAt"] != first_next


async def test_webhook_schedule_has_no_next_run_and_gets_a_url():
    async with client_app() as (client, _app):
        created = await create_task(client, schedule={"type": "webhook"})
        assert created["nextRunAt"] is None
        assert created["webhookUrl"] is not None
        assert s(created["webhookUrl"]).startswith("/tasks/hooks/")


# --- run_now: non-overlap + happy path with seeded grants --------------------------


async def test_run_now_creates_conversation_run_and_seeds_grants(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch, output_text="All set, nothing else to do.")
    async with client_app() as (client, app):
        created = await create_task(client, pre_authorized=["platform_search", "agents_write"])
        task_id = s(created["id"])

        run_now = await client.post(f"/tasks/{task_id}/run_now")
        assert run_now.status_code == 202
        task_run_id = s(json_body(run_now)["taskRunId"])

        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert row["outcome"] == "ok"
        assert row["runId"] is not None
        assert row["conversationId"] is not None
        assert "All set" in s(row["summary"])

        granted = await _grants(app).list("operator", s(row["conversationId"]))
        assert {g.tool_name for g in granted} == {"platform_search", "agents_write"}
        # Whole-tool by shape, but never the operator's wider pick: nobody was in front of
        # this run to make it, and the thread it opened is one they can reopen later. A
        # seed promoted by its shape would go on answering past this single execution.
        assert not any(g.decisive for g in granted)

        # The task's own bookkeeping reflects the fire too.
        refreshed = obj(response_items(await client.get("/tasks"))[0])
        assert refreshed["lastRunAt"] is not None


async def test_a_fire_drops_a_once_only_pre_authorization(monkeypatch: pytest.MonkeyPatch):
    # The route refuses to store such a scope, but a row written before that rule — or by
    # anything else with the table — still reaches the fire. This is the second writer of
    # conversation grants, and the unattended run is exactly where a standing grant on a
    # per-call gate would do its damage: nobody is reading what it covers.
    patch_model_resolution(monkeypatch, output_text="All set, nothing else to do.")
    async with client_app() as (_client, app):
        view = ScheduledTaskView(
            id="task-1",
            owner_id="operator",
            kind="agent",
            title="t",
            prompt="p",
            output="chat",
            pre_authorized=["code_request_egress", "platform_search"],
        )
        result = await _scheduler(app).executor(view)
        assert result.outcome == "ok"
        assert result.conversation_id is not None
        granted = await _grants(app).list("operator", result.conversation_id)
        assert {g.tool_name for g in granted} == {"platform_search"}


async def test_run_now_starts_alongside_a_still_live_execution():
    async with client_app() as (client, app):
        created = await create_task(client)
        task_id = s(created["id"])

        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def slow_executor(view: ScheduledTaskView) -> TaskRunResult:
            calls.append(view.id)
            started.set()
            _ = await release.wait()
            return TaskRunResult(outcome="ok")

        _scheduler(app).executor = slow_executor
        first = await client.post(f"/tasks/{task_id}/run_now")
        _ = await asyncio.wait_for(started.wait(), timeout=2.0)
        second = await client.post(f"/tasks/{task_id}/run_now")
        assert second.status_code == 202
        first_id, second_id = s(json_body(first)["taskRunId"]), s(json_body(second)["taskRunId"])
        assert first_id != second_id
        for _ in range(200):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == 2  # both running at once

        release.set()
        _ = await asyncio.wait_for(_wait_finished(client, task_id, first_id), timeout=2.0)
        row = await asyncio.wait_for(_wait_finished(client, task_id, second_id), timeout=2.0)
        assert row["outcome"] == "ok"
        assert len(calls) == 2


async def _wait_finished(
    client: httpx.AsyncClient, task_id: str, task_run_id: str
) -> dict[str, JsonValue]:
    while True:
        items = response_items(await client.get(f"/tasks/{task_id}/runs"))
        row = next((obj(r) for r in items if obj(r)["id"] == task_run_id), None)
        if row is not None and row["finishedAt"] is not None:
            return row
        await asyncio.sleep(0.01)


async def test_run_now_unknown_task_404s():
    async with client_app() as (client, _app):
        assert (await client.post("/tasks/nope/run_now")).status_code == 404


# --- outcome mapping ----------------------------------------------------------------


def _orchestrator_factory(orch: Orchestrator) -> Callable[..., Orchestrator]:
    def factory(_prompt: object, **_kw: object) -> Orchestrator:
        return orch

    return factory


async def test_run_now_outcome_mapping_error_and_blocked(monkeypatch: pytest.MonkeyPatch):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):

        async def error_orch(_run: Run) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "routes.chat.turn.build_chat_orchestrator", _orchestrator_factory(error_orch)
        )
        created = await create_task(client)
        run_now = await client.post(f"/tasks/{created['id']}/run_now")
        row = await await_task_run_finalized(
            client, s(created["id"]), s(json_body(run_now)["taskRunId"])
        )
        assert row["outcome"] == "error"
        assert row["summary"] == "boom"

        async def blocked_orch(run: Run) -> None:
            run.block("hit a limit")

        monkeypatch.setattr(
            "routes.chat.turn.build_chat_orchestrator", _orchestrator_factory(blocked_orch)
        )
        created2 = await create_task(client)
        run_now2 = await client.post(f"/tasks/{created2['id']}/run_now")
        row2 = await await_task_run_finalized(
            client, s(created2["id"]), s(json_body(run_now2)["taskRunId"])
        )
        assert row2["outcome"] == "blocked"
        assert row2["summary"] == "hit a limit"


# --- reminders + task_outcome notifications -----------------------------------------


async def test_reminder_task_fires_reminder_notification_verbatim():
    async with client_app() as (client, app):
        created = await create_task(
            client,
            kind="reminder",
            title="Take a break",
            prompt="Stand up and stretch for five minutes.",
            output="notification",
        )
        run_now = await client.post(f"/tasks/{created['id']}/run_now")
        row = await await_task_run_finalized(
            client, s(created["id"]), s(json_body(run_now)["taskRunId"])
        )
        assert row["outcome"] == "ok"

        items, _ = await _notifications(app).list_notifications("operator", limit=50)
        reminder = next(n for n in items if n.kind == "reminder")
        assert reminder.title == "Take a break"
        assert reminder.body == "Stand up and stretch for five minutes."


async def test_agent_task_with_notification_output_fires_task_outcome(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch, output_text="All done, report attached.")
    async with client_app() as (client, app):
        created = await create_task(client, title="Nightly report", output="notification")
        run_now = await client.post(f"/tasks/{created['id']}/run_now")
        row = await await_task_run_finalized(
            client, s(created["id"]), s(json_body(run_now)["taskRunId"])
        )
        assert row["outcome"] == "ok"

        items, _ = await _notifications(app).list_notifications("operator", limit=50)
        outcome_notifs = [n for n in items if n.kind == "task_outcome"]
        assert len(outcome_notifs) == 1
        assert outcome_notifs[0].title == "Nightly report"
        assert "All done" in (outcome_notifs[0].body or "")
        assert outcome_notifs[0].conversation_id == row["conversationId"]


async def test_agent_task_with_chat_output_does_not_fire_task_outcome(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        created = await create_task(client, output="chat")
        run_now = await client.post(f"/tasks/{created['id']}/run_now")
        _ = await await_task_run_finalized(
            client, s(created["id"]), s(json_body(run_now)["taskRunId"])
        )

        items, _ = await _notifications(app).list_notifications("operator", limit=50)
        assert [n for n in items if n.kind == "task_outcome"] == []


# --- delete cascades its runs --------------------------------------------------------


async def test_delete_task_cascades_its_runs():
    async with client_app() as (client, app):
        created = await create_task(client)
        task_id = s(created["id"])

        async def instant_ok(_view: ScheduledTaskView) -> TaskRunResult:
            return TaskRunResult(outcome="ok")

        _scheduler(app).executor = instant_ok
        run_now = await client.post(f"/tasks/{task_id}/run_now")
        _ = await await_task_run_finalized(client, task_id, s(json_body(run_now)["taskRunId"]))

        assert (await client.delete(f"/tasks/{task_id}")).status_code == 204

        def work(session: Session) -> list[TaskRun]:
            return list(session.exec(select(TaskRun).where(TaskRun.task_id == task_id)).all())

        remaining = await in_session(app_state(app).db_engine, work)
        assert remaining == []


# --- webhook: fire, bad token, rotation ---------------------------------------------


async def test_webhook_fires_bad_token_404s_and_rotation_invalidates_old(
    monkeypatch: pytest.MonkeyPatch,
):
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        created = await create_task(client, schedule={"type": "webhook"})
        task_id = s(created["id"])
        token = s(created["webhookUrl"]).rsplit("/", 1)[-1]

        assert (await client.post(f"/tasks/hooks/{token}")).status_code == 202
        assert (await client.post("/tasks/hooks/definitely-not-a-real-token")).status_code == 404

        rotated = await client.patch(f"/tasks/{task_id}", json={"rotateWebhookToken": True})
        assert rotated.status_code == 200
        new_url = s(json_body(rotated)["webhookUrl"])
        assert new_url != created["webhookUrl"]

        assert (await client.post(f"/tasks/hooks/{token}")).status_code == 404
        new_token = new_url.rsplit("/", 1)[-1]
        assert (await client.post(f"/tasks/hooks/{new_token}")).status_code == 202


async def test_webhook_does_not_fire_once_the_task_is_disabled(monkeypatch: pytest.MonkeyPatch):
    # security-02: disabling a task is the operator's only way to "pause" a webhook
    # trigger — the token itself stays unguessable and unrotated, so this must be
    # what actually stops it firing. Still 202 (no info leak to whoever holds the
    # token either way), but no TaskRun is ever recorded.
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, _app):
        created = await create_task(client, schedule={"type": "webhook"})
        task_id = s(created["id"])
        token = s(created["webhookUrl"]).rsplit("/", 1)[-1]

        disabled = await client.patch(f"/tasks/{task_id}", json={"enabled": False})
        assert disabled.status_code == 200
        assert json_body(disabled)["enabled"] is False

        assert (await client.post(f"/tasks/hooks/{token}")).status_code == 202
        await asyncio.sleep(0.05)
        assert response_items(await client.get(f"/tasks/{task_id}/runs")) == []


async def test_a_webhook_token_cannot_fire_another_owners_task(monkeypatch: pytest.MonkeyPatch):
    """The hook route is auth-exempt, so the candidate scan is the *only* place ownership
    can be enforced — `scheduler.fire_now` does none of its own, its docstring assuming
    the route already did. An unscoped scan would let a token minted under one owner match
    another owner's row the moment the `owner_id` seam carries more than one operator, so
    the predicate is asserted now rather than discovered then."""
    patch_model_resolution(monkeypatch)
    async with client_app() as (client, app):
        created = await create_task(client, schedule={"type": "webhook"})
        task_id = s(created["id"])
        token = s(created["webhookUrl"]).rsplit("/", 1)[-1]

        # Hand the row to somebody else, leaving the token itself untouched: the caller
        # still holds a genuine, unrotated credential — it just isn't for this owner.
        def reassign(session: Session) -> None:
            row = session.get(ScheduledTask, task_id)
            assert row is not None
            row.owner_id = "somebody-else"
            session.add(row)

        await in_session(app_state(app).db_engine, reassign)

        # 404, exactly as an unknown token does — no way to tell the two apart.
        assert (await client.post(f"/tasks/hooks/{token}")).status_code == 404
        await asyncio.sleep(0.05)

        def runs(session: Session) -> list[TaskRun]:
            return list(session.exec(select(TaskRun).where(TaskRun.task_id == task_id)).all())

        assert await in_session(app_state(app).db_engine, runs) == []


async def test_run_now_refuses_a_disabled_task():
    # security-04: the same gate as the webhook path, but for the authenticated
    # manual-fire route — a clear 409 rather than silently running (or the
    # misleading generic 404 "not found").
    async with client_app() as (client, _app):
        created = await create_task(client, enabled=False)
        resp = await client.post(f"/tasks/{created['id']}/run_now")
        assert resp.status_code == 409


async def test_rotate_webhook_token_rejected_for_non_webhook_task():
    async with client_app() as (client, _app):
        created = await create_task(client)  # schedule type "once"
        resp = await client.patch(f"/tasks/{created['id']}", json={"rotateWebhookToken": True})
        assert resp.status_code == 422


# --- auth exemption: exactly /tasks/hooks/ ------------------------------------------


async def test_hooks_route_is_auth_exempt_but_rest_of_tasks_is_not():
    async with client_app(auth_enabled=True, passphrase=None) as (client, _app):
        setup = await client.post("/setup", json={"password": "correct horse battery staple"})
        assert setup.status_code == 200

        created = await create_task(client, schedule={"type": "webhook"})
        token = s(created["webhookUrl"]).rsplit("/", 1)[-1]

        client.cookies.clear()
        assert (await client.get("/tasks")).status_code == 401
        assert (await client.post("/tasks", json={})).status_code == 401
        assert (await client.patch(f"/tasks/{created['id']}", json={})).status_code == 401
        assert (await client.post(f"/tasks/{created['id']}/run_now")).status_code == 401

        assert (await client.post(f"/tasks/hooks/{token}")).status_code == 202


# --- AE-3.5: a task run hitting an out-of-scope sensitive tool parks + notifies ------


def install_sensitive_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors `test_approval_routes.py`'s own helper: a TestModel that always calls
    one approval-required tool, so a task's unattended run parks exactly like an
    interactive one when the action falls outside its pre-authorization. Pair with
    ``swap_tool_catalog(app, danger_categories())`` after boot."""

    async def fake_resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(
            self, TestModel(custom_output_text="done", call_tools=["danger_delete_thing"])
        )

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", fake_resolve_detailed)


async def await_parked_task_run(app: FastAPI, *, timeout: float = 2.0) -> Run:
    loop_budget = int(timeout / 0.01)
    for _ in range(loop_budget):
        waiters = app_state(app).task_run_waiters
        if waiters:
            run_id = next(iter(waiters))
            run = app_state(app).runs.get(run_id)
            if run is not None and run.status == "awaiting_input":
                return run
        await asyncio.sleep(0.01)
    raise AssertionError("task run never parked")


async def test_task_run_hitting_sensitive_tool_outside_grants_parks_and_notifies(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        created = await create_task(client, pre_authorized=[])  # no standing grant
        task_id = s(created["id"])

        run_now = await client.post(f"/tasks/{task_id}/run_now")
        task_run_id = s(json_body(run_now)["taskRunId"])

        run = await await_parked_task_run(app)
        assert run.conversation_id is not None

        # The park notifies exactly like an interactive run's would.
        items, _ = await _notifications(app).list_notifications("operator", limit=50)
        approval_notifs = [n for n in items if n.kind == "approval_needed"]
        assert any(n.conversation_id == run.conversation_id for n in approval_notifs)

        # The TaskRun is still unfinished while parked — not silently marked done.
        pending = response_items(await client.get(f"/tasks/{task_id}/runs"))
        assert next(obj(r) for r in pending if obj(r)["id"] == task_run_id)["finishedAt"] is None

        # Approving from the (notification-driven) approval route resumes the run —
        # and the resume settles the TaskRun.
        call_id = parked_call_id(run)
        approve = await client.post(
            f"/runs/{run.id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert approve.status_code == 202

        row = await await_task_run_finalized(client, task_id, task_run_id)
        assert row["outcome"] == "ok"
        assert row["runId"] == run.id
        assert row["conversationId"] == run.conversation_id
