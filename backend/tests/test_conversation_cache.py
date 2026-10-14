"""What the conversation store keeps in memory, and what it gives back.

Every warm entry is a fully *decrypted* tree — inline image bytes included — so an
unbounded one grows to the size of every conversation the process has ever touched. The
bound is what makes that a working set instead of a leak, and it is only real if the
things that make an entry unevictable (a queued write, a rehydrate in flight) are the
exceptions and not the rule. These pin the residency policy itself, since nothing about
it is visible from a passing read.
"""

from __future__ import annotations

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from core.db import init_db, make_engine
from core.vault import Vault
from services.conversations import ConversationStore

OWNER = "operator"


async def _store(tmp_path, **kwargs) -> ConversationStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return ConversationStore(engine, vault, **kwargs)


def _turn(prompt: str, answer: str) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=prompt)]),
        ModelResponse(parts=[TextPart(content=answer)]),
    ]


async def test_the_tree_cache_stays_at_its_cap_however_many_threads_are_touched(tmp_path):
    store = await _store(tmp_path, max_cached_conversations=3)
    ids = [await store.create_conversation(OWNER) for _ in range(10)]

    assert len(store._cache) == 3
    # Least-recently-used first: the survivors are the last three created.
    assert list(store._cache) == ids[-3:]


async def test_reading_an_evicted_thread_rehydrates_it_and_evicts_the_coldest(tmp_path):
    store = await _store(tmp_path, max_cached_conversations=2)
    await store.start()  # the drainer, so the turn is durable and its pin released
    try:
        first = await store.create_conversation(OWNER)
        store.record(first, _turn("q", "a"))
        await store._worker.join()
        await store.create_conversation(OWNER)
        await store.create_conversation(OWNER)
        assert first not in store._cache  # pushed out by the two newer threads

        # A miss costs one rehydrate and nothing else — the turn is still there.
        history = await store.messages_view(first)

        assert [m.content for m in history] == ["q", "a"]
        assert first in store._cache
        assert len(store._cache) == 2
    finally:
        await store.stop()


async def test_a_thread_with_a_queued_write_is_never_evicted_under_it(tmp_path):
    # `record()` extends the cached tree in place and queues only the new slice, so
    # evicting mid-turn would leave the next append building on an empty tree. The cache
    # runs over its cap instead; the overflow drains as the writes land.
    store = await _store(tmp_path, max_cached_conversations=1)
    pinned = await store.create_conversation(OWNER)
    store.record(pinned, _turn("q", "a"))  # queued: the drainer was never started

    for _ in range(5):
        await store.create_conversation(OWNER)

    assert pinned in store._cache
    assert store._pending[pinned] == 1


async def test_the_owner_memo_is_bounded_too(tmp_path):
    # The memo sits on the write-behind path, which touches every conversation the
    # process persists a turn for — it has to have a ceiling of its own, since it long
    # outlives the trees it was populated beside.
    store = await _store(tmp_path, max_cached_conversations=2)
    ids = [await store.create_conversation(OWNER) for _ in range(40)]

    assert len(store._owners) == store._max_memoized_owners == 16
    assert ids[0] not in store._owners

    # Pure cache: a miss re-reads the immutable column rather than losing the owner.
    assert await store._owner_of(ids[0]) == OWNER
