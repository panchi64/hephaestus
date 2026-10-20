"""Attachment provenance: which uploads chats still reference, and detaching a deleted one."""

from __future__ import annotations

from collections.abc import Iterable

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
        tree = await self.tree(conversation_id)
        if message_id is not None:
            if message_id not in tree.nodes:
                return []
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
        surviving_refs |= await self.referenced_upload_ids(
            owner_id, exclude_conversation_id=conversation_id
        )
        return sorted(doomed_refs - surviving_refs)

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
