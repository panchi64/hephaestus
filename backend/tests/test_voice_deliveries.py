"""What an unattended eval may approve, and how sub-agent report deliveries are captured."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from devkit.instance import DevInstance
from evals.drive.client import DevClient, refusal
from evals.voice.collect import Collector
from evals.voice.document import render
from evals.voice.record import Approval, Run, Sample, Timeline, Turn
from evals.voice.spec import Scenario
from runs.events.approvals import ApprovalRequired
from runs.events.lifecycle import AnswerDelta, RunEnded
from runs.events.union import EventBody


def _call(name: str, **args: object) -> ApprovalRequired:
    return ApprovalRequired(tool_call_id="c", name=name, args=args, summary="s")


@pytest.mark.parametrize(
    ("call", "approved"),
    [
        (_call("shell_run_command", command="rm x", reach="workspace"), True),
        (_call("files_write_file", path="a.py"), True),
        (_call("shell_run_command", command="open -a Docker", reach="host"), False),
        (_call("code_run_host_command", command="ls ~"), False),
        (_call("mail_send", to="someone@example.com"), False),
    ],
)
def test_only_vm_bound_calls_are_approved(call: ApprovalRequired, approved: bool) -> None:
    assert (refusal(call, approve=True) is None) is approved


def test_an_approve_none_scenario_refuses_everything() -> None:
    assert refusal(_call("files_write_file"), approve=False) is not None


def test_the_document_shows_a_refused_call() -> None:
    turn = Turn(prompt="why is docker dying")
    Timeline(turn).feed(_call("code_run_host_command", command="ls ~"))
    approval = turn.segments[0]
    assert isinstance(approval, Approval) and approval.refused
    scenario = Scenario.model_validate(
        {
            "id": "s",
            "voice": "talos",
            "about": "a",
            "prompts": ["p"],
            "dimensions": ["judgment"],
            "expect": ["e"],
        }
    )
    run = Run(
        group="g",
        name="A",
        started_at="now",
        prompt_fingerprint="f",
        stub=False,
        samples=[Sample(scenario="s", sample=1, turns=[turn])],
    )
    assert "(refused by the eval:" in render(run, [scenario])


def _sse(*bodies: EventBody) -> str:
    return "".join(
        f"data: {json.dumps({**body.model_dump(mode='json'), 'seq': seq})}\n\n"
        for seq, body in enumerate(bodies, start=1)
    )


async def test_a_wake_run_is_recorded_as_a_delivery_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evals.drive.instance._DELIVERY_POLL_S", 0.0)
    wake = {
        "id": "w1",
        "kind": "wake",
        "status": "done",
        "owner_id": "o",
        "created_at": "2026-10-17T12:00:00Z",
        "last_seq": 2,
        "conversationId": "c1",
    }
    other = {**wake, "id": "w2", "conversationId": "elsewhere"}

    def handle(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/runs":
                return httpx.Response(200, json=[wake, other])
            case "/runs/w1/events":
                reply = AnswerDelta(text="The reviewer found two bugs.")
                return httpx.Response(200, text=_sse(reply, RunEnded(outcome="done")))
            case "/conversations/c1/subagents":
                card = {"handle": "rev", "task": "review", "status": "done", "summary": "Two."}
                return httpx.Response(200, json={"subagents": [card]})
            case "/conversations/c1/inbox":
                return httpx.Response(200, json={"messages": [], "agents_paused": False})
            case _:
                return httpx.Response(404)

    client = DevClient("http://dev", transport=httpx.MockTransport(handle))
    collector = Collector(client, DevInstance(name="t", slot=1, root=tmp_path, password=""))
    scenario = Scenario.model_validate(
        {
            "id": "delegate",
            "voice": "subagent",
            "about": "a",
            "prompts": ["p"],
            "dimensions": ["report"],
            "expect": ["e"],
            "requires_vm": True,
        }
    )
    record = Sample(scenario="delegate", sample=1, conversation_id="c1")
    await collector.deliveries(scenario, record, "c1")
    await client.aclose()

    assert [(t.delivery, t.final_reply()) for t in record.turns] == [
        (True, "The reviewer found two bugs.")
    ]
    assert [(a.status, a.report) for a in record.subagents] == [("done", "Two.")]
    assert record.problem is None
