"""Agents and skills a published plugin contributes."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from core.container import ServiceContainer
from core.exceptions import PluginValidationError
from routes.deps import app_state
from runs import Run, RunStream
from services.agents import AgentCatalog
from services.home.scratch import ScratchDirs
from services.plugins import PluginStore, validate_files
from services.skills import SkillStore
from tools.deps import RunDeps
from tools.skills import skill_catalog

from ._agent_helpers import make_stores
from ._helpers import client_app
from ._json import arr, json_body, obj
from ._plugin_helpers import OWNER, make_store
from ._plugin_helpers import make_stores as make_plugin_stores
from .test_skills_tools import FakeSessions, skill_tool

_PATHS = TypeAdapter(list[str])

MANIFEST = "name: helpers\ndescription: Helpers for release work.\n"

AGENT = """---
name: changelog-writer
description: Writes changelog entries.
---

Write the entry.
"""

SKILL = """---
name: release-checklist
description: The steps before tagging a release.
---

Run the tests, then tag.
"""


def _files(**extra: bytes) -> dict[str, bytes]:
    return {
        "plugin.yaml": MANIFEST.encode(),
        "agents/changelog-writer.md": AGENT.encode(),
        "skills/release-checklist/SKILL.md": SKILL.encode(),
        "skills/release-checklist/scripts/check.sh": b"echo ok\n",
        **extra,
    }


async def _published(store: PluginStore, files: dict[str, bytes]) -> None:
    view = await store.write_draft(OWNER, "helpers", files, source="agent")
    _ = await store.publish(OWNER, view.id)


def test_a_plugin_may_contribute_only_agents_and_skills():
    manifest = validate_files(_files())
    assert manifest.tools == ()
    [agent] = manifest.agents
    assert (agent.name, agent.source, agent.published) == ("changelog-writer", "plugin", True)
    [skill] = manifest.skills
    assert skill.skill.name == "release-checklist"
    assert skill.files == (("scripts/check.sh", b"echo ok\n"),)


def test_a_plugin_contributing_nothing_is_refused():
    with pytest.raises(PluginValidationError):
        _ = validate_files({"plugin.yaml": MANIFEST.encode()})


def test_a_broken_agent_file_names_its_path():
    with pytest.raises(PluginValidationError) as raised:
        _ = validate_files(_files(**{"agents/bad.md": b"---\nname: [\n---\n"}))
    assert raised.value.field == "agents"


async def test_only_a_published_plugins_agents_join_the_catalog(tmp_path: Path):
    plugins = await make_store(tmp_path / "plugins")
    agents, _conversations = await make_stores(tmp_path / "agents")
    catalog = AgentCatalog(agents)
    catalog.contribute(plugins.agents)

    _ = await plugins.write_draft(OWNER, "helpers", _files(), source="agent")
    assert "changelog-writer" not in await catalog.agents(OWNER)

    view = await plugins.get_by_name(OWNER, "helpers")
    assert view is not None
    _ = await plugins.publish(OWNER, view.id)
    assert (await catalog.agents(OWNER))["changelog-writer"].source == "plugin"


async def test_a_plugin_agent_never_shadows_a_built_in(tmp_path: Path):
    plugins = await make_store(tmp_path / "plugins")
    agents, _conversations = await make_stores(tmp_path / "agents")
    catalog = AgentCatalog(agents)
    catalog.contribute(plugins.agents)
    researcher = AGENT.replace("changelog-writer", "researcher")
    await _published(plugins, _files(**{"agents/changelog-writer.md": researcher.encode()}))

    assert (await catalog.agents(OWNER))["researcher"].source == "builtin"


def _deps(
    plugins: PluginStore, skills: SkillStore | None = None, sessions: FakeSessions | None = None
) -> RunDeps:
    caps = ServiceContainer()
    caps.add(plugins)
    if skills is not None:
        caps.add(skills)
    if sessions is not None:
        caps.add(sessions, as_type=ScratchDirs)
    run = Run(id="run-1", kind="chat", owner_id=OWNER, stream=RunStream())
    return RunDeps(run=run, owner_id=OWNER, conversation_id="conv-1", caps=caps)


def _ctx(deps: RunDeps) -> RunContext[RunDeps]:
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


async def test_a_plugin_skill_opens_and_stages_its_files(tmp_path: Path):
    plugins, skills = await make_plugin_stores(tmp_path)
    await _published(plugins, _files())
    sessions = FakeSessions()

    result = cast(
        dict[str, object],
        await skill_tool("open")(_ctx(_deps(plugins, skills, sessions)), "release-checklist"),
    )

    assert result["ok"] is True
    assert "Run the tests" in str(result["instructions"])
    assert _PATHS.validate_python(result["files"])[-1] == (
        "/work/skills/release-checklist/scripts/check.sh"
    )


async def test_a_stored_skill_shadows_a_plugin_skill_of_the_same_name(tmp_path: Path):
    plugins, skills = await make_plugin_stores(tmp_path)
    await _published(plugins, _files())
    stored = await skills.create(
        OWNER, name="release-checklist", description="Mine.", body="My steps."
    )
    _ = await skills.set_published(OWNER, stored.id, True)

    entries = await skill_catalog(_ctx(_deps(plugins, skills)))
    assert [(entry.name, entry.description) for entry in entries] == [
        ("release-checklist", "Mine.")
    ]


ROUTINE = """---
title: Weekly release check
schedule: cron 0 9 * * 1
agent: changelog-writer
---

Check what shipped this week and draft the changelog.
"""


def test_a_plugin_offers_routine_templates():
    manifest = validate_files(
        {"plugin.yaml": MANIFEST.encode(), "routines/weekly.md": ROUTINE.encode()}
    )
    [template] = manifest.routines
    assert (template.name, template.schedule, template.agent) == (
        "weekly",
        "cron 0 9 * * 1",
        "changelog-writer",
    )
    assert template.prompt.startswith("Check what shipped")


@pytest.mark.parametrize(
    "broken",
    [
        ROUTINE.replace("cron 0 9 * * 1", "every monday"),
        ROUTINE.replace("title: Weekly release check\n", ""),
        ROUTINE.split("---\n\n")[0] + "---\n",
    ],
)
def test_a_broken_routine_template_names_its_path(broken: str):
    with pytest.raises(PluginValidationError) as raised:
        _ = validate_files(
            {"plugin.yaml": MANIFEST.encode(), "routines/weekly.md": broken.encode()}
        )
    assert raised.value.field == "routines"
    assert "routines/weekly.md" in str(raised.value)


async def test_a_published_plugins_templates_are_listed_for_the_operator():
    async with client_app() as (client, app):
        plugins = app_state(app).plugins
        await _published(plugins, _files(**{"routines/weekly.md": ROUTINE.encode()}))
        [item] = arr(json_body(await client.get("/tasks/templates"))["items"])
        template = obj(item)
        assert (template["plugin"], template["title"]) == ("helpers", "Weekly release check")
        assert obj(template["schedule"])["cron"] == "0 9 * * 1"
        assert template["agentName"] == "changelog-writer"
