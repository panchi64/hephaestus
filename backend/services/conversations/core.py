"""The store's shared state and residency: the tree cache, the per-conversation rehydrate, the
owner memo, the write-behind queue, and the thread lifecycle (create, delete).
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from typing import TYPE_CHECKING

from sqlalchemy import Engine, delete
from sqlmodel import Session, col, select

from core.db import in_session
from core.vault import Vault
from core.worker import WriteBehindWorker
from models.conversation import Conversation, Message
from services.embeddings import Embedder
from services.permissions import DEFAULT_PERMISSION, permission_level
from services.sealing import open_sealed
from services.workspace_kind import DEFAULT_WORKSPACE_KIND
from services.workspace_kind import workspace_kind as normalise_workspace_kind

from .projection import MESSAGE
from .records import ConversationBinding, ConversationSummaryView
from .rows import HydratedRow, PersistJob, defer_fk
from .tree import Node, Tree

# How many conversation trees stay decrypted in memory. Sized for one operator: the
# working set is the handful of threads actually being read or written, and a miss
# costs one rehydrate from the database, not an error.
_DEFAULT_MAX_CACHED_CONVERSATIONS = 64

# How many owner memo rows are kept per cached tree. An owner row is two short strings
# against a whole decrypted conversation, so the memo can outlive the trees it was
# populated alongside by a wide margin and still cost nothing worth counting.
_OWNER_MEMO_MULTIPLE = 8


class StoreCore:
    if TYPE_CHECKING:
        # Implemented by sibling mixins; declared here so mixins can call across.
        async def _persist(self, _job: PersistJob, /) -> None: ...
        def _on_drop(self, _job: PersistJob, _exc: Exception, /) -> None: ...
        def _open_work_summary(self, _conversation: Conversation, /) -> str | None: ...
        async def get_summary(
            self, _conversation_id: str, _owner_id: str, /
        ) -> ConversationSummaryView | None: ...
        async def binding(self, _conversation_id: str, /) -> ConversationBinding: ...

    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        embedder: Embedder | None = None,
        *,
        max_cached_conversations: int = _DEFAULT_MAX_CACHED_CONVERSATIONS,
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        # Embeds each persisted turn's text for cross-chat semantic search. Optional
        # (and best-effort): with no embedder, or when it degrades, messages persist
        # without a vector and recall over them falls back to keyword (EMB-2).
        self._embedder: Embedder | None = embedder
        # Least-recently-used first. Every entry holds a fully *decrypted* tree —
        # including any inline image bytes a turn carried — so an unbounded dict grows
        # to the size of every conversation the process has ever touched and never
        # gives it back. It is a pure cache (`tree` rehydrates on a miss), so evicting
        # costs one reload and nothing else.
        self._cache: OrderedDict[str, Tree] = OrderedDict()
        self._max_cached: int = max_cached_conversations
        # Conversations with durable work still queued. `record()` extends the cached
        # tree in place and hands the drainer a *slice*, so evicting between the two
        # would leave `setdefault` rebuilding an empty tree and the turn appending to
        # nothing. Never evict one of these; the count drains to zero as the writes land.
        self._pending: Counter[str] = Counter()
        # conversation id → owner. Immutable per conversation, so it is memoized rather
        # than re-read on the write-behind path once per persisted turn. Bounded like the
        # tree cache and for the same reason — a memo that only ever grows is a leak with
        # a nicer name, and this one is written from the drainer, which touches every
        # conversation the process ever persists a turn for. Entries are two short strings,
        # so its cap is a multiple of the tree cap rather than the same number: the point
        # is a ceiling, and evicting a row that a later turn re-reads costs one query.
        self._owners: OrderedDict[str, str] = OrderedDict()
        self._max_memoized_owners: int = max_cached_conversations * _OWNER_MEMO_MULTIPLE
        self._locks: dict[str, asyncio.Lock] = {}
        self._worker: WriteBehindWorker[PersistJob] = WriteBehindWorker(
            self._persist,
            name="persistence-drainer",
            unlocked=vault.unlocked_event,
            on_drop=self._on_drop,
        )

    async def start(self) -> None:
        await self._worker.start()

    async def stop(self) -> None:
        await self._worker.stop()

    @property
    def engine(self) -> Engine:
        """The database engine the conversations live in."""
        return self._engine

    @property
    def embedder(self) -> Embedder | None:
        """The embedder for message vectors; assignable when the endpoint changes."""
        return self._embedder

    @embedder.setter
    def embedder(self, value: Embedder | None) -> None:
        self._embedder = value

    @property
    def vault(self) -> Vault:
        """The vault that seals message payloads."""
        return self._vault

    async def flush(self) -> None:
        """Wait until the write-behind queue has drained."""
        await self._worker.join()

    def evict_cache(self) -> None:
        """Drop every cached tree so the next read rehydrates from the database."""
        self._cache.clear()

    @property
    def cached_ids(self) -> list[str]:
        """Cached conversation ids, least recently used first."""
        return list(self._cache)

    def pending_writes(self, conversation_id: str) -> int:
        """How many queued durable writes still pin this conversation's tree."""
        return self._pending[conversation_id]

    @property
    def memoized_owner_ids(self) -> list[str]:
        """Conversation ids whose owner is memoized, least recently used first."""
        return list(self._owners)

    @property
    def max_memoized_owners(self) -> int:
        """The owner memo's ceiling."""
        return self._max_memoized_owners

    # --- cache residency -----------------------------------------------------------
    def _cache_put(self, conversation_id: str, tree: Tree) -> Tree:
        """Install a tree as the most-recently-used entry and trim back to the cap."""
        self._cache[conversation_id] = tree
        self._cache.move_to_end(conversation_id)
        self._trim_cache()
        return tree

    def cache_get(self, conversation_id: str) -> Tree | None:
        """The cached tree, counting the read as a use so it isn't evicted next.

        Only genuine reads go through here. The listing summary and the compaction
        leaf check peek with a plain ``self._cache.get`` on purpose: the listing walks
        *every* conversation, so promoting each would flatten the ordering into
        meaninglessness and could evict the thread actually being used.
        """
        tree = self._cache.get(conversation_id)
        if tree is not None:
            self._cache.move_to_end(conversation_id)
        return tree

    def _trim_cache(self) -> None:
        """Drop least-recently-used trees until the cache is back within its cap.

        Entries with queued durable work are skipped, never dropped: `record()` mutates
        the cached tree and queues only the new slice, so evicting one mid-turn would
        leave the next `record` appending to a freshly-built empty tree. If every entry
        is pending — far more in flight than the cap — the cache simply runs over rather
        than corrupting a tree; the overflow drains on its own as the writes land.
        """
        if len(self._cache) <= self._max_cached:
            return
        for conversation_id in list(self._cache):
            if len(self._cache) <= self._max_cached:
                break
            if self._pending[conversation_id]:
                continue
            # A held lock means a rehydrate is in flight for this id. Dropping the lock
            # under it would let a second rehydrate run concurrently and clobber the
            # tree the first one is about to install.
            lock = self._locks.get(conversation_id)
            if lock is not None and lock.locked():
                continue
            del self._cache[conversation_id]
            _ = self._locks.pop(conversation_id, None)

    async def create_conversation(
        self,
        owner_id: str,
        title: str | None = None,
        ephemeral: bool = False,
        *,
        project_id: str | None = None,
        workspace_kind: str = DEFAULT_WORKSPACE_KIND,
        permission: str | None = None,
        agent_name: str | None = None,
        parent_id: str | None = None,
        workspace_key: str | None = None,
    ) -> str:
        """Start a thread. ``project_id`` files it (null is unfiled, which is visible
        under every scope). ``workspace_kind`` is where its file work happens; only a fork
        of a worktree thread starts in one (see `models/conversation.py`).

        ``permission`` is the level it *starts* at and moves freely afterwards; absent, the
        default level applies. ``agent_name`` is the agent it runs; absent, the default.
        ``parent_id`` and ``workspace_key`` are a spawned agent's: the thread that spawned it,
        and the workspace every turn of it works in."""

        def work(session: Session) -> str:
            conversation = Conversation(
                owner_id=owner_id,
                title_enc=self._seal_title(title),
                ephemeral=ephemeral,
                project_id=project_id,
                workspace_kind=normalise_workspace_kind(workspace_kind),
                agent_name=agent_name,
                parent_id=parent_id,
                workspace_key=workspace_key or None,
                permission_level=(
                    DEFAULT_PERMISSION if permission is None else permission_level(permission)
                ),
            )
            session.add(conversation)
            session.flush()
            return conversation.id

        conversation_id = await in_session(self._engine, work)
        _ = self._cache_put(conversation_id, Tree())
        self._remember_owner(conversation_id, owner_id)
        return conversation_id

    async def exists(self, conversation_id: str, owner_id: str) -> bool:
        """Whether ``conversation_id`` names a conversation owned by ``owner_id``."""

        def work(session: Session) -> bool:
            conversation = session.get(Conversation, conversation_id)
            return conversation is not None and conversation.owner_id == owner_id

        return await in_session(self._engine, work)

    async def tree(self, conversation_id: str) -> Tree:
        """The conversation's tree — from the cache, or rehydrated once from the DB."""
        cached = self.cache_get(conversation_id)
        if cached is not None:
            return cached

        # Serialize rehydration per conversation and re-check inside the lock, so a
        # concurrent record()/history() can't be clobbered by a stale DB snapshot.
        async with self._locks.setdefault(conversation_id, asyncio.Lock()):
            cached = self.cache_get(conversation_id)
            if cached is not None:
                return cached

            def work(session: Session) -> tuple[list[HydratedRow], str | None]:
                rows = session.exec(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(col(Message.seq))
                ).all()
                conversation = session.get(Conversation, conversation_id)
                active = conversation.active_leaf_id if conversation is not None else None
                return [
                    HydratedRow(
                        id=r.id,
                        parent_id=r.parent_id,
                        seq=r.seq,
                        pinned=r.pinned,
                        blob=r.blob,
                        attachment_ids=r.attachment_ids,
                        blocked_reason=r.blocked_reason,
                        compacted=r.compacted,
                        compacted_through=r.compacted_through,
                        llm_ms=r.llm_ms,
                        ttft_ms=r.ttft_ms,
                        tool_ms=r.tool_ms,
                    )
                    for r in rows
                ], active

            rows, active = await in_session(self._engine, work)
            tree = Tree()
            for row in rows:  # pre-sorted by seq
                message = MESSAGE.validate_json(self._vault.decrypt_str(row.blob))
                tree.add(
                    Node(
                        id=row.id,
                        parent_id=row.parent_id,
                        seq=row.seq,
                        message=message,
                        pinned=row.pinned,
                        attachment_ids=row.attachment_ids,
                        blocked_reason=row.blocked_reason,
                        compacted=row.compacted,
                        compacted_through=row.compacted_through,
                        llm_ms=row.llm_ms,
                        ttft_ms=row.ttft_ms,
                        tool_ms=row.tool_ms,
                    )
                )
            tree.active_leaf_id = active if active in tree.nodes else tree.fallback_leaf()
            return self._cache_put(conversation_id, tree)

    def _seal_title(self, title: str | None) -> str | None:
        """A title on its way to the DB. ``None`` stays ``None`` — an untitled thread has
        no ciphertext, which is not the same as one whose title is the empty string."""
        return None if title is None else self._vault.encrypt_str(title)

    def _open_title(self, conversation: Conversation) -> str | None:
        """A title on its way out — sealed, or the legacy cleartext of a row the startup
        backfill hasn't reached yet, so a half-migrated DB never renders a garbled or
        missing thread name."""
        return open_sealed(self._vault, conversation.title_enc, conversation.title)

    async def delete_conversation(self, conversation_id: str) -> None:
        """Drop a conversation and its messages from the durable record, and evict
        the in-memory tree."""

        def work(session: Session) -> None:
            conversation = session.get(Conversation, conversation_id)
            # Defer FK checks so the whole message set drops in one statement,
            # without ordering children before parents for the self-referential FK.
            defer_fk(session)
            _ = session.exec(delete(Message).where(col(Message.conversation_id) == conversation_id))
            if conversation is not None:
                session.delete(conversation)

        await in_session(self._engine, work)
        _ = self._cache.pop(conversation_id, None)
        _ = self._locks.pop(conversation_id, None)
        _ = self._owners.pop(conversation_id, None)

    async def owner_of(self, conversation_id: str) -> str | None:
        """The conversation's owner, memoized. A conversation never changes hands, and
        this sits on the write-behind path — one extra round trip per persisted turn,
        purely to re-read an immutable column."""
        cached = self._owners.get(conversation_id)
        if cached is not None:
            self._owners.move_to_end(conversation_id)
            return cached

        def work(session: Session) -> str | None:
            conversation = session.get(Conversation, conversation_id)
            return conversation.owner_id if conversation is not None else None

        owner_id = await in_session(self._engine, work)
        if owner_id is not None:
            self._remember_owner(conversation_id, owner_id)
        return owner_id

    def _remember_owner(self, conversation_id: str, owner_id: str) -> None:
        """Memoize an owner as the most-recently-used row, dropping the oldest past the
        cap. Pure cache: `owner_of` re-reads the column on a miss."""
        self._owners[conversation_id] = owner_id
        self._owners.move_to_end(conversation_id)
        while len(self._owners) > self._max_memoized_owners:
            _ = self._owners.popitem(last=False)

    def _submit(self, job: PersistJob) -> None:
        """Queue durable work and pin its conversation in the cache until it lands.

        The pin is what makes eviction safe: between this call and the write, the
        cached tree is the only place the turn's structure exists.
        """
        self._pending[job.conversation_id] += 1
        self._worker.submit(job)

    def _release(self, job: PersistJob) -> None:
        """Drop this job's pin once it has reached a terminal disposition."""
        conversation_id = job.conversation_id
        if self._pending[conversation_id] <= 1:
            del self._pending[conversation_id]  # keep the Counter from growing forever
        else:
            self._pending[conversation_id] -= 1
