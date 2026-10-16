"""A project's own instruction files: the byte budget on them, and handing them over the
first time a thread touches the project.

What the budget pins is the *order of sacrifice*: broad files go whole before the specific
one is cut at all, and a cut lands on a line boundary with a marker the model can read. A
budget that truncated whichever file it reached first would be arithmetically correct and
practically useless — it would take the sentences the agent is standing in front of and
keep the ones about the monorepo.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.repo_context._loader import ContextFile

from runs import Run, RunStream
from services.home.backend import Mount
from services.home.paths import SCRATCH, Place
from services.home.table import RunHome
from tools.briefing import BRIEF_KEY, with_brief, with_brief_payload
from tools.deps import RunDeps
from tools.repo_instructions import (
    INSTRUCTIONS_BYTE_BUDGET,
    repo_instruction_text,
    within_budget,
)

API = Place("projects", "api")


def _file(name: str, content: str) -> ContextFile:
    return ContextFile(directory=Path("/repo"), path=Path("/repo") / name, content=content)


def test_files_that_fit_are_all_kept_untouched():
    files = [_file("AGENTS.md", "a" * 100), _file("CLAUDE.md", "b" * 100)]
    assert within_budget(files, 1_000) == files


def test_the_broad_file_is_dropped_whole_before_the_specific_one_is_cut():
    broad = _file("AGENTS.md", "broad\n" * 200)
    specific = _file("CLAUDE.md", "specific\n" * 10)
    kept = within_budget([broad, specific], 200)
    # Discovery orders broadest-first, so the survivor is the last one — and it is intact.
    assert [f.path.name for f in kept] == ["CLAUDE.md"]
    assert kept[0].content == specific.content


def test_the_last_file_standing_is_cut_rather_than_dropped():
    only = _file("CLAUDE.md", "".join(f"rule {n}\n" for n in range(500)))
    kept = within_budget([only], 200)

    assert len(kept) == 1
    body = kept[0].content
    assert len(body.encode("utf-8")) <= 200
    # Cut at a line boundary, with a marker — a brief that stops mid-rule reads as a
    # corrupted instruction rather than a shortened one.
    assert "cut here" in body
    assert "rule 0\n" in body
    assert "rule 499" not in body


async def test_a_repo_with_no_instruction_file_contributes_nothing(tmp_path: Path):
    assert await repo_instruction_text(tmp_path) == ""


async def test_a_repos_own_file_is_rendered_whole_when_it_is_a_sane_size(tmp_path: Path):
    _ = (tmp_path / "CLAUDE.md").write_text("Run the tests with `uv run pytest`.\n")

    text = await repo_instruction_text(tmp_path)

    assert "uv run pytest" in text
    assert 'path="CLAUDE.md"' in text  # labelled relative to the project root


async def test_an_oversized_file_cannot_swallow_the_window(tmp_path: Path):
    _ = (tmp_path / "CLAUDE.md").write_text("x" * (INSTRUCTIONS_BYTE_BUDGET * 3))

    text = await repo_instruction_text(tmp_path)

    # The whole rendering, framing included, stays within a small multiple of the budget:
    # what must not happen is the file arriving at its own size.
    assert len(text.encode("utf-8")) < INSTRUCTIONS_BYTE_BUDGET + 1_000


def test_the_budget_is_sized_against_a_small_window_not_a_large_one():
    """The brief is re-sent with the history on every later request until a fold, so what
    it costs is measured against the smallest window the app runs against."""
    assert INSTRUCTIONS_BYTE_BUDGET == 16 * 1024


# --- handed over on the first touch ---------------------------------------------------


def _home(project: Path, scratch: Path) -> RunHome:
    return RunHome(
        mounts=(Mount(SCRATCH, scratch, writable=True), Mount(API, project, writable=True)),
        default=SCRATCH,
        key="conv-1",
    )


def _ctx(messages: list[ModelMessage] | None = None) -> RunContext[RunDeps]:
    run = Run(id="r1", kind="chat", owner_id="operator", stream=RunStream())
    return RunContext(
        deps=RunDeps(run=run, owner_id="operator", conversation_id="conv-1"),
        model=TestModel(),
        usage=RunUsage(),
        messages=messages or [],
    )


async def test_the_first_result_in_a_project_carries_its_instructions(tmp_path: Path):
    project = tmp_path / "api"
    project.mkdir()
    _ = (project / "CLAUDE.md").write_text("Run the tests with `make check`.\n")
    home = _home(project, tmp_path / "scratch")
    ctx = _ctx()

    first = await with_brief(ctx, home, [API], "file contents")
    assert isinstance(first, str) and "make check" in first and "~/projects/api" in first
    # Once a run, however many calls it makes there.
    assert await with_brief(ctx, home, [API], "again") == "again"
    # A scratch result carries nothing.
    assert await with_brief(_ctx(), home, [SCRATCH], "x") == "x"


async def test_a_brief_still_in_the_history_is_not_handed_over_again(tmp_path: Path):
    project = tmp_path / "api"
    project.mkdir()
    _ = (project / "CLAUDE.md").write_text("Run the tests with `make check`.\n")
    home = _home(project, tmp_path / "scratch")
    delivered = await with_brief_payload(_ctx(), home, [API], {"ok": True})
    brief = delivered[BRIEF_KEY]
    replayed: list[ModelMessage] = [
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="shell_run_command", content=delivered, tool_call_id="c")
            ]
        )
    ]

    # A later run of the same thread replays the brief, so it is not given twice; once a
    # fold has taken it out of the history, the next touch hands it over again.
    assert await with_brief_payload(_ctx(replayed), home, [API], {"ok": True}) == {"ok": True}
    again = await with_brief_payload(_ctx(), home, [API], {"ok": True})
    assert again[BRIEF_KEY] == brief
