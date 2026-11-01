"""``MailTransport`` — the pluggable seam every mail provider implements.

The same shape as the two seams already in `services/`: an explicit ``Protocol`` (like
:class:`~services.upload_extraction.UploadExtractor`) that the layers above program
against, with one adapter per backend. IMAP+SMTP,
JMAP, Gmail and Microsoft Graph are then interchangeable, and a test drives the whole
service against a fake that implements nothing but this protocol.

Two rules make the seam worth having:

1. **Domain models only.** Every method returns the dataclasses in
   :mod:`plugins.mail.services.models` — never an ``aioimaplib`` response, a JMAP blob, or a REST
   dict. Nothing above this file knows which provider it is talking to.
2. **Domain errors only.** Failures surface as :mod:`plugins.mail.services.errors` types, so the
   route layer maps them to HTTP and the tool layer decides retry-vs-degrade.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .models import MailBody, MailFolder, MailHeader, OutgoingMail, TransportCapabilities


@runtime_checkable
class MailTransport(Protocol):
    """One connected mailbox, as the rest of the system sees it."""

    def capabilities(self) -> TransportCapabilities:
        """What this provider supports. Synchronous and cheap — it is static per
        adapter, so callers can branch on it without a round trip."""
        ...

    async def probe(self) -> None:
        """Verify the account can actually connect and authenticate. Returns ``None`` on
        success; raises :class:`~plugins.mail.services.errors.MailAuthError` for rejected
        credentials or :class:`~plugins.mail.services.errors.MailUnavailableError` for an
        unreachable server. Drives the operator-facing account health (`XC-DEG-3`)."""
        ...

    async def list_folders(self) -> list[MailFolder]:
        """Every mailbox on the account, each stamped with a normalized role."""
        ...

    async def list_messages(
        self, folder: str, *, limit: int = 50, since_uid: str | None = None
    ) -> list[MailHeader]:
        """Newest-first headers from ``folder``. ``since_uid`` requests only what arrived
        after a previously-seen uid — the incremental path the sync loop uses so a
        re-sync costs one small request rather than a full listing."""
        ...

    async def fetch(self, folder: str, uid: str) -> MailBody:
        """One message with its body, already reduced to plain text by the parse layer."""
        ...

    async def send(self, message: OutgoingMail) -> str:
        """Send ``message``; returns the RFC 5322 Message-ID it was sent with, or an
        empty string where the provider mints the id server-side and doesn't disclose it
        (Microsoft Graph). Callers must not treat the id as guaranteed."""
        ...

    async def flag(
        self, folder: str, uid: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        """Set the read/flagged state remotely. ``None`` leaves that flag untouched."""
        ...

    async def move(self, folder: str, uid: str, destination: str) -> None:
        """Move a message between folders."""
        ...

    async def delete(self, folder: str, uid: str) -> None:
        """Delete a message (the provider's own semantics — trash or expunge)."""
        ...

    async def close(self) -> None:
        """Release any connection held. Idempotent."""
        ...


@runtime_checkable
class WatchableTransport(Protocol):
    """The optional push half, implemented only where the protocol has one (IMAP IDLE).

    Kept off :class:`MailTransport` deliberately: a transport that cannot push should not
    have to stub a method, and the sync loop already reads
    :attr:`~plugins.mail.services.models.TransportCapabilities.idle` to choose push-vs-poll.
    """

    def watch(self, folder: str) -> AsyncIterator[None]:
        """Yield once per server-side change notification for ``folder``, until the
        caller stops iterating."""
        ...
