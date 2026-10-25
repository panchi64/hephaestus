"""The mail capability's domain shapes — what every transport speaks.

These dataclasses are the *only* currency crossing the
:class:`~plugins.mail.services.transport.MailTransport` seam. No protocol type ever escapes an
adapter: an IMAP ``FETCH`` response, a JMAP ``Email/get`` blob and a Gmail REST resource
all land here, so everything above the seam (the sync loop, triage, the routes, the
agent's tools) is written once against one shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Well-known folder roles, normalized across providers: IMAP special-use flags
# (`\Sent`, `\Junk`, …), JMAP mailbox roles and Gmail label ids all map onto these,
# so "the sent folder" is answerable without provider-specific naming.
ROLE_INBOX = "inbox"
ROLE_SENT = "sent"
ROLE_DRAFTS = "drafts"
ROLE_TRASH = "trash"
ROLE_ARCHIVE = "archive"
ROLE_SPAM = "spam"
ROLE_OTHER = "other"


@dataclass(frozen=True, slots=True)
class MailAddress:
    """One participant. ``name`` is the display name when the provider gave one."""

    address: str
    name: str | None = None

    def format(self) -> str:
        """RFC 5322 form for a header (``Ada Lovelace <ada@example.org>``)."""
        return f"{self.name} <{self.address}>" if self.name else self.address


@dataclass(frozen=True, slots=True)
class MailFolder:
    """A mailbox/folder as the provider names it, plus its normalized ``role``."""

    id: str  # the provider-side identifier the other calls take (IMAP: the mailbox path)
    name: str  # display name, last path segment for IMAP
    role: str = ROLE_OTHER
    total: int | None = None
    unread: int | None = None


@dataclass(frozen=True, slots=True)
class MailHeader:
    """A listing entry — everything the inbox view needs without fetching a body."""

    uid: str
    sender: MailAddress
    subject: str = ""
    received_at: datetime | None = None
    to: tuple[MailAddress, ...] = ()
    cc: tuple[MailAddress, ...] = ()
    snippet: str = ""
    thread_id: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    seen: bool = False
    flagged: bool = False
    has_attachments: bool = False
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MailBody:
    """A fetched message: its header plus the body, already reduced to plain text.

    ``text`` is the canonical body every layer above reads — an HTML-only message has
    been converted by the parse layer, so no consumer deals with markup. ``html`` is
    kept only so a rich reader can render the original.
    """

    header: MailHeader
    text: str = ""
    html: str | None = None
    attachments: tuple[str, ...] = ()  # filenames only; bytes are never cached


@dataclass(frozen=True, slots=True)
class OutgoingMail:
    """A message to send. ``in_reply_to``/``references`` carry RFC 5322 Message-IDs so a
    reply threads correctly in the recipient's client."""

    to: tuple[MailAddress, ...]
    subject: str
    body: str
    cc: tuple[MailAddress, ...] = ()
    bcc: tuple[MailAddress, ...] = ()
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransportCapabilities:
    """What this provider's transport can actually do, so callers degrade rather than
    guess. A transport that can't ``move`` (some REST APIs only label) reports it here
    and the service surfaces "not supported by this account" instead of failing oddly."""

    idle: bool = False  # push/IDLE support — the sync loop polls when absent
    search: bool = False  # server-side search
    move: bool = False
    flags: bool = False  # seen/flagged writes
    threads: bool = False  # provider-side thread grouping


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """Everything a transport needs to connect, assembled by the service from a
    ``MailAccount`` row: its clear transport ``config`` plus the freshly-opened secret.

    The secret is passed as a value, never read from the DB by the adapter — the vault
    stays above this layer, and an adapter can be exercised in a test with a literal.
    """

    account_id: str
    address: str
    provider: str
    auth_kind: str
    config: dict[str, object] = field(default_factory=dict[str, object])
    password: str | None = None
    access_token: str | None = None
