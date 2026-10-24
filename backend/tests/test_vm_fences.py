"""When the conversation VM counts as a fence for the structural stage: only with no network."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ToolCallPart

import agent.gating as gating
from core.container import ServiceContainer
from runs import Run, RunStream
from services.egress import EgressPolicy
from services.home.backend import Mount
from services.home.paths import SCRATCH
from services.home.table import RunHome
from services.permissions import ReviewVerdict
from services.permissions.reviewer import Reviewer, ReviewRequest, ReviewUnavailable, ScoredVerdict
from services.vm import ConversationMachines, GuestImage, Platform
from tools import RunDeps

from .conftest import egress_policy
from .vm_support import install_fake_smol, ready_platform

OWNER = "operator"
CONV = "conv-1"


def _machines(platform: Platform) -> ConversationMachines:
    return ConversationMachines(
        platform,
        prefix="heph-test",
        cpus=1,
        memory_mb=256,
        idle_ttl_s=3600.0,
        reap_interval_s=3600.0,
        max_machines=1,
    )


def _deps(machines: ConversationMachines | None, policy: EgressPolicy | None) -> RunDeps:
    caps = ServiceContainer()
    if machines is not None:
        caps.add(machines, as_type=ConversationMachines)
    if policy is not None:
        caps.add(policy, as_type=EgressPolicy)
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    return RunDeps(run=run, owner_id=OWNER, caps=caps, workspace_key=CONV)


def _call(tool: str, args: dict[str, object]) -> ToolCallPart:
    return ToolCallPart(tool_name=tool, args=args, tool_call_id="c1")


LS = _call("shell_run_command", {"command": "ls src"})


class TestWhatCountsAsAFence:
    async def test_a_vm_with_no_allowed_host_is_a_fence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        machines = _machines(await ready_platform(tmp_path))
        assert await gating.vm_fences(_deps(machines, egress_policy(())), [LS])

    async def test_a_vm_that_can_reach_a_host_is_not(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        machines = _machines(await ready_platform(tmp_path))
        deps = _deps(machines, egress_policy(("api.github.com",)))
        assert not await gating.vm_fences(deps, [LS])

    async def test_a_batch_asking_for_egress_is_not(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        machines = _machines(await ready_platform(tmp_path))
        deps = _deps(machines, egress_policy(()))
        request = _call("code_request_egress", {"domains": ["pypi.org"]})
        assert not await gating.vm_fences(deps, [LS, request])

    async def test_a_vm_that_is_down_or_missing_is_not(self, tmp_path: Path):
        down = _machines(Platform(GuestImage(tmp_path / "images")))
        assert not down.platform.available
        assert not await gating.vm_fences(_deps(down, egress_policy(())), [LS])
        assert not await gating.vm_fences(_deps(None, egress_policy(())), [LS])


class TestAtAutoTheFenceDecidesWhoReviews:
    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[ReviewRequest]:
        seen: list[ReviewRequest] = []

        async def reviewer(request: ReviewRequest) -> ScoredVerdict | ReviewUnavailable:
            seen.append(request)
            return ScoredVerdict(ReviewVerdict(risk="low", authorization="neutral"), {})

        async def resolve(_caps: ServiceContainer, _owner_id: str) -> Reviewer | None:
            return reviewer

        async def home(_deps: RunDeps) -> RunHome:
            return RunHome(
                mounts=(Mount(SCRATCH, tmp_path, writable=True),), default=SCRATCH, key=CONV
            )

        monkeypatch.setattr(gating, "resolve_reviewer", resolve)
        monkeypatch.setattr(gating, "resolve_run_home", home)
        return seen

    async def _settle(self, deps: RunDeps) -> list[ToolCallPart]:
        _settled, manual = await gating.settle_deferred(
            deps.run,
            [LS],
            caps=deps.caps,
            conversation_id=CONV,
            deps=deps,
            messages=[],
            permission="auto",
        )
        return manual

    async def test_a_contained_command_is_cleared_without_a_reviewer_when_offline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        seen = self._install(monkeypatch, tmp_path)
        machines = _machines(await ready_platform(tmp_path))
        assert await self._settle(_deps(machines, egress_policy(()))) == []
        assert seen == []

    async def test_the_same_command_goes_to_the_reviewer_when_the_vm_has_egress(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        seen = self._install(monkeypatch, tmp_path)
        machines = _machines(await ready_platform(tmp_path))
        assert await self._settle(_deps(machines, egress_policy(("api.github.com",)))) == []
        assert [request.capability.tool for request in seen] == ["shell_run_command"]
