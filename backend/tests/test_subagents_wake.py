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
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic_ai import ModelRequest, UserPromptPart

from agent import turn as turn_module
from agent.injected import injected_text
from harness.manifests._subagent_wake import WAKE_CHAIN_LIMIT, SubagentWake
from runs import QueuedMessage, Run, RunStatus, RunStream
from runs.events import MessageSource
from tests._helpers import client_app, patch_model_resolution


def _wake(app) -> SubagentWake:
    wake = getattr(app.state, "subagent_wake", None)
    assert isinstance(wake, SubagentWake), "the subagents manifest did not wire the wake"
    return wake


def _queued(text: str, *, source: MessageSource = "subagent") -> QueuedMessage:
    return QueuedMessage(id="m1", text=text, queued_at=datetime.now(UTC), source=source)


def _woken(monkeypatch) -> list[dict]:
    """Every turn the wake composes, captured as it happens.

    Asking the registry afterwards is a race the test loses at random: a wake turn against
    a stubbed model can finish, and stop being the conversation's active run, before the
    assertion reads it. Capturing the composition is the same fact without the timing.
    """
    from harness.manifests import _subagent_wake

    calls: list[dict] = []
    real = _subagent_wake.compose_turn

    def capture(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(_subagent_wake, "compose_turn", capture)
    return calls


async def _drain(app) -> None:
    """Let every run the test started finish, so nothing is torn down mid-turn."""
    for run_id in list(getattr(app.state.runs, "_runs", {})):
        run = app.state.runs.get(run_id)
        if run is not None and run.task is not None:
            await asyncio.gather(run.task, return_exceptions=True)


async def _settled(app) -> None:
    """Wait for the app's own terminal hooks to finish reacting.

    A run's terminal hooks are background tasks, so `await run.task` returning does not
    mean they have run — and the wake is one of them. A test that stages a pending message
    without waiting here is racing the real hook for it, which is the test being wrong
    rather than the code: in production nothing appends to that queue except the wake
    itself, under its own per-thread lock.
    """
    pending = list(app.state.run_terminal_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TestItNeverInterrupts:
    def test_a_report_is_framed_as_a_report_and_not_as_the_operator(self):
        text = injected_text(_queued("I found it in parser.py"))

        # The model has one shape for a message from outside itself, so without this the
        # transcript would claim the operator typed the report, and every later turn
        # would replay it that way.
        assert "I found it in parser.py" in text
        assert "not a message from the operator" in text

    def test_the_operators_own_words_are_handed_over_untouched(self):
        # Framing between the operator and the model is framing neither asked for.
        assert injected_text(_queued("wait, stop", source="operator")) == "wait, stop"

    async def test_a_report_lands_on_the_next_request_not_the_current_stream(self, monkeypatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="the answer")
            created = await client.post("/chat", json={"prompt": "hello"})
            run = app.state.runs.get(created.json()["run_id"])
            assert run is not None

            run.enqueue_message("a sub-agent finished", source="subagent")
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
            turns = await app.state.conversations.messages_view(created.json()["conversation_id"])
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
    async def test_it_queues_onto_a_parent_that_is_still_running(self, monkeypatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="working")
            created = await client.post("/chat", json={"prompt": "go"})
            body = created.json()
            run = app.state.runs.get(body["run_id"])
            assert run is not None

            await _wake(app).deliver("operator", body["conversation_id"], "a report")

            # Into the inbox the running turn will drain, rather than starting a second
            # turn against a history this one has not finished writing.
            assert [m.source for m in run.pending_messages] == ["subagent"]
            if run.task is not None:
                await run.task

    async def test_it_starts_a_turn_when_the_parent_has_nothing_running(self, monkeypatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = created.json()
            first = app.state.runs.get(body["run_id"])
            assert first is not None
            if first.task is not None:
                await first.task

            composed = _woken(monkeypatch)

            await _wake(app).deliver("operator", body["conversation_id"], "a report")

            # The normal case: the launching turn ended long ago, so the report has to
            # bring the thread back to life rather than wait for a boundary that will
            # never come.
            assert [c["kind"] for c in composed] == ["wake"]
            # Framed as a report, exactly as one queued into a running turn is: the two
            # roads are the same event, and a model that could tell them apart would
            # answer one of them as though the operator had spoken.
            assert "a report" in composed[0]["prompt"]
            assert "not a message from the operator" in composed[0]["prompt"]
            await _drain(app)

    async def test_a_wake_turn_does_not_take_the_interactive_lane(self):
        from runs.lanes import lane_for

        # A burst of sub-agents finishing at once must never put the operator's own
        # message in a queue behind them.
        assert lane_for("wake") == "linked"


class TestTheReportThatNearlyGotDropped:
    async def test_a_report_queued_during_the_final_stream_survives_terminal(self, monkeypatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = created.json()
            run = app.state.runs.get(body["run_id"])
            assert run is not None
            if run.task is not None:
                await run.task
            await _settled(app)
            # Exactly the case: it arrived while the last answer was streaming, so no
            # further model request was ever made and the substrate is about to drop it.
            run.pending_messages.append(_queued("the sub-agent's only report"))
            composed = _woken(monkeypatch)

            await _wake(app).settled(run, False)

            assert composed, "the report was dropped with the run"
            assert "the sub-agent's only report" in composed[0]["prompt"]
            await _drain(app)

    async def test_the_operators_undelivered_message_is_left_alone(self, monkeypatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = created.json()
            run = app.state.runs.get(body["run_id"])
            assert run is not None
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
    async def test_a_thread_stops_driving_itself_at_the_ceiling(self, monkeypatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = created.json()
            first = app.state.runs.get(body["run_id"])
            assert first is not None
            if first.task is not None:
                await first.task
            wake = _wake(app)
            wake._chain[body["conversation_id"]] = WAKE_CHAIN_LIMIT  # noqa: SLF001
            composed = _woken(monkeypatch)

            await wake.deliver("operator", body["conversation_id"], "another report")

            # Every wake is a full turn against a large context, and a sub-agent can wake
            # the parent which can launch a sub-agent. The report is still recorded
            # against the sub-agent; the thread simply stops driving itself.
            assert composed == []

    async def test_the_operators_own_turn_clears_the_count(self, monkeypatch):
        async with client_app() as (client, app):
            patch_model_resolution(monkeypatch, output_text="ok")
            created = await client.post("/chat", json={"prompt": "go"})
            body = created.json()
            run = app.state.runs.get(body["run_id"])
            assert run is not None
            wake = _wake(app)
            wake._chain[body["conversation_id"]] = WAKE_CHAIN_LIMIT  # noqa: SLF001

            wake.observed(run)

            # The rail exists to stop an unattended loop. A human sending a message is
            # exactly the evidence that this is no longer one.
            assert body["conversation_id"] not in wake._chain  # noqa: SLF001
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
    def test_how_a_subagent_ended_is_what_the_card_shows(self, status, expected):
        from harness.manifests._subagent_wake import _status_of

        run = Run(id="r", kind="linked", owner_id="operator", stream=RunStream())
        run.status = status
        # A sub-agent stopped at a bound did not do its job, and a card calling that
        # "done" is a card the operator would be wrong to trust.
        assert _status_of(run) == expected
