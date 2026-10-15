"""`builtin_ask_user`: the turn stops on the operator for an *answer* rather than a
permission, and the answer they give becomes the call's own result.

The approval path and this one share a park, a payload and a resume, so most of what is
checked here is that sharing one machine for two reasons hasn't made either wrong: a turn
holding both piles parks once and refuses a half-answer, an answer is rendered from the
parked call rather than from whatever the client said it was, and a run nobody is watching
is never offered the tool at all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    FunctionToolset,
    ModelMessage,
    ToolReturnPart,
)
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
    StreamFunctionDef,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import AbstractToolset

from agent import stream_agent_run
from routes.deps import app_state
from runs import QuestionOption, QuestionSpec, Run, RunStream
from services.answers import (
    ASK_USER_TOOL,
    AnswerError,
    parse_answer,
    questions_of,
    render_answer,
)
from services.registry import ModelRegistry
from services.tool_policy import lane_disabled_tools
from tools import RunDeps, build_agent_toolsets
from tools.builtin import NO_OPERATOR, builtin_toolset

from ._helpers import (
    await_parked,
    client_app,
    collect_sse_events,
    parked_turn,
    register_stub_provider,
    stub_resolution,
    swap_tool_catalog,
)
from ._json import arr, json_body, obj, s

OWNER = "operator"
# The constant the projection keys on. Used rather than a literal so the tests below —
# which reach the real tool through a model that calls it by name — are what pins it
# against the name the catalog actually registers.
ASK = ASK_USER_TOOL

# One two-question call: a single-select and a multi-select, so the shapes the panel
# renders and the shapes the renderer writes are both exercised by one park.
QUESTIONS: dict[str, list[dict[str, object]]] = {
    "questions": [
        {
            "question": "Which database?",
            "options": [
                {"label": "Postgres", "description": "Relational, boring, correct"},
                {"label": "SQLite"},
            ],
        },
        {
            "question": "Which extras?",
            "options": [{"label": "Auth"}, {"label": "Billing"}],
            "multi_select": True,
        },
    ]
}


Output = str | DeferredToolRequests
NO_REPLIES: list[dict[str, object]] = []
NO_SELECTIONS: list[str] = []


def _tool_ran(messages: list[ModelMessage]) -> bool:
    return any(
        type(part).__name__ == "ToolReturnPart" for message in messages for part in message.parts
    )


def _calls_once(tool_name: str, args: Mapping[str, object]) -> StreamFunctionDef:
    """A model that calls one tool once, then answers with text once it has a result."""

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        if _tool_ran(messages):
            yield "done"
        else:
            yield {0: DeltaToolCall(name=tool_name, json_args=json.dumps(args))}

    return stream_fn


def _agent(kind: str = "chat") -> tuple[Agent[RunDeps, Output], Run, RunDeps]:
    agent = Agent(
        FunctionModel(stream_function=_calls_once(ASK, QUESTIONS)),
        deps_type=RunDeps,
        toolsets=build_agent_toolsets({"builtin": builtin_toolset()}),
        output_type=[str, DeferredToolRequests],
    )
    run = Run(id="r1", kind=kind, owner_id=OWNER, stream=RunStream())
    deps = RunDeps(
        run=run,
        owner_id=OWNER,
        disabled_tools=lane_disabled_tools(kind),
    )
    return agent, run, deps


async def _drive(
    agent: Agent[RunDeps, Output],
    run: Run,
    deps: RunDeps,
    *,
    deferred_results: DeferredToolResults | None = None,
    message_history: list[ModelMessage] | None = None,
) -> AgentRunResult[Output]:
    prompt = None if message_history else "pick one"
    async with agent.iter(
        prompt,
        deps=deps,
        deferred_tool_results=deferred_results,
        message_history=message_history,
    ) as agent_run:
        await stream_agent_run(agent_run, run)
    assert agent_run.result is not None
    return agent_run.result


# --- the call defers, and the answer comes back as its result -----------------------


async def test_the_call_defers_for_execution_not_for_approval():
    """`ask_user` must land in `calls`, not `approvals`. The distinction is the whole
    design: an approval comes back a boolean, and an answer comes back a value."""
    agent, run, deps = _agent()
    result = await _drive(agent, run, deps)

    assert isinstance(result.output, DeferredToolRequests)
    assert not result.output.approvals
    assert len(result.output.calls) == 1
    assert result.output.calls[0].tool_name == ASK


async def test_the_answer_becomes_the_tools_return_value():
    agent, run, deps = _agent()
    first = await _drive(agent, run, deps)
    assert isinstance(first.output, DeferredToolRequests)
    call_id = first.output.calls[0].tool_call_id

    second = await _drive(
        agent,
        run,
        deps,
        message_history=first.all_messages(),
        deferred_results=DeferredToolResults(calls={call_id: "Q: Which database?\nA: SQLite"}),
    )

    returns = [
        part
        for message in second.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert any("SQLite" in str(part.content) for part in returns)


# --- a run nobody is watching is never offered it ----------------------------------


def test_the_tool_is_withheld_from_unattended_lanes():
    assert lane_disabled_tools("chat") == frozenset()
    assert ASK in lane_disabled_tools("task")
    assert ASK in lane_disabled_tools("linked")
    # An unmapped kind lands in `background`, which is the conservative direction.
    assert ASK in lane_disabled_tools("something-new")


async def test_an_unattended_call_refuses_instead_of_parking():
    """Belt-and-braces: the withholding above is the real gate, but a call that somehow
    reaches the tool in a background run must return rather than park — a parked
    unattended run waits until the process restarts."""
    agent, run, deps = _agent(kind="task")
    # Deliberately offer the tool despite the lane, to reach the guard inside it.
    deps.disabled_tools = frozenset()
    result = await _drive(agent, run, deps)

    assert not isinstance(result.output, DeferredToolRequests)
    returns = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert any(NO_OPERATOR in str(part.content) for part in returns)


# --- rendering the reply -----------------------------------------------------------


def _spec(question: str, labels: list[str], multi: bool = False) -> QuestionSpec:
    return QuestionSpec(
        question=question,
        options=[QuestionOption(label=label) for label in labels],
        multi_select=multi,
    )


def test_questions_are_parsed_off_the_calls_arguments():
    parsed = questions_of(QUESTIONS)
    assert [q.question for q in parsed] == ["Which database?", "Which extras?"]
    assert parsed[0].options[0].description == "Relational, boring, correct"
    assert parsed[0].multi_select is False
    assert parsed[1].multi_select is True


def test_malformed_arguments_degrade_rather_than_raise():
    """The park is a turn stopping cleanly; an exception here would strand it."""
    assert questions_of({}) == []
    assert questions_of({"questions": ["nonsense", 7]}) == []
    [only] = questions_of({"questions": [{"question": "?", "options": [{"nope": 1}]}]})
    assert only.options == []


def test_a_chosen_option_is_rendered_against_its_question():
    rendered = render_answer(
        [_spec("Which database?", ["Postgres", "SQLite"])], [(["SQLite"], None)]
    )
    assert rendered == "Q: Which database?\nA: SQLite"


def test_several_selections_and_a_written_addition_are_kept_distinct():
    rendered = render_answer(
        [_spec("Which extras?", ["Auth", "Billing"], multi=True)],
        [(["Auth", "Billing"], "  and rate limiting  ")],
    )
    assert "A: Auth, Billing" in rendered
    assert "They also wrote: and rate limiting" in rendered


def test_writing_instead_of_choosing_says_so():
    """The model must not read the prose as a label it should have offered."""
    rendered = render_answer([_spec("Which database?", ["Postgres"])], [([], "DuckDB")])
    assert rendered == "Q: Which database?\nA: (none of the options) DuckDB"


def test_an_option_that_was_never_offered_is_refused():
    try:
        _ = render_answer([_spec("Which database?", ["Postgres"])], [(["MySQL"], None)])
    except AnswerError as exc:
        assert "MySQL" in str(exc)
    else:
        raise AssertionError("an unoffered label was accepted")


def test_a_question_answered_with_nothing_is_refused():
    try:
        _ = render_answer([_spec("Which database?", ["Postgres"])], [([], "   ")])
    except AnswerError as exc:
        assert "nothing was answered" in str(exc)
    else:
        raise AssertionError("an empty reply was accepted")


# --- reading the reply back ---------------------------------------------------------

# One case per shape the renderer can write, since the round trip is only as good as its
# least-exercised branch: a bare selection, prose instead of a selection, both together,
# several selections, and a label that contains the separator the selections are joined on.
ROUND_TRIPS: list[tuple[list[QuestionSpec], list[tuple[list[str], str | None]]]] = [
    ([_spec("Which database?", ["Postgres", "SQLite"])], [(["SQLite"], None)]),
    ([_spec("Which database?", ["Postgres"])], [([], "DuckDB, actually")]),
    ([_spec("Which database?", ["Postgres"])], [(["Postgres"], "with PostGIS")]),
    (
        [_spec("Which extras?", ["Auth", "Billing"], multi=True)],
        [(["Auth", "Billing"], None)],
    ),
    (
        [_spec("Which order?", ["Postgres, then MySQL", "MySQL"], multi=True)],
        [(["Postgres, then MySQL", "MySQL"], "in that order")],
    ),
    (
        [_spec("Which database?", ["Postgres", "SQLite"]), _spec("Which extras?", ["Auth"])],
        [(["Postgres"], None), ([], "none of them")],
    ),
]


def test_render_and_parse_are_a_round_trip():
    """The two halves are one format, and the cold transcript is read back with the
    parser while the live stream is built from the replies — so a wording change on one
    side that the other doesn't know about is a card that silently empties on reload."""
    for questions, replies in ROUND_TRIPS:
        parsed = parse_answer(questions, render_answer(questions, replies))
        assert [a.question for a in parsed] == [q.question for q in questions]
        assert [a.selections for a in parsed] == [list(s) for s, _ in replies]
        assert [a.text for a in parsed] == [(t or "").strip() or None for _, t in replies]


