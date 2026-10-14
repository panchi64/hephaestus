"""Branching: regenerate, edit, version switch, rewind, and subtree delete.

Each turn runs through the real chat orchestrator with a ``TestModel`` (no tools ⇒
two messages per turn), so these exercise the store's tree exactly as production
does: history is the active path, and record() chains new messages off the active
leaf — which navigation has moved to create a branch.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.models.test import TestModel
from sqlalchemy import Engine

from agent import build_chat_orchestrator
from core.db import init_db, make_engine
from core.vault import Vault
from runs import RunRegistry
from services.conversations import ConversationStore


async def _unlocked_vault(tmp_path: Path, name: str = "keyfile.json") -> Vault:
    vault = Vault(tmp_path / name)
    if not vault.is_initialized:
        await vault.setup("pw")
    else:
        _ = await vault.unlock("pw")
    return vault


async def fresh_store(tmp_path: Path) -> tuple[ConversationStore, Engine]:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return ConversationStore(engine, await _unlocked_vault(tmp_path)), engine


async def _turn(
    store: ConversationStore, conv: str, reg: RunRegistry, *, prompt: str | None, answer: str
) -> None:
    orch = build_chat_orchestrator(
        prompt,
        model=TestModel(custom_output_text=answer),
        categories={},  # no tools → request + response only
        store=store,
        conversation_id=conv,
    )
    run = reg.submit(kind="chat", owner_id="operator", orchestrator=orch)
    await run.wait()


async def test_regenerate_adds_a_sibling_answer(tmp_path: Path):
    store, _ = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="hello", answer="first answer")
    view = await store.messages_view(conv)
    assert [m.role for m in view] == ["user", "assistant"]
    assistant_id = view[1].id
    assert view[1].content == "first answer"
    assert view[1].version_count == 1

    # Regenerate that answer: re-run from the user request (no new prompt).
    assert await store.regenerate_point(conv, assistant_id)
    await _turn(store, conv, reg, prompt=None, answer="second answer")

    view = await store.messages_view(conv)
    assert [m.role for m in view] == ["user", "assistant"]  # still one of each
    assert view[0].content == "hello"  # the user turn is untouched
    assert view[1].content == "second answer"
    assert view[1].version_count == 2  # two answers now
    assert view[1].version_index == 1  # showing the newest
    await store.stop()


async def test_switch_version_restores_an_earlier_answer(tmp_path: Path):
    store, _ = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="hello", answer="first answer")
    assistant_id = (await store.messages_view(conv))[1].id
    assert await store.regenerate_point(conv, assistant_id)
    await _turn(store, conv, reg, prompt=None, answer="second answer")

    # Cycle back to version 0 — the original answer is restored as the active path.
    newest_id = (await store.messages_view(conv))[1].id
    assert await store.switch_version(conv, newest_id, 0)
    view = await store.messages_view(conv)
    assert view[1].content == "first answer"
    assert view[1].version_index == 0
    assert view[1].version_count == 2

    # An out-of-range version is rejected.
    assert not await store.switch_version(conv, view[1].id, 5)
    await store.stop()


async def test_edit_forks_the_user_turn(tmp_path: Path):
    store, _ = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="first question", answer="answer one")
    user_id = (await store.messages_view(conv))[0].id

    # Edit the question: branch from its parent and re-ask.
    assert await store.edit_point(conv, user_id)
    await _turn(store, conv, reg, prompt="second question", answer="answer two")

    view = await store.messages_view(conv)
    assert view[0].content == "second question"
    assert view[0].version_count == 2  # the user turn now has two versions
    assert view[0].version_index == 1
    assert view[1].content == "answer two"

    # Switching the user turn back to version 0 restores the original exchange.
    assert await store.switch_version(conv, view[0].id, 0)
    view = await store.messages_view(conv)
    assert view[0].content == "first question"
    assert view[1].content == "answer one"
    await store.stop()


async def test_rewind_then_send_branches(tmp_path: Path):
    store, _ = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="q1", answer="a1")
    await _turn(store, conv, reg, prompt="q2", answer="a2")
    assert len(await store.messages_view(conv)) == 4

    # Rewind to the first assistant turn, then ask something different.
    first_assistant = (await store.messages_view(conv))[1].id
    assert await store.rewind(conv, first_assistant)
    assert len(await store.messages_view(conv)) == 2  # thread ends at the first turn

    await _turn(store, conv, reg, prompt="q2-alt", answer="a2-alt")
    view = await store.messages_view(conv)
    assert [m.content for m in view] == ["q1", "a1", "q2-alt", "a2-alt"]
    # The second user turn has two versions: the original q2 and the new q2-alt.
    assert view[2].version_count == 2
    await store.stop()


async def test_rewind_to_user_turn_ends_before_its_answer(tmp_path: Path):
    store, _ = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="q1", answer="a1")
    await _turn(store, conv, reg, prompt="q2", answer="a2")

    # Rewinding to a user turn ends the thread at that message (its answer is the
    # next turn, so it must not be carried along).
    second_user = (await store.messages_view(conv))[2].id
    assert await store.rewind(conv, second_user)
    view = await store.messages_view(conv)
    assert [m.content for m in view] == ["q1", "a1", "q2"]
    await store.stop()


async def test_delete_removes_turn_and_downstream(tmp_path: Path):
    store, engine = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="q1", answer="a1")
    await _turn(store, conv, reg, prompt="q2", answer="a2")

    # Delete the second user turn — it and its answer go; the first turn remains.
    second_user = (await store.messages_view(conv))[2].id
    assert await store.delete_message(conv, second_user)
    view = await store.messages_view(conv)
    assert [m.content for m in view] == ["q1", "a1"]

    # The deletion is durable: a cold store sees the same trimmed history.
    await store.stop()
    cold = ConversationStore(engine, await _unlocked_vault(tmp_path))
    await cold.start()
    cold_view = await cold.messages_view(conv)
    assert [m.content for m in cold_view] == ["q1", "a1"]
    await cold.stop()


async def test_pin_is_projected_and_durable(tmp_path: Path):
    store, engine = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="remember this", answer="noted")
    assistant_id = (await store.messages_view(conv))[1].id
    assert (await store.messages_view(conv))[1].pinned is False

    assert await store.set_pin(conv, assistant_id, True)
    assert (await store.messages_view(conv))[1].pinned is True
    assert not await store.set_pin(conv, "nope", True)  # unknown id

    # The pin survives a cold reload.
    await store.stop()
    cold = ConversationStore(engine, await _unlocked_vault(tmp_path))
    await cold.start()
    assert (await cold.messages_view(conv))[1].pinned is True
    await cold.stop()


async def test_navigation_survives_cold_reload(tmp_path: Path):
    """A regenerated branch and its active-leaf choice persist across a restart."""
    store, engine = await fresh_store(tmp_path)
    await store.start()
    reg = RunRegistry()
    conv = await store.create_conversation("operator")

    await _turn(store, conv, reg, prompt="hello", answer="first answer")
    assistant_id = (await store.messages_view(conv))[1].id
    assert await store.regenerate_point(conv, assistant_id)
    await _turn(store, conv, reg, prompt=None, answer="second answer")
    await store.stop()

    cold = ConversationStore(engine, await _unlocked_vault(tmp_path))
    await cold.start()
    view = await cold.messages_view(conv)
    assert view[1].content == "second answer"  # active leaf restored
    assert view[1].version_count == 2  # both answers persisted
    await cold.stop()
