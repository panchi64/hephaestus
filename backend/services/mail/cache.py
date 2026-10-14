"""The local inbox cache (`EMAIL-5`, `XC-PERF-4`) — sealed mail, in the clear enough to list.

Opening an IMAP session and fetching a folder listing takes seconds; the operator opens
their inbox constantly. So every header the sync sees is written to ``mail_messages`` and
the listing is answered from there, with the provider consulted only when the cache is
older than a short freshness window. The remote mailbox stays authoritative — the cache
is reconciled by ``(account, folder, uid)``, never treated as the source of truth.

This module owns the **seal/open boundary** for mail content: rows go in encrypted and
come out as :class:`MessageView` / :class:`MessageDetail`. Callers above never see a
``_enc`` column, and nothing below here touches the vault.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from pydantic import TypeAdapter
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.vault import Vault
from models._fields import utcnow
from models.mail import MailMessage

from .models import MailAddress, MailBody, MailHeader
from .quoting import split_body

_ADDRESSES = TypeAdapter(list[str])
_EVENTS = TypeAdapter(list[dict[str, object]])

logger = logging.getLogger(__name__)


def _uid_order(uid: str) -> tuple[int, str]:
    """Sort key for a provider uid: numeric uids compare as numbers (IMAP), everything
    else lexically. Zero-padding keeps both in one comparable tuple."""
    return (0, uid.rjust(20, "0")) if uid.isdigit() else (1, uid)


@dataclass(frozen=True, slots=True)
class MessageView:
    """A cached message as the inbox list shows it — no full body."""

    id: str
    account_id: str
    folder: str
    uid: str
    from_address: str
    from_name: str | None
    to: tuple[str, ...]
    subject: str
    snippet: str
    received_at: datetime
    seen: bool
    flagged: bool
    has_attachments: bool
    urgency: str
    tags: tuple[str, ...]
    spam: bool
    summary: str | None
    thread_id: str | None = None
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class MessageDetail:
    """A cached message opened for reading — adds the body and its `EMAIL-4` split."""

    message: MessageView
    body: str
    reply_text: str
    quoted_text: str | None
    signature: str | None
    cc: tuple[str, ...] = ()
    implied_events: tuple[dict[str, object], ...] = ()


class MailCache:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault

    # --- writes ---------------------------------------------------------------

    async def upsert_headers(
        self, owner_id: str, account_id: str, folder: str, headers: list[MailHeader]
    ) -> list[str]:
        """Reconcile ``headers`` into the cache. Returns the ids of rows that are **new**
        — the backlog triage works through, so a re-sync of already-seen mail doesn't
        re-spend model calls on it."""
        if not headers:
            return []
        now = utcnow()

        def work(session: Session) -> list[str]:
            existing = {
                row.uid: row
                for row in session.exec(
                    select(MailMessage).where(
                        MailMessage.account_id == account_id, MailMessage.folder == folder
                    )
                ).all()
            }
            fresh: list[str] = []
            for header in headers:
                row = existing.get(header.uid)
                if row is None:
                    row = self._new_row(owner_id, account_id, folder, header, now)
                    fresh.append(row.id)
                else:
                    # Flags change remotely; content doesn't. Only restamp what can move.
                    row.seen = header.seen
                    row.flagged = header.flagged
                    row.cached_at = now
                session.add(row)
            return fresh

        return await in_session(self._engine, work)

    async def store_body(self, message_id: str, body: MailBody) -> None:
        """Seal a fetched body onto its cached row, with the `EMAIL-4` split alongside so
        the separation is computed once at ingest rather than per render."""
        parts = split_body(body.text)

        def work(session: Session) -> None:
            row = session.get(MailMessage, message_id)
            if row is None:
                return
            row.body_enc = self._seal(body.text)
            row.reply_text_enc = self._seal(parts.reply)
            row.quoted_text_enc = self._seal(parts.quoted)
            row.signature_enc = self._seal(parts.signature)
            row.has_attachments = bool(body.attachments) or row.has_attachments
            row.cached_at = utcnow()
            session.add(row)

        await in_session(self._engine, work)

    async def apply_triage(
        self,
        message_id: str,
        *,
        summary: str | None,
        urgency: str,
        tags: list[str],
        spam: bool,
        implied_events: list[dict[str, object]] | None = None,
    ) -> None:
        """Stamp one message's triage verdicts (`EMAIL-2`) and implied events (`EMAIL-4`)."""

        def work(session: Session) -> None:
            row = session.get(MailMessage, message_id)
            if row is None:
                return
            row.summary_enc = self._seal(summary)
            row.urgency = urgency
            row.tags = list(tags)
            row.spam = spam
            row.implied_events_enc = self._seal(
                json.dumps(implied_events) if implied_events else None
            )
            row.triaged_at = utcnow()
            session.add(row)

        await in_session(self._engine, work)

    async def set_flags(
        self, message_id: str, *, seen: bool | None = None, flagged: bool | None = None
    ) -> None:
        def work(session: Session) -> None:
            row = session.get(MailMessage, message_id)
            if row is None:
                return
            if seen is not None:
                row.seen = seen
            if flagged is not None:
                row.flagged = flagged
            session.add(row)

        await in_session(self._engine, work)

    async def forget(self, message_id: str) -> None:
        def work(session: Session) -> None:
            row = session.get(MailMessage, message_id)
            if row is not None:
                session.delete(row)

        await in_session(self._engine, work)

    # --- reads ----------------------------------------------------------------

    async def list_messages(
        self,
        owner_id: str,
        *,
        account_id: str | None = None,
        folder: str | None = None,
        limit: int = 50,
        unread_only: bool = False,
        include_spam: bool = False,
    ) -> list[MessageView]:
        """Newest-first page of cached messages."""

        def work(session: Session) -> list[MailMessage]:
            query = select(MailMessage).where(MailMessage.owner_id == owner_id)
            if account_id is not None:
                query = query.where(MailMessage.account_id == account_id)
            if folder is not None:
                query = query.where(MailMessage.folder == folder)
            if unread_only:
                query = query.where(MailMessage.seen == False)  # noqa: E712 — SQL, not Python
            if not include_spam:
                query = query.where(MailMessage.spam == False)  # noqa: E712
            query = query.order_by(col(MailMessage.received_at).desc()).limit(limit)
            return list(session.exec(query).all())

        return [self._to_view(row) for row in await in_session(self._engine, work)]

    async def get(self, owner_id: str, message_id: str) -> MessageDetail:
        row = await self._row(owner_id, message_id)
        return self._to_detail(row)

    async def row_for(self, owner_id: str, message_id: str) -> MailMessage:
        """The raw row — for callers that need the provider coordinates (account, folder,
        uid) to act on the remote mailbox."""
        return await self._row(owner_id, message_id)

    async def newest_uid(self, account_id: str, folder: str) -> str | None:
        """The newest uid cached for a folder — the incremental sync's cursor.

        Picked in Python rather than by SQL ordering because uid ordering is
        provider-shaped: IMAP uids are ascending integers (so ``"10"`` is newer than
        ``"9"``, which a lexical sort gets backwards), while a REST provider's ids are
        opaque strings with no order at all — for those the cursor is only ever used as
        an equality boundary ("stop when you reach this one"), which
        :meth:`~services.mail.transport.MailTransport.list_messages` honours.
        """

        def work(session: Session) -> list[str]:
            return [
                row.uid
                for row in session.exec(
                    select(MailMessage).where(
                        MailMessage.account_id == account_id, MailMessage.folder == folder
                    )
                ).all()
            ]

        uids = await in_session(self._engine, work)
        return max(uids, key=_uid_order) if uids else None

    async def untriaged(self, owner_id: str, *, limit: int = 20) -> list[MessageDetail]:
        """Messages that have never been triaged (`EMAIL-2`), oldest first so a backlog
        drains in arrival order."""

        def work(session: Session) -> list[MailMessage]:
            query = (
                select(MailMessage)
                .where(MailMessage.owner_id == owner_id, MailMessage.triaged_at == None)  # noqa: E711
                .order_by(col(MailMessage.received_at))
                .limit(limit)
            )
            return list(session.exec(query).all())

        return [self._to_detail(row) for row in await in_session(self._engine, work)]

    # --- internals -------------------------------------------------------------

    async def _row(self, owner_id: str, message_id: str) -> MailMessage:
        def work(session: Session) -> MailMessage | None:
            row = session.get(MailMessage, message_id)
            return row if row is not None and row.owner_id == owner_id else None

        row = await in_session(self._engine, work)
        if row is None:
            raise NotFoundError(f"no such message: {message_id}")
        return row

    def _new_row(
        self, owner_id: str, account_id: str, folder: str, header: MailHeader, now: datetime
    ) -> MailMessage:
        return MailMessage(
            owner_id=owner_id,
            account_id=account_id,
            folder=folder,
            uid=header.uid,
            thread_id=header.thread_id,
            message_id=header.message_id,
            in_reply_to=header.in_reply_to,
            from_address_enc=self._vault.encrypt_str(header.sender.address),
            from_name_enc=self._seal(header.sender.name),
            to_enc=self._seal_list(header.to),
            cc_enc=self._seal_list(header.cc),
            subject_enc=self._seal(header.subject),
            snippet_enc=self._seal(header.snippet),
            received_at=header.received_at or now,
            seen=header.seen,
            flagged=header.flagged,
            has_attachments=header.has_attachments,
            size_bytes=header.size_bytes,
            cached_at=now,
        )

    def _seal(self, value: str | None) -> str | None:
        return self._vault.encrypt_str(value) if value else None

    def _seal_list(self, addresses: tuple[MailAddress, ...]) -> str | None:
        return self._seal(json.dumps([a.address for a in addresses])) if addresses else None

    def _open(self, token: str | None) -> str | None:
        return self._vault.decrypt_str(token) if token else None

    def _open_list(self, token: str | None) -> tuple[str, ...]:
        raw = self._open(token)
        return tuple(_ADDRESSES.validate_json(raw)) if raw else ()

    def _to_view(self, row: MailMessage) -> MessageView:
        return MessageView(
            id=row.id,
            account_id=row.account_id,
            folder=row.folder,
            uid=row.uid,
            from_address=self._vault.decrypt_str(row.from_address_enc),
            from_name=self._open(row.from_name_enc),
            to=self._open_list(row.to_enc),
            subject=self._open(row.subject_enc) or "",
            snippet=self._open(row.snippet_enc) or "",
            received_at=row.received_at,
            seen=row.seen,
            flagged=row.flagged,
            has_attachments=row.has_attachments,
            urgency=row.urgency,
            tags=tuple(row.tags or ()),
            spam=row.spam,
            summary=self._open(row.summary_enc),
            thread_id=row.thread_id,
            message_id=row.message_id,
        )

    def _to_detail(self, row: MailMessage) -> MessageDetail:
        body = self._open(row.body_enc) or ""
        events = self._open(row.implied_events_enc)
        return MessageDetail(
            message=self._to_view(row),
            body=body,
            reply_text=self._open(row.reply_text_enc) or body,
            quoted_text=self._open(row.quoted_text_enc),
            signature=self._open(row.signature_enc),
            cc=self._open_list(row.cc_enc),
            implied_events=tuple(_EVENTS.validate_json(events)) if events else (),
        )
