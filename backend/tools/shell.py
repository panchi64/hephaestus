"""The `shell` category — running commands in a code conversation's worktree.

Four tools: `run_command` (blocking), `start_command` / `check_command` / `stop_command`
(background), so a dev server or a long test run is a process the agent checks on rather
than a turn that blocks for ten minutes. Rebound per run to the project's worktree, the
same way `files` is. The mechanics are in ``services/sandbox/shell_runner.py``; this file
is the contract the model sees, and the one place that turns what the model *said* into
the boundary the OS holds it to.

**The two executing tools take a third argument: `reach`.** The model states how far a
command needs to go — the worktree, the network, or the operator's whole machine — and
this file turns that declaration into an OS fence profile
(``services/sandbox/fence.workspace_profile``) which ``FencedShell`` spawns the command
under. A `workspace` command that writes outside the worktree, or reaches the network at
all, fails *inside* the fence with a note it can act on instead of quietly succeeding. The
same declaration is what the permission layer rules on
(``services/permissions/judge.py``), so what a command was cleared for and what it can
actually do are one statement rather than two that drift.

**Three layers, one line each, and they do not overlap.** The model *declares* a reach;
this file *translates* it into a profile; ``FencedShell`` *applies* the profile to a
process. Nothing below this file knows what a reach is, and nothing above it builds a
sandbox config. That is the seam to preserve — a fence detail leaking up into the tool, or
a permissions concept leaking down into the runner, is how this becomes two mechanisms
again.

**The fence is required here.** Every command is wrapped by OS-level confinement — the
credential paths and the data directory unreadable, writes landing in the worktree and the
build caches, egress only where the declared reach allows (loopback stays open, so a dev
server and its tests work; ``services/sandbox/host.py`` states what that leaves reachable).
Unlike the host hatch, which degrades and says so because the operator approved *that
specific command*, this refuses when the platform has no primitive: nobody consented to an
unfenced agent shell, and a code conversation is a long stretch of commands nobody reads
one by one.

**Reads are the one half of a declaration the fence cannot hold.** The runtime offers a
read denylist and no read allowlist, so what bounds a read is the stage that ruled on the
command (``services/permissions/judge.py``, ``shell_ast.py``) — never this wrapper. What
the fence makes binding is the *write* and *egress* halves. The tracked working directory
is contained for the same reason: the judge measures relative paths against the worktree
root, so a `cd` that escaped it would leave every later containment claim measured against
the wrong place (``FencedShell._apply_captured_cwd``).

**The fence is applied to every command — cleared or not.** A declaration is a statement
about the command, and it costs nothing to hold a command to its own statement whether the
operator approved it by hand or a review cleared it. In particular a command the
deterministic stage *declined* is still fenced to what it declared: its refusals are about
readability, and keying the fence off them would let a command escape by being written so
nothing could read it. The two exceptions are the approver's own — see :func:`_profile`.

What makes that affordable is everything around it: the throwaway branch the edits land
on, the merge the operator has to approve, one approval on the conversation's first
command, `denied_env_patterns` keeping the operator's model keys out of every spawned
environment, the git config pins that take the keys turning an ordinary `git status` into
an execution back off the repository (``services/sandbox/gitenv.py``), and code mode being
chosen for a thread bound to a project they named. An allowlist of programs was considered
instead of all of it and rejected twice over: an agent writing code needs whatever build
tool the project uses, so any allowlist honest enough to be useful is long enough to be
meaningless, and it would still be bypassable through an allowed interpreter. Bounding
what a spawned program may *do* covers `make`, `npm run` and a test suite without naming
any of them.

**Refused outright outside a worktree mode.** `mode_disabled_tools` already hides these
tools from every mode whose spec does not admit the `shell` category, but that is a
filter, and a filter is the wrong place for the only thing standing between a host command
and a sandbox thread. The check is here too.

**What a command hands back says where it ran, at every permission level.** The result is
structure (``services/sandbox/shell_runner.ShellResult``) with the boundary on it: the
`reach` the model declared, whether a fence was actually built from it, and — when one was
not — why. That last part is the :class:`services.permissions.Capability` this file has
always computed to decide the profile and then dropped on the floor; a contradicted
declaration is the whole reason a command ran unfenced, and it was readable nowhere.
Reviewing levels announce the same two facts on the stream (`review.started` /
`review.completed`), but only Auto reviews, so every other level left the operator with a
command and no account of what held it. The tool's own result is the one place that is
true at all five, warm and on a cold reload alike.

**Output streams while the command runs.** `run_command` passes the runner a progress hook
and each new piece of output goes out as `tool.progress`, the frame a sandbox start
already uses — so a ten-minute build reads as work rather than as a stall. Only the
blocking tool streams: a background command is *already* the answer to "this takes too
long to watch", and the agent reads it with `check_command`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai import AbstractToolset, FunctionToolset, RunContext
from pydantic_ai.exceptions import ApprovalRequired

from core.config import Settings, get_settings
from runs import ToolProgress
from services.egress import EgressPolicy
from services.modes import mode_spec
from services.permissions import Reach, capability_of
from services.sandbox import denied_read_paths, fence
from services.sandbox.shell_runner import (
    BackgroundStatus,
    Confiner,
    FencedShell,
    ProgressHook,
    ShellResult,
)
from services.workspace import RunWorkspace

from .deps import RunDeps
from .emit import RunEventEmitted
from .rebound import WorkspaceToolset
from .workspace import run_workspace

if TYPE_CHECKING:  # pragma: no cover — the fence's own type, never imported at runtime
    from sandbox_runtime import SandboxRuntimeConfig

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
    "Shell commands are only available in a code conversation, which runs in a "
    "project's git worktree. This conversation runs in a container — use `code_execute` "
    "instead."
)

_NO_FENCE = (
    "Shell commands are unavailable: they cannot be confined on this machine ({reason}). "
    "Every command in a code conversation runs under OS-level confinement, and running "
    "one without it is not something this tool will do. Tell the operator what is "
    "missing; until it is fixed, work by reading and editing files rather than by "
    "running commands, and say plainly what you could not verify."
)


async def _fence_gap(confiner: Confiner | None) -> str:
    """Why commands would run unfenced, or "" when they will not.

    An injected confiner *is* the fence — the caller supplied one, and there is nothing to
    probe. Without one the platform's is resolved, which is also what configures it.
    """
    if confiner is not None:
        return ""
    resolved = await fence.resolve_confinement(get_settings())
    return "" if resolved.active else resolved.reason


async def _guard(
    name: str,
    ctx: RunContext[RunDeps],
    workspace: RunWorkspace,
    *,
    confiner: Confiner | None,
) -> str | None:
    """Refuse outside a worktree mode or without a fence; otherwise pause for approval on
    the first command.

    Both refusals are plain strings rather than `ModelRetry`: neither is something the
    model can fix by rephrasing, and handing it a retry would only buy a loop.
    `tool_call_approved` is set on the re-invocation after an approval, so the raise below
    fires once and then lets the command run — and a conversation-scoped grant means the
    operator is asked once per thread, not once per command.
    """
    if mode_spec(ctx.deps.mode).workspace != "worktree" or workspace.kind != "worktree":
        return _WRONG_MODE
    gap = await _fence_gap(confiner)
    if gap:
        return _NO_FENCE.format(reason=gap)
    approved = ctx.tool_call_approved or ctx.deps.delegated_approved
    if name in EXECUTING_TOOLS and not approved:
        raise ApprovalRequired()
    return None


async def _domains(ctx: RunContext[RunDeps]) -> tuple[str, ...]:
    """Everything this conversation may reach, for a command that declared `network`.

    The installation's allowlist plus whatever `code_request_egress` has widened for this
    workspace (``services/egress.py``). No policy bound means nothing is known to be
    allowed and the command reaches nothing — the fence is not the place to guess.

    A grant can only ever *narrow* what actually happens, and saying so here is more use
    than a docstring that implies otherwise: one proxy serves every confined process and
    filters against the configuration installed at boot, so a per-call list wider than the
    installation's buys nothing. What the per-call list decides is whether this command
    gets a route out at all.
    """
    policy = ctx.deps.caps.get_optional(EgressPolicy)
    if policy is None:
        return ()
    return tuple(sorted(await policy.allowed_for(ctx.deps.workspace_key)))


@dataclass(frozen=True, slots=True)
class _Boundary:
    """What holds one command, as something a reader can be shown.

    Built once per call and carried to both ends of it: the profile goes to the shell,
    and the other three fields go onto the result, which is how the declared reach and the
    fence's verdict reach the operator at a level that never reviews anything.
    """

    reach: Reach
    profile: SandboxRuntimeConfig | None
    #: Why no fence was built, in the operator's language. None when one was — the two
    #: are the same fact read twice, and :attr:`fenced` is the one to branch on.
    reason: str | None

    @property
    def fenced(self) -> bool:
        return self.profile is not None


#: Why a command ran with no fence around it. Every one of them is somebody's explicit
#: yes (see :func:`_boundary`), so they are worded as what was asked for rather than as a
#: failure — an operator reading "unfenced" with no reason beside it would be right to
#: read it as the gate having broken.
_HOST_DECLARED = "the command declared `host`, which asks for the operator's machine itself"
_ESCAPES = "the command names {paths}, outside the worktree its declaration covers"
_CONTRADICTED = "the command reaches the network, which its `workspace` declaration does not cover"


async def _boundary(
    name: str,
    command: str,
    reach: Reach,
    workspace: RunWorkspace,
    domains: tuple[str, ...],
    settings: Settings,
) -> _Boundary:
    """What holds this command: the fence profile, or none and the reason there is none.

    **Built from the declaration, not from a judgement**, and that separation is the whole
    of it: the permission layer's stages answer *who must approve*, and this answers *what
    the boundary is*. Nothing is threaded through the park, the approval payload or the
    resume, and a call the operator approved by hand at another level is fenced identically,
    having never passed a gate at all.

    Keying it off the deterministic stage instead would hand the model the boundary as a
    lever. That stage declines `ls $(true)` and `git commit -m 'fix the parser'` for being
    *unreadable* — not for reaching anywhere — so running what it declined unwrapped would
    let any command escape the fence by being written so nothing could read it, and the
    reviewer that then allowed it was never asked about the fence.

    There are exactly two ways out and each is somebody's explicit yes. A `host`
    declaration asks for the unfenced machine, which is what the approver was shown — and
    what separates it from :func:`_guard`'s refusal, which is about a *platform* that
    cannot fence rather than an operator who chose not to. A declaration the command's own
    syntax **contradicts** — a `workspace` command naming `/etc/passwd` or a URL — was
    approved in the words it was read in, and fencing it to the reach it plainly does not
    have would break that act rather than bound it.

    **An empty allowed-domains list is not one of them.** A `network` declaration with
    nothing allowed still runs fenced, with no egress at all — the command fails at the
    boundary and says so, which is the whole point of the declaration. Reading the empty
    list as "no list to hold it to" and lifting the fence inverted the setting: an operator
    who empties it to reach *less* would have handed every approved networked command the
    loosest execution path there is, write confinement and the read denials included.

    **The extraction is kept, not consumed and dropped.** The capability read off the
    command is the only account of *why* a command ran as written, and one of the two ways
    out is readable nowhere else: a `host` declaration is in the call's own arguments, but
    a declaration the command's syntax contradicts is in neither the arguments nor the
    result, so an operator seeing `reach: workspace` beside an unfenced command would have
    no way to tell that from a bug. The reason is carried out with the profile.
    """
    if reach == "host":
        return _Boundary(reach=reach, profile=None, reason=_HOST_DECLARED)
    capability = capability_of(
        f"{_NAMESPACE}{name}", {"command": command, "reach": reach}, root=workspace.root
    )
    if capability.escapes:
        return _Boundary(
            reach=reach,
            profile=None,
            reason=_ESCAPES.format(paths=", ".join(capability.escapes)),
        )
    if reach == "workspace" and capability.network:
        return _Boundary(reach=reach, profile=None, reason=_CONTRADICTED)
    profile = fence.workspace_profile(
        workspace.root,
        fence.GitDirs.read(workspace.root),
        workspace.branch,
        allowed_domains=domains if reach == "network" else (),
        # The host hatch's own denials, not a second list: a per-call profile replaces the
        # global one rather than narrowing it, and a command the deterministic stage
        # cleared for itself must never be fenced more loosely than one the operator read
        # and approved.
        deny_read=denied_read_paths(settings),
        # A *separate* setting from the host hatch's, and the asymmetry is the point: a
        # host command is one the operator read, so its list may be as broad as `~`; a
        # `workspace` command is one nobody was asked about, so the same breadth here would
        # be the fence dissolved. Seeded with the build caches and nothing else.
        allow_write=settings.worktree_command_allow_write,
        linux=sys.platform.startswith("linux"),
    )
    return _Boundary(reach=reach, profile=profile, reason=None)


def _fence_note(boundary: _Boundary, result: ShellResult) -> str | None:
    """The fence's note about a permission failure in this command's output, or None.

    Read back off :func:`services.sandbox.fence.annotate` rather than restating the
    denials it looks for: what a fence failure looks like has one definition, and a second
    copy here would be the copy that stops being updated. It used to be appended to the
    one labelled string; with fields to put it in, it is its own.
    """
    if not boundary.fenced:
        return None
    text = f"{result.stdout}\n{result.stderr}"
    return fence.annotate(text)[len(text) :].strip() or None


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
        await ctx.emit(
            RunEventEmitted(
                body=ToolProgress(tool_call_id=tool_call_id, elapsed_s=elapsed_s, partial=chunk)
            )
        )

    return hook


def _run_payload(result: ShellResult, boundary: _Boundary, timeout_s: float) -> dict[str, Any]:
    """One finished command, as the model and the operator's terminal both read it.

    The optional keys are **absent rather than null** when they say nothing, the same way
    `code_execute`'s result omits its failure hint on success: a key whose presence is the
    fact is easier to read than a null a reader has to know the meaning of.
    """
    payload: dict[str, Any] = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        **_boundary_fields(boundary),
    }
    note = _fence_note(boundary, result)
    if note is not None:
        payload["fence_note"] = note
    if result.timed_out:
        payload["error"] = (
            f"The command was still running after {timeout_s}s and was terminated. "
            "Everything it printed before that is above."
        )
    elif not result.ok:
        payload["error"] = f"The command exited with status {result.exit_code}."
    return payload


def _boundary_fields(boundary: _Boundary) -> dict[str, Any]:
    """The declaration and the fence's verdict, as the two keys every executing tool
    carries — at every permission level, which is the whole point of them being here."""
    fields: dict[str, Any] = {"reach": boundary.reach, "fenced": boundary.fenced}
    if boundary.reason is not None:
        fields["unfenced_reason"] = boundary.reason
    return fields


def _background_payload(status: BackgroundStatus) -> dict[str, Any]:
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


def _unknown_command(command_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "command_id": command_id,
        "error": (
            f"No background command with ID {command_id!r} is running in this "
            "conversation. It may have already been stopped."
        ),
    }


def _tools_for(shell: FencedShell, settings: Settings) -> FunctionToolset[RunDeps]:
    """The four tools, over one bound shell."""
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    async def boundary_for(
        ctx: RunContext[RunDeps], name: str, command: str, reach: Reach
    ) -> _Boundary:
        """This call's boundary — resolved here so both executing tools ask one question.

        The workspace is read per call rather than captured when the shell was built, and
        that is not incidental: `WorkspaceToolset` caches one :class:`FencedShell` per
        *root* and shares it across every run working there, while the branch a profile
        scopes its ref writes to belongs to the run. Freezing the branch into the shell
        would fence one thread's commits to another thread's ref.

        It costs no second resolution — `bind` has already filled the run's memo
        (``tools/workspace.run_workspace``), so this is the very object it refused or
        allowed on. `None` cannot reach here for the same reason: a run with no workspace
        was turned away in words before any tool body ran.
        """
        workspace = await run_workspace(ctx)
        if workspace is None:  # pragma: no cover — `bind` refuses this before we get here
            return _Boundary(reach=reach, profile=None, reason=None)
        return await _boundary(name, command, reach, workspace, await _domains(ctx), settings)

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def run_command(
        ctx: RunContext[RunDeps],
        command: str,
        reach: Reach = "workspace",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Execute a shell command in the project's worktree and return its output.

        Args:
            command: The shell command to run.
            reach: How far this command needs to go. `workspace` runs it fenced to the
                worktree with no network — the right answer for builds, tests, git and
                anything reading or writing the checkout. `network` additionally allows the
                domains the operator has permitted, for installing dependencies. `host`
                lifts the fence for a command that has to act on the machine itself. A
                command that needs more than it declared fails inside the fence and says
                so; declare again rather than working around it.
            timeout_seconds: Maximum seconds to wait (default: 300).

        Returns:
            `ok`, `exit_code` (null if it timed out), `stdout`, `stderr`, `timed_out`,
            `duration_ms`, the `reach` you declared and whether it ran `fenced`.
        """
        boundary = await boundary_for(ctx, "run_command", command, reach)
        result = await shell.run(
            command,
            profile=boundary.profile,
            timeout_seconds=timeout_seconds,
            on_progress=_progress_hook(ctx),
        )
        timeout = timeout_seconds if timeout_seconds is not None else _TIMEOUT_S
        return _run_payload(result, boundary, timeout)

    @toolset.tool(metadata={"code_arg_name": "command", "code_arg_language": "shell"})
    async def start_command(
        ctx: RunContext[RunDeps], command: str, reach: Reach = "workspace"
    ) -> dict[str, Any]:
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
        command_id = await shell.start(command, profile=boundary.profile)
        return {
            "ok": True,
            "command_id": command_id,
            "command": command,
            **_boundary_fields(boundary),
        }

    @toolset.tool
    async def check_command(ctx: RunContext[RunDeps], command_id: str) -> dict[str, Any]:
        """Check the status and recent output of a background command.

        Args:
            command_id: The ID returned by start_command.

        Returns:
            `status` (`running` or `finished`), `exit_code` once it has one, and
            everything it has printed so far in `stdout` / `stderr`.
        """
        status = await shell.check(command_id)
        return _unknown_command(command_id) if status is None else _background_payload(status)

    @toolset.tool
    async def stop_command(ctx: RunContext[RunDeps], command_id: str) -> dict[str, Any]:
        """Stop a background command and return its final output.

        Args:
            command_id: The ID returned by start_command.

        Returns:
            `status` of `stopped`, the final `exit_code` where it had one, and everything
            the command printed.
        """
        status = await shell.stop(command_id)
        return _unknown_command(command_id) if status is None else _background_payload(status)

    return toolset


