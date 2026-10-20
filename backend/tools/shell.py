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

- `offline` runs in the VM with no network at all;
- `network` runs in the VM with the conversation's egress credentials, reaching the
  domains the operator has allowed (``services/egress.py``);
- `host` runs on the operator's machine itself, unfenced, for a command that has to act
  on that machine — which is what the approver was shown. It starts in the host folder of
  the project the working directory is in, else in the host scratch directory. What it
  changes there is not recorded for revert (``services/home/changes.py``): it writes the
  folder directly rather than coming back through the VM.

The same declaration is what the permission layer rules on
(``services/permissions/judge.py``), so what a command was cleared for and where it runs
are one statement. An `offline` declaration the command contradicts (it names a URL, or
installs or clones) was approved in the words it was read in, so it gets the network it
plainly needs rather than failing.

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
from services.home.paths import HomePathError, Place, parse
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

from .briefing import with_brief_payload
from .deps import RunDeps
from .emit import RunEventEmitted
from .home import change_log, resolve_run_home
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
            + "the `files_*` tools, and say plainly what you could not verify"
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

    Only an explicit `host` declaration leaves the VM. An `offline` declaration the
    command contradicts, by naming an address or by installing or cloning, gets the
    network, because that is what the approver read."""
    if reach == "host":
        return _Boundary(reach=reach, where="host", network=False, reason=_HOST_DECLARED)
    network = reach == "network"
    if not network:
        capability = capability_of(
            f"{_NAMESPACE}{name}", {"command": command, "reach": reach}, root=home.fence(cwd)
        )
        network = capability.network or capability.fetches
    return _Boundary(reach=reach, where="vm", network=network, reason=None)


#: What a command run without the proxy prints when it tries to reach a host: the guest
#: resolver fails before any connection is made, so the gateway never sees the request.
_NO_NETWORK_SIGNS = (
    "Temporary failure in name resolution",
    "Name or service not known",
    "Could not resolve host",
    "Network is unreachable",
    "No address associated with hostname",
    "failed to lookup address",
    "EAI_AGAIN",
    "ENOTFOUND",
)

_OFFLINE_NOTE = (
    "This command ran with reach `offline`, which has no network. The VM does have "
    "network: run the same command again with reach `network` to reach the domains the "
    "operator has allowed. Do not move it to `host`; installs and downloads belong in the VM."
)


def fence_note(boundary: _Boundary, result: ShellResult) -> str | None:
    """What the model should do about a refusal by the VM's fence, or None."""
    if not boundary.fenced:
        return None
    text = f"{result.stdout}\n{result.stderr}"
    if DENIED_MARKER in text:
        return (
            "The VM's egress gateway refused a host this conversation has not been allowed "
            "to reach. Ask for it with `code_request_egress`, then run the command again."
        )
    if "407" in text and "hephaestus-egress" in text:
        return _OFFLINE_NOTE
    if not boundary.network and any(sign in text for sign in _NO_NETWORK_SIGNS):
        return _OFFLINE_NOTE
    if "Read-only file system" in text:
        return (
            "A project you may only read is mounted read-only. To change it, edit the file "
            "with the `files_*` tools, which ask the operator once and then let this thread "
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
    note = fence_note(boundary, result)
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
    """Where a host command starts: the host folder of the project or worktree the
    working directory is in, else the host scratch directory."""
    try:
        target = parse(cwd)
    except HomePathError:
        return host_scratch_dir()
    mount = home.mount(target.place)
    if mount is None or target.place.kind not in ("projects", "worktrees"):
        return host_scratch_dir()
    return mount.root / target.rel if target.rel else mount.root


def _touched(state: _Ready, command: str, bound: _Boundary, cwd: str) -> list[Place]:
    """The projects and worktrees a command works in: the ones the VM binds for it, or the
    one a host command starts in."""
    if bound.fenced:
        reached = state.shell.home.reached(command, state.home.mounts, network=bound.network)
        return [mount.place for mount in reached]
    try:
        return [parse(cwd).place]
    except HomePathError:
        return []


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
    session = sessions.existing(ctx.deps.scratch_key) if sessions is not None else None
    if home is None or session is None:
        return unavailable(ctx.deps)
    approved = ctx.tool_call_approved or ctx.deps.delegated_approved
    if name in EXECUTING_TOOLS and not approved:
        raise ApprovalRequired()
    shell = shells.get(ctx.deps.workspace_key, start=home.default.path)
    shell.home.stand_within({m.place for m in home.mounts}, home.default.path)
    return _Ready(home=home, session=session, copies=copies, shell=shell)


def shell_toolset() -> FunctionToolset[RunDeps]:
    """The `shell` category, built once at app assembly. Each thread's working directory
    and background commands live in the app's :class:`ThreadShells`."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def run_command(
        ctx: RunContext[RunDeps],
        command: str,
        reach: Reach = "offline",
        timeout_seconds: float | None = None,
    ) -> dict[str, object] | str:
        """Runs a shell command in your home and waits for it to finish. Use it for builds,
        tests, git, package installs and anything else a terminal does. Read, search and
        edit files with the `files_*` tools rather than `cat`, `grep` or `sed`, because
        their edits are checked against the file's current contents.

        Your home holds `~/scratch`, which is yours, and `~/projects/<name>` for each of
        the operator's projects. A command starts where the last one left off, so `cd`
        carries over between calls. Output streams to the operator while it runs.

        The call blocks until the command exits or its timeout passes, 300 seconds by
        default. Start a server, a watcher or anything that runs until stopped with
        `shell_start_command` instead, because here it would hold the turn until the
        timeout kills it.

        Returns `ok`, `exit_code` (null after a timeout), `stdout`, `stderr`, `timed_out`,
        `duration_ms`, the `cwd` the next command starts in, the `reach` you declared and
        whether it ran `fenced` in the VM. A `fence_note` explains a refusal by the fence,
        such as a blocked host or a read-only project; act on it rather than retrying the
        same command. `notes` reports what happened when your changes were copied back.

        Args:
            command: The command, as you would type it in bash.
            reach: How far the command needs to go. `offline` runs it in your VM with
                no network: builds, tests, git and anything that reads or writes files.
                `network` runs it in your VM with the domains the operator has allowed,
                for installs, clones and downloads; a package install or clone gets it
                even when declared `offline`. `host` runs it unfenced on the operator's
                own machine, starting in the project's folder there; use it only when the
                command has to act on that machine, never to install packages, and know
                that its changes cannot be reverted.
            timeout_seconds: Seconds to wait before the command is killed. Default 300.
        """
        state = await _ready(ctx, "run_command")
        if isinstance(state, str):
            return state
        shell = state.shell
        cwd = shell.home.cwd
        bound = boundary("run_command", command, reach, state.home, cwd)
        touched = _touched(state, command, bound, cwd)
        timeout = timeout_seconds if timeout_seconds is not None else SHELL_TIMEOUT_S
        if not bound.fenced:
            result = await shell.host_at(_host_root(state.home, cwd)).run(
                command, timeout_seconds=timeout_seconds, on_progress=_progress_hook(ctx)
            )
            payload = _run_payload(result, bound, timeout, cwd=cwd)
        else:
            done = await shell.home.run(
                state.session,
                state.copies,
                command,
                mounts=state.home.mounts,
                network=bound.network,
                timeout_seconds=timeout_seconds,
                on_progress=_progress_hook(ctx),
                changes=change_log(ctx.deps),
            )
            payload = _run_payload(done.result, bound, timeout, cwd=done.cwd, notes=done.notes)
        return await with_brief_payload(ctx, state.home, touched, payload)

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def start_command(
        ctx: RunContext[RunDeps], command: str, reach: Reach = "offline"
    ) -> dict[str, object] | str:
        """Starts a command in the background and returns at once. Use it for a dev
        server, a watcher, or a build or test run long enough that you want to keep
        working while it goes; use `shell_run_command` for anything that finishes on its
        own within a few minutes.

        Check on it with `shell_check_command`, and stop it with `shell_stop_command`
        once you are done with it, because it keeps running, and keeps its output files,
        until it is stopped. To show the operator a web server you started for them, use
        `view_show` with `serve` instead, which runs it where they can open it.

        Returns the `command_id` that the other two tools take, the `command`, and the
        `reach` and `fenced` pair every executing call carries.

        Args:
            command: The command, as you would type it in bash.
            reach: How far the command needs to go: `offline`, `network` or `host`,
                meaning what they mean for `shell_run_command`.
        """
        state = await _ready(ctx, "start_command")
        if isinstance(state, str):
            return state
        shell = state.shell
        cwd = shell.home.cwd
        bound = boundary("start_command", command, reach, state.home, cwd)
        touched = _touched(state, command, bound, cwd)
        if bound.fenced:
            command_id = await shell.home.start(
                state.session,
                state.copies,
                command,
                mounts=state.home.mounts,
                network=bound.network,
                changes=change_log(ctx.deps),
            )
        else:
            command_id = await shell.host_at(_host_root(state.home, cwd)).start(command)
        payload: dict[str, object] = {
            "ok": True,
            "command_id": command_id,
            "command": command,
            **_boundary_fields(bound),
        }
        return await with_brief_payload(ctx, state.home, touched, payload)

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
    async def check_command(ctx: RunContext[RunDeps], command_id: str) -> dict[str, object] | str:
        """Returns where a background command stands: `status` (`running` or `finished`),
        `exit_code` once it has one, and everything it has printed so far in `stdout` and
        `stderr`.

        Use it to wait for a server to come up or a long run to finish. Between checks,
        get on with other work rather than calling this again straight away.

        Args:
            command_id: The `command_id` that `shell_start_command` returned.
        """
        return await _find(ctx, command_id, stop=False)

    @toolset.tool
    async def stop_command(ctx: RunContext[RunDeps], command_id: str) -> dict[str, object] | str:
        """Stops a background command and returns everything it printed, with its final
        `exit_code` where it had one.

        Stop each command you started once you no longer need it, including before you
        end a task, so nothing keeps running unnoticed.

        Args:
            command_id: The `command_id` that `shell_start_command` returned.
        """
        return await _find(ctx, command_id, stop=True)

    return toolset
