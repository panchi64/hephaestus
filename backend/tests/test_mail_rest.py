"""The two REST adapters (Gmail, Microsoft Graph) against mocked vendor APIs.

Both are exercised through the same seam as IMAP and JMAP, so what these assert is the
vendor-shape → domain-model translation and the few places the vendors are genuinely
different (Gmail's labels-as-folders, Graph's structured body)."""

from __future__ import annotations

import base64

import httpx
import pytest
from pydantic import JsonValue

from services.mail.errors import MailAuthError
from services.mail.gmail import GmailTransport
from services.mail.graph import GraphTransport
from services.mail.models import (
    ROLE_INBOX,
    ROLE_SENT,
    ROLE_SPAM,
    ROLE_TRASH,
    AccountSpec,
    MailAddress,
    OutgoingMail,
)

from ._json import arr, obj, parse_json, s

RAW = (
    b"From: Ada Lovelace <ada@example.org>\r\n"
    b"To: operator@example.com\r\n"
    b"Subject: Engine notes\r\n"
    b"Message-ID: <n1@example.org>\r\n"
    b"\r\n"
    b"The engine weaves algebraic patterns.\r\n"
)


def _spec(provider: str) -> AccountSpec:
    return AccountSpec(
        account_id="a1",
        address="operator@example.com",
        provider=provider,
        auth_kind="oauth",
        config={},
        access_token="access-token",
    )


class _Recorder:
    """Records every request and replies from a path → payload table."""

    def __init__(self, routes: dict[str, JsonValue], *, status: int = 200) -> None:
        self.routes: dict[str, JsonValue] = routes
        self.status: int = status
        self.calls: list[tuple[str, str, JsonValue]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = parse_json(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if self.status != 200:
            return httpx.Response(self.status, json={})
        for suffix, payload in self.routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={})


def _client(recorder: _Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder.handle))


# --- Gmail --------------------------------------------------------------------

GMAIL_ROUTES: dict[str, JsonValue] = {
    "/labels": {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "messagesTotal": 4, "messagesUnread": 2},
            {"id": "SENT", "name": "SENT"},
            {"id": "SPAM", "name": "SPAM"},
            {"id": "Label_7", "name": "Receipts"},
        ]
    },
    "/messages": {"messages": [{"id": "m2"}, {"id": "m1"}]},
    "/messages/m1": {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["INBOX", "UNREAD", "STARRED"],
        "internalDate": "1786000000000",
        "sizeEstimate": 812,
        "snippet": "The engine weaves",
        "raw": base64.urlsafe_b64encode(RAW).decode(),
    },
    "/messages/m2": {
        "id": "m2",
        "threadId": "t2",
        "labelIds": ["INBOX"],
        "raw": base64.urlsafe_b64encode(RAW).decode(),
    },
}


async def test_gmail_labels_are_presented_as_folders():
    folders = {
        f.id: f
        for f in await GmailTransport(
            _spec("gmail"), client=_client(_Recorder(GMAIL_ROUTES))
        ).list_folders()
    }
    assert folders["INBOX"].role == ROLE_INBOX
    assert folders["INBOX"].unread == 2
    assert folders["SENT"].role == ROLE_SENT
    assert folders["SPAM"].role == ROLE_SPAM
    assert folders["Label_7"].role == "other"  # a user label is just a folder


async def test_gmail_reads_raw_rfc5322_through_the_shared_parser():
    body = await GmailTransport(_spec("gmail"), client=_client(_Recorder(GMAIL_ROUTES))).fetch(
        "INBOX", "m1"
    )
    assert body.text.strip() == "The engine weaves algebraic patterns."
    assert body.header.sender == MailAddress(address="ada@example.org", name="Ada Lovelace")
    assert body.header.thread_id == "t1"
    # Gmail's own state is authoritative over the RFC headers.
    assert body.header.seen is False  # carries UNREAD
    assert body.header.flagged is True  # carries STARRED
    assert body.header.size_bytes == 812


async def test_gmail_listing_stops_at_the_last_seen_message():
    headers = await GmailTransport(
        _spec("gmail"), client=_client(_Recorder(GMAIL_ROUTES))
    ).list_messages("INBOX", since_uid="m1")
    assert [h.uid for h in headers] == ["m2"]


async def test_gmail_marks_read_by_removing_the_unread_label():
    recorder = _Recorder(GMAIL_ROUTES)
    await GmailTransport(_spec("gmail"), client=_client(recorder)).flag(
        "INBOX", "m1", seen=True, flagged=False
    )
    _method, path, body = recorder.calls[-1]
    assert path.endswith("/messages/m1/modify")
    assert body == {"addLabelIds": [], "removeLabelIds": ["UNREAD", "STARRED"]}


async def test_gmail_delete_trashes_rather_than_erasing():
    recorder = _Recorder(GMAIL_ROUTES)
    await GmailTransport(_spec("gmail"), client=_client(recorder)).delete("INBOX", "m1")
    assert recorder.calls[-1][1].endswith("/messages/m1/trash")


