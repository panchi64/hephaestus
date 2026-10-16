"""A finished sub-agent's report reaching the thread that launched it.

The launching turn does not wait: it starts sub-agents, carries on, and ends. So something
has to carry each report back afterwards and get the model working again, and these hold
the properties that make that safe rather than merely working.

The load-bearing one is first: **a sub-agent finishing mid-stream must not interrupt the
parent.** A report rides the same road an operator's mid-turn message does, and that road
hands a queued message to the *next, not-yet-sent* request — so the model's tokens finish
and the report lands at the following boundary. There is deliberately no second delivery
route; a second route is how that guarantee would be lost.

The one that road does not cover is next: a report arriving during the parent's **final**
stream, where no further request is ever made. The substrate drops what is still queued at
terminal — correct for the operator, whose client rebuilds their own text from the replay,
and silently lossy for a report, which nothing else holds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from pydantic_ai import ModelRequest, ModelRetry, RunContext, RunUsage, UserPromptPart
from pydantic_ai.models.test import TestModel

from agent import turn as turn_module
from agent.injected import injected_text
from harness.manifests._subagent_wake import SubagentWake, report_text
from routes.chat import compose_turn
from routes.deps import app_state
from runs import QueuedMessage, Run, RunStatus, RunStream
from runs.events import MessageSource
from services.agents import ToolAccess
from services.subagent_store import SubagentStore
from services.subagents import FiledReport, ReportStatus
from tests._helpers import client_app, patch_model_resolution
from tests._json import json_body, s
from tools import RunDeps, build_agent_toolsets
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
    """Every turn the wake composes, captured as it happens.

    Asking the registry afterwards is a race the test loses at random: a wake turn against
    a stubbed model can finish, and stop being the conversation's active run, before the
    assertion reads it. Capturing the composition is the same fact without the timing.
    """
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "harness.manifests._subagent_wake.compose_turn", _capturing(compose_turn, calls)
    )
    return calls


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
    """Wait for the app's own terminal hooks to finish reacting.

    A run's terminal hooks are background tasks, so `await run.task` returning does not
    mean they have run — and the wake is one of them. A test that stages a pending message
    without waiting here is racing the real hook for it, which is the test being wrong
    rather than the code: in production nothing appends to that queue except the wake
    itself, under its own per-thread lock.
    """
    pending = list(app_state(app).run_terminal_tasks)
    if pending:
        _ = await asyncio.gather(*pending, return_exceptions=True)


class TestItNeverInterrupts:
    def test_a_report_is_framed_as_a_report_and_not_as_the_operator(self):
        text = injected_text(_queued("I found it in parser.py"))

        # The model has one shape for a message from outside itself, so without this the
        # transcript would claim the operator typed the report, and every later turn
        # would replay it that way.
        assert "I found it in parser.py" in text
        assert "not from the operator" in text

    def test_the_operators_own_words_are_handed_over_untouched(self):
        # Framing between the operator and the model is framing neither asked for.
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

            # THE assertion, and the reason a report rides the steering road at all: it is
            # queued, never spliced into a request already in flight. `drain_messages` is
            # what hands it over, and `agent/turn.py` calls that on a node yielded *before*
            # it streams.
            #
            # Queued before the turn's first request, the report rides that request
            # alongside the prompt (which was already on record) and is recorded behind
            # it as a node of its own — the answer to both follows, whole.
            turns = await app_state(app).conversations.messages_view(s(created["conversation_id"]))
            assert [t.role for t in turns][:3] == ["user", "subagent", "assistant"]

    def test_every_road_a_queued_message_takes_is_framed_the_same_way(self):
        """One drain, one framing — and there is more than one drain.

        A turn takes queued messages in two places: at a model-request boundary mid-turn,
        and again when the model has *finished* and something arrived while it was
        answering. The second is where a sub-agent that finishes during the parent's reply
        actually lands, which makes it the common road rather than the odd one — and it
        spent a while handing the text over bare, so the report read as the operator's in
        the history every later turn replays.

        Pinned by counting rather than by driving a turn, because the failure is not a
        behaviour that can be provoked on demand: it is one call site out of two that
        forgot, and which site a test happens to exercise is a matter of how fast the
        model answered. Every drain must be matched by a framing, and this notices a third
        one arriving unframed as readily as it notices this one regressing.
        """
        source = (Path(turn_module.__file__)).read_text()

        assert source.count("drain_messages()") == source.count("injected_text(")
        # And nothing builds the part straight off the message, which is how both of the
        # counts above could stay equal while one road went bare again.
        assert "UserPromptPart(m.text)" not in source
        assert "UserPromptPart(message.text)" not in source


class TestHowTheOperatorReadsIt:
    """The envelope is written for the model and read back for the operator.

    Both halves matter and only one of them is about the model. A report arrives in the
    one shape there is for a message from outside the model, so on the way back out it
    would show as a turn the operator took — a message they have not even seen, attributed
    to them, in their own thread.
    """

    def test_a_report_turn_is_not_shown_as_the_operator_speaking(self):
        from services.conversation_view import MessageView, project_tree
        from services.subagents.report import report_envelope

        views: list[MessageView] = project_tree(
            [("n1", ModelRequest(parts=[UserPromptPart(content=report_envelope("I found it"))]))]
        )

        assert [v.role for v in views] == ["subagent"]
        # And the framing written for the model is taken back off: it addresses the model
        # about the operator, in the third person, and the operator is who reads this.
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
        # Not the operator's turn, and the framing written for the model comes off.
        assert [v.role for v in views] == ["subagent"]
        assert views[0].author == "lexer"
        assert views[0].content == "only the parser"

    def test_the_operators_own_words_are_still_theirs(self):
        from services.conversation_view import project_tree

        views = project_tree(
            [("n1", ModelRequest(parts=[UserPromptPart(content="<subagent-report> nice try")]))]
        )
        # Matched on the exact envelope, so text that merely resembles one is not
        # relabelled — a message of theirs attributed to a sub-agent is the same lie
        # in reverse.
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

            # Into the inbox the running turn will drain, rather than starting a second
            # turn against a history this one has not finished writing.
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

            # The normal case: the launching turn ended long ago, so the report has to
            # bring the thread back to life rather than wait for a boundary that will
            # never come.
            assert [c["kind"] for c in composed] == ["wake"]
            # Framed as a report, exactly as one queued into a running turn is: the two
            # roads are the same event, and a model that could tell them apart would
            # answer one of them as though the operator had spoken.
            assert "a report" in _prompt(composed[0])
            assert "not from the operator" in _prompt(composed[0])
            await _drain(app)

    async def test_a_wake_turn_does_not_take_the_interactive_lane(self):
        from runs.lanes import lane_for

        # A burst of sub-agents finishing at once must never put the operator's own
        # message in a queue behind them.
        assert lane_for("wake") == "linked"


class TestTheReportThatNearlyGotDropped:
    async def test_a_report_queued_during_the_final_stream_survives_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            run = _run_of(app, s(body["run_id"]))
            if run.task is not None:
                await run.task
            await _settled(app)
            # Exactly the case: it arrived while the last answer was streaming, so no
            # further model request was ever made and the substrate is about to drop it.
            run.pending_messages.append(_queued("the sub-agent's only report"))
            composed = _woken(monkeypatch)

            await _wake(app).settled(run, False)

            assert composed, "the report was dropped with the run"
            assert "the sub-agent's only report" in _prompt(composed[0])
            await _drain(app)

    async def test_the_operators_undelivered_message_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = json_body(created)
            run = _run_of(app, s(body["run_id"]))
            if run.task is not None:
                await run.task
            await _settled(app)
            run.pending_messages.append(_queued("hang on", source="operator"))
            composed = _woken(monkeypatch)

            await _wake(app).settled(run, False)

            # Their client still holds the text and rebuilds the pending bubble from the
            # replay. Reviving it here would put words in the thread they may have since
            # thought better of.
            assert composed == []


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

            # Every wake is a full turn against a large context, and a sub-agent can wake
            # the parent which can launch a sub-agent. The report is still recorded
            # against the sub-agent; the thread simply stops driving itself.
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

            # The operator sending a turn into the tree is what clears the rail, and the
            # report waits for exactly that rather than being dropped at the ceiling.
            wake.observed(run)
            await wake.settled(run, False)

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
            paused = [n for n in listed if n.title == "Agents paused for you"]
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

            wake.observed(run)

            # The rail exists to stop an unattended loop. A human sending a message is
            # exactly the evidence that this is no longer one.
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
        # A sub-agent stopped at a bound did not do its job, and a card calling that
        # "done" is a card the operator would be wrong to trust.
        assert status_of(run) == expected


async def _spawned(app: FastAPI) -> tuple[str, str]:
    """A spawned agent under ``parent-1`` whose first turn has ended: its record id and
    its thread."""
    from services.agents import builtin_roster
    from services.subagents import SubagentLauncher, SubagentParent

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
    # Its first report woke the parent; that turn ends before the test's own delivery, so
    # the delivery composes a turn rather than queueing onto it.
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

    async def test_a_stop_overrides_a_report_already_filed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            _, conversation_id = await _spawned(app)
            composed = _woken(monkeypatch)
            run = _ended(conversation_id, RunStatus.cancelled)

            _wake(app).file(run.id, FiledReport(status="done", text="all good"))
            await _wake(app).settled(run, False)

            # Stopped is what happened; a report written before the stop does not undo it.
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

        # Without it an agent has no way to finish, which no setting is meant to cause.
        assert await _offered(ctx)


async def _offered(ctx: RunContext[RunDeps]) -> bool:
    (stack,) = build_agent_toolsets({"subagent": report_toolset()})
    return "subagent_report" in await stack.get_tools(ctx)


def _validator_ctx(kind: str, *, reports: bool) -> RunContext[RunDeps]:
    run = Run(id="r", kind=kind, owner_id="operator", stream=RunStream())
    deps = RunDeps(run=run, owner_id="operator", reports_to_parent=reports)
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())
