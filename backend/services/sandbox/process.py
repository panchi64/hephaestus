"""Running a command on the operator's real machine, and reaping the tree it starts.

Two paths land here: ``shell_run_on_host``, the approval-gated escape hatch, and the
host shell in a project folder. Both go through :func:`run_on_host`, which spawns, times out,
kills the process tree and handles cancellation in one place.

Nothing here decides what a command may reach: :func:`run_on_host` wraps a command with
:func:`services.sandbox.fence.fence_command` when handed an active confinement, and runs it
as given otherwise. This module guarantees that whatever was started can be stopped whole,
so a command that backgrounds a server does not leave it running once its run is gone.
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

from pydantic_ai_harness.shell import LLM_API_KEY_ENV_PATTERNS

from .base import HostExecutionError, SandboxResult
from .fence import HostConfinement, fence_command
from .gitenv import git_config_pins
from .limits import TIMEOUT_EXIT
from .shell_result import ProgressHook
from .streams import Tail, read_text, stream_files, unlink

#: How long a process group gets to shut itself down before it is killed outright.
_GRACE_S = 2.0


#: Process groups started by :func:`spawn_in_session` that may still be alive, each with the
#: leader that opened it. A backgrounded server outlives its reaped shell, so without this a
#: quit app left it running. Pruned on each spawn.
groups: dict[int, asyncio.subprocess.Process] = {}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: not ours to signal or track
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

    A group id is its leader's pid. While the group has any member the kernel reserves the
    number; once it empties the number can go to an unrelated session leader (the operator's
    own shell), and signalling it would kill that.

    Provably ours: the leader is unreaped (``returncode`` is ``None``), so it or its zombie
    holds the pid; or it is reaped, nothing holds the pid, and the group still has members.
    A reaped leader with a process at ``pgid`` means the number was reused and our group is
    gone. Both probes are signal-0 calls, identical on macOS and Linux.

    Residual window: between the leader being reaped and asyncio recording ``returncode``, a
    recycled pid reads as the leader's. It needs the group to empty and the pid to recycle
    within that interval.
    """
    if leader.returncode is not None and _pid_exists(pgid):
        return False
    return _group_alive(pgid)


async def stop_all_groups(grace_s: float = 1.0) -> None:
    """SIGTERM every process group this app started and still has running, give them
    ``grace_s`` to leave, then SIGKILL what remains. Called once, from app shutdown.

    Ownership is re-checked (:func:`group_is_ours`) on every poll, since a group that
    empties during the grace period frees its number for the SIGKILL pass to hit.
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
    """Kill the whole process group, not just the shell, so its children don't outlive it."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        proc.kill()
    except OSError:  # already reaped, or no such group
        pass


async def terminate_tree(proc: asyncio.subprocess.Process) -> None:
    """Ask the process group to stop, then make it, and wait for it either way.

    SIGTERM first lets a dev server close its socket, a compose stack stop its services and
    a test run clean up; SIGKILL is the fallback so a process ignoring SIGTERM can't hold a
    turn open.

    Not for teardown paths: a cancelled coroutine cannot await, so use :func:`kill_tree`.
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
        # Cancelled mid-grace: a process ignoring SIGTERM would outlive the run.
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

    Two rules live together so a caller building its own environment for one cannot lose
    the other (passing the git pins as an explicit ``env`` once skipped the credential
    filter on the host hatch).

    The operator's model credentials are removed. Not a boundary, since a same-user
    process can reach the parent's environment, but the fence denies the paths those keys
    are stored at and this keeps them out of the one place a command reads for free.

    The git settings a repository must not choose are pinned over it (``gitenv.py``),
    applied over the filtered environment so an operator's own git settings are continued.
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
    # The session leader's pid is the group id (see ``groups``).
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
    """Run ``command`` in the host shell, after approval. The process group is terminated
    on a wall-clock overrun and killed on cancellation. A timed-out command reports exit
    124 with whatever it printed.

    ``on_progress``, where given, is handed each new piece of output as the command runs,
    from a task of its own so a blocking hook cannot delay reaping.

    ``kill_leftovers`` kills what the command left running in its process group once the
    shell itself has exited, so a command that backgrounds a child does not outlive the
    call. A caller that keeps background processes on purpose passes ``False``.

    A launch failure raises :class:`HostExecutionError` with the spawn's own ``OSError``
    or ``ValueError`` as its cause.

    ``confinement`` is resolved by the caller (see
    :func:`services.sandbox.fence.resolve_confinement`) so the tool reports the same fence
    it asked for. ``None``, or an inactive one, runs the command unconfined.

    ``env`` defaults to :func:`filtered_env`: the fence denies the paths the operator's
    model keys live at but not the environment, which would otherwise hand them over.
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
            # Cancellation (run bound hit, or Stop): unwinding unreaped would leave the tree
            # alive on the operator's machine. Not awaited, as this coroutine is being
            # torn down.
            kill_tree(proc)
            raise
        else:
            if kill_leftovers:
                _kill_leftovers(proc)
        if watcher is not None and task is not None:
            # Stopped, not cancelled, so the last tick reads the command's final output.
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
