"""Talking to a sub-agent that is still working.

Handing work over is not the same as forgetting about it. Half way through a sub-agent's
run the thread that launched it can learn something that changes what it wanted — the
operator narrowed the question, a sibling already covered half of it, a constraint arrived
— and without a way to say so the only options are to let it finish the wrong work or to
cancel and start again, both of which are paid for in full.

So a direction travels back down the link a report comes up. What is pinned here:

- it rides the **steering road**, which already guarantees a queued message is handed to
  the *next, not-yet-sent* request — so a direction can no more interrupt a sub-agent
  mid-stream than an operator's own mid-turn message can;
- it is **framed as a direction**, never bare and never as a report. A sub-agent's thread
  has no operator in it, so unframed text there is from nobody at all;
- the transcript reads that framing **back off**, so the operator sees the direction and
  not our markup;
- a sub-agent that has already settled is a **refusal with the truth in it** — its report
  is on its way — rather than a direction dropped in silence;
- and a sub-agent is offered **neither** this nor the listing, because both address another
  sub-agent by id and nothing about one's job needs to reach a sibling.
"""

from __future__ import annotations

import pytest

from agent.injected import injected_text
from runs.events import MessageSource, now_utc
from runs.run import QueuedMessage
from services.agents import builtin_roster
from services.subagents import (
    SubagentParent,
    SubagentUnavailableError,
)
from services.subagents.report import (
    DIRECTION_OPEN,
    direction_body,
    direction_envelope,
    report_envelope,
)
from tests._helpers import client_app, patch_model_resolution
from tests.test_subagents_launch import _launcher, _settle

EXPLORER = builtin_roster()["explorer"]


def _queued(text: str, *, source: MessageSource = "parent") -> QueuedMessage:
    return QueuedMessage(id="m1", text=text, queued_at=now_utc(), source=source)


class TestHowADirectionReads:
    def test_it_is_framed_as_coming_from_the_agent_that_launched_it(self):
        text = injected_text(_queued("only the parser, not the lexer"))
        assert "only the parser, not the lexer" in text
        # The three things a sub-agent would otherwise get wrong: who this is from, that it
        # supersedes the brief, and that there is nobody here to answer.
        assert "The agent that launched you" in text
        assert "changes the task you were given" in text

    def test_it_is_not_framed_as_a_report(self):
        # The two cross the same link in opposite directions and must not be confusable:
        # a direction read as a report would have the sub-agent believe it had already
        # finished, and a report read as a direction would rewrite the reader's own brief.
        direction = injected_text(_queued("narrow it"))
        assert direction != report_envelope("narrow it")
        assert "reported back" not in direction

    def test_the_transcript_shows_the_direction_and_not_our_markup(self):
        assert direction_body(direction_envelope("narrow it")) == "narrow it"
        # And the two readers cannot be crossed, which is what makes the envelope worth
        # having at all rather than a prefix.
        assert direction_body(report_envelope("I found it")) is None

    def test_an_opening_tag_alone_is_not_an_envelope(self):
        # Matched on the whole header, opener *and* preamble. Anyone who can type angle
        # brackets can type the tag, and a message relabelled as a launcher's direction is
        # the same lie as a report relabelled as the operator's.
        assert direction_body(f"{DIRECTION_OPEN}\ndo something else") is None


