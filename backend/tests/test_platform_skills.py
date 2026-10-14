"""Platform and project skills: read from disk, merged into one catalog with the stored
ones, and switched off by the operator."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.db import init_db, make_engine
from core.exceptions import NotFoundError
from services.settings_store import SettingsStore
from services.skills.platform import PlatformSkills
from services.skills.sources import platform_skills, project_skills, read_skill_dir
from services.skills.store import SkillCatalogEntry
from tools import skills as skill_tools
from tools.deps import RunDeps
from tools.skills import _skill_catalog_block, skill_catalog

from ._helpers import client_app
from ._json import json_list, obj, s
from .test_skills_tools import (
    OWNER,
    FakeSession,
    FakeSessions,
    _deps,
    _published,
    _store,
    _tool,
)


def _ctx(deps: RunDeps) -> RunContext[RunDeps]:
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


_PATHS = TypeAdapter(list[str])


async def _open(deps: RunDeps, name: str) -> dict[str, object]:
    return cast(dict[str, object], await _tool("open")(_ctx(deps), name))


def _by_name(rows: list[JsonValue]) -> dict[str, dict[str, JsonValue]]:
    return {s(obj(row)["name"]): obj(row) for row in rows}


def _write_skill(root: Path, name: str, description: str = "Does a thing.") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    _ = (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nThe {name} steps.\n"
    )
    return directory


def _platform(tmp_path: Path, *names: str, written: bool = False) -> PlatformSkills:
    if not written:
        for name in names:
            _ = _write_skill(tmp_path / "platform", name)
    skills = tuple(read_skill_dir(tmp_path / "platform" / name) for name in names)
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return PlatformSkills(SettingsStore(engine), lambda: skills)


def test_the_shipped_skills_all_parse():
    names = {entry.skill.name for entry in platform_skills()}
    assert {"research", "delegation", "build-a-plugin", "build-an-agent"} <= names


def test_a_project_declares_skills_under_its_asset_roots(tmp_path: Path):
    scripts = _write_skill(tmp_path / ".claude" / "skills", "deploy") / "scripts"
    scripts.mkdir()
    _ = (scripts / "go.sh").write_text("echo go\n")
    _ = _write_skill(tmp_path / ".claude" / "skills", "broken").joinpath("SKILL.md").write_text("x")
    found = project_skills(tmp_path)
    assert [entry.skill.name for entry in found] == ["deploy"]
    assert found[0].files == (("scripts/go.sh", b"echo go\n"),)


def test_a_project_skill_cannot_stage_files_from_outside_its_tree(tmp_path: Path):
    secret = tmp_path / "secret.txt"
    _ = secret.write_text("key")
    directory = _write_skill(tmp_path / ".claude" / "skills", "deploy")
    (directory / "leak.txt").symlink_to(secret)
    assert project_skills(tmp_path)[0].files == ()


async def test_switching_a_platform_skill_off_and_on(tmp_path: Path):
    platform = _platform(tmp_path, "research", "delegation")
    await platform.set_enabled(OWNER, "research", False)
    assert [e.skill.name for e in await platform.enabled(OWNER)] == ["delegation"]
    await platform.set_enabled(OWNER, "research", True)
    assert len(await platform.enabled(OWNER)) == 2
    with pytest.raises(NotFoundError):
        await platform.set_enabled(OWNER, "nope", False)


async def test_open_reads_a_platform_skill_and_stages_its_files(tmp_path: Path):
    _ = _write_skill(tmp_path / "platform", "research").joinpath("notes.md").write_text("notes")
    platform = _platform(tmp_path, "research", written=True)
    session = FakeSession()
    deps = _deps(sandbox_sessions=FakeSessions(session))
    deps.caps.add(platform)
    result = await _open(deps, "research")
    assert str(result["instructions"]).strip() == "The research steps."
    assert any(
        path.endswith("skills/research/notes.md")
        for path in _PATHS.validate_python(result["files"])
    )


async def test_a_stored_skill_shadows_a_platform_skill_of_the_same_name(tmp_path: Path):
    store = await _store()
    _ = await _published(store, name="research", body="My own way.")
    deps = _deps(store, sandbox_sessions=FakeSessions())
    deps.caps.add(_platform(tmp_path, "research"))
    entries = await skill_catalog(_ctx(deps))
    assert [(e.name, e.source) for e in entries] == [("research", "authored")]
    result = await _open(deps, "research")
    assert result["instructions"] == "My own way."


async def test_a_switched_off_platform_skill_cannot_be_opened(tmp_path: Path):
    platform = _platform(tmp_path, "research")
    await platform.set_enabled(OWNER, "research", False)
    deps = _deps()
    deps.caps.add(platform)
    assert await skill_catalog(_ctx(deps)) == []
    with pytest.raises(ModelRetry):
        _ = await _open(deps, "research")


async def test_project_skills_rank_above_the_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = _write_skill(tmp_path / "repo" / ".claude" / "skills", "research").parents[2]

    async def tree(_ctx: RunContext[RunDeps]) -> Path:
        return project

    monkeypatch.setattr(skill_tools, "project_tree", tree)
    deps = _deps()
    deps.run.id = "run-project-skills"
    deps.caps.add(_platform(tmp_path, "research", "delegation"))
    entries = {e.name: e.source for e in await skill_catalog(_ctx(deps))}
    assert entries == {"research": "project", "delegation": "platform"}


def test_the_brief_lists_platform_skills_and_counts_the_rest():
    block = _skill_catalog_block(
        [
            SkillCatalogEntry("triage", "Triage a bug."),
            SkillCatalogEntry("research", "Investigate.", source="platform"),
            SkillCatalogEntry("delegation", "Delegate.", source="platform"),
        ]
    )
    assert "- delegation: Delegate.\n- research: Investigate." in block
    assert "1 saved skill," in block
    assert "triage" not in block


def test_the_brief_with_only_platform_skills_has_no_count():
    block = _skill_catalog_block([SkillCatalogEntry("research", "R.", source="platform")])
    assert "saved skill" not in block
    assert "skills_open" in block


async def test_the_platform_skill_routes():
    async with client_app() as (client, _app):
        listed = _by_name(json_list(await client.get("/skills/platform")))
        assert listed["research"]["enabled"] is True
        assert listed["research"]["shadowed"] is False
        assert (await client.post("/skills/platform/research/disable")).status_code == 204
        listed = _by_name(json_list(await client.get("/skills/platform")))
        assert listed["research"]["enabled"] is False
        assert (await client.post("/skills/platform/research/enable")).status_code == 204
        assert (await client.post("/skills/platform/nope/disable")).status_code == 404
