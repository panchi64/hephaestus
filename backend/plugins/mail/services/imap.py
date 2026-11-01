"""IMAP + SMTP — the universal baseline transport.

Reading is ``aioimaplib`` (asyncio IMAP4rev1, including IDLE so a mailbox pushes rather
than being polled); sending is ``aiosmtplib``. Both are pure-Python asyncio clients over
the stdlib's TLS, so this adapter carries no platform-specific facility (`XC-PORT-1`).

Connections are **opened lazily and held**: IMAP login is expensive (TLS handshake +
auth), and the sync loop comes back to the same mailbox repeatedly. Every command
re-establishes the connection if it dropped, so a server that times the session out
recovers on the next call instead of failing it.

An OAuth account authenticates with ``XOAUTH2`` using the access token the service opened
and refreshed; a password account uses ``LOGIN``. Either way, the secret arrives as a
value on the :class:`~plugins.mail.services.models.AccountSpec` — this adapter never touches the
vault or the database.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import suppress
from dataclasses import replace
from typing import override

import aioimaplib
import aiosmtplib

from .errors import MailAuthError, MailError, MailUnavailableError
from .models import (
    ROLE_ARCHIVE,
    ROLE_DRAFTS,
    ROLE_INBOX,
    ROLE_OTHER,
    ROLE_SENT,
    ROLE_SPAM,
    ROLE_TRASH,
    AccountSpec,
    MailAddress,
    MailBody,
    MailFolder,
    MailHeader,
    OutgoingMail,
    TransportCapabilities,
)
from .parse import build_outgoing, parse_message
from .transport import MailTransport, WatchableTransport

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 20.0
# How long an IDLE wait is held before it is renewed. RFC 2177 requires a re-issue at
# least every 29 minutes; servers are stricter in practice, so renew well inside that.
_IDLE_RENEW_S = 240.0

# IMAP special-use flags (RFC 6154) → our normalized roles. A server without special-use
# falls back to the well-known-name map below.
_SPECIAL_USE = {
    "\\inbox": ROLE_INBOX,
    "\\sent": ROLE_SENT,
    "\\drafts": ROLE_DRAFTS,
    "\\trash": ROLE_TRASH,
    "\\archive": ROLE_ARCHIVE,
    "\\junk": ROLE_SPAM,
    "\\all": ROLE_ARCHIVE,
}
_NAME_ROLES = {
    "inbox": ROLE_INBOX,
    "sent": ROLE_SENT,
    "sent items": ROLE_SENT,
    "sent mail": ROLE_SENT,
    "drafts": ROLE_DRAFTS,
    "trash": ROLE_TRASH,
    "deleted items": ROLE_TRASH,
    "archive": ROLE_ARCHIVE,
    "junk": ROLE_SPAM,
    "spam": ROLE_SPAM,
    "junk e-mail": ROLE_SPAM,
}

# `LIST` replies look like: (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+("?(?P<sep>[^"]*)"?|NIL)\s+(?P<name>.+)$')


class ImapTransport(MailTransport, WatchableTransport):
    """A :class:`~plugins.mail.services.transport.MailTransport` over IMAP (read) + SMTP (send)."""

    def __init__(self, spec: AccountSpec) -> None:
        self._spec: AccountSpec = spec
        self._client: aioimaplib.IMAP4_SSL | None = None
        self._selected: str | None = None
        # One command at a time: an IMAP connection is a single command stream, and the
        # sync loop, the routes and the agent's tools all share this one transport.
        self._lock: asyncio.Lock = asyncio.Lock()

    # --- seam ---------------------------------------------------------------

    @override
    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(idle=True, search=True, move=True, flags=True, threads=False)

    @override
    async def probe(self) -> None:
        async with self._lock:
            _ = await self._connect()

    @override
    async def list_folders(self) -> list[MailFolder]:
        async with self._lock:
            client = await self._connect()
            status, lines = await self._command(client.list('""', "*"))
            if status != "OK":
                raise MailError("the server refused to list this account's folders")
            return [folder for folder in map(parse_list_line, lines) if folder is not None]

    @override
    async def list_messages(
        self, folder: str, *, limit: int = 50, since_uid: str | None = None
    ) -> list[MailHeader]:
        async with self._lock:
            client = await self._connect()
            await self._select(client, folder)
            # `UNDELETED`, not `ALL`: a message flagged `\Deleted` is gone as far as every
            # mail client is concerned, and on a server without UIDPLUS that flag is all a
            # delete leaves behind (see `_expunge`) — listing it would resurrect it.
            criteria = f"UID {int(since_uid) + 1}:* UNDELETED" if since_uid else "UNDELETED"
            status, lines = await self._command(client.uid_search(criteria))
            if status != "OK":
                raise MailError(f"the server refused to search {folder!r}")
            uids = search_uids(lines)
            # A `since_uid` search that finds nothing still returns the highest uid on
            # some servers (the `:*` range always matches one message); drop it.
            if since_uid:
                uids = [uid for uid in uids if int(uid) > int(since_uid)]
            wanted = uids[-limit:][::-1]
            return [await self._fetch_header(client, uid) for uid in wanted]

    @override
    async def fetch(self, folder: str, uid: str) -> MailBody:
        async with self._lock:
            client = await self._connect()
            await self._select(client, folder)
            status, lines = await self._command(client.uid("fetch", uid, "(BODY.PEEK[])"))
            raw = first_literal(lines)
            if status != "OK" or raw is None:
                raise MailError("that message is no longer on the server")
            return parse_message(raw, uid=uid)

    @override
    async def send(self, message: OutgoingMail) -> str:
        sender = _self_address(self._spec)
        composed = build_outgoing(sender, message)
        config = self._spec.config
        try:
            _ = await aiosmtplib.send(
                composed,
                hostname=str(config.get("smtp_host") or config.get("imap_host") or ""),
                port=_int_setting(config.get("smtp_port"), 587),
                username=self._username(),
                password=self._spec.password,
                use_tls=bool(config.get("smtp_ssl", False)),
                start_tls=None if config.get("smtp_ssl") else True,
                timeout=_CONNECT_TIMEOUT_S,
            )
        except aiosmtplib.SMTPAuthenticationError as exc:
            raise MailAuthError("the mail server rejected this account's credentials") from exc
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise MailUnavailableError(f"the message could not be sent: {exc}") from exc
        return str(composed.get("Message-ID"))

    @override
    async def flag(
        self, folder: str, uid: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        changes = [(seen, "\\Seen"), (flagged, "\\Flagged")]
        async with self._lock:
            client = await self._connect()
            await self._select(client, folder)
            for wanted, flag in changes:
                if wanted is None:
                    continue
                verb = "+FLAGS" if wanted else "-FLAGS"
                _ = await self._command(client.uid("store", uid, verb, f"({flag})"))

    @override
    async def move(self, folder: str, uid: str, destination: str) -> None:
        async with self._lock:
            client = await self._connect()
            await self._select(client, folder)
            if await self._moved(client, uid, destination):
                return
            # Pre-RFC 6851 servers have no MOVE: copy, then flag the original deleted.
            _ = await self._command(client.uid("copy", uid, destination))
            _ = await self._command(client.uid("store", uid, "+FLAGS", "(\\Deleted)"))
            await self._expunge(client, uid)

    @override
    async def delete(self, folder: str, uid: str) -> None:
        async with self._lock:
            client = await self._connect()
            await self._select(client, folder)
            _ = await self._command(client.uid("store", uid, "+FLAGS", "(\\Deleted)"))
            await self._expunge(client, uid)

    @override
    async def close(self) -> None:
        client, self._client, self._selected = self._client, None, None
        if client is None:
            return
        with suppress(Exception):
            _ = await asyncio.wait_for(client.logout(), timeout=5.0)
        # LOGOUT is a courtesy to the server, not a release of anything on this side —
        # aioimaplib leaves the transport open either way, and a logout that timed out or
        # failed leaves it open with nothing coming.
        _drop_socket(client)

    # --- push (IMAP IDLE) ------------------------------------------------------

    @override
    async def watch(self, folder: str) -> AsyncIterator[None]:
        """Yield once per server-side change in ``folder`` (RFC 2177 IDLE), renewing the
        idle well inside the protocol's 29-minute ceiling. The caller stops by breaking
        out of the iteration; the connection is left open for ordinary commands."""
        while True:
            async with self._lock:
                client = await self._connect()
                await self._select(client, folder)
                idle = await client.idle_start(timeout=_IDLE_RENEW_S)
                try:
                    _ = await client.wait_server_push()
                finally:
                    client.idle_done()
                    with suppress(asyncio.TimeoutError):
                        _ = await asyncio.wait_for(idle, timeout=5.0)
            yield None

    # --- internals -------------------------------------------------------------

    def _username(self) -> str:
        return str(self._spec.config.get("username") or self._spec.address)

    async def _connect(self) -> aioimaplib.IMAP4_SSL:
        if self._client is not None:
            return self._client
        config = self._spec.config
        host = str(config.get("imap_host") or "")
        if not host:
            raise MailError("this account has no IMAP server configured")
        client = aioimaplib.IMAP4_SSL(
            host=host, port=_int_setting(config.get("imap_port"), 993), timeout=_CONNECT_TIMEOUT_S
        )
        try:
            await client.wait_hello_from_server()
            if self._spec.access_token:
                status, _lines = await self._command(
                    client.xoauth2(self._username(), self._spec.access_token)
                )
            else:
                status, _lines = await self._command(
                    client.login(self._username(), self._spec.password or "")
                )
            if status != "OK":
                raise MailAuthError("the mail server rejected this account's credentials")
        except (TimeoutError, OSError) as exc:
            # aioimaplib opens the socket from its constructor, so a connection that is
            # never returned is still holding one. Nobody else can release it — the handle
            # exists only in this frame — and the sync loop comes back every few minutes.
            _drop_socket(client)
            raise MailUnavailableError(f"could not reach the mail server: {exc}") from exc
        except ValueError as exc:
            # aioimaplib refuses a command argument holding CR, LF or NUL.
            _drop_socket(client)
            raise MailAuthError(
                "this account's username or credentials contain a line break or NUL byte"
            ) from exc
        except Exception:
            _drop_socket(client)  # a refused login, or a domain error out of `_command`
            raise
        self._client = client
        self._selected = None
        return client

    async def _select(self, client: aioimaplib.IMAP4_SSL, folder: str) -> None:
        if self._selected == folder:
            return
        status, _lines = await self._command(client.select(folder))
        if status != "OK":
            raise MailError(f"no such folder: {folder!r}")
        self._selected = folder

    async def _moved(self, client: aioimaplib.IMAP4_SSL, uid: str, destination: str) -> bool:
        """Whether the server took a UID MOVE (RFC 6851).

        The capability is checked before issuing because aioimaplib *raises* rather than
        answering when MOVE was never advertised — which would surface as a dropped
        connection and leave :meth:`move`'s copy fallback unreachable on exactly the
        servers it exists for. A server that advertises MOVE can still refuse this one,
        so a non-OK status falls back too.
        """
        if not client.has_capability("MOVE"):
            return False
        status, _lines = await self._command(client.uid("move", uid, destination))
        return status == "OK"

    async def _expunge(self, client: aioimaplib.IMAP4_SSL, uid: str) -> None:
        """Reap one ``\\Deleted``-flagged message, never the whole mailbox.

        A bare ``EXPUNGE`` is folder-wide: it permanently destroys *every* message in the
        selected mailbox that anything has flagged ``\\Deleted``, including deletions
        another client staged and has not yet committed. ``UID EXPUNGE`` (RFC 4315) is the
        scoped form and is used wherever the server advertises UIDPLUS.

        Without UIDPLUS there is no way to narrow it, so nothing is expunged and the
        message is simply left flagged: that is the ordinary, recoverable IMAP outcome,
        the server collapses it on the next client-issued expunge or close, and listings
        already hide it (they search ``UNDELETED``). Destroying a stranger's pending
        deletions to save a tombstone is not a trade worth making.
        """
        if not client.has_capability("UIDPLUS"):
            logger.debug("no UIDPLUS: leaving uid %s flagged \\Deleted rather than expunging", uid)
            return
        _ = await self._command(client.uid("expunge", uid))

    async def _command(self, awaitable: Awaitable[aioimaplib.Response]) -> tuple[str, list[object]]:
        """Await one IMAP command, turning connection-level failures into domain errors
        and dropping the cached connection so the next call reconnects."""
        try:
            response = await awaitable
        # `uid` and `fetch` time out through aioimaplib's own per-command timer, which raises
        # `CommandTimeout` rather than `TimeoutError`.
        except (TimeoutError, OSError, aioimaplib.Abort, aioimaplib.CommandTimeout) as exc:
            # Forgetting the handle is not releasing the connection: the client still owns
            # a socket, a TLS session and a protocol object, and servers drop idle IMAP
            # sessions routinely — so one leak per drop, growing for the life of the
            # process. No LOGOUT here the way `close` does one: this connection just
            # failed, it has nothing left to say, and the caller must not wait on a dead
            # socket to be told its command failed. Closing the transport is immediate.
            client, self._client, self._selected = self._client, None, None
            _drop_socket(client)
            raise MailUnavailableError(f"the mail server connection failed: {exc}") from exc
        return response.result, list(response.lines)

    async def _fetch_header(self, client: aioimaplib.IMAP4_SSL, uid: str) -> MailHeader:
        """One listing entry. ``BODY.PEEK[]`` (rather than a HEADER-only fetch) is used
        so the snippet is real body text; IMAP has no snippet of its own, and a second
        round trip per message to build one would cost more than the fetch."""
        status, lines = await self._command(
            client.uid("fetch", uid, "(FLAGS RFC822.SIZE BODY.PEEK[])")
        )
        raw = first_literal(lines)
        if status != "OK" or raw is None:
            raise MailError(f"message {uid} could not be read")
        flags = _flags_of(lines)
        return replace(
            parse_message(raw, uid=uid).header,
            seen="\\seen" in flags,
            flagged="\\flagged" in flags,
        )


def _drop_socket(client: aioimaplib.IMAP4_SSL | None) -> None:
    """Give the connection's socket back to the OS. Idempotent and best-effort — it is
    called on connections that are already broken, and a teardown that raises would mask
    the failure that prompted it."""
    if client is None:
        return
    with suppress(Exception):
        protocol = client.protocol
        if protocol is not None and protocol.transport is not None:
            protocol.transport.close()


def _self_address(spec: AccountSpec) -> MailAddress:
    display = str(spec.config.get("display_name") or "")
    return MailAddress(address=spec.address, name=display or None)


def _flags_of(lines: list[object]) -> set[str]:
    for line in lines:
        text = line.decode("utf-8", "replace") if isinstance(line, bytes | bytearray) else str(line)
        match = re.search(r"FLAGS \(([^)]*)\)", text)
        if match:
            return {flag.lower() for flag in match.group(1).split()}
    return set()


def first_literal(lines: list[object]) -> bytes | None:
    """The first literal in a FETCH response — the raw RFC 5322 message.

    aioimaplib returns a flat list mixing the untagged response line, the literal
    payload(s), and the closing paren. The message is the longest ``bytearray``/``bytes``
    chunk; picking by length rather than position is what keeps this robust across the
    server-to-server variation in how many chunks a FETCH is split into.
    """
    literals = [line for line in lines if isinstance(line, bytes | bytearray) and len(line) > 32]
    return bytes(max(literals, key=len)) if literals else None


def search_uids(lines: list[object]) -> list[str]:
    for line in lines:
        text = line.decode("utf-8", "replace") if isinstance(line, bytes | bytearray) else str(line)
        tokens = text.split()
        if tokens and all(token.isdigit() for token in tokens):
            return tokens
    return []


def _int_setting(value: object, default: int) -> int:
    """A port-like account setting, falling back to ``default`` when it is unset or empty."""
    return int(value) if value and isinstance(value, str | int | float) else default


def parse_list_line(line: object) -> MailFolder | None:
    text = line.decode("utf-8", "replace") if isinstance(line, bytes | bytearray) else str(line)
    match = _LIST_LINE.match(text.strip())
    if match is None:
        return None
    flags = {flag.lower() for flag in match.group("flags").split()}
    if "\\noselect" in flags:
        return None
    path = match.group("name").strip().strip('"')
    separator = match.group("sep") or "/"
    display = path.split(separator)[-1] if separator else path
    role = next((_SPECIAL_USE[flag] for flag in flags if flag in _SPECIAL_USE), None)
    role = role or _NAME_ROLES.get(display.lower(), ROLE_OTHER)
    return MailFolder(id=path, name=display, role=role)
