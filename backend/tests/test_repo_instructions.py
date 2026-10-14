"""The byte budget on a project's own instruction files.

What is pinned is the *order of sacrifice*: broad files go whole before the specific one
is cut at all, and a cut lands on a line boundary with a marker the model can read. A
budget that truncated whichever file it reached first would be arithmetically correct and
practically useless — it would take the sentences the agent is standing in front of and
keep the ones about the monorepo.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai_harness.repo_context._loader import ContextFile

from tools.repo import _MAX_BRIEFS, _briefs, _run_brief
from tools.repo_instructions import (
    INSTRUCTIONS_BYTE_BUDGET,
    repo_instruction_text,
    within_budget,
)


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
    assert 'path="CLAUDE.md"' in text  # labelled relative to the worktree root


async def test_an_oversized_file_cannot_swallow_the_prompt_head(tmp_path: Path):
    _ = (tmp_path / "CLAUDE.md").write_text("x" * (INSTRUCTIONS_BYTE_BUDGET * 3))

    text = await repo_instruction_text(tmp_path)

    # The whole rendering, framing included, stays within a small multiple of the budget:
    # what must not happen is the file arriving at its own size.
    assert len(text.encode("utf-8")) < INSTRUCTIONS_BYTE_BUDGET + 1_000


# --- the brief is built once per run, not once per model request ----------------------


async def test_a_runs_brief_is_read_from_disk_once_however_many_requests_it_makes(
    tmp_path: Path,
):
    """The provider re-resolves on every model request and a turn makes up to
    `agent_request_limit` of them — twenty-five walks of the worktree, twenty-five SHA256
    passes and twenty-five budget encodes, for content the module calls static per
    project. Worse, an agent that edited the project's own `CLAUDE.md` mid-turn would
    rewrite the prompt head under itself and invalidate the whole turn's prefix cache."""
    _ = (tmp_path / "CLAUDE.md").write_text("Run the tests with `uv run pytest`.\n")
    first = await _run_brief("run-1", tmp_path)

    _ = (tmp_path / "CLAUDE.md").write_text("Something else entirely.\n")

    assert await _run_brief("run-1", tmp_path) == first
    assert "uv run pytest" in first


async def test_the_next_turn_reads_the_file_again(tmp_path: Path):
    # A turn boundary is the one place the file may legitimately have changed since it
    # was read — the agent's own edit landed, or the operator's did.
    _ = (tmp_path / "CLAUDE.md").write_text("Run the tests with `uv run pytest`.\n")
    _ = await _run_brief("run-1", tmp_path)

    _ = (tmp_path / "CLAUDE.md").write_text("Run the tests with `just test`.\n")

    assert "just test" in await _run_brief("run-2", tmp_path)


async def test_the_memo_does_not_grow_with_every_run_the_process_ever_serves(tmp_path: Path):
    _ = (tmp_path / "CLAUDE.md").write_text("Keep it short.\n")
    for n in range(_MAX_BRIEFS * 3):
        _ = await _run_brief(f"run-{n}", tmp_path)
    assert len(_briefs) <= _MAX_BRIEFS


# --- what the brief costs, and what it points at --------------------------------------


def test_the_budget_is_sized_against_a_small_window_not_a_large_one():
    """The brief is re-sent on every model request of every turn, so what it costs is
    measured against the smallest window the app runs against, not the largest. 16KB is
    roughly 4k tokens — an eighth of a 32k window spent before the first message."""
    assert INSTRUCTIONS_BYTE_BUDGET == 16 * 1024


async def test_the_brief_names_the_inventory_tool_the_way_it_is_offered(tmp_path: Path):
    """The harness writes its own un-namespaced function name into the hint; the catalog
    offers the tool namespaced. A brief that points at `inventory_agent_context` sends the
    model to call a tool that is not there."""
    _ = (tmp_path / "CLAUDE.md").write_text("Run the tests with `uv run pytest`.\n")

    brief = await _run_brief("run-inventory", tmp_path)

    assert "repo_inventory_agent_context" in brief
    assert "`inventory_agent_context`" not in brief
