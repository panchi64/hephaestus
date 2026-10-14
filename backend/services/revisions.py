"""Version history for what the operator publishes, shared by every kind that has it.

A kind takes part by being :class:`Versioned`: it can render one item as files
(:meth:`~Versioned.snapshot`) and rewrite an item from such files
(:meth:`~Versioned.restore_snapshot`). Its mutating methods carry :func:`records`, which
saves a revision whenever the published item changes, so history holds every version
the model could have acted under. Drafts are working copies and are not recorded.
"""

from __future__ import annotations

import base64
import contextvars
import difflib
import functools
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Concatenate, Literal, Protocol

from pydantic import TypeAdapter
from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.vault import Vault
from models.revision import Revision

type Bundle = dict[str, bytes]

#: Older revisions beyond this many are dropped as new ones are recorded.
KEEP = 50
_FILES = TypeAdapter(dict[str, str])

_restoring: contextvars.ContextVar[bool] = contextvars.ContextVar("restoring", default=False)


@dataclass(frozen=True)
class RevisionInfo:
    number: int
    note: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class FileDiff:
    path: str
    status: Literal["added", "removed", "changed"]
    #: A unified diff, or None for a file that is not UTF-8 text.
    patch: str | None


class Published(Protocol):
    @property
    def id(self) -> str | None: ...
    @property
    def published(self) -> bool: ...


class Versioned[T: Published](Protocol):
    revision_kind: str
    revisions: RevisionStore | None

    async def snapshot(self, owner_id: str, subject_id: str, /) -> Bundle:
        """The item as published, or as it is now when the item has no separate draft."""
        ...

    async def working_copy(self, owner_id: str, subject_id: str, /) -> Bundle:
        """What publishing would make live."""
        ...

    async def restore_snapshot(self, owner_id: str, subject_id: str, bundle: Bundle, /) -> T: ...


class RevisionStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault

    async def record(
        self, owner_id: str, kind: str, subject_id: str, bundle: Bundle, note: str
    ) -> RevisionInfo | None:
        """Save ``bundle`` as the next revision, unless it matches the latest one."""
        raw = encode_bundle(bundle)
        digest = hashlib.sha256(raw).hexdigest()

        def work(session: Session) -> RevisionInfo | None:
            latest = _latest(session, owner_id, kind, subject_id)
            if latest is not None and latest.digest == digest:
                return None
            row = Revision(
                owner_id=owner_id,
                kind=kind,
                subject_id=subject_id,
                number=1 if latest is None else latest.number + 1,
                note=note,
                digest=digest,
                size_bytes=len(raw),
                bundle_enc=self._vault.encrypt_bytes(raw),
            )
            session.add(row)
            session.flush()
            _ = session.exec(
                delete(Revision)
                .where(col(Revision.owner_id) == owner_id)
                .where(col(Revision.kind) == kind)
                .where(col(Revision.subject_id) == subject_id)
                .where(col(Revision.number) <= row.number - KEEP)
            )
            return _info(row)

        return await in_session(self._engine, work)

    async def history(self, owner_id: str, kind: str, subject_id: str) -> list[RevisionInfo]:
        """Newest first."""

        def work(session: Session) -> list[RevisionInfo]:
            rows = session.exec(
                _subject(owner_id, kind, subject_id).order_by(col(Revision.number).desc())
            ).all()
            return [_info(row) for row in rows]

        return await in_session(self._engine, work)

    async def load(self, owner_id: str, kind: str, subject_id: str, number: int) -> Bundle:
        def work(session: Session) -> Bundle:
            row = session.exec(
                _subject(owner_id, kind, subject_id).where(col(Revision.number) == number)
            ).first()
            if row is None:
                raise NotFoundError(f"there is no version {number}")
            return decode_bundle(self._vault.decrypt_bytes(row.bundle_enc))

        return await in_session(self._engine, work)

    async def latest(self, owner_id: str, kind: str, subject_id: str) -> Bundle | None:
        def work(session: Session) -> Bundle | None:
            row = _latest(session, owner_id, kind, subject_id)
            return None if row is None else decode_bundle(self._vault.decrypt_bytes(row.bundle_enc))

        return await in_session(self._engine, work)

    async def forget(self, owner_id: str, kind: str, subject_id: str) -> None:
        def work(session: Session) -> None:
            _ = session.exec(
                delete(Revision)
                .where(col(Revision.owner_id) == owner_id)
                .where(col(Revision.kind) == kind)
                .where(col(Revision.subject_id) == subject_id)
            )

        await in_session(self._engine, work)


