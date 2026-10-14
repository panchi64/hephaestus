"""The JMAP adapter: batched method calls in, domain models out — and the SSRF guard
that stands between an operator-supplied server URL and the host's own network."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import JsonValue

from services.mail.errors import MailAuthError, MailError
from services.mail.jmap import JmapTransport
from services.mail.models import ROLE_INBOX, ROLE_SENT, AccountSpec, MailAddress, OutgoingMail
from services.mail.transport import MailTransport

from ._json import arr, obj, parse_json, s

SESSION_URL = "https://jmap.example.com/.well-known/jmap"
API_URL = "https://jmap.example.com/api/"

SESSION: dict[str, JsonValue] = {
    "apiUrl": API_URL,
    "primaryAccounts": {"urn:ietf:params:jmap:mail": "acct-1"},
}

MAILBOXES: dict[str, JsonValue] = {
    "list": [
        {"id": "mb1", "name": "Inbox", "role": "inbox", "totalEmails": 3, "unreadEmails": 1},
        {"id": "mb2", "name": "Sent", "role": "sent"},
        {"id": "mb3", "name": "Notes", "role": None},
        {"id": "mb-trash", "name": "Trash", "role": "trash"},
    ]
}

EMAILS: list[dict[str, JsonValue]] = [
    {
        "id": "e2",
        "threadId": "t2",
        "subject": "Second",
        "from": [{"email": "ada@example.org", "name": "Ada"}],
        "to": [{"email": "operator@example.com"}],
        "receivedAt": "2026-08-13T09:05:00Z",
        "preview": "the second one",
        "keywords": {"$seen": True},
        "messageId": ["<e2@example.org>"],
        "hasAttachment": True,
        "size": 900,
    },
    {
        "id": "e1",
        "threadId": "t1",
        "subject": "First",
        "from": [{"email": "charles@example.org"}],
        "receivedAt": "2026-08-13T09:00:00Z",
        "preview": "the first one",
        "keywords": {},
    },
]

type _Call = tuple[str, dict[str, JsonValue], str]


def _spec() -> AccountSpec:
    return AccountSpec(
        account_id="a1",
        address="operator@example.com",
        provider="jmap",
        auth_kind="password",
        config={"session_url": SESSION_URL},
        password="api-token",
    )


class _Server:
    """A scripted JMAP endpoint: one session document, and a handler per method name."""

    def __init__(self, *, status: int = 200) -> None:
        self.status: int = status
        self.requests: list[httpx.Request] = []
        self.handlers: dict[str, Callable[[dict[str, JsonValue]], JsonValue]] = {
            "Mailbox/get": lambda _args: MAILBOXES,
            "Email/query": lambda _args: {"ids": [obj(e)["id"] for e in EMAILS]},
            "Email/get": self._email_get,
            "Email/set": lambda _args: {"created": {"draft": {"id": "e9"}}},
            "EmailSubmission/set": lambda _args: {"created": {"s0": {"id": "sub1"}}},
            "Identity/get": lambda _args: {
                "list": [{"id": "id1", "email": "operator@example.com"}]
            },
        }
        self.last_calls: list[_Call] = []

    def _email_get(self, args: dict[str, JsonValue]) -> JsonValue:
        ids = args.get("ids")
        if ids is None:  # the query's `#ids` back-reference
            return {"list": list(EMAILS)}
        wanted: list[JsonValue] = []
        for e in EMAILS:
            if e["id"] in arr(ids):
                email = dict(e)
                email["bodyValues"] = {"p1": {"value": "Hello from JMAP."}}
                email["textBody"] = [{"partId": "p1", "type": "text/plain"}]
                wanted.append(email)
        return {"list": wanted}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={})
        if request.method == "GET":
            return httpx.Response(200, json=SESSION)
        body = obj(parse_json(request.content))
        self.last_calls = []
        for call in arr(body["methodCalls"]):
            name, args, call_id = arr(call)
            self.last_calls.append((s(name), obj(args), s(call_id)))
        responses = [
            [name, self.handlers[name](args), call_id] for name, args, call_id in self.last_calls
        ]
        return httpx.Response(200, json={"methodResponses": responses})


def _transport(server: _Server, spec: AccountSpec | None = None) -> JmapTransport:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    return JmapTransport(spec or _spec(), client=client)


@pytest.fixture(autouse=True)
def _allow_outbound(monkeypatch: pytest.MonkeyPatch):
    """The SSRF guard resolves hostnames for real; stub it so tests do no DNS. The guard
    itself is exercised by `test_a_private_server_url_is_refused` below."""

    async def _ok(_url: str) -> None:
        return None

    monkeypatch.setattr("services.mail.jmap.assert_public_url", _ok)


def test_the_adapter_satisfies_the_transport_protocol():
    assert isinstance(JmapTransport(_spec()), MailTransport)


async def test_probe_loads_the_session_and_resolves_the_account():
    server = _Server()
    _ = await _transport(server).probe()
    assert str(server.requests[0].url) == SESSION_URL


async def test_folders_map_jmap_roles():
    folders = {f.id: f for f in await _transport(_Server()).list_folders()}
    assert folders["mb1"].role == ROLE_INBOX
    assert folders["mb1"].unread == 1
    assert folders["mb2"].role == ROLE_SENT
    assert folders["mb3"].role == "other"


async def test_a_listing_is_one_batched_round_trip():
    server = _Server()
    headers = await _transport(server).list_messages("mb1", limit=10)
    # Session GET + a single POST carrying both the query and the get.
    assert len([r for r in server.requests if r.method == "POST"]) == 1
    assert [name for name, _args, _id in server.last_calls] == ["Email/query", "Email/get"]
    assert obj(server.last_calls[1][1]["#ids"])["resultOf"] == "q0"
    assert [h.uid for h in headers] == ["e2", "e1"]
    assert headers[0].sender == MailAddress(address="ada@example.org", name="Ada")
    assert headers[0].seen is True
    assert headers[0].has_attachments is True
    assert headers[1].seen is False


async def test_incremental_listing_stops_at_the_last_seen_message():
    headers = await _transport(_Server()).list_messages("mb1", since_uid="e1")
    assert [h.uid for h in headers] == ["e2"]


async def test_fetch_resolves_body_values_by_reference():
    body = await _transport(_Server()).fetch("mb1", "e1")
    assert body.text == "Hello from JMAP."
    assert body.header.subject == "First"


async def test_flag_patches_only_the_requested_keyword():
    server = _Server()
    await _transport(server).flag("mb1", "e1", seen=True)
    update = obj(server.last_calls[0][1]["update"])["e1"]
    assert update == {"keywords/$seen": True}


async def test_move_patches_both_mailbox_memberships():
    server = _Server()
    await _transport(server).move("mb1", "e1", "mb3")
    assert obj(server.last_calls[0][1]["update"])["e1"] == {
        "mailboxIds/mb1": None,
        "mailboxIds/mb3": True,
    }


def test_an_unguarded_same_folder_patch_collapses_to_a_no_op():
    """Why `move` needs its guard, kept executable rather than described: with
    ``folder == destination`` the two computed keys are the same string, a dict literal
    keeps only the last, and the removal half disappears — leaving a patch that asks the
    server for nothing while every layer above reads it as a completed move."""
    folder = destination = "mb1"
    patch = {f"mailboxIds/{folder}": None, f"mailboxIds/{destination}": True}
    assert patch == {"mailboxIds/mb1": True}


async def test_moving_a_message_to_the_folder_it_is_already_in_does_nothing():
    server = _Server()
    await _transport(server).move("mb1", "e1", "mb1")
    assert [r for r in server.requests if r.method == "POST"] == []


async def test_deleting_from_trash_destroys_rather_than_re_trashing():
    """`delete` promises the message is gone from where it was, and the service drops it
    from the cache on return. Trashing a message already in Trash would patch nothing, so
    it would come back on the next sync — emptying the trash has to be a real destroy."""
    server = _Server()
    await _transport(server).delete("mb-trash", "e1")
    assert server.last_calls[0][1]["destroy"] == ["e1"]


async def test_deleting_from_elsewhere_still_trashes():
    server = _Server()
    await _transport(server).delete("mb1", "e1")
    assert obj(server.last_calls[0][1]["update"])["e1"] == {
        "mailboxIds/mb1": None,
        "mailboxIds/mb-trash": True,
    }


async def test_send_creates_a_draft_and_submits_it():
    server = _Server()
    message_id = await _transport(server).send(
        OutgoingMail(to=(MailAddress(address="ada@example.org"),), subject="hi", body="hello")
    )
    names = [name for name, _args, _id in server.last_calls]
    assert names == ["Email/set", "EmailSubmission/set"]
    submission = server.last_calls[1][1]
    assert obj(submission["create"])["s0"] == {"emailId": "#draft", "identityId": "id1"}
    assert submission["onSuccessDestroyEmail"] == ["#draft"]
    assert message_id == "e9"


async def test_an_unauthorized_response_is_an_auth_error():
    with pytest.raises(MailAuthError):
        _ = await _transport(_Server(status=401)).probe()


async def test_a_private_server_url_is_refused(monkeypatch: pytest.MonkeyPatch):
    """The guard is re-run per request, so a host that resolves privately is refused even
    though the account was added successfully."""
    from core.exceptions import SSRFError

    async def _blocked(_url: str) -> None:
        raise SSRFError("resolves to a private address")

    monkeypatch.setattr("services.mail.jmap.assert_public_url", _blocked)
    server = _Server()
    with pytest.raises(MailError):
        _ = await _transport(server).probe()
    assert server.requests == []
