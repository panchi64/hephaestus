"""The per-turn workspace block: what the model is told it already has on disk.

The bug it closes is not a bug in any one function. `code_execute` returns two streams
and `files_write_file` returns a byte count, so the only record that a file exists was
the tool call still sitting in the replayed history — and a compaction replaces that
stretch with a summary. Past the fold the agent's own files went invisible to it and it
rebuilt them. So the two things worth pinning here are that the block is derived from
disk rather than from anything remembered, and that asking for it costs nothing when
there is nothing to say.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

import tools.workspace_context as wc
from agent import build_chat_orchestrator
from core.config import Settings
from core.container import ServiceContainer
from core.vault import Vault
from runs import RunRegistry, RunStatus
from runs.events import ContextInjected
from services.conversations import ConversationBinding
from services.sandbox import SandboxSessionManager
from services.sandbox.base import safe_key
from services.sandbox.walk import walk_files
from services.workspace_kind import WorkspaceKind
from tools.deps import PromptContextRequest
from tools.workspace_context import _MAX_ENTRIES, workspace_context

from .conftest import egress_policy
from .vm_fakes import FakeVM


async def _vault(tmp_path: Path):
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return vault


async def _caps(
    tmp_path: Path, *, excludes: Iterable[str] | None = None
) -> tuple[ServiceContainer, SandboxSessionManager]:
    manager = SandboxSessionManager(
        FakeVM(tmp_path / "sandbox" / "work"),
        await _vault(tmp_path),
        egress=egress_policy(tmp_path),
        data_dir=tmp_path,
        idle_ttl_s=1800.0,
        reap_interval_s=60.0,
        excludes=Settings().sandbox_walk_excludes if excludes is None else excludes,
    )
    caps = ServiceContainer()
    caps.add(manager, as_type=SandboxSessionManager)
    return caps, manager


def _request(
    caps: ServiceContainer, *, kind: WorkspaceKind = "sandbox", key: str = "conv-a"
) -> PromptContextRequest:
    return PromptContextRequest(
        caps=caps,
        owner_id="operator",
        conversation_id="conv-a",
        workspace_kind=kind,
        project_id=None,
        workspace_key=key,
    )


def _populate(manager: SandboxSessionManager, key: str, files: dict[str, str]) -> Path:
    root = manager.work_root / safe_key(key)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text)
    return root


# --- the block itself --------------------------------------------------------
async def test_it_names_the_files_the_agent_already_wrote(tmp_path: Path):
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"analysis.py": "print('hi')", "out/chart.csv": "a,b\n1,2\n"})

    block = await workspace_context(_request(caps))

    assert "/work/analysis.py" in block
    assert "/work/out/chart.csv" in block
    assert "do not rebuild something that is already listed" in block


async def test_it_carries_each_file_s_size(tmp_path: Path):
    """A path alone cannot tell an empty stub from a finished file, and "rewrite it, it
    looked empty" is the same wasted turn this block exists to prevent."""
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"stub.py": "", "real.py": "x" * 2048})

    block = await workspace_context(_request(caps))

    assert "/work/stub.py  0B" in block
    assert "/work/real.py  2.0K" in block


async def test_it_prunes_the_bloat_the_rest_of_the_walk_prunes(tmp_path: Path):
    # One answer about what a workspace is, shared with the fork's manifest and the
    # history snapshot — a block advertising files the merge will not carry would be
    # describing a workspace nothing else agrees exists.
    caps, manager = await _caps(tmp_path)
    _ = _populate(
        manager,
        "conv-a",
        {"keep.py": "x", ".venv/lib/big.so": "y", "node_modules/left-pad/i.js": "z"},
    )

    block = await workspace_context(_request(caps))

    assert "/work/keep.py" in block
    assert "big.so" not in block
    assert "left-pad" not in block


async def test_it_only_claims_not_listed_for_what_is_actually_excluded(tmp_path: Path):
    """The exclusions are an operator setting, so the trailer cannot be a hardcoded
    list: a `dist/` they chose to make visible would be listed *and* announced as
    "not listed" in the same block."""
    # The operator wants build output visible, and takes `dist` off the list.
    caps, manager = await _caps(tmp_path, excludes=(".venv",))
    _ = _populate(manager, "conv-a", {"dist/bundle.js": "z", ".venv/lib/big.so": "y"})

    block = await workspace_context(_request(caps))

    assert "/work/dist/bundle.js" in block
    assert "dist/" not in block.split("Also present")[1]
    assert "Also present, not listed: .venv/." in block


