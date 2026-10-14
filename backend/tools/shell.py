"""The `shell` category — running commands in a project chat's folder.

Four tools: `run_command` (blocking), `start_command` / `check_command` / `stop_command`
(background), so a dev server or a long test run is a process the agent checks on rather
than a turn that blocks for ten minutes. Rebound per run to the project's folder, the
same way `files` is. This file is the contract the model sees, and the one place that
turns what the model *said* into where a command runs.

**Commands run in the platform VM, not on the operator's machine.** The project folder is
brought into the VM before each command and what it changed is brought back after
(``services/sandbox/project_shell.py``, ``services/vm/project.py``): what git counts as
the project crosses, ignored build output stays on its own side, and commits come back
only as a fast-forward of the branch. A command cannot read the operator's home directory,
their other projects, or anything else the folder does not hold.

**The executing tools take a `reach`.** The model states how far a command needs to go,
and this file holds it to that:

- `workspace` runs in the VM with no network at all;
- `network` runs in the VM with the conversation's egress credentials, reaching the
  domains the operator has allowed (``services/egress.py``);
- `host` runs on the operator's machine itself, unfenced, for a command that has to act
  on that machine — which is what the approver was shown.

The same declaration is what the permission layer rules on
(``services/permissions/judge.py``), so what a command was cleared for and where it runs
are one statement. A `workspace` declaration the command's own syntax contradicts (it
names a URL) was approved in the words it was read in, so it gets the network it plainly
needs rather than failing.

**Refused outside a project folder, and while the VM is down.** The enabled gate already
hides these tools from sandbox chats (``services/workspace_kind.py``), but a filter is the
wrong place for the only thing standing between a host command and a sandbox thread, so
the check is here too. Without the VM there is nowhere to run a fenced command, and
running one on the operator's machine instead is not something this tool will do.

**What a command hands back says where it ran.** The result carries the `reach` the
model declared, whether it ran `fenced` in the VM, and — when it ran on the host — why.
Bringing the work back can have something to say too (commits on another branch, a
rewritten branch); that goes on the result as `notes`.

**Output streams while the command runs.** `run_command` passes a progress hook and each
new piece of output goes out as `tool.progress`, so a ten-minute build reads as work
rather than as a stall. Only the blocking tool streams: a background command is *already*
the answer to "this takes too long to watch", and the agent reads it with
`check_command`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic_ai import AbstractToolset, FunctionToolset, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from runs import ToolProgress
from services.egress import EgressPolicy
from services.permissions import Reach, capability_of
from services.sandbox.project_shell import ProjectShell
from services.sandbox.session import proxy_env
from services.sandbox.shell_runner import (
    BackgroundStatus,
    HostShell,
    ProgressHook,
    ShellResult,
)
from services.vm import DENIED_MARKER
from services.vm.project import ProjectCopies
from services.workspace import RunWorkspace
from services.workspace_kind import is_project_folder

from .deps import RunDeps
from .emit import RunEventEmitted
from .rebound import WorkspaceToolset
from .workspace import run_workspace

#: Long enough for a real build or test suite; short enough that a hung command doesn't
#: hold the turn open indefinitely. The agent can pass its own timeout per call, and any
#: genuinely long-running thing belongs in `start_command`.
_TIMEOUT_S = 300.0

#: Effectively no cap. This codebase has one context reduction — conversation compaction,
#: on measured pressure — and truncating a tool result is exactly what was torn out of
#: `code_execute`. A pathological command's output is caught by the run's own
#: context-overflow stop, which says so out loud rather than silently costing the model
#: the middle of what it just asked for.
_MAX_OUTPUT_CHARS = 2_000_000

#: The two tools that execute something new. `check_command` and `stop_command` act on a
#: process that was already approved into existence, so re-asking would be noise.
EXECUTING_TOOLS = frozenset({"run_command", "start_command"})

#: The same two, namespaced — the conditionally-gated names this category contributes to
#: the approval-scope vocabulary. **This declaration is what makes the gate usable.** The
#: raise below parks the run either way, but a name absent from `app.state.gated_tools`
#: never reaches `tools/catalog.approval_scopes`, so the operator could not grant it for
#: the conversation (nor pre-authorize it on a scheduled task) and would be asked again on
#: every single command — which would make the approval gate the nuisance an allowlist was
#: rejected for being.
GATED_TOOLS = frozenset(f"shell_{name}" for name in EXECUTING_TOOLS)

#: The prefix `tools/toolsets.py` will put in front of these names. Spelled here because
#: the capability extraction is keyed on the namespaced name and this file only ever sees
#: the bare one.
_NAMESPACE = "shell_"

#: A root no run will ever have. The template toolset exists to answer `get_tools`, which
#: does not depend on where a command would run, and a plausible-looking path here would
#: invite the assumption that it does.
_TEMPLATE_ROOT = Path("/nonexistent-template-root")

_WRONG_MODE = (
    "Shell commands are only available in a conversation filed under a project, which "
    "works in that project's folder. This conversation runs in the sandbox — use "
    "`code_execute` instead."
)

_NO_VM = (
    "Shell commands are unavailable: the platform VM they run in is not running ({reason}). "
    "Commands in a project chat run in the VM, and running one on the operator's machine "
    "instead is not something this tool will do. Tell the operator; until it is back, "
    "work by reading and editing files rather than by running commands, and say plainly "
    "what you could not verify."
)

#: How long the first command of a session waits for a VM that is still starting.
_VM_WAIT_S = 120.0


async def _guard(name: str, ctx: RunContext[RunDeps], workspace: RunWorkspace) -> str | None:
    """Refuse outside a project folder or without the VM; otherwise pause for approval on
    the first command.

    Both refusals are plain strings rather than `ModelRetry`: neither is something the
    model can fix by rephrasing, and handing it a retry would only buy a loop.
    `tool_call_approved` is set on the re-invocation after an approval, so the raise below
    fires once and then lets the command run — and a conversation-scoped grant means the
    operator is asked once per thread, not once per command.
    """
    if not is_project_folder(ctx.deps.workspace_kind) or workspace.kind == "sandbox":
        return _WRONG_MODE
    copies = ctx.deps.caps.get_optional(ProjectCopies)
    if copies is None:
        return _NO_VM.format(reason="it is disabled")
    vm = copies.vm
    if not vm.available and not await vm.wait_ready(_VM_WAIT_S):
        return _NO_VM.format(reason=vm.failure or "it is still starting")
    approved = ctx.tool_call_approved or ctx.deps.delegated_approved
    if name in EXECUTING_TOOLS and not approved:
        raise ApprovalRequired()
    return None


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


def boundary(name: str, command: str, reach: Reach, workspace: RunWorkspace) -> _Boundary:
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
            f"{_NAMESPACE}{name}", {"command": command, "reach": reach}, root=workspace.root
        )
        network = capability.network
    return _Boundary(reach=reach, where="vm", network=network, reason=None)


async def _vm_env(ctx: RunContext[RunDeps], boundary: _Boundary) -> dict[str, str]:
    """The environment a VM command runs with: a scratch `HOME` outside the project, and
    the conversation's egress credentials when it may reach the network."""
    env = {"HOME": "/tmp", "GIT_PAGER": "cat", "PAGER": "cat"}
    if not boundary.network:
        return env
    policy = ctx.deps.caps.get_optional(EgressPolicy)
    copies = ctx.deps.caps.get_optional(ProjectCopies)
    if policy is None or copies is None:
        return env
    allow_dir = await policy.materialise(ctx.deps.workspace_key)
    return {**env, **proxy_env(copies.vm.gateway.credentials_for(allow_dir))}


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
    return None


