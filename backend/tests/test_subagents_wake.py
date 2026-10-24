"""A finished sub-agent's report reaching the thread that launched it.

A report never interrupts the parent mid-stream: it rides the operator's mid-turn message
road and lands at the next request boundary. One that arrives during the final stream stays
in the inbox for the thread's next turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from pydantic_ai import (
    FunctionToolset,
    ModelRequest,
    ModelRetry,
    RunContext,
    RunUsage,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel

from agent import turn as turn_module
from agent.injected import injected_text
from harness.manifests._inbox_dispatch import InboxDispatcher
from harness.manifests._subagent_wake import SubagentWake, report_text
from routes.chat import compose_turn
from routes.deps import app_state
from runs import QueuedMessage, Run, RunStatus, RunStream
from runs.events import MessageSource
from runs.run import StopCause
from services.agents import ToolAccess
from services.subagent_store import SubagentStore
from services.subagents import FiledReport, ReportStatus, SubagentLauncher
from tests._helpers import client_app, patch_model_resolution, settle
from tests._json import json_body, s
from tools import RunDeps, build_agent_toolsets
from tools.delegation import add_delegation_tools
from tools.report import report_toolset, require_report


def _wake(app: FastAPI) -> SubagentWake:
    wake = cast(object, app.state.subagent_wake)
    assert isinstance(wake, SubagentWake)
    return wake


def _run_of(app: FastAPI, run_id: str) -> Run:
    run = app_state(app).runs.get(run_id)
    assert run is not None
    return run


def _queued(text: str, *, source: MessageSource = "subagent") -> QueuedMessage:
    return QueuedMessage(id="m1", text=text, queued_at=datetime.now(UTC), source=source)


def _capturing[**P, R](real: Callable[P, R], calls: list[dict[str, object]]) -> Callable[P, R]:
    def capture(*args: P.args, **kwargs: P.kwargs) -> R:
        calls.append(dict(kwargs))
        return real(*args, **kwargs)

    return capture


def _woken(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Every turn the wake composes; a stubbed wake turn can finish before the registry is read."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "harness.manifests._inbox_dispatch.compose_turn", _capturing(compose_turn, calls)
    )
    return calls


def _dispatcher(app: FastAPI) -> InboxDispatcher:
    dispatcher = app_state(app).inbox_dispatcher
    assert isinstance(dispatcher, InboxDispatcher)
    return dispatcher


def _prompt(call: dict[str, object]) -> str:
    prompt = call["prompt"]
    assert isinstance(prompt, str)
    return prompt


async def _drain(app: FastAPI) -> None:
    """Let every run the test started finish, so nothing is torn down mid-turn."""
    for run in app_state(app).runs.list():
        if run.task is not None:
            _ = await asyncio.gather(run.task, return_exceptions=True)


async def _settled(app: FastAPI) -> None:
    """Wait for the run's terminal hooks, the wake among them; they outlive `run.task`."""
    pending = list(app_state(app).run_terminal_tasks)
    if pending:
        _ = await asyncio.gather(*pending, return_exceptions=True)


class TestItNeverInterrupts:
    def test_a_report_is_framed_as_a_report_and_not_as_the_operator(self):
        text = injected_text(_queued("I found it in parser.py"))

        assert "I found it in parser.py" in text
        assert "not from the operator" in text

    def test_the_operators_own_words_are_handed_over_untouched(self):
        assert injected_text(_queued("wait, stop", source="operator")) == "wait, stop"

    async def test_a_report_lands_on_the_next_request_not_the_current_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="the answer")
            created = json_body(await client.post("/chat", json={"prompt": "hello"}))
            run = _run_of(app, s(created["run_id"]))

            _ = run.enqueue_message("a sub-agent finished", source="subagent")
            if run.task is not None:
                await run.task

            # Queued before the first request, the report rides it behind the prompt and
            # is never spliced into a request already streaming.
            turns = await app_state(app).conversations.messages_view(s(created["conversation_id"]))
            assert [t.role for t in turns][:3] == ["user", "subagent", "assistant"]

    def test_every_road_a_queued_message_takes_is_framed_the_same_way(self):
        """Every `drain_messages()` call site frames its text; counted, as the site hit varies."""
        source = (Path(turn_module.__file__)).read_text()

        assert source.count("drain_messages()") == source.count("injected_text(")
        assert "UserPromptPart(m.text)" not in source
        assert "UserPromptPart(message.text)" not in source


