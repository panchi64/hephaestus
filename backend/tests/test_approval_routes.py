"""POST /runs/{id}/approve: the end-to-end approval flow over HTTP."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from pydantic import JsonValue
from pydantic_ai import FunctionToolset
from pydantic_ai.models.test import TestModel

from routes.deps import app_state
from runs import Orchestrator, Run
from services.notification_view import NotificationView
from services.registry import ModelRegistry
from tools import RunDeps

from ._helpers import (
    await_parked,
    client_app,
    collect_sse_events,
    parked_turn,
    register_stub_provider,
    stub_resolution,
    swap_tool_catalog,
)
from ._json import JSON, arr, json_body, json_list, obj, s


def danger_categories() -> dict[str, FunctionToolset[RunDeps]]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    return {"danger": toolset}


def egress_categories() -> dict[str, FunctionToolset[RunDeps]]:
    """A `code` category whose one tool is namespaced `code_request_egress` — the name
    `ONCE_ONLY_TOOLS` pins."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True)
    def request_egress(domain: str) -> str:
        return f"allowed {domain}"

    return {"code": toolset}


def install_sensitive_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the model at a TestModel; pair with ``swap_tool_catalog(app,
    danger_categories())`` after boot so the catalog is exactly the one
    approval-required tool."""

    async def fake_resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        # One patch covers the whole run: the turn's own model and the titler that
        # runs (on a toolless agent) after the approved turn completes both resolve
        # through here. A plain text model names the thread without tool calls.
        return await stub_resolution(self, TestModel(custom_output_text="done"))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", fake_resolve_detailed)


async def _chat(client: httpx.AsyncClient, prompt: str) -> str:
    return s(json_body(await client.post("/chat", json={"prompt": prompt}))["run_id"])


async def _grants(client: httpx.AsyncClient, conv_id: str | None) -> list[JsonValue]:
    return json_list(await client.get(f"/conversations/{conv_id}/grants"))


async def test_approve_flow_resumes_and_completes(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        run = await await_parked(app, run_id)

        status = json_body(await client.get(f"/runs/{run_id}"))
        assert status["status"] == "awaiting_input"

        call_id = parked_turn(run).requests.approvals[0].tool_call_id
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202

        events = await collect_sse_events(client, run_id)

    types = [e["type"] for e in events]
    assert "approval.required" in types
    assert "tool.completed" in types
    assert types[-1] == "run.ended"


async def test_approve_resumes_under_the_operators_bounds(monkeypatch: pytest.MonkeyPatch):
    # The continuation is a turn like any other, so it must run under the operator's own
    # bounds rather than the registry defaults — a wall clock that applied to send but not
    # to the resumed half of an approval-gated turn would be a setting that lies.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        _ = await client.put(
            "/chat/settings", json={"wall_clock_timeout_s": 900, "inactivity_timeout_s": 45}
        )
        run_id = await _chat(client, "delete it")
        run = await await_parked(app, run_id)

        seen: dict[str, object] = {}
        runs = app_state(app).runs
        real_resume = runs.resume

        async def spy(rid: str, orchestrator: Orchestrator, **kwargs: float | None) -> Run | None:
            seen.update(kwargs)
            return await real_resume(rid, orchestrator, **kwargs)

        monkeypatch.setattr(runs, "resume", spy)

        call_id = parked_turn(run).requests.approvals[0].tool_call_id
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202
        _ = await collect_sse_events(client, run_id)

    assert seen == {"wall_clock_timeout_s": 900, "inactivity_timeout_s": 45}


async def test_approve_rejects_unknown_and_unparked():
    async with client_app() as (client, app):
        # unknown run
        resp = await client.post("/runs/nope/approve", json={"decisions": []})
        assert resp.status_code == 404

        # a finished (not parked) run → 409
        async def orch(_run: Run) -> None:
            return None

        run = app_state(app).runs.submit(kind="chat", owner_id="operator", orchestrator=orch)
        await run.wait()
        resp = await client.post(f"/runs/{run.id}/approve", json={"decisions": []})
        assert resp.status_code == 409


async def test_approve_with_conversation_scope_records_grant(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation",
                    }
                ]
            },
        )
        assert resp.status_code == 202

        # The grant is recorded and visible on the conversation's grants surface. The tool
        # runs no command, so it is the whole-tool scope — an empty prefix.
        assert conv_id is not None
        granted = await app_state(app).approval_grants.list("operator", conv_id)
        assert (approval.tool_name, ()) in {(g.tool_name, g.command_prefix) for g in granted}
        listed = await _grants(client, conv_id)
        assert any(
            obj(g)["tool_name"] == approval.tool_name and obj(g)["command_prefix"] == []
            for g in listed
        )


def command_categories() -> dict[str, FunctionToolset[RunDeps]]:
    """A catalog holding one approval-gated tool that *runs a command* — the shape a
    conversation grant is scoped to rather than granted wholesale."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True, name="run_host_command")
    def run_host_command(command: str) -> str:
        return f"ran {command}"

    return {"code": toolset}


