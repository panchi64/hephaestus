"""Resolving an agent across stored, project and built-in sources."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.exceptions import AgentValidationError
from services.agents import DEFAULT_AGENT, AgentCatalog, AgentStore, builtin_roster, lower_of

from ._agent_helpers import OWNER, agent_values, make_stores

Parts = tuple[AgentStore, AgentCatalog]


@pytest.fixture
async def parts(tmp_path: Path) -> Parts:
    store, _ = await make_stores(tmp_path)
    return store, AgentCatalog(store)


def _project_agent(root: Path, name: str, description: str) -> None:
    path = root / ".claude" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\nDo it.\n")


async def test_the_builtins_are_there_with_no_other_source(parts: Parts):
    _, catalog = parts
    assert set(await catalog.agents(OWNER)) == set(builtin_roster())


async def test_a_project_agent_shadows_a_builtin(parts: Parts, tmp_path: Path):
    _, catalog = parts
    _project_agent(tmp_path, "reviewer", "Ours.")
    agents = await catalog.agents(OWNER, project_root=tmp_path)
    assert agents["reviewer"].description == "Ours."
    assert agents["reviewer"].source == "project"


async def test_a_stored_agent_shadows_a_project_agent(parts: Parts, tmp_path: Path):
    store, catalog = parts
    _project_agent(tmp_path, "reviewer", "Ours.")
    _ = await store.create(OWNER, agent_values("reviewer", description="Stored."), published=True)
    agents = await catalog.agents(OWNER, project_root=tmp_path)
    assert agents["reviewer"].description == "Stored."


async def test_drafts_are_hidden_unless_asked_for(parts: Parts):
    store, catalog = parts
    _ = await store.create(OWNER, agent_values("wip"))
    assert "wip" not in await catalog.agents(OWNER)
    assert "wip" in await catalog.agents(OWNER, include_drafts=True)


async def test_a_stored_agent_cannot_be_named_default(parts: Parts):
    store, _ = parts
    with pytest.raises(AgentValidationError):
        _ = await store.create(OWNER, agent_values(DEFAULT_AGENT), published=True)


async def test_a_project_file_named_default_does_not_shadow_it(parts: Parts, tmp_path: Path):
    _, catalog = parts
    _project_agent(tmp_path, DEFAULT_AGENT, "Impostor.")
    agents = await catalog.agents(OWNER, project_root=tmp_path)
    assert agents[DEFAULT_AGENT] == builtin_roster()[DEFAULT_AGENT]


async def test_resolve_finds_a_published_agent(parts: Parts):
    store, catalog = parts
    _ = await store.create(OWNER, agent_values("live"), published=True)
    assert (await catalog.resolve(OWNER, "live")).name == "live"


async def test_resolve_falls_back_to_the_default(parts: Parts):
    store, catalog = parts
    _ = await store.create(OWNER, agent_values("wip"))
    for name in (None, "nobody", "wip"):
        assert (await catalog.resolve(OWNER, name)).name == DEFAULT_AGENT


class TestLowerOf:
    def test_the_smaller_wins(self):
        assert lower_of(60, 10) == 10
        assert lower_of(5, 10) == 5

    def test_none_defers_to_the_other_side(self):
        assert lower_of(None, 10) == 10
        assert lower_of(60, None) == 60
        assert lower_of(None, None) is None
