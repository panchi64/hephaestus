"""Per-conversation live sandboxes: lazy acquisition, the idle reaper, what a reap
does and does not touch, how a session drives the platform VM, and (with confinement
available) file continuity across calls and across a reap."""

from __future__ import annotations

import asyncio
import io
import tarfile
import threading
import time
from pathlib import Path

import pytest

import services.sandbox.manager as manager_mod
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
from services.sandbox.fork import fork_marker
from services.sandbox.legacy_seal import adopt_legacy_archive, partial_marker
from services.sandbox.session import proxy_env
from services.sandbox.walk import excluded
from services.vm.driver import VMError
from services.vm.platform import WORKSPACE_PROXY

from .conftest import egress_policy
from .vm_fakes import FakeVM
from .vm_local import can_confine, local_vm

_EXCLUDES = Settings().sandbox_walk_excludes


async def _vault(tmp_path) -> Vault:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return vault


def _vm(tmp_path, **kwargs) -> FakeVM:
    return FakeVM(tmp_path / "sandbox" / "work", **kwargs)


def _manager(tmp_path, vault, **overrides) -> SandboxSessionManager:
    vm = overrides.pop("vm", None) or _vm(tmp_path)
    opts = dict(
        egress=egress_policy(tmp_path),
        data_dir=tmp_path,
        idle_ttl_s=1800.0,
        reap_interval_s=60.0,
        excludes=_EXCLUDES,
    )
    opts.update(overrides)
    return SandboxSessionManager(vm, vault, **opts)


def _legacy_archive(tmp_path: Path, vault: Vault, files: dict[str, str]) -> bytes:
    """An archive in the format the sealing build wrote, so the one-way adoption path
    can be tested against real bytes. Built here rather than imported because the code
    that produced these is gone — this is the last spelling of that format, and it is a
    test fixture rather than a seam."""
    staging = tmp_path / f"legacy-{len(list(tmp_path.iterdir()))}"
    staging.mkdir(parents=True)
    for rel, text in files.items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(staging.iterdir()):
            tar.add(item, arcname=item.name)
    return vault.encrypt_bytes(buf.getvalue())


def _session(tmp_path, vault, **overrides) -> SandboxSession:
    """A bare session, built without a manager — the unit under test in the bring-up
    and teardown cases below."""
    opts = dict(
        workspace=tmp_path / "work",
        sealed=tmp_path / "sealed.tar.enc.gz",
        egress_dir=tmp_path / "egress",
        vm=_vm(tmp_path),
        vault=vault,
        excludes=(),
    )
    opts.update(overrides)
    return SandboxSession("s1", **opts)


# --- naming + exclusion ------------------------------------------------------
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