async def test_a_conversation_grant_on_a_command_names_the_command(
    monkeypatch: pytest.MonkeyPatch,
):
    # The scope is derived here, from the parked call, and never sent by the client: what
    # the operator said yes to is the act the agent actually asked for.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, command_categories())
        run_id = await _chat(client, "run it")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        assert approval.tool_name == "code_run_host_command"
        command = s(obj(JSON.validate_python(approval.args_as_dict()))["command"])
        conv_id = run.conversation_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation",
                    }
                ]
            },
        )
        assert resp.status_code == 202

        listed = await _grants(client, conv_id)
        assert [(obj(g)["tool_name"], obj(g)["command_prefix"]) for g in listed] == [
            ("code_run_host_command", command.split())
        ]

        # Revoking is by the same pair, and the scope goes back **exactly as it was
        # listed** — one parameter per word, never a sentence something else has to split.
        # The whole-tool form leaves the narrower grant standing; only the matching scope
        # drops it, which is what makes the listing and the delete one round trip.
        assert (
            await client.delete(f"/conversations/{conv_id}/grants/code_run_host_command")
        ).status_code == 204
        assert len(await _grants(client, conv_id)) == 1
        assert (
            await client.delete(
                f"/conversations/{conv_id}/grants/code_run_host_command",
                params={
                    "command_prefix": [s(word) for word in arr(obj(listed[0])["command_prefix"])]
                },
            )
        ).status_code == 204
        assert await _grants(client, conv_id) == []


async def test_the_wider_width_grants_the_whole_tool_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
):
    # The operator's deliberate second option: this tool, everything it runs. It derives
    # nothing from the command, so there is nothing that could fail to derive — and it is
    # recorded as decisive, which is what the reviewing level reads as their answer.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, command_categories())
        run_id = await _chat(client, "run it")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation_tool",
                    }
                ]
            },
        )
        assert resp.status_code == 202
        # The whole-tool scope is an empty prefix, and it is never reported as unscoped —
        # the operator got exactly the thing they asked for.
        assert json_body(resp) == {"status": "resuming", "granted": [[]], "unscoped": []}

        listed = await _grants(client, conv_id)
        assert [
            (obj(g)["tool_name"], obj(g)["command_prefix"], obj(g)["decisive"]) for g in listed
        ] == [("code_run_host_command", [], True)]


async def test_the_wider_width_is_still_refused_for_a_once_only_tool(
    monkeypatch: pytest.MonkeyPatch,
):
    # A grant auto-approves a tool name and nothing narrower, so a standing yes to the
    # egress request would mean every domain the agent goes on to name. Widening the width
    # the operator may ask for does not widen the set of tools it may be asked about.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, egress_categories())
        run_id = await _chat(client, "reach out")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation_tool",
                    }
                ]
            },
        )
        # The call itself is approved; only the standing part is dropped, and silently —
        # the card was not wrong to offer the box.
        assert resp.status_code == 202
        assert json_body(resp)["granted"] == []
        assert await _grants(client, conv_id) == []


async def test_a_grant_names_the_command_the_operator_edited_it_to(
    monkeypatch: pytest.MonkeyPatch,
):
    # An override *replaces* the call's arguments, so the act being approved is the edited
    # one. Deriving the standing yes from the arguments the model wrote would record a
    # grant for a command nobody is going to run — and, worse, for the one the operator
    # rejected by editing it away.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, command_categories())
        run_id = await _chat(client, "run it")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation",
                        "override_args": {"command": "echo skipped"},
                    }
                ]
            },
        )
        assert resp.status_code == 202
        assert json_body(resp)["granted"] == [["echo", "skipped"]]

        listed = await _grants(client, conv_id)
        assert [obj(g)["command_prefix"] for g in listed] == [["echo", "skipped"]]


async def test_a_command_no_scope_could_stand_for_records_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
):
    # `grant_scopes` refuses a command the grammar cannot read, which is the right call —
    # but the operator ticked a box that promised a standing yes, so the refusal is
    # reported rather than left to be noticed as a missing chip.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, command_categories())
        run_id = await _chat(client, "run it")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation",
                        "override_args": {"command": "cat $TARGET"},
                    }
                ]
            },
        )
        assert resp.status_code == 202
        assert json_body(resp) == {
            "status": "resuming",
            "granted": [],
            "unscoped": [approval.tool_call_id],
        }
        assert await _grants(client, conv_id) == []