def _progress_hook(ctx: RunContext[RunDeps]) -> ProgressHook | None:
    """Put this command's output on the operator's stream as it arrives, or None.

    None where there is no call to attribute a frame to — a tool driven outside a run, in
    a test or a direct call. `tool.progress` is the frame a cold sandbox start already
    uses, so there is nothing new on the wire: what is new is that a `partial` can now
    arrive many times for one call, each carrying the *next* piece of output rather than
    the whole of it so far. A reader appends; the completed result is still the record,
    and is what a reload reads.
    """
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
    result: ShellResult, boundary: _Boundary, timeout_s: float, notes: tuple[str, ...] = ()
) -> dict[str, object]:
    """One finished command, as the model and the operator's terminal both read it.

    The optional keys are **absent rather than null** when they say nothing, the same way
    `code_execute`'s result omits its failure hint on success: a key whose presence is the
    fact is easier to read than a null a reader has to know the meaning of.
    """
    payload: dict[str, object] = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
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
    """The declaration and the fence's verdict, as the two keys every executing tool
    carries — at every permission level, which is the whole point of them being here."""
    fields: dict[str, object] = {"reach": boundary.reach, "fenced": boundary.fenced}
    if boundary.reason is not None:
        fields["unfenced_reason"] = boundary.reason
    return fields


def _background_payload(status: BackgroundStatus) -> dict[str, object]:
    """A background command as it stands. ``ok`` reads the same way it does everywhere
    else here — nothing went wrong — so a process still running is fine, and one that has
    exited non-zero is not."""
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


