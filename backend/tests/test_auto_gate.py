"""The Auto level: what the offline VM bounds runs, what a standing grant covers runs, and
everything else parks with its reason."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import RunContext, ToolApproved, ToolDenied
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

import agent.gating as gating
import tools.home_workspace as home_workspace
from agent.gating import EnvApproved, GrantApproved
from core.container import ServiceContainer
from core.db import init_db, make_engine
from runs import Run, RunStream
from services.approval_grants import ApprovalGrantStore
from services.egress import EgressPolicy
from services.home.backend import Mount
from services.home.paths import SCRATCH, Place
from services.home.table import RunHome
from services.permissions import (
    AUTO_KEY,
    AutoEnv,
    Decision,
    auto_decision,
    environment_clears,
)
from services.vm import ConversationMachines, GuestImage, Platform
from tools import RunDeps
from tools.home_workspace import env_still_clears, still_bounded

from .conftest import egress_policy
from .vm_support import install_fake_smol, ready_platform

OWNER = "operator"
CONV = "conv-1"

OFFLINE = AutoEnv(vm_up=True, offline=True, writable_checkout=False)


class TestTheRule:
    @pytest.mark.parametrize("tool", ["shell_run_command", "shell_start_command", "code_execute"])
    def test_vm_work_clears_while_the_vm_is_up_offline_and_holds_no_checkout(self, tool: str):
        assert environment_clears(tool, OFFLINE) == (True, "runs in the offline VM")

    @pytest.mark.parametrize(
        ("env", "reason"),
        [
            (AutoEnv(vm_up=False, offline=True, writable_checkout=False), "the VM is unavailable"),
            (
                AutoEnv(vm_up=True, offline=False, writable_checkout=False),
                "the VM can reach the network",
            ),
            (
                AutoEnv(vm_up=True, offline=True, writable_checkout=True),
                "this thread's own checkout is writable",
            ),
        ],
    )
    def test_vm_work_parks_when_the_vm_is_no_fence(self, env: AutoEnv, reason: str):
        assert environment_clears("shell_run_command", env) == (False, reason)

    def test_the_host_and_everything_outside_the_vm_park(self):
        assert environment_clears("shell_run_on_host", OFFLINE) == (False, "runs on the host")
        assert environment_clears("mail_send", OFFLINE) == (False, "acts outside the VM")
        # An unknown name resolves to the class that reaches furthest, never to a read.
        assert environment_clears("external_x_read", OFFLINE) == (False, "acts outside the VM")

    def test_a_classified_read_clears_wherever_the_vm_stands(self):
        down = AutoEnv(vm_up=False, offline=False, writable_checkout=True)
        assert environment_clears("browse_navigate", down) == (True, "only reads")

    def test_a_grant_clears_what_the_environment_does_not(self):
        outcome = auto_decision("shell_run_on_host", env=OFFLINE, granted=True)
        assert outcome.decision is Decision.ALLOW and outcome.by_grant
        assert not outcome.by_environment
        parked = auto_decision("shell_run_on_host", env=OFFLINE, granted=False)
        assert parked.decision is Decision.ASK and parked.reason == "runs on the host"

    def test_only_vm_work_is_marked_for_the_execution_check(self):
        assert auto_decision("shell_run_command", env=OFFLINE, granted=False).by_environment
        assert not auto_decision("browse_navigate", env=OFFLINE, granted=False).by_environment


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


def _deps(
    machines: ConversationMachines | None,
    policy: EgressPolicy | None,
    grants: ApprovalGrantStore | None = None,
) -> RunDeps:
    caps = ServiceContainer()
    if machines is not None:
        caps.add(machines, as_type=ConversationMachines)
    if policy is not None:
        caps.add(policy, as_type=EgressPolicy)
    if grants is not None:
        caps.add(grants)
    run = Run(id="r1", kind="chat", owner_id=OWNER, stream=RunStream())
    return RunDeps(run=run, owner_id=OWNER, caps=caps, workspace_key=CONV, permission="auto")


def _grants() -> ApprovalGrantStore:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return ApprovalGrantStore(engine, 3600)


def _call(tool: str, command: str, call_id: str = "c1") -> ToolCallPart:
    return ToolCallPart(tool_name=tool, args={"command": command}, tool_call_id=call_id)


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, checkout: bool = False) -> None:
    mounts = [Mount(SCRATCH, tmp_path, writable=True)]
    if checkout:
        mounts.append(Mount(Place("projects", "app"), tmp_path / "app", writable=True))

    async def home(_deps: RunDeps) -> RunHome:
        return RunHome(mounts=tuple(mounts), default=SCRATCH, key=CONV)

    monkeypatch.setattr(home_workspace, "resolve_run_home", home)


async def _settle(
    deps: RunDeps, calls: list[ToolCallPart]
) -> tuple[dict[str, ToolApproved | ToolDenied], list[ToolCallPart], dict[str, dict[str, object]]]:
    metadata: dict[str, dict[str, object]] = {}
    settled, manual = await gating.settle_deferred(
        deps.run,
        calls,
        caps=deps.caps,
        conversation_id=CONV,
        deps=deps,
        permission="auto",
        metadata=metadata,
    )
    return settled, manual, metadata


LS = _call("shell_run_command", "ls src")


class TestTheGate:
    async def test_vm_work_runs_when_the_vm_is_offline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        _home(monkeypatch, tmp_path)
        deps = _deps(_machines(await ready_platform(tmp_path)), egress_policy(()))
        # The commands the old syntax judge parked on run here like any other.
        calls = [
            LS,
            _call("shell_run_command", 'rg "foo|bar" src', "c2"),
            _call("shell_run_command", "cat <<'EOF' > notes.md\nhi\nEOF", "c3"),
        ]
        settled, manual, _ = await _settle(deps, calls)
        assert manual == []
        assert all(isinstance(settled[c.tool_call_id], EnvApproved) for c in calls)
        assert deps.env_cleared == {"c1", "c2", "c3"}

    async def test_vm_work_parks_with_its_reason_when_the_vm_can_reach_a_host(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        _home(monkeypatch, tmp_path)
        deps = _deps(_machines(await ready_platform(tmp_path)), egress_policy(("pypi.org",)))
        _, manual, metadata = await _settle(deps, [LS])
        assert manual == [LS]
        assert metadata["c1"][AUTO_KEY] == "the VM can reach the network"

    async def test_an_egress_request_in_the_batch_counts_as_network(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        _home(monkeypatch, tmp_path)
        deps = _deps(_machines(await ready_platform(tmp_path)), egress_policy(()))
        egress = ToolCallPart(
            tool_name="code_request_egress", args={"domains": ["pypi.org"]}, tool_call_id="c2"
        )
        _, manual, _ = await _settle(deps, [LS, egress])
        assert LS in manual

    async def test_vm_work_parks_without_a_vm(self, tmp_path: Path):
        down = _machines(Platform(GuestImage(tmp_path / "images")))
        _, manual, metadata = await _settle(_deps(down, egress_policy(())), [LS])
        assert manual == [LS]
        assert metadata["c1"][AUTO_KEY] == "the VM is unavailable"

    async def test_vm_work_parks_in_a_thread_with_a_writable_checkout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        _home(monkeypatch, tmp_path, checkout=True)
        deps = _deps(_machines(await ready_platform(tmp_path)), egress_policy(()))
        _, manual, metadata = await _settle(deps, [LS])
        assert manual == [LS]
        assert metadata["c1"][AUTO_KEY] == "this thread's own checkout is writable"

    async def test_a_command_grant_clears_a_host_command_and_no_other(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _home(monkeypatch, tmp_path)
        grants = _grants()
        _ = await grants.grant(OWNER, CONV, "shell_run_on_host", ("uv", "run", "pytest"))
        deps = _deps(None, None, grants)
        granted = _call("shell_run_on_host", "uv run pytest -q", "c1")
        other = _call("shell_run_on_host", "uv run ruff check", "c2")
        settled, manual, metadata = await _settle(deps, [granted, other])
        assert isinstance(settled["c1"], GrantApproved)
        assert manual == [other]
        assert metadata["c2"][AUTO_KEY] == "runs on the host"
        assert deps.env_cleared == set()

    async def test_a_whole_tool_grant_clears_every_call_to_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _home(monkeypatch, tmp_path)
        grants = _grants()
        _ = await grants.grant(OWNER, CONV, "shell_run_on_host")
        settled, manual, _ = await _settle(
            _deps(None, None, grants), [_call("shell_run_on_host", "rm -rf build")]
        )
        assert manual == [] and isinstance(settled["c1"], GrantApproved)


def _ctx(deps: RunDeps, call_id: str) -> RunContext[RunDeps]:
    return RunContext(
        deps=deps, model=TestModel(), usage=RunUsage(), tool_call_id=call_id, messages=[]
    )


class TestTheExecutionCheck:
    async def test_a_cleared_call_does_not_run_once_the_vm_can_reach_a_host(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _ = install_fake_smol(monkeypatch)
        _home(monkeypatch, tmp_path)
        policy = egress_policy(())
        deps = _deps(_machines(await ready_platform(tmp_path)), policy)
        _ = await _settle(deps, [LS])
        _ = await policy.allow(deps.scratch_key, ["pypi.org"])
        assert not await still_bounded(_ctx(deps, "c1"), "shell_run_command")
        # Checked once: the mark is spent either way.
        assert deps.env_cleared == set()

    async def test_a_call_auto_did_not_clear_is_not_checked(self):
        deps = _deps(None, None)
        assert await still_bounded(_ctx(deps, "c9"), "shell_run_command")

    async def test_with_the_run_gone_nothing_can_say_the_vm_still_bounds_it(self):
        assert not await env_still_clears(None, "shell_run_command")
