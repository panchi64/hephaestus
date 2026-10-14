"""The agent's task list, persisted per conversation and streamed as it changes.

``pydantic_ai_harness.Planning`` owns the tools and the model-facing behaviour; it depends
only on a six-method :class:`~pydantic_ai_harness.planning.PlanStore` protocol. This is our
implementation of it — sealed under the vault, keyed by conversation, and emitting a
``tasks.updated`` event on every mutation so the chat surface can render the list live.

**The harness's vocabulary is not ours.** Upstream calls an item a ``PlanItem`` and the
protocol a ``PlanStore``, because upstream has only one such concept. Here *plan* means the
written, approvable document a Plan-level turn produces (``services/plan_mode.py``), and
this is the running checklist underneath it — so the upstream names are imported under ours
and nothing below this line says "plan".

**Every mutation emits, including the bulk replace.** The harness's own stores leave
``set_items`` event-silent (it is a wholesale replacement), which would be a real hole
here: ``write`` is the tool a model reaches for first and most often, so a surface built on
events alone would sit empty through exactly the call that matters. Emitting from the store
rather than from a hook means every path — bulk or granular — reports uniformly.

**The event carries the whole list, not a delta.** The per-run stream is replayable from
any sequence number (``runs/``), so a full-state event is idempotent on replay and needs no
ordering rules; the list is a handful of short strings, and correctness here is worth more
than the bytes.

**A locked vault degrades to no tasks rather than an error.** The list is an aid to the
turn, not the turn itself: a run that cannot read it should carry on without one instead of
failing, which is why reads fall back to empty and writes are dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging

from pydantic_ai_harness.planning import PlanItem as TaskItem
from pydantic_ai_harness.planning import PlanStore as TaskStoreProtocol
from pydantic_ai_harness.planning import TaskStatus, render_plan
from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.vault import Vault, VaultError, VaultLocked
from models._fields import new_id, utcnow
from models.task_list import ConversationTaskList
from runs import Run, TasksUpdated

logger = logging.getLogger(__name__)

#: The harness's renderer, re-exported under our word for it: the same text, reached by
#: the name the rest of this codebase uses. Callers render the task list for a prompt
#: through this rather than importing the upstream name and re-introducing "plan".
render_tasks = render_plan


def _dump(items: list[TaskItem]) -> str:
    return json.dumps([item.model_dump(mode="json") for item in items])


def _load(raw: str) -> list[TaskItem]:
    return [TaskItem.model_validate(row) for row in json.loads(raw)]


def tasks_payload(items: list[TaskItem]) -> list[dict[str, object]]:
    """The task list as the frontend consumes it — the same shape on the event and on the
    REST backfill, so a reload rebuilds exactly what the stream was drawing."""
    return [
        {
            "id": item.id,
            "content": item.content,
            "status": (
                item.status.value if isinstance(item.status, TaskStatus) else str(item.status)
            ),
            "active_form": item.active_form,
        }
        for item in items
    ]


class ConversationTasks:
    """Reads and writes the stored task list for a conversation. Owner-scoped;
    vault-sealed."""

    def __init__(self, db_engine: Engine, vault: Vault) -> None:
        self._db = db_engine
        self._vault = vault
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, conversation_id: str) -> asyncio.Lock:
        """The mutation lock for one conversation's task list.

        Every write is a read-modify-write of the whole list across two awaits, and a
        model may emit several task calls in a single response that Pydantic AI then runs
        **concurrently** — without this, two "mark task done" calls interleave and the
        second silently discards the first. Per conversation rather than global so two
        threads working on different chats never wait on each other.
        """
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = self._locks[conversation_id] = asyncio.Lock()
        return lock

    async def items(self, owner_id: str, conversation_id: str) -> list[TaskItem]:
        def work(session: Session) -> str | None:
            row = session.exec(
                select(ConversationTaskList)
                .where(ConversationTaskList.owner_id == owner_id)
                .where(ConversationTaskList.conversation_id == conversation_id)
            ).first()
            return row.items_enc if row else None

        sealed = await in_session(self._db, work)
        if sealed is None:
            return []
        try:
            return _load(self._vault.decrypt_str(sealed))
        except VaultLocked, VaultError:
            logger.debug("task list unreadable for %s: vault locked", conversation_id)
            return []

    async def delete_for_conversation(self, owner_id: str, conversation_id: str) -> None:
        """Drop a thread's task list when the thread goes.

        The list restates what the operator asked for, so leaving it behind would keep a
        description of a deleted conversation on disk — the same reason the delete path
        already purges the View history and the sandbox workspace. Works while the vault
        is locked: it only destroys.
        """

        def work(session: Session) -> None:
            for row in session.exec(
                select(ConversationTaskList)
                .where(ConversationTaskList.owner_id == owner_id)
                .where(ConversationTaskList.conversation_id == conversation_id)
            ).all():
                session.delete(row)

        await in_session(self._db, work)
        self._locks.pop(conversation_id, None)

    async def replace(self, owner_id: str, conversation_id: str, items: list[TaskItem]) -> bool:
        """Store ``items``; ``False`` when the vault was locked and nothing was written.

        The caller needs the distinction: announcing a change that was silently dropped
        would leave the panel showing tasks a reload proves were never saved.
        """
        try:
            sealed = self._vault.encrypt_str(_dump(items))
        except VaultLocked, VaultError:
            logger.debug("tasks not stored for %s: vault locked", conversation_id)
            return False

        def work(session: Session) -> None:
            row = session.exec(
                select(ConversationTaskList)
                .where(ConversationTaskList.owner_id == owner_id)
                .where(ConversationTaskList.conversation_id == conversation_id)
            ).first()
            if row is None:
                row = ConversationTaskList(
                    id=new_id(),
                    owner_id=owner_id,
                    conversation_id=conversation_id,
                    items_enc=sealed,
                )
            else:
                row.items_enc = sealed
            row.updated_at = utcnow()
            session.add(row)

        await in_session(self._db, work)
        return True

    async def seed(
        self, owner_id: str, conversation_id: str, steps: list[str], *, run: Run | None = None
    ) -> list[TaskItem]:
        """Replace the list with ``steps`` as fresh pending tasks, announcing the result.

        The one write that does not come from the model. An approved plan's steps *are*
        the work, so execution starts from a list that already says so rather than from an
        empty one the agent has to restate — and it replaces rather than appends, because
        an approved plan supersedes whatever the thread was tracking before it.

        Emits through the same event as every other mutation, so the panel cannot tell a
        seeded list from a written one — which is right: by the time the operator sees it,
        it is simply the thread's task list.
        """
        items = [
            TaskItem(id=f"t{index}", content=step, status=TaskStatus.pending)
            for index, step in enumerate(steps, start=1)
        ]
        async with self.lock_for(conversation_id):
            stored = await self.replace(owner_id, conversation_id, items)
        if stored and run is not None:
            run.emit(TasksUpdated(items=tasks_payload(items)))
        return items if stored else []


class ConversationTaskStore(TaskStoreProtocol):
    """One conversation's task list, as the harness's store protocol.

    Bound to a run so each mutation can emit; the six protocol methods are expressed over
    read-modify-write of the whole list, which keeps the stored form and the emitted form
    the same thing and leaves no path that changes one without the other.
    """

    def __init__(
        self,
        tasks: ConversationTasks,
        *,
        owner_id: str,
        conversation_id: str,
        run: Run | None = None,
    ) -> None:
        self._tasks = tasks
        self._owner_id = owner_id
        self._conversation_id = conversation_id
        self._run = run
        # Held across each read-modify-write below. It lives on the shared `tasks` handle,
        # not here, because a fresh store object is built per run — a lock owned by this
        # object would be a different lock for every caller and guard nothing.
        self._lock = tasks.lock_for(conversation_id)

    def bind_run(self, run: Run | None) -> None:
        """Point emissions at the run currently working this conversation.

        The store outlives any one turn (it is cached per conversation so the task tools
        keep their identity), while the `Run` it emits on is per turn — without this, the
        second turn's task changes would stream onto the first turn's dead stream and the
        panel would stop updating live.
        """
        self._run = run

    async def get_items(self) -> list[TaskItem]:
        return await self._tasks.items(self._owner_id, self._conversation_id)

    async def set_items(self, items: list[TaskItem]) -> None:
        # A wholesale replace reads nothing first, so it needs no lock of its own — but it
        # still takes one so it can't land in the middle of another call's read-modify-write.
        async with self._lock:
            await self._commit(list(items))

    async def get_item(self, item_id: str) -> TaskItem | None:
        return next((i for i in await self.get_items() if i.id == item_id), None)

    async def add_item(self, item: TaskItem) -> TaskItem:
        async with self._lock:
            items = await self.get_items()
            if any(existing.id == item.id for existing in items):
                # The protocol requires this: a duplicate id would shadow the original and
                # make later updates land on one of them at random.
                raise ValueError(f"task {item.id!r} already exists")
            items.append(item)
            await self._commit(items)
        return item

    async def update_item(
        self,
        item_id: str,
        *,
        content: str | None = None,
        status: TaskStatus | None = None,
        active_form: str | None = None,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> TaskItem | None:
        async with self._lock:
            items = await self.get_items()
            updated: TaskItem | None = None
            for index, item in enumerate(items):
                if item.id != item_id:
                    continue
                updated = item.model_copy(
                    update={
                        key: value
                        for key, value in (
                            ("content", content),
                            ("status", status),
                            ("active_form", active_form),
                            ("parent_id", parent_id),
                            ("depends_on", depends_on),
                        )
                        if value is not None
                    }
                )
                items[index] = updated
                break
            if updated is None:
                return None
            await self._commit(items)
        return updated

    async def remove_item(self, item_id: str) -> bool:
        async with self._lock:
            items = await self.get_items()
            remaining = [i for i in items if i.id != item_id]
            if len(remaining) == len(items):
                return False
            await self._commit(remaining)
        return True

    async def _commit(self, items: list[TaskItem]) -> None:
        stored = await self._tasks.replace(self._owner_id, self._conversation_id, items)
        # Only announce what actually landed. A locked vault drops the write silently, and
        # emitting anyway would draw a panel that a reload contradicts.
        if stored and self._run is not None:
            self._run.emit(TasksUpdated(items=tasks_payload(items)))