class TestHowTheOperatorReadsIt:
    """The envelope is written for the model and must read back to the operator as a sub-agent's."""

    def test_a_report_turn_is_not_shown_as_the_operator_speaking(self):
        from services.conversation_view import MessageView, project_tree
        from services.subagents.report import report_envelope

        views: list[MessageView] = project_tree(
            [("n1", ModelRequest(parts=[UserPromptPart(content=report_envelope("I found it"))]))]
        )

        assert [v.role for v in views] == ["subagent"]
        assert views[0].content == "I found it"

    def test_a_report_turn_names_who_reported_and_has_no_author(self):
        from services.conversation_view import project_tree
        from services.subagents.report import report_envelope

        text = report_text("done", "fixer", "worker", "fixed", None, None, callsign="BRAX")
        request = ModelRequest(parts=[UserPromptPart(content=report_envelope(text))])
        views = project_tree([("n1", request)])

        assert (views[0].reporter, views[0].author) == ("fixer", None)

    def test_a_message_from_another_agent_is_shown_with_its_sender(self):
        from services.conversation_view import project_tree
        from services.subagents.report import message_envelope

        views = project_tree(
            [
                (
                    "n1",
                    ModelRequest(
                        parts=[UserPromptPart(content=message_envelope("lexer", "only the parser"))]
                    ),
                )
            ]
        )
        assert [v.role for v in views] == ["subagent"]
        assert views[0].author == "lexer"
        assert views[0].content == "only the parser"

    def test_the_operators_own_words_are_still_theirs(self):
        from services.conversation_view import project_tree

        views = project_tree(
            [("n1", ModelRequest(parts=[UserPromptPart(content="<subagent-report> nice try")]))]
        )
        # Only the exact envelope is relabelled; lookalike text stays the operator's.
        assert [v.role for v in views] == ["user"]


