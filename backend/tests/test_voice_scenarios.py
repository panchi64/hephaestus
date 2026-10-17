"""The voice scenario file and its rubric stay consistent with each other and the doc."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evals.voice.grading import DIMENSIONS
from evals.voice.spec import FIXTURES_DIR, PROJECT_TOKEN, load_scenarios

GRADING = Path(__file__).parents[1] / "evals" / "voice" / "GRADING.md"


def test_the_file_loads_with_unique_ids() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 20
    assert len({s.id for s in scenarios}) == len(scenarios)


def test_every_voice_is_covered() -> None:
    assert {s.voice for s in load_scenarios()} == {"talos", "working", "subagent"}


def test_helper_is_implicit_and_report_belongs_to_subagents() -> None:
    for scenario in load_scenarios():
        assert "helper" not in scenario.dimensions, scenario.id
        assert ("report" in scenario.dimensions) == (scenario.voice == "subagent"), scenario.id


def test_fixtures_exist_and_every_fixture_is_used() -> None:
    used = {s.fixture for s in load_scenarios() if s.fixture}
    on_disk = {p.name for p in FIXTURES_DIR.iterdir() if p.is_dir()}
    assert used == on_disk


def test_a_fixture_scenario_names_its_project() -> None:
    for scenario in load_scenarios():
        if scenario.fixture:
            assert any(PROJECT_TOKEN in p for p in scenario.prompts), scenario.id


def test_grading_defines_every_dimension_once() -> None:
    headings = re.findall(r"^### `([a-z]+)`", GRADING.read_text(), re.M)
    assert tuple(headings) == DIMENSIONS


def test_expect_items_cite_the_voice_doc() -> None:
    for scenario in load_scenarios():
        for item in scenario.expect:
            assert re.search(r"§\d+", item), f"{scenario.id}: {item}"


def test_a_project_token_without_a_fixture_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    _ = path.write_text(
        '[[scenario]]\nid = "x"\nvoice = "talos"\nabout = "a"\n'
        'prompts = ["look at {project}"]\ndimensions = ["shape"]\nexpect = ["§5"]\n'
    )
    with pytest.raises(ValueError, match="no fixture"):
        _ = load_scenarios(path)
