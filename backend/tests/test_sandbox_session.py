"""Per-conversation live sandboxes: lazy acquisition, the idle reaper, what a reap
does and does not touch, how a session drives the platform VM, and (with confinement
available) file continuity across calls and across a reap."""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from pathlib import Path

import pytest

import services.sandbox.preview_tokens as preview_tokens
import services.sandbox.session as session_mod
from core.config import Settings
from core.vault import Vault
from services.sandbox import (
    PreviewHandle,
    SandboxError,
    SandboxSession,
    SandboxSessionManager,
    SandboxSpec,
)
from services.sandbox.base import safe_key
from services.sandbox.legacy_seal import adopt_legacy_archive, partial_marker
from services.sandbox.session import proxy_env
from services.sandbox.walk import excluded
from services.vm.driver import VMError
from services.vm.platform import WORKSPACE_PROXY, PlatformVM

from .conftest import egress_policy
from .vm_fakes import FakeVM
from .vm_local import can_confine, local_vm

_EXCLUDES = Settings().sandbox_walk_excludes


async def _vault(tmp_path: Path) -> Vault:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return vault


def _vm(tmp_path: Path, *, available: bool = True) -> FakeVM:
    return FakeVM(tmp_path / "sandbox" / "work", available=available)


def _manager(
    tmp_path: Path,
    vault: Vault,
    *,
    vm: FakeVM | PlatformVM | None = None,
    idle_ttl_s: float = 1800.0,
    max_sessions: int = 8,
) -> SandboxSessionManager:
    vm = vm or _vm(tmp_path)
    return SandboxSessionManager(
        vm,
        vault,
        egress=egress_policy(tmp_path),
        data_dir=tmp_path,
        idle_ttl_s=idle_ttl_s,
        reap_interval_s=60.0,
        excludes=_EXCLUDES,
        max_sessions=max_sessions,
    )


def _legacy_archive(tmp_path: Path, vault: Vault, files: dict[str, str]) -> bytes:
    """An archive in the retired sealing format, whose writer is gone."""
    staging = tmp_path / f"legacy-{len(list(tmp_path.iterdir()))}"
    staging.mkdir(parents=True)
    for rel, text in files.items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(staging.iterdir()):
            tar.add(item, arcname=item.name)
    return vault.encrypt_bytes(buf.getvalue())


def test_safe_key_is_path_safe():
    key = safe_key("conv/../weird id!")
    assert key.startswith("s")
    assert all(c.isalnum() or c in "_.-" for c in key)


def test_excluded_drops_envs_and_caches_only():
    assert excluded(".venv", _EXCLUDES)
    assert excluded("pkg/__pycache__/x.pyc", _EXCLUDES)
    assert excluded("node_modules", _EXCLUDES)
    assert not excluded("analysis.py", _EXCLUDES)
    assert not excluded("output/chart.png", _EXCLUDES)


