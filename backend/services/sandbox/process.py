"""Running a command on the operator's real machine, and reaping the tree it starts.

Two paths land here — the host shell and ``code_run_host_command``, the approval-gated
escape hatch — and they differ only in what they do with the streams afterwards. The
mechanics live beside the fence rather than inside it (``host.py``) so that *what a command
may reach* and *how a process is started and killed* have separate reasons to change.

Nothing here decides the first of those. Every caller rewrites its command through
:func:`services.sandbox.host.confine` before it gets this far. What this module guarantees
is that whatever was started can be stopped **whole** — a command that backgrounds a
server must not leave it running once the run that started it is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from pydantic_ai_harness.shell._capability import LLM_API_KEY_ENV_PATTERNS

from .base import HostExecutionError, SandboxResult
from .gitenv import git_config_pins
from .host import HostConfinement, confine

#: How long a process group gets to shut itself down before it is killed outright. Long
#: enough for a server to close its listeners and drop a lockfile, short enough that an
#: agent stopping a background command does not sit there waiting for it.
_GRACE_S = 2.0


#: Process groups started by :func:`spawn_confined` that may still be alive, each with the
#: leader process that opened it. A command that backgrounds a server returns, its shell is
#: reaped, and the group lives on — nothing else remembers it, so without this a quit app
#: left every such server running on the operator's machine. Pruned on each spawn, so it
#: never grows past what is actually running.
_groups: dict[int, asyncio.subprocess.Process] = {}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: it exists but is not ours to signal — not ours to track either
        return False
    return True


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: something holds the number, which is all this asks
        return True
    return True


def _group_is_ours(pgid: int, leader: asyncio.subprocess.Process) -> bool:
    """Whether ``pgid`` still names the group ``leader`` opened, rather than a number the
    OS has since handed to something else.

    A group id is its leader's pid, and a pid is free for reuse the moment nothing holds
    it. While the group has any member the kernel keeps the number reserved, so a live
    group at ``pgid`` is ours as long as no *other* process owns the pid. Once the group
    empties it is not: the number can be given to an unrelated session leader — the
    operator's own shell — and signalling it by number would kill that.

    Two states are provably ours. The leader has not been reaped (``returncode`` is
    ``None``): it, or its zombie, still holds the pid. Or the leader has been reaped and
    nothing holds the pid but the group still has members: the kernel only keeps a pid
    out of reuse for a live group, so the group is the one we opened. A reaped leader with
    a process *at* ``pgid`` means the number was reused, which in turn means our group is
    already gone. No process listing, no ancestry walk, no dependency — both probes are
    signal-0 calls that behave the same on macOS and Linux.

    The one residual window is the instant between the leader being reaped and asyncio
    recording ``returncode`` on the loop; a number reused inside it is read as the
    leader's. It needs the group to empty and the OS to recycle the pid within that
    interval, which is why it is documented rather than closed.
    """
    if leader.returncode is not None and _pid_exists(pgid):
        return False
    return _group_alive(pgid)


async def stop_all_groups(grace_s: float = 1.0) -> None:
    """SIGTERM every process group this app started and still has running, give them
    ``grace_s`` to leave, then SIGKILL what remains. Called once, from app shutdown.

    Ownership is re-checked (:func:`_group_is_ours`) before each signal and on every poll
    of the grace window, not once up front: a group that empties during the grace period
    frees its number, and the SIGKILL pass must not land on whatever claimed it.
    """
    tracked = dict(_groups)
    _groups.clear()
    live = {g: p for g, p in tracked.items() if _group_is_ours(g, p)}
    for pgid in live:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + grace_s
    while live and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        live = {g: p for g, p in live.items() if _group_is_ours(g, p)}
    for pgid in live:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group, not just the shell — otherwise a child the command
    spawned (a server, a backgrounded job) outlives whatever stopped its parent."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        proc.kill()
    except OSError:  # already reaped, or no such group
        pass


async def terminate_tree(proc: asyncio.subprocess.Process) -> None:
    """Ask the process group to stop, then make it, and wait for it either way.

    What a `SIGKILL` alone costs is everything the process would have done on its way out:
    a dev server never closes its socket, a compose stack leaves its services up, a test
    run leaves a scratch database and a lockfile behind. Killing outright is still the
    fallback, because a process that ignores `SIGTERM` must not be able to hold a turn
    open — but it is the fallback, not the opening move.

    Not for teardown paths: a cancelled coroutine cannot await, and :func:`kill_tree` is
    the version that reaps without one.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError, PermissionError:
        proc.terminate()
    except OSError:  # already reaped, or no such group
        return
    try:
        _ = await asyncio.wait_for(proc.wait(), timeout=_GRACE_S)
    except TimeoutError:
        kill_tree(proc)
        _ = await proc.wait()


