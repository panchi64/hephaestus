"""The `/mail` surface (`EMAIL-1..5`): what the Email screen reads and writes, and how a
provider failure is reported rather than leaked."""

from __future__ import annotations

from dataclasses import replace

import httpx
from fastapi import FastAPI
from pydantic import JsonValue

from routes.deps import app_state
from services.mail.models import MailBody
from services.mail.service import AccountView

from ._helpers import client_app
from ._json import json_body, json_list, obj, parse_json, s
from .mail_fakes import FakeTransport, install_transport, sample_header


async def _connected(
    app: FastAPI, *, subject: str = "Quarterly report", text: str = "The report is attached.\n"
) -> tuple[AccountView, FakeTransport]:
    """An account with a fake transport already wired, so nothing touches a network."""
    mail = app_state(app).mail
    account = await mail.create_account(
        "operator", name="Personal", address="operator@example.com", password="hunter2"
    )
    transport = FakeTransport()
    transport.messages["INBOX"] = [
        MailBody(header=replace(sample_header("1"), subject=subject), text=text)
    ]
    await install_transport(mail, "operator", account.id, transport)
    return account, transport


async def _boot(client: httpx.AsyncClient) -> None:
    """Force the lazily-built mail service onto ``app.state`` the way a request does."""
    _ = await client.get("/mail/accounts")


async def _only_message(client: httpx.AsyncClient, account_id: str) -> dict[str, JsonValue]:
    [listed] = json_list(await client.get(f"/mail/messages?account_id={account_id}"))
    return obj(listed)


async def test_accounts_round_trip_without_ever_returning_the_secret():
    async with client_app() as (client, _app):
        created = await client.post(
            "/mail/accounts",
            json={
                "name": "Personal",
                "address": "operator@example.com",
                "password": "hunter2",
            },
        )
        assert created.status_code == 201
        body = json_body(created)
        assert body["address"] == "operator@example.com"
        assert body["status"] == "untested"
        assert "hunter2" not in created.text
        assert "secret" not in body and "password" not in body

        listed = json_list(await client.get("/mail/accounts"))
        assert [obj(a)["id"] for a in listed] == [body["id"]]

        assert (await client.delete(f"/mail/accounts/{body['id']}")).status_code == 204
        assert json_list(await client.get("/mail/accounts")) == []


async def test_an_account_needs_an_address():
    async with client_app() as (client, _app):
        resp = await client.post("/mail/accounts", json={"address": "   "})
        assert resp.status_code == 422


async def test_a_missing_account_is_a_404_not_a_crash():
    async with client_app() as (client, _app):
        assert (await client.get("/mail/accounts/nope/folders")).status_code == 404
        assert (await client.post("/mail/accounts/nope/probe")).status_code == 404
        assert (await client.delete("/mail/accounts/nope")).status_code == 404


async def test_the_listing_carries_the_triage_the_backend_decided():
    """Urgency, tags, spam and the summary are backend verdicts — the screen renders
    them, it never derives them."""
    async with client_app() as (client, app):
        await _boot(client)
        account, _transport = await _connected(app)
        resp = await client.get(f"/mail/messages?account_id={account.id}")
        assert resp.status_code == 200
        [row] = json_list(resp)
        message = obj(row)

    assert message["accountId"] == account.id
    assert message["folderId"] == "INBOX"
    assert message["from"] == "ada@example.org"
    assert message["subject"] == "Quarterly report"
    assert message["body"] == ""  # a listing carries no body
    assert set(message) >= {"urgency", "tags", "spam", "summary", "read", "flagged"}


async def test_reading_a_message_returns_the_body_split_from_its_quoted_history():
    """`EMAIL-4` — the reading pane can show the sender's own prose without the thread."""
    async with client_app() as (client, app):
        await _boot(client)
        account, _transport = await _connected(
            app,
            text="Sounds good.\n\nOn Mon, Ada wrote:\n> the original question\n",
        )
        listed = await _only_message(client, account.id)
        read = json_body(await client.get(f"/mail/messages/{listed['id']}"))

    assert "Sounds good." in s(read["body"])
    assert s(read["replyText"]).strip() == "Sounds good."
    assert read["quotedText"] is not None
    assert "the original question" in s(read["quotedText"])


