"""Stored agents: what the operator and agents write."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.exceptions import AgentValidationError, NotFoundError
from services.agents import AgentStore, ToolAccess
from services.conversations import ConversationStore

from ._agent_helpers import OWNER, agent_values, make_stores

Stores = tuple[AgentStore, ConversationStore]


@pytest.fixture
async def stores(tmp_path: Path) -> Stores:
    return await make_stores(tmp_path)


async def test_a_new_agent_is_a_draft_by_default(stores: Stores):
    store, _ = stores
    agent = await store.create(OWNER, agent_values())
    assert agent.published is False
    assert agent.source == "user"
    assert agent.id is not None


async def test_the_source_and_published_flag_are_the_callers_to_set(stores: Stores):
    store, _ = stores
    agent = await store.create(OWNER, agent_values(), source="agent", published=True)
    assert (agent.source, agent.published) == ("agent", True)


async def test_the_default_name_is_reserved(stores: Stores):
    store, _ = stores
    with pytest.raises(AgentValidationError) as raised:
        _ = await store.create(OWNER, agent_values("default"))
    assert raised.value.field == "name"


async def test_a_duplicate_name_is_refused(stores: Stores):
    store, _ = stores
    _ = await store.create(OWNER, agent_values())
    with pytest.raises(AgentValidationError) as raised:
        _ = await store.create(OWNER, agent_values())
    assert raised.value.field == "name"


async def test_names_are_normalised(stores: Stores):
    store, _ = stores
    assert (await store.create(OWNER, agent_values("Helper Two"))).name == "helper-two"


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"name": ""}, "name"),
        ({"name": "bad name!"}, "name"),
        ({"description": "  "}, "description"),
        ({"permission_ceiling": "godmode"}, "permission_ceiling"),
        ({"workspace": "elsewhere"}, "workspace"),
        ({"wall_clock_timeout_s": -1.0}, "wall_clock_timeout_s"),
        ({"max_depth": -1}, "max_depth"),
        ({"source": "builtin"}, "source"),
    ],
)
async def test_bad_input_names_the_field(stores: Stores, values: dict[str, object], field: str):
    store, _ = stores
    with pytest.raises(AgentValidationError) as raised:
        _ = await store.create(OWNER, {**agent_values(), **values})
    assert raised.value.field == field


async def test_every_field_round_trips(stores: Stores):
    store, _ = stores
    created = await store.create(
        OWNER,
        agent_values(
            tools=ToolAccess(allow=frozenset({"files", "web_search"}), deny=frozenset({"shell"})),
            permission_ceiling="plan",
            workspace="own",
            wall_clock_timeout_s=30.0,
            can_delegate=True,
            max_depth=2,
            endpoint_id="ep",
            model="m",
        ),
    )
    assert created.id is not None
    got = await store.get(OWNER, created.id)
    assert got == created
    assert got.tools.allow == frozenset({"files", "web_search"})
    assert got.tools.deny == frozenset({"shell"})
    assert (got.permission_ceiling, got.workspace) == ("plan", "own")
    assert (got.wall_clock_timeout_s, got.max_depth) == (30.0, 2)
    assert got.can_delegate is True


async def test_unrestricted_tools_come_back_unrestricted(stores: Stores):
    store, _ = stores
    created = await store.create(OWNER, agent_values())
    assert created.tools == ToolAccess()


async def test_an_update_changes_only_what_it_names(stores: Stores):
    store, _ = stores
    created = await store.create(OWNER, agent_values())
    assert created.id is not None
    changed = await store.update(OWNER, created.id, {"description": "Now different."})
    assert changed.description == "Now different."
    assert changed.instructions == "Be helpful."


async def test_renaming_carries_the_threads_that_ran_it(stores: Stores):
    store, conversations = stores
    created = await store.create(OWNER, agent_values())
    assert created.id is not None
    mine = await conversations.create_conversation(OWNER, agent_name="helper")
    other = await conversations.create_conversation(OWNER, agent_name="someone-else")

    _ = await store.update(OWNER, created.id, {"name": "assistant"})

    assert (await conversations.binding(mine)).agent_name == "assistant"
    assert (await conversations.binding(other)).agent_name == "someone-else"


async def test_renaming_onto_a_taken_name_is_refused(stores: Stores):
    store, _ = stores
    _ = await store.create(OWNER, agent_values("one"))
    two = await store.create(OWNER, agent_values("two"))
    assert two.id is not None
    with pytest.raises(AgentValidationError):
        _ = await store.update(OWNER, two.id, {"name": "one"})


async def test_publishing_and_unpublishing(stores: Stores):
    store, _ = stores
    created = await store.create(OWNER, agent_values())
    assert created.id is not None
    assert (await store.set_published(OWNER, created.id, True)).published is True
    assert (await store.set_published(OWNER, created.id, False)).published is False


async def test_listing_can_be_limited_to_published(stores: Stores):
    store, _ = stores
    draft = await store.create(OWNER, agent_values("draft"))
    live = await store.create(OWNER, agent_values("live"), published=True)
    assert draft.id is not None
    assert [a.name for a in await store.list(OWNER)] == ["draft", "live"]
    assert [a.name for a in await store.list(OWNER, published_only=True)] == [live.name]


async def test_delete_removes_it(stores: Stores):
    store, _ = stores
    created = await store.create(OWNER, agent_values())
    assert created.id is not None
    await store.delete(OWNER, created.id)
    assert await store.get_by_name(OWNER, "helper") is None
    with pytest.raises(NotFoundError):
        _ = await store.get(OWNER, created.id)


async def test_another_owners_agent_is_not_found(stores: Stores):
    store, _ = stores
    created = await store.create(OWNER, agent_values())
    assert created.id is not None
    with pytest.raises(NotFoundError):
        _ = await store.get("someone-else", created.id)
    assert await store.list("someone-else") == []
