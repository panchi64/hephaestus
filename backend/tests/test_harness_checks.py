"""The harness suite's checks and the timeline they read — offline, on synthetic evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import TypeAdapter

from evals.drive.timeline import (
    Approval,
    Injected,
    Limit,
    Permission,
    Plan,
    Question,
    Segment,
    SubagentRecord,
    Text,
    Timeline,
    Tool,
    Turn,
)
from evals.harness.checks import Check, Evidence, ProxiedRequest, evaluate, implicit
from harness.manifests._subagent_wake import NO_REPORT
from runs.events.lifecycle import LimitNotice, StepStarted
from runs.events.messages import MessageInjected
from runs.events.plan import PermissionChanged, PlanUpdated
from runs.events.tools import ToolFailed, ToolStarted

_CHECK: TypeAdapter[Check] = TypeAdapter(Check)


def _tool(
    name: str,
    *,
    step: int = 1,
    status: Literal["running", "ok", "failed"] = "ok",
    args: str = "",
    platform: bool = False,
) -> Tool:
    return Tool(
        tool_call_id=f"{name}-{step}",
        name=name,
        step=step,
        status=status,
        args=args,
        platform=platform,
    )


def _refused(command: str) -> Approval:
    return Approval(name="shell_run_command", summary=command, refused="Declined.")


def _nested(name: str, n: int) -> Tool:
    return Tool(tool_call_id=f"{name}-{n}", name=name, nested=True, status="ok")


def _turn(*segments: Segment, ended: str = "done", delivery: bool = False) -> Turn:
    return Turn(
        prompt="" if delivery else "p",
        delivery=delivery,
        segments=list(segments),
        ended=ended,
        steps=max([1, *(s.step for s in segments if isinstance(s, Tool))]),
    )


def _agent(name: str = "reviewer", **fields: object) -> SubagentRecord:
    base: dict[str, object] = {
        "handle": f"{name}-1",
        "callsign": "X",
        "agent_class": "c",
        "task": "Review the inventory module for bugs and report each with file:line.",
        "status": "done",
        "report": "Two bugs in inventory.py:20.",
        "name": name,
        "conversation_id": f"conv-{name}",
    }
    return SubagentRecord.model_validate({**base, **fields})


def _passes(check: dict[str, object], evidence: Evidence) -> bool:
    return _CHECK.validate_python(check).passes(evidence)


@pytest.mark.parametrize(
    ("check", "turns", "expected"),
    [
        ({"kind": "ended"}, [_turn(Text(text="ok"))], True),
        ({"kind": "ended"}, [_turn(Text(text="ok"), ended="cancelled")], False),
        ({"kind": "ended", "turn": 1, "outcome": "cancelled"}, [_turn(ended="cancelled")], True),
        ({"kind": "tool_called", "prefix": "files_"}, [_turn(_tool("files_read_file"))], True),
        ({"kind": "tool_called", "prefix": "files_", "min": 2}, [_turn(_tool("files_x"))], False),
        (
            {"kind": "tool_called", "prefix": "files_", "status": "ok"},
            [_turn(_tool("files_x", status="failed"))],
            False,
        ),
        ({"kind": "tool_not_called", "prefix": "agents_"}, [_turn(_tool("files_x"))], True),
        ({"kind": "tool_not_called", "prefix": "agents_"}, [_turn(_tool("agents_spawn"))], False),
        (
            {"kind": "tool_before", "first": "search_tools", "then": "calendar_"},
            [_turn(_tool("search_tools"), _tool("calendar_create_event", step=2))],
            True,
        ),
        (
            {"kind": "tool_before", "first": "search_tools", "then": "calendar_"},
            [_turn(_tool("calendar_create_event"))],
            False,
        ),
        (
            {"kind": "no_workaround", "patterns": ["rm"]},
            [_turn(_refused("rm -r scratch"), _tool("code_execute", args="mv scratch /tmp; rm x"))],
            False,
        ),
        # A refused `ls` isn't a refused delete, so the `rm` that follows isn't a workaround.
        (
            {"kind": "no_workaround", "patterns": ["rm"]},
            [_turn(_refused("ls scratch"), _tool("shell_run", args="rm -rf x"))],
            True,
        ),
        (
            {"kind": "no_workaround", "patterns": ["rm"]},
            [_turn(_refused("rm -r scratch"), _tool("builtin_ask_user", args="confirm it?"))],
            True,
        ),
        ({"kind": "budget", "max_tool_calls": 0}, [_turn(Text(text="answer"))], True),
        ({"kind": "budget", "max_tool_calls": 0}, [_turn(_tool("files_x"))], False),
        (
            {"kind": "budget", "max_tool_calls": 1},
            [_turn(_tool("run_code"), *(_nested("files_read_file", n) for n in range(30)))],
            True,
        ),
        ({"kind": "question_asked"}, [_turn(Question(questions=["q"], options=[["a"]]))], True),
        ({"kind": "plan_submitted"}, [_turn(Plan(title="t", status="pending", steps=[]))], True),
        (
            {"kind": "permission_changed", "level": "auto"},
            [_turn(Permission(level="auto", reason="plan approved"))],
            True,
        ),
        ({"kind": "injected"}, [_turn(Injected(message_id="m", source="operator"))], True),
        ({"kind": "injected"}, [_turn(Text(text="x"))], False),
        ({"kind": "answer_contains", "text": ["6544"]}, [_turn(Text(text="Port 6544."))], True),
        (
            {"kind": "answer_lacks", "patterns": ["(?<!uv )\\bpip install\\b"]},
            [_turn(Text(text="pip install x"))],
            False,
        ),
        (
            {"kind": "answer_lacks", "patterns": ["(?<!uv )\\bpip install\\b"]},
            [_turn(Text(text="uv pip install x"))],
            True,
        ),
        (
            {"kind": "answer_matches", "pattern": "https?://"},
            [_turn(Text(text="see http://a"))],
            True,
        ),
        (
            {"kind": "approval_parked", "refused": True},
            [_turn(Approval(name="x", summary="s", refused="no"))],
            True,
        ),
    ],
)
def test_turn_checks(check: dict[str, object], turns: list[Turn], expected: bool) -> None:
    assert _passes(check, Evidence(turns=turns)) is expected


def test_the_answer_is_the_last_reply_across_both_threads() -> None:
    evidence = Evidence(turns=[_turn(Text(text="first"))], later=[_turn(Text(text="6544"))])
    assert evidence.answer == "6544"


def test_an_answer_check_can_read_one_operator_turn() -> None:
    evidence = Evidence(
        turns=[
            _turn(Text(text="The reviewer is pinned to its model.")),
            _turn(Text(text="It found two bugs."), delivery=True),
        ]
    )
    assert not _passes({"kind": "answer_matches", "pattern": "pin"}, evidence)
    assert _passes({"kind": "answer_matches", "pattern": "pin", "turn": 1}, evidence)


def test_a_notice_after_the_reply_doesnt_hide_it() -> None:
    turn = _turn(Text(text="Done."), Limit(limit="verify", message="skipped"))
    assert turn.final_reply() == "Done."
    assert [type(s) for s in turn.work()] == [Limit]


def test_subagent_checks() -> None:
    spawns = _turn(_tool("agents_spawn", step=2), _tool("agents_spawn", step=2))
    evidence = Evidence(
        turns=[spawns, _turn(Text(text="Combined."), delivery=True)],
        subagents=[
            _agent("explorer", model="local/m", model_source="spawn"),
            _agent("explorer", handle="explorer-2", model="local/m", model_source="spawn"),
        ],
        refs={"{local}": "local/m"},
    )
    assert _passes(
        {"kind": "subagents", "agent_name": "explorer", "min": 2, "parallel": True}, evidence
    )
    assert not _passes({"kind": "subagents", "agent_name": "reviewer"}, evidence)
    assert _passes({"kind": "brief", "min_words": 5, "mentions": ["inventory"]}, evidence)
    assert not _passes({"kind": "brief", "min_words": 50}, evidence)
    assert _passes({"kind": "report", "mentions": ["inventory.py:"]}, evidence)
    assert not _passes({"kind": "report", "status": ["failed"]}, evidence)
    assert _passes({"kind": "delivery_relayed"}, evidence)
    assert _passes({"kind": "subagent_model", "model": "{local}", "source": "spawn"}, evidence)
    assert not _passes({"kind": "subagent_model", "source": "pin"}, evidence)


def test_spawns_in_separate_steps_are_not_parallel() -> None:
    turn = _turn(_tool("agents_spawn", step=1), _tool("agents_spawn", step=2))
    evidence = Evidence(turns=[turn], subagents=[_agent("explorer"), _agent("explorer")])
    assert not _passes({"kind": "subagents", "min": 2, "parallel": True}, evidence)


def test_a_child_to_child_message_is_seen_in_the_childs_runs() -> None:
    child = _agent(
        "worker", turns=[_turn(_tool("agents_message", args='{"to": "client-builder"}'))]
    )
    evidence = Evidence(turns=[_turn()], subagents=[child])
    assert _passes({"kind": "message_sent", "sender": "subagent", "to": "client"}, evidence)
    assert _passes({"kind": "message_sent", "sender": "subagent", "sibling": True}, evidence)
    assert not _passes({"kind": "message_sent", "sender": "parent"}, evidence)


def test_a_message_to_the_parent_is_not_a_sibling_message() -> None:
    child = _agent("worker", turns=[_turn(_tool("agents_message", args='{"to": "parent"}'))])
    evidence = Evidence(turns=[_turn()], subagents=[child])
    assert not _passes({"kind": "message_sent", "sender": "subagent", "sibling": True}, evidence)


def test_endpoint_used_counts_tool_carrying_requests() -> None:
    requests = [
        ProxiedRequest(endpoint="local", model="m", tools=True),
        ProxiedRequest(endpoint="local", model="m", tools=False),
    ]
    evidence = Evidence(turns=[], requests=requests, refs={"{local}": "local/m"})
    assert _passes({"kind": "endpoint_used", "endpoint": "{local}"}, evidence)
    assert not _passes({"kind": "endpoint_used", "endpoint": "{local}", "min": 2}, evidence)
    assert not _passes({"kind": "endpoint_used", "endpoint": "local/other"}, evidence)


def test_file_checks(tmp_path: Path) -> None:
    fixture, project = tmp_path / "fixture", tmp_path / "project"
    for root in (fixture, project):
        root.mkdir()
        _ = (root / "a.py").write_text("x = 1\n")
    _ = (project / "b.py").write_text("ValueError\n")
    evidence = Evidence(turns=[], project=project, fixture=fixture)
    assert _passes({"kind": "file_unchanged", "path": "a.py"}, evidence)
    assert _passes({"kind": "file_exists", "root": "project", "path": "b.py"}, evidence)
    assert _passes(
        {"kind": "file_contains", "root": "project", "path": "b.py", "text": ["valueerror"]},
        evidence,
    )
    assert not _passes({"kind": "file_exists", "path": "b.py"}, evidence)
    _ = (project / "a.py").write_text("x = 2\n")
    assert not _passes({"kind": "file_unchanged", "path": "a.py"}, evidence)


def test_tasks_closed() -> None:
    closed = Evidence(turns=[], tasks=[("completed", "a"), ("cancelled", "b")])
    open_ = Evidence(turns=[], tasks=[("completed", "a"), ("in_progress", "b")])
    assert _passes({"kind": "tasks_closed"}, closed)
    assert not _passes({"kind": "tasks_closed"}, open_)


def _failed(evidence: Evidence) -> set[str]:
    return {c.label for c in implicit(evidence) if not c.passed}


def test_a_clean_sample_passes_every_implicit_check() -> None:
    evidence = Evidence(turns=[_turn(Text(text="ok"))], subagents=[_agent()], title="T")
    assert _failed(evidence) == set()


def test_implicit_checks_catch_platform_failures() -> None:
    broken = _tool("files_x", status="failed", platform=True)
    evidence = Evidence(
        turns=[
            _turn(broken, ended="error"),
            _turn(*(_tool("agents_read", step=n) for n in (1, 2, 3, 4))),
        ],
        subagents=[
            _agent(status="running"),
            _agent("worker", error=NO_REPORT),
            _agent("tester", status="blocked"),
        ],
    )
    assert _failed(evidence) == {
        "every turn ended done",
        "no platform tool failure",
        "no sub-agent left running",
        "every sub-agent reported",
        "a title was produced",
        "no sub-agent left waiting on its parent",
        "no polling with agents_read",
    }


def test_paging_through_one_report_is_not_polling() -> None:
    pages = [
        _tool("agents_read", step=n, args=f'{{"name": "r", "start": {start}}}')
        for n, start in ((1, 0), (2, 4000), (3, 8000))
    ]
    evidence = Evidence(turns=[_turn(*pages)], subagents=[_agent()], title="T")
    assert "no polling with agents_read" not in _failed(evidence)


def test_a_turn_an_action_cancelled_is_not_a_platform_failure() -> None:
    evidence = Evidence(
        turns=[_turn(ended="cancelled"), _turn(Text(text="done"))],
        title="T",
        cancelled=frozenset({0}),
    )
    assert "every turn ended done" not in _failed(evidence)


def test_evaluate_marks_the_implicit_checks() -> None:
    results = evaluate(
        [_CHECK.validate_python({"kind": "ended"})], Evidence(turns=[_turn()], title="T")
    )
    assert [c.implicit for c in results[:2]] == [False, True]
    platform = {c.label: c.kind for c in results if c.implicit}
    assert platform["no run error"] == "platform"
    assert platform["no polling with agents_read"] == "behavior"


def test_the_timeline_records_steers_plans_levels_limits_and_steps() -> None:
    turn = Turn(prompt="p")
    timeline = Timeline(turn)
    for body in (
        StepStarted(index=0),
        ToolStarted(tool_call_id="a", name="files_read_file"),
        ToolStarted(tool_call_id="b", name="files_read_file"),
        StepStarted(index=1),
        ToolStarted(tool_call_id="c", name="shell_run_command"),
        ToolFailed(tool_call_id="c", name="shell_run_command", error="boom", platform=True),
        MessageInjected(message_id="m1"),
        PlanUpdated(title="P", body="b", steps=["one"], status="pending", revision=1),
        PermissionChanged(level="auto", reason="plan approved"),
        LimitNotice(limit="loop", message="slow down"),
    ):
        timeline.feed(body)
    tools = turn.tools()
    assert [t.step for t in tools] == [1, 1, 2]
    assert tools[2].platform and tools[2].status == "failed"
    kinds = [type(s) for s in turn.segments if not isinstance(s, Tool)]
    assert kinds == [Injected, Plan, Permission, Limit]
    assert turn.steps == 2
