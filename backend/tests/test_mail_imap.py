"""The IMAP/SMTP adapter, driven against a stub ``aioimaplib`` client.

No live server: the point under test is the wire-response → domain-model translation and
the failure mapping (a refused login is an auth error, a dropped socket is an
unavailability the next call recovers from), all of which are ours.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import override

import aioimaplib
import pytest

from services.mail.errors import MailAuthError, MailError, MailUnavailableError
from services.mail.imap import ImapTransport, first_literal, parse_list_line, search_uids
from services.mail.models import ROLE_SENT, ROLE_SPAM, AccountSpec

RAW = (
    b"From: Ada Lovelace <ada@example.org>\r\n"
    b"To: operator@example.com\r\n"
    b"Subject: Analytical Engine\r\n"
    b"Message-ID: <note-1@example.org>\r\n"
    b"Date: Thu, 13 Aug 2026 09:00:00 +0000\r\n"
    b"\r\n"
    b"The engine weaves algebraic patterns.\r\n"
)


@dataclass
class _Response:
    result: str
    lines: list[object]


class _StubTransport:
    """aioimaplib parks the asyncio transport on the protocol object, and closing it is
    the only thing that actually hands the socket back — LOGOUT does not."""

    def __init__(self) -> None:
        self.closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class _StubProtocol:
    transport: _StubTransport


class _StubClient:
    """Just enough of ``aioimaplib.IMAP4_SSL`` for the adapter's command surface.

    ``capabilities`` is part of that surface: the adapter asks before issuing MOVE or a
    UID EXPUNGE, because the real client raises rather than answering when the server
    never advertised them. Default to the modern set; a test narrows it to play an older
    server.
    """

    def __init__(
        self, *, login_ok: bool = True, capabilities: Iterable[str] = ("MOVE", "UIDPLUS")
    ) -> None:
        self.login_ok: bool = login_ok
        self.capabilities: set[str] = set(capabilities)
        self.commands: list[tuple[object, ...]] = []
        self.selected: list[str] = []
        self.protocol: _StubProtocol = _StubProtocol(transport=_StubTransport())

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    async def wait_hello_from_server(self) -> None:
        return None

    async def login(self, user: str, _password: str) -> _Response:
        self.commands.append(("login", user))
        return _Response("OK" if self.login_ok else "NO", [])

    async def xoauth2(self, user: str, token: str) -> _Response:
        self.commands.append(("xoauth2", user, token))
        return _Response("OK" if self.login_ok else "NO", [])

    async def select(self, folder: str) -> _Response:
        self.selected.append(folder)
        return _Response("OK", [])

    async def list(self, _reference: str, _pattern: str) -> _Response:
        return _Response(
            "OK",
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
                b'(\\HasNoChildren) "/" "Junk"',
            ],
        )

    async def uid_search(self, criteria: str) -> _Response:
        self.commands.append(("search", criteria))
        return _Response("OK", [b"1 2 3"])

    async def uid(self, verb: str, *args: str) -> _Response:
        self.commands.append((verb, *args))
        if verb == "fetch":
            return _Response("OK", [b"1 FETCH (FLAGS (\\Seen) RFC822.SIZE 120", RAW, b")"])
        return _Response("OK", [])

    async def expunge(self) -> _Response:
        # The folder-wide form. It permanently destroys every `\Deleted` message in the
        # mailbox, including ones another client staged, so the adapter must never issue
        # it — scoped UID EXPUNGE arrives through `uid` above. Failing loudly here is what
        # keeps a regression from quietly costing the operator someone else's mail.
        raise AssertionError("the adapter issued a folder-wide EXPUNGE")

    async def logout(self) -> _Response:
        self.commands.append(("logout",))
        return _Response("OK", [])


def _spec(auth_kind: str = "password", access_token: str | None = None) -> AccountSpec:
    return AccountSpec(
        account_id="a1",
        address="operator@example.com",
        provider="imap",
        auth_kind=auth_kind,
        config={
            "imap_host": "mail.example.com",
            "imap_port": 993,
            "smtp_host": "mail.example.com",
        },
        password="hunter2",
        access_token=access_token,
    )


def _install(monkeypatch: pytest.MonkeyPatch, client: _StubClient) -> None:
    def factory(**_kwargs: object) -> _StubClient:
        return client

    monkeypatch.setattr(aioimaplib, "IMAP4_SSL", factory)


async def test_probe_logs_in(monkeypatch: pytest.MonkeyPatch):
    client = _StubClient()
    _install(monkeypatch, client)
    await ImapTransport(_spec()).probe()
    assert client.commands[0] == ("login", "operator@example.com")


async def test_an_oauth_account_authenticates_with_xoauth2(monkeypatch: pytest.MonkeyPatch):
    client = _StubClient()
    _install(monkeypatch, client)
    await ImapTransport(_spec(auth_kind="oauth", access_token="ya29.token")).probe()
    assert client.commands[0] == ("xoauth2", "operator@example.com", "ya29.token")


async def test_a_refused_login_is_an_auth_error(monkeypatch: pytest.MonkeyPatch):
    client = _StubClient(login_ok=False)
    _install(monkeypatch, client)
    with pytest.raises(MailAuthError):
        await ImapTransport(_spec()).probe()
    # A connection that is never returned is holding a socket no one else can reach: the
    # handle lives only inside `_connect`, and the sync loop retries every few minutes.
    assert client.protocol.transport.closed is True


async def test_an_unreachable_server_is_an_availability_error(monkeypatch: pytest.MonkeyPatch):
    class _Dead(_StubClient):
        @override
        async def wait_hello_from_server(self) -> None:
            raise OSError("connection refused")

    client = _Dead()
    _install(monkeypatch, client)
    with pytest.raises(MailUnavailableError):
        await ImapTransport(_spec()).probe()
    assert client.protocol.transport.closed is True


async def test_a_dropped_connection_is_retried_on_the_next_call(monkeypatch: pytest.MonkeyPatch):
    class _Flaky(_StubClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_search: bool = True

        @override
        async def uid_search(self, criteria: str) -> _Response:
            if self.fail_next_search:
                self.fail_next_search = False
                raise TimeoutError
            return await super().uid_search(criteria)

    client = _Flaky()
    _install(monkeypatch, client)
    transport = ImapTransport(_spec())
    assert client.protocol.transport.closed is False  # control: nothing has closed it yet
    with pytest.raises(MailUnavailableError):
        _ = await transport.list_messages("INBOX")
    # Dropped means released, not just forgotten. Servers time idle IMAP sessions out
    # routinely, and a client left behind holds its socket and TLS session for as long as
    # the process runs — one leak per drop, with nothing to bound it.
    assert client.protocol.transport.closed is True
    # The failed command dropped the cached connection, so this one logs in afresh.
    assert await transport.list_messages("INBOX")
    assert [c for c in client.commands if c[0] == "login"]


async def test_close_logs_out_and_then_releases_the_socket(monkeypatch: pytest.MonkeyPatch):
    """LOGOUT is a courtesy to the server, not a release on this side — aioimaplib leaves
    the transport open either way, so the socket is closed explicitly after it."""
    client = _StubClient()
    _install(monkeypatch, client)
    transport = ImapTransport(_spec())
    await transport.probe()
    await transport.close()
    assert client.commands[-1] == ("logout",)
    assert client.protocol.transport.closed is True


async def test_folders_carry_special_use_and_name_derived_roles(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch, _StubClient())
    folders = {f.id: f for f in await ImapTransport(_spec()).list_folders()}
    assert set(folders) == {"INBOX", "[Gmail]/Sent Mail", "Junk"}
    assert folders["[Gmail]/Sent Mail"].role == ROLE_SENT
    assert folders["[Gmail]/Sent Mail"].name == "Sent Mail"
    assert folders["Junk"].role == ROLE_SPAM  # no special-use flag — matched by name


async def test_listing_maps_wire_responses_to_headers(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch, _StubClient())
    headers = await ImapTransport(_spec()).list_messages("INBOX", limit=2)
    assert [h.uid for h in headers] == ["3", "2"]
    assert headers[0].sender.address == "ada@example.org"
    assert headers[0].sender.name == "Ada Lovelace"
    assert headers[0].subject == "Analytical Engine"
    assert headers[0].seen is True
    assert "algebraic patterns" in headers[0].snippet


async def test_incremental_listing_drops_the_boundary_uid(monkeypatch: pytest.MonkeyPatch):
    client = _StubClient()
    _install(monkeypatch, client)
    headers = await ImapTransport(_spec()).list_messages("INBOX", since_uid="2")
    assert ("search", "UID 3:* UNDELETED") in client.commands
    assert [h.uid for h in headers] == ["3"]


async def test_listings_hide_messages_flagged_deleted(monkeypatch: pytest.MonkeyPatch):
    """A `\\Deleted` message is gone to every mail client, and on a server without UIDPLUS
    that flag is all a delete leaves behind — listing it would resurrect it."""
    client = _StubClient()
    _install(monkeypatch, client)
    _ = await ImapTransport(_spec()).list_messages("INBOX")
    assert ("search", "UNDELETED") in client.commands


async def test_fetch_returns_a_parsed_body(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch, _StubClient())
    body = await ImapTransport(_spec()).fetch("INBOX", "1")
    assert body.text.strip() == "The engine weaves algebraic patterns."
    assert body.header.message_id == "<note-1@example.org>"


async def test_flag_writes_only_the_requested_flags(monkeypatch: pytest.MonkeyPatch):
    client = _StubClient()
    _install(monkeypatch, client)
    await ImapTransport(_spec()).flag("INBOX", "1", seen=True)
    stores = [c for c in client.commands if c[0] == "store"]
    assert stores == [("store", "1", "+FLAGS", "(\\Seen)")]


async def test_move_falls_back_to_copy_when_the_server_refuses_move(
    monkeypatch: pytest.MonkeyPatch,
):
    class _NoMove(_StubClient):
        @override
        async def uid(self, verb: str, *args: str) -> _Response:
            if verb == "move":
                self.commands.append((verb, *args))
                return _Response("NO", [])
            return await super().uid(verb, *args)

    client = _NoMove()
    _install(monkeypatch, client)
    await ImapTransport(_spec()).move("INBOX", "1", "Archive")
    assert [c[0] for c in client.commands][-3:] == ["copy", "store", "expunge"]
    assert client.commands[-1] == ("expunge", "1")  # scoped to the message just copied


async def test_move_skips_straight_to_the_fallback_when_move_is_unadvertised(
    monkeypatch: pytest.MonkeyPatch,
):
    """aioimaplib raises instead of answering when MOVE was never advertised, so the
    capability has to be read before issuing — otherwise the fallback is unreachable on
    exactly the servers that need it, and the move surfaces as a dropped connection."""
    client = _StubClient(capabilities=("UIDPLUS",))
    _install(monkeypatch, client)
    await ImapTransport(_spec()).move("INBOX", "1", "Archive")
    verbs = [c[0] for c in client.commands]
    assert "move" not in verbs
    assert verbs[-3:] == ["copy", "store", "expunge"]


async def test_delete_expunges_only_the_message_it_was_given(monkeypatch: pytest.MonkeyPatch):
    client = _StubClient()
    _install(monkeypatch, client)
    await ImapTransport(_spec()).delete("INBOX", "2")
    assert client.commands[-2:] == [
        ("store", "2", "+FLAGS", "(\\Deleted)"),
        ("expunge", "2"),  # UID EXPUNGE (RFC 4315), not the folder-wide form
    ]
    # And the form it replaced, kept executable: `delete` used to end in a bare
    # `client.expunge()`. Reintroducing it here or in the move fallback trips the stub.
    with pytest.raises(AssertionError, match="folder-wide EXPUNGE"):
        _ = await client.expunge()


async def test_delete_without_uidplus_leaves_the_message_flagged(monkeypatch: pytest.MonkeyPatch):
    """No UIDPLUS means no way to narrow an expunge, so nothing is expunged: the message
    is left flagged `\\Deleted`, which is recoverable and hidden from listings. Reaping it
    would take every other client's pending deletion in the folder with it."""
    client = _StubClient(capabilities=("MOVE",))
    _install(monkeypatch, client)
    await ImapTransport(_spec()).delete("INBOX", "2")
    assert [c[0] for c in client.commands] == ["login", "store"]
    assert client.commands[-1] == ("store", "2", "+FLAGS", "(\\Deleted)")


async def test_a_missing_folder_is_a_domain_error(monkeypatch: pytest.MonkeyPatch):
    class _NoFolder(_StubClient):
        @override
        async def select(self, folder: str) -> _Response:
            return _Response("NO", [])

    _install(monkeypatch, _NoFolder())
    with pytest.raises(MailError):
        _ = await ImapTransport(_spec()).fetch("Nope", "1")


def test_response_parsing_helpers():
    assert search_uids([b"* SEARCH", b"4 5 6"]) == ["4", "5", "6"]
    assert search_uids([b"* SEARCH"]) == []
    assert first_literal([b"1 FETCH (", RAW, b")"]) == RAW
    assert first_literal([b")"]) is None
    assert parse_list_line(b'(\\Noselect) "/" "[Gmail]"') is None