def filtered_env() -> dict[str, str]:
    """The environment every command this process spawns on the host runs in.

    Two rules, and they are here together because both answer the same question — what a
    command reads out of its environment without trying — and because a caller that built
    its own environment to get one of them would silently lose the other. That is not
    hypothetical: passing the git pins in as an explicit ``env`` is exactly how the
    credential filter came to be skipped on the host hatch.

    **The operator's model credentials are removed.** Not a boundary — a same-user process
    can reach the parent's environment through the OS — but the fence denies the paths
    those keys are *stored* at, and this keeps them out of the one place a command reads
    for free. A key denied to the shell and handed to the approved host command would be
    the same secret guarded on one route and not the other.

    **The git settings a repository must not choose for us are pinned over it**
    (``gitenv.py``) — otherwise `git status` in a cloned worktree runs whatever that
    repository's own config names as a filesystem monitor, and a pager blocks on a pipe.
    Applied over the *filtered* environment, so an operator already passing git settings
    this way is continued rather than overwritten and the two rules compose in one
    direction only.
    """
    return git_config_pins(
        {
            name: value
            for name, value in os.environ.items()
            if not any(fnmatch.fnmatchcase(name, p) for p in LLM_API_KEY_ENV_PATTERNS)
        }
    )


async def spawn_confined(
    command: str,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdout: int | IO[bytes],
    stderr: int | IO[bytes],
) -> asyncio.subprocess.Process:
    """Start ``command`` in a session of its own, so :func:`kill_tree` can reach it.

    The spawn's own errors are left to propagate unwrapped. The two callers read them
    differently — the shell turns a working directory the model's last command deleted
    into something it can retry, while the hatch reports any launch failure as one — and
    flattening them here would take that distinction away from both.
    """
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,  # own process group, so we can kill the whole tree
    )
    # The session leader's pid is the group id. Recorded so app shutdown can reach a
    # backgrounded child whose shell has long since exited (see ``_groups``).
    for pgid, leader in list(_groups.items()):
        if not _group_is_ours(pgid, leader):
            del _groups[pgid]
    _groups[proc.pid] = proc
    return proc


async def run_on_host(
    command: str,
    *,
    timeout_s: float = 120.0,
    confinement: HostConfinement | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> SandboxResult:
    """Run ``command`` in the host shell, after approval. Bounded by a wall-clock
    timeout; the process group is killed on overrun — and on cancellation, so a stopped
    run never leaves the command's tree running on the operator's machine.

    ``confinement`` is resolved by the caller (see
    :func:`services.sandbox.host.resolve_confinement`) and passed in rather than looked up
    here, so the tool reports the same fence it asked for. ``None`` runs the command
    unconfined.

    ``env`` defaults to :func:`filtered_env` rather than to the backend's own — the fence
    denies the *paths* the operator's model keys live at and says nothing about the
    environment, so inheriting it whole would hand them to the one command an operator
    approved without ever being shown that it could read them.
    """
    if confinement is not None and confinement.active:
        try:
            command = await confine(
                command,
                allowed_domains=confinement.allowed_domains,
                allow_write=confinement.allow_write,
                deny_read=confinement.deny_read,
            )
        except Exception as exc:  # noqa: BLE001 - wrapping must never lose the command
            raise HostExecutionError(f"failed to confine host command: {exc}") from exc
    try:
        proc = await spawn_confined(
            command,
            cwd=cwd,
            env=env if env is not None else filtered_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        raise HostExecutionError(f"failed to launch host command: {exc}") from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        kill_tree(proc)
        _ = await proc.wait()
        return SandboxResult(
            exit_code=124, stdout="", stderr="host command timed out", timed_out=True
        )
    except BaseException:
        # Cancellation lands here — the run hit its inactivity/wall-clock bound, or the
        # operator pressed Stop. Unwinding without reaping would leave the approved
        # command's whole tree alive on the operator's real machine with no run left to
        # stop it, which is precisely what the process group exists to prevent. Not
        # awaited: this coroutine is already being torn down, and the group is signalled.
        kill_tree(proc)
        raise
    return SandboxResult(
        exit_code=proc.returncode or 0,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )
