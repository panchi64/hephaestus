"""A batch of approved shell calls: a `cd` anywhere before a call moves where it starts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from pydantic_ai.messages import ToolCallPart

import routes.runs as runs_routes
from agent import ParkedTurn
from core.config import get_settings
from routes.runs import ApprovalDecision, ApprovalDecisions
from tools import RunDeps

_SCRATCH = "/home/agent/scratch"


def _call(index: int, command: str) -> ToolCallPart:
    return ToolCallPart(
        tool_name="shell_run_command", args={"command": command}, tool_call_id=f"c{index}"
    )


async def _guarded(
    monkeypatch: pytest.MonkeyPatch,
    approvals: list[ToolCallPart],
    decisions: list[ApprovalDecision],
) -> set[str]:
    async def no_home(_deps: RunDeps) -> None:
        return None

    monkeypatch.setattr(runs_routes, "home_of", no_home)

    def scratch(_deps: RunDeps) -> str:
        return _SCRATCH

    monkeypatch.setattr(runs_routes, "shell_cwd", scratch)
    parked = SimpleNamespace(
        compaction=SimpleNamespace(deps=object()),
        requests=SimpleNamespace(approvals=approvals, metadata={}),
    )
    return await runs_routes.guarded_approvals(
        ApprovalDecisions(decisions=decisions),
        cast(ParkedTurn, cast(object, parked)),
        get_settings(),
    )


def _approve(index: int, command: str | None = None) -> ApprovalDecision:
    override: dict[str, object] | None = None if command is None else {"command": command}
    return ApprovalDecision(tool_call_id=f"c{index}", approved=True, override_args=override)


async def test_a_command_after_an_approved_cd_is_not_placed(monkeypatch: pytest.MonkeyPatch):
    guarded = await _guarded(
        monkeypatch,
        [_call(1, "cd ~/projects"), _call(2, "ls")],
        [_approve(1), _approve(2, "rm -rf api")],
    )

    assert guarded == {"c2"}


async def test_a_command_after_a_settled_cd_is_not_placed_either(monkeypatch: pytest.MonkeyPatch):
    # The cd needed no answer, so it is absent from the decisions; it runs all the same.
    guarded = await _guarded(
        monkeypatch,
        [_call(1, "cd ~/projects"), _call(2, "ls")],
        [_approve(2, "rm -rf .")],
    )

    assert guarded == {"c2"}


async def test_an_override_with_no_cd_before_it_stays_ordinary(monkeypatch: pytest.MonkeyPatch):
    guarded = await _guarded(
        monkeypatch,
        [_call(1, "ls"), _call(2, "ls")],
        [_approve(1), _approve(2, "echo hi")],
    )

    assert guarded == set()
