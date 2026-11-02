"""Driving the dev instance: steering a busy turn, mid-run actions, waiting out deliveries
and recording sub-agents' own runs — against a fake transport."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from devkit.instance import DevInstance
from evals.drive.client import DevClient, Policy, Queued, Started, policy_refusal
from evals.drive.instance import Instance
from evals.harness.sample import Actor, Watcher
from evals.harness.spec import Action
from runs.events.approvals import ApprovalRequired
from runs.events.lifecycle import AnswerDelta, RunEnded
from runs.events.tools import ToolStarted
from runs.events.union import EventBody
from tests._json import obj


def _sse(*bodies: EventBody) -> str:
    return "".join(
        f"data: {json.dumps({**body.model_dump(mode='json'), 'seq': seq})}\n\n"
        for seq, body in enumerate(bodies, start=1)
    )


def _run(run_id: str, conversation_id: str, kind: str = "linked") -> dict[str, object]:
    return {
        "id": run_id,
        "kind": kind,
        "status": "done",
        "created_at": "2026-11-01T12:00:00Z",
        "conversationId": conversation_id,
    }


async def test_a_send_into_a_busy_thread_comes_back_queued() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = obj(json.loads(request.content))
        if body.get("conversation_id"):
            return httpx.Response(
                202, json={"conversation_id": "c1", "queued_message_id": "m1", "run_id": "r1"}
            )
        return httpx.Response(202, json={"run_id": "r1", "conversation_id": "c1"})

    client = DevClient("http://dev", transport=httpx.MockTransport(handle))
    assert await client.post("hi", None) == Started("r1", "c1")
    assert await client.post("also this", "c1") == Queued("c1", "m1", "r1")
    with pytest.raises(RuntimeError, match="queued"):
        _ = await client.send("again", "c1")
    await client.aclose()


async def test_actions_fire_once_on_their_trigger(tmp_path: Path) -> None:
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path}")
        if request.url.path == "/chat":
            return httpx.Response(202, json={"conversation_id": "c1", "queued_message_id": "m1"})
        return httpx.Response(202, json={})

    client = DevClient("http://dev", transport=httpx.MockTransport(handle))
    driver = Instance(client, DevInstance(name="t", slot=1, root=tmp_path, password=""))
    log: list[str] = []
    actions = [
        Action(at="first_tool", do="steer", text="Also count lines."),
        Action(at="tool:files_", do="cancel"),
    ]
    actor = Actor(driver, actions, "c1", log)
    actor.start("r1")
    await actor.on_frame(ToolStarted(tool_call_id="a", name="shell_run_command"))
    await actor.on_frame(ToolStarted(tool_call_id="b", name="shell_run_command"))
    await actor.on_frame(ToolStarted(tool_call_id="c", name="files_write_file"))
    actor.stop()
    await client.aclose()

    assert sent == ["POST /chat", "POST /runs/r1/cancel"]
    assert actor.cancelled
    assert log == [
        "steered at first_tool: 'Also count lines.' (queued into the running turn)",
        "cancelled the turn at tool:files_",
    ]


async def test_an_action_on_a_turn_that_already_ended_is_logged_not_raised(
    tmp_path: Path,
) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "run already done"})

    client = DevClient("http://dev", transport=httpx.MockTransport(handle))
    driver = Instance(client, DevInstance(name="t", slot=1, root=tmp_path, password=""))
    log: list[str] = []
    actor = Actor(driver, [Action(at="first_tool", do="cancel")], "c1", log)
    actor.start("r1")
    await actor.on_frame(ToolStarted(tool_call_id="a", name="files_write_file"))
    actor.stop()
    await client.aclose()
    assert not actor.cancelled
    assert log and log[0].startswith("cancel at first_tool failed")


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evals.drive.instance._DELIVERY_POLL_S", 0.0)


async def test_a_blocked_child_with_nothing_due_doesnt_hold_the_wait(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/conversations/c1/subagents":
                card = {"handle": "w", "task": "t", "status": "blocked"}
                return httpx.Response(200, json={"subagents": [card]})
            case "/conversations/c1/inbox":
                return httpx.Response(200, json={"messages": [], "agents_paused": False})
            case "/runs":
                return httpx.Response(200, json=[])
            case _:
                return httpx.Response(404)

    client = DevClient("http://dev", transport=httpx.MockTransport(handle))
    driver = Instance(client, DevInstance(name="t", slot=1, root=tmp_path, password=""))
    found = await driver.wait_deliveries("c1", Policy(), timeout_s=10)
    await client.aclose()
    assert [a.status for a in found.subagents] == ["blocked"]
    assert found.problem is None


async def test_the_delivery_wait_holds_while_the_inbox_has_a_report(tmp_path: Path) -> None:
    inbox_reads = 0
    wake_listed = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal inbox_reads, wake_listed
        match request.url.path:
            case "/conversations/c1/subagents":
                card = {"handle": "rev", "task": "review", "status": "done", "summary": "Two."}
                return httpx.Response(200, json={"subagents": [card]})
            case "/conversations/c1/inbox":
                inbox_reads += 1
                # The report waits in the inbox on the first read; the wake has it by the second.
                pending = [{"held": False}] if inbox_reads == 1 else []
                wake_listed = inbox_reads > 1
                return httpx.Response(200, json={"messages": pending, "agents_paused": False})
            case "/runs":
                return httpx.Response(200, json=[_run("w1", "c1", "wake")] if wake_listed else [])
            case "/runs/w1/events":
                reply = AnswerDelta(text="The reviewer found two bugs.")
                return httpx.Response(200, text=_sse(reply, RunEnded(outcome="done")))
            case _:
                return httpx.Response(404)

    client = DevClient("http://dev", transport=httpx.MockTransport(handle))
    driver = Instance(client, DevInstance(name="t", slot=1, root=tmp_path, password=""))
    found = await driver.wait_deliveries("c1", Policy(), timeout_s=10)
    await client.aclose()

    assert [t.final_reply() for t in found.turns] == ["The reviewer found two bugs."]
    assert found.problem is None


async def test_the_watcher_records_each_sub_agents_own_runs(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/conversations/c1/subagents":
                card = {"handle": "rev", "task": "t", "status": "done", "conversation_id": "child"}
                return httpx.Response(200, json={"subagents": [card]})
            case "/runs":
                return httpx.Response(200, json=[_run("k1", "child"), _run("p1", "c1", "chat")])
            case "/runs/k1/events":
                return httpx.Response(
                    200, text=_sse(AnswerDelta(text="Reported."), RunEnded(outcome="done"))
                )
            case _:
                return httpx.Response(404)

    client = DevClient("http://dev", transport=httpx.MockTransport(handle))
    driver = Instance(client, DevInstance(name="t", slot=1, root=tmp_path, password=""))
    watcher = Watcher(driver, Policy(), timeout_s=10)
    watcher.watch("c1")
    turns = await watcher.finish()
    await client.aclose()

    assert list(turns) == ["child"]
    assert [(t.run_id, t.final_reply()) for t in turns["child"]] == [("k1", "Reported.")]


@pytest.mark.parametrize(
    ("command", "refused"), [("ls -la scratch && git status", False), ("rm -rf scratch", True)]
)
def test_denied_arguments_refuse_only_the_matching_call(command: str, refused: bool):
    policy = Policy(deny_args=(r"\brm\b",), refusal="Declined. Leave it as it is.")
    call = ApprovalRequired(
        tool_call_id="t1", name="shell_run_command", args={"command": command}, summary=""
    )

    expected = "Declined. Leave it as it is." if refused else None
    assert policy_refusal(call, policy) == expected