async def test_it_names_the_expensive_directories_it_does_not_list(tmp_path: Path):
    """Pruning `.venv` and `dist` keeps thousands of files out of the block; saying
    nothing about them is the seal's mistake in a new place. Each is minutes of work the
    agent would otherwise do twice."""
    caps, manager = await _caps(tmp_path)
    _ = _populate(
        manager,
        "conv-a",
        {
            "src/analysis.py": "x",
            ".venv/lib/big.so": "y",
            "dist/bundle.js": "z",
            ".git/HEAD": "ref",
            "__pycache__/m.pyc": "junk",
        },
    )

    block = await workspace_context(_request(caps))

    assert "Also present, not listed: .venv/, .git/, dist/." in block
    assert "don't reinstall, rebuild or re-clone them" in block
    assert "__pycache__" not in block  # nothing the agent decides turns on a cache


async def test_a_workspace_that_is_only_pruned_directories_still_says_so(tmp_path: Path):
    # `uv sync` and nothing else yet: no file to list, and the single most useful thing
    # to say is that the virtualenv is already there.
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {".venv/lib/big.so": "y"})

    block = await workspace_context(_request(caps))

    assert "Also present, not listed: .venv/." in block


async def test_scratch_directories_are_named_nowhere(tmp_path: Path):
    # `.home` and `.tmp` back HOME and TMPDIR inside the box. They are ours, not the
    # agent's, and every turn that mentioned them would be a turn spent on our plumbing.
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"keep.py": "x", ".home/.cache/f": "y", ".tmp/scratch": "z"})

    block = await workspace_context(_request(caps))

    assert ".home" not in block
    assert ".tmp" not in block


async def test_it_reads_disk_rather_than_anything_remembered(tmp_path: Path):
    """The point of the whole thing: a file written by a turn the model can no longer
    see is still named, and one deleted since is not."""
    caps, manager = await _caps(tmp_path)
    root = _populate(manager, "conv-a", {"kept.py": "x", "gone.py": "y"})
    (root / "gone.py").unlink()
    _ = (root / "later.py").write_text("z")

    block = await workspace_context(_request(caps))

    assert "/work/kept.py" in block
    assert "/work/later.py" in block
    assert "gone.py" not in block


# --- and nothing at all, wherever a block would be noise ---------------------
async def test_no_workspace_means_no_block(tmp_path: Path):
    caps, _manager = await _caps(tmp_path)
    assert await workspace_context(_request(caps)) == ""


async def test_an_empty_workspace_means_no_block(tmp_path: Path):
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {})

    assert await workspace_context(_request(caps)) == ""


@pytest.mark.parametrize("kind", ["checkout", "worktree"])
async def test_a_project_folder_thread_gets_no_block(tmp_path: Path, kind: WorkspaceKind):
    """A project folder is the operator's own repository, with git in it and
    `repo_instructions` already describing it — listing thousands of tracked files every
    turn would be this idea's whole cost with none of its benefit."""
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"analysis.py": "x"})

    assert await workspace_context(_request(caps, kind=kind)) == ""


async def test_no_sandbox_capability_means_no_block():
    assert await workspace_context(_request(ServiceContainer())) == ""


# --- what it must never do ---------------------------------------------------
async def test_it_creates_neither_a_session_nor_a_directory(tmp_path: Path):
    """The single worst available mistake here. Resolving the workspace properly would
    mint a session and an empty directory on every turn — including the great majority
    that never touch a file — which is exactly the cost the lazy-container design is
    built to avoid."""
    caps, manager = await _caps(tmp_path)

    assert await workspace_context(_request(caps)) == ""

    assert manager.sessions == {}
    assert not manager.work_root.exists()


async def test_a_delegated_run_is_described_its_own_fork(tmp_path: Path):
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"parents-file.py": "x"})
    _ = _populate(manager, "conv-a/deleg-1", {"childs-file.py": "y"})

    block = await workspace_context(_request(caps, key="conv-a/deleg-1"))

    assert "/work/childs-file.py" in block
    assert "parents-file.py" not in block


# --- the cap ----------------------------------------------------------------
async def test_past_the_cap_it_counts_and_points_instead_of_listing(tmp_path: Path):
    caps, manager = await _caps(tmp_path)
    _ = _populate(
        manager,
        "conv-a",
        {f"generated/f{i:04d}.txt": "x" for i in range(_MAX_ENTRIES + 25)},
    )

    block = await workspace_context(_request(caps))

    assert len([ln for ln in block.splitlines() if ln.startswith("/work/")]) == _MAX_ENTRIES
    assert "… and 25 more (mostly under generated)" in block


# --- it never claims to be complete when it is not --------------------------
async def test_a_whole_workspace_is_described_as_whole(tmp_path: Path):
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"analysis.py": "x", "out/chart.csv": "y"})

    block = await workspace_context(_request(caps))

    assert "This is the whole of it" in block


async def test_a_capped_listing_is_not_described_as_whole(tmp_path: Path):
    """The claim of completeness is the block's whole value and its sharpest edge. A
    model told "this is everything", not finding `report.md` among the 200 shown,
    concludes it was never written and writes it again — the redo this exists to stop."""
    caps, manager = await _caps(tmp_path)
    _ = _populate(
        manager, "conv-a", {f"generated/f{i:04d}.txt": "x" for i in range(_MAX_ENTRIES + 5)}
    )

    block = await workspace_context(_request(caps))

    assert "This is the whole of it" not in block
    assert "This is a sample, not the whole of it" in block