class TestDelivery:
    async def test_it_queues_onto_a_sub_agent_that_is_still_working(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            run = app.state.runs.get(started.run_id)
            assert run is not None

            view = await _launcher(app).steer(
                "operator", "c-parent", "parser-finder", "only the parser"
            )

            # THE assertion. Into the inbox its next request drains, never spliced into one
            # already in flight — the same guarantee the operator's own steering has.
            assert [m.source for m in run.pending_messages] == ["parent"]
            # Addressed by the name the launch gave it, and it lands on the right one.
            assert view.subagent_id == started.subagent_id
            assert view.handle == "parser-finder"
            await _settle(app, started.run_id)

    async def test_a_sub_agent_that_has_finished_is_told_about_rather_than_dropped(
        self, monkeypatch
    ):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            await _settle(app, started.run_id)

            with pytest.raises(SubagentUnavailableError) as refused:
                await _launcher(app).steer("operator", "c-parent", "parser-finder", "actually…")
            # Not a failure of the caller's — it is racing something that finished. The
            # answer it wanted is already on its way, so say that rather than just "no".
            detail = str(refused.value)
            assert "already finished" in detail
            assert "report" in detail

    async def test_an_unknown_handle_is_refused_in_words_that_name_it(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            with pytest.raises(SubagentUnavailableError) as refused:
                await _launcher(app).steer("operator", "c-parent", "nope", "hello?")
            # The failure this nearly always reports is a model half-remembering a name, so
            # the useful thing to say back is *which* name did not land — and where the
            # ones that would are.
            detail = str(refused.value)
            assert "'nope'" in detail
            assert "subagents_list" in detail

    async def test_one_threads_handle_does_not_address_anothers_sub_agent(self, monkeypatch):
        """A handle is unique inside a thread and says nothing outside one.

        The lookup is scoped to the launching conversation for the same reason the listing
        is: an agent that could reach a sibling thread's sub-agent by guessing a plausible
        name could redirect work it never asked for, with text the envelope frames as
        coming from the agent that launched it.
        """
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            with pytest.raises(SubagentUnavailableError):
                await _launcher(app).steer(
                    "operator", "a-different-thread", "parser-finder", "stop"
                )
            await _settle(app, started.run_id)

    async def test_another_owners_sub_agent_is_not_steerable(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            with pytest.raises(SubagentUnavailableError):
                await _launcher(app).steer("someone-else", "c-parent", "parser-finder", "stop")
            await _settle(app, started.run_id)

    async def test_a_direction_with_nothing_in_it_is_refused(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            with pytest.raises(SubagentUnavailableError):
                await _launcher(app).steer("operator", "c-parent", "parser-finder", "   ")
            await _settle(app, started.run_id)


class TestHowAReportNamesItself:
    """The other direction down the same link, and the same problem: which one is this?

    A thread with three `explorer`s out gets three reports, and the spec alone labels all
    three identically — leaving the report's own body as the only thing to tell them apart,
    which is exactly the thing the model has not read yet.
    """

    def test_it_is_named_by_the_handle_with_the_spec_beside_it(self):
        from harness.manifests._subagent_wake import _report_text

        text = _report_text("helper-function-finder", "explorer", "found it", None, None)
        assert text.startswith("Sub-agent `helper-function-finder` (explorer) finished.")

    def test_a_sub_agent_named_after_its_spec_is_not_named_twice(self):
        from harness.manifests._subagent_wake import _report_text

        # Every row the migration backfilled is in this shape, and `explorer (explorer)` is
        # noise in the first line of every report they will ever send.
        text = _report_text("explorer", "explorer", "found it", None, None)
        assert text.startswith("Sub-agent `explorer` finished.")


class TestCheckingOnOne:
    async def test_it_is_read_back_by_the_name_it_was_launched_under(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it in parser.py")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            await _settle(app, started.run_id)

            view = await _launcher(app).read("operator", "c-parent", "parser-finder")

            # The whole point of the handle: no id was carried anywhere, and the sub-agent
            # that comes back is the one the launching turn named.
            assert view.subagent_id == started.subagent_id
            assert view.summary == "found it in parser.py"

    async def test_an_unknown_handle_says_which_name_did_not_land(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch)
            with pytest.raises(SubagentUnavailableError) as refused:
                await _launcher(app).read("operator", "c-parent", "parser-finder")
            assert "'parser-finder'" in str(refused.value)


class TestWhatIsStillOut:
    async def test_a_working_sub_agent_is_listed(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            live = await _launcher(app).live("operator", conversation_id="c-parent")
            assert [view.subagent_id for view in live] == [started.subagent_id]
            await _settle(app, started.run_id)

    async def test_a_thread_is_never_shown_another_threads_sub_agents(self, monkeypatch):
        """`live` reads `None` as *every* thread's, which is right for the operator's cap
        and wrong for the tool.

        A run with no conversation of its own — a scheduled task — would otherwise be
        handed every live sub-agent the operator has, and with them the ids to redirect one
        it never launched. That is the sibling-injection hole the withheld set closes on
        the other axis, and it has to be closed here too.
        """
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            # What a thread-less run is scoped to: its own launches, recorded under "".
            assert await _launcher(app).live("operator", conversation_id="") == []
            # And the unscoped read, which is the cap's and must stay whole.
            assert len(await _launcher(app).live("operator")) == 1
            await _settle(app, started.run_id)

    async def test_one_that_has_reported_falls_off_the_list(self, monkeypatch):
        async with client_app() as (_client, app):
            patch_model_resolution(monkeypatch, output_text="found it")
            started = await _launcher(app).launch(
                "operator",
                EXPLORER,
                "find the parser",
                handle="parser-finder",
                parent=_parent_thread(),
            )
            await _settle(app, started.run_id)

            # The whole reason the model is given a list rather than a history: what it
            # needs to know before concluding is what it is still *waiting* on, and a
            # finished sub-agent has already told it what it found.
            assert await _launcher(app).live("operator", conversation_id="c-parent") == []


def _parent_thread() -> SubagentParent:
    return SubagentParent(conversation_id="c-parent")


class TestWhatTheToolHandsBack:
    async def test_a_settled_sub_agent_is_an_answer_not_a_retry(self):
        """`launch` returns a dict for the same exception, and this must too.

        Everything `steer` refuses is settled — the sub-agent finished, or there is no such
        id — so a retry of the identical call cannot come out differently. Raising
        `ModelRetry` spends a model round trip proving that, and on a turn near its limit
        it can be the round trip that ends it.
        """
        from pydantic_ai import RunContext
        from pydantic_ai.models.test import TestModel
        from pydantic_ai.usage import RunUsage

        from core.container import ServiceContainer
        from runs import Run, RunStream
        from services.subagents import SubagentLauncher
        from tools import RunDeps
        from tools import subagents as module

        class _Settled(SubagentLauncher):
            async def launch(self, *a, **k):  # pragma: no cover — not under test
                raise NotImplementedError

            async def steer(self, owner_id, conversation_id, handle, message):
                raise SubagentUnavailableError("`researcher` has already finished.")

            async def read(self, *a, **k):  # pragma: no cover — not under test
                raise NotImplementedError

            async def live(self, *a, **k):  # pragma: no cover — not under test
                raise NotImplementedError

            async def for_parent(self, *a, **k):  # pragma: no cover — not under test
                raise NotImplementedError

        caps = ServiceContainer()
        caps.add(_Settled(), as_type=SubagentLauncher)
        run = Run(id="t", kind="chat", owner_id="operator", stream=RunStream())
        ctx = RunContext(
            deps=RunDeps(run=run, owner_id="operator", caps=caps),
            model=TestModel(),
            usage=RunUsage(),
        )
        toolset = module.subagents_toolset()
        tools = await toolset.get_tools(ctx)
        result = await toolset.call_tool(
            "send", {"name": "the-researcher", "message": "narrow it"}, ctx, tools["send"]
        )
        assert result["sent"] is False
        assert "already finished" in result["detail"]


class TestASpecAsksRatherThanGrants:
    """The numeric fields are folded against what the operator allowed, never substituted.

    ``or`` is the shape that looks right here and is wrong: it takes the spec's number
    whenever the spec has one, which is a sub-agent overruling the operator. The engine
    already refuses to let a floor do that — ``get_agent_request_limit_override`` exists
    only so an explicitly lowered ceiling can be told apart from an unset one — and a spec
    must not be the way around it.
    """

    def test_an_operator_who_lowered_the_ceiling_is_not_overruled(self):
        from services.agents import lower_of as _lower_of

        # The researcher asks for 60; the operator said 10. They get 10, and the settings
        # page keeps telling the truth.
        assert _lower_of(60, 10) == 10

    def test_a_spec_may_still_raise_a_default_nobody_chose(self):
        from services.agents import lower_of as _lower_of

        # No operator ceiling: the spec's number stands. That is a floor over a shipped
        # default, which is a floor rather than an override.
        assert _lower_of(60, None) == 60

    def test_a_spec_that_asks_for_nothing_takes_the_bound(self):
        from services.agents import lower_of as _lower_of

        assert _lower_of(None, 10) == 10
        assert _lower_of(None, None) is None


class TestASubAgentCannotReachItsSiblings:
    def test_neither_tool_is_offered_to_a_sub_agent(self):
        from harness.manifests._subagents import _NO_RECURSION

        # Not about depth — `read` stays, and is harmless. These two address a sub-agent
        # by id within the *owner*, so one keeping them could redirect a sibling with text
        # the envelope frames as coming from the agent that launched it.
        assert {"subagents_send", "subagents_list"} <= _NO_RECURSION
        assert "subagents_read" not in _NO_RECURSION
