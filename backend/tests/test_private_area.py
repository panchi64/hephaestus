"""The Private area: PIN, relock, and what the API, recall and memory let through."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlmodel import Session

from agent.naming import approval_conversation_title
from core.container import ServiceContainer
from core.exceptions import InvalidInputError, PermissionDeniedError, PrivateAreaLocked
from models.conversation import Conversation
from models.subagent import SubagentRecord
from routes.deps import app_state
from runs import Run, RunStream
from services.memory import MemoryStore
from services.permissions.guards import GUARD_KEY
from services.private_area import MAX_FAILED_UNLOCKS, PrivateArea
from services.settings_store import SettingsStore
from tools.deps import RunDeps
from tools.memory import memory_toolset
from tools.platform import PlatformDocument, PlatformResults

from ._helpers import client_app
from ._json import b, json_body, json_list, obj, s
from .test_conversation_search import record_conversation
from .test_platform_search import OWNER, Platform, make_platform

PIN = "4821"


class Clock:
    def __init__(self) -> None:
        self.now: datetime = datetime(2026, 10, 31, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Env:
    platform: Platform
    area: PrivateArea
    clock: Clock

    def ctx(
        self, *, approved: bool = False, conversation_id: str = "current"
    ) -> RunContext[RunDeps]:
        run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
        caps = ServiceContainer.of(self.platform.index, self.area)
        caps.add(self.platform.memory, as_type=MemoryStore)
        deps = RunDeps(run=run, owner_id=OWNER, conversation_id=conversation_id, caps=caps)
        return RunContext(
            deps=deps, model=TestModel(), usage=RunUsage(), tool_call_approved=approved
        )

    async def search(self, query: str, sources: list[str] | None = None) -> PlatformResults:
        result = cast(
            PlatformResults | str,
            await self.platform.tools["search"].function(
                self.ctx(approved=True), query, sources=sources
            ),
        )
        assert isinstance(result, PlatformResults)
        return result

    async def read(
        self,
        ref: str,
        purpose: str | None = None,
        *,
        approved: bool = False,
        conversation_id: str = "current",
    ) -> PlatformDocument | str:
        return cast(
            PlatformDocument | str,
            await self.platform.tools["read"].function(
                self.ctx(approved=approved, conversation_id=conversation_id), ref, purpose=purpose
            ),
        )

    async def remember(self, conversation_id: str, *, approved: bool = False) -> str:
        return cast(
            str,
            await memory_toolset()
            .tools["remember"]
            .function(
                self.ctx(conversation_id=conversation_id, approved=approved),
                "owes 3,120",
                description="Tax owed",
            ),
        )


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    platform = await make_platform()
    clock = Clock()
    area = PrivateArea(
        platform.vault,
        SettingsStore(platform.engine),
        platform.store,
        clock=clock,
    )
    yield Env(platform, area, clock)
    await platform.store.stop()


async def _private_chat(env: Env, *turns: tuple[str, str]) -> str:
    conversation_id = await record_conversation(env.platform.store, OWNER, *turns)
    await env.platform.store.set_title(conversation_id, "Taxes 2026")
    await env.platform.store.set_private(conversation_id, True)
    return conversation_id


# --- PIN and relock -----------------------------------------------------------------------


async def test_setting_a_pin_opens_the_area_and_the_pin_reopens_it(env: Env):
    assert not (await env.area.status(OWNER)).pin_set
    await env.area.set_pin(OWNER, PIN)
    assert env.area.is_unlocked()
    env.area.lock()
    assert not await env.area.unlock(OWNER, "0000")
    assert await env.area.unlock(OWNER, PIN)


async def test_a_pin_must_be_four_to_twelve_digits(env: Env):
    for bad in ("123", "abcd", "1234567890123"):
        with pytest.raises(InvalidInputError):
            await env.area.set_pin(OWNER, bad)


async def test_replacing_a_pin_needs_the_current_pin_or_the_password(env: Env):
    await env.area.set_pin(OWNER, PIN)
    with pytest.raises(PermissionDeniedError):
        await env.area.set_pin(OWNER, "9999")
    await env.area.set_pin(OWNER, "9999", current_pin=PIN)
    await env.area.set_pin(OWNER, "5555", password="pw")
    env.area.lock()
    assert await env.area.unlock(OWNER, "5555")


async def test_the_fifth_wrong_pin_locks_the_vault(env: Env):
    await env.area.set_pin(OWNER, PIN)
    env.area.lock()
    for _ in range(MAX_FAILED_UNLOCKS - 1):
        assert not await env.area.unlock(OWNER, "0000")
    assert env.platform.vault.is_unlocked
    assert not await env.area.unlock(OWNER, "0000")
    assert not env.platform.vault.is_unlocked


async def test_wrong_proofs_on_a_pin_change_count_toward_the_vault_lock(env: Env):
    await env.area.set_pin(OWNER, PIN)
    for _ in range(MAX_FAILED_UNLOCKS - 1):
        with pytest.raises(PermissionDeniedError):
            await env.area.set_pin(OWNER, "9999", current_pin="0000")
    assert env.platform.vault.is_unlocked
    assert not await env.area.unlock(OWNER, "0000")
    assert not env.platform.vault.is_unlocked


async def test_the_area_relocks_after_the_idle_window_and_use_extends_it(env: Env):
    await env.area.set_pin(OWNER, PIN)
    _ = await env.area.set_relock_minutes(OWNER, 10)
    env.clock.now += timedelta(minutes=8)
    env.area.touch()
    env.clock.now += timedelta(minutes=8)
    assert env.area.is_unlocked()
    env.clock.now += timedelta(minutes=3)
    assert not env.area.is_unlocked()
    env.area.touch()
    assert not env.area.is_unlocked()


async def test_a_vault_lock_closes_the_area(env: Env):
    await env.area.set_pin(OWNER, PIN)
    env.platform.vault.register_on_lock(env.area.lock)
    env.platform.vault.lock()
    assert not env.area.is_unlocked()


async def test_a_private_thread_is_refused_while_locked(env: Env):
    await env.area.set_pin(OWNER, PIN)
    chat = await _private_chat(env, ("q", "a"))
    await env.area.require_open(chat)
    with pytest.raises(PrivateAreaLocked):
        await env.area.require_open(chat, by_operator=False)
    env.area.lock()
    with pytest.raises(PrivateAreaLocked):
        await env.area.require_open(chat)


async def test_a_spawned_thread_inherits_and_follows_its_parents_flag(env: Env):
    store = env.platform.store
    parent = await store.create_conversation(OWNER)
    await store.set_private(parent, True)
    child = await store.create_conversation(OWNER, parent_id=parent)
    assert await env.area.is_private(child)
    await store.set_private(parent, False)
    assert not await env.area.is_private(child)


async def test_filing_a_spawned_thread_moves_its_whole_family(env: Env):
    store = env.platform.store
    parent = await store.create_conversation(OWNER)
    child = await store.create_conversation(OWNER, parent_id=parent)
    grandchild = await store.create_conversation(OWNER, parent_id=child)
    await store.set_private(child, True)
    assert [await env.area.is_private(c) for c in (parent, child, grandchild)] == [True] * 3
    await store.set_private(grandchild, False)
    assert [await env.area.is_private(c) for c in (parent, child, grandchild)] == [False] * 3


# --- recall --------------------------------------------------------------------------------


async def _private_material(env: Env) -> str:
    chat = await _private_chat(env, ("what did I owe in taxes", "you owed 3,120 dollars"))
    now = datetime.now(UTC)
    vault = env.platform.vault
    with Session(env.platform.engine) as session:
        row = session.get(Conversation, chat)
        assert row is not None
        row.work_summary_enc = vault.encrypt_str("Worked out the taxes owed for 2026.")
        row.work_summary_at = now
        session.add(row)
        session.add(
            SubagentRecord(
                owner_id=OWNER,
                parent_conversation_id=chat,
                child_conversation_id="child",
                run_id="run-x",
                spec_name="explorer",
                handle="tax-checker",
                task_enc=vault.encrypt_str("Check the taxes owed"),
                summary_enc=vault.encrypt_str("The taxes owed add up"),
                status="done",
                ended_at=now,
            )
        )
        session.commit()
    return chat


async def test_a_locked_area_leaves_no_trace_in_recall(env: Env):
    await env.area.set_pin(OWNER, PIN)
    chat = await _private_material(env)
    env.area.lock()
    results = await env.search("taxes owed", ["conversations", "summaries", "reports"])
    assert results.hits == []
    read = await env.read(f"surf-conversations:{chat}", "need the total")
    assert isinstance(read, str) and read.startswith("Nothing found")


async def test_an_open_area_shows_only_a_placeholder(env: Env):
    await env.area.set_pin(OWNER, PIN)
    chat = await _private_material(env)
    results = await env.search("taxes owed", ["conversations", "summaries", "reports"])
    assert [hit.ref for hit in results.hits] == [f"surf-conversations:{chat}"]
    hit = results.hits[0]
    assert hit.title == "Locked chat"
    assert "3,120" not in hit.excerpt and "Taxes 2026" not in hit.excerpt


async def test_reading_a_private_chat_needs_a_purpose_and_the_operator(env: Env):
    await env.area.set_pin(OWNER, PIN)
    chat = await _private_material(env)
    asker = await record_conversation(env.platform.store, OWNER, ("budget", "ok"))
    await env.platform.store.set_title(asker, "Budget planning")
    ref = f"surf-conversations:{chat}"
    with pytest.raises(ModelRetry):
        _ = await env.read(ref)
    with pytest.raises(ApprovalRequired) as asked:
        _ = await env.read(ref, "the tax total", conversation_id=asker)
    metadata: dict[str, object] = asked.value.metadata or {}
    # The card is parked in the asking thread and outlives the area being open.
    assert "Taxes 2026" not in str(metadata[GUARD_KEY])
    assert metadata["explanation"] == 'Asked from "Budget planning": the tax total'

    document = await env.read(ref, "the tax total", approved=True)
    assert isinstance(document, PlatformDocument)
    assert "3,120" in document.text


async def test_an_approval_landing_after_relock_reads_nothing(env: Env):
    await env.area.set_pin(OWNER, PIN)
    chat = await _private_material(env)
    env.area.lock()
    read = await env.read(f"surf-conversations:{chat}", "the total", approved=True)
    assert isinstance(read, str) and read.startswith("Nothing found")


async def test_recall_without_the_area_shows_nothing_private(env: Env):
    await env.area.set_pin(OWNER, PIN)
    chat = await _private_material(env)
    ctx = env.ctx(approved=True)
    ctx.deps.caps = ServiceContainer.of(env.platform.index)
    tools = env.platform.tools
    found = cast(
        PlatformResults,
        await tools["search"].function(
            ctx, "taxes owed", sources=["conversations", "summaries", "reports"]
        ),
    )
    assert found.hits == []
    read = cast(str, await tools["read"].function(ctx, f"surf-conversations:{chat}", "total"))
    assert read.startswith("Nothing found")


# --- memory and notifications ------------------------------------------------------------


async def test_a_private_chat_asks_before_writing_memory(env: Env):
    await env.area.set_pin(OWNER, PIN)
    chat = await _private_chat(env, ("q", "a"))
    with pytest.raises(ApprovalRequired) as asked:
        _ = await env.remember(chat)
    assert GUARD_KEY in (asked.value.metadata or {})
    assert (await env.remember(chat, approved=True)).startswith("Remembered")
    ordinary = await record_conversation(env.platform.store, OWNER, ("q", "a"))
    assert (await env.remember(ordinary)).startswith("Remembered")


async def test_a_notification_never_names_a_private_chat(env: Env):
    chat = await _private_chat(env, ("q", "a"))
    assert await approval_conversation_title(env.platform.store, OWNER, chat) == "A private chat"


# --- routes --------------------------------------------------------------------------------


async def test_the_routes_hide_and_refuse_private_threads_while_locked():
    async with client_app() as (client, app):
        store = app_state(app).conversations
        chat = await store.create_conversation("operator")
        plain = await store.create_conversation("operator")

        refused = await client.put(f"/conversations/{chat}/private", json={"private": True})
        assert refused.status_code == 422  # no PIN yet
        status = obj(json_body(await client.post("/private/pin", json={"pin": PIN})))
        assert b(status["pin_set"]) and b(status["unlocked"])
        moved = await client.put(f"/conversations/{chat}/private", json={"private": True})
        assert b(obj(json_body(moved))["private"])

        listed = {s(obj(row)["id"]) for row in json_list(await client.get("/conversations"))}
        assert listed == {chat, plain}

        _ = await client.post("/private/lock")
        listed = {s(obj(row)["id"]) for row in json_list(await client.get("/conversations"))}
        assert listed == {plain}
        for response in (
            await client.get(f"/conversations/{chat}"),
            await client.patch(f"/conversations/{chat}", json={"title": "x"}),
            await client.put(f"/conversations/{chat}/private", json={"private": False}),
            await client.post("/chat", json={"prompt": "hi", "conversation_id": chat}),
            await client.get("/views/snapshots", params={"conversation_id": chat}),
            await client.get(f"/browser/session/{chat}"),
            await client.get(f"/worktrees/{chat}"),
        ):
            assert response.status_code == 403
            assert json_body(response) == {"detail": "private_locked"}
        assert (await client.get(f"/conversations/{plain}")).status_code == 200

        wrong = await client.post("/private/unlock", json={"pin": "0000"})
        assert wrong.status_code == 403
        assert json_body(wrong) == {"detail": "wrong PIN"}
        assert (await client.post("/private/unlock", json={"pin": PIN})).status_code == 200
        assert (await client.get(f"/conversations/{chat}")).status_code == 200
