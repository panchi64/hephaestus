"""The `shell` category: running commands in the agent's home.

Four tools: `run_command` (blocking), `start_command` / `check_command` / `stop_command`
(background), so a dev server or a long test run is a process the agent checks on rather
than a turn that blocks for ten minutes. This file is the contract the model sees, and the
one place that turns what the model *said* into where a command runs.

**Commands run in the platform VM, in the home** (``services/sandbox/home_shell.py``).
The thread's scratch is always there; a project is brought in when the working directory
is in it or the command names it, and what the command changed is brought back after. A
project the thread may only read is mounted read-only. The working directory persists per
thread (``services/sandbox/thread_shells.py``) and starts in the thread's own place: its
project, or its scratch.

**The executing tools take a `reach`.** The model states how far a command needs to go,
and this file holds it to that:

- `workspace` runs in the VM with no network at all;
- `network` runs in the VM with the conversation's egress credentials, reaching the
  domains the operator has allowed (``services/egress.py``);
- `host` runs on the operator's machine itself, unfenced, for a command that has to act
  on that machine — which is what the approver was shown. It starts in the host folder of
  the project the working directory is in, else in the host scratch directory.

The same declaration is what the permission layer rules on
(``services/permissions/judge.py``), so what a command was cleared for and where it runs
are one statement. A `workspace` declaration the command's own syntax contradicts (it
names a URL) was approved in the words it was read in, so it gets the network it plainly
needs rather than failing.

**Refused while the VM is down.** Without the VM there is nowhere to run a fenced
command, and running one on the operator's machine instead is not something this tool
will do.

**Output streams while the command runs.** `run_command` passes a progress hook and each
new piece of output goes out as `tool.progress`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from core.diagnostics import render_tool_error
from runs import ToolProgress
from services.home.paths import HomePathError, parse
from services.home.table import RunHome
from services.permissions import Reach, capability_of
from services.projects.worktree import WorktreeBusyError
from services.sandbox import SandboxSessionManager
from services.sandbox.host import host_scratch_dir
from services.sandbox.session import SandboxSession
from services.sandbox.shell_runner import BackgroundStatus, ProgressHook, ShellResult
from services.sandbox.thread_shells import ThreadShell, ThreadShells
from services.vm import DENIED_MARKER
from services.vm.project import ProjectCopies

from .deps import RunDeps
from .emit import RunEventEmitted
from .home import resolve_run_home
from .workspace import unavailable

#: Long enough for a real build or test suite; short enough that a hung command doesn't
#: hold the turn open indefinitely. The agent can pass its own timeout per call, and any
#: genuinely long-running thing belongs in `start_command`.
SHELL_TIMEOUT_S = 300.0

#: Effectively no cap: a pathological command's output is caught by the run's own
#: context-overflow stop, which says so out loud rather than silently dropping the middle.
SHELL_MAX_OUTPUT_CHARS = 2_000_000

#: The two tools that execute something new. `check_command` and `stop_command` act on a
#: process that was already approved into existence, so re-asking would be noise.
EXECUTING_TOOLS = frozenset({"run_command", "start_command"})

#: The same two, namespaced: the conditionally-gated names this category contributes to
#: the approval-scope vocabulary. A name absent from `app.state.gated_tools` could not be
#: granted for the conversation, so the operator would be asked on every command.
GATED_TOOLS = frozenset(f"shell_{name}" for name in EXECUTING_TOOLS)

#: The prefix `tools/toolsets.py` puts in front of these names; the capability extraction
#: is keyed on the namespaced name and this file only ever sees the bare one.
_NAMESPACE = "shell_"


def _no_vm(reason: str) -> str:
    return render_tool_error(
        f"shell commands are unavailable: the platform VM they run in is not running ({reason})",
        hints=[
            "tell the operator; until it is back, work by reading and editing files with "
            + "the files tools, and say plainly what you could not verify"
        ],
    )


#: How long the first command of a session waits for a VM that is still starting.
_VM_WAIT_S = 120.0


@dataclass(frozen=True, slots=True)
class _Boundary:
    """Where one command runs, as something a reader can be shown."""

    reach: Reach
    where: Literal["vm", "host"]
    #: Whether a VM command gets the conversation's egress credentials.
    network: bool
    #: Why it runs on the host. None for a VM command.
    reason: str | None

    @property
    def fenced(self) -> bool:
        return self.where == "vm"


_HOST_DECLARED = "the command declared `host`, which asks for the operator's machine itself"


def boundary(name: str, command: str, reach: Reach, home: RunHome, cwd: str) -> _Boundary:
    """Where this command runs, built from the declaration rather than from a judgement:
    the permission layer answers *who must approve*, and this answers *where it runs*.

    Only an explicit `host` declaration leaves the VM. A `workspace` declaration the
    command's syntax contradicts by reaching the network gets the network, because that is
    what the approver read."""
    if reach == "host":
        return _Boundary(reach=reach, where="host", network=False, reason=_HOST_DECLARED)
    network = reach == "network"
    if not network:
        capability = capability_of(
            f"{_NAMESPACE}{name}", {"command": command, "reach": reach}, root=home.fence(cwd)
        )
        network = capability.network
    return _Boundary(reach=reach, where="vm", network=network, reason=None)


def _vm_note(boundary: _Boundary, result: ShellResult) -> str | None:
    """A note on a request the VM's egress gateway refused, or None."""
    if not boundary.fenced:
        return None
    text = f"{result.stdout}\n{result.stderr}"
    if DENIED_MARKER in text:
        return (
            "The VM's egress gateway refused a host this conversation has not been allowed "
            "to reach. Ask for it with `code_request_egress`, then run the command again."
        )
    if "407" in text and "hephaestus-egress" in text:
        return (
            "This command declared `workspace`, which has no network. Declare `network` "
            "to reach the domains the operator has allowed."
        )
    if "Read-only file system" in text:
        return (
            "A project you may only read is mounted read-only. To change it, edit the file "
            "with the files tools, which ask the operator once and then let this thread "
            "write that project, and run the command again."
        )
    return None


