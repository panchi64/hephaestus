"""Answering a guarded call over HTTP: only the operator's session, and never a standing yes."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import FunctionToolset

from services.permissions.guards import GUARD_KEY
from tools import RunDeps

from ._helpers import await_parked, client_app, parked_turn, swap_tool_catalog
from ._json import json_body, json_list, s
from .test_approval_routes import command_categories, install_sensitive_tool

_ROUTE = "/runs/{run_id}/approve"


async def _operator_and_token(client: httpx.AsyncClient) -> dict[str, str]:
    _ = await client.post("/setup", json={"password": "the-password"})
    issued = await client.post("/tokens", json={"label": "cli", "scopes": ["chat"]})
    return {"Authorization": f"Bearer {s(json_body(issued)['token'])}"}


async def _park(
    client: httpx.AsyncClient, app: FastAPI, *, guarded: bool
) -> tuple[str, str, str | None]:
    swap_tool_catalog(app, command_categories())
    run_id = s(json_body(await client.post("/chat", json={"prompt": "run it"}))["run_id"])
    run = await await_parked(app, run_id)
    call_id = parked_turn(run).requests.approvals[0].tool_call_id
    if guarded:
        parked_turn(run).requests.metadata[call_id] = {GUARD_KEY: "Removes your home."}
    return run_id, call_id, run.conversation_id


async def _grants(client: httpx.AsyncClient, conversation_id: str | None) -> list[object]:
    return list(json_list(await client.get(f"/conversations/{conversation_id}/grants")))


def _decision(call_id: str, **extra: object) -> dict[str, object]:
    return {"decisions": [{"tool_call_id": call_id, "approved": True, **extra}]}


async def test_an_api_token_cannot_approve_a_guarded_call(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        token = await _operator_and_token(client)
        run_id, call_id, _ = await _park(client, app, guarded=True)
        session = dict(client.cookies)
        client.cookies.clear()

        refused = await client.post(
            _ROUTE.format(run_id=run_id), json=_decision(call_id), headers=token
        )
        assert refused.status_code == 403

        client.cookies.update(session)
        status = json_body(await client.get(f"/runs/{run_id}"))
        assert status["status"] == "awaiting_input"


async def test_the_operator_approves_a_guarded_call_and_no_grant_is_left(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        _ = await _operator_and_token(client)
        run_id, call_id, conversation_id = await _park(client, app, guarded=True)

        resp = await client.post(
            _ROUTE.format(run_id=run_id), json=_decision(call_id, scope="conversation_tool")
        )

        assert resp.status_code == 202
        assert json_body(resp)["granted"] == []
        assert await _grants(client, conversation_id) == []


async def test_replacement_arguments_that_are_guarded_need_the_operator_and_leave_no_grant(
    monkeypatch: pytest.MonkeyPatch,
):
    install_sensitive_tool(monkeypatch)
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        token = await _operator_and_token(client)
        run_id, call_id, conversation_id = await _park(client, app, guarded=False)
        replaced = _decision(call_id, scope="conversation", override_args={"command": "rm -rf ~"})
        session = dict(client.cookies)
        client.cookies.clear()

        refused = await client.post(_ROUTE.format(run_id=run_id), json=replaced, headers=token)
        assert refused.status_code == 403

        client.cookies.update(session)
        resp = await client.post(_ROUTE.format(run_id=run_id), json=replaced)
        assert resp.status_code == 202
        assert json_body(resp)["granted"] == []
        assert await _grants(client, conversation_id) == []


async def test_a_token_still_approves_an_ordinary_call(monkeypatch: pytest.MonkeyPatch):
    install_sensitive_tool(monkeypatch)
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        token = await _operator_and_token(client)
        run_id, call_id, _ = await _park(client, app, guarded=False)
        client.cookies.clear()

        resp = await client.post(
            _ROUTE.format(run_id=run_id), json=_decision(call_id), headers=token
        )

        assert resp.status_code == 202


def _file_categories() -> dict[str, FunctionToolset[RunDeps]]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool_plain(requires_approval=True, name="write_file")
    def write_file(path: str, content: str) -> str:
        return f"wrote {path} ({len(content)})"

    return {"files": toolset}


_HOOK = {"path": ".git/hooks/x", "content": "x"}


async def _unplaceable(monkeypatch: pytest.MonkeyPatch, *, restored: bool) -> None:
    """No home to place paths in: a restored park whose deps are not rebuilt, or a live
    one whose home cannot be opened."""

    async def no_home(_deps: RunDeps) -> None:
        return None

    monkeypatch.setattr("routes.runs.home_of", no_home)
    if restored:

        async def not_rebuilt(*_args: object) -> bool:
            return False

        monkeypatch.setattr("routes.runs.recompose", not_rebuilt)


@pytest.mark.parametrize("restored", [True, False])
async def test_a_token_cannot_redirect_a_file_write_it_cannot_place(
    monkeypatch: pytest.MonkeyPatch, restored: bool
):
    install_sensitive_tool(monkeypatch)
    async with client_app(auth_enabled=True, passphrase=None) as (client, app):
        token = await _operator_and_token(client)
        swap_tool_catalog(app, _file_categories())
        run_id = s(json_body(await client.post("/chat", json={"prompt": "write it"}))["run_id"])
        run = await await_parked(app, run_id)
        parked = parked_turn(run)
        call_id = parked.requests.approvals[0].tool_call_id
        assert parked.requests.approvals[0].tool_name == "files_write_file"
        if restored:
            parked.agent = None
            parked.compaction = None
        await _unplaceable(monkeypatch, restored=restored)
        redirected = _decision(call_id, scope="conversation", override_args=_HOOK)
        session = dict(client.cookies)
        client.cookies.clear()

        refused = await client.post(_ROUTE.format(run_id=run_id), json=redirected, headers=token)
        assert refused.status_code == 403

        client.cookies.update(session)
        resp = await client.post(_ROUTE.format(run_id=run_id), json=redirected)
        assert resp.status_code == 202
        assert json_body(resp)["granted"] == []
