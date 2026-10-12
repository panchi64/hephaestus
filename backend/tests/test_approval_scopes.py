"""The approval-scope vocabulary (`GET /tools/approval-scopes`).

The set of tools a conversation grant (`AE-3.7`) or a scheduled task's pre-authorization
(`AE-3.5`) may name has to be *derived*, not written down: it spans three sources that
each change independently — tools marked `requires_approval=True`, tools that raise
`ApprovalRequired` at call time, and the operator's own external tools, whose names come
from servers and connectors registered at runtime.
"""

from __future__ import annotations

import json

from core.db import in_session
from models.external_tool import McpServer

from ._helpers import client_app


def _names(body) -> set[str]:
    return {row["name"] for row in body}


async def test_statically_marked_tools_are_scopes():
    """A `requires_approval=True` tool is read off the live registry, so one lands in the
    vocabulary the day it lands in the catalog — mail send/reply and the vault reads were
    all missing from the constant this replaced."""
    async with client_app() as (client, _app):
        body = (await client.get("/tools/approval-scopes")).json()
    names = _names(body)
    assert {
        "code_run_host_command",
        "mail_send",
        "mail_reply",
        "vault_list_entries",
        "vault_get_entry",
        "skills_edit",
    } <= names


async def test_once_only_tools_are_not_scopes():
    """The egress request pauses a run, but a grant on its name would cover every domain
    the agent goes on to ask for, so the engine never records one. Listing it would put a
    checkbox on the task form that stores a scope the fire silently drops — so it is
    absent from the vocabulary and refused on write, the same way a typo is."""
    async with client_app() as (client, _app):
        names = _names((await client.get("/tools/approval-scopes")).json())
        assert "code_request_egress" not in names
        resp = await client.post(
            "/tasks",
            json={
                "kind": "agent",
                "title": "t",
                "prompt": "p",
                "schedule": {"type": "once", "runAt": "2999-01-01T00:00:00Z"},
                "output": "chat",
                "preAuthorized": ["code_request_egress"],
            },
        )
    assert resp.status_code == 422
    assert "code_request_egress" in resp.json()["detail"]


async def test_conditionally_gated_tools_are_scopes():
    """The gates that fire from inside the call can't be discovered by inspection, so
    they're listed explicitly — but every name must still resolve to a real tool."""
    async with client_app() as (client, _app):
        body = (await client.get("/tools/approval-scopes")).json()
        catalog = {t["name"] for t in (await client.get("/tools")).json()}
    names = _names(body)
    assert "platform_search" in names
    assert names <= catalog | {n for n in names if n.startswith("external_")}


async def test_ungated_tools_are_not_scopes():
    """The vocabulary is the tools that can *pause* a run — pre-authorizing something
    that never asks would be a checkbox that grants nothing."""
    async with client_app() as (client, _app):
        body = (await client.get("/tools/approval-scopes")).json()
    names = _names(body)
    ungated_names = (
        "builtin_now",
        "mail_read",
        "mail_list_messages",
        # Long-term memory is the operator's own notes — the recall gate covers content
        # someone else wrote, which this never is.
        "memory_recall",
        # A skill the agent writes is a draft the operator must publish before it can
        # ever reach the model — their review already stands where the prompt would.
        "skills_create",
    )
    for ungated in ungated_names:
        assert ungated not in names


async def test_external_tools_appear_as_scopes_and_validate():
    """The reason this can't be a constant. A server the operator registers contributes
    `external_{slug}_{tool}` names that no list written ahead of time could contain — and
    a task must be able to pre-authorize one."""
    async with client_app() as (client, app):
        # Through `in_session`, never a raw `Session(engine)`: the in-memory test
        # engine shares one connection, and only `in_session` takes the per-engine
        # lock that keeps this write from interleaving with a background drainer's
        # (a raw session's BEGIN can land inside another thread's open transaction).
        await in_session(
            app.state.db_engine,
            lambda session: session.add(
                McpServer(
                    owner_id="operator",
                    name="Ticket Desk",
                    slug="ticket_desk",
                    transport="http",
                    url="https://tickets.example.com/mcp",
                    status="connected",
                    tools_json=json.dumps(
                        [{"name": "close_ticket", "description": "Close a ticket."}]
                    ),
                )
            ),
        )

        names = _names((await client.get("/tools/approval-scopes")).json())
        assert "external_ticket_desk_close_ticket" in names

        created = await client.post(
            "/tasks",
            json={
                "kind": "agent",
                "title": "close stale tickets",
                "prompt": "close anything untouched for 90 days",
                "schedule": {"type": "once", "runAt": "2999-01-01T00:00:00Z"},
                "output": "chat",
                "preAuthorized": ["external_ticket_desk_close_ticket"],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["preAuthorized"] == ["external_ticket_desk_close_ticket"]


async def test_unknown_scope_is_still_rejected():
    """Deriving the vocabulary must not weaken it — a name that matches no tool is a
    typo, and a task carrying it would pre-authorize nothing while looking like it did."""
    async with client_app() as (client, _app):
        resp = await client.post(
            "/tasks",
            json={
                "kind": "agent",
                "title": "t",
                "prompt": "p",
                "schedule": {"type": "once", "runAt": "2999-01-01T00:00:00Z"},
                "output": "chat",
                "preAuthorized": ["external_nope_do_it", "mail_read"],
            },
        )
    assert resp.status_code == 422
    assert "external_nope_do_it" in resp.json()["detail"]
    assert "mail_read" in resp.json()["detail"]