def test_parsing_recovers_what_it_can_from_a_string_it_cannot_read():
    """It runs over history stored long before it existed. An unreadable string costs the
    operator the card, never the transcript."""
    assert parse_answer([], "") == []
    assert parse_answer([], "nothing that was ever rendered") == []
    # A question whose answer line was lost still reports the question.
    [only] = parse_answer([], "Q: Which database?")
    assert (only.question, only.selections, only.text) == ("Which database?", [], None)
    # An option renamed since the answer was written is no longer in the offered set, so
    # the separator is all there is to go on — the label still comes back.
    [renamed] = parse_answer([_spec("Which?", ["Postgres 17"])], "Q: Which?\nA: Postgres 16")
    assert renamed.selections == ["Postgres 16"]


def test_a_reply_per_question_is_required():
    try:
        _ = render_answer([_spec("a", ["x"]), _spec("b", ["y"])], [(["x"], None)])
    except AnswerError as exc:
        assert "one reply per question" in str(exc)
    else:
        raise AssertionError("a short reply list was accepted")


# --- over HTTP ---------------------------------------------------------------------


def _ask_and_danger_categories() -> dict[str, AbstractToolset[RunDeps]]:
    """The `ask_user` tool plus one approval-gated tool, so a single turn can stop for
    both reasons at once."""
    danger: FunctionToolset[RunDeps] = FunctionToolset()

    @danger.tool_plain(requires_approval=True)
    def delete_thing(name: str) -> str:
        return f"deleted {name}"

    return {"builtin": builtin_toolset(), "danger": danger}


