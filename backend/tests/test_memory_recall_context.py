"""The memories an operator's message recalls ride the tail of an attended turn."""

from __future__ import annotations

from core.container import ServiceContainer
from services.memory import MemoryStore
from tools.deps import PromptContextRequest
from tools.memory import memory_recall_context

from .test_memory import OWNER, FakeEmbedder, memory_store


async def _store() -> MemoryStore:
    _engine, vault, store = memory_store(FakeEmbedder())
    await vault.setup("pw")
    _ = await store.remember(OWNER, "My pet cat is called Biscuit", description="Pet cat")
    _ = await store.remember(OWNER, "My commute uses a car", description="Commute")
    return store


def _request(store: MemoryStore, text: str, *, attended: bool = True) -> PromptContextRequest:
    return PromptContextRequest(
        caps=ServiceContainer.of(store),
        owner_id=OWNER,
        conversation_id="c",
        project_id=None,
        worktree=False,
        workspace_key="c",
        prompt_text=text,
        attended=attended,
    )


async def test_a_message_that_recalls_a_memory_carries_it():
    store = await _store()
    block = await memory_recall_context(_request(store, "what is my cat called"))
    assert "- Pet cat: My pet cat is called Biscuit" in block
    assert "Commute" not in block


async def test_a_close_paraphrase_carries_the_memory():
    store = await _store()
    block = await memory_recall_context(_request(store, "tell me about my feline"))
    assert "Biscuit" in block


async def test_sharing_a_word_without_the_meaning_carries_nothing():
    store = await _store()
    assert await memory_recall_context(_request(store, "how was my weekend")) == ""


async def test_an_unattended_run_or_an_empty_message_carries_nothing():
    store = await _store()
    assert await memory_recall_context(_request(store, "my cat", attended=False)) == ""
    assert await memory_recall_context(_request(store, "  ")) == ""


async def test_a_locked_vault_carries_nothing():
    _engine, _vault, store = memory_store(FakeEmbedder())
    assert await memory_recall_context(_request(store, "my cat")) == ""
