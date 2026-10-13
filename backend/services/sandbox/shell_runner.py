"""The mechanics behind the project shell: one persistent working directory, the
background processes started from it, on the operator's machine.

**The behaviour is the harness `Shell`'s on purpose.** The model has been trained on those
four tools — their names, their arguments, the shape of what they return — and changing any
of it would cost more than it gains. The shape is copied and the spawn is ours.
``tools/shell.py`` keeps the model-facing contract; what happens to a process is here.

The working directory persists between calls because a person's shell does, and because
the permission judge reads relative paths against it (``services/permissions/judge.py``).
It is captured out of band, into a temp file whose random name the agent's own command
cannot address: parsing a sentinel out of stdout would let any command that prints the
sentinel redirect where the next one runs.

**What a command hands back is structure, not a rendered string.** A run returns
:class:`ShellResult` — exit code, the two streams apart, whether it was killed on a
timeout, and how long it took — and a background command returns
:class:`BackgroundStatus`. It used to be one labelled string with `[stdout]` / `[stderr]`
/ `[exit code: N]` markers in it, which meant every reader downstream had to take the
markers back apart to get at a field: the operator's terminal card parsed them in the
browser, and a timed-out command threw its output away because there was no slot to put
it in. The prose a *model* reads is composed one layer up (``tools/shell.py``), from these
fields; nothing here decides how the result is worded.

**Output can be watched while it accumulates.** :meth:`HostShell.run` takes an optional
``on_progress`` and calls it with whatever the command has newly printed, every
:data:`_PROGRESS_INTERVAL_S`. It is a hook and not an event: this module knows nothing
about runs or streams, and what a caller does with a chunk is theirs
(``tools/shell.py`` puts it on the operator's stream). Progress is best-effort in the
strong sense — a hook that raises, or a file that cannot be read, must never fail the
command that was running fine.
"""

from __future__ import annotations

import asyncio
import codecs
import errno
import functools
import logging
import os
import re
import shlex
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Concatenate, Literal

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai_harness._output import truncate_tail
from pydantic_ai_harness.shell._capability import _DEFAULT_DENIED_COMMANDS

from .process import filtered_env, kill_tree, spawn_confined, terminate_tree

logger = logging.getLogger(__name__)

#: How often a running command's new output is handed to ``on_progress``. Half a second
#: is under the threshold at which a build stops reading as live, and well above the rate
#: at which a chatty command would turn one tool call into thousands of stream frames.
_PROGRESS_INTERVAL_S = 0.5

#: How much of one command's output is *streamed* while it runs. The whole of it still
#: comes back on :class:`ShellResult`, which is the record; this bounds only what the live
#: stream (and the replay buffer behind it, which is memory) carries for a command that
#: prints megabytes. Past it the streaming stops and the operator reads the rest when the
#: command lands.
_PROGRESS_MAX_CHARS = 200_000

#: Destructive programs (`rm`, `dd`, `mkfs`, `shutdown`, …) refused by name. Taken from the
#: harness rather than restated: it is a guardrail against a slip, not a boundary, and two
#: copies of a guardrail is one copy that stops being updated when the other grows an entry.
_DENIED_COMMANDS: frozenset[str] = frozenset(_DEFAULT_DENIED_COMMANDS)

#: Programs that want a terminal. Here they would not fail — they would hang until the
#: timeout, spending the turn on nothing.
_INTERACTIVE = re.compile(
    r"^(vi|vim|nano|emacs|less|more|top|htop|man)\b|^sudo\s|^(passwd|ssh|telnet|ftp)\b"
)

# Spawning fails with `FileNotFoundError`/`NotADirectoryError` when the working directory
# is gone — which the model's own last command may well have deleted, and which it can fix.
# Every other errno (EMFILE, ENOMEM) is the host's, and must keep aborting the run rather
# than sending the model into a retry loop it cannot win.
_RECOVERABLE_ERRNOS: dict[int | None, str] = {
    errno.ENOENT: "The working directory no longer exists.",
    errno.ENOTDIR: "The working directory is no longer a directory.",
}