def _install_asking_model(
    monkeypatch: pytest.MonkeyPatch, args: Mapping[str, object], *, also_danger: bool = False
) -> None:
    """Point every model resolution at a model that makes the call(s) under test once,
    then answers with text."""

    async def stream_fn(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> AsyncIterator[DeltaToolCalls | str]:
        if _tool_ran(messages):
            yield "done"
            return
        yield {0: DeltaToolCall(name=ASK, json_args=json.dumps(args))}
        if also_danger:
            yield {
                1: DeltaToolCall(name="danger_delete_thing", json_args=json.dumps({"name": "x"}))
            }

    async def fake_resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        return await stub_resolution(self, FunctionModel(stream_function=stream_fn))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", fake_resolve_detailed)


async def _chat(client: httpx.AsyncClient, prompt: str) -> str:
    return s(json_body(await client.post("/chat", json={"prompt": prompt}))["run_id"])


def _run(app: FastAPI, run_id: str) -> Run:
    run = app_state(app).runs.get(run_id)
    assert run is not None
    return run


async def test_asking_parks_the_run_and_announces_the_questions(monkeypatch: pytest.MonkeyPatch):
    _install_asking_model(monkeypatch, QUESTIONS)
    async with client_app() as (client, app):
        swap_tool_catalog(app, {"builtin": builtin_toolset()})
        run_id = await _chat(client, "pick one")
        run = await await_parked(app, run_id)
        call_id = parked_turn(run).requests.calls[0].tool_call_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "answers": [
                    {
                        "tool_call_id": call_id,
                        "replies": [
                            {"selections": ["Postgres"]},
                            {"selections": ["Auth"], "text": "and rate limiting"},
                        ],
                    }
                ]
            },
        )
        assert resp.status_code == 202
        events = await collect_sse_events(client, run_id)

    asked = [e for e in events if e["type"] == "question.asked"]
    assert len(asked) == 1
    questions = arr(asked[0]["questions"])
    assert [obj(q)["question"] for q in questions] == ["Which database?", "Which extras?"]
    assert obj(questions[1])["multi_select"] is True
    # The answer rides back as the call's own result, so the transcript's record of it is
    # the ordinary tool block rather than anything written by hand.
    completed = [e for e in events if e["type"] == "tool.completed" and e["name"] == ASK]
    assert completed and "Postgres" in str(completed[0]["result"])
    assert [e["type"] for e in events][-1] == "run.ended"