async def test_gmail_send_posts_a_base64url_rfc5322_message():
    recorder = _Recorder(GMAIL_ROUTES)
    message_id = await GmailTransport(_spec("gmail"), client=_client(recorder)).send(
        OutgoingMail(
            to=(MailAddress(address="ada@example.org"),),
            subject="Re: Engine",
            body="Agreed.",
            in_reply_to="<n1@example.org>",
        )
    )
    _method, path, body = recorder.calls[-1]
    assert path.endswith("/messages/send")
    assert body is not None
    raw = base64.urlsafe_b64decode(s(obj(body)["raw"]))
    assert b"In-Reply-To: <n1@example.org>" in raw
    assert message_id.startswith("<")


async def test_a_rejected_token_is_an_auth_error():
    with pytest.raises(MailAuthError):
        await GmailTransport(_spec("gmail"), client=_client(_Recorder({}, status=401))).probe()


# --- Microsoft Graph -----------------------------------------------------------

GRAPH_MESSAGE: dict[str, JsonValue] = {
    "id": "g1",
    "conversationId": "c1",
    "internetMessageId": "<g1@example.org>",
    "subject": "Quarterly review",
    "from": {"emailAddress": {"address": "ada@example.org", "name": "Ada"}},
    "toRecipients": [{"emailAddress": {"address": "operator@example.com"}}],
    "ccRecipients": [{"emailAddress": {"address": "cc@example.com"}}],
    "receivedDateTime": "2026-08-13T09:00:00Z",
    "isRead": True,
    "flag": {"flagStatus": "flagged"},
    "hasAttachments": True,
    "bodyPreview": "Please review",
    "body": {
        "contentType": "html",
        "content": "<article><p>Please review the numbers "
        + "before Thursday, and let me know if anything looks wrong.</p></article>",
    },
}

GRAPH_ROUTES: dict[str, JsonValue] = {
    "/mailFolders": {
        "value": [
            {"id": "f1", "displayName": "Inbox", "totalItemCount": 5, "unreadItemCount": 1},
            {"id": "f2", "displayName": "Sent Items"},
            {"id": "f3", "displayName": "Deleted Items"},
            {"id": "f4", "displayName": "Team"},
        ]
    },
    "/mailFolders/f1/messages": {"value": [GRAPH_MESSAGE]},
    "/messages/g1": GRAPH_MESSAGE,
}


async def test_graph_folders_map_well_known_names():
    folders = {
        f.id: f
        for f in await GraphTransport(
            _spec("graph"), client=_client(_Recorder(GRAPH_ROUTES))
        ).list_folders()
    }
    assert folders["f1"].role == ROLE_INBOX
    assert folders["f1"].unread == 1
    assert folders["f2"].role == ROLE_SENT
    assert folders["f3"].role == ROLE_TRASH
    assert folders["f4"].role == "other"


async def test_graph_headers_map_structured_fields():
    headers = await GraphTransport(
        _spec("graph"), client=_client(_Recorder(GRAPH_ROUTES))
    ).list_messages("f1")
    header = headers[0]
    assert header.uid == "g1"
    assert header.sender == MailAddress(address="ada@example.org", name="Ada")
    assert [a.address for a in header.cc] == ["cc@example.com"]
    assert header.seen is True
    assert header.flagged is True
    assert header.has_attachments is True
    assert header.received_at is not None


async def test_graph_html_body_is_reduced_to_text():
    body = await GraphTransport(_spec("graph"), client=_client(_Recorder(GRAPH_ROUTES))).fetch(
        "f1", "g1"
    )
    assert "review the numbers" in body.text
    assert "<p>" not in body.text
    assert body.html is not None


async def test_graph_flag_patches_only_what_was_asked():
    recorder = _Recorder(GRAPH_ROUTES)
    await GraphTransport(_spec("graph"), client=_client(recorder)).flag("f1", "g1", seen=False)
    method, path, body = recorder.calls[-1]
    assert (method, path.endswith("/messages/g1")) == ("PATCH", True)
    assert body == {"isRead": False}


async def test_graph_send_threads_with_internet_message_headers():
    recorder = _Recorder(GRAPH_ROUTES)
    _ = await GraphTransport(_spec("graph"), client=_client(recorder)).send(
        OutgoingMail(
            to=(MailAddress(address="ada@example.org"),),
            subject="Re: Quarterly review",
            body="Looks right.",
            in_reply_to="<g1@example.org>",
            references=("<root@example.org>",),
        )
    )
    _method, path, body = recorder.calls[-1]
    assert path.endswith("/sendMail")
    assert body is not None
    message = obj(obj(body)["message"])
    headers = {
        s(obj(h)["name"]): s(obj(h)["value"]) for h in arr(message["internetMessageHeaders"])
    }
    assert headers["In-Reply-To"] == "<g1@example.org>"
    assert headers["References"] == "<root@example.org> <g1@example.org>"
