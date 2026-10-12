"""Per-conversation live sandboxes: lazy acquisition, the idle reaper, what a reap
does and does not touch, and (with a runtime) file continuity across calls and across
a reap."""

from __future__ import annotations

import asyncio
import io
import tarfile
import threading
import time
from pathlib import Path

import httpx
import pytest

import services.sandbox.manager as manager_mod
import services.sandbox.preview_tokens as preview_tokens
import services.sandbox.reconcile as reconcile_mod
import services.sandbox.session as session_mod
import services.sandbox.sidecar as sidecar_mod
from core.config import Settings
from core.vault import Vault
from services.sandbox import (
    ContainerSandbox,
    PreviewHandle,
    SandboxError,
    SandboxSession,
    SandboxSessionManager,
    SandboxSpec,
)
from services.sandbox.base import safe_key
from services.sandbox.fork import fork_marker
from services.sandbox.legacy_seal import adopt_legacy_archive, partial_marker
from services.sandbox.walk import excluded
from services.sandbox.warmup import ImageWarmup

from .conftest import egress_policy
from .test_sandbox import _runtime_ready

_EXCLUDES = Settings().sandbox_walk_excludes


async def _vault(tmp_path) -> Vault:
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    return vault


class _NoRuntime(ContainerSandbox):
    """A backend that resolves no container runtime, whatever the developer happens to
    have installed. The default for these tests, which exercise bookkeeping — sessions,
    seals, sweeps, boot reconciliation — and must never reach a real daemon: several of
    those paths issue `rm --force` / `network rm` by name, and a developer running the
    suite with the app live would otherwise have their own running sandbox removed
    from under them. A test that genuinely wants a runtime asks for one by name."""

    @property
    def runtime(self) -> str | None:
        return None


def _manager(tmp_path, vault, **overrides) -> SandboxSessionManager:
    backend = overrides.pop("backend", None) or _NoRuntime()
    opts = dict(
        egress=egress_policy(tmp_path),
        data_dir=tmp_path,
        idle_ttl_s=1800.0,
        reap_interval_s=60.0,
        excludes=_EXCLUDES,
    )
    opts.update(overrides)
    return SandboxSessionManager(backend, vault, **opts)


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
        backend=_pinned_backend(),
        vault=vault,
        excludes=(),
    )
    opts.update(overrides)
    return SandboxSession("s1", **opts)


# --- naming + exclusion ------------------------------------------------------
def test_safe_key_is_container_safe():
    key = safe_key("conv/../weird id!")
    assert key.startswith("s")
    assert all(c.isalnum() or c in "_.-" for c in key)


def test_excluded_drops_envs_and_caches_only():
    assert excluded(".venv", _EXCLUDES)
    assert excluded("pkg/__pycache__/x.pyc", _EXCLUDES)
    assert excluded("node_modules", _EXCLUDES)
    assert not excluded("analysis.py", _EXCLUDES)
    assert not excluded("output/chart.png", _EXCLUDES)


# --- write_file: staging a file into the workspace (no runtime needed) --------
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

    await session.shutdown()  # takes the container down; the files are not its business

    assert session.workspace.exists()
    assert session.read_file("attachments/keep.txt") == b"hold onto me"


# --- what a reap leaves alone ------------------------------------------------
async def test_a_reap_keeps_the_dependencies_and_history_a_run_built(tmp_path):
    """The bug this whole design exists to close: a reap used to archive the workspace
    minus `.venv`, `node_modules`, `dist` and `.git`, so the next turn found a tree
    missing exactly what the last one had spent minutes building — and re-did it."""
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
    """Reaping used to need the vault key, because it sealed. It no longer touches a
    file, so a locked vault — the state a machine sits in for hours — no longer means
    every idle container is kept alive."""
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
    # Lazy: no container or workspace exists yet, just the bookkeeping object.
    assert not first.workspace.exists()


# --- image warm-up coordination: a cold create waits, not races (sandbox-01) --
def _pinned_backend() -> ContainerSandbox:
    return ContainerSandbox(runtime="docker")


