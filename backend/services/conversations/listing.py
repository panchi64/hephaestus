"""Listing and lookup: the sidebar summaries, counts, titles and the sweep's candidate query."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_
from sqlmodel import Session, select

from core.db import in_session
from models.conversation import Conversation, Message
from services.projects import project_clause
from services.workspace_kind import workspace_kind

from .core import _StoreCore
from .paths import _active_path_model
from .projection import _project
from .records import ConversationSummaryView


def _db_stats(session: Session, conversation_ids: list[str]) -> dict[str, tuple[int, str | None]]:
    """(message_count, last-message text) per conversation, from the durable rows.

    One ``COUNT … GROUP BY`` for the counts and one max-seq lookup for the last
    text — no per-conversation row scan. Used only for **cold** conversations (no
    in-memory tree): the count is the total node count and the preview the latest
    by seq, which can include off-path branches; an active conversation overrides
    both from its tree (exact for the visible path). The returned text is still the
    encrypted ``Message.text`` ciphertext; the caller decrypts only what it renders.
    The last-used model isn't derived here — it rides denormalized on the
    ``Conversation`` row (kept in step with the active leaf), so listing never has
    to open a message blob."""
    if not conversation_ids:
        return {}
    counts = dict(
        session.exec(
            select(Message.conversation_id, func.count())
            .where(Message.conversation_id.in_(conversation_ids))
            .group_by(Message.conversation_id)
        ).all()
    )
    latest = (
        select(Message.conversation_id, func.max(Message.seq).label("seq"))
        .where(Message.conversation_id.in_(conversation_ids))
        .group_by(Message.conversation_id)
        .subquery()
    )
    last_text = dict(
        session.exec(
            select(Message.conversation_id, Message.text).join(
                latest,
                (Message.conversation_id == latest.c.conversation_id)
                & (Message.seq == latest.c.seq),
            )
        ).all()
    )
    return {cid: (counts.get(cid, 0), last_text.get(cid)) for cid in conversation_ids}


class _ListingMixin(_StoreCore):
    def _summarize(
        self, conversation: Conversation, db_count: int, last_text_enc: str | None
    ) -> ConversationSummaryView:
        """Build a listing summary, preferring the in-memory tree's active-path
        count + preview + model (exact for the visible thread, and ahead of the DB
        by the write-behind drainer) over the durable rows. Runs outside the DB
        session — only touches the vault + cache. The cold model comes from the
        denormalized ``Conversation.model``, kept in step with the active leaf, so
        warm and cold agree and listing never opens a blob."""
        cached = self._cache.get(conversation.id)
        if cached is not None:
            # Compaction checkpoints are chassis bookkeeping, not turns: they must not be
            # counted as messages, and the newest one must not become the listing preview
            # (it is the tip right after a compaction, so it otherwise would).
            path = [node for node in cached.active_path() if not node.compacted]
            count = len(path)
            preview = next(
                (text for text in (_project(n.message)[1] for n in reversed(path)) if text), None
            )
            model = _active_path_model(path)
        else:
            count = db_count
            decrypted = self._vault.decrypt_str(last_text_enc).strip() if last_text_enc else ""
            preview = decrypted or None
            model = conversation.model
        return ConversationSummaryView(
            id=conversation.id,
            title=self._open_title(conversation),
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            message_count=count,
            preview=preview[:140] if preview else None,
            work_summary=self._open_work_summary(conversation),
            model=model,
            workspace_kind=workspace_kind(conversation.workspace_kind),
            project_id=conversation.project_id,
            agent_name=conversation.agent_name,
        )

    async def list_conversations(
        self, owner_id: str, *, visible_projects: tuple[str | None, ...] | None = None
    ) -> list[ConversationSummaryView]:
        """Owner's conversations, newest-updated first, with a derived count +
        preview. The durable rows are the base; an active conversation's in-memory
        tree overrides count/preview so a just-sent turn shows immediately.

        ``visible_projects`` is the project scope (``services.projects``); ``None`` — the
        default — means no filtering, which is what every non-route caller wants."""

        def work(session: Session) -> list[tuple[Conversation, int, str | None]]:
            query = (
                select(Conversation)
                .where(Conversation.owner_id == owner_id)
                .where(Conversation.ephemeral == False)  # noqa: E712 — SQL boolean compare
            )
            scope = project_clause(Conversation.project_id, visible_projects)
            if scope is not None:
                query = query.where(scope)
            conversations = session.exec(query.order_by(Conversation.updated_at.desc())).all()
            stats = _db_stats(session, [c.id for c in conversations])
            return [(c, *stats.get(c.id, (0, None))) for c in conversations]

        rows = await in_session(self._engine, work)
        return [self._summarize(*row) for row in rows]

    async def count_conversations(self, owner_id: str) -> int:
        """How many conversations the owner has — a scalar count that loads no
        rows or previews (the overview readout, not the listing). Conversations
        are persisted on creation, so this is accurate the moment a turn starts."""

        def work(session: Session) -> int:
            return session.exec(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.owner_id == owner_id)
                .where(Conversation.ephemeral == False)  # noqa: E712 — SQL boolean compare
            ).one()

        return await in_session(self._engine, work)

    async def get_summary(
        self, conversation_id: str, owner_id: str
    ) -> ConversationSummaryView | None:
        """A single conversation's listing summary, or None if it isn't owned by
        ``owner_id``. Reads one thread's rows, not the whole corpus."""

        def work(session: Session) -> tuple[Conversation, int, str | None] | None:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or conversation.owner_id != owner_id:
                return None
            count, last_text_enc = _db_stats(session, [conversation_id])[conversation_id]
            return conversation, count, last_text_enc

        result = await in_session(self._engine, work)
        if result is None:
            return None
        return self._summarize(*result)

    async def titles(self, conversation_ids: list[str], owner_id: str) -> dict[str, str]:
        """The titles of the owner's conversations among ``conversation_ids``, keyed by id.

        Untitled threads and ids the owner doesn't have are absent. This is deliberately
        narrower than :meth:`get_summary` — no message count, no last-message preview, and
        one query for the whole set. The runs listing wants a name per run and nothing
        else; asking for a full summary per run made it read every thread's rows.
        """
        wanted = list(dict.fromkeys(conversation_ids))
        if not wanted:
            return {}

        def work(session: Session) -> list[Conversation]:
            return list(
                session.exec(
                    select(Conversation).where(
                        Conversation.owner_id == owner_id,
                        Conversation.id.in_(wanted),  # type: ignore[attr-defined]
                    )
                ).all()
            )

        rows = await in_session(self._engine, work)
        opened = ((row.id, self._open_title(row)) for row in rows)
        return {cid: title for cid, title in opened if title is not None}

    async def work_summary_candidates(
        self, owner_id: str, *, idle_before: datetime, limit: int, min_messages: int = 2
    ) -> list[str]:
        """Ids of threads whose work summary is worth (re)writing — the sweep's whole query.

        Three conditions, and each drops work the model should never be asked to do. The
        thread has been quiet since ``idle_before``, because a summary of a conversation
        still in flight is out of date before it is stored. Its summary is **stale** — never
        written, or written before the thread last spoke — so an idle thread costs nothing
        on every sweep after its first. And it has at least ``min_messages`` messages, since
        a thread holding one unanswered prompt has no work to account for and would produce
        a summary that only restates its own title.

        Ordered by most recently active and capped at ``limit``: a first sweep over a large
        workspace would otherwise queue one model call per thread the operator has ever
        opened, and the threads they are plausibly about to re-enter are the recent ones.
        Ephemeral threads are excluded like they are from every listing — nothing renders a
        band over a compare pane.
        """
        if limit <= 0:
            return []

        def work(session: Session) -> list[str]:
            # Correlated, not a `GROUP BY` over the whole table joined in. The grouped
            # form aggregates every message the operator has ever sent on every sweep —
            # a minute-by-minute full scan to learn the count for the handful of threads
            # that pass the filters below, most passes finding nothing to do at all.
            # Correlated, it runs per surviving row against the `conversation_id` index,
            # which is the same shape `_db_stats` uses for the same question.
            message_count = (
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == Conversation.id)
                .scalar_subquery()
            )
            query = (
                select(Conversation.id)
                .where(Conversation.owner_id == owner_id)
                .where(Conversation.ephemeral == False)  # noqa: E712 — SQL boolean compare
                .where(Conversation.updated_at < idle_before)
                .where(message_count >= min_messages)
                .where(
                    or_(
                        Conversation.work_summary_at.is_(None),  # type: ignore[union-attr]
                        Conversation.work_summary_at < Conversation.updated_at,
                    )
                )
                .order_by(Conversation.updated_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            return list(session.exec(query).all())

        return await in_session(self._engine, work)