async def test_the_answers_payload_agrees_warm_and_cold(monkeypatch: pytest.MonkeyPatch):
    """The card is drawn twice from two different sources — live from
    ``question.answered``, built out of the replies before they were ever rendered, and on
    a reload from the call's own arguments and the prose in its result. They must say the
    same thing, or the panel changes under the operator when they refresh the page."""
    _install_asking_model(monkeypatch, QUESTIONS)
    async with client_app() as (client, app):
        swap_tool_catalog(app, {"builtin": builtin_toolset()})
        created = json_body(await client.post("/chat", json={"prompt": "pick one"}))
        run_id, conversation_id = s(created["run_id"]), s(created["conversation_id"])
        run = await await_parked(app, run_id)
        call_id = parked_turn(run).requests.calls[0].tool_call_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "answers": [
                    {
                        "tool_call_id": call_id,
                        "replies": [
                            {"selections": ["Postgres"], "text": "  with PostGIS  "},
                            {"selections": ["Auth", "Billing"]},
                        ],
                    }
                ]
            },
        )
        assert resp.status_code == 202
        events = await collect_sse_events(client, run_id)

        [live] = [e for e in events if e["type"] == "question.answered"]
        assert live["tool_call_id"] == call_id
        assert live["answers"] == [
            {
                "question": "Which database?",
                "selections": ["Postgres"],
                # Trimmed on the way in, so the card never carries the composer's padding.
                "text": "with PostGIS",
            },
            {"question": "Which extras?", "selections": ["Auth", "Billing"], "text": None},
        ]

        # …and again with nothing left in memory to read it from: the sealed blob holds
        # the call and its result, and the projection takes the same answer back out.
        store = app_state(app).conversations
        await store.flush()
        store.evict_cache()
        detail = json_body(await client.get(f"/conversations/{conversation_id}"))
        tools = [
            obj(tool) for message in arr(detail["messages"]) for tool in arr(obj(message)["tools"])
        ]
        [cold] = [tool for tool in tools if tool["name"] == ASK]
        assert cold["answers"] == live["answers"]

        # Every other tool row stays empty — this is a card for one tool, not a field
        # every call now has to mean something by.
        assert all(tool["answers"] == [] for tool in tools if tool["name"] != ASK)


async def test_an_approval_and_a_question_park_once_and_resume_once(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_asking_model(monkeypatch, QUESTIONS, also_danger=True)
    async with client_app() as (client, app):
        swap_tool_catalog(app, _ask_and_danger_categories())
        run_id = await _chat(client, "go")
        run = await await_parked(app, run_id)
        parked = parked_turn(run)
        assert len(parked.requests.calls) == 1
        assert len(parked.requests.approvals) == 1

        question_id = parked.requests.calls[0].tool_call_id
        approval_id = parked.requests.approvals[0].tool_call_id

        # Half a body is refused: the run resumes once, so a body that settled only the
        # approval would continue a turn whose question still had no answer.
        half = await client.post(
            f"/runs/{run_id}/approve",
            json={"decisions": [{"tool_call_id": approval_id, "approved": True}]},
        )
        assert half.status_code == 400
        assert question_id in half.json()["detail"]

        whole = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [{"tool_call_id": approval_id, "approved": True}],
                "answers": [
                    {
                        "tool_call_id": question_id,
                        "replies": [{"selections": ["SQLite"]}, {"selections": ["Billing"]}],
                    }
                ],
            },
        )
        assert whole.status_code == 202
        events = await collect_sse_events(client, run_id)

    types = [e["type"] for e in events]
    assert types.count("question.asked") == 1
    assert types.count("approval.required") == 1
    assert types[-1] == "run.ended"


