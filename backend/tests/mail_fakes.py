"""A fake ``MailTransport`` — the seam's own conformance target.

Every mail test above the adapters runs against this instead of a live mailbox, which is
the point of the transport being a Protocol: the sync loop, the cache, triage and the
agent's tools are exercised end to end with no network, and anything they rely on is by
construction part of the published seam rather than an IMAP detail that leaked upward.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

from plugins.mail.services.errors import MailError, MailUnsupportedError
from plugins.mail.services.models import (
    ROLE_INBOX,
    ROLE_SENT,
    MailAddress,
    MailBody,
    MailFolder,
    MailHeader,
    OutgoingMail,
    TransportCapabilities,
)
from plugins.mail.services.service import LiveTransport, MailService
from plugins.mail.services.transport import MailTransport


def sample_header(uid: str = "1", **overrides: object) -> MailHeader:
    base = MailHeader(
        uid=uid,
        sender=MailAddress(address="ada@example.org", name="Ada Lovelace"),
        subject=f"Message {uid}",
        received_at=datetime(2026, 8, 13, 9, 0, tzinfo=UTC),
        to=(MailAddress(address="operator@example.com"),),
        snippet=f"body of {uid}",
        message_id=f"<{uid}@example.org>",
    )
    return replace(base, **overrides)


async def install_transport(
    service: MailService, owner_id: str, account_id: str, transport: object
) -> None:
    """Wire ``transport`` into the service's per-account cache in place of a real one.

    The cache holds an adapter *and* the credentials it was built with as one unit, so
    that a rotated token can never be served by a transport still holding the old one.
    Resolving the credentials here the same way the service does keeps a fake honest
    against that rule — an installed transport is cached on the same terms as a built one,
    and a test never has to restate the account's secret.
    """
    account = await service.row(owner_id, account_id)
    credentials = await service.secrets.open_access(account)
    service.transports[account_id] = LiveTransport(cast(MailTransport, transport), credentials)


class FakeTransport:
    """An in-memory mailbox implementing :class:`plugins.mail.services.transport.MailTransport`."""

    def __init__(self, *, messages: dict[str, list[MailBody]] | None = None) -> None:
        self.messages: dict[str, list[MailBody]] = messages or {
            "INBOX": [
                MailBody(header=sample_header("1"), text="body of 1"),
                MailBody(header=sample_header("2"), text="body of 2"),
            ],
            "Sent": [],
        }
        self.sent: list[OutgoingMail] = []
        self.flagged: list[tuple[str, str, bool | None, bool | None]] = []
        self.moved: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.closed: bool = False
        self.probe_error: Exception | None = None
        self.supports_move: bool = True

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            idle=False, search=False, move=self.supports_move, flags=True, threads=False
        )

    async def probe(self) -> None:
        if self.probe_error is not None:
            raise self.probe_error

    async def list_folders(self) -> list[MailFolder]:
        roles = {"INBOX": ROLE_INBOX, "Sent": ROLE_SENT}
        return [
            MailFolder(id=name, name=name, role=roles.get(name, "other"), total=len(bodies))
            for name, bodies in self.messages.items()
        ]

    async def list_messages(
        self, folder: str, *, limit: int = 50, since_uid: str | None = None
    ) -> list[MailHeader]:
        bodies = self.messages.get(folder)
        if bodies is None:
            raise MailError(f"no such folder: {folder!r}")
        headers = [body.header for body in bodies]
        if since_uid is not None:
            headers = [h for h in headers if int(h.uid) > int(since_uid)]
        return headers[-limit:][::-1]

    async def fetch(self, folder: str, uid: str) -> MailBody:
        for body in self.messages.get(folder, []):
            if body.header.uid == uid:
                return body
        raise MailError("that message is no longer on the server")

    async def send(self, message: OutgoingMail) -> str:
        self.sent.append(message)
        return f"<sent-{len(self.sent)}@example.com>"

    async def flag(
        self, folder: str, uid: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        self.flagged.append((folder, uid, seen, flagged))

    async def move(self, folder: str, uid: str, destination: str) -> None:
        if not self.supports_move:
            raise MailUnsupportedError("this account cannot move messages")
        self.moved.append((folder, uid, destination))

    async def delete(self, folder: str, uid: str) -> None:
        self.deleted.append((folder, uid))

    async def close(self) -> None:
        self.closed = True