def _progress_hook(ctx: RunContext[RunDeps]) -> ProgressHook | None:
    """Put this command's output on the operator's stream as it arrives, or None where
    there is no call to attribute a frame to. Each frame carries the *next* piece of
    output; the completed result is still the record a reload reads."""
    tool_call_id = ctx.tool_call_id
    if not tool_call_id:
        return None

    async def hook(chunk: str, elapsed_s: float) -> None:
        _ = await ctx.emit(
            RunEventEmitted(
                body=ToolProgress(tool_call_id=tool_call_id, elapsed_s=elapsed_s, partial=chunk)
            )
        )

    return hook


def _run_payload(
    result: ShellResult,
    boundary: _Boundary,
    timeout_s: float,
    *,
    cwd: str,
    notes: tuple[str, ...] = (),
) -> dict[str, object]:
    """One finished command, as the model and the operator's terminal both read it. The
    optional keys are absent rather than null when they say nothing."""
    payload: dict[str, object] = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "cwd": cwd,
        **_boundary_fields(boundary),
    }
    note = _vm_note(boundary, result)
    if note is not None:
        payload["fence_note"] = note
    if notes:
        payload["notes"] = list(notes)
    if result.timed_out:
        payload["error"] = (
            f"The command was still running after {timeout_s}s and was terminated. "
            "Everything it printed before that is above."
        )
    elif not result.ok:
        payload["error"] = f"The command exited with status {result.exit_code}."
    return payload


def _boundary_fields(boundary: _Boundary) -> dict[str, object]:
    """The declaration and the fence's verdict, which every executing tool carries."""
    fields: dict[str, object] = {"reach": boundary.reach, "fenced": boundary.fenced}
    if boundary.reason is not None:
        fields["unfenced_reason"] = boundary.reason
    return fields


def _background_payload(status: BackgroundStatus) -> dict[str, object]:
    """A background command as it stands; ``ok`` means nothing went wrong, so a process
    still running is fine and one that exited non-zero is not."""
    return {
        "ok": status.exit_code in (None, 0),
        "command_id": status.command_id,
        "status": status.status,
        "exit_code": status.exit_code,
        "stdout": status.stdout,
        "stderr": status.stderr,
    }


def _unknown_command(command_id: str) -> dict[str, object]:
    return {
        "ok": False,
        "command_id": command_id,
        "error": (
            f"No background command with ID {command_id!r} is running in this "
            "conversation. It may have already been stopped."
        ),
    }


def _host_root(home: RunHome, cwd: str) -> Path:
    """Where a host command starts: the host folder of the project the working directory
    is in, else the host scratch directory."""
    try:
        target = parse(cwd)
    except HomePathError:
        return host_scratch_dir()
    mount = home.mount(target.place)
    if mount is None or target.place.kind != "projects":
        return host_scratch_dir()
    return mount.root / target.rel if target.rel else mount.root


@dataclass(frozen=True, slots=True)
class _Ready:
    home: RunHome
    session: SandboxSession
    copies: ProjectCopies
    shell: ThreadShell


async def _ready(ctx: RunContext[RunDeps], name: str) -> _Ready | str:
    """The run's home, its scratch session, the VM's copies and the thread's shell, or why
    there are none. An executing call asks for approval once all of those are there."""
    copies = ctx.deps.caps.get_optional(ProjectCopies)
    shells = ctx.deps.caps.get_optional(ThreadShells)
    if copies is None or shells is None:
        return _no_vm("it is disabled")
    vm = copies.vm
    if not vm.available and not await vm.wait_ready(_VM_WAIT_S):
        return _no_vm(vm.failure or "it is still starting")
    try:
        home = await resolve_run_home(ctx.deps)
    except WorktreeBusyError as exc:
        return str(exc)
    sessions = ctx.deps.caps.get_optional(SandboxSessionManager)
    session = sessions.existing(ctx.deps.workspace_key) if sessions is not None else None
    if home is None or session is None:
        return unavailable(ctx.deps)
    approved = ctx.tool_call_approved or ctx.deps.delegated_approved
    if name in EXECUTING_TOOLS and not approved:
        raise ApprovalRequired()
    shell = shells.get(ctx.deps.workspace_key, start=home.default.path)
    return _Ready(home=home, session=session, copies=copies, shell=shell)