async def checkpoint[T: Published](store: Versioned[T], owner_id: str, item: T, note: str) -> None:
    """Record ``item`` if it is published. A no-op while a restore is rewriting it: the
    restore records the result once, under its own note."""
    if store.revisions is None or item.id is None or not item.published or _restoring.get():
        return
    bundle = await store.snapshot(owner_id, item.id)
    _ = await store.revisions.record(owner_id, store.revision_kind, item.id, bundle, note)


def records[S: Versioned[Published], **P, T: Published](
    note: str,
) -> Callable[
    [Callable[Concatenate[S, str, P], Awaitable[T]]],
    Callable[Concatenate[S, str, P], Awaitable[T]],
]:
    """Mark a store method ``(self, owner_id, ...) -> item`` as one that can change a
    published item, so the change lands in its history."""

    def wrap(
        method: Callable[Concatenate[S, str, P], Awaitable[T]],
    ) -> Callable[Concatenate[S, str, P], Awaitable[T]]:
        @functools.wraps(method)
        async def run(self: S, owner_id: str, /, *args: P.args, **kwargs: P.kwargs) -> T:
            item = await method(self, owner_id, *args, **kwargs)
            await checkpoint(self, owner_id, item, note)
            return item

        return run

    return wrap


async def restore[T: Published](
    store: Versioned[T], owner_id: str, subject_id: str, number: int
) -> T:
    """Rewrite the item as it was at version ``number``. A published item records the
    result as a new version; a draft just takes the old content."""
    bundle = await _revisions(store).load(owner_id, store.revision_kind, subject_id, number)
    token = _restoring.set(True)
    try:
        item = await store.restore_snapshot(owner_id, subject_id, bundle)
    finally:
        _restoring.reset(token)
    await checkpoint(store, owner_id, item, f"restored version {number}")
    return item


async def compare(
    store: Versioned[Published], owner_id: str, subject_id: str, number: int | None = None
) -> list[FileDiff]:
    """How the working copy differs from version ``number``, or from the latest version
    when ``number`` is None (everything is new if it was never published)."""
    revisions = _revisions(store)
    if number is None:
        before = await revisions.latest(owner_id, store.revision_kind, subject_id) or {}
    else:
        before = await revisions.load(owner_id, store.revision_kind, subject_id, number)
    return diff(before, await store.working_copy(owner_id, subject_id))


def diff(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> list[FileDiff]:
    changes: list[FileDiff] = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        status: Literal["added", "removed", "changed"] = (
            "added" if old is None else "removed" if new is None else "changed"
        )
        changes.append(FileDiff(path, status, _patch(path, old or b"", new or b"")))
    return changes


def _patch(path: str, old: bytes, new: bytes) -> str | None:
    try:
        before, after = old.decode(), new.decode()
    except UnicodeDecodeError:
        return None
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _revisions(store: Versioned[Published]) -> RevisionStore:
    if store.revisions is None:
        raise NotFoundError("this store keeps no history")
    return store.revisions


def _subject(owner_id: str, kind: str, subject_id: str):
    return (
        select(Revision)
        .where(col(Revision.owner_id) == owner_id)
        .where(col(Revision.kind) == kind)
        .where(col(Revision.subject_id) == subject_id)
    )


def _latest(session: Session, owner_id: str, kind: str, subject_id: str) -> Revision | None:
    return session.exec(
        _subject(owner_id, kind, subject_id).order_by(col(Revision.number).desc())
    ).first()


def _info(row: Revision) -> RevisionInfo:
    return RevisionInfo(row.number, row.note, row.size_bytes, row.created_at)


def encode_bundle(bundle: Bundle) -> bytes:
    files = {path: base64.b64encode(blob).decode() for path, blob in sorted(bundle.items())}
    return json.dumps(files, separators=(",", ":")).encode()


def decode_bundle(raw: bytes) -> Bundle:
    return {path: base64.b64decode(text) for path, text in _FILES.validate_json(raw).items()}
