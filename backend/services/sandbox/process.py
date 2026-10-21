"""Running a command on the operator's real machine, and reaping the tree it starts.

Two paths land here: ``code_run_host_command``, the approval-gated escape hatch, and the
host shell in a project folder. Both go through :func:`run_on_host`, which spawns, times out,
kills the process tree and handles cancellation in one place. The mechanics live beside the
fence rather than inside it (``fence.py``) so that *what a command may reach* and *how a
process is started and killed* have separate reasons to change.

Nothing here decides what a command may reach. :func:`run_on_host` wraps a command with
:func:`services.sandbox.fence.fence_command` when it is handed a confinement, and runs it as
given when it is not or the confinement is inactive. The host shell passes the fence it
resolves (``shell_confinement``); ``code_run_host_command`` passes its own. What this module
guarantees is that whatever was started can be stopped **whole** — a command that backgrounds
a server must not leave it running once the run that started it is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import signal
import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from pydantic_ai_harness.shell._capability import LLM_API_KEY_ENV_PATTERNS

from .base import HostExecutionError, SandboxResult
from .fence import HostConfinement, fence_command
from .gitenv import git_config_pins
from .limits import TIMEOUT_EXIT
from .shell_result import ProgressHook
from .streams import Tail, read_text, stream_files, unlink

#: How long a process group gets to shut itself down before it is killed outright. Long
#: enough for a server to close its listeners and drop a lockfile, short enough that an
#: agent stopping a background command does not sit there waiting for it.
_GRACE_S = 2.0


#: Process groups started by :func:`spawn_in_session` that may still be alive, each with the
#: leader process that opened it. A command that backgrounds a server returns, its shell is
#: reaped, and the group lives on — nothing else remembers it, so without this a quit app
#: left every such server running on the operator's machine. Pruned on each spawn, so it
#: never grows past what is actually running.
groups: dict[int, asyncio.subprocess.Process] = {}


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


def group_is_ours(pgid: int, leader: asyncio.subprocess.Process) -> bool:
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

    Ownership is re-checked (:func:`group_is_ours`) before each signal and on every poll
    of the grace window, not once up front: a group that empties during the grace period
    frees its number, and the SIGKILL pass must not land on whatever claimed it.
    """
    tracked = dict(groups)
    groups.clear()
    live = {g: p for g, p in tracked.items() if group_is_ours(g, p)}
    for pgid in live:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + grace_s
    while live and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        live = {g: p for g, p in live.items() if group_is_ours(g, p)}
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
    except BaseException:
        # Cancelled mid-grace: the group was only asked to stop, and a process that
        # ignores SIGTERM would outlive the run.
        kill_tree(proc)
        raise


def _kill_leftovers(proc: asyncio.subprocess.Process) -> None:
    """Kill what is still running in the group ``proc`` led, once ``proc`` itself has
    exited. Never signals any other group."""
    if group_is_ours(proc.pid, proc):
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)


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


async def spawn_in_session(
    command: str,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdout: int | IO[bytes],
    stderr: int | IO[bytes],
) -> asyncio.subprocess.Process:
    """Start ``command`` through the host shell in a session of its own, so
    :func:`kill_tree` can reach it. The fence, where one applies, is the caller's
    to apply first (:func:`~services.sandbox.fence.fence_command`).

    The spawn's own errors are left to propagate unwrapped, so each caller decides how a
    launch failure reads.
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
    # backgrounded child whose shell has long since exited (see ``groups``).
    for pgid, leader in list(groups.items()):
        if not group_is_ours(pgid, leader):
            del groups[pgid]
    groups[proc.pid] = proc
    return proc


async def run_on_host(
    command: str,
    *,
    timeout_s: float = 120.0,
    confinement: HostConfinement | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    on_progress: ProgressHook | None = None,
    kill_leftovers: bool = True,
) -> SandboxResult:
    """Run ``command`` in the host shell, after approval. Bounded by a wall-clock
    timeout; the process group is terminated on overrun — and killed on cancellation, so a
    stopped run never leaves the command's tree running on the operator's machine. A
    timed-out command reports exit 124 with whatever it printed.

    ``on_progress``, where given, is handed each new piece of output as the command runs.
    It is watched from a task of its own so a hook that blocks cannot delay the process
    being reaped, and the watcher is torn down on every way out of this function.

    ``kill_leftovers`` kills what the command left running in its process group once the
    shell itself has exited, so a command that backgrounds a child does not outlive the
    call. A caller that keeps background processes on purpose passes ``False``.

    A launch failure raises :class:`HostExecutionError` with the spawn's own ``OSError``
    or ``ValueError`` as its cause.

    ``confinement`` is resolved by the caller (see
    :func:`services.sandbox.fence.resolve_confinement`) and passed in rather than looked up
    here, so the tool reports the same fence it asked for. ``None``, or an inactive one,
    runs the command unconfined.

    ``env`` defaults to :func:`filtered_env` rather than to the backend's own — the fence
    denies the *paths* the operator's model keys live at and says nothing about the
    environment, so inheriting it whole would hand them to the one command an operator
    approved without ever being shown that it could read them.
    """
    command = await fence_command(command, confinement)
    out, err = stream_files("run")
    started = time.monotonic()
    task: asyncio.Task[None] | None = None
    try:
        try:
            proc = await spawn_in_session(
                command,
                cwd=cwd,
                env=env if env is not None else filtered_env(),
                stdout=out,
                stderr=err,
            )
        except (OSError, ValueError) as exc:
            raise HostExecutionError(f"failed to launch host command: {exc}") from exc
        # The spawned process holds its own descriptors; ours are done with once it has them.
        out.close()
        err.close()
        watcher = (
            None
            if on_progress is None
            else Tail(out.name, err.name, started=started, hook=on_progress)
        )
        task = None if watcher is None else asyncio.create_task(watcher.pump())
        timed_out = False
        try:
            _ = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except TimeoutError:
            await terminate_tree(proc)
            timed_out = True
        except BaseException:
            # Cancellation lands here — the run hit its inactivity/wall-clock bound, or the
            # operator pressed Stop. Unwinding without reaping would leave the command's
            # whole tree alive on the operator's real machine with no run left to stop it,
            # which is precisely what the process group exists to prevent. Not awaited: this
            # coroutine is already being torn down, and the group is signalled.
            kill_tree(proc)
            raise
        else:
            if kill_leftovers:
                _kill_leftovers(proc)
        if watcher is not None and task is not None:
            # Stopped rather than cancelled, so the last tick reads what the command printed
            # on its way out.
            watcher.stop()
            await task
        return SandboxResult(
            exit_code=TIMEOUT_EXIT if timed_out else (proc.returncode or 0),
            stdout=read_text(out.name),
            stderr=read_text(err.name),
            timed_out=timed_out,
        )
    finally:
        if task is not None:
            _ = task.cancel()
        out.close()
        err.close()
        unlink(out.name, err.name)
