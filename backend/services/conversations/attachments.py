"""Attachment provenance: which uploads chats still reference, and detaching a deleted one."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import String, column, func, true, type_coerce
from sqlmodel import Session, col, select

from core.db import in_session
from models.conversation import Conversation, Message

from .core import StoreCore


class AttachmentsMixin(StoreCore):
    # ``attachment_ids`` is a clear JSON column, so everything here reads it directly with
    # no decrypt. Deletes use these to purge an attached image only once nothing references it.

    async def referenced_upload_ids(
        self, owner_id: str, *, exclude_conversation_id: str | None = None
    ) -> set[str]:
        """Every upload id still referenced by one of the owner's messages — the union of
        ``attachment_ids`` across the durable record (a DB scan) overlaid with the warm
        in-memory trees (which can run ahead of the write-behind drainer). Pass
        ``exclude_conversation_id`` to ignore one conversation — how the delete flow asks
        "does anything *else* still reference this image?"."""

        def work(session: Session) -> set[str]:
            stmt = (
                select(col(Message.attachment_ids))
                .join(Conversation, col(Message.conversation_id) == Conversation.id)
                .where(Conversation.owner_id == owner_id)
            )
            if exclude_conversation_id is not None:
                stmt = stmt.where(Message.conversation_id != exclude_conversation_id)
            refs: set[str] = set()
            for ids in session.exec(stmt):
                if ids:
                    refs.update(ids)
            return refs

        refs = await in_session(self._engine, work)
        for cid, tree in self._cache.items():
            if cid == exclude_conversation_id:
                continue
            for node in tree.nodes.values():
                if node.attachment_ids:
                    refs.update(node.attachment_ids)
        return refs

    async def orphaned_attachments_for_delete(
        self, owner_id: str, conversation_id: str, *, message_id: str | None = None
    ) -> list[str]:
        """The upload ids that would lose their last reference if this delete proceeds —
        attached only inside the doomed scope and nowhere surviving. ``message_id`` scopes
        the delete to that turn's subtree; ``None`` means the whole conversation.

        Computed from the authoritative in-memory tree (ahead of the drainer, so write-
        behind lag can't trigger a false purge), minus every surviving reference: this
        conversation's nodes *outside* the doomed set, plus every *other* conversation. The
        surviving set is a deliberate superset (DB ∪ warm caches), which can only ever
        spare an image, never purge one still in use. Returns ids regardless of mime — the
        caller filters to images."""
        doomed_refs, surviving_refs = await self._split_refs(conversation_id, message_id)
        if not doomed_refs:
            return []
        surviving_refs |= await self.referenced_upload_ids(
            owner_id, exclude_conversation_id=conversation_id
        )
        return sorted(doomed_refs - surviving_refs)

    async def subtree_attachment_ids(self, conversation_id: str, message_id: str) -> set[str]:
        """The upload ids attached in this turn's subtree."""
        doomed_refs, _ = await self._split_refs(conversation_id, message_id)
        return doomed_refs

    async def _split_refs(
        self, conversation_id: str, message_id: str | None
    ) -> tuple[set[str], set[str]]:
        """Upload ids attached inside the doomed scope, and those attached in the rest of
        the thread. ``message_id`` scopes to that turn's subtree; ``None`` is the thread."""
        tree = await self.tree(conversation_id)
        if message_id is not None:
            if message_id not in tree.nodes:
                return set(), set()
            doomed = set(tree.subtree_ids(message_id))
        else:
            doomed = set(tree.nodes)
        doomed_refs: set[str] = set()
        surviving_refs: set[str] = set()
        for node_id, node in tree.nodes.items():
            if not node.attachment_ids:
                continue
            bucket = doomed_refs if node_id in doomed else surviving_refs
            bucket.update(node.attachment_ids)
        return doomed_refs, surviving_refs

    async def attachment_ids(self, owner_id: str, conversation_ids: Iterable[str]) -> set[str]:
        """Every upload id attached anywhere in these conversations: the durable rows
        overlaid with the warm trees, which can run ahead of the drainer."""
        wanted = set(conversation_ids)

        def work(session: Session) -> set[str]:
            stmt = (
                select(col(Message.attachment_ids))
                .join(Conversation, col(Message.conversation_id) == Conversation.id)
                .where(Conversation.owner_id == owner_id)
                .where(col(Message.conversation_id).in_(wanted))
            )
            refs: set[str] = set()
            for ids in session.exec(stmt):
                if ids:
                    refs.update(ids)
            return refs

        refs = await in_session(self._engine, work)
        for cid in wanted:
            tree = self._cache.get(cid)
            if tree is None:
                continue
            for node in tree.nodes.values():
                if node.attachment_ids:
                    refs.update(node.attachment_ids)
        return refs

    async def attachment_privacy(
        self, owner_id: str, upload_ids: Iterable[str]
    ) -> dict[str, bool | None]:
        """For each upload: True when every chat attaching it is private, False when any
        ordinary chat does, None when no chat does."""
        wanted = set(upload_ids)
        if not wanted:
            return {}

        users: dict[str, set[str]] = {uid: set() for uid in wanted}
        for cid, tree in self._cache.items():
            for node in tree.nodes.values():
                for uid in node.attachment_ids or ():
                    if uid in users:
                        users[uid].add(cid)

        def work(session: Session) -> dict[str, bool]:
            attached = func.json_each(Message.attachment_ids).table_valued(column("value", String))
            upload_id = type_coerce(attached.columns[0], String)
            stmt = (
                select(col(Message.conversation_id), upload_id)
                .join(attached, true())
                .join(Conversation, col(Message.conversation_id) == Conversation.id)
                .where(Conversation.owner_id == owner_id)
                .where(upload_id.in_(wanted))
                .distinct()
            )
            for cid, uid in session.exec(stmt):
                users[uid].add(cid)
            # The warm trees run ahead of the drainer; their threads count only if they
            # are this owner's.
            cids = {cid for refs in users.values() for cid in refs}
            return dict(
                session.exec(
                    select(Conversation.id, Conversation.private).where(
                        col(Conversation.id).in_(cids), Conversation.owner_id == owner_id
                    )
                ).all()
            )

        private = await in_session(self._engine, work)
        result: dict[str, bool | None] = {}
        for uid, refs in users.items():
            owned = [private[cid] for cid in refs if cid in private]
            result[uid] = all(owned) if owned else None
        return result

    async def detach_uploads(self, owner_id: str, upload_ids: Iterable[str]) -> None:
        """Drop these upload ids from every message that lists them, in the warm trees and
        the durable rows, so no conversation points at bytes that are gone."""
        gone = set(upload_ids)
        if not gone:
            return
        for tree in self._cache.values():
            for node in tree.nodes.values():
                if node.attachment_ids and gone.intersection(node.attachment_ids):
                    node.attachment_ids = [a for a in node.attachment_ids if a not in gone]

        def work(session: Session) -> None:
            rows = session.exec(
                select(Message)
                .join(Conversation, col(Message.conversation_id) == Conversation.id)
                .where(Conversation.owner_id == owner_id)
            ).all()
            for row in rows:
                if row.attachment_ids and gone.intersection(row.attachment_ids):
                    row.attachment_ids = [a for a in row.attachment_ids if a not in gone]
                    session.add(row)

        await in_session(self._engine, work)