async def test_a_call_settled_by_the_wrong_pile_is_refused(monkeypatch: pytest.MonkeyPatch):
    """An approval answered as if it were a question — or the reverse — is a 400, not a
    crash. The two piles are settled by different code and looked up in different maps,
    so a body checked only against their *union* would pass here and then index the map
    the id is not in."""
    _install_asking_model(monkeypatch, QUESTIONS, also_danger=True)
    async with client_app() as (client, app):
        swap_tool_catalog(app, _ask_and_danger_categories())
        run_id = await _chat(client, "go")
        parked = parked_turn(await await_parked(app, run_id))
        question_id = parked.requests.calls[0].tool_call_id
        approval_id = parked.requests.approvals[0].tool_call_id

        swapped = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "decisions": [{"tool_call_id": question_id, "approved": True}],
                "answers": [{"tool_call_id": approval_id, "replies": NO_REPLIES}],
            },
        )
        assert swapped.status_code == 400
        # Still parked and still answerable — a refused body settles nothing.
        assert _run(app, run_id).status == "awaiting_input"
        _ = await client.post(f"/runs/{run_id}/cancel")


async def test_an_option_that_was_never_offered_is_refused_over_http(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_asking_model(monkeypatch, QUESTIONS)
    async with client_app() as (client, app):
        swap_tool_catalog(app, {"builtin": builtin_toolset()})
        run_id = await _chat(client, "pick one")
        run = await await_parked(app, run_id)
        call_id = parked_turn(run).requests.calls[0].tool_call_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "answers": [
                    {
                        "tool_call_id": call_id,
                        "replies": [{"selections": ["MongoDB"]}, {"selections": ["Auth"]}],
                    }
                ]
            },
        )
        assert resp.status_code == 422
        assert "MongoDB" in resp.json()["detail"]
        # Refused, not resumed: the run is still parked and still answerable.
        assert _run(app, run_id).status == "awaiting_input"
        _ = await client.post(f"/runs/{run_id}/cancel")


async def test_a_question_answered_with_nothing_is_refused_over_http(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_asking_model(monkeypatch, QUESTIONS)
    async with client_app() as (client, app):
        swap_tool_catalog(app, {"builtin": builtin_toolset()})
        run_id = await _chat(client, "pick one")
        run = await await_parked(app, run_id)
        call_id = parked_turn(run).requests.calls[0].tool_call_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "answers": [
                    {
                        "tool_call_id": call_id,
                        "replies": [
                            {"selections": NO_SELECTIONS, "text": ""},
                            {"selections": ["Auth"]},
                        ],
                    }
                ]
            },
        )
        assert resp.status_code == 422
        _ = await client.post(f"/runs/{run_id}/cancel")


async def test_writing_an_answer_instead_of_choosing_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """The affordance the whole feature turns on: the options are the model's guesses,
    not the range of allowed answers."""
    _install_asking_model(monkeypatch, QUESTIONS)
    async with client_app() as (client, app):
        swap_tool_catalog(app, {"builtin": builtin_toolset()})
        run_id = await _chat(client, "pick one")
        run = await await_parked(app, run_id)
        call_id = parked_turn(run).requests.calls[0].tool_call_id

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={
                "answers": [
                    {
                        "tool_call_id": call_id,
                        "replies": [
                            {"selections": NO_SELECTIONS, "text": "DuckDB, actually"},
                            {"selections": ["Auth"]},
                        ],
                    }
                ]
            },
        )
        assert resp.status_code == 202
        events = await collect_sse_events(client, run_id)

    completed = [e for e in events if e["type"] == "tool.completed" and e["name"] == ASK]
    assert completed and "DuckDB, actually" in str(completed[0]["result"])


async def test_answering_a_run_that_is_not_parked_conflicts(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve_detailed(self: ModelRegistry, _role: str, **_kwargs: object):
        # `call_tools=[]` or the model calls every tool it is offered — including
        # `ask_user`, which would park the very run this test needs to have finished.
        return await stub_resolution(self, TestModel(custom_output_text="done", call_tools=[]))

    register_stub_provider(monkeypatch)
    monkeypatch.setattr(ModelRegistry, "resolve_detailed", fake_resolve_detailed)
    async with client_app() as (client, app):
        swap_tool_catalog(app, {"builtin": builtin_toolset()})
        run_id = await _chat(client, "hello")
        await asyncio.wait_for(_run(app, run_id).wait(), timeout=10)

        resp = await client.post(
            f"/runs/{run_id}/approve",
            json={"answers": [{"tool_call_id": "nope", "replies": NO_REPLIES}]},
        )
        assert resp.status_code == 409
