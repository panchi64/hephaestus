"""A sub-agent, launched as an ordinary turn.

Delegation used to run a bespoke `Agent(...)` loop of its own, inside the calling tool,
blocking the parent's turn for as long as it took. It is now a conversation composed by
the same `compose_turn` every other turn goes through. What that buys — and therefore what
these hold onto — is that the sub-agent is an *ordinary* run: the same engine, the same
persisted transcript, the same context gauge, the same approval machinery.

The things worth pinning are the ones that would quietly stop being true:

- `launch` **returns before the sub-agent finishes**, which is the whole point of the
  change and the one property the blocking version could not have;
- the sub-agent can never do **more than the thread that launched it**;
- an agent whose definition may not delegate is **not offered the spawn tool**;
- its thread is **hidden but real** — no one can type into it, and its transcript is a
  conversation the panel can open by id;
- it is **registered durably**, because a link held only in memory leaves the operator
  with unreachable transcripts after a restart;
- isolation only ever **increases**: the launch may ask a sharing sub-agent to step out of
  the way, and may never pull an isolated one into the operator's own files.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter

from core.container import ServiceContainer
from routes.chat import compose_turn
from runs import RunRegistry
from services.agents import AgentDefinition, builtin_roster
from services.conversations import ConversationStore
from services.settings_store import SettingsStore, set_subagent_limit
from services.subagent_store import SubagentStore
from services.subagents import (
    SubagentLauncher,
    SubagentParent,
    SubagentUnavailableError,
)
from tests._helpers import app_state, client_app, patch_model_resolution

EXPLORER = builtin_roster()["explorer"]
WORKER = builtin_roster()["worker"]


def _launcher(app: FastAPI) -> SubagentLauncher:
    launcher = app_state(app, "capabilities", ServiceContainer).get_optional(SubagentLauncher)
    assert launcher is not None, "the subagents manifest did not export its implementation"
    return launcher


def _records(app: FastAPI) -> SubagentStore:
    return app_state(app, "subagent_records", SubagentStore)


def _terminal_tasks(app: FastAPI) -> list[asyncio.Task[None]]:
    return list(cast(set[asyncio.Task[None]], app_state(app, "run_terminal_tasks", set)))


async def _settle(app: FastAPI, run_id: str) -> None:
    """Let the submitted run finish, *and* let the hooks that react to it finish too.

    A run's terminal hooks are background tasks, so awaiting the run alone leaves the wake
    — which settles the sub-agent's row — still in flight. A test that read the register
    there would be racing it.
    """
    run = app_state(app, "runs", RunRegistry).get(run_id)
    assert run is not None
    if run.task is not None:
        await run.task
    # Looped, not a single gather: the dispatcher is invoked as the run settles, so a
    # snapshot taken the instant the task returns can still be empty, and a hook that
    # schedules nothing on its first pass may on its second.
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

            started = await _launcher(app).launch(
                "operator", EXPLORER, "find the parser", handle="parser-finder"
            )

            # THE assertion. A tool that awaited this would spend its whole turn here,
            # which is exactly what the blocking delegation did.
            assert app_state(app, "runs", RunRegistry).get(started.run_id) is not None
            await _settle(app, started.run_id)

    async def test_the_task_is_what_names_the_thread(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _launcher(app).launch(
                "operator", EXPLORER, "find the parser", handle="parser-finder"
            )
            await _settle(app, started.run_id)

            summary = await app_state(app, "conversations", ConversationStore).get_summary(
                started.conversation_id, "operator"
            )
            # Nobody wrote this thread's opening message, so without the task in its name
            # a card reads "Untitled" and the operator cannot tell it from its neighbours.
            assert summary is not None
            assert "find the parser" in (summary.title or "")

    async def test_the_thread_is_hidden_but_readable(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _launcher(app).launch(
                "operator", EXPLORER, "read the config", handle="config-reader"
            )
            await _settle(app, started.run_id)

            listed = await app_state(app, "conversations", ConversationStore).list_conversations(
                "operator"
            )
            assert started.conversation_id not in {c.id for c in listed}
            # Hidden from the listing, and still a whole conversation — which is what the
            # panel opens when a card is expanded, and what keeps the transcript.
            turns = await app_state(app, "conversations", ConversationStore).messages_view(
                started.conversation_id
            )
            assert [t.role for t in turns] == ["user", "assistant"]

    async def test_an_empty_task_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            with pytest.raises(SubagentUnavailableError):
                _ = await _launcher(app).launch("operator", EXPLORER, "   ", handle="nothing-doer")


class TestWhatItMayDo:
    async def test_it_never_reaches_further_than_its_parent(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            # A worker's own ceiling is the mode's default; the parent is down at Manual.
            started = await _launcher(app).launch(
                "operator",
                WORKER,
                "rename the thing",
                handle="renamer",
                parent=SubagentParent(permission="manual"),
            )
            await _settle(app, started.run_id)

            binding = await app_state(app, "conversations", ConversationStore).binding(
                started.conversation_id
            )
            # Otherwise one approved launch buys a standing level the operator never chose.
            assert binding.permission == "manual"

    async def test_an_agent_that_may_not_delegate_is_not_offered_spawning(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            composed = _capture_composition(monkeypatch)

            started = await _launcher(app).launch(
                "operator", EXPLORER, "look around", handle="looker"
            )
            await _settle(app, started.run_id)

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

            started = await _launcher(app).launch(
                "operator", EXPLORER, "look around", handle="looker"
            )
            await _settle(app, started.run_id)

            # Not the interactive lane: a burst of sub-agents must never hold up the turn
            # the operator is sitting in front of (`runs/lanes.py`).
            assert composed["kind"] == "linked"
            # And a wall clock, because nobody is watching — the inactivity watchdog
            # cannot end a run that keeps streaming tokens.
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
            # It may not delegate, so `agents_spawn` is withheld from it — and an agent that
            # ran anyway would answer from model memory as though it had done the work.
            with pytest.raises(SubagentUnavailableError, match="agents_spawn"):
                _ = await _launcher(app).launch(
                    "operator", impossible, "go", handle="the-impossible"
                )


class TestItsHandle:
    """The name the *launching model* gives a sub-agent, and the only one it ever sees.

    The uuid it used to be handed had to survive four tool calls and tens of thousands of
    tokens of other work, with nothing about it to notice a transposed character in. A
    handle the model chose for the job is legible, and — because it is chosen at launch and
    made unique in the thread — it is also the thing a report, a card and a thread title can
    all be named by without any of them disagreeing.
    """

    async def test_it_is_normalised_by_the_rule_agent_files_are_read_under(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _launcher(app).launch(
                "operator", EXPLORER, "find it", handle="Helper Function Finder"
            )
            await _settle(app, started.run_id)

            # One rule, shared with `.claude/agents/*.md`'s own `name:`, because the two are
            # read in the same places by the same eyes. A model writing the words rather
            # than the identifier meant exactly one thing.
            assert started.handle == "helper-function-finder"

    async def test_a_handle_that_is_not_a_name_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            with pytest.raises(SubagentUnavailableError, match="not a usable agent name"):
                _ = await _launcher(app).launch("operator", EXPLORER, "find it", handle="find it!!")

    async def test_a_launch_naming_nothing_takes_its_specs_name(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _launcher(app).launch("operator", EXPLORER, "find it", handle="")
            await _settle(app, started.run_id)

            # The same fallback the migration backfilled every pre-handle row to, so there
            # is one answer to "what is this sub-agent called" rather than two.
            assert started.handle == "explorer"

    async def test_a_clash_inside_a_thread_is_suffixed_rather_than_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent = SubagentParent(conversation_id="parent-handles")
            first = await _launcher(app).launch(
                "operator", EXPLORER, "look here", handle="looker", parent=parent
            )
            second = await _launcher(app).launch(
                "operator", EXPLORER, "look there", handle="looker", parent=parent
            )
            third = await _launcher(app).launch(
                "operator", EXPLORER, "look everywhere", handle="looker", parent=parent
            )

            # A launch is the expensive thing the model just decided to do; failing it over
            # a name would spend a retry re-deciding, and `looker-2` is legible enough that
            # a report naming it is obviously the second one.
            assert [first.handle, second.handle, third.handle] == [
                "looker",
                "looker-2",
                "looker-3",
            ]
            for one in (first, second, third):
                await _settle(app, one.run_id)

    async def test_a_handle_taken_by_a_finished_sub_agent_is_still_taken(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent = SubagentParent(conversation_id="parent-handles-2")
            first = await _launcher(app).launch(
                "operator", EXPLORER, "look here", handle="looker", parent=parent
            )
            await _settle(app, first.run_id)

            second = await _launcher(app).launch(
                "operator", EXPLORER, "look there", handle="looker", parent=parent
            )
            await _settle(app, second.run_id)

            # Unique for the life of the *thread*, not of the run: a parent can read a
            # settled sub-agent's report back by name long afterwards, and re-using the
            # handle would silently redirect that read to different work.
            assert second.handle == "looker-2"

    async def test_launches_in_the_same_step_do_not_take_the_same_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The case the suffix exists for, launched the way it actually happens.

        A fan-out is parallel tool calls, and a launch does not write its row until a whole
        turn has been composed — so three launches reading the register would all be told
        "nothing taken" and all three would be called `looker`. Every direction after that
        lands on whichever row is newest, and the other two are unaddressable for good.
        """
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            parent = SubagentParent(conversation_id="parent-handles-3")
            started = await asyncio.gather(
                *(
                    _launcher(app).launch(
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
                await _settle(app, one.run_id)

    async def test_two_threads_may_each_have_a_looker(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            mine = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "look here",
                handle="looker",
                parent=SubagentParent(conversation_id="thread-a"),
            )
            theirs = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "look there",
                handle="looker",
                parent=SubagentParent(conversation_id="thread-b"),
            )

            # Unique *within* a thread and meaningless outside one — which is also why the
            # lookup takes the conversation, and why an agent cannot reach a sibling
            # thread's sub-agent by guessing a plausible name.
            assert mine.handle == theirs.handle == "looker"
            for one in (mine, theirs):
                await _settle(app, one.run_id)

    async def test_the_handle_is_what_names_the_thread(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _launcher(app).launch(
                "operator", EXPLORER, "find the parser", handle="parser-finder"
            )
            await _settle(app, started.run_id)

            summary = await app_state(app, "conversations", ConversationStore).get_summary(
                started.conversation_id, "operator"
            )
            assert summary is not None
            # Three `explorer`s produce three titles that differ only in whatever survives
            # the width; three handles differ in the first word.
            assert (summary.title or "").startswith("parser-finder:")


class TestTheRegister:
    async def test_a_launch_is_recorded_so_the_transcript_stays_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="what I found")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=SubagentParent(conversation_id="parent-1"),
            )
            await _settle(app, started.run_id)

            rows = await _records(app).descendants("parent-1", "operator")
            assert [row.child_conversation_id for row in rows] == [started.conversation_id]
            # The link is what a restart would otherwise lose, leaving the operator with a
            # transcript nothing can name.
            assert rows[0].spec_name == "explorer"

    async def test_a_stranded_row_is_reconciled_rather_than_left_running(self):
        async with client_app() as (_client, app):
            records = _records(app)
            # Written directly rather than by launching one: what a restart leaves behind
            # is a row with no run, which is exactly this and nothing else. Going through
            # a real launch would mean racing the very hook that settles it.
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

            # The count is deliberately not asserted: the pass is global, so it reflects
            # whatever else the suite has left lying around. What matters is what happened
            # to *this* row.
            row = await records.get(subagent_id, "operator")
            assert row is not None
            # A row still live after a restart describes work that stopped when the
            # process did: left alone it counts against the cap and promises a report
            # that is never coming.
            assert row.status == "cancelled"
            assert row.ended_at is not None


class TestTheCap:
    async def test_there_is_no_limit_until_the_operator_sets_one(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = [
                await _launcher(app).launch(
                    "operator", EXPLORER, f"look at {n}", handle=f"looker-at-{n}"
                )
                for n in range(4)
            ]

            # The default the operator gets without choosing anything. A cap picked in
            # advance, before anyone knows what the work splits into, would mostly be wrong.
            assert len({s.subagent_id for s in started}) == 4
            for one in started:
                await _settle(app, one.run_id)

    async def test_a_launch_over_the_cap_is_refused_with_something_to_do_instead(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            _ = await set_subagent_limit(
                app_state(app, "settings_store", SettingsStore), "operator", 1
            )
            first = await _launcher(app).launch(
                "operator", EXPLORER, "look here", handle="here-looker"
            )

            with pytest.raises(SubagentUnavailableError) as refused:
                _ = await _launcher(app).launch(
                    "operator", EXPLORER, "look there", handle="there-looker"
                )

            # A model told only "no" retries immediately and spends the turn doing it. This
            # says what it is waiting for and that the wait ends by itself.
            assert "finishes" in str(refused.value)
            await _settle(app, first.run_id)

    async def test_a_finished_subagent_gives_its_slot_back(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            _ = await set_subagent_limit(
                app_state(app, "settings_store", SettingsStore), "operator", 1
            )
            first = await _launcher(app).launch(
                "operator", EXPLORER, "look here", handle="here-looker"
            )
            await _settle(app, first.run_id)

            # Otherwise the cap is a lifetime budget rather than a concurrency one, and a
            # long session would stop being able to launch anything at all.
            second = await _launcher(app).launch(
                "operator", EXPLORER, "look there", handle="there-looker"
            )
            await _settle(app, second.run_id)

    async def test_nothing_is_created_for_a_launch_that_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            _ = await set_subagent_limit(
                app_state(app, "settings_store", SettingsStore), "operator", 1
            )
            first = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "look here",
                handle="here-looker",
                parent=SubagentParent(conversation_id="parent-cap"),
            )

            with pytest.raises(SubagentUnavailableError):
                _ = await _launcher(app).launch(
                    "operator",
                    EXPLORER,
                    "look there",
                    handle="there-looker",
                    parent=SubagentParent(conversation_id="parent-cap"),
                )

            # Checked before anything exists, so a refusal leaves no hidden conversation,
            # no row and no forked workspace behind for the operator to wonder about.
            rows = await _records(app).descendants("parent-cap", "operator")
            assert len(rows) == 1
            await _settle(app, first.run_id)


class TestIsolation:
    async def test_sharing_puts_the_subagent_in_the_parents_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "read it",
                handle="reader",
                parent=SubagentParent(conversation_id="parent-1", workspace_key="parent-1"),
            )
            await _settle(app, started.run_id)

            row = await _records(app).get(started.subagent_id, "operator")
            assert row is not None
            # The default: it sees the work in progress, and what it does is simply there.
            assert row.workspace_key == "parent-1"
            assert row.delegation_id is None

    async def test_isolating_gives_it_a_delegated_key_of_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            started = await _launcher(app).launch(
                "operator",
                WORKER,
                "rewrite it",
                handle="rewriter",
                parent=SubagentParent(conversation_id="parent-1", workspace_key="parent-1"),
                isolate=True,
            )
            await _settle(app, started.run_id)

            row = await _records(app).get(started.subagent_id, "operator")
            assert row is not None
            # A delegated key — `<parent>/<delegation>` — which is what routes it to its
            # own copy in both workspace kinds (`services/workspace.py`).
            assert row.workspace_key is not None
            assert row.workspace_key.startswith("parent-1/")
            assert row.delegation_id is not None

    async def test_isolation_only_ever_increases(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="done")
            apart = AgentDefinition(
                name="apart",
                description="works apart",
                instructions="do it elsewhere",
                workspace="isolated",
            )
            started = await _launcher(app).launch(
                "operator",
                apart,
                "go",
                handle="the-apart-one",
                parent=SubagentParent(conversation_id="parent-1", workspace_key="parent-1"),
                isolate=False,
            )
            await _settle(app, started.run_id)

            row = await _records(app).get(started.subagent_id, "operator")
            assert row is not None
            # Omitting `isolate` must not pull a sub-agent written to stay out of the way
            # back into the operator's own files.
            assert row.workspace_policy == "isolated"


def _capture_composition(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """The arguments the sub-agent's turn was actually composed with.

    Wrapping the real `compose_turn` rather than replacing it: the turn still runs, so
    these stay tests of a sub-agent that works rather than of a call that was made.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr("harness.manifests._subagents.compose_turn", _recording(compose_turn, seen))
    return seen


def _recording[**P, R](real: Callable[P, R], seen: dict[str, object]) -> Callable[P, R]:
    def capture(*args: P.args, **kwargs: P.kwargs) -> R:
        seen.update(kwargs)
        return real(*args, **kwargs)

    return capture