class TestDelivery:
    async def test_it_queues_onto_a_parent_that_is_still_running(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="working")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            run = _run_of(app, s(body["run_id"]))

            _ = await _wake(app).deliver("operator", s(body["conversation_id"]), "a report")

            assert [m.source for m in run.pending_messages] == ["subagent"]
            if run.task is not None:
                await run.task

    async def test_it_starts_a_turn_when_the_parent_has_nothing_running(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            first = _run_of(app, s(body["run_id"]))
            if first.task is not None:
                await first.task

            composed = _woken(monkeypatch)

            _ = await _wake(app).deliver("operator", s(body["conversation_id"]), "a report")

            assert [c["kind"] for c in composed] == ["wake"]
            assert "a report" in _prompt(composed[0])
            assert "not from the operator" in _prompt(composed[0])
            await _drain(app)

    async def test_a_wake_turn_does_not_take_the_interactive_lane(self):
        from runs.lanes import lane_for

        assert lane_for("wake") == "linked"


class TestNothingQueuedIsLostAtTerminal:
    async def test_a_report_queued_during_the_final_stream_survives_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            conversation_id = s(body["conversation_id"])
            run = _run_of(app, s(body["run_id"]))
            await settle(app)
            # Arrives during the final stream, so no later request on this run drains it.
            _ = (
                app_state(app)
                .runs.inbox(conversation_id, "operator")
                .enqueue("the sub-agent's only report", source="subagent")
            )
            composed = _woken(monkeypatch)

            await _dispatcher(app).settled(run, False)

            assert composed, "the report was dropped with the run"
            assert "the sub-agent's only report" in _prompt(composed[0])
            await settle(app)

    async def test_the_operators_queued_message_opens_its_own_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            conversation_id = s(body["conversation_id"])
            run = _run_of(app, s(body["run_id"]))
            await settle(app)
            _ = app_state(app).runs.inbox(conversation_id, "operator").enqueue("hang on")
            composed = _woken(monkeypatch)

            await _dispatcher(app).settled(run, False)
            await settle(app)

            assert composed == []
            turns = await app_state(app).conversations.messages_view(conversation_id)
            assert [t.content for t in turns if t.role == "user"] == ["go", "hang on"]


class TestTheRunawayRail:
    async def test_a_thread_stops_driving_itself_at_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            first = _run_of(app, s(body["run_id"]))
            if first.task is not None:
                await first.task
            wake = _wake(app)
            wake.spend_budget(s(body["conversation_id"]))
            composed = _woken(monkeypatch)

            _ = await wake.deliver("operator", s(body["conversation_id"]), "another report")

            assert composed == []

    async def test_a_held_report_goes_out_on_the_operators_next_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            conversation_id = s(body["conversation_id"])
            run = _run_of(app, s(body["run_id"]))
            if run.task is not None:
                await run.task
            await _settled(app)
            wake = _wake(app)
            wake.spend_budget(conversation_id)
            composed = _woken(monkeypatch)

            _ = await wake.deliver("operator", conversation_id, "the held report")
            assert composed == []

            # An operator turn clears the rail; the held report waits for it.
            _dispatcher(app).observed(run)
            await _dispatcher(app).settled(run, False)

            assert [c["kind"] for c in composed] == ["wake"]
            assert "the held report" in _prompt(composed[0])
            await _drain(app)

    async def test_the_operator_is_told_once_when_a_tree_pauses(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from services.notifications import NotificationService

        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            conversation_id = s(body["conversation_id"])
            run = _run_of(app, s(body["run_id"]))
            if run.task is not None:
                await run.task
            await _settled(app)
            notifications = app_state(app).capabilities.get(NotificationService)
            _, before = await notifications.list_notifications("operator")
            wake = _wake(app)
            wake.spend_budget(conversation_id)

            _ = await wake.deliver("operator", conversation_id, "one")
            _ = await wake.deliver("operator", conversation_id, "two")

            listed, unread = await notifications.list_notifications("operator")
            paused = [n for n in listed if n.title == "Sub-agents paused for you"]
            assert len(paused) == 1
            assert paused[0].conversation_id == conversation_id
            assert unread == before + 1

    async def test_the_operators_own_turn_clears_the_count(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            run = _run_of(app, s(body["run_id"]))
            wake = _wake(app)
            wake.spend_budget(s(body["conversation_id"]))

            _dispatcher(app).observed(run)

            assert wake.unattended_turns(s(body["conversation_id"])) == 0
            if run.task is not None:
                await run.task


class TestStatus:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (RunStatus.done, "done"),
            (RunStatus.cancelled, "cancelled"),
            (RunStatus.error, "failed"),
            (RunStatus.blocked, "failed"),
        ],
    )
    def test_how_a_subagent_ended_is_what_the_card_shows(self, status: RunStatus, expected: str):
        from harness.manifests._subagent_wake import status_of

        run = Run(id="r", kind="linked", owner_id="operator", stream=RunStream())
        run.status = status
        assert status_of(run) == expected


async def _spawned(app: FastAPI) -> tuple[str, str]:
    """A spawned agent under ``parent-1`` whose first turn has ended: its record id and thread."""
    from services.agents import builtin_roster
    from services.subagents import SubagentParent

    launcher = app_state(app).capabilities.get(SubagentLauncher)
    started = await launcher.launch(
        "operator",
        builtin_roster()["worker"],
        "migrate the schema",
        handle="migrate-schema",
        parent=SubagentParent(conversation_id="parent-1"),
    )
    run = _run_of(app, started.run_id)
    if run.task is not None:
        await run.task
    await _settled(app)
    # The first report's wake turn must end so the test's delivery composes rather than queues.
    await _drain(app)
    await _settled(app)
    return started.subagent_id, started.conversation_id


def _records(app: FastAPI) -> SubagentStore:
    records = cast(object, app.state.subagent_records)
    assert isinstance(records, SubagentStore)
    return records


def _ended(conversation_id: str, status: RunStatus = RunStatus.done) -> Run:
    """A later unattended turn of the agent, ended."""
    run = Run(
        id="later-turn",
        kind="wake",
        owner_id="operator",
        stream=RunStream(),
        conversation_id=conversation_id,
    )
    run.status = status
    return run


class TestTheReportContract:
    @pytest.mark.parametrize(
        ("status", "hint"),
        [
            ("done", None),
            ("blocked", "help: answer it with `agents_message` to `migrate-schema`"),
            ("failed", "help: spawn a fresh agent with a narrower brief"),
        ],
    )
    async def test_a_filed_report_decides_how_the_agent_ended(
        self, monkeypatch: pytest.MonkeyPatch, status: ReportStatus, hint: str | None
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            subagent_id, conversation_id = await _spawned(app)
            wake = _wake(app)
            composed = _woken(monkeypatch)
            run = _ended(conversation_id)

            wake.file(run.id, FiledReport(status=status, text="the report body"))
            await wake.settled(run, False)

            row = await _records(app).get(subagent_id, "operator")
            assert row is not None
            assert row.status == status
            prompt = _prompt(composed[0])
            assert "`migrate-schema` (" in prompt
            assert f"): {status}." in prompt
            assert "the report body" in prompt
            if hint is None:
                assert "help:" not in prompt
            else:
                assert hint in prompt
            await _drain(app)

    async def test_an_unattended_turn_without_a_report_is_failed_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            _, conversation_id = await _spawned(app)
            composed = _woken(monkeypatch)

            await _wake(app).settled(_ended(conversation_id), False)

            prompt = _prompt(composed[0])
            assert "): failed." in prompt
            assert "error: it ended without reporting" in prompt
            await _drain(app)

    async def test_a_stop_overrides_a_report_already_filed(self, monkeypatch: pytest.MonkeyPatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            _, conversation_id = await _spawned(app)
            composed = _woken(monkeypatch)
            run = _ended(conversation_id, RunStatus.cancelled)

            _wake(app).file(run.id, FiledReport(status="done", text="all good"))
            await _wake(app).settled(run, False)

            assert "): cancelled." in _prompt(composed[0])
            await _drain(app)

    async def test_the_agent_is_sent_back_once_before_its_answer_stands(self):
        ctx = _validator_ctx("wake", reports=True)

        with pytest.raises(ModelRetry, match="without calling `subagent_report`"):
            _ = await require_report(ctx, "all done")
        assert await require_report(ctx, "all done") == "all done"

    @pytest.mark.parametrize(
        ("kind", "reports", "reported"),
        [("chat", True, False), ("wake", False, False), ("wake", True, True)],
    )
    async def test_only_an_unreported_unattended_agent_turn_is_sent_back(
        self, kind: str, reports: bool, reported: bool
    ):
        ctx = _validator_ctx(kind, reports=reports)
        ctx.deps.reported = reported

        assert await require_report(ctx, "answer") == "answer"


class TestWhoIsOfferedTheReportTool:
    @pytest.mark.parametrize(("reports", "offered"), [(True, True), (False, False)])
    async def test_only_a_spawned_thread_sees_it(self, reports: bool, offered: bool):
        assert await _offered(_validator_ctx("wake", reports=reports)) is offered

    async def test_switching_off_its_category_does_not_take_it_away(self):
        ctx = _validator_ctx("wake", reports=True)
        ctx.deps.disabled_tools = frozenset({"subagent_report"})
        ctx.deps.tool_access = ToolAccess(deny=frozenset({"subagent"}))

        assert await _offered(ctx)


async def _offered(ctx: RunContext[RunDeps]) -> bool:
    (stack,) = build_agent_toolsets({"subagent": report_toolset()})
    return "subagent_report" in await stack.get_tools(ctx)


def _validator_ctx(kind: str, *, reports: bool) -> RunContext[RunDeps]:
    run = Run(id="r", kind=kind, owner_id="operator", stream=RunStream())
    deps = RunDeps(run=run, owner_id="operator", reports_to_parent=reports)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


class TestWhoStoppedIt:
    @pytest.mark.parametrize(
        ("status", "cause", "heading"),
        [
            ("cancelled", StopCause.operator, "cancelled (stopped by the operator)"),
            ("cancelled", StopCause.parent, "cancelled (stopped by its parent)"),
            ("blocked", StopCause.time_limit, "blocked (time limit)"),
            ("blocked", StopCause.turn_limit, "blocked (turn limit)"),
        ],
    )
    def test_the_heading_names_the_cause(self, status: str, cause: StopCause, heading: str):
        text = report_text(status, "migrate", "worker", "so far", None, None, cause=cause)

        assert text.startswith(f"`migrate` (worker): {heading}.")

    def test_an_operator_stop_tells_the_parent_not_to_redo_the_work(self):
        text = report_text(
            "cancelled", "migrate", "worker", None, None, None, cause=StopCause.operator
        )

        assert "Don't relaunch it or redo its task unless the operator asks." in text

    def test_a_parents_own_stop_adds_no_such_instruction(self):
        text = report_text(
            "cancelled", "migrate", "worker", None, None, None, cause=StopCause.parent
        )

        assert "operator asks" not in text

    def test_a_time_limit_says_the_work_is_partial_not_that_it_awaits_an_answer(self):
        text = report_text(
            "blocked", "migrate", "worker", "half", None, None, cause=StopCause.time_limit
        )

        assert "its work is partial: it ran out of time" in text
        assert "answer it with" not in text

    def test_a_turn_limit_says_the_work_is_partial_and_can_be_continued(self):
        text = report_text(
            "blocked", "migrate", "worker", "half", None, None, cause=StopCause.turn_limit
        )

        assert "its work is partial" in text
        assert "continue it with `agents_message` to `migrate`" in text

    @pytest.mark.parametrize(
        ("status", "cause", "heading"),
        [
            (RunStatus.cancelled, StopCause.operator, "cancelled (stopped by the operator)"),
            (RunStatus.cancelled, StopCause.parent, "cancelled (stopped by its parent)"),
            (RunStatus.blocked, StopCause.time_limit, "blocked (time limit)"),
            (RunStatus.blocked, StopCause.turn_limit, "blocked (turn limit)"),
        ],
    )
    async def test_a_run_ending_carries_its_cause_into_the_report(
        self, monkeypatch: pytest.MonkeyPatch, status: RunStatus, cause: StopCause, heading: str
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            subagent_id, conversation_id = await _spawned(app)
            composed = _woken(monkeypatch)
            run = _ended(conversation_id, status)
            run.stop_cause = cause

            await _wake(app).settled(run, False)

            assert f"): {heading}." in _prompt(composed[0])
            row = await _records(app).get(subagent_id, "operator")
            assert row is not None
            # A limit ending is stored as terminal `stopped`, so it holds no cap slot.
            expected = "stopped" if status is RunStatus.blocked else status.value
            assert row.status == expected
            assert subagent_id not in {r.id for r in await _records(app).live("operator")}
            await _drain(app)


def _tool(app: FastAPI, name: str) -> Callable[..., Awaitable[dict[str, object]]]:
    """A delegation tool as thread ``parent-1`` would call it on ``migrate-schema``."""
    toolset = FunctionToolset[RunDeps]()
    add_delegation_tools(toolset)
    function = cast(Callable[..., Awaitable[dict[str, object]]], toolset.tools[name].function)
    run = Run(id="r", kind="chat", owner_id="operator", stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id="operator",
        caps=app_state(app).capabilities,
        conversation_id="parent-1",
    )
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    async def call(**kwargs: object) -> dict[str, object]:
        return await function(ctx, "migrate-schema", **kwargs)

    return call


class TestLargeReports:
    async def test_a_report_over_the_limit_is_previewed_and_read_in_full_on_request(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            _, conversation_id = await _spawned(app)
            composed = _woken(monkeypatch)
            limit = app_state(app).settings.tool_output_spill_chars
            full = "A" * limit + "THE-MIDDLE" + "B" * limit
            run = _ended(conversation_id)

            _wake(app).file(run.id, FiledReport(status="done", text=full))
            await _wake(app).settled(run, False)

            prompt = _prompt(composed[0])
            assert "THE-MIDDLE" not in prompt
            assert len(prompt) < limit + 1_000
            assert "characters omitted" in prompt
            assert "read the full report with `agents_read migrate-schema`" in prompt
            read = _tool(app, "read")
            pages: list[str] = []
            start = 0
            while True:
                out = await read(start=start)
                pages.append(str(out["report"]))
                length = out["report_length"]
                assert isinstance(length, int)
                assert length >= len(full)
                next_start = out.get("next_start")
                if next_start is None:
                    break
                assert isinstance(next_start, int)
                start = next_start
            assert len(pages) > 1
            assert all(len(page) < limit for page in pages)
            assert "".join(pages).startswith(full)
            await _drain(app)

    async def test_a_report_within_the_limit_is_delivered_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            _, conversation_id = await _spawned(app)
            composed = _woken(monkeypatch)
            run = _ended(conversation_id)

            _wake(app).file(run.id, FiledReport(status="done", text="short and complete"))
            await _wake(app).settled(run, False)

            prompt = _prompt(composed[0])
            assert "short and complete" in prompt
            assert "agents_read" not in prompt
            await _drain(app)
