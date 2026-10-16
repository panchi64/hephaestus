"""The memory toolset and the pinned-memory catalog the engine injects (`MEM-4`)."""

from __future__ import annotations

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from prompts.memory import MEMORY_PINNED_LIMIT
from runs import Run, RunStream
from services.memory import MemoryStore
from tools.deps import RunDeps
from tools.memory import memory_catalog_instructions, memory_toolset

from .test_memory import OWNER, FakeEmbedder, _store

_RESULT = TypeAdapter(dict[str, JsonValue])


def _ctx(store: MemoryStore | None) -> RunContext[RunDeps]:
    caps = ServiceContainer()
    if store is not None:
        caps.add(store, as_type=MemoryStore)
    run = Run(id="run-1", kind="chat", owner_id=OWNER, stream=RunStream())
    deps = RunDeps(run=run, owner_id=OWNER, conversation_id="conv-1", caps=caps)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


async def _invoke(name: str, ctx: RunContext[RunDeps], **kwargs: object) -> dict[str, JsonValue]:
    return _RESULT.validate_python(await memory_toolset().tools[name].function(ctx, **kwargs))


async def _unlocked() -> MemoryStore:
    _engine, vault, store = _store(FakeEmbedder())
    await vault.setup("pw")
    return store


# --- catalog --------------------------------------------------------------


async def test_catalog_lists_pins_sorted_by_id_and_counts_the_rest():
    store = await _unlocked()
    a = await store.remember(OWNER, "Call them Fran.", description="Form of address", pinned=True)
    b = await store.remember(OWNER, "Answers short.", description="Answer length", pinned=True)
    _ = await store.remember(OWNER, "The boiler is in the cellar.")

    block = await memory_catalog_instructions(_ctx(store))

    first, second = sorted([(a.id, "Form of address"), (b.id, "Answer length")])
    assert f"- {first[0]}: {first[1]}\n- {second[0]}: {second[1]}" in block
    assert "holds 1 more memory." in block
    assert "`memory_recall`" in block and "`memory_open`" in block
    assert "boiler" not in block


async def test_catalog_caps_the_pinned_list_and_counts_the_overflow():
    store = await _unlocked()
    for i in range(MEMORY_PINNED_LIMIT + 2):
        _ = await store.remember(OWNER, f"fact {i}", description=f"fact {i}", pinned=True)

    block = await memory_catalog_instructions(_ctx(store))

    assert block.count("\n- ") == MEMORY_PINNED_LIMIT
    assert "holds 2 more memories." in block


async def test_catalog_is_byte_stable_across_an_unrelated_save():
    store = await _unlocked()
    _ = await store.remember(OWNER, "Call them Fran.", description="Form of address", pinned=True)
    _ = await store.remember(OWNER, "x", description="x")
    before = await memory_catalog_instructions(_ctx(store))
    _ = await store.update(OWNER, (await store.list_memories(OWNER))[0].id, content="y")
    assert await memory_catalog_instructions(_ctx(store)) == before


async def test_catalog_is_empty_without_pins_store_or_unlocked_vault():
    assert await memory_catalog_instructions(_ctx(None)) == ""

    _engine, vault, store = _store(FakeEmbedder())
    await vault.setup("pw")
    _ = await store.remember(OWNER, "unpinned")
    assert await memory_catalog_instructions(_ctx(store)) == ""

    _ = await store.remember(OWNER, "Call them Fran.", pinned=True)
    assert await memory_catalog_instructions(_ctx(store)) != ""
    vault.lock()
    assert await memory_catalog_instructions(_ctx(store)) == ""


# --- tools ----------------------------------------------------------------


async def test_open_returns_the_full_memory():
    store = await _unlocked()
    view = await store.remember(OWNER, "Call them Fran.\nNever Francisco.", description="Name")

    result = await _invoke("open", _ctx(store), id=view.id)

    assert result == {
        "id": view.id,
        "description": "Name",
        "content": "Call them Fran.\nNever Francisco.",
        "pinned": False,
    }


async def test_open_unknown_id_retries_with_a_help_line():
    store = await _unlocked()
    with pytest.raises(ModelRetry) as raised:
        _ = await _invoke("open", _ctx(store), id="nope")
    assert str(raised.value).startswith("error: no memory with id 'nope'")
    assert "\n  help: " in str(raised.value)


async def test_update_changes_only_what_is_given():
    store = await _unlocked()
    view = await store.remember(OWNER, "Call them Francisco.", description="Name")

    result = await _invoke(
        "update", _ctx(store), id=view.id, content="Call them Fran.", pinned=True
    )

    assert result["content"] == "Call them Fran."
    assert result["description"] == "Name"
    assert result["pinned"] is True
    assert "Name" in await memory_catalog_instructions(_ctx(store))


async def test_update_unknown_id_retries():
    store = await _unlocked()
    with pytest.raises(ModelRetry):
        _ = await _invoke("update", _ctx(store), id="nope", pinned=True)


async def test_remember_and_recall_carry_id_and_description():
    store = await _unlocked()
    ctx = _ctx(store)
    saved = (
        await memory_toolset()
        .tools["remember"]
        .function(ctx, content="The boiler is in the cellar.", description="Boiler location")
    )
    assert "Remembered" in saved

    hits = await memory_toolset().tools["recall"].function(ctx, query="boiler")
    assert hits[0]["description"] == "Boiler location"
    assert hits[0]["id"] == (await store.list_memories(OWNER))[0].id
