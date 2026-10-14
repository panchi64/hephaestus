"""Mail schema (`EMAIL-*`) — accounts, the inbox cache, drafts, and the style profile.

Four tables, one per durable shape the mail capability needs:

- :class:`MailAccount` — one connected mailbox. Its **per-account secret** (an IMAP/SMTP
  password, or an OAuth ``{access_token, refresh_token, expires_at, scope}`` bundle) lives
  in a single sealed column here, sealed with the vault exactly like ``ModelEndpoint``'s
  ``api_key``. ``ServiceCredential`` is deliberately *not* reused: it is a static-catalog,
  one-key-per-service table and cannot express several accounts per provider, refresh
  tokens, or expiry. The OAuth *client* registration (the Google/Microsoft client id +
  secret, one per install) does belong there, and lives in its catalog.
- :class:`MailMessage` — the local inbox cache (`EMAIL-5`, `XC-PERF-4`), also the row the
  automatic triage (`EMAIL-2`) and the quoted/signature split (`EMAIL-4`) are stamped onto.
- :class:`MailDraft` — a composed or pre-generated reply (`EMAIL-3`), until it is sent.
- :class:`MailStyleProfile` — the writing-style profile learned from Sent mail (`EMAIL-3`),
  which the operator can view and edit.

**What is sealed vs clear.** Everything that is operator content — addresses, subjects,
bodies, snippets, AI summaries — is application-layer encrypted. What stays in the clear is
structural: the provider kind, host/port transport config (never a secret), the opaque
provider-side message uid, timestamps, and the triage *verdicts* (urgency, spam, category
tags) so the inbox can be filtered and ordered in the DB without decrypting every row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table

# Provider kinds a `MailAccount` can speak — which transport the service instantiates.
PROVIDER_IMAP = "imap"
PROVIDER_JMAP = "jmap"
PROVIDER_GMAIL = "gmail"
PROVIDER_GRAPH = "graph"

# How the account authenticates: a stored password, or an OAuth token bundle that is
# refreshed and re-sealed in place.
AUTH_PASSWORD = "password"
AUTH_OAUTH = "oauth"


class MailAccount(Table, table=True):
    __tablename__ = "mail_accounts"
    # An operator's account labels are unique so routes/tools can refer to one stably
    # and a re-add can't silently duplicate a mailbox.
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_mail_account_owner_name"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The operator's label for the mailbox ("Personal", "Work"). Clear: it is chosen
    # metadata, uniqueness is enforced on it, and it never carries message content.
    name: str
    # The mailbox's own address — operator content, sealed.
    address_enc: str
    provider: str = PROVIDER_IMAP
    auth_kind: str = AUTH_PASSWORD
    # Transport configuration: hosts, ports, TLS flags, the JMAP session url, the OAuth
    # account identifier. Structural, never a secret — secrets live in `secret_enc`.
    config: dict[str, object] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    # AEAD ciphertext of this account's secret bundle, as JSON: ``{"password": …}`` for
    # `AUTH_PASSWORD`, ``{"access_token", "refresh_token", "expires_at", "scope"}`` for
    # `AUTH_OAUTH`. Re-sealed on every OAuth refresh — a rotated refresh token never
    # lands in the clear.
    secret_enc: str | None = None
    # Disable-without-delete: a benched account keeps its config and cached mail but is
    # skipped by the sync loop and by the agent's tools.
    enabled: bool = True
    # Last sync/connection outcome — operator-facing health (`XC-DEG-3`), all cleartext
    # structural metadata (never a secret, never message content). ``last_status`` is
    # ``"untested"`` until the first probe.
    last_status: str | None = None  # "ok" | "error" | "untested"
    last_error_detail: str | None = None
    last_synced_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MailMessage(Table, table=True):
    """One cached message. The cache is what the inbox listing reads (`EMAIL-5`) and what
    triage (`EMAIL-2`) stamps its verdicts onto; the remote mailbox stays authoritative and
    a re-sync reconciles by ``(account_id, folder, uid)``."""

    __tablename__ = "mail_messages"
    __table_args__ = (
        UniqueConstraint("account_id", "folder", "uid", name="uq_mail_message_account_uid"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    account_id: str = Field(foreign_key="mail_accounts.id", index=True, ondelete="CASCADE")
    # The provider-side folder/mailbox name ("INBOX", "[Gmail]/Sent") and the opaque
    # per-folder message identifier. Both clear — they are the reconciliation key, carry
    # no content, and must be queryable to dedupe a re-sync.
    folder: str = Field(index=True)
    uid: str
    # Provider-side conversation grouping, when it exposes one (Gmail/JMAP thread ids).
    thread_id: str | None = None
    # RFC 5322 Message-ID / In-Reply-To — identifiers rather than content, and needed to
    # thread a reply correctly.
    message_id: str | None = None
    in_reply_to: str | None = None

    # --- sealed content -------------------------------------------------------
    from_address_enc: str
    from_name_enc: str | None = None
    to_enc: str | None = None  # JSON list of addresses
    cc_enc: str | None = None  # JSON list of addresses
    subject_enc: str | None = None
    snippet_enc: str | None = None
    # The full plain-text body, then the `EMAIL-4` split of it: the sender's own new
    # prose, the quoted history below it, and the trailing signature block.
    body_enc: str | None = None
    reply_text_enc: str | None = None
    quoted_text_enc: str | None = None
    signature_enc: str | None = None
    # AI-written one-line summary (`EMAIL-2`) and the calendar events the message implies
    # (`EMAIL-4`, JSON list) — both derived from operator content, so both sealed.
    summary_enc: str | None = None
    implied_events_enc: str | None = None

    # --- clear structure + triage verdicts -------------------------------------
    received_at: datetime = Field(index=True)
    seen: bool = False
    flagged: bool = False
    has_attachments: bool = False
    size_bytes: int | None = None
    # Triage verdicts (`EMAIL-2`). Labels and scores, not content — kept clear so the
    # inbox can filter/sort on them in the DB. ``triaged_at`` is null until triage runs,
    # which is how the sync loop finds the backlog.
    urgency: str = "normal"  # "low" | "normal" | "high"
    spam: bool = False
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    triaged_at: datetime | None = None
    # When this row was last refreshed from the provider — the freshness window the
    # cached listing is served within (`XC-PERF-4`).
    cached_at: datetime = Field(default_factory=utcnow)


class MailDraft(Table, table=True):
    """A composed message that has not been sent — either the operator's own draft or a
    pre-generated reply suggestion (`EMAIL-3`)."""

    __tablename__ = "mail_drafts"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    account_id: str = Field(foreign_key="mail_accounts.id", index=True, ondelete="CASCADE")
    # The cached message this drafts a reply to, when it is a reply. Nullable (a fresh
    # compose) and cascade-deleted with the message it answers.
    in_reply_to_id: str | None = Field(
        default=None, foreign_key="mail_messages.id", index=True, ondelete="CASCADE"
    )
    # "manual" (the operator wrote it) | "suggested" (pre-generated, `EMAIL-3`).
    kind: str = "manual"
    # Sealed content: recipients, subject, body, and the suggestion's short label.
    to_enc: str | None = None  # JSON list
    cc_enc: str | None = None  # JSON list
    bcc_enc: str | None = None  # JSON list
    subject_enc: str | None = None
    body_enc: str | None = None
    label_enc: str | None = None
    sent_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MailStyleProfile(Table, table=True):
    """The operator's writing-style profile, learned from their Sent mail and editable by
    hand (`EMAIL-3`). One per owner — the style is the person's, not the mailbox's."""

    __tablename__ = "mail_style_profiles"
    __table_args__ = (UniqueConstraint("owner_id", name="uq_mail_style_owner"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The profile itself — prose describing tone, greeting/sign-off habits, length. Sealed:
    # it is derived from the operator's own writing.
    profile_enc: str | None = None
    # How many sent messages the current profile was learned from, and whether the
    # operator has hand-edited it since (an edited profile is never silently overwritten
    # by a later learn pass).
    sample_count: int = 0
    edited: bool = False
    learned_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)
