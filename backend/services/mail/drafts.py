"""Drafts and the writing-style profile — the two sealed stores behind `EMAIL-3`.

Both are small, both are pure persistence (no model calls, no network), and both are the
seal/open boundary for their own content — the same split :mod:`services.mail.cache` makes
for messages. Kept together because they are two halves of one feature: a profile
describes how the operator writes, and a draft is what that produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.vault import Vault
from models._fields import utcnow
from models.mail import MailDraft, MailStyleProfile

# A draft the operator composed, versus one pre-generated as a reply suggestion.
KIND_MANUAL = "manual"
KIND_SUGGESTED = "suggested"


@dataclass(frozen=True, slots=True)
class DraftView:
    id: str
    account_id: str
    in_reply_to_id: str | None
    kind: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    label: str | None
    sent_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StyleProfileView:
    """The learned profile, as the operator sees and edits it (`EMAIL-3`)."""

    profile: str | None
    sample_count: int
    edited: bool
    learned_at: datetime | None
    updated_at: datetime


class DraftStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine = engine
        self._vault = vault

    async def create(
        self,
        owner_id: str,
        account_id: str,
        *,
        in_reply_to_id: str | None = None,
        kind: str = KIND_MANUAL,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str = "",
        body: str = "",
        label: str | None = None,
    ) -> DraftView:
        draft = MailDraft(
            owner_id=owner_id,
            account_id=account_id,
            in_reply_to_id=in_reply_to_id,
            kind=kind,
            to_enc=self._seal_list(to),
            cc_enc=self._seal_list(cc),
            bcc_enc=self._seal_list(bcc),
            subject_enc=self._seal(subject),
            body_enc=self._seal(body),
            label_enc=self._seal(label),
        )

        def work(session: Session) -> None:
            session.add(draft)

        await in_session(self._engine, work)
        return self._to_view(draft)

    async def update(self, owner_id: str, draft_id: str, **fields) -> DraftView:
        """Edit a draft. Only the fields passed are touched; ``None`` clears one."""
        sealed = {
            "to_enc": self._seal_list(fields["to"]) if "to" in fields else None,
            "cc_enc": self._seal_list(fields["cc"]) if "cc" in fields else None,
            "bcc_enc": self._seal_list(fields["bcc"]) if "bcc" in fields else None,
            "subject_enc": self._seal(fields.get("subject")) if "subject" in fields else None,
            "body_enc": self._seal(fields.get("body")) if "body" in fields else None,
            "label_enc": self._seal(fields.get("label")) if "label" in fields else None,
        }
        touched = {
            key: value for key, value in sealed.items() if key.removesuffix("_enc") in fields
        }

        def work(session: Session) -> MailDraft | None:
            row = session.get(MailDraft, draft_id)
            if row is None or row.owner_id != owner_id:
                return None
            for key, value in touched.items():
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.add(row)
            return row

        row = await in_session(self._engine, work)
        if row is None:
            raise NotFoundError(f"no such draft: {draft_id}")
        return self._to_view(row)

    async def get(self, owner_id: str, draft_id: str) -> DraftView:
        def work(session: Session) -> MailDraft | None:
            row = session.get(MailDraft, draft_id)
            return row if row is not None and row.owner_id == owner_id else None

        row = await in_session(self._engine, work)
        if row is None:
            raise NotFoundError(f"no such draft: {draft_id}")
        return self._to_view(row)

    async def list_drafts(self, owner_id: str, *, account_id: str | None = None) -> list[DraftView]:
        def work(session: Session) -> list[MailDraft]:
            query = select(MailDraft).where(
                MailDraft.owner_id == owner_id,
                MailDraft.sent_at == None,  # noqa: E711 — SQL NULL test
            )
            if account_id is not None:
                query = query.where(MailDraft.account_id == account_id)
            return list(session.exec(query.order_by(MailDraft.updated_at.desc())).all())

        return [self._to_view(row) for row in await in_session(self._engine, work)]

    async def suggestions_for(self, owner_id: str, message_id: str) -> list[DraftView]:
        """The pre-generated reply suggestions already stored for one message."""

        def work(session: Session) -> list[MailDraft]:
            query = select(MailDraft).where(
                MailDraft.owner_id == owner_id,
                MailDraft.in_reply_to_id == message_id,
                MailDraft.kind == KIND_SUGGESTED,
            )
            return list(session.exec(query.order_by(MailDraft.created_at)).all())

        return [self._to_view(row) for row in await in_session(self._engine, work)]

    async def mark_sent(self, owner_id: str, draft_id: str) -> None:
        def work(session: Session) -> None:
            row = session.get(MailDraft, draft_id)
            if row is not None and row.owner_id == owner_id:
                row.sent_at = utcnow()
                session.add(row)

        await in_session(self._engine, work)

    async def delete(self, owner_id: str, draft_id: str) -> None:
        def work(session: Session) -> None:
            row = session.get(MailDraft, draft_id)
            if row is not None and row.owner_id == owner_id:
                session.delete(row)

        await in_session(self._engine, work)

    # --- internals -------------------------------------------------------------

    def _seal(self, value: str | None) -> str | None:
        return self._vault.encrypt_str(value) if value else None

    def _seal_list(self, values: list[str] | None) -> str | None:
        return self._seal(json.dumps(values)) if values else None

    def _open(self, token: str | None) -> str | None:
        return self._vault.decrypt_str(token) if token else None

    def _open_list(self, token: str | None) -> tuple[str, ...]:
        raw = self._open(token)
        return tuple(json.loads(raw)) if raw else ()

    def _to_view(self, row: MailDraft) -> DraftView:
        return DraftView(
            id=row.id,
            account_id=row.account_id,
            in_reply_to_id=row.in_reply_to_id,
            kind=row.kind,
            to=self._open_list(row.to_enc),
            cc=self._open_list(row.cc_enc),
            bcc=self._open_list(row.bcc_enc),
            subject=self._open(row.subject_enc) or "",
            body=self._open(row.body_enc) or "",
            label=self._open(row.label_enc),
            sent_at=row.sent_at,
            updated_at=row.updated_at,
        )


class StyleProfileStore:
    """The one-per-owner writing-style profile (`EMAIL-3`) — viewable and editable."""

    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine = engine
        self._vault = vault

    async def get(self, owner_id: str) -> StyleProfileView | None:
        def work(session: Session) -> MailStyleProfile | None:
            return session.exec(
                select(MailStyleProfile).where(MailStyleProfile.owner_id == owner_id)
            ).first()

        row = await in_session(self._engine, work)
        return self._to_view(row) if row is not None else None

    async def set(
        self, owner_id: str, profile: str | None, *, sample_count: int = 0, edited: bool
    ) -> StyleProfileView:
        """Upsert the profile. ``edited=True`` marks it the operator's own words, which a
        later learn pass must not overwrite; ``edited=False`` stamps ``learned_at``."""
        sealed = self._vault.encrypt_str(profile) if profile else None
        now = utcnow()

        def work(session: Session) -> MailStyleProfile:
            row = session.exec(
                select(MailStyleProfile).where(MailStyleProfile.owner_id == owner_id)
            ).first()
            if row is None:
                row = MailStyleProfile(owner_id=owner_id)
            row.profile_enc = sealed
            row.edited = edited
            row.updated_at = now
            if not edited:
                row.sample_count = sample_count
                row.learned_at = now
            session.add(row)
            return row

        return self._to_view(await in_session(self._engine, work))

    def _to_view(self, row: MailStyleProfile) -> StyleProfileView:
        return StyleProfileView(
            profile=self._vault.decrypt_str(row.profile_enc) if row.profile_enc else None,
            sample_count=row.sample_count,
            edited=row.edited,
            learned_at=row.learned_at,
            updated_at=row.updated_at,
        )