# --- write_file: staging a file into the workspace (no VM needed) -------------
async def test_write_file_round_trips_through_read_file(tmp_path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")

    session.write_file("attachments/data.csv", b"a,b\n1,2\n")

    assert session.read_file("attachments/data.csv") == b"a,b\n1,2\n"
    assert (session.workspace / "attachments" / "data.csv").read_bytes() == b"a,b\n1,2\n"


async def test_write_file_rejects_a_path_escape(tmp_path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")

    with pytest.raises(SandboxError):
        session.write_file("../escape.txt", b"nope")


async def test_written_file_survives_a_reap(tmp_path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")
    session.write_file("attachments/keep.txt", b"hold onto me")

    await session.shutdown()  # wipes the guest copy; the files are not its business

    assert session.workspace.exists()
    assert session.read_file("attachments/keep.txt") == b"hold onto me"


# --- what a reap leaves alone ------------------------------------------------
async def test_a_reap_keeps_the_dependencies_and_history_a_run_built(tmp_path):
    """A reap that dropped `.venv`, `node_modules`, `dist` or `.git` would send the next
    turn back to a tree missing exactly what the last one spent minutes building."""
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")
    work = session.ensure_workspace()
    (work / "analysis.py").write_text("print('hi')")
    for built in (".venv/lib", "node_modules/left-pad", "dist", ".git/refs"):
        (work / built).mkdir(parents=True)
        (work / built / "f").write_text("expensive")

    await session.shutdown()

    for built in (".venv/lib", "node_modules/left-pad", "dist", ".git/refs"):
        assert (work / built / "f").read_text() == "expensive", built
    assert (work / "analysis.py").read_text() == "print('hi')"


async def test_a_reap_writes_no_archive(tmp_path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-x")
    session.write_file("keep.txt", b"data")

    await session.shutdown()

    assert not session.sealed.exists()


async def test_the_reaper_runs_with_the_vault_locked(tmp_path):
    """Reaping wipes a guest copy and touches no file, so a locked vault — the state a
    machine sits in for hours — does not keep every idle guest copy alive."""
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    vault.lock()
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.write_file("f.txt", b"data")

    await manager._sweep()

    assert not manager._sessions  # reaped, key or no key
    assert session.read_file("f.txt") == b"data"  # and the files are untouched


async def test_the_cap_is_enforced_with_the_vault_locked(tmp_path):
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    vault.lock()
    manager = _manager(tmp_path, vault, max_sessions=1)
    first = await manager.acquire("conv-a")
    first.write_file("f.txt", b"data")

    second = await manager.acquire("conv-b")

    assert set(manager._sessions) == {safe_key("conv-b")}
    assert second is not first
    assert first.read_file("f.txt") == b"data"


# --- errors surface legibly, never as a crash --------------------------------
async def test_run_wraps_an_unexpected_error_as_sandbox_error(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-a")

    def boom() -> None:
        raise ValueError("something deep broke")

    monkeypatch.setattr(session, "_ensure_workspace", boom)
    with pytest.raises(SandboxError):  # the agent gets a sandbox failure, not a ValueError
        await session.run(SandboxSpec(command=["echo", "hi"]))


async def test_adopting_a_damaged_archive_raises_sandbox_error(tmp_path):
    vault = await _vault(tmp_path)
    archive = tmp_path / "old.tar.enc.gz"
    archive.write_bytes(b"not a valid sealed archive")
    with pytest.raises(SandboxError):
        adopt_legacy_archive(archive, tmp_path / "out", vault)


# --- archives written by the sealing build are read back once, then retired ---
async def test_a_sealing_era_archive_is_adopted_and_deleted(tmp_path):
    vault = await _vault(tmp_path)
    archive = tmp_path / "old.tar.enc.gz"
    archive.write_bytes(_legacy_archive(tmp_path, vault, {"report.md": "the work"}))
    work = tmp_path / "adopted"

    assert adopt_legacy_archive(archive, work, vault) is True

    assert (work / "report.md").read_text() == "the work"
    # Left in place it would be a second copy nothing updates, which the next cold open
    # would silently prefer over everything done since.
    assert not archive.exists()


async def test_an_archive_a_materialised_workspace_outlived_is_retired(tmp_path):
    """Adoption unlinks the archive as its last step, so a process dying in the window
    between the extract finishing and that unlink leaves both on disk — and the
    workspace being complete means adoption is never reached again. This is the only
    other place that can notice."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-old")
    session.write_file("live.txt", b"the work since")
    session.sealed.parent.mkdir(parents=True, exist_ok=True)
    session.sealed.write_bytes(_legacy_archive(tmp_path, vault, {"stale.txt": "superseded"}))

    session.ensure_workspace()

    assert not session.sealed.exists()
    assert session.read_file("live.txt") == b"the work since"  # the live copy wins
    assert not (session.workspace / "stale.txt").exists()


async def test_adopting_nothing_is_the_ordinary_case(tmp_path):
    vault = await _vault(tmp_path)
    assert adopt_legacy_archive(tmp_path / "absent.tar.enc.gz", tmp_path / "w", vault) is False


async def test_a_conversation_opens_its_sealing_era_archive_on_first_use(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-old")
    session.sealed.parent.mkdir(parents=True, exist_ok=True)
    session.sealed.write_bytes(_legacy_archive(tmp_path, vault, {"notes.txt": "from before"}))

    assert session.read_file("notes.txt") == b"from before"
    assert not session.sealed.exists()


async def test_adopting_an_archive_needs_the_key(tmp_path):
    vault = Vault(tmp_path / "k.json")
    await vault.setup("pw")
    archive = tmp_path / "old.tar.enc.gz"
    archive.write_bytes(_legacy_archive(tmp_path, vault, {"a.txt": "x"}))
    vault.lock()

    with pytest.raises(SandboxError):
        adopt_legacy_archive(archive, tmp_path / "w", vault)
    assert archive.exists()  # refused, never destroyed


async def test_a_fragment_beside_an_archive_is_thrown_away_not_kept(tmp_path):
    """A sealing-era restore that died mid-extract left a directory holding *less* than
    the archive beside it. The archive is the whole copy, so it is what wins."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-old")
    session.workspace.mkdir(parents=True, exist_ok=True)
    (session.workspace / "half.txt").write_text("torn")
    partial_marker(session.workspace).touch()
    session.sealed.parent.mkdir(parents=True, exist_ok=True)
    session.sealed.write_bytes(_legacy_archive(tmp_path, vault, {"whole.txt": "all of it"}))

    session.ensure_workspace()

    assert (session.workspace / "whole.txt").read_text() == "all of it"
    assert not (session.workspace / "half.txt").exists()
    assert not partial_marker(session.workspace).exists()


# --- lazy acquisition --------------------------------------------------------
async def test_acquire_is_lazy_and_idempotent_per_key(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    first = await manager.acquire("conv-a")
    again = await manager.acquire("conv-a")
    other = await manager.acquire("conv-b")
    assert first is again  # same conversation reuses its session
    assert other is not first
    # Lazy: no guest copy or workspace exists yet, just the bookkeeping object.
    assert not first.workspace.exists()


# --- driving the platform VM -------------------------------------------------
async def test_a_command_runs_in_the_vm_and_its_files_come_back(tmp_path):
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
    key, argv, env, stdin = vm.runs[0]
    assert (key, stdin, env["FOO"]) == (session.key, "in", "x")
    assert env["HOME"] == "/work/.home"  # the package-manager defaults reach the command


async def test_a_nonzero_exit_is_a_result_and_a_timeout_is_marked(tmp_path):
    vault = await _vault(tmp_path)
    session = await _manager(tmp_path, vault).acquire("conv-a")

    failed = await session.run(SandboxSpec(command=["sh", "-c", "exit 3"]))
    timed_out = await session.run(SandboxSpec(command=["sleep", "5"], timeout_s=0.2))

    assert failed.exit_code == 3 and not failed.timed_out
    assert timed_out.timed_out and not timed_out.ok


async def test_the_workspace_and_credentials_are_in_place_before_the_command_runs(
    tmp_path, monkeypatch
):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    real_grant = vm.gateway.grant

    def grant(allow_dir):
        vm.events.append(("grant", session.key))
        return real_grant(allow_dir)

    monkeypatch.setattr(vm.gateway, "grant", grant)

    await session.run(SandboxSpec(command=["true"]))

    ops = [op for op, _ in vm.events]
    assert ops.index("grant") < ops.index("push") < ops.index("run")
    env = vm.runs[0][2]
    assert env["HTTPS_PROXY"].endswith(f"@{WORKSPACE_PROXY}")
    assert env["https_proxy"] == env["HTTPS_PROXY"]  # curl reads this one


def test_proxy_env_carries_the_credentials_in_both_spellings():
    env = proxy_env("workspace:tok")
    url = f"http://workspace:tok@{WORKSPACE_PROXY}"
    assert env["HTTP_PROXY"] == env["http_proxy"] == url
    assert env["HTTPS_PROXY"] == env["https_proxy"] == url
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1"


async def test_an_edit_made_on_the_host_between_commands_is_pushed_in(tmp_path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    await session.run(SandboxSpec(command=["true"]))
    pushes = len(vm.pushes)

    await session.run(SandboxSpec(command=["true"]))
    assert len(vm.pushes) == pushes  # nothing changed, nothing crosses

    session.write_file("edited.txt", b"x")
    await session.run(SandboxSpec(command=["true"]))
    assert "edited.txt" in vm.pushes[-1][1].changed


async def test_a_rebuilt_vm_gets_the_workspace_again(tmp_path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    session.write_file("keep.txt", b"x")
    await session.run(SandboxSpec(command=["true"]))

    vm.restart()
    assert not session.is_warm  # the guest copy went with the VM
    await session.run(SandboxSpec(command=["true"]))

    assert session.is_warm
    assert "keep.txt" in vm.pushes[-1][1].changed


async def test_shutdown_wipes_the_guest_copy_and_revokes_the_credentials(tmp_path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    await session.run(SandboxSpec(command=["true"]))
    token = session._credentials.partition(":")[2]
    assert token in vm.gateway._grants

    await session.shutdown()

    assert not session.is_warm
    assert vm.events[-1] == ("wipe", session.key)
    assert token not in vm.gateway._grants
    assert session._credentials is None


async def test_each_run_after_a_reap_is_granted_fresh_credentials(tmp_path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    await session.run(SandboxSpec(command=["true"]))
    await session.shutdown()
    await session.run(SandboxSpec(command=["true"]))

    first, second = (run[2]["HTTPS_PROXY"] for run in vm.runs)
    assert first != second


async def test_a_vm_that_is_not_up_is_reported_with_its_reason(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path, available=False)
    vm.failure = "the platform VM is disabled in settings"
    monkeypatch.setattr(session_mod, "_VM_WAIT_S", 0.01)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")

    with pytest.raises(SandboxError, match="disabled in settings"):
        await session.run(SandboxSpec(command=["true"]))
    assert vm.runs == []


async def test_a_vm_failure_surfaces_as_a_sandbox_error(tmp_path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path)
    session = await _manager(tmp_path, vault, vm=vm).acquire("conv-a")
    vm.fail_with = VMError("the VM stopped answering")

    with pytest.raises(SandboxError, match="stopped answering"):
        await session.run(SandboxSpec(command=["true"]))


async def test_the_manager_reports_a_vm_that_is_still_starting(tmp_path):
    vault = await _vault(tmp_path)
    vm = _vm(tmp_path, available=False)
    manager = _manager(tmp_path, vault, vm=vm)
    assert manager.vm_starting

    vm.failure = "boot failed"
    assert not manager.vm_starting

    vm.failure = None
    vm.available = True
    assert not manager.vm_starting


# --- the idle reaper ---------------------------------------------------------
async def test_reaper_drops_an_idle_session_and_keeps_its_files(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.write_file("notes.txt", b"keep me")

    await manager._sweep()

    assert not manager._sessions  # reaped from the registry
    assert session.workspace.exists()  # the guest copy went; the files did not

    # Resuming the conversation finds them exactly where it left them.
    revived = await manager.acquire("conv-a")
    assert (revived.ensure_workspace() / "notes.txt").read_text() == "keep me"


async def test_start_stop_manages_the_reaper_task(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    await manager.start()
    assert manager._reaper.running
    await manager.stop()  # cancels the reaper and tears down any live sessions
    assert not manager._reaper.running


async def test_reaper_spares_fresh_and_busy_sessions(tmp_path):
    vault = await _vault(tmp_path)
    fresh_mgr = _manager(tmp_path / "a", vault, idle_ttl_s=3600.0)
    await fresh_mgr.acquire("conv-a")
    await fresh_mgr._sweep()
    assert fresh_mgr._sessions  # within TTL → spared

    busy_mgr = _manager(tmp_path / "b", vault, idle_ttl_s=0.0)
    session = await busy_mgr.acquire("conv-b")
    await session._lock.acquire()  # simulate a call in flight
    try:
        await busy_mgr._sweep()
        assert busy_mgr._sessions  # never reaped mid-run, even past TTL
    finally:
        session._lock.release()


# --- the live-session cap: a ceiling in count, not only in time --------------
async def test_a_new_session_displaces_the_least_recently_used_one_at_the_cap(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=2)
    first = await manager.acquire("conv-a")
    second = await manager.acquire("conv-b")
    second.write_file("notes.txt", b"keep me")
    await asyncio.sleep(0.01)
    first.touch()  # conv-b is now the least recently used

    third = await manager.acquire("conv-c")

    assert set(manager._sessions) == {safe_key("conv-a"), safe_key("conv-c")}
    assert third is manager._sessions[safe_key("conv-c")]
    # Displaced, not discarded: torn down exactly as an idle reap tears down, and the
    # files are still there the next time that conversation runs code.
    assert second.workspace.exists()
    revived = await manager.acquire("conv-b")
    assert (revived.ensure_workspace() / "notes.txt").read_text() == "keep me"


async def test_the_cap_never_displaces_a_session_with_a_call_in_flight(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    busy = await manager.acquire("conv-a")
    await busy._lock.acquire()  # simulate a call in flight
    try:
        await manager.acquire("conv-b")
        # Over the cap rather than failing the tool call the operator is watching; the
        # idle sweep collects the overflow once the work finishes.
        assert set(manager._sessions) == {safe_key("conv-a"), safe_key("conv-b")}
    finally:
        busy._lock.release()


class _Work:
    """A stand-in for the ``Run`` a session is claimed by — the sandbox only ever asks
    whether the work is over."""

    def __init__(self) -> None:
        self.is_terminal = False


async def test_the_cap_never_displaces_a_session_a_live_run_is_working_in(tmp_path):
    # The lock is held for one exec; a turn is a dozen of them with the model thinking in
    # between. Displacing in one of those gaps wipes the guest copy — and with it the
    # interpreter, servers and shell state the run's next tool call expects to find.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    turn = _Work()
    working = await manager.acquire("conv-a", holder=turn)
    (working.workspace / "node_modules").mkdir(parents=True, exist_ok=True)

    await manager.acquire("conv-b")  # thinking, not executing: the lock is free

    assert not working.is_busy  # the old signal says "reap me"
    assert set(manager._sessions) == {safe_key("conv-a"), safe_key("conv-b")}
    assert (working.workspace / "node_modules").exists()


async def test_the_claim_lasts_exactly_as_long_as_the_run_does(tmp_path):
    # And no longer: a claim released only by a hand-back would be leaked by every
    # cancelled turn, and the cap would decay into no cap at all.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    turn = _Work()
    working = await manager.acquire("conv-a", holder=turn)
    working.write_file("notes.txt", b"keep me")
    await manager.acquire("conv-b")

    turn.is_terminal = True  # stopped, failed or answered — the sandbox cannot tell

    assert working.is_displaceable
    await manager.acquire("conv-c")
    assert safe_key("conv-a") not in manager._sessions
    assert working.read_file("notes.txt") == b"keep me"


async def test_the_cap_never_displaces_a_conversation_serving_a_live_preview(tmp_path):
    # Reaping drops the token the proxy resolves, so the page the operator is watching
    # turns into a 404 — with the server killed out from under an iframe that has no
    # reason to expect it. The idle sweep may still collect a preview nobody has loaded
    # in half an hour; that is the difference between a deadline and a ceiling.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    serving = await manager.acquire("conv-a")
    serving._preview = _fake_preview("tok-live")
    manager._preview_tokens.index("tok-live", serving.key)

    await manager.acquire("conv-b")

    assert set(manager._sessions) == {safe_key("conv-a"), safe_key("conv-b")}
    assert manager.preview_status("tok-live") == "running"


# --- an evicted session's teardown belongs to the manager, not to whoever triggered it ---
async def test_a_cancelled_acquire_does_not_abort_another_conversations_teardown(tmp_path):
    """Stop is the operator's most-used control, and at the cap the acquiring run is
    tearing down somebody *else's* conversation. Cancelled partway, its guest copy would
    be left out of every map — no sweep can find them and no purge names them."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    displaced = await manager.acquire("conv-a")
    displaced.write_file("notes.txt", b"keep me")
    await displaced.run(SandboxSpec(command=["true"]))
    assert displaced.is_warm

    tearing, resume = asyncio.Event(), asyncio.Event()
    real_shutdown = displaced.shutdown

    async def slow_shutdown() -> None:
        tearing.set()
        await resume.wait()
        await real_shutdown()

    displaced.shutdown = slow_shutdown  # type: ignore[method-assign]

    acquiring = asyncio.create_task(manager.acquire("conv-b"))
    await asyncio.wait_for(tearing.wait(), timeout=2.0)
    in_flight = list(manager._teardowns)
    acquiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquiring

    resume.set()
    await asyncio.gather(*in_flight)

    # The teardown finished on the manager's own task, and the conversation's files
    # were never what it was reclaiming.
    assert not displaced.is_warm
    assert ("wipe", displaced.key) in manager._vm.events
    assert displaced.read_file("notes.txt") == b"keep me"
    assert set(manager._sessions) == {safe_key("conv-b")}


async def test_a_failed_teardown_leaves_the_session_live_for_the_sweeper(tmp_path):
    # A session dropped from the map on a failed teardown is a guest copy nothing will
    # ever reap. Put it back and let the idle
    # sweep try again.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    displaced = await manager.acquire("conv-a")

    async def failing_shutdown() -> None:
        raise RuntimeError("the guest copy could not be brought out")

    displaced.shutdown = failing_shutdown  # type: ignore[method-assign]

    await manager.acquire("conv-b")

    assert manager._sessions[safe_key("conv-a")] is displaced
    assert not manager._tearing_down  # and not wedged behind a tombstone either
    assert await manager.acquire("conv-a") is displaced


# --- a sweep's sealing must not stall unrelated conversations (sandbox-02) ---
async def test_sweep_does_not_block_acquire_for_an_unrelated_conversation(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    stale = await manager.acquire("conv-stale")
    stale.workspace.mkdir(parents=True, exist_ok=True)

    seal_started = asyncio.Event()
    release_seal = asyncio.Event()

    async def slow_shutdown(self) -> None:
        seal_started.set()
        await release_seal.wait()

    monkeypatch.setattr(SandboxSession, "shutdown", slow_shutdown)

    sweep_task = asyncio.create_task(manager._sweep())
    await asyncio.wait_for(seal_started.wait(), timeout=1.0)

    # A different conversation must proceed immediately — it must not wait on
    # the manager lock for the sum of every in-flight seal.
    other = await asyncio.wait_for(manager.acquire("conv-other"), timeout=1.0)
    assert other is not None

    release_seal.set()
    await asyncio.wait_for(sweep_task, timeout=1.0)


async def test_acquire_for_a_mid_seal_key_waits_for_its_own_teardown(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    original = await manager.acquire("conv-a")
    original.workspace.mkdir(parents=True, exist_ok=True)

    seal_started = asyncio.Event()
    release_seal = asyncio.Event()

    async def slow_shutdown(self) -> None:
        seal_started.set()
        await release_seal.wait()

    monkeypatch.setattr(SandboxSession, "shutdown", slow_shutdown)

    sweep_task = asyncio.create_task(manager._sweep())
    await asyncio.wait_for(seal_started.wait(), timeout=1.0)

    acquire_task = asyncio.create_task(manager.acquire("conv-a"))
    await asyncio.sleep(0.05)
    assert not acquire_task.done()  # same key mid-seal — must wait for it specifically

    release_seal.set()
    revived = await asyncio.wait_for(acquire_task, timeout=1.0)
    await asyncio.wait_for(sweep_task, timeout=1.0)
    assert revived is not original  # a fresh session, minted only once teardown finished
    assert not manager._tearing_down  # the tombstone is cleared afterward


async def test_purge_waits_for_an_in_flight_sweep_seal_on_the_same_key(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)

    seal_started = asyncio.Event()
    release_seal = asyncio.Event()

    async def slow_shutdown(self) -> None:
        seal_started.set()
        await release_seal.wait()

    monkeypatch.setattr(SandboxSession, "shutdown", slow_shutdown)

    sweep_task = asyncio.create_task(manager._sweep())
    await asyncio.wait_for(seal_started.wait(), timeout=1.0)

    purge_task = asyncio.create_task(manager.purge("conv-a"))
    await asyncio.sleep(0.05)
    assert not purge_task.done()  # waits for the sweep's seal before deleting anything

    release_seal.set()
    await asyncio.wait_for(purge_task, timeout=1.0)
    await asyncio.wait_for(sweep_task, timeout=1.0)
    assert not manager._tearing_down


# --- purge: deleting a conversation removes its sandbox outright -------------
async def test_purge_drops_the_session_and_deletes_its_workspace(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    (session.workspace / "f.txt").write_text("data")

    await manager.purge("conv-a")

    assert not manager._sessions  # evicted from the registry
    assert not session.workspace.exists()  # plaintext deleted, not sealed
    assert not session.sealed.exists()  # nothing preserved


async def test_purge_deletes_a_cold_sealed_archive_with_no_live_session(tmp_path):
    # A sealed-but-unloaded conversation: an archive on disk and no session object.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    safe = safe_key("conv-cold")
    sealed = tmp_path / "sandbox" / "sealed" / f"{safe}.tar.enc.gz"
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(b"sealed-bytes")
    work = tmp_path / "sandbox" / "work" / safe
    work.mkdir(parents=True, exist_ok=True)
    (work / "leftover.txt").write_text("x")

    await manager.purge("conv-cold")  # nothing in the registry to stop

    assert not sealed.exists()
    assert not work.exists()


async def test_purge_is_safe_when_there_is_nothing_to_remove(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    await manager.purge("never-existed")  # must not raise
    assert not manager._sessions


# --- live-preview status: reap/purge leave a legible "stopped" signal --------
def _fake_preview(token: str) -> PreviewHandle:
    return PreviewHandle(token=token, host_port=1, server_port=2, command=("srv",))


async def test_preview_status_is_unknown_for_a_token_never_seen(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    assert manager.preview_status("no-such-token") == "unknown"


async def test_preview_status_is_running_while_the_preview_is_live(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-a")
    session._preview = _fake_preview("tok-1")
    manager._preview_tokens.index("tok-1", session.key)

    assert manager.preview_status("tok-1") == "running"


async def test_idle_reap_marks_the_running_previews_token_stopped(tmp_path):
    # The failure this guards: an idle-reaped preview used to vanish with no signal
    # at all — `preview_status` now lets a client learn the head died, not just that
    # its URL 404s.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    session._preview = _fake_preview("tok-1")
    manager._preview_tokens.index("tok-1", session.key)
    assert manager.preview_status("tok-1") == "running"

    await manager._sweep()

    assert not manager._sessions  # the session itself was reaped, as before
    assert manager.preview_status("tok-1") == "stopped"  # but the token now says why


async def test_purge_marks_the_running_previews_token_stopped(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    session._preview = _fake_preview("tok-1")
    manager._preview_tokens.index("tok-1", session.key)

    await manager.purge("conv-a")

    assert manager.preview_status("tok-1") == "stopped"


async def test_stopped_tokens_are_pruned_after_their_ttl(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.workspace.mkdir(parents=True, exist_ok=True)
    session._preview = _fake_preview("tok-1")
    manager._preview_tokens.index("tok-1", session.key)

    await manager._sweep()
    assert manager.preview_status("tok-1") == "stopped"

    # Fast-forward past the tombstone's TTL, then force a prune via another mark
    # (mirrors real usage — the map is pruned lazily on the next stop/reap/purge).
    frozen_future = time.monotonic() + preview_tokens._STOPPED_TTL_S + 1
    monkeypatch.setattr("time.monotonic", lambda: frozen_future)
    other = await manager.acquire("conv-b")
    other.workspace.mkdir(parents=True, exist_ok=True)
    other._preview = _fake_preview("tok-2")
    manager._preview_tokens.index("tok-2", other.key)
    await manager.purge("conv-b")

    assert manager.preview_status("tok-1") == "unknown"  # aged out
    assert manager.preview_status("tok-2") == "stopped"  # freshly tombstoned


# --- forking a workspace for a delegated agent -------------------------------
_CHILD = "conv-parent#d1"


async def _forked(tmp_path, vault, **overrides):
    """A parent mid-session — files, and an environment that cost minutes — and the
    fork taken out of it."""
    manager = _manager(tmp_path, vault, **overrides)
    parent = await manager.acquire("conv-parent")
    parent.ensure_workspace()
    (parent.workspace / "notes.txt").write_text("what the parent wrote")
    (parent.workspace / "keep.txt").write_text("still wanted")
    (parent.workspace / ".venv" / "lib").mkdir(parents=True)
    (parent.workspace / ".venv" / "lib" / "big.so").write_bytes(b"x" * 1000)
    child = await manager.fork("conv-parent", _CHILD)
    return manager, parent, child


async def test_fork_copies_the_parents_workspace_including_what_a_seal_drops(tmp_path):
    vault = await _vault(tmp_path)
    manager, parent, child = await _forked(tmp_path, vault)

    assert (child.workspace / "notes.txt").read_text() == "what the parent wrote"
    # The manifest skips a virtualenv because it is rebuildable; a fork keeps it because
    # rebuilding it is the minutes the delegated agent would spend before its first
    # useful command.
    assert (child.workspace / ".venv" / "lib" / "big.so").read_bytes() == b"x" * 1000
    assert child.workspace != parent.workspace
    # A delegated agent may reach exactly what the conversation that delegated to it may.
    assert child._egress_dir == manager._egress.allow_dir("conv-parent")
    # And a domain approved during the delegated run is written to that same directory,
    # not to one nothing has mounted — which would read as an approval that did nothing.
    assert manager._egress.allow_dir(_CHILD) == child._egress_dir
    # The record the merge decides against: the parent as it stood, minus the bloat the
    # merge never walks.
    assert child.fork_manifest.keys() == {"notes.txt", "keep.txt"}


async def test_a_fork_holds_the_parent_still_for_the_whole_copy(tmp_path, monkeypatch):
    # The copy reads the parent's directory for as long as a warm workspace takes. A seal
    # landing in that window rmtree's the tree mid-read: either an error that kills the
    # delegation, or a torn copy whose manifest records the tear as the fork point — and
    # the merge back would then read the parent's surviving files as never having existed.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    parent = await manager.acquire("conv-parent")
    parent.ensure_workspace()
    real_clone = session_mod.clone_workspace
    locked: list[bool] = []

    def watched(source, dest):
        locked.append(_held_elsewhere(parent._disk))
        real_clone(source, dest)

    monkeypatch.setattr(session_mod, "clone_workspace", watched)
    delegated = _Work()
    await manager.fork("conv-parent", _CHILD, holder=delegated)

    assert locked == [True]  # the lock a seal takes is held across the copy
    # And the cap cannot displace the parent out from under the copy either.
    assert parent.is_claimed


def _held_elsewhere(lock: threading.RLock) -> bool:
    """Whether ``lock`` is taken — asked from another thread, because it is reentrant and
    the thread holding it would happily take it a second time."""
    answer: list[bool] = []

    def ask() -> None:
        taken = lock.acquire(blocking=False)
        if taken:
            lock.release()
        answer.append(not taken)

    probe = threading.Thread(target=ask)
    probe.start()
    probe.join()
    return answer[0]


async def test_a_fork_is_ephemeral_and_its_shutdown_leaves_no_archive(tmp_path):
    # Its files are a copy of a workspace that already has an archive under the parent's
    # key; a second one would be the same bytes at rest under a key nothing reopens.
    vault = await _vault(tmp_path)
    _manager_, _parent, child = await _forked(tmp_path, vault)
    assert child.ephemeral

    await child.shutdown()

    assert not child.sealed.exists()
    assert not child.workspace.exists()
    assert not fork_marker(child.workspace).exists()


async def test_merge_back_lands_the_childs_work_and_purges_the_fork(tmp_path):
    vault = await _vault(tmp_path)
    manager, parent, child = await _forked(tmp_path, vault)
    child.write_file("notes.txt", b"what the delegated agent changed")
    child.write_file("report/out.md", b"and something new")

    report = await manager.merge_back(_CHILD, "conv-parent")

    assert report.merged
    assert report.files == ["notes.txt", "report/out.md"]
    assert (parent.workspace / "notes.txt").read_bytes() == b"what the delegated agent changed"
    assert (parent.workspace / "report" / "out.md").read_bytes() == b"and something new"
    # The fork is gone with it — a second copy of the parent's files kept "just in case"
    # is how a disk fills up, and what was refused is in the report.
    assert safe_key(_CHILD) not in manager._sessions
    assert not child.workspace.exists()
    assert not child.sealed.exists()


async def test_merge_back_brings_the_childs_work_out_of_the_vm_before_it_walks(
    tmp_path, monkeypatch
):
    # The delegated agent is free to leave a process running in its guest copy. Walking
    # while that copy is still in the VM would judge whatever it was halfway through
    # writing, and the walk is the one writer that runs on the host.
    vm = _vm(tmp_path)
    vault = await _vault(tmp_path)
    manager, parent, child = await _forked(tmp_path, vault, vm=vm)
    await child.run(SandboxSpec(command=["sh", "-c", "echo made > made.txt"]))
    assert child.is_warm
    real_merge = parent.merge_fork
    at_walk: list[list[tuple[str, str]]] = []

    def watched(*args):
        at_walk.append(list(vm.events))
        return real_merge(*args)

    monkeypatch.setattr(parent, "merge_fork", watched)
    report = await manager.merge_back(_CHILD, "conv-parent")

    assert ("wipe", child.key) in at_walk[0]
    assert report.files == ["made.txt"]


async def test_merge_back_refuses_to_overwrite_what_the_parent_changed_meanwhile(tmp_path):
    vault = await _vault(tmp_path)
    manager, parent, child = await _forked(tmp_path, vault)
    parent.write_file("notes.txt", b"what the parent did while it waited")
    child.write_file("notes.txt", b"what the child did")
    child.write_file("fresh.txt", b"only the child touched this")
    (child.workspace / "keep.txt").unlink()

    report = await manager.merge_back(_CHILD, "conv-parent")

    assert not report.merged
    assert report.conflicts == ["notes.txt"]
    assert (parent.workspace / "notes.txt").read_bytes() == b"what the parent did while it waited"
    # A conflict on one path does not hold the rest of the work hostage.
    assert report.files == ["fresh.txt"]
    assert (parent.workspace / "fresh.txt").exists()
    # A deletion is reported and never applied: the child dropping a file is not
    # evidence the parent wanted it gone.
    assert report.deleted == ["keep.txt"]
    assert (parent.workspace / "keep.txt").read_text() == "still wanted"


async def test_a_merge_refuses_to_write_through_a_symlink_out_of_the_workspace(tmp_path):
    # The agent in the box can point a symlink anywhere, and the merge is the one writer
    # in this path that runs on the host — outside every fence, and past the gate a merge
    # is supposed to be.
    vault = await _vault(tmp_path)
    manager, parent, child = await _forked(tmp_path, vault)
    outside = tmp_path / "outside"
    outside.mkdir()
    (parent.workspace / "docs").symlink_to(outside)
    child.write_file("docs/stolen.txt", b"this must never leave the workspace")

    report = await manager.merge_back(_CHILD, "conv-parent")

    assert report.conflicts == ["docs/stolen.txt"]
    assert not (outside / "stolen.txt").exists()


async def test_a_merge_that_never_reported_keeps_the_fork(tmp_path, monkeypatch):
    # A fork has no archive, so deleting it on a merge that produced no report — the
    # vault re-locked while the delegated agent worked, the operator pressing Stop
    # mid-walk — would lose every file that agent wrote, with nothing said about what
    # landed and no second copy to retry from.
    vault = await _vault(tmp_path)
    manager, parent, child = await _forked(tmp_path, vault)
    child.write_file("notes.txt", b"what the delegated agent changed")

    def boom(*_args):
        raise SandboxError("cannot restore the sandbox workspace: vault is locked")

    monkeypatch.setattr(parent, "merge_fork", boom)
    with pytest.raises(SandboxError):
        await manager.merge_back(_CHILD, "conv-parent")

    assert (child.workspace / "notes.txt").read_bytes() == b"what the delegated agent changed"
    # Back in the map with its tombstone released, so the merge can simply be asked for
    # again rather than the fork being stranded behind an event nothing will set.
    assert manager._sessions[safe_key(_CHILD)] is child
    assert not manager._tearing_down
    monkeypatch.undo()
    assert (await asyncio.wait_for(manager.merge_back(_CHILD, "conv-parent"), 2.0)).merged


async def test_a_fork_whose_copy_fails_releases_the_sessions_it_displaced(tmp_path, monkeypatch):
    # Those sessions are already out of the live map with a teardown tombstone each.
    # Abandoned there, every later acquire for them waits on an event nothing will set,
    # and their guest copies and plaintext workspaces are in no map a sweep can reach.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=2)
    parent = await manager.acquire("conv-parent")
    parent.ensure_workspace()
    await manager.acquire("conv-other")

    def boom(*_args):
        raise OSError("no space left on device")

    monkeypatch.setattr(session_mod, "clone_workspace", boom)
    with pytest.raises(OSError):
        await manager.fork("conv-parent", _CHILD)

    assert not manager._tearing_down
    assert safe_key(_CHILD) not in manager._sessions
    # The displaced conversation comes back rather than waiting forever on its own
    # teardown.
    await asyncio.wait_for(manager.acquire("conv-other"), 2.0)


async def test_a_fork_stopped_mid_copy_leaves_no_session_over_a_half_copy(tmp_path, monkeypatch):
    # The copy is the longest await a fork has, so an operator's Stop lands there far more
    # often than any error does — and a cancellation is not an `Exception`. Left in the
    # map, the entry counts against the cap and its half-copied *plaintext* fork sits in
    # the clear until the parent's run goes terminal and a sweep passes.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    parent = await manager.acquire("conv-parent")
    parent.ensure_workspace()

    def stopped(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(session_mod, "clone_workspace", stopped)
    with pytest.raises(asyncio.CancelledError):
        await manager.fork("conv-parent", _CHILD)

    assert safe_key(_CHILD) not in manager._sessions
    assert not (tmp_path / "sandbox" / "work" / safe_key(_CHILD)).exists()


async def test_purging_a_fork_drops_the_allowlist_alias_it_borrowed(tmp_path):
    # A fork is fenced by its parent's allowlist, and that alias is remembered in memory.
    # Nothing ever names the child key again — the delegation id lives only in the call
    # that took it — so an alias kept past the purge is one more dead pair of strings per
    # delegation for the life of the process.
    vault = await _vault(tmp_path)
    manager, _parent, _child = await _forked(tmp_path, vault)
    policy = manager._egress
    await policy.allow("conv-parent", ["example.com"])
    assert "example.com" in await policy.allowed_for(_CHILD)

    await manager.purge(_CHILD)

    assert "example.com" not in await policy.allowed_for(_CHILD)


async def test_forking_the_same_key_twice_is_refused(tmp_path):
    vault = await _vault(tmp_path)
    manager, _parent, _child = await _forked(tmp_path, vault)
    # One key names one delegation; minting a second session onto the same directory
    # would give two agents one workspace and no way to tell their work apart.
    with pytest.raises(SandboxError):
        await manager.fork("conv-parent", _CHILD)


async def test_a_refused_fork_leaves_no_allowlist_alias(tmp_path):
    # A fork refused before it has a session goes through no purge, and the purge is
    # what drops the alias — so it must not have been written yet. One dead pair of
    # strings per refused delegation for the life of the process is the leak otherwise.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    parent = await manager.acquire("conv-parent")
    parent.ensure_workspace()
    manager._tearing_down[safe_key(_CHILD)] = asyncio.Event()

    with pytest.raises(SandboxError):
        await manager.fork("conv-parent", _CHILD)

    assert manager._egress._fenced_by(_CHILD) == _CHILD


async def test_a_fork_is_not_displaced_by_the_cap_while_its_run_is_live(tmp_path):
    # Displacing a fork discards it — nothing here is ever sealed — so an unclaimed one
    # is work the operator would simply lose.
    vault = await _vault(tmp_path)
    manager, _parent, child = await _forked(tmp_path, vault, max_sessions=1)
    delegated = _Work()
    child.hold(delegated)

    await manager.acquire("conv-other")

    assert safe_key(_CHILD) in manager._sessions
    assert (child.workspace / "notes.txt").exists()


async def test_taking_a_fork_does_not_displace_the_parent_it_was_forked_from(tmp_path):
    # The conversation that just delegated is the one demonstrably mid-work. Reaping it
    # to make room for its own child would hand it back, on the very turn it is waiting
    # on that child, a cold guest copy with its processes gone.
    vault = await _vault(tmp_path)
    manager, parent, _child = await _forked(tmp_path, vault, max_sessions=1)

    assert parent.key in manager._sessions
    assert (parent.workspace / "notes.txt").exists()


async def test_the_sweep_leaves_a_fork_whose_run_is_still_live_alone(tmp_path):
    # Reaping an ordinary session is lossless — the guest copy goes and the files
    # stay — so the sweep collects a run parked on an unanswered approval. A fork has no archive to
    # come back through, and the merge that followed would report an empty workspace as
    # having landed.
    vault = await _vault(tmp_path)
    manager, _parent, child = await _forked(tmp_path, vault, idle_ttl_s=0.0)
    child.hold(_Work())

    await manager._sweep()

    assert safe_key(_CHILD) in manager._sessions
    assert (child.workspace / "notes.txt").exists()


async def test_the_sweep_discards_a_stranded_fork_instead_of_keeping_it(tmp_path):
    # What a crash leaves of a delegation: a plaintext copy of a parent workspace that
    # is already on disk under its own key. Keeping it would leave that duplicate forever,
    # under a key no conversation will ever open again.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    safe = safe_key("conv-parent#d9")
    stranded = manager._work_root / safe
    stranded.mkdir(parents=True)
    (stranded / "notes.txt").write_text("a delegated agent's leftovers")
    fork_marker(stranded).touch()

    await manager._sweep()

    assert not (manager._sealed_root / f"{safe}.tar.enc.gz").exists()
    assert not stranded.exists()
    assert not fork_marker(stranded).exists()
    assert not manager._tearing_down


# --- what an unclean shutdown leaves behind ----------------------------------
def _strand_fork(manager: SandboxSessionManager, key: str) -> Path:
    """A delegated fork on disk with nobody left to merge it — what a crash mid-
    delegation leaves behind."""
    workspace = manager._work_root / safe_key(key)
    workspace.mkdir(parents=True)
    (workspace / "notes.txt").write_text("a sub-agent's copy of its parent's files")
    fork_marker(workspace).touch()
    return workspace


async def test_sweep_leaves_a_stranded_conversation_workspace_alone(tmp_path):
    """A workspace no session owns is not residue — it is that conversation's files,
    waiting for its next turn. Collecting it would be deleting the agent's work."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    orphan = manager._work_root / safe_key("conv-orphan")
    orphan.mkdir(parents=True)
    (orphan / "notes.txt").write_text("the last process died before it could finish")

    await manager._sweep()

    assert (orphan / "notes.txt").read_text() == "the last process died before it could finish"
    assert not manager._sessions  # and nothing was revived as a session either


async def test_sweep_collects_a_stranded_delegated_fork(tmp_path):
    # The one directory a crash strands that nobody will ever ask for again: a copy of
    # a parent workspace that still exists under its own key, for a delegation that
    # ended when the process died.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    orphan = _strand_fork(manager, "conv-a/deleg-1")

    await manager._sweep()

    assert not orphan.exists()
    assert not fork_marker(orphan).exists()
    assert not manager._sessions  # collecting one does not revive it as a session
    assert not manager._tearing_down  # the tombstone it went through is released


async def test_sweep_collects_stranded_forks_a_slice_at_a_time(tmp_path):
    # Every key in a batch is tombstoned for the whole batch, and an `acquire()` that
    # lands on a tombstone waits with nothing to show the operator. A crash mid-fan-out
    # must not turn into a hundred-teardown wait for whoever opens the last one, so a
    # sweep takes a slice and the next sweep takes the rest.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    forks = [
        _strand_fork(manager, f"conv-a/deleg-{i}")
        for i in range(manager_mod._ORPHAN_FORKS_PER_SWEEP + 2)
    ]

    await manager._sweep()
    assert sum(not f.exists() for f in forks) == manager_mod._ORPHAN_FORKS_PER_SWEEP
    assert not manager._tearing_down  # and every tombstone in the slice is released

    await manager._sweep()

    assert not any(f.exists() for f in forks)


async def test_a_stranded_forks_failed_teardown_releases_its_tombstone(tmp_path, monkeypatch):
    # The tombstones for a whole batch of orphans go down before any of them is torn
    # down, and only the teardown releases them. One that fails must not leave one set
    # forever: `acquire()` for that conversation waits on it with no timeout.
    async def failing_shutdown(self) -> None:
        raise RuntimeError("the VM is having a bad day")

    monkeypatch.setattr(SandboxSession, "shutdown", failing_shutdown)
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    orphan = _strand_fork(manager, "conv-a/deleg-1")

    await manager._sweep()

    assert not manager._tearing_down
    assert orphan.exists()  # not collected, and retried by the next sweep
    assert await asyncio.wait_for(manager.acquire("conv-a/deleg-1"), timeout=1.0)


async def test_a_file_write_racing_a_fork_waits_it_out_instead_of_tearing_the_copy(
    tmp_path, monkeypatch
):
    # The file tools take no session lock — that is what keeps browsing and editing off
    # the VM's critical path — so a run that has just delegated can have its next
    # write arrive mid-clone. Interleaved, the copy is torn and its manifest records the
    # tear as the fork point, so the merge back reads surviving files as never having
    # existed. `_disk` is what serialises them.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault)
    session = await manager.acquire("conv-race")
    session.write_file("notes.txt", b"what the clone captures")
    real_clone = session_mod.clone_workspace

    def slow_clone(source, child):
        time.sleep(0.2)  # a real tree copy of a `.venv` is seconds; same window
        return real_clone(source, child)

    monkeypatch.setattr(session_mod, "clone_workspace", slow_clone)

    child = tmp_path / "fork"
    cloning = asyncio.to_thread(session.clone_into, child)
    await asyncio.sleep(0.05)  # let the clone thread get well inside the copy
    manifest, _ = await asyncio.gather(
        cloning, asyncio.to_thread(session.write_file, "late.txt", b"x")
    )

    # The write landed either side of the clone, never through it: the manifest is a
    # coherent snapshot, and both files are on the parent.
    assert set(manifest) <= {"notes.txt", "late.txt"}
    assert "notes.txt" in manifest
    assert session.read_file("notes.txt") == b"what the clone captures"
    assert session.read_file("late.txt") == b"x"


async def test_sweep_does_not_touch_a_workspace_its_own_session_still_holds(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=3600.0)
    session = await manager.acquire("conv-live")
    session.ensure_workspace()
    (session.workspace / "wip.txt").write_text("still working")

    await manager._sweep()

    assert (session.workspace / "wip.txt").exists()
    assert manager.existing("conv-live") is session


# --- the real guest scripts (only where confinement is available) -------------
confined = pytest.mark.skipif(not can_confine(), reason="needs root and bubblewrap")


@confined
async def test_a_workspace_persists_across_calls_and_across_a_reap(tmp_path):
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
