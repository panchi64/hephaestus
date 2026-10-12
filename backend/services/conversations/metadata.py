"""A thread's own settings and sealed text: titles, work summary, binding, permission level,
compaction override and context overhead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from core.db import in_session
from models.conversation import Conversation
from runs.overhead import TurnOverhead
from services.permissions import PermissionLevel, permission_level
from services.sealing import open_sealed
from services.workspace_kind import workspace_kind

from .core import _StoreCore
from .records import ConversationBinding


class _MetadataMixin(_StoreCore):
    async def set_title(self, conversation_id: str, title: str | None) -> None:
        """Rename a conversation (and bump its updated_at)."""
        title_enc = self._seal_title(title)

        def work(session: Session) -> None:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.title_enc = title_enc
                # A rename supersedes any legacy cleartext this row still carried, so drop
                # it here rather than leaving the old name readable until the backfill runs.
                conversation.title = None
                conversation.updated_at = datetime.now(UTC)

        await in_session(self._engine, work)

    async def set_title_if_absent(self, conversation_id: str, title: str) -> bool:
        """Set the title only when the conversation has none yet; return whether it
        was applied. This is the authoritative "fill, don't overwrite" guard for
        auto-titling — it can never clobber a name the operator chose, and a caller
        only announces the title when this returns True. Atomic within one session
        (check-and-set), so it is safe against a concurrent rename."""

        title_enc = self._vault.encrypt_str(title)

        def work(session: Session) -> bool:
            conversation = session.get(Conversation, conversation_id)
            # "Has a name already" is asked of the *effective* title, so a legacy
            # cleartext name still blocks an auto-title from clobbering it.
            if conversation is None or (self._open_title(conversation) or "").strip():
                return False
            conversation.title_enc = title_enc
            conversation.title = None
            conversation.updated_at = datetime.now(UTC)
            return True

        return await in_session(self._engine, work)

    def _open_work_summary(self, conversation: Conversation) -> str | None:
        """The thread's work summary on its way out.

        Through the same sealed-column reader the title uses, with no legacy cleartext to
        fall back to: the column was born sealed, so there is no half-migrated state here
        and ``None`` means only "never summarized". Sharing the reader rather than calling
        the vault directly keeps one answer to "how is a sealed column opened" — the day a
        row can be sealed under an older key, this reads it the way the title does."""
        return open_sealed(self._vault, conversation.work_summary_enc, None)

    async def set_work_summary(self, conversation_id: str, summary: str) -> None:
        """Store this thread's work summary, replacing whatever was there.

        A **plain replace**, deliberately unlike :meth:`set_title_if_absent`: the title is
        fill-only because the operator may have named the thread and an auto-title must
        never clobber a name they chose, while the summary has no operator-authored twin —
        it is the sweep's own output and a fresher one is strictly better than the account
        of a thread that has moved on since.

        Sealed on the write path like the title, and for the same reason: the sweep runs
        with the vault unlocked, and this does not ride the drainer's queue.

        **``updated_at`` is deliberately not bumped.** It is what the staleness rule and
        the idle window are both read off, so touching it here would make every summary
        immediately look fresh *and* push the thread's idle clock forward — the write would
        rewrite the two facts it is supposed to be measured against. ``work_summary_at``
        carries the time instead."""
        summary_enc = self._vault.encrypt_str(summary)
        generated_at = datetime.now(UTC)

        def work(session: Session) -> None:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.work_summary_enc = summary_enc
                conversation.work_summary_at = generated_at

        await in_session(self._engine, work)

    async def binding(self, conversation_id: str) -> ConversationBinding:
        """This thread's binding — the facts a run needs to know where it works and how far
        it may go, read together so no path can pick up one and default the others.

        A missing conversation reads as an unfiled sandbox thread at the default level,
        which is the same answer a stateless turn gets and the safe one: a sandbox never
        touches the host.
        """

        def work(session: Session) -> ConversationBinding:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return ConversationBinding()
            # Both columns are plain strings, so an unrecognised value (a restored backup
            # from a future version, a hand-edited row) is normalised towards the answer
            # that does least.
            return ConversationBinding(
                workspace_kind=workspace_kind(conversation.workspace_kind),
                project_id=conversation.project_id,
                permission=permission_level(conversation.permission_level),
            )

        return await in_session(self._engine, work)

    async def is_hidden(self, conversation_id: str) -> bool:
        """Whether this thread is kept out of the operator's listing.

        Exists for the one caller that has to know whether there is anywhere to send the
        operator: a notification is a deep link, and a thread they cannot find in their own
        session list is one the link lands them in with no way back. A conversation that no
        longer exists reads as hidden, which is the answer that says nothing rather than
        the one that announces a row nobody can open.
        """

        def work(session: Session) -> bool:
            conversation = session.get(Conversation, conversation_id)
            return True if conversation is None else bool(conversation.ephemeral)

        return await in_session(self._engine, work)

    async def open_worktree(self, conversation_id: str) -> bool:
        """Move this thread from its sandbox to its project's worktree. One-way.

        False when the thread does not exist or has no project to check out.
        """

        def work(session: Session) -> bool:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or conversation.project_id is None:
                return False
            conversation.workspace_kind = "worktree"
            return True

        return await in_session(self._engine, work)

    async def set_permission_level(self, conversation_id: str, level: str) -> PermissionLevel:
        """Move this thread to ``level``, returning what it is now.

        The one binding fact that moves after creation, and the reason it is a plain write
        rather than a new row: the level is the operator's live control, changed by sending
        at a different level or by accepting a plan, and a thread mid-flight keeps every
        other thing about itself. A run already in progress is unaffected — it carries the
        level it started with on its own binding, so a switch lands on the next turn rather
        than halfway through this one.

        Deliberately does not bump ``updated_at``: choosing what the model may do next is
        not activity in the thread, the same reasoning the compaction override follows.
        """
        resolved = permission_level(level)

        def work(session: Session) -> None:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.permission_level = resolved

        await in_session(self._engine, work)
        return resolved

    async def get_compaction_override(self, conversation_id: str) -> bool | None:
        """This conversation's auto-compaction override — ``None`` inherits the operator
        default, ``True``/``False`` force it on/off for this thread."""

        def work(session: Session) -> bool | None:
            conversation = session.get(Conversation, conversation_id)
            return conversation.auto_compact_override if conversation is not None else None

        return await in_session(self._engine, work)

    async def set_compaction_override(self, conversation_id: str, override: bool | None) -> None:
        """Set (or clear, with ``None``) this conversation's auto-compaction override.
        A quiet preference, so it deliberately does not bump ``updated_at`` (it isn't
        activity)."""

        def work(session: Session) -> None:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.auto_compact_override = override

        await in_session(self._engine, work)

    async def get_overhead(self, conversation_id: str) -> TurnOverhead | None:
        """What this thread's last turn carried besides the conversation — the standing
        brief and the tool schemas, itemised.

        The half of the context readout that a cold load cannot measure: neither reaches
        the message history, so a reopened thread has only the footprint total without
        this. ``None`` for a thread that has not run a turn since this was recorded, and
        the breakdown is then absent rather than guessed."""

        def work(session: Session) -> Any:
            conversation = session.get(Conversation, conversation_id)
            return conversation.context_overhead if conversation is not None else None

        return TurnOverhead.from_dict(await in_session(self._engine, work))

    async def set_overhead(self, conversation_id: str, overhead: TurnOverhead | None) -> None:
        """Record what the turn that just ran weighed, for the next cold load of *this*
        thread.

        A failed measurement is ignored rather than stored as absence: the previous good
        figure still describes this thread's configuration better than nothing does. Like
        the compaction override, this deliberately does not bump ``updated_at`` — it is
        bookkeeping about a turn, not activity of its own."""
        if overhead is None:
            return

        def work(session: Session) -> None:
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.context_overhead = overhead.as_dict()

        await in_session(self._engine, work)