async def test_egress_request_never_records_a_conversation_grant(
    monkeypatch: pytest.MonkeyPatch,
):
    # A conversation grant auto-approves a tool *name*, so one on the egress request would
    # not mean "this domain again" — it would mean every domain the agent goes on to name.
    # The call is approved; the scope is dropped.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, egress_categories())
        run_id = await _chat(client, "reach pypi")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id
        assert approval.tool_name == "code_request_egress"

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation",
                    }
                ]
            },
        )
        assert resp.status_code == 202

        # The call itself went through — dropping the scope must not read as a denial.
        events = await collect_sse_events(client, run_id)
        assert "tool.completed" in [e["type"] for e in events]

        # Nothing standing, on either surface the operator or the engine reads.
        assert conv_id is not None
        assert await app_state(app).approval_grants.list("operator", conv_id) == []
        assert await _grants(client, conv_id) == []


async def test_failed_resume_rolls_back_the_recorded_grant(monkeypatch: pytest.MonkeyPatch):
    # The grant is written *before* resume (so the resumed turn's inline check sees it),
    # but a resume that can't be accepted must leave no standing auto-approval behind.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id

        async def fail_resume(
            _run_id: str, _orchestrator: Orchestrator, **_kwargs: float | None
        ) -> Run | None:
            return None

        monkeypatch.setattr(app_state(app).runs, "resume", fail_resume)
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation",
                    }
                ]
            },
        )
        assert resp.status_code == 409
        assert conv_id is not None
        granted = await app_state(app).approval_grants.list("operator", conv_id)
        assert approval.tool_name not in {g.tool_name for g in granted}


async def test_a_failed_resume_leaves_a_grant_that_was_already_there(
    monkeypatch: pytest.MonkeyPatch,
):
    # The rollback drops what this request *created*, not everything it wrote. A grant
    # does not settle a call at the level that reviews, so a thread can hold a live
    # standing yes and still park on the very act it names; approving that refreshes the
    # row rather than creating one. Revoking it because a run the operator was never told
    # about failed to resume would take away something they granted earlier and never
    # withdrew.
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        run = await await_parked(app, run_id)
        approval = parked_turn(run).requests.approvals[0]
        conv_id = run.conversation_id
        assert conv_id is not None
        grants = app_state(app).approval_grants
        # Standing before this card was ever opened.
        _ = await grants.grant("operator", conv_id, approval.tool_name)

        async def fail_resume(
            _run_id: str, _orchestrator: Orchestrator, **_kwargs: float | None
        ) -> Run | None:
            return None

        monkeypatch.setattr(app_state(app).runs, "resume", fail_resume)
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [
                    {
                        "tool_call_id": approval.tool_call_id,
                        "approved": True,
                        "scope": "conversation",
                    }
                ]
            },
        )
        assert resp.status_code == 409
        granted = await grants.list("operator", conv_id)
        assert approval.tool_name in {g.tool_name for g in granted}


async def test_approve_rejects_decision_mismatch(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        _ = await await_parked(app, run_id)
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": "wrong-id", "approved": True}]},
        )
        assert resp.status_code == 400


# --- approval_needed notification: park creates it, every decision resolves it -----


async def _run_notifications(app: FastAPI, run_id: str) -> list[NotificationView]:
    items, _ = await app_state(app).notifications.list_notifications("operator", limit=100)
    return [n for n in items if n.run_id == run_id]


async def _drain_terminal_notify_tasks(app: FastAPI) -> None:
    """Await every in-flight run-terminal notify task. `_on_run_terminal` (app.py)
    adds its task to `run_terminal_tasks` synchronously before the registry's own
    cancel/`run.wait()` call can return, so it's already registered by the time a
    caller reaches here — one `gather` is enough to settle it."""
    pending = list(app_state(app).run_terminal_tasks)
    if pending:
        _ = await asyncio.gather(*pending, return_exceptions=True)


async def test_park_creates_approval_needed_and_approve_resolves_it(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        run = await await_parked(app, run_id)

        notifs = await _run_notifications(app, run_id)
        assert len(notifs) == 1
        assert notifs[0].kind == "approval_needed"
        assert notifs[0].resolved_at is None

        call_id = parked_turn(run).requests.approvals[0].tool_call_id
        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": True}]},
        )
        assert resp.status_code == 202

        notifs = await _run_notifications(app, run_id)
        assert notifs[0].resolved_at is not None


async def test_deny_resolves_the_approval_needed_notification(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        run = await await_parked(app, run_id)
        call_id = parked_turn(run).requests.approvals[0].tool_call_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": call_id, "approved": False}]},
        )
        assert resp.status_code == 202

        notifs = await _run_notifications(app, run_id)
        assert notifs[0].resolved_at is not None


async def test_cancel_while_parked_resolves_the_approval_needed_notification(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    async with client_app() as (client, app):
        swap_tool_catalog(app, danger_categories())
        run_id = await _chat(client, "delete it")
        _ = await await_parked(app, run_id)

        assert await app_state(app).runs.cancel(run_id) is True
        await _drain_terminal_notify_tasks(app)

        notifs = await _run_notifications(app, run_id)
        assert len(notifs) == 1
        assert notifs[0].resolved_at is not None
        # Cancelling never itself creates a run_completed/run_failed notification.
        assert all(n.kind == "approval_needed" for n in notifs)
