"""The kind of ask a parked run is waiting on: the derivation and the routes that carry it."""

from __future__ import annotations

import pytest
from pydantic_ai import DeferredToolRequests, ToolCallPart

from agent import AwaitingKind, ParkedTurn, awaiting_kind
from routes.deps import app_state
from runs import Run, RunStream
from services.answers import ASK_USER_TOOL

from ._helpers import client_app
from ._json import json_body, json_list, obj


def _parked(*, approvals: tuple[str, ...] = (), questions: int = 0) -> ParkedTurn:
    return ParkedTurn(
        agent=None,
        message_history=[],
        requests=DeferredToolRequests(
            approvals=[
                ToolCallPart(name, {}, tool_call_id=f"a{i}") for i, name in enumerate(approvals)
            ],
            calls=[ToolCallPart(ASK_USER_TOOL, {}, tool_call_id=f"q{i}") for i in range(questions)],
        ),
    )


def _run(payload: object | None, *, park: bool = True) -> Run:
    run = Run(id="r", kind="chat", owner_id="operator", stream=RunStream())
    if park:
        run.park(payload)
    return run


def test_not_parked_has_no_kind():
    assert awaiting_kind(_run(None, park=False)) is None


def test_question_call_makes_a_question():
    assert awaiting_kind(_run(_parked(questions=1))) == "question"


def test_approvals_only_make_an_approval():
    assert awaiting_kind(_run(_parked(approvals=("danger_rm",)))) == "approval"


def test_a_question_among_approvals_is_a_question():
    assert awaiting_kind(_run(_parked(approvals=("danger_rm",), questions=1))) == "question"


def test_a_park_without_a_payload_is_an_approval():
    assert awaiting_kind(_run(None)) == "approval"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(_parked(questions=1), "question"), (_parked(approvals=("danger_rm",)), "approval")],
)
async def test_routes_carry_the_kind(payload: ParkedTurn, expected: AwaitingKind):
    async with client_app() as (client, app):
        conversations = app_state(app).conversations
        conversation_id = await conversations.create_conversation("operator")

        async def orch(run: Run) -> None:
            run.park(payload)

        runs = app_state(app).runs
        run = runs.submit(
            kind="chat", owner_id="operator", orchestrator=orch, conversation_id=conversation_id
        )
        await run.wait()

        listed = obj(json_list(await client.get("/runs"))[0])
        assert listed["awaiting_kind"] == expected
        assert obj(json_body(await client.get(f"/runs/{run.id}")))["awaiting_kind"] == expected

        rows = json_list(await client.get("/conversations"))
        assert obj(rows[0])["awaiting_kind"] == expected
        detail = obj(json_body(await client.get(f"/conversations/{conversation_id}")))
        assert detail["awaiting_kind"] == expected

        assert await runs.cancel(run.id) is True
        assert obj(json_list(await client.get("/conversations"))[0])["awaiting_kind"] is None
