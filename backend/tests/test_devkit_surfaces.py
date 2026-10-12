"""The discoverability contract.

A test rather than a convention, because the thing being protected is invisible from
inside the code: whether a session that has never seen this project can find the dev
instance and use it correctly. Nothing in the app breaks if these files stop being
written — the next session simply does not know the instance exists, discovers nothing,
and takes its visual claims on trust again.

The gitignore is what makes it fragile. ``.claude/`` and ``CLAUDE.md`` are both ignored on
purpose, so a hand-written copy dies with its worktree. These generate from the committed
recipe on every ``up``, and this is what keeps that true.
"""

from __future__ import annotations

import json

import pytest

from devkit import surfaces
from devkit.instance import DevInstance

CONFIG = "odysseus-dev"


@pytest.fixture
def instance(tmp_path):
    return DevInstance(name="a-worktree", slot=3, root=tmp_path / "state", password="pw")


def test_the_recipe_is_committed_and_holds_the_stanza():
    # The whole design rests on this file existing in git: it is the only copy that
    # survives a fresh clone, and everything a session reads is generated from it.
    assert surfaces.SOURCE.is_file()
    text = surfaces.SOURCE.read_text()
    stanza = surfaces.claude_md_stanza(text)
    assert stanza, "the CLAUDE.md markers are missing from docs/dev-instance.md"
    assert "dev_instance.py up" in stanza
    assert "status --json" in stanza, "the stanza must say not to assume the ports"


def test_the_launch_config_points_at_this_instances_frontend(instance, tmp_path):
    path = surfaces.write_launch_json(instance, CONFIG, tmp_path / ".claude")
    entry = json.loads(path.read_text())["configurations"][0]
    assert entry["name"] == CONFIG
    assert entry["port"] == instance.frontend_port
    assert entry["url"] == instance.frontend_url
    # No command: `up` owns starting the services, and a second launcher racing it would
    # fight over ports.
    assert "runtimeExecutable" not in entry


def test_regenerating_keeps_configurations_we_do_not_own(instance, tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "launch.json").write_text(
        json.dumps({"version": "0.0.1", "configurations": [{"name": "operators-own", "port": 1}]})
    )
    path = surfaces.write_launch_json(instance, CONFIG, claude)
    names = [entry["name"] for entry in json.loads(path.read_text())["configurations"]]
    assert set(names) == {CONFIG, "operators-own"}


def test_a_corrupt_launch_json_is_replaced_rather_than_fatal(instance, tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "launch.json").write_text("{ not json at all")
    path = surfaces.write_launch_json(instance, CONFIG, claude)
    assert json.loads(path.read_text())["configurations"][0]["name"] == CONFIG


def test_the_skill_carries_the_frontmatter_that_makes_it_findable(instance, tmp_path):
    path = surfaces.write_skill(instance, CONFIG, tmp_path / ".claude")
    text = path.read_text()
    assert path.parent.name == surfaces.SKILL_NAME
    assert text.startswith("---\n")
    assert f"name: {surfaces.SKILL_NAME}" in text
    # The description is what retrieval matches on, so it has to carry the words a
    # session would actually use rather than the words this package uses.
    description = text.split("---")[1]
    for phrase in ("run", "screenshot", "visually"):
        assert phrase in description.lower()


def test_the_skill_names_this_instances_own_port(instance, tmp_path):
    # Slot 3, not slot 0: a copy generated in one worktree must not send a session to
    # another worktree's instance.
    body = surfaces.write_skill(instance, CONFIG, tmp_path / ".claude").read_text()
    assert str(instance.stub_port) in body
    assert "{{" not in body, "an unsubstituted placeholder reached the generated file"


def test_the_claude_md_block_is_inserted_once_and_then_replaced_in_place(instance, tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# Notes\n\nSomething the operator wrote.\n")

    surfaces.write_claude_md(instance, CONFIG, path)
    first = path.read_text()
    assert "Something the operator wrote." in first
    assert first.count(surfaces._BEGIN) == 1

    surfaces.write_claude_md(instance, CONFIG, path)
    assert path.read_text() == first, "a second run must be a no-op, not an append"


def test_the_block_updates_when_the_instance_moves(instance, tmp_path):
    # The reason it is rewritten every run rather than only created: ports move between
    # worktrees, and a stale block would aim a session at the wrong instance.
    path = tmp_path / "CLAUDE.md"
    surfaces.write_claude_md(instance, CONFIG, path)
    surfaces.write_claude_md(
        DevInstance(name="other", slot=7, root=tmp_path, password="pw"), CONFIG, path
    )
    assert path.read_text().count(surfaces._BEGIN) == 1
    assert "a-worktree" not in path.read_text()


def test_the_operators_notes_survive_around_the_block(instance, tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text(f"# Before\n\n{surfaces._BEGIN}\nstale\n{surfaces._END}\n\n# After\n")
    surfaces.write_claude_md(instance, CONFIG, path)
    text = path.read_text()
    assert text.startswith("# Before")
    assert text.rstrip().endswith("# After")
    assert "stale" not in text


def test_a_missing_recipe_degrades_to_a_pointer(instance, tmp_path, monkeypatch):
    # `up` must not refuse to start an instance because a document was renamed, and a
    # session told where to look beats one told nothing.
    monkeypatch.setattr(surfaces, "SOURCE", tmp_path / "gone.md")
    body = surfaces.write_skill(instance, CONFIG, tmp_path / ".claude").read_text()
    assert "status --json" in body
    # With no recipe there is no stanza, so CLAUDE.md is left alone rather than emptied.
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Notes\n")
    surfaces.write_claude_md(instance, CONFIG, claude)
    assert claude.read_text() == "# Notes\n"


def test_write_all_produces_every_surface(instance, tmp_path):
    written = surfaces.write_all(instance, tmp_path / ".claude")
    assert {path.name for path in written} == {"launch.json", "SKILL.md", "CLAUDE.md"}
    assert all(path.is_file() for path in written)
