"""The discoverability contract.

A test rather than a convention, because the thing being protected is invisible from
inside the code: whether a session that has never seen this project can find the dev
instance and use it correctly. Nothing in the app breaks if these files stop being
written — the next session simply does not know the instance exists, discovers nothing,
and takes its visual claims on trust again.

The gitignore is what makes it fragile. ``.claude/`` is ignored on purpose, so a
hand-written copy dies with its worktree. These generate from the committed recipe on every
``up``, and this is what keeps that true.
"""

from __future__ import annotations

import json

import pytest

from devkit import surfaces
from devkit.instance import DevInstance

CONFIG = "hephaestus-dev"


@pytest.fixture
def instance(tmp_path):
    return DevInstance(name="a-worktree", slot=3, root=tmp_path / "state", password="pw")


def test_the_recipe_is_committed():
    # The whole design rests on this file existing in git: it is the only copy that
    # survives a fresh clone, and everything a session reads is generated from it.
    assert surfaces.SOURCE.is_file()
    text = surfaces.SOURCE.read_text()
    assert "dev_instance.py up" in text
    assert "status --json" in text, "the recipe must say not to assume the ports"


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


def test_a_missing_recipe_degrades_to_a_pointer(instance, tmp_path, monkeypatch):
    # `up` must not refuse to start an instance because a document was renamed, and a
    # session told where to look beats one told nothing.
    monkeypatch.setattr(surfaces, "SOURCE", tmp_path / "gone.md")
    body = surfaces.write_skill(instance, CONFIG, tmp_path / ".claude").read_text()
    assert "status --json" in body


def test_write_all_produces_every_surface(instance, tmp_path):
    written = surfaces.write_all(instance, tmp_path / ".claude")
    assert {path.name for path in written} == {"launch.json", "SKILL.md"}
    assert all(path.is_file() for path in written)