async def test_pruned_directories_also_make_it_a_sample(tmp_path: Path):
    # Two files listed and a `.venv` beside them: the listing is short, but it is still
    # not everything on disk.
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"analysis.py": "x", ".venv/lib/big.so": "y"})

    block = await workspace_context(_request(caps))

    assert "This is the whole of it" not in block
    assert "This is a sample, not the whole of it" in block


# --- the walk is bounded, not just the listing -------------------------------
async def test_the_walk_stops_counting_rather_than_stat_ing_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_MAX_ENTRIES` bounds the tokens; `_MAX_SCAN` bounds the syscalls. Without the
    second, a workspace holding a downloaded dataset is stat'ed in full, every turn, to
    render two hundred lines."""
    monkeypatch.setattr(wc, "_MAX_SCAN", 10)
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {f"data/f{i:04d}.txt": "x" for i in range(400)})

    visited = 0
    real_walk = walk_files

    def counting_walk(root: Path, excludes: Iterable[str]) -> Iterator[tuple[str, Path]]:
        nonlocal visited
        for item in real_walk(root, excludes):
            visited += 1
            yield item

    monkeypatch.setattr(wc, "walk_files", counting_walk)
    block = await workspace_context(_request(caps))

    assert visited <= 11  # the scan cap, plus the one that trips it
    assert "at least" in block  # the count is a floor now, and says so
    assert "This is the whole of it" not in block


async def test_the_cap_keeps_what_was_touched_most_recently(tmp_path: Path):
    # If something has to go, the file the agent was last working in is the one it is
    # most likely to reach for next.
    caps, manager = await _caps(tmp_path)
    root = _populate(manager, "conv-a", {f"old/f{i:04d}.txt": "x" for i in range(_MAX_ENTRIES + 5)})
    fresh = root / "the-one-i-just-wrote.py"
    _ = fresh.write_text("y")
    os.utime(fresh, (2_000_000_000, 2_000_000_000))

    block = await workspace_context(_request(caps))

    assert "/work/the-one-i-just-wrote.py" in block


@pytest.mark.parametrize(
    ("size", "rendered"),
    [(0, "0B"), (512, "512B"), (1024, "1.0K"), (1536, "1.5K"), (1024**2, "1.0M")],
)
async def test_sizes_read_at_a_glance(tmp_path: Path, size: int, rendered: str):
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"f.bin": "x" * size})

    assert f"/work/f.bin  {rendered}" in await workspace_context(_request(caps))


# --- through a real turn -----------------------------------------------------
async def test_a_live_turn_carries_the_block_at_the_tail(tmp_path: Path):
    """The wiring, end to end: the prelude builds the request off the thread's binding
    and workspace key, this provider reads the directory, and the block lands at the
    tail of the turn the model is actually handed — announced, like every other
    injection, before the answer it shaped."""
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"analysis.py": "print('done')"})

    orch = build_chat_orchestrator(
        "carry on",
        model=TestModel(custom_output_text="ok", call_tools=[]),
        categories={},
        capabilities=caps,
        prompt_context_providers=[workspace_context],
        binding=ConversationBinding(workspace_kind="sandbox"),
        workspace_key="conv-a",
        context_window=100_000,
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()
    assert run.status is RunStatus.done

    events = run.stream.replay()
    injected = [e for e in events if isinstance(e.body, ContextInjected)]
    block = next(
        e.body
        for e in injected
        if isinstance(e.body, ContextInjected) and e.body.contributor == "workspace"
    )
    assert block.placement == "prompt"  # the tail, never the cacheable head
    assert "/work/analysis.py" in block.text
    first_answer = next(e.seq for e in events if e.body.type == "answer.delta")
    assert all(e.seq < first_answer for e in injected)


@pytest.mark.parametrize("kind", ["checkout", "worktree"])
async def test_a_live_project_folder_turn_carries_no_block(tmp_path: Path, kind: WorkspaceKind):
    caps, manager = await _caps(tmp_path)
    _ = _populate(manager, "conv-a", {"analysis.py": "x"})

    orch = build_chat_orchestrator(
        "carry on",
        model=TestModel(custom_output_text="ok", call_tools=[]),
        categories={},
        capabilities=caps,
        prompt_context_providers=[workspace_context],
        binding=ConversationBinding(workspace_kind=kind, project_id="proj-1"),
        workspace_key="conv-a",
        context_window=100_000,
    )
    run = RunRegistry().submit(kind="chat", owner_id="operator", orchestrator=orch)
    _ = await run.wait()
    assert run.status is RunStatus.done

    listings = [
        e
        for e in run.stream.replay()
        if e.body.type == "context.injected"
        and e.body.contributor == "workspace"
        and e.body.placement == "prompt"
    ]
    assert listings == []