def shell_toolset() -> FunctionToolset[RunDeps]:
    """The `shell` category, built once at app assembly. Each thread's working directory
    and background commands live in the app's :class:`ThreadShells`."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def run_command(
        ctx: RunContext[RunDeps],
        command: str,
        reach: Reach = "workspace",
        timeout_seconds: float | None = None,
    ) -> dict[str, object] | str:
        """Execute a shell command in your home and return its output.

        It starts in the working directory the last command left (`cwd` in each result).
        `~/scratch` is yours; `~/projects/<name>` is each of the operator's projects.

        Args:
            command: The shell command to run.
            reach: How far this command needs to go. `workspace` runs it in your VM with no
                network — the right answer for builds, tests, git and anything reading or
                writing files. `network` additionally reaches the domains the operator has
                permitted, for installing dependencies. `host` runs it on the operator's
                machine itself, for a command that has to act on that machine.
            timeout_seconds: Maximum seconds to wait (default: 300).

        Returns:
            `ok`, `exit_code` (null if it timed out), `stdout`, `stderr`, `timed_out`,
            `duration_ms`, the `cwd` the next command starts in, the `reach` you declared,
            whether it ran `fenced` in the VM, and any `notes` from bringing its work back.
        """
        state = await _ready(ctx, "run_command")
        if isinstance(state, str):
            return state
        shell = state.shell
        cwd = shell.home.cwd
        bound = boundary("run_command", command, reach, state.home, cwd)
        timeout = timeout_seconds if timeout_seconds is not None else SHELL_TIMEOUT_S
        if not bound.fenced:
            result = await shell.host_at(_host_root(state.home, cwd)).run(
                command, timeout_seconds=timeout_seconds, on_progress=_progress_hook(ctx)
            )
            return _run_payload(result, bound, timeout, cwd=cwd)
        done = await shell.home.run(
            state.session,
            state.copies,
            command,
            mounts=state.home.mounts,
            network=bound.network,
            timeout_seconds=timeout_seconds,
            on_progress=_progress_hook(ctx),
        )
        return _run_payload(done.result, bound, timeout, cwd=done.cwd, notes=done.notes)

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def start_command(
        ctx: RunContext[RunDeps], command: str, reach: Reach = "workspace"
    ) -> dict[str, object] | str:
        """Start a long-running command in the background (e.g. a server or watcher).

        Callers MUST call `stop_command(command_id)` when done to terminate the
        process and clean up temporary output files.

        Args:
            command: The shell command to run in the background.
            reach: How far this command needs to go — the same three answers
                `run_command` takes, enforced the same way.

        Returns:
            `command_id` for later check/stop calls, the `command` itself, and the same
            `reach` / `fenced` pair every executing call carries.
        """
        state = await _ready(ctx, "start_command")
        if isinstance(state, str):
            return state
        shell = state.shell
        cwd = shell.home.cwd
        bound = boundary("start_command", command, reach, state.home, cwd)
        if bound.fenced:
            command_id = await shell.home.start(
                state.session,
                state.copies,
                command,
                mounts=state.home.mounts,
                network=bound.network,
            )
        else:
            command_id = await shell.host_at(_host_root(state.home, cwd)).start(command)
        return {"ok": True, "command_id": command_id, "command": command, **_boundary_fields(bound)}

    async def _find(
        ctx: RunContext[RunDeps], command_id: str, *, stop: bool
    ) -> dict[str, object] | str:
        state = await _ready(ctx, "stop_command" if stop else "check_command")
        if isinstance(state, str):
            return state
        shell = state.shell
        act = shell.home.stop if stop else shell.home.check
        status = await act(state.copies, command_id)
        for host in shell.hosts.values():
            if status is None:
                status = await (host.stop if stop else host.check)(command_id)
        return _unknown_command(command_id) if status is None else _background_payload(status)

    @toolset.tool
    async def check_command(
        ctx: RunContext[RunDeps], command_id: str
    ) -> dict[str, object] | str:
        """Check the status and recent output of a background command.

        Args:
            command_id: The ID returned by start_command.

        Returns:
            `status` (`running` or `finished`), `exit_code` once it has one, and
            everything it has printed so far in `stdout` / `stderr`.
        """
        return await _find(ctx, command_id, stop=False)

    @toolset.tool
    async def stop_command(ctx: RunContext[RunDeps], command_id: str) -> dict[str, object] | str:
        """Stop a background command and return its final output.

        Args:
            command_id: The ID returned by start_command.

        Returns:
            `status` of `stopped`, the final `exit_code` where it had one, and everything
            the command printed.
        """
        return await _find(ctx, command_id, stop=True)

    return toolset
