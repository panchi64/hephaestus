"""A sub-agent, launched as an ordinary turn.

A sub-agent is an ordinary `compose_turn` run: `launch` returns before it finishes, it never
exceeds its parent's permission, its thread is hidden but readable, its link is stored durably,
and isolation only ever increases.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter

from prompts.voice import CLASSES
from routes.chat import compose_turn
from routes.deps import app_state
from runs import Run
from services.agents import AgentDefinition, builtin_roster
from services.projects.repo import run_git_ok
from services.settings_store import set_subagent_limit
from services.subagent_store import SubagentStore
from services.subagents import (
    SubagentLauncher,
    SubagentParent,
    SubagentUnavailableError,
)
from services.subagents.callsigns import stem_of
from tests._helpers import client_app, patch_model_resolution

EXPLORER = builtin_roster()["explorer"]
WORKER = builtin_roster()["worker"]


def launcher_of(app: FastAPI) -> SubagentLauncher:
    launcher = app_state(app).capabilities.get_optional(SubagentLauncher)
    assert launcher is not None, "the subagents manifest did not export its implementation"
    return launcher


def records_of(app: FastAPI) -> SubagentStore:
    records = app_state(app).subagent_records
    assert records is not None
    return records


def _terminal_tasks(app: FastAPI) -> list[asyncio.Task[None]]:
    return list(app_state(app).run_terminal_tasks)


async def settle_run(app: FastAPI, run_id: str) -> None:
    """Let the run and its terminal hooks finish; the wake that settles the row outlives the run."""
    run = app_state(app).runs.get(run_id)
    assert run is not None
    if run.task is not None:
        await run.task
    # Looped: hooks are scheduled as the run settles, so one snapshot can miss them.
    for _ in range(5):
        pending = _terminal_tasks(app)
        if not pending:
            await asyncio.sleep(0)
            if not _terminal_tasks(app):
                return
            continue
        _ = await asyncio.gather(*pending, return_exceptions=True)


class TestLaunching:
    async def test_returns_before_the_subagent_finishes(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="what I found")

            started = await launcher_of(app).launch(
                "operator", EXPLORER, "find the parser", handle="parser-finder"
            )

            assert app_state(app).runs.get(started.run_id) is not None
            await settle_run(app, started.run_id)

    async def test_the_task_is_what_names_the_thread(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await launcher_of(app).launch(
                "operator", EXPLORER, "find the parser", handle="parser-finder"
            )
            await settle_run(app, started.run_id)

            summary = await app_state(app).conversations.get_summary(
                started.conversation_id, "operator"
            )
            assert summary is not None
            assert "find the parser" in (summary.title or "")

    async def test_the_thread_is_hidden_but_readable(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await launcher_of(app).launch(
                "operator", EXPLORER, "read the config", handle="config-reader"
            )
            await settle_run(app, started.run_id)

            listed = await app_state(app).conversations.list_conversations("operator")
            assert started.conversation_id not in {c.id for c in listed}
            turns = await app_state(app).conversations.messages_view(started.conversation_id)
            assert [t.role for t in turns] == ["user", "assistant"]

    async def test_an_empty_task_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            with pytest.raises(SubagentUnavailableError):
                _ = await launcher_of(app).launch(
                    "operator", EXPLORER, "   ", handle="nothing-doer"
                )


class TestWhatItMayDo:
    async def test_it_never_reaches_further_than_its_parent(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            # The worker's own ceiling is the mode default, above the parent's Manual.
            started = await launcher_of(app).launch(
                "operator",
                WORKER,
                "rename the thing",
                handle="renamer",
                parent=SubagentParent(permission="manual"),
            )
            await settle_run(app, started.run_id)

            binding = await app_state(app).conversations.binding(started.conversation_id)
            assert binding.permission == "manual"

    async def test_an_agent_that_may_not_delegate_is_not_offered_spawning(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            composed = _capture_composition(monkeypatch)

            started = await launcher_of(app).launch(
                "operator", EXPLORER, "look around", handle="looker"
            )
            await settle_run(app, started.run_id)

            assert not EXPLORER.can_delegate
            assert "agents_spawn" in TypeAdapter(frozenset[str]).validate_python(
                composed["disabled_tools"]
            )

    async def test_it_runs_in_the_lane_for_work_the_agent_opened_for_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            composed = _capture_composition(monkeypatch)

            started = await launcher_of(app).launch(
                "operator", EXPLORER, "look around", handle="looker"
            )
            await settle_run(app, started.run_id)

            assert composed["kind"] == "linked"
            # The inactivity watchdog cannot end an unattended run that keeps streaming.
            assert composed["wall_clock_timeout_s"]

    async def test_a_spec_needing_a_withheld_tool_refuses_rather_than_pretends(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            impossible = AgentDefinition(
                name="impossible",
                description="needs something that is not there",
                instructions="do the thing",
                required=frozenset({"agents_spawn"}),
            )
            with pytest.raises(SubagentUnavailableError, match="agents_spawn"):
                _ = await launcher_of(app).launch(
                    "operator", impossible, "go", handle="the-impossible"
                )


class TestItsHandle:
    """The launching model's name for a sub-agent, unique within its thread."""

    async def test_it_is_normalised_by_the_rule_agent_files_are_read_under(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await launcher_of(app).launch(
                "operator", EXPLORER, "find it", handle="Helper Function Finder"
            )
            await settle_run(app, started.run_id)

            assert started.handle == "helper-function-finder"

    async def test_a_handle_that_is_not_a_name_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            with pytest.raises(SubagentUnavailableError, match="not a usable agent name"):
                _ = await launcher_of(app).launch(
                    "operator", EXPLORER, "find it", handle="find it!!"
                )

    async def test_a_launch_naming_nothing_takes_its_specs_name(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await launcher_of(app).launch("operator", EXPLORER, "find it", handle="")
            await settle_run(app, started.run_id)

            assert started.handle == "explorer"

    async def test_a_clash_inside_a_thread_is_suffixed_rather_than_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent = SubagentParent(conversation_id="parent-handles")
            first = await launcher_of(app).launch(
                "operator", EXPLORER, "look here", handle="looker", parent=parent
            )
            second = await launcher_of(app).launch(
                "operator", EXPLORER, "look there", handle="looker", parent=parent
            )
            third = await launcher_of(app).launch(
                "operator", EXPLORER, "look everywhere", handle="looker", parent=parent
            )

            assert [first.handle, second.handle, third.handle] == [
                "looker",
                "looker-2",
                "looker-3",
            ]
            for one in (first, second, third):
                await settle_run(app, one.run_id)

    async def test_a_handle_taken_by_a_finished_sub_agent_is_still_taken(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent = SubagentParent(conversation_id="parent-handles-2")
            first = await launcher_of(app).launch(
                "operator", EXPLORER, "look here", handle="looker", parent=parent
            )
            await settle_run(app, first.run_id)

            second = await launcher_of(app).launch(
                "operator", EXPLORER, "look there", handle="looker", parent=parent
            )
            await settle_run(app, second.run_id)

            assert second.handle == "looker-2"

    async def test_launches_in_the_same_step_do_not_take_the_same_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The row is written after the turn is composed, so parallel launches must not collide."""
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent = SubagentParent(conversation_id="parent-handles-3")
            started = await asyncio.gather(
                *(
                    launcher_of(app).launch(
                        "operator",
                        EXPLORER,
                        f"look at {n}",
                        handle="looker",
                        parent=parent,
                    )
                    for n in range(3)
                )
            )

            assert sorted(one.handle for one in started) == [
                "looker",
                "looker-2",
                "looker-3",
            ]
            for one in started:
                await settle_run(app, one.run_id)

    async def test_two_threads_may_each_have_a_looker(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            mine = await launcher_of(app).launch(
                "operator",
                EXPLORER,
                "look here",
                handle="looker",
                parent=SubagentParent(conversation_id="thread-a"),
            )
            theirs = await launcher_of(app).launch(
                "operator",
                EXPLORER,
                "look there",
                handle="looker",
                parent=SubagentParent(conversation_id="thread-b"),
            )

            assert mine.handle == theirs.handle == "looker"
            for one in (mine, theirs):
                await settle_run(app, one.run_id)

    async def test_the_handle_is_what_names_the_thread(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await launcher_of(app).launch(
                "operator", EXPLORER, "find the parser", handle="parser-finder"
            )
            await settle_run(app, started.run_id)

            summary = await app_state(app).conversations.get_summary(
                started.conversation_id, "operator"
            )
            assert summary is not None
            callsign = started.callsign
            assert stem_of(callsign) in CLASSES["kourai"].callsigns
            assert (summary.title or "").startswith(f"{callsign} · parser-finder:")


class TestTheRegister:
    async def test_a_launch_is_recorded_so_the_transcript_stays_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="what I found")
            started = await launcher_of(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=SubagentParent(conversation_id="parent-1"),
            )
            await settle_run(app, started.run_id)

            rows = await records_of(app).descendants("parent-1", "operator")
            assert [row.child_conversation_id for row in rows] == [started.conversation_id]
            assert rows[0].spec_name == "explorer"

    async def test_a_stranded_row_is_reconciled_rather_than_left_running(self):
        async with client_app() as (_client, app):
            records = records_of(app)
            # Recorded directly: a real launch would race the hook that settles the row.
            subagent_id = f"stranded-{uuid4().hex}"
            await records.record(
                subagent_id=subagent_id,
                owner_id="operator",
                parent_conversation_id="parent-1",
                root_conversation_id="parent-1",
                depth=1,
                child_conversation_id="child-1",
                run_id="gone-with-the-process",
                parent_run_id=None,
                spec_name="explorer",
                handle="the-stranded-one",
                task="look",
                workspace_policy="shared",
                workspace_key="parent-1",
                delegation_id=None,
            )

            _ = await records.reconcile_stranded()

            # The returned count is not asserted: the pass is global.
            row = await records.get(subagent_id, "operator")
            assert row is not None
            assert row.status == "cancelled"
            assert row.ended_at is not None


async def _record_child(records: SubagentStore, parent: str, child: str) -> str:
    subagent_id = f"child-{uuid4().hex}"
    await records.record(
        subagent_id=subagent_id,
        owner_id="operator",
        parent_conversation_id=parent,
        root_conversation_id=parent,
        depth=1,
        child_conversation_id=child,
        run_id=f"run-{uuid4().hex}",
        parent_run_id=None,
        spec_name="explorer",
        handle=f"h-{uuid4().hex[:8]}",
        task="look",
        workspace_policy="shared",
        workspace_key=parent,
        delegation_id=None,
    )
    return subagent_id


class TestAwaitingReports:
    """What holds a thread's open-tasks notice: a turn under way in an agent it spawned."""

    def _while_running(self, app: FastAPI, conversation_id: str) -> tuple[Run, asyncio.Event]:
        release = asyncio.Event()

        async def hold(_run: Run) -> None:
            _ = await release.wait()

        # Not a chat kind, so the wake hook leaves it alone when it ends.
        run = app_state(app).runs.submit(
            kind="probe", owner_id="operator", orchestrator=hold, conversation_id=conversation_id
        )
        return run, release

    async def test_a_child_is_awaited_only_while_its_turn_runs(self):
        async with client_app() as (_client, app):
            launcher = launcher_of(app)
            parent, child = f"p-{uuid4().hex}", f"c-{uuid4().hex}"
            _ = await _record_child(records_of(app), parent, child)
            # The row reads `running` with no run behind it, as one whose settle failed does.
            assert not await launcher.awaiting_reports("operator", parent)

            run, release = self._while_running(app, child)
            assert await launcher.awaiting_reports("operator", parent)
            release.set()
            await run.wait()
            assert not await launcher.awaiting_reports("operator", parent)

    async def test_a_child_that_reported_blocked_is_waiting_on_its_parent(self):
        async with client_app() as (_client, app):
            records = records_of(app)
            parent = f"p-{uuid4().hex}"
            row = await _record_child(records, parent, f"c-{uuid4().hex}")
            await records.settle(row, status="blocked", summary="which branch?")
            assert not await launcher_of(app).awaiting_reports("operator", parent)

    async def test_a_grandchild_reports_to_its_own_parent_alone(self):
        async with client_app() as (_client, app):
            launcher, records = launcher_of(app), records_of(app)
            parent, child, grandchild = (f"{n}-{uuid4().hex}" for n in ("p", "c", "g"))
            _ = await _record_child(records, parent, child)
            _ = await _record_child(records, child, grandchild)

            run, release = self._while_running(app, grandchild)
            assert not await launcher.awaiting_reports("operator", parent)
            assert await launcher.awaiting_reports("operator", child)
            release.set()
            await run.wait()


class TestTheCap:
    async def test_eight_run_at_once_until_the_operator_sets_a_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent = f"p-{uuid4().hex}"
            # The startup pass closes run-less live rows, so let it finish before recording these.
            startup = [t for t in asyncio.all_tasks() if t.get_name() == "subagent-reconcile"]
            _ = await asyncio.gather(*startup)
            for _ in range(8):
                _ = await _record_child(records_of(app), parent, f"c-{uuid4().hex}")

            with pytest.raises(SubagentUnavailableError) as refused:
                _ = await launcher_of(app).launch(
                    "operator",
                    EXPLORER,
                    "look further",
                    handle="one-more",
                    parent=SubagentParent(conversation_id=parent),
                )

            assert "8 agents are already working" in str(refused.value)
            assert "Wait for a running agent's report" in str(refused.value)
            assert "do not retry" in str(refused.value)

    async def test_the_default_cap_leaves_room_to_split_work(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = [
                await launcher_of(app).launch(
                    "operator", EXPLORER, f"look at {n}", handle=f"looker-at-{n}"
                )
                for n in range(4)
            ]

            assert len({s.subagent_id for s in started}) == 4
            for one in started:
                await settle_run(app, one.run_id)

    async def test_a_launch_over_the_cap_is_refused_with_something_to_do_instead(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            _ = await set_subagent_limit(app_state(app).settings_store, "operator", 1)
            first = await launcher_of(app).launch(
                "operator", EXPLORER, "look here", handle="here-looker"
            )

            with pytest.raises(SubagentUnavailableError) as refused:
                _ = await launcher_of(app).launch(
                    "operator", EXPLORER, "look there", handle="there-looker"
                )

            assert "finishes" in str(refused.value)
            await settle_run(app, first.run_id)

    async def test_a_finished_subagent_gives_its_slot_back(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            _ = await set_subagent_limit(app_state(app).settings_store, "operator", 1)
            first = await launcher_of(app).launch(
                "operator", EXPLORER, "look here", handle="here-looker"
            )
            await settle_run(app, first.run_id)

            second = await launcher_of(app).launch(
                "operator", EXPLORER, "look there", handle="there-looker"
            )
            await settle_run(app, second.run_id)

    async def test_nothing_is_created_for_a_launch_that_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            _ = await set_subagent_limit(app_state(app).settings_store, "operator", 1)
            first = await launcher_of(app).launch(
                "operator",
                EXPLORER,
                "look here",
                handle="here-looker",
                parent=SubagentParent(conversation_id="parent-cap"),
            )

            with pytest.raises(SubagentUnavailableError):
                _ = await launcher_of(app).launch(
                    "operator",
                    EXPLORER,
                    "look there",
                    handle="there-looker",
                    parent=SubagentParent(conversation_id="parent-cap"),
                )

            rows = await records_of(app).descendants("parent-cap", "operator")
            assert len(rows) == 1
            await settle_run(app, first.run_id)


class TestIsolation:
    async def test_sharing_puts_the_subagent_in_the_parents_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await launcher_of(app).launch(
                "operator",
                EXPLORER,
                "read it",
                handle="reader",
                parent=SubagentParent(conversation_id="parent-1", workspace_key="parent-1"),
            )
            await settle_run(app, started.run_id)

            row = await records_of(app).get(started.subagent_id, "operator")
            assert row is not None
            assert row.workspace_key == "parent-1"
            assert row.delegation_id is None

    async def test_a_worktree_needs_a_project_to_cut_it_from(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            with pytest.raises(SubagentUnavailableError, match="not filed under a project"):
                _ = await launcher_of(app).launch(
                    "operator",
                    WORKER,
                    "rewrite it",
                    handle="rewriter",
                    parent=SubagentParent(conversation_id="parent-1", workspace_key="parent-1"),
                    worktree=True,
                )

    async def test_a_worktree_is_cut_from_the_checkout_and_left_for_the_parent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        root = await _git_repo(tmp_path)
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent, project_id = await _filed_parent(app, root)
            started = await launcher_of(app).launch(
                "operator",
                WORKER,
                "rewrite it",
                handle="rewriter",
                parent=SubagentParent(
                    conversation_id=parent, project_id=project_id, workspace_key=parent
                ),
                worktree=True,
            )
            await settle_run(app, started.run_id)

            row = await records_of(app).get(started.subagent_id, "operator")
            assert row is not None
            assert row.workspace_policy == "isolated"
            assert row.workspace_key == f"{parent}/{row.delegation_id}"
            assert row.base_ref
            opened = await launcher_of(app).worktrees("operator", parent)
            assert [child.handle for child in opened] == ["rewriter"]
            assert (opened[0].path / "hello.txt").read_text() == "unsaved\n"
            report = (await launcher_of(app).read("operator", parent, "rewriter")).summary
            assert report is not None
            assert opened[0].branch in report
            assert "agents_merge" in report
            assert (root / "hello.txt").read_text() == "unsaved\n"

    async def test_a_definition_that_works_apart_gets_a_worktree_unasked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        root = await _git_repo(tmp_path)
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent, project_id = await _filed_parent(app, root)
            apart = AgentDefinition(
                name="apart",
                description="works apart",
                instructions="do it elsewhere",
                workspace="isolated",
            )
            started = await launcher_of(app).launch(
                "operator",
                apart,
                "go",
                handle="the-apart-one",
                parent=SubagentParent(
                    conversation_id=parent, project_id=project_id, workspace_key=parent
                ),
            )
            await settle_run(app, started.run_id)

            row = await records_of(app).get(started.subagent_id, "operator")
            assert row is not None
            assert row.workspace_policy == "isolated"


async def _git_repo(tmp_path: Path) -> Path:
    """A repository with one commit, and an edit the operator has not committed."""
    root = tmp_path / "project"
    root.mkdir()
    _ = (root / "hello.txt").write_text("original\n")
    for argv in (
        ("init", "-b", "main"),
        ("add", "-A"),
        ("-c", "user.name=T", "-c", "user.email=t@e", "commit", "-m", "first"),
    ):
        _ = await run_git_ok(root, *argv)
    _ = (root / "hello.txt").write_text("unsaved\n")
    return root


async def _filed_parent(app: FastAPI, root: Path) -> tuple[str, str]:
    """A thread filed under a project at ``root``, and the project's id."""
    state = app_state(app)
    project = await state.projects.create("operator", "demo", str(root))
    parent = await state.conversations.create_conversation("operator", project_id=project.id)
    return parent, project.id


def _capture_composition(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """The arguments the sub-agent's turn was composed with; the real `compose_turn` still runs."""
    seen: dict[str, object] = {}
    monkeypatch.setattr("harness.turns.compose_turn", _recording(compose_turn, seen))
    return seen


def _recording[**P, R](real: Callable[P, R], seen: dict[str, object]) -> Callable[P, R]:
    def capture(*args: P.args, **kwargs: P.kwargs) -> R:
        seen.update(kwargs)
        return real(*args, **kwargs)

    return capture