#: Called with everything a running command has newly printed and how long it has been
#: running. One string per tick, stdout ahead of stderr — the two are separate files and
#: nothing records how they interleaved, which was equally true of the labelled string
#: this replaced. The finished :class:`ShellResult` is where they are apart.
type ProgressHook = Callable[[str, float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ShellResult:
    """One finished command, as fields.

    ``exit_code`` is **None only when the command was killed on its timeout** — there was
    no status to collect, and a zero there would read as success. The streams are whole
    and unlabelled, ``duration_ms`` is wall clock measured around the spawn, and a command
    that printed nothing carries two empty strings rather than a sentence saying so.

    A timed-out command keeps what it managed to print. It used to be answered with the
    timeout line alone, which threw away the very output that says where it hung.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        """Whether the command itself succeeded — ran to completion, and exited zero."""
        return not self.timed_out and self.exit_code == 0


@dataclass(frozen=True, slots=True)
class BackgroundStatus:
    """A background command as it stands: how far it has got, and everything it printed.

    ``exit_code`` is None while it is still running, and after a stop that had to
    terminate a process that had not chosen its own status.
    """

    command_id: str
    status: Literal["running", "finished", "stopped"]
    exit_code: int | None
    stdout: str
    stderr: str


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

    __slots__ = ("proc", "out_path", "err_path", "finished", "exit_code")

    def __init__(self, proc: asyncio.subprocess.Process, out_path: str, err_path: str) -> None:
        self.proc = proc
        self.out_path = out_path
        self.err_path = err_path
        self.finished = False
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
        self._root = root
        self._cwd = root
        self._default_timeout = default_timeout
        self._max_output_chars = max_output_chars
        self._background: dict[str, _Background] = {}

    @property
    def cwd(self) -> Path:
        """The directory the next command will start in — the worktree root, or wherever
        inside it an earlier `cd` left the session."""
        return self._cwd

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
        runs (see the module docstring). It is watched from a task of its own so a hook
        that blocks cannot delay the process being reaped, and it is torn down on every
        way out of this method — including the cancellation one, where there is no time
        left to drain anything and the partial output is about to stop mattering.
        """
        check_command(command)
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        wrapped, cwd_file = self._with_cwd_capture(command)
        out, err = _stream_files("run")
        started = time.monotonic()
        try:
            proc = await self._spawn(wrapped, out, err)
            out.close()
            err.close()
            watcher = (
                None
                if on_progress is None
                else _Tail(out.name, err.name, started=started, hook=on_progress)
            )
            task = None if watcher is None else asyncio.create_task(watcher.pump())
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                timed_out = False
            except TimeoutError:
                await terminate_tree(proc)
                timed_out = True
            except BaseException:
                # Cancellation lands here — the run hit its bound, or the operator pressed
                # Stop. Unwinding without reaping would leave a build, a test run or a
                # server the command started alive on the operator's machine, writing into
                # two files the `finally` below is about to unlink, with no run left to
                # stop it. Not awaited: this coroutine is already being torn down.
                kill_tree(proc)
                if task is not None:
                    task.cancel()
                raise
            if watcher is not None and task is not None:
                # Stopped rather than cancelled, so the last tick reads what the command
                # printed on its way out: the process is gone, nothing more is coming, and
                # a pump that returns on its own needs no exception to unwind.
                watcher.stop()
                await task
            duration_ms = round((time.monotonic() - started) * 1000)
            exit_code = None if timed_out else (proc.returncode or 0)
            if exit_code == 0:
                self._apply_captured_cwd(cwd_file)
            return ShellResult(
                exit_code=exit_code,
                stdout=self._capped(_read(out.name)),
                stderr=self._capped(_read(err.name)),
                timed_out=timed_out,
                duration_ms=duration_ms,
            )
        finally:
            # The spawned process holds its own descriptors; ours are done with once it
            # has them, and a leaked pair per command would outlast the conversation.
            out.close()
            err.close()
            _unlink(out.name, err.name, str(cwd_file))

    @_recoverable
    async def start(self, command: str) -> str:
        """Start ``command`` in the background, and return its id."""
        check_command(command)
        command_id = uuid.uuid4().hex[:12]
        out, err = _stream_files(command_id)
        try:
            proc = await self._spawn(command, out, err)
        except BaseException:
            _unlink(out.name, err.name)
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
        _unlink(bg.out_path, bg.err_path)
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
            stdout=self._capped(_read(bg.out_path)),
            stderr=self._capped(_read(bg.err_path)),
        )

    async def shutdown(self) -> None:
        """Stop everything still running here, for a shell whose workspace is going away.

        The agent is told to stop what it starts, and one that ran out of time or was
        cancelled never got there. A shell that outlives its turn can wait for that call;
        a delegated run's cannot — its ids lived only in a transcript that has ended, and
        its processes were started in sessions of their own, so nothing else reaps them.
        """
        for command_id in list(self._background):
            await self.stop(command_id)

    async def _spawn(
        self, command: str, out: IO[bytes], err: IO[bytes]
    ) -> asyncio.subprocess.Process:
        """Start ``command`` in the tracked directory with a filtered env."""
        return await spawn_confined(
            command, cwd=self._cwd, env=filtered_env(), stdout=out, stderr=err
        )

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

        Both spellings of the root are compared, because the two ends disagree about it on
        macOS: the shell starts at the path it was given and `pwd` reports the one the
        kernel resolved, so `/tmp/wt` and `/private/tmp/wt` are one directory under two
        names and comparing against a single spelling would stop tracking `cd` entirely.

        The whole read is guarded, not only the open: the command it belongs to already
        succeeded, so a capture that is not UTF-8 or a path the OS refuses to stat is
        bookkeeping this can drop, not a tool failure to report."""
        try:
            recorded = cwd_file.read_text(encoding="utf-8").strip()
            if not recorded:
                return
            landed = Path(recorded)
            if landed.is_dir() and self._contains(landed):
                self._cwd = landed
        except OSError, ValueError:
            return

    def _contains(self, candidate: Path) -> bool:
        """Whether ``candidate`` is the worktree root or something under it."""
        for root in (self._root, self._root.resolve()):
            if candidate == root or root in candidate.parents:
                return True
        return False

    def _capped(self, text: str) -> str:
        """Trimmed from the front, per stream — the end of a log is what says how it went.

        Per stream rather than over the pair: they are two fields now, and a shared budget
        would let a chatty stdout push the stderr that explains the failure out entirely.
        """
        return truncate_tail(text, self._max_output_chars)


def check_command(command: str) -> None:
    """Refuse what this shell will not run at all, before anything is spawned. Best-effort,
    and not the boundary: a determined agent walks around a name check with an interpreter.
    The boundary is the permission layer in front of it."""
    if "\x00" in command:
        raise ModelRetry("The command contains a NUL byte, which cannot be passed to a process.")
    try:
        # `os.fsencode`, not `str.encode`: the spawn encodes with the filesystem encoding
        # and `surrogateescape`, so plain UTF-8 here would reject commands the OS runs.
        os.fsencode(command)
    except UnicodeEncodeError as exc:
        raise ModelRetry(
            "The command contains characters that cannot be encoded for the operating system."
        ) from exc
    if _INTERACTIVE.match(command.strip()):
        raise PermissionError(f"Interactive commands are not allowed. Command: {command!r}")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return
    if tokens and tokens[0] in _DENIED_COMMANDS:
        raise PermissionError(f"Command {tokens[0]!r} is denied.")


def _stream_files(prefix: str) -> tuple[IO[bytes], IO[bytes]]:
    """Two temp files for one command's streams. Files rather than pipes, in the foreground
    too: a pipe has to be drained while the process runs or it fills and deadlocks, and the
    same buffers then serve `check` without a second mechanism for it."""
    return (
        tempfile.NamedTemporaryFile(mode="w+b", prefix=f"hephaestus_{prefix}_out_", delete=False),
        tempfile.NamedTemporaryFile(mode="w+b", prefix=f"hephaestus_{prefix}_err_", delete=False),
    )


class _StreamTail:
    """One output file, read forward from wherever the last read stopped.

    Decoding is **incremental** rather than a decode per chunk: a read lands wherever the
    command happened to have written to, which is regularly the middle of a multi-byte
    character, and decoding each chunk on its own would turn every one of those into two
    replacement characters in the operator's terminal. The decoder holds the remainder
    until the rest of it arrives.
    """

    __slots__ = ("_path", "_offset", "_decoder")

    def __init__(self, path: str) -> None:
        self._path = path
        self._offset = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def read(self) -> str:
        """Whatever has been written since the last call, or "" — including on an error.

        A file that cannot be read is a command whose output the operator will still see
        in full when it finishes; it is never a reason to interrupt one that is running.
        """
        try:
            with open(self._path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return ""
        return self._decoder.decode(chunk) if chunk else ""


class _Tail:
    """The watcher behind ``on_progress``: both streams, polled while the command runs.

    It stops on a signal rather than on a cancellation, so the caller gets one final read
    after the process is gone — the output of a command's last half-second is exactly the
    part that says why it ended.

    **Nothing here may end a command.** The hook belongs to a caller and the files belong
    to a process; a failure in either is logged and the loop carries on, because a command
    that ran perfectly well must not be reported as failed by the thing that was only
    watching it.
    """

    __slots__ = ("_out", "_err", "_started", "_hook", "_stop", "_streamed")

    def __init__(self, out_path: str, err_path: str, *, started: float, hook: ProgressHook):
        self._out = _StreamTail(out_path)
        self._err = _StreamTail(err_path)
        self._started = started
        self._hook = hook
        self._stop = asyncio.Event()
        self._streamed = 0

    def stop(self) -> None:
        """Ask the pump to take one last read and return."""
        self._stop.set()

    async def pump(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), _PROGRESS_INTERVAL_S)
            except TimeoutError:
                await self._tick()
                continue
            await self._tick()
            return

    async def _tick(self) -> None:
        remaining = _PROGRESS_MAX_CHARS - self._streamed
        if remaining <= 0:
            return
        try:
            chunk = self._out.read() + self._err.read()
            if not chunk:
                return
            # The budget bounds the *tick* as well as the total: a command that printed a
            # megabyte between two reads would otherwise put all of it on the stream in
            # one frame, having never exceeded the budget on any earlier one.
            self._streamed += len(chunk)
            await self._hook(chunk[:remaining], time.monotonic() - self._started)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — watching a command must not be able to fail it
            logger.debug("shell progress hook failed", exc_info=True)


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _unlink(*paths: str) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