async def test_write_file_round_trips_through_read_file(tmp_path: Path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")

    session.write_file("attachments/data.csv", b"a,b\n1,2\n")

    assert session.read_file("attachments/data.csv") == b"a,b\n1,2\n"
    assert (session.workspace / "attachments" / "data.csv").read_bytes() == b"a,b\n1,2\n"


async def test_write_file_rejects_a_path_escape(tmp_path: Path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")

    with pytest.raises(SandboxError):
        session.write_file("../escape.txt", b"nope")


async def test_written_file_survives_a_reap(tmp_path: Path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")
    session.write_file("attachments/keep.txt", b"hold onto me")

    await session.shutdown()

    assert session.workspace.exists()
    assert session.read_file("attachments/keep.txt") == b"hold onto me"


async def test_a_reap_keeps_the_dependencies_and_history_a_run_built(tmp_path: Path):
    """Dropping `.venv`, `node_modules`, `dist` or `.git` would discard minutes of build work."""
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")
    work = session.ensure_workspace()
    _ = (work / "analysis.py").write_text("print('hi')")
    for built in (".venv/lib", "node_modules/left-pad", "dist", ".git/refs"):
        (work / built).mkdir(parents=True)
        _ = (work / built / "f").write_text("expensive")

    await session.shutdown()

    for built in (".venv/lib", "node_modules/left-pad", "dist", ".git/refs"):
        assert (work / built / "f").read_text() == "expensive", built
    assert (work / "analysis.py").read_text() == "print('hi')"


async def test_a_reap_writes_no_archive(tmp_path: Path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")
    session.write_file("keep.txt", b"data")

    await session.shutdown()

    assert not session.sealed.exists()


async def test_the_reaper_runs_with_the_vault_locked(tmp_path: Path):
    """Reaping touches no file, so a locked vault must not keep idle guest copies alive."""
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    vault.lock()
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.write_file("f.txt", b"data")

    await manager.sweep()

    assert not manager.sessions
    assert session.read_file("f.txt") == b"data"


async def test_the_cap_is_enforced_with_the_vault_locked(tmp_path: Path):
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    vault.lock()
    manager = _manager(tmp_path, vault, max_sessions=1)
    first = await manager.acquire("conv-a")
    first.write_file("f.txt", b"data")

    second = await manager.acquire("conv-b")

    assert set(manager.sessions) == {safe_key("conv-b")}
    assert second is not first
    assert first.read_file("f.txt") == b"data"


async def test_run_wraps_an_unexpected_error_as_sandbox_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-a")

    def boom() -> None:
        raise ValueError("something deep broke")

    monkeypatch.setattr(session, "_ensure_workspace", boom)
    with pytest.raises(SandboxError):
        _ = await session.run(SandboxSpec(command=["echo", "hi"]))


async def test_adopting_a_damaged_archive_raises_sandbox_error(tmp_path: Path):
    vault = await _vault(tmp_path)
    archive = tmp_path / "old.tar.enc.gz"
    _ = archive.write_bytes(b"not a valid sealed archive")
    with pytest.raises(SandboxError):
        _ = adopt_legacy_archive(archive, tmp_path / "out", vault)


async def test_a_sealing_era_archive_is_adopted_and_deleted(tmp_path: Path):
    vault = await _vault(tmp_path)
    archive = tmp_path / "old.tar.enc.gz"
    _ = archive.write_bytes(_legacy_archive(tmp_path, vault, {"report.md": "the work"}))
    work = tmp_path / "adopted"

    assert adopt_legacy_archive(archive, work, vault) is True

    assert (work / "report.md").read_text() == "the work"
    # A leftover archive would be a stale second copy the next cold open prefers.
    assert not archive.exists()


async def test_an_archive_a_materialised_workspace_outlived_is_retired(tmp_path: Path):
    """A crash between extract and unlink leaves both on disk; adoption is never reached again."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-old")
    session.write_file("live.txt", b"the work since")
    session.sealed.parent.mkdir(parents=True, exist_ok=True)
    _ = session.sealed.write_bytes(_legacy_archive(tmp_path, vault, {"stale.txt": "superseded"}))

    _ = session.ensure_workspace()

    assert not session.sealed.exists()
    assert session.read_file("live.txt") == b"the work since"
    assert not (session.workspace / "stale.txt").exists()


async def test_adopting_nothing_is_the_ordinary_case(tmp_path: Path):
    vault = await _vault(tmp_path)
    assert adopt_legacy_archive(tmp_path / "absent.tar.enc.gz", tmp_path / "w", vault) is False


async def test_a_conversation_opens_its_sealing_era_archive_on_first_use(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-old")
    session.sealed.parent.mkdir(parents=True, exist_ok=True)
    _ = session.sealed.write_bytes(_legacy_archive(tmp_path, vault, {"notes.txt": "from before"}))

    assert session.read_file("notes.txt") == b"from before"
    assert not session.sealed.exists()


async def test_adopting_an_archive_needs_the_key(tmp_path: Path):
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    archive = tmp_path / "old.tar.enc.gz"
    _ = archive.write_bytes(_legacy_archive(tmp_path, vault, {"a.txt": "x"}))
    vault.lock()

    with pytest.raises(SandboxError):
        _ = adopt_legacy_archive(archive, tmp_path / "w", vault)
    assert archive.exists()


async def test_a_fragment_beside_an_archive_is_thrown_away_not_kept(tmp_path: Path):
    """A restore that died mid-extract leaves less than the archive beside it, which wins."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-old")
    session.workspace.mkdir(parents=True, exist_ok=True)
    _ = (session.workspace / "half.txt").write_text("torn")
    partial_marker(session.workspace).touch()
    session.sealed.parent.mkdir(parents=True, exist_ok=True)
    _ = session.sealed.write_bytes(_legacy_archive(tmp_path, vault, {"whole.txt": "all of it"}))

    _ = session.ensure_workspace()

    assert (session.workspace / "whole.txt").read_text() == "all of it"
    assert not (session.workspace / "half.txt").exists()
    assert not partial_marker(session.workspace).exists()


async def test_acquire_is_lazy_and_idempotent_per_key(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    first = await manager.acquire("conv-a")
    again = await manager.acquire("conv-a")
    other = await manager.acquire("conv-b")
    assert first is again
    assert other is not first
    assert not first.workspace.exists()


async def test_a_command_runs_in_the_vm_and_its_files_come_back(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    assert not session.is_warm

    result = await session.run(
        SandboxSpec(command=["sh", "-c", "cat; echo $FOO > out.txt"], stdin="in", env={"FOO": "x"})
    )

    assert result.ok and result.stdout == "in"
    assert session.is_warm
    assert session.read_file("out.txt") == b"x\n"
    key, _argv, env, stdin = vm.runs[0]
    assert (key, stdin, env["FOO"]) == (session.key, "in", "x")
    assert env["HOME"] == "/work/.home"


async def test_a_nonzero_exit_is_a_result_and_a_timeout_is_marked(tmp_path: Path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-a")

    failed = await session.run(SandboxSpec(command=["sh", "-c", "exit 3"]))
    timed_out = await session.run(SandboxSpec(command=["sleep", "5"], timeout_s=0.2))

    assert failed.exit_code == 3 and not failed.timed_out
    assert timed_out.timed_out and not timed_out.ok


async def test_the_workspace_and_credentials_are_in_place_before_the_command_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    real_grant = vm.gateway.grant

    def grant(allow_dir: Path) -> str:
        vm.events.append(("grant", session.key))
        return real_grant(allow_dir)

    monkeypatch.setattr(vm.gateway, "grant", grant)

    _ = await session.run(SandboxSpec(command=["true"]))

    ops = [op for op, _ in vm.events]
    assert ops.index("grant") < ops.index("push") < ops.index("run")
    env = vm.runs[0][2]
    assert env["HTTPS_PROXY"].endswith(f"@{WORKSPACE_PROXY}")
    assert env["https_proxy"] == env["HTTPS_PROXY"]


def test_proxy_env_carries_the_credentials_in_both_spellings():
    env = proxy_env("workspace:tok")
    url = f"http://workspace:tok@{WORKSPACE_PROXY}"
    assert env["HTTP_PROXY"] == env["http_proxy"] == url
    assert env["HTTPS_PROXY"] == env["https_proxy"] == url
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1"


async def test_an_edit_made_on_the_host_between_commands_is_pushed_in(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    _ = await session.run(SandboxSpec(command=["true"]))
    pushes = len(vm.pushes)

    _ = await session.run(SandboxSpec(command=["true"]))
    assert len(vm.pushes) == pushes

    session.write_file("edited.txt", b"x")
    _ = await session.run(SandboxSpec(command=["true"]))
    assert "edited.txt" in vm.pushes[-1][1].changed


async def test_a_rebuilt_vm_gets_the_workspace_again(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    session.write_file("keep.txt", b"x")
    _ = await session.run(SandboxSpec(command=["true"]))

    vm.restart()
    assert not session.is_warm
    _ = await session.run(SandboxSpec(command=["true"]))

    assert session.is_warm
    assert "keep.txt" in vm.pushes[-1][1].changed


async def test_shutdown_wipes_the_guest_copy_and_revokes_the_credentials(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    _ = await session.run(SandboxSpec(command=["true"]))
    credentials = session.credentials
    assert credentials is not None
    assert vm.gateway.grant_for(credentials) is not None

    await session.shutdown()

    assert not session.is_warm
    assert vm.events[-1] == ("wipe", session.key)
    assert vm.gateway.grant_for(credentials) is None
    assert session.credentials is None


async def test_each_run_after_a_reap_is_granted_fresh_credentials(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    _ = await session.run(SandboxSpec(command=["true"]))
    await session.shutdown()
    _ = await session.run(SandboxSpec(command=["true"]))

    first, second = (run[2]["HTTPS_PROXY"] for run in vm.runs)
    assert first != second


async def test_a_vm_that_is_not_up_is_reported_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path, available=False)
    vm.failure = "the platform VM is disabled in settings"
    monkeypatch.setattr(session_mod, "VM_WAIT_S", 0.01)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")

    with pytest.raises(SandboxError, match="disabled in settings"):
        _ = await session.run(SandboxSpec(command=["true"]))
    assert vm.runs == []


async def test_a_vm_failure_surfaces_as_a_sandbox_error(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    vm.fail_with = VMError("the VM stopped answering")

    with pytest.raises(SandboxError, match="stopped answering"):
        _ = await session.run(SandboxSpec(command=["true"]))


async def test_the_manager_reports_a_vm_that_is_still_starting(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path, available=False)
    manager = _manager(tmp_path, vault, vm=vm)
    assert manager.vm_starting

    vm.failure = "boot failed"
    assert not manager.vm_starting

    vm.failure = None
    vm.available = True
    assert not manager.vm_starting


async def test_reaper_drops_an_idle_session_and_keeps_its_files(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.write_file("notes.txt", b"keep me")

    await manager.sweep()

    assert not manager.sessions
    assert session.workspace.exists()

    revived = await manager.acquire("conv-a")
    assert (revived.ensure_workspace() / "notes.txt").read_text() == "keep me"


async def test_start_stop_manages_the_reaper_task(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    await manager.start()
    assert manager.reaper.running
    await manager.stop()
    assert not manager.reaper.running


async def test_reaper_spares_fresh_and_busy_sessions(tmp_path: Path):
    vault = await _vault(tmp_path)
    fresh_mgr = _manager(tmp_path / "a", vault, idle_ttl_s=3600.0)
    _ = await fresh_mgr.acquire("conv-a")
    await fresh_mgr.sweep()
    assert fresh_mgr.sessions

    busy_mgr = _manager(tmp_path / "b", vault, idle_ttl_s=0.0)
    session = await busy_mgr.acquire("conv-b")
    _ = await session.lock.acquire()  # simulate a call in flight
    try:
        await busy_mgr.sweep()
        assert busy_mgr.sessions
    finally:
        session.lock.release()


async def test_a_new_session_displaces_the_least_recently_used_one_at_the_cap(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=2)
    first = await manager.acquire("conv-a")
    second = await manager.acquire("conv-b")
    second.write_file("notes.txt", b"keep me")
    await asyncio.sleep(0.01)
    first.touch()  # conv-b is now the least recently used

    third = await manager.acquire("conv-c")

    assert set(manager.sessions) == {safe_key("conv-a"), safe_key("conv-c")}
    assert third is manager.sessions[safe_key("conv-c")]
    assert second.workspace.exists()
    revived = await manager.acquire("conv-b")
    assert (revived.ensure_workspace() / "notes.txt").read_text() == "keep me"


async def test_the_cap_never_displaces_a_session_with_a_call_in_flight(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    busy = await manager.acquire("conv-a")
    _ = await busy.lock.acquire()  # simulate a call in flight
    try:
        _ = await manager.acquire("conv-b")
        # Over the cap rather than failing the call; the idle sweep collects the overflow.
        assert set(manager.sessions) == {safe_key("conv-a"), safe_key("conv-b")}
    finally:
        busy.lock.release()


class _Work:
    """A stand-in for the ``Run`` a session is claimed by."""

    def __init__(self) -> None:
        self.is_terminal: bool = False


async def test_the_cap_never_displaces_a_session_a_live_run_is_working_in(tmp_path: Path):
    # The lock covers one exec, not the gaps between a turn's calls; displacing in a gap
    # would wipe the interpreter, servers and shell state the next call expects.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    turn = _Work()
    working = await manager.acquire("conv-a", holder=turn)
    (working.workspace / "node_modules").mkdir(parents=True, exist_ok=True)

    _ = await manager.acquire("conv-b")  # the lock is free: the model is thinking

    assert not working.is_busy
    assert set(manager.sessions) == {safe_key("conv-a"), safe_key("conv-b")}
    assert (working.workspace / "node_modules").exists()


async def test_the_claim_lasts_exactly_as_long_as_the_run_does(tmp_path: Path):
    # A claim released only by a hand-back would leak on every cancelled turn.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    turn = _Work()
    working = await manager.acquire("conv-a", holder=turn)
    working.write_file("notes.txt", b"keep me")
    _ = await manager.acquire("conv-b")

    turn.is_terminal = True  # stopped, failed or answered

    assert working.is_displaceable
    _ = await manager.acquire("conv-c")
    assert safe_key("conv-a") not in manager.sessions
    assert working.read_file("notes.txt") == b"keep me"


async def test_the_cap_never_displaces_a_conversation_serving_a_live_preview(tmp_path: Path):
    # Reaping drops the proxy token, turning a page the operator is watching into a 404.
    # The idle sweep may still collect a long-unloaded preview.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    serving = await manager.acquire("conv-a")
    serving.preview = _fake_preview("tok-live")
    manager.preview_tokens.index("tok-live", serving.key)

    _ = await manager.acquire("conv-b")

    assert set(manager.sessions) == {safe_key("conv-a"), safe_key("conv-b")}
    assert manager.preview_status("tok-live") == "running"


async def test_a_cancelled_acquire_does_not_abort_another_conversations_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cancelling the acquiring run must not strand the displaced guest copy outside every map."""
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    manager = _manager(tmp_path, vault, vm=vm, max_sessions=1)
    displaced = await manager.acquire("conv-a")
    displaced.write_file("notes.txt", b"keep me")
    _ = await displaced.run(SandboxSpec(command=["true"]))
    assert displaced.is_warm

    tearing, resume = asyncio.Event(), asyncio.Event()
    real_shutdown = displaced.shutdown

    async def slow_shutdown() -> None:
        tearing.set()
        _ = await resume.wait()
        await real_shutdown()

    monkeypatch.setattr(displaced, "shutdown", slow_shutdown)

    acquiring = asyncio.create_task(manager.acquire("conv-b"))
    _ = await asyncio.wait_for(tearing.wait(), timeout=2.0)
    in_flight = list(manager.teardowns)
    _ = acquiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    resume.set()
    _ = await asyncio.gather(*in_flight)

    assert not displaced.is_warm
    assert ("wipe", displaced.key) in vm.events
    assert displaced.read_file("notes.txt") == b"keep me"
    assert set(manager.sessions) == {safe_key("conv-b")}


async def test_a_failed_teardown_leaves_the_session_live_for_the_sweeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A session dropped on a failed teardown is a guest copy nothing would ever reap.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    displaced = await manager.acquire("conv-a")

    async def failing_shutdown() -> None:
        raise RuntimeError("the guest copy could not be brought out")

    monkeypatch.setattr(displaced, "shutdown", failing_shutdown)

    _ = await manager.acquire("conv-b")

    assert manager.sessions[safe_key("conv-a")] is displaced
    assert not manager.tearing_down
    assert await manager.acquire("conv-a") is displaced


async def test_sweep_does_not_block_acquire_for_an_unrelated_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    stale = await manager.acquire("conv-stale")
    stale.workspace.mkdir(parents=True, exist_ok=True)

    seal_started = asyncio.Event()
    release_seal = asyncio.Event()

    async def slow_shutdown(_self: SandboxSession) -> None:
        seal_started.set()
        _ = await release_seal.wait()

    monkeypatch.setattr(SandboxSession, "shutdown", slow_shutdown)

    sweep_task = asyncio.create_task(manager.sweep())
    _ = await asyncio.wait_for(seal_started.wait(), timeout=1.0)

    other = await asyncio.wait_for(manager.acquire("conv-other"), timeout=1.0)
    assert other is not None

    release_seal.set()
    _ = await asyncio.wait_for(sweep_task, timeout=1.0)


async def test_acquire_for_a_mid_seal_key_waits_for_its_own_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    original = await manager.acquire("conv-a")
    original.workspace.mkdir(parents=True, exist_ok=True)

    seal_started = asyncio.Event()
    release_seal = asyncio.Event()

    async def slow_shutdown(_self: SandboxSession) -> None:
        seal_started.set()
        _ = await release_seal.wait()

    monkeypatch.setattr(SandboxSession, "shutdown", slow_shutdown)

    sweep_task = asyncio.create_task(manager.sweep())
    _ = await asyncio.wait_for(seal_started.wait(), timeout=1.0)

    acquire_task = asyncio.create_task(manager.acquire("conv-a"))
    await asyncio.sleep(0.05)
    assert not acquire_task.done()

    release_seal.set()
    revived = await asyncio.wait_for(acquire_task, timeout=1.0)
    _ = await asyncio.wait_for(sweep_task, timeout=1.0)
    assert revived is not original
    assert not manager.tearing_down


async def test_purge_waits_for_an_in_flight_sweep_seal_on_the_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)

    seal_started = asyncio.Event()
    release_seal = asyncio.Event()

    async def slow_shutdown(_self: SandboxSession) -> None:
        seal_started.set()
        _ = await release_seal.wait()

    monkeypatch.setattr(SandboxSession, "shutdown", slow_shutdown)

    sweep_task = asyncio.create_task(manager.sweep())
    _ = await asyncio.wait_for(seal_started.wait(), timeout=1.0)

    purge_task = asyncio.create_task(manager.purge("conv-a"))
    await asyncio.sleep(0.05)
    assert not purge_task.done()

    release_seal.set()
    _ = await asyncio.wait_for(purge_task, timeout=1.0)
    _ = await asyncio.wait_for(sweep_task, timeout=1.0)
    assert not manager.tearing_down


async def test_purge_drops_the_session_and_deletes_its_workspace(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    _ = (session.workspace / "f.txt").write_text("data")

    await manager.purge("conv-a")

    assert not manager.sessions
    assert not session.workspace.exists()
    assert not session.sealed.exists()


async def test_purge_deletes_a_cold_sealed_archive_with_no_live_session(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    safe = safe_key("conv-cold")
    sealed = tmp_path / "sandbox" / "sealed" / f"{safe}.tar.enc.gz"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    _ = sealed.write_bytes(b"sealed-bytes")
    work = tmp_path / "sandbox" / "work" / safe
    work.mkdir(parents=True, exist_ok=True)
    _ = (work / "leftover.txt").write_text("x")

    await manager.purge("conv-cold")

    assert not sealed.exists()
    assert not work.exists()


async def test_purge_is_safe_when_there_is_nothing_to_remove(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    await manager.purge("never-existed")
    assert not manager.sessions


def _fake_preview(token: str) -> PreviewHandle:
    return PreviewHandle(token=token, host_port=1, server_port=2, command=("srv",))


async def test_preview_status_is_unknown_for_a_token_never_seen(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    assert manager.preview_status("no-such-token") == "unknown"


async def test_preview_status_is_running_while_the_preview_is_live(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-a")
    session.preview = _fake_preview("tok-1")
    manager.preview_tokens.index("tok-1", session.key)

    assert manager.preview_status("tok-1") == "running"


async def test_idle_reap_marks_the_running_previews_token_stopped(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    session.preview = _fake_preview("tok-1")
    manager.preview_tokens.index("tok-1", session.key)
    assert manager.preview_status("tok-1") == "running"

    await manager.sweep()

    assert not manager.sessions
    assert manager.preview_status("tok-1") == "stopped"


async def test_purge_marks_the_running_previews_token_stopped(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    session.preview = _fake_preview("tok-1")
    manager.preview_tokens.index("tok-1", session.key)

    await manager.purge("conv-a")

    assert manager.preview_status("tok-1") == "stopped"


async def test_stopped_tokens_are_pruned_after_their_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    session.preview = _fake_preview("tok-1")
    manager.preview_tokens.index("tok-1", session.key)

    await manager.sweep()
    assert manager.preview_status("tok-1") == "stopped"

    # Tombstones are pruned lazily, on the next stop/reap/purge after the TTL.
    frozen_future = time.monotonic() + preview_tokens.STOPPED_TTL_S + 1
    monkeypatch.setattr("time.monotonic", lambda: frozen_future)
    other = await manager.acquire("conv-b")
    other.workspace.mkdir(parents=True, exist_ok=True)
    other.preview = _fake_preview("tok-2")
    manager.preview_tokens.index("tok-2", other.key)
    await manager.purge("conv-b")

    assert manager.preview_status("tok-1") == "unknown"
    assert manager.preview_status("tok-2") == "stopped"


async def test_sweep_leaves_a_stranded_conversation_workspace_alone(tmp_path: Path):
    """A workspace no session owns is that conversation's files, not residue."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    orphan = manager.work_root / safe_key("conv-orphan")
    orphan.mkdir(parents=True)
    _ = (orphan / "notes.txt").write_text("the last process died before it could finish")

    await manager.sweep()

    assert (orphan / "notes.txt").read_text() == "the last process died before it could finish"
    assert not manager.sessions


async def test_sweep_does_not_touch_a_workspace_its_own_session_still_holds(tmp_path: Path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=3600.0)
    session = await manager.acquire("conv-live")
    _ = session.ensure_workspace()
    _ = (session.workspace / "wip.txt").write_text("still working")

    await manager.sweep()

    assert (session.workspace / "wip.txt").exists()
    assert manager.existing("conv-live") is session


confined = pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")


@confined
async def test_a_workspace_persists_across_calls_and_across_a_reap(tmp_path: Path):
    vault = await _vault(tmp_path)
    vm, _driver = await local_vm(tmp_path)
    manager = _manager(tmp_path, vault, vm=vm)
    try:
        session = await manager.acquire("conv-a")
        wrote = await session.run(
            SandboxSpec(command=["sh", "-c", "echo persisted > note.txt"], timeout_s=30)
        )
        assert wrote.ok
        read = await session.run(SandboxSpec(command=["cat", "note.txt"], timeout_s=30))
        assert read.stdout.strip() == "persisted"
        assert (session.workspace / "note.txt").read_text().strip() == "persisted"

        await session.shutdown()
        assert not session.is_warm
        again = await session.run(SandboxSpec(command=["cat", "note.txt"], timeout_s=30))
        assert again.stdout.strip() == "persisted"
    finally:
        await manager.stop()
        await vm.stop()
