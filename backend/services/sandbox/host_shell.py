"""The project shell on the operator's machine: one persistent working directory, and the
background processes started from it.

**The behaviour is the harness `Shell`'s on purpose.** The model has been trained on those
four tools — their names, their arguments, the shape of what they return — and changing any
of it would cost more than it gains. The shape is copied and the spawn is ours.
``tools/shell.py`` keeps the model-facing contract; what happens to a process is in
:mod:`services.sandbox.process`.

The working directory persists between calls because a person's shell does, and because
the permission judge reads relative paths against it (``services/permissions/judge.py``).
It is captured out of band, into a temp file whose random name the agent's own command
cannot address: parsing a sentinel out of stdout would let any command that prints the
sentinel redirect where the next one runs.

A foreground run goes through :func:`~services.sandbox.process.run_on_host`, so it times
out, reaps its process tree and reports progress the way the approval-gated host command
does. Commands run inside the OS fence (:func:`~services.sandbox.fence.shell_confinement`)
when it is active, and as plain subprocesses when it is disabled or unavailable.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import os
import shlex
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar, Concatenate, Literal

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai_harness._output import truncate_tail

from core.config import get_settings

from .base import HostExecutionError
from .command_check import check_command
from .fence import fence_command, shell_confinement
from .process import filtered_env, run_on_host, spawn_in_session, terminate_tree
from .shell_result import BackgroundStatus, ProgressHook, ShellResult
from .streams import read_text, stream_files, unlink

# Spawning fails with `FileNotFoundError`/`NotADirectoryError` when the working directory
# is gone — which the model's own last command may well have deleted, and which it can fix.
# Every other errno (EMFILE, ENOMEM) is the host's, and must keep aborting the run rather
# than sending the model into a retry loop it cannot win.
_RECOVERABLE_ERRNOS: dict[int | None, str] = {
    errno.ENOENT: "The working directory no longer exists.",
    errno.ENOTDIR: "The working directory is no longer a directory.",
}


def _recoverable[R, **P](
    fn: Callable[Concatenate[HostShell, P], Awaitable[R]],
) -> Callable[Concatenate[HostShell, P], Awaitable[R]]:
    """Turn what the model can correct into `ModelRetry`, and leave the rest alone.

    Pydantic AI feeds only `ModelRetry` back as a retry prompt; anything else aborts the
    whole run. A refused command and a working directory an earlier command destroyed are
    both the agent's to act on, so they go back to it rather than ending the turn."""

    @functools.wraps(fn)
    async def wrapper(self: HostShell, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(self, *args, **kwargs)
        except PermissionError as exc:
            raise ModelRetry(str(exc)) from exc
        except OSError as exc:
            reason = _RECOVERABLE_ERRNOS.get(exc.errno)
            if reason is None:
                raise
            # `str(exc)` embeds the absolute host path; the reason alone doesn't.
            raise ModelRetry(reason) from exc

    return wrapper


class _Background:
    """A process `start` left running, and the files its output accumulates in."""

    __slots__: ClassVar[tuple[str, ...]] = ("proc", "out_path", "err_path", "finished", "exit_code")

    def __init__(self, proc: asyncio.subprocess.Process, out_path: str, err_path: str) -> None:
        self.proc: asyncio.subprocess.Process = proc
        self.out_path: str = out_path
        self.err_path: str = err_path
        self.finished: bool = False
        self.exit_code: int | None = None


class HostShell:
    """One workspace's shell on the operator's machine: the drifting working directory and
    the background processes started in it. Built per worktree and shared by the runs
    working there, so a `cd` and a dev server both outlive the turn that made them."""

    def __init__(
        self,
        root: Path,
        *,
        default_timeout: float,
        max_output_chars: int,
    ) -> None:
        self._root: Path = root
        self._cwd: Path = root
        self._default_timeout: float = default_timeout
        self._max_output_chars: int = max_output_chars
        self._background: dict[str, _Background] = {}

    @property
    def cwd(self) -> Path:
        """The directory the next command will start in — the worktree root, or wherever
        inside it an earlier `cd` left the session."""
        return self._cwd

    @property
    def busy(self) -> bool:
        """Whether a background process it started may still be running."""
        return bool(self._background)

    @_recoverable
    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float | None = None,
        on_progress: ProgressHook | None = None,
    ) -> ShellResult:
        """Run ``command`` to completion and return what it did.

        ``on_progress``, where given, is handed each new piece of output as the command
        runs. A command killed on its timeout carries no exit code and keeps its output.
        """
        check_command(command)
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        wrapped, cwd_file = self._with_cwd_capture(command)
        started = time.monotonic()
        try:
            try:
                result = await run_on_host(
                    wrapped,
                    timeout_s=timeout,
                    confinement=await shell_confinement(get_settings()),
                    cwd=self._cwd,
                    on_progress=on_progress,
                    kill_leftovers=False,
                )
            except HostExecutionError as exc:
                launch_error = exc.__cause__
                if isinstance(launch_error, OSError):
                    raise launch_error from None
                raise
            duration_ms = round((time.monotonic() - started) * 1000)
            exit_code = None if result.timed_out else result.exit_code
            if exit_code == 0:
                self._apply_captured_cwd(cwd_file)
            return ShellResult(
                exit_code=exit_code,
                stdout=self._capped(result.stdout),
                stderr=self._capped(result.stderr),
                timed_out=result.timed_out,
                duration_ms=duration_ms,
            )
        finally:
            unlink(str(cwd_file))

    @_recoverable
    async def start(self, command: str) -> str:
        """Start ``command`` in the background, and return its id."""
        check_command(command)
        command_id = uuid.uuid4().hex[:12]
        out, err = stream_files(command_id)
        try:
            fenced = await fence_command(command, await shell_confinement(get_settings()))
            proc = await spawn_in_session(
                fenced, cwd=self._cwd, env=filtered_env(), stdout=out, stderr=err
            )
        except BaseException:
            unlink(out.name, err.name)
            raise
        finally:
            out.close()
            err.close()
        self._background[command_id] = _Background(proc, out.name, err.name)
        return command_id

    async def check(self, command_id: str) -> BackgroundStatus | None:
        """How far a background command has got, and everything it has printed.

        ``None`` for an id this shell does not know — which is a fact about the call
        rather than about a process, so the sentence the model reads about it is composed
        where the rest of this tool's prose is (``tools/shell.py``).
        """
        bg = self._background.get(command_id)
        if bg is None:
            return None
        if not bg.finished and bg.proc.returncode is not None:
            bg.exit_code = bg.proc.returncode
            bg.finished = True
        return self._status(command_id, bg, "finished" if bg.finished else "running", bg.exit_code)

    async def stop(self, command_id: str) -> BackgroundStatus | None:
        """Stop a background command's whole group and hand back its final output.

        Asked to stop before it is made to: the agent is told to call this on everything
        it starts, so a server or a compose stack reaches it in the normal course of work
        and deserves the chance to shut itself down."""
        bg = self._background.pop(command_id, None)
        if bg is None:
            return None
        if not bg.finished:
            await terminate_tree(bg.proc)
            bg.exit_code = bg.proc.returncode
            bg.finished = True
        status = self._status(command_id, bg, "stopped", bg.exit_code)
        unlink(bg.out_path, bg.err_path)
        return status

    def _status(
        self,
        command_id: str,
        bg: _Background,
        status: Literal["running", "finished", "stopped"],
        exit_code: int | None,
    ) -> BackgroundStatus:
        """One background command's streams read off its files, capped like any other."""
        return BackgroundStatus(
            command_id=command_id,
            status=status,
            exit_code=exit_code,
            stdout=self._capped(read_text(bg.out_path)),
            stderr=self._capped(read_text(bg.err_path)),
        )

    async def shutdown(self) -> None:
        """Stop everything still running here, for a shell whose workspace is going away.

        The agent is told to stop what it starts, and one that ran out of time or was
        cancelled never got there. A shell that outlives its turn can wait for that call;
        a delegated run's cannot — its ids lived only in a transcript that has ended, and
        its processes were started in sessions of their own, so nothing else reaps them.
        """
        for command_id in list(self._background):
            _ = await self.stop(command_id)

    def _with_cwd_capture(self, command: str) -> tuple[str, Path]:
        """``command`` with its final directory recorded out of band (see the module
        docstring)."""
        fd, name = tempfile.mkstemp(prefix="hephaestus_cwd_")
        os.close(fd)
        wrapped = f"{command}\n__heph_ec=$?\npwd > {shlex.quote(name)}\nexit $__heph_ec"
        return wrapped, Path(name)

    def _apply_captured_cwd(self, cwd_file: Path) -> None:
        """Move the tracked directory to wherever the command ended up — **while that is
        still inside the worktree**.

        The containment is the load-bearing half. The permission layer measures every
        relative path a later command writes against the worktree root
        (``services/permissions/shell_ast.py``), so a `cd` that moved the tracked directory
        out of the worktree would leave every later containment claim measured against the
        wrong place: `cat secrets.txt` would clear as contained while reading some other
        directory's file.

        Both sides are resolved through symlinks, so a link inside the worktree that points
        elsewhere does not count as inside, and `/tmp/wt` and `/private/tmp/wt` (one
        directory on macOS) compare equal.

        The whole read is guarded, not only the open: the command it belongs to already
        succeeded, so a capture that is not UTF-8 or a path the OS refuses to stat is
        bookkeeping this can drop, not a tool failure to report."""
        try:
            recorded = cwd_file.read_text(encoding="utf-8").strip()
            if not recorded:
                return
            landed = self._contained(Path(recorded))
            if landed is not None and landed.is_dir():
                self._cwd = landed
        except OSError, ValueError:
            return

    def _contained(self, candidate: Path) -> Path | None:
        """``candidate`` resolved through symlinks, when that lands in the worktree root or
        under it; ``None`` otherwise, or when it cannot be resolved."""
        try:
            resolved = candidate.resolve(strict=True)
            if resolved.is_relative_to(self._root.resolve()):
                return resolved
        except OSError, RuntimeError:
            pass
        return None

    def _capped(self, text: str) -> str:
        """Trimmed from the front, per stream — the end of a log is what says how it went.

        Per stream rather than over the pair, so a chatty stdout cannot push the stderr
        that explains the failure out entirely.
        """
        return truncate_tail(text, self._max_output_chars)