def _tools_for(project: ProjectShell, host: HostShell) -> FunctionToolset[RunDeps]:
    """The four tools, over one folder's VM shell and its host shell."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    async def boundary_for(
        ctx: RunContext[RunDeps], name: str, command: str, reach: Reach
    ) -> _Boundary:
        workspace = await run_workspace(ctx)
        if workspace is None:  # pragma: no cover — `bind` refuses this before we get here
            return _Boundary(reach=reach, where="vm", network=False, reason=None)
        return boundary(name, command, reach, workspace)

    def copies_of(ctx: RunContext[RunDeps]) -> ProjectCopies:
        copies = ctx.deps.caps.get_optional(ProjectCopies)
        if copies is None:  # pragma: no cover — the guard refuses this before we get here
            raise RuntimeError("the project shell has no platform VM")
        return copies

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def run_command(
        ctx: RunContext[RunDeps],
        command: str,
        reach: Reach = "workspace",
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        """Execute a shell command in the project's folder and return its output.

        Args:
            command: The shell command to run.
            reach: How far this command needs to go. `workspace` runs it in your VM's copy
                of the project with no network — the right answer for builds, tests, git and
                anything reading or writing the project. `network` additionally reaches the
                domains the operator has permitted, for installing dependencies. `host` runs
                it on the operator's machine itself, for a command that has to act on that
                machine. Files you change come back to the project after every command;
                ignored paths (dependencies, build output) stay in the VM.
            timeout_seconds: Maximum seconds to wait (default: 300).

        Returns:
            `ok`, `exit_code` (null if it timed out), `stdout`, `stderr`, `timed_out`,
            `duration_ms`, the `reach` you declared, whether it ran `fenced` in the VM,
            and any `notes` from bringing its work back.
        """
        boundary = await boundary_for(ctx, "run_command", command, reach)
        timeout = timeout_seconds if timeout_seconds is not None else _TIMEOUT_S
        if not boundary.fenced:
            result = await host.run(
                command,
                timeout_seconds=timeout_seconds,
                on_progress=_progress_hook(ctx),
            )
            return _run_payload(result, boundary, timeout)
        done = await project.run(
            copies_of(ctx),
            command,
            env=await _vm_env(ctx, boundary),
            timeout_seconds=timeout_seconds,
            on_progress=_progress_hook(ctx),
        )
        return _run_payload(done.result, boundary, timeout, done.notes)

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def start_command(
        ctx: RunContext[RunDeps], command: str, reach: Reach = "workspace"
    ) -> dict[str, object]:
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
        boundary = await boundary_for(ctx, "start_command", command, reach)
        if boundary.fenced:
            command_id = await project.start(
                copies_of(ctx), command, env=await _vm_env(ctx, boundary)
            )
        else:
            command_id = await host.start(command)
        return {
            "ok": True,
            "command_id": command_id,
            "command": command,
            **_boundary_fields(boundary),
        }

    @toolset.tool
    async def check_command(ctx: RunContext[RunDeps], command_id: str) -> dict[str, object]:
        """Check the status and recent output of a background command.

        Args:
            command_id: The ID returned by start_command.

        Returns:
            `status` (`running` or `finished`), `exit_code` once it has one, and
            everything it has printed so far in `stdout` / `stderr`.
        """
        status = await project.check(copies_of(ctx), command_id) or await host.check(command_id)
        return _unknown_command(command_id) if status is None else _background_payload(status)

    @toolset.tool
    async def stop_command(ctx: RunContext[RunDeps], command_id: str) -> dict[str, object]:
        """Stop a background command and return its final output.

        Args:
            command_id: The ID returned by start_command.

        Returns:
            `status` of `stopped`, the final `exit_code` where it had one, and everything
            the command printed.
        """
        status = await project.stop(copies_of(ctx), command_id) or await host.stop(command_id)
        return _unknown_command(command_id) if status is None else _background_payload(status)

    return toolset


@dataclass(frozen=True, slots=True)
class BoundShell:
    """One folder's two shells, for the caller that owns their lifetime."""

    project: ProjectShell
    host: HostShell

    async def shutdown(self, copies: ProjectCopies | None) -> None:
        if copies is not None:
            await self.project.shutdown(copies)
        await self.host.shutdown()


def _toolset_for(
    root: Path,
    *,
    shells: list[BoundShell] | None = None,
) -> AbstractToolset[RunDeps]:
    bound = BoundShell(
        project=ProjectShell(root, default_timeout=_TIMEOUT_S, max_output_chars=_MAX_OUTPUT_CHARS),
        host=HostShell(
            root,
            default_timeout=_TIMEOUT_S,
            max_output_chars=_MAX_OUTPUT_CHARS,
        ),
    )
    if shells is not None:
        shells.append(bound)
    return _tools_for(bound.project, bound.host)


def shell_toolset(*, shells: list[BoundShell] | None = None) -> AbstractToolset[RunDeps]:
    """The `shell` category, built once per turn and rebound per run's project folder.

    ``shells`` collects every shell this toolset binds, for the one caller that owns their
    lifetime: a conversation's shell outlives its turns on purpose and is stopped through
    `stop_command`, while a run whose workspace is taken away when it ends — an isolated
    sub-agent's fork — has to have what it left running reaped with it.
    """
    build = partial(_toolset_for, shells=shells)
    return WorkspaceToolset("shell", build(_TEMPLATE_ROOT), build, guard=_guard)