def _toolset_for(
    root: Path,
    *,
    confiner: Confiner,
    settings: Settings,
    shells: list[FencedShell] | None = None,
) -> AbstractToolset[RunDeps]:
    shell = FencedShell(
        root,
        confiner=confiner,
        default_timeout=_TIMEOUT_S,
        max_output_chars=_MAX_OUTPUT_CHARS,
    )
    if shells is not None:
        shells.append(shell)
    return _tools_for(shell, settings)


def shell_toolset(
    *, confiner: Confiner | None = None, shells: list[FencedShell] | None = None
) -> AbstractToolset[RunDeps]:
    """The `shell` category, built once per turn and rebound per run's worktree.

    ``confiner`` replaces the platform's fence, and is how a test drives these tools
    without running seatbelt over its own machine. Passing one asserts that commands are
    fenced, so it is never a way to switch the fence off.

    ``shells`` collects every shell this toolset binds, for the one caller that owns their
    lifetime: a conversation's shell outlives its turns on purpose and is stopped through
    `stop_command`, while a run whose workspace is taken away when it ends — an isolated
    sub-agent's fork — has to have what it left running reaped with it.
    """
    settings = get_settings()
    build = partial(_toolset_for, confiner=confiner or fence.wrap, settings=settings, shells=shells)
    return WorkspaceToolset(
        "shell",
        build(_TEMPLATE_ROOT),
        build,
        guard=partial(_guard, confiner=confiner),
    )