def _fake_bringup(monkeypatch, *, answer=None):
    """Fake every runtime call a bring-up or teardown makes — the session's own and the
    sidecar's — into one ordered log, with the sidecar's readiness poll short-circuited.

    One log across both modules because the *order* is the containment: the network before
    anything joins it, the sidecar answering before the box whose traffic it gates starts.
    Removals are logged as ``["rm", name]`` so a teardown reads in the same list."""
    calls: list[list[str]] = []

    async def fake_run_subprocess(argv, **_kwargs):
        calls.append(list(argv))
        if answer is not None:
            return (False, *answer(list(argv)))
        if argv[1:3] == ["network", "inspect"]:
            return False, 0, b"172.31.0.0/16\n", b""
        return False, 0, b"", b""

    async def fake_force_remove(_runtime, name, **_kwargs) -> None:
        calls.append(["rm", name])

    async def fake_ready(_runtime, _name, _marker, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(session_mod, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(session_mod, "force_remove_container", fake_force_remove)
    monkeypatch.setattr(sidecar_mod, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(sidecar_mod, "force_remove_container", fake_force_remove)
    monkeypatch.setattr(sidecar_mod, "await_log_marker", fake_ready)
    return calls


async def test_ensure_up_waits_for_a_pending_image_warmup_before_creating(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    warmup = ImageWarmup()
    warmup.start_pulling()  # simulate the background pull actually being in flight
    session = _session(tmp_path, vault, warmup=warmup)
    created = _fake_bringup(monkeypatch)

    task = asyncio.create_task(session._ensure_up())
    await asyncio.sleep(0.02)  # let it start and block on the still-pending pull
    assert not task.done()
    assert not created  # nothing created at all while the pull is in flight

    warmup.mark_done(True)  # the background pull resolves
    await asyncio.wait_for(task, timeout=1.0)
    assert session.is_warm
    assert created  # now proceeds to the (fast, image-cached) create


async def test_ensure_up_needs_no_warmup_wire_up_at_all(tmp_path, monkeypatch):
    # A bare unit-constructed session (warmup=None, the default) skips the
    # coordination outright — existing callers that don't wire one keep working.
    vault = await _vault(tmp_path)
    session = _session(tmp_path, vault)
    _fake_bringup(monkeypatch)

    await asyncio.wait_for(session._ensure_up(), timeout=1.0)
    assert session.is_warm


async def test_ensure_up_gives_a_truthful_message_when_the_pull_never_resolves(
    tmp_path, monkeypatch
):
    vault = await _vault(tmp_path)
    warmup = ImageWarmup()
    warmup.start_pulling()  # in flight, and never marked done — simulates a stuck pull
    session = _session(tmp_path, vault, warmup=warmup)
    monkeypatch.setattr(session_mod, "IMAGE_PULL_TIMEOUT_S", 0.05)

    with pytest.raises(SandboxError, match="still downloading"):
        await asyncio.wait_for(session._ensure_up(), timeout=1.0)


async def test_ensure_up_proceeds_when_the_pull_resolved_but_failed(tmp_path, monkeypatch):
    # The pull resolved (event set) but found nothing cached either — that's a
    # genuinely-unavailable image, not "still downloading"; let the ordinary
    # create attempt run and report its own real error (fail-closed, unchanged).
    vault = await _vault(tmp_path)
    warmup = ImageWarmup()
    warmup.mark_done(False)
    session = _session(tmp_path, vault, warmup=warmup)

    def answer(argv):
        # Only the session container's create fails: the fence comes up fine, the image
        # the *workspace* runs is the one that isn't there.
        if "odysseus-sbx-s1" in argv:
            return 1, b"", b"no such image"
        if argv[1:3] == ["network", "inspect"]:
            return 0, b"172.31.0.0/16\n", b""
        return 0, b"", b""

    _fake_bringup(monkeypatch, answer=answer)

    with pytest.raises(SandboxError, match="failed to start sandbox session"):
        await asyncio.wait_for(session._ensure_up(), timeout=1.0)


# --- the fence: an internal network and its proxy, raised before anything runs ---
async def test_ensure_up_creates_network_sidecar_then_container_in_order(tmp_path, monkeypatch):
    vault = await _vault(tmp_path)
    allow_dir = tmp_path / "egress" / "s1"
    session = _session(tmp_path, vault, egress_dir=allow_dir, proxy_image="python:alpine")
    calls = _fake_bringup(monkeypatch)

    await asyncio.wait_for(session._ensure_up(), timeout=1.0)

    runtime_calls = [c for c in calls if c[0] != "rm"]
    assert [c[1:3] for c in runtime_calls] == [
        ["network", "create"],  # the wall exists before anything joins it
        ["network", "inspect"],  # its subnet is what the proxy refuses outsiders by
        ["run", "--detach"],  # the sidecar
        ["network", "connect"],  # ...and only then its leg on the open web
        ["run", "--detach"],  # the workspace container, last
    ]
    create, connect, workspace = runtime_calls[2], runtime_calls[3], runtime_calls[4]
    assert "odysseus-net-s1" in create and "odysseus-egress-s1" in create
    assert f"{allow_dir}:/allow:ro" in create  # the allowlist, never writable from inside
    assert "--env EGRESS_CLIENT_SUBNET=172.31.0.0/16" in " ".join(create)
    assert "python:alpine" in create
    assert connect[1:] == ["network", "connect", "bridge", "odysseus-egress-s1"]
    joined = " ".join(workspace)
    assert "--network odysseus-net-s1" in joined
    assert "--env HTTPS_PROXY=http://odysseus-egress-s1:3128" in joined
    assert "--env https_proxy=http://odysseus-egress-s1:3128" in joined  # curl reads this one
    assert "--env NO_PROXY=localhost,127.0.0.1,odysseus-egress-s1" in joined


async def test_kill_removes_container_sidecar_and_network(tmp_path, monkeypatch):
    # Innermost first: a network cannot be removed while anything is still attached to
    # it, so the order here is not cosmetic — reversed, the network removal fails and the
    # next boot inherits it.
    vault = await _vault(tmp_path)
    session = _session(tmp_path, vault)
    calls = _fake_bringup(monkeypatch)
    await asyncio.wait_for(session._ensure_up(), timeout=1.0)
    calls.clear()

    await session._kill()

    assert calls == [
        ["rm", "odysseus-sbx-s1"],
        ["rm", "odysseus-egress-s1"],
        ["docker", "network", "rm", "odysseus-net-s1"],
    ]


# --- the idle reaper ---------------------------------------------------------
async def test_reaper_drops_an_idle_session_and_keeps_its_files(tmp_path):
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, idle_ttl_s=0.0)
    session = await manager.acquire("conv-a")
    session.write_file("notes.txt", b"keep me")

    await manager._sweep()

    assert not manager._sessions  # reaped from the registry
    assert session.workspace.exists()  # the container went; the files did not

    # Resuming the conversation finds them exactly where it left them.
    revived = await manager.acquire("conv-a")
    assert (revived.ensure_workspace() / "notes.txt").read_text() == "keep me"


async def test_start_stop_manages_the_reaper_task(tmp_path):
    vault = await _vault(tmp_path)
    # `start()` reconciles first, and reconciliation removes containers by name — with
    # the runtime-less default backend it has nothing to talk to, which is the point.
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
    # between. Displacing in one of those gaps seals the workspace — and the seal drops
    # `node_modules`/`.venv`/`.git` by design, so the run's next tool call would come back
    # to a workspace missing exactly what it just spent minutes installing.
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
    tearing down somebody *else's* conversation. Cancelled partway, its containers would
    be left out of every map — no sweep can find them and no purge names them."""
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    displaced = await manager.acquire("conv-a")
    displaced.write_file("notes.txt", b"keep me")

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
    assert not displaced._running
    assert displaced.read_file("notes.txt") == b"keep me"
    assert set(manager._sessions) == {safe_key("conv-b")}


async def test_a_failed_seal_leaves_the_session_live_for_the_sweeper(tmp_path):
    # A session dropped from the map on a failed teardown is a container nothing will
    # ever reap and a workspace nothing will ever seal. Put it back and let the idle
    # sweep try again.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, max_sessions=1)
    displaced = await manager.acquire("conv-a")

    async def failing_shutdown() -> None:
        raise RuntimeError("the archive could not be written")

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
    return PreviewHandle(
        token=token, container="c", host_port=1, container_port=2, command=("srv",)
    )


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
    # The seal drops a virtualenv because it is rebuildable; a fork keeps it because
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


async def test_merge_back_drops_the_childs_boxes_before_it_walks(tmp_path, monkeypatch):
    # They have the fork bind-mounted at /work, and the delegated agent is free to leave a
    # process running in there. Walking under a live mount copies whatever that process is
    # halfway through writing — and gives it the window to swing a path the host-side walk
    # has already judged onto a host file the box itself cannot see.
    removed_at_walk: list[list[str]] = []
    removed: list[str] = []

    async def fake_force_remove(_runtime, name: str, **_kwargs) -> None:
        removed.append(name)

    _fake_runtime_calls(monkeypatch)
    monkeypatch.setattr(session_mod, "force_remove_container", fake_force_remove)
    vault = await _vault(tmp_path)
    manager, parent, child = await _forked(tmp_path, vault, backend=_pinned_backend())
    child.write_file("notes.txt", b"what the delegated agent changed")
    real_merge = parent.merge_fork

    def watched(*args):
        removed_at_walk.append(list(removed))
        return real_merge(*args)

    monkeypatch.setattr(parent, "merge_fork", watched)
    await manager.merge_back(_CHILD, "conv-parent")

    assert child.container in removed_at_walk[0]
    assert child._preview_container in removed_at_walk[0]


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
    # and their containers and plaintext workspaces are in no map a sweep can reach.
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
    # The conversation that just delegated is the one demonstrably mid-work. Sealing it
    # to make room for its own child would hand it back, on the very turn it is waiting
    # on that child, a workspace missing everything the seal drops.
    vault = await _vault(tmp_path)
    manager, parent, _child = await _forked(tmp_path, vault, max_sessions=1)

    assert parent.key in manager._sessions
    assert (parent.workspace / "notes.txt").exists()


async def test_the_sweep_leaves_a_fork_whose_run_is_still_live_alone(tmp_path):
    # Reaping an ordinary session is lossless — it seals, and the next run restores — so
    # the sweep collects a run parked on an unanswered approval. A fork has no archive to
    # come back through, and the merge that followed would report an empty workspace as
    # having landed.
    vault = await _vault(tmp_path)
    manager, _parent, child = await _forked(tmp_path, vault, idle_ttl_s=0.0)
    child.hold(_Work())

    await manager._sweep()

    assert safe_key(_CHILD) in manager._sessions
    assert (child.workspace / "notes.txt").exists()


async def test_the_sweep_discards_a_stranded_fork_instead_of_sealing_it(tmp_path):
    # What a crash leaves of a delegation: a plaintext copy of a parent workspace that
    # is already archived under its own key. Sealing it would file that duplicate at
    # rest forever, under a key no conversation will ever open again.
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


# --- boot reconciliation + sealing what an unclean shutdown left --------------
def _fake_runtime_calls(monkeypatch, replies: dict[str, tuple[int, bytes]] | None = None):
    """Record every runtime argv and answer canned output per subcommand — the
    listings reconciliation reads, with no real runtime anywhere near it."""
    calls: list[list[str]] = []
    answers = replies or {}

    async def fake_run_subprocess(argv, **_kwargs):
        calls.append(list(argv))
        code, out = answers.get(" ".join(argv[1:3]), (0, b""))
        return False, code, out, b""

    monkeypatch.setattr(reconcile_mod, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(session_mod, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(sidecar_mod, "run_subprocess", fake_run_subprocess)
    return calls


async def test_reconcile_removes_stale_named_containers_and_networks(tmp_path, monkeypatch):
    # What a crash leaves behind is invisible to every map the manager keeps: a
    # container nothing will exec into, a network nothing joins, and the scratch dirs
    # of a pool this build no longer runs. Boot is the one moment clearing them
    # wholesale is safe, so this is where it has to happen.
    removed: list[str] = []

    async def fake_force_remove(_runtime, name: str, **_kwargs) -> None:
        removed.append(name)

    calls = _fake_runtime_calls(
        monkeypatch,
        {
            "ps -a": (0, b"odysseus-sbx-sconv-a\nodysseus-pre-sconv-a\nodysseus-egress-sconv-b\n"),
            "network ls": (0, b"odysseus-net-sconv-a\n"),
        },
    )
    monkeypatch.setattr(reconcile_mod, "force_remove_container", fake_force_remove)
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=_pinned_backend())
    leftover_pool_dir = manager._work_root / "_spare-1-abcd"
    leftover_pool_dir.mkdir(parents=True)
    kept = manager._work_root / safe_key("conv-real")
    kept.mkdir(parents=True)

    await manager.reconcile()

    # The listings are anchored to our own names — an unanchored filter would sweep
    # up a container the operator merely named after the project.
    listing = [c for c in calls if c[1] in ("ps", "network")]
    assert listing[0] == [
        "docker",
        "ps",
        "-a",
        "--filter",
        "name=^odysseus-(sbx|pre|egress)-",
        "--format",
        "{{.Names}}",
    ]
    assert listing[1] == [
        "docker",
        "network",
        "ls",
        "--filter",
        "name=^odysseus-net-",
        "--format",
        "{{.Name}}",
    ]
    assert removed == [
        "odysseus-sbx-sconv-a",
        "odysseus-pre-sconv-a",
        "odysseus-egress-sconv-b",
    ]
    assert ["docker", "network", "rm", "odysseus-net-sconv-a"] in calls
    assert not leftover_pool_dir.exists()
    assert kept.exists()  # a conversation's own workspace is not pool scratch


async def test_reconcile_survives_a_runtime_that_cannot_answer(tmp_path, monkeypatch):
    # A daemon that is down at boot is a reason to skip reconciliation, never to
    # stop the app from starting.
    _fake_runtime_calls(monkeypatch, {"ps -a": (1, b""), "network ls": (1, b"")})
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=_pinned_backend())

    await manager.reconcile()  # must not raise


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


async def test_collecting_a_stranded_fork_first_drops_the_containers_holding_it(
    tmp_path, monkeypatch
):
    # The dead process's containers can still be alive with this very directory mounted
    # at /work — boot reconciliation is skipped whenever the runtime was not up yet.
    # Deleting under a live mount sends their later writes to a deleted inode, so every
    # mount comes off first — the egress box included, which is the one a cancelled
    # network call leaves behind.
    removed: list[tuple[str, bool]] = []
    safe = safe_key("conv-a/deleg-1")

    async def fake_force_remove(_runtime, name: str, **_kwargs) -> None:
        removed.append((name, orphan.exists()))

    _fake_runtime_calls(monkeypatch)
    monkeypatch.setattr(session_mod, "force_remove_container", fake_force_remove)
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=_pinned_backend())
    orphan = _strand_fork(manager, "conv-a/deleg-1")

    await manager._sweep()

    # Still on disk at every removal — the directory goes only once nothing holds it.
    assert removed == [
        (f"odysseus-sbx-{safe}", True),
        (f"odysseus-pre-{safe}", True),
        (f"odysseus-egress-{safe}", True),
    ]
    assert not orphan.exists()


async def test_dropping_the_mounts_is_bounded_so_the_collection_still_happens(
    tmp_path, monkeypatch
):
    # A whole batch of orphans is tombstoned while their mounts come off, and every
    # `acquire()` for those keys waits behind it with nothing to show the operator. So a
    # daemon that has stopped answering costs a bounded pause and then the collection
    # goes ahead — the removals it did not manage are the next boot's problem.
    removed: list[str] = []

    async def fake_force_remove(_runtime, name: str, **_kwargs) -> None:
        removed.append(name)

    _fake_runtime_calls(monkeypatch)
    monkeypatch.setattr(session_mod, "force_remove_container", fake_force_remove)
    monkeypatch.setattr(session_mod, "_MOUNT_RELEASE_BUDGET_S", 0.0)
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=_pinned_backend())
    orphan = _strand_fork(manager, "conv-a/deleg-1")

    await manager._sweep()

    assert removed == []  # not even one call is worth making with no budget left
    assert not orphan.exists()
    assert not manager._tearing_down


async def test_a_stranded_workspace_releases_its_tombstone_even_if_the_mounts_will_not_drop(
    tmp_path, monkeypatch
):
    # The tombstones for a whole batch of orphans go down before any of them is sealed,
    # and only the teardown releases them. A removal that raises — or a reaper cancelled
    # mid-eviction — must not leave one set forever: `acquire()` for that conversation
    # waits on it with no timeout and nothing to show the operator.
    async def fake_force_remove(_runtime, _name: str, **_kwargs) -> None:
        raise RuntimeError("the daemon is having a bad day")

    _fake_runtime_calls(monkeypatch)
    monkeypatch.setattr(session_mod, "force_remove_container", fake_force_remove)
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=_pinned_backend())
    safe = safe_key("conv-orphan")
    orphan = manager._work_root / safe
    orphan.mkdir(parents=True)
    (orphan / "notes.txt").write_text("plaintext the last process never sealed")

    await manager._sweep()

    assert not manager._tearing_down  # released, so the next acquire is not stuck
    assert await asyncio.wait_for(manager.acquire("conv-orphan"), timeout=1.0)


async def test_reconcile_stops_when_its_boot_budget_is_spent(tmp_path, monkeypatch):
    # Boot waits on this pass, and a daemon that accepts the connection without ever
    # answering would otherwise park startup there — per-container timeouts alone still
    # let a hundred leftovers add up. What the budget cuts short the next boot finishes.
    removed: list[str] = []

    async def fake_force_remove(_runtime, name: str, **_kwargs) -> None:
        removed.append(name)

    calls = _fake_runtime_calls(
        monkeypatch,
        {"ps -a": (0, b"odysseus-sbx-sconv-a\n"), "network ls": (0, b"odysseus-net-sconv-a\n")},
    )
    monkeypatch.setattr(reconcile_mod, "force_remove_container", fake_force_remove)
    monkeypatch.setattr(reconcile_mod, "_BUDGET_S", 0.0)
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=_pinned_backend())

    await manager.reconcile()

    assert calls == []  # not even the listing is worth a call with no budget left
    assert removed == []


async def test_a_file_write_racing_a_fork_waits_it_out_instead_of_tearing_the_copy(
    tmp_path, monkeypatch
):
    # The file tools take no session lock — that is what keeps browsing and editing off
    # the container's critical path — so a run that has just delegated can have its next
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


# --- exec runtime faults heal by container rebuild, not model flailing --------
_OCI_FAULT = (
    b"OCI runtime exec failed: exec failed: unable to start container process: "
    b"current working directory is outside of container mount namespace root "
    b"-- possible container breakout detected\r\n"
)


async def _healing_session(tmp_path, monkeypatch, exec_results):
    """A session whose fake runtime pops one canned (code, stdout) per exec and
    succeeds every container create; returns (session, calls, removed)."""
    calls: list[list[str]] = []
    removed: list[str] = []

    async def fake_run_subprocess(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "exec":
            code, out = exec_results.pop(0)
            return False, code, out, b""
        if argv[1:3] == ["network", "inspect"]:
            return False, 0, b"172.31.0.0/16\n", b""
        return False, 0, b"", b""

    async def fake_force_remove(_runtime, name: str, **_kwargs) -> None:
        removed.append(name)

    async def fake_ready(_runtime, _name, _marker, **_kwargs) -> bool:
        return True

    monkeypatch.setattr(session_mod, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(session_mod, "force_remove_container", fake_force_remove)
    monkeypatch.setattr(sidecar_mod, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(sidecar_mod, "force_remove_container", fake_force_remove)
    monkeypatch.setattr(sidecar_mod, "await_log_marker", fake_ready)
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=_pinned_backend())
    session = await manager.acquire("conv-heal")
    return session, calls, removed


async def test_exec_runtime_fault_rebuilds_the_container_and_retries(tmp_path, monkeypatch):
    # The failure this guards: a broken warm container (e.g. a stale workdir
    # mount) used to surface every exec as a "code failure" the model could only
    # flail at — now the container is rebuilt and the exec retried once.
    session, calls, removed = await _healing_session(
        tmp_path, monkeypatch, [(128, _OCI_FAULT), (0, b"healed")]
    )

    result = await session.run(SandboxSpec(command=["bash", "-c", "true"], timeout_s=5))

    assert result.ok
    assert result.stdout == "healed"
    # _ensure_up pre-clears the name once per create; the middle removal is the
    # heal tearing the broken container down.
    assert removed.count(session.container) == 3
    creates = [a for a in calls if a[1] == "run" and session.container in a]
    assert len(creates) == 2  # create + rebuild
    assert len([a for a in calls if a[1] == "exec"]) == 2  # fault + retry


async def test_exec_runtime_fault_twice_raises_a_legible_sandbox_error(tmp_path, monkeypatch):
    session, _calls, _removed = await _healing_session(
        tmp_path, monkeypatch, [(128, _OCI_FAULT), (128, _OCI_FAULT)]
    )

    with pytest.raises(SandboxError, match="container rebuild"):
        await session.run(SandboxSpec(command=["bash", "-c", "true"], timeout_s=5))


async def test_ordinary_code_failure_is_not_mistaken_for_a_runtime_fault(tmp_path, monkeypatch):
    session, calls, removed = await _healing_session(
        tmp_path, monkeypatch, [(1, b"NameError: x is not defined")]
    )

    result = await session.run(SandboxSpec(command=["python", "-c", "x"], timeout_s=5))

    assert not result.ok
    assert result.exit_code == 1
    # Only the bring-up's own pre-create clears (the container's, then the sidecar's) —
    # no heal teardown, no rebuild: the failure goes back to the model to fix.
    assert removed == [session.container, session._egress]
    assert len([a for a in calls if a[1] == "run" and session.container in a]) == 1
    assert len([a for a in calls if a[1] == "exec"]) == 1


# --- live container (only when a real runtime is present) --------------------
@pytest.mark.container
@pytest.mark.skipif(not _runtime_ready(), reason="no usable container runtime")
async def test_live_session_persists_files_across_calls(tmp_path):
    vault = await _vault(tmp_path)
    # The one test here that wants the developer's actual runtime rather than the
    # runtime-less default.
    manager = _manager(tmp_path, vault, backend=ContainerSandbox())
    try:
        session = await manager.acquire("conv-a")
        wrote = await session.run(
            SandboxSpec(command=["bash", "-c", "echo persisted > note.txt"], timeout_s=60)
        )
        assert wrote.ok
        # A later call in the same session sees the file the earlier one wrote.
        read = await session.run(SandboxSpec(command=["bash", "-c", "cat note.txt"], timeout_s=60))
        assert "persisted" in read.stdout
    finally:
        await manager.stop()


# The fence, exercised the way the agent meets it: not a flag, but a real proxy the
# workspace's traffic actually goes through. `curl` is not in the sandbox image (and
# `apt` can never work in a capability-dropped non-root box), so the probes are Python.
_FETCH = (
    "import sys, urllib.request, urllib.error\n"
    "try:\n"
    "    print(urllib.request.urlopen(sys.argv[1], timeout=30).status)\n"
    "except urllib.error.HTTPError as exc:\n"
    "    print(exc.code); print(exc.read().decode('utf-8', 'replace'))\n"
)


@pytest.mark.container
@pytest.mark.skipif(not _runtime_ready(), reason="no usable container runtime")
async def test_pip_install_through_the_egress_proxy(tmp_path):
    # The whole point of the shape: a package index is on the installation-wide list, so
    # installing is ordinary work that costs no approval — through a proxy, on a network
    # with no route of its own.
    vault = await _vault(tmp_path)
    manager = _manager(
        tmp_path,
        vault,
        backend=ContainerSandbox(),
        egress=egress_policy(tmp_path, ("pypi.org", "files.pythonhosted.org")),
    )
    try:
        session = await manager.acquire("conv-egress")
        install = await session.run(
            SandboxSpec(command=["pip", "install", "--no-cache-dir", "six"], timeout_s=300)
        )
        assert install.ok, install.stderr
        imported = await session.run(
            SandboxSpec(command=["python", "-c", "import six; print(six.__name__)"], timeout_s=60)
        )
        assert imported.ok
        assert "six" in imported.stdout
    finally:
        await manager.stop()


@pytest.mark.container
@pytest.mark.skipif(not _runtime_ready(), reason="no usable container runtime")
async def test_unlisted_host_is_denied_with_marker(tmp_path):
    # A refusal has to say which fence refused and which host, in the body the agent
    # actually reads — an anonymous 403 is something it would only try to route around.
    vault = await _vault(tmp_path)
    manager = _manager(
        tmp_path, vault, backend=ContainerSandbox(), egress=egress_policy(tmp_path, ("pypi.org",))
    )
    try:
        session = await manager.acquire("conv-denied")
        result = await session.run(
            SandboxSpec(command=["python", "-c", _FETCH, "http://example.com/"], timeout_s=60)
        )
        assert "403" in result.stdout
        assert "odysseus-egress: denied example.com" in result.stdout
        # And the tunnelled form is refused too — CONNECT is checked before it is opened.
        tunnelled = await session.run(
            SandboxSpec(command=["python", "-c", _FETCH, "https://example.com/"], timeout_s=60)
        )
        assert "200" not in tunnelled.stdout
    finally:
        await manager.stop()


@pytest.mark.container
@pytest.mark.skipif(not _runtime_ready(), reason="no usable container runtime")
async def test_a_preview_is_published_by_the_sidecar_and_answers(tmp_path):
    # The preview container publishes nothing (it cannot, on an internal network): the
    # sidecar owns the loopback port and relays inward.
    vault = await _vault(tmp_path)
    manager = _manager(tmp_path, vault, backend=ContainerSandbox(), preview_startup_timeout_s=60.0)
    try:
        handle = await manager.start_preview(
            "conv-preview", ["python", "-m", "http.server", "8000"], 8000
        )
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{handle.host_port}/", timeout=30.0)
        assert resp.status_code == 200
    finally:
        await manager.stop()