async def test_marking_read_writes_through_to_the_provider():
    async with client_app() as (client, app):
        await _boot(client)
        account, transport = await _connected(app)
        listed = await _only_message(client, account.id)
        assert listed["read"] is False

        resp = await client.patch(f"/mail/messages/{listed['id']}", json={"read": True})
        assert resp.status_code == 204
        assert transport.flagged == [("INBOX", "1", True, None)]

        again = await _only_message(client, account.id)
        assert again["read"] is True


async def test_sending_needs_a_recipient():
    async with client_app() as (client, app):
        await _boot(client)
        account, _transport = await _connected(app)
        resp = await client.post(
            "/mail/send", json={"account_id": account.id, "to": [], "subject": "hi"}
        )
        assert resp.status_code == 422


async def test_the_operator_can_send_without_an_approval_gate():
    """Pressing SEND in your own client *is* the consent — the gate is on the agent's
    tool, not here."""
    async with client_app() as (client, app):
        await _boot(client)
        account, transport = await _connected(app)
        resp = await client.post(
            "/mail/send",
            json={
                "account_id": account.id,
                "to": ["ada@example.org"],
                "subject": "Re: report",
                "body": "On its way.",
            },
        )
        assert resp.status_code == 200
        assert json_body(resp)["messageId"]

    [sent] = transport.sent
    assert [a.address for a in sent.to] == ["ada@example.org"]
    assert sent.body == "On its way."


async def test_a_reply_threads_the_original():
    async with client_app() as (client, app):
        await _boot(client)
        account, transport = await _connected(app)
        listed = await _only_message(client, account.id)
        resp = await client.post(
            f"/mail/messages/{listed['id']}/reply", json={"body": "Understood."}
        )
        assert resp.status_code == 200

    [sent] = transport.sent
    assert sent.subject.startswith("Re: ")
    assert [a.address for a in sent.to] == ["ada@example.org"]


async def test_a_missing_message_is_a_404_on_every_message_endpoint():
    async with client_app() as (client, app):
        await _boot(client)
        _ = await _connected(app)
        assert (await client.get("/mail/messages/nope")).status_code == 404
        assert (await client.patch("/mail/messages/nope", json={"read": True})).status_code == 404
        assert (await client.delete("/mail/messages/nope")).status_code == 404
        assert (
            await client.post("/mail/messages/nope/reply", json={"body": "x"})
        ).status_code == 404


async def test_folders_report_their_normalized_role():
    """So the screen can find Sent without telling IMAP flags from Gmail label ids."""
    async with client_app() as (client, app):
        await _boot(client)
        account, _transport = await _connected(app)
        folders = [
            obj(f) for f in json_list(await client.get(f"/mail/accounts/{account.id}/folders"))
        ]

    assert [f["id"] for f in folders]
    inbox = next(f for f in folders if f["role"] == "inbox")
    assert inbox["accountId"] == account.id
    assert isinstance(inbox["count"], int)


async def test_the_style_profile_is_absent_until_one_exists_then_operator_editable():
    """`EMAIL-3` — the operator's own words outrank ours, which is what `edited` records."""
    async with client_app() as (client, _app):
        await _boot(client)
        assert parse_json((await client.get("/mail/style-profile")).content) is None

        saved = await client.put("/mail/style-profile", json={"profile": "Terse. No filler."})
        assert saved.status_code == 200
        assert json_body(saved) == {
            "profile": "Terse. No filler.",
            "sampleCount": 0,
            "edited": True,
        }
        assert json_body(await client.get("/mail/style-profile"))["edited"] is True


async def test_an_empty_style_profile_is_refused():
    async with client_app() as (client, _app):
        await _boot(client)
        assert (await client.put("/mail/style-profile", json={"profile": ""})).status_code == 422


async def test_suggestions_degrade_to_none_with_no_utility_model_bound():
    """A workspace with nothing to generate from returns an empty list, not a 500."""
    async with client_app() as (client, app):
        await _boot(client)
        account, _transport = await _connected(app)
        listed = await _only_message(client, account.id)
        resp = await client.get(f"/mail/messages/{listed['id']}/suggestions")

    assert resp.status_code == 200
    assert json_list(resp) == []
