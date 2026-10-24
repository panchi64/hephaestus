"""The `shell` category: commands in the conversation's VM, and one fenced escape to the host.

``run_command``, ``start_command``, ``check_command`` and ``stop_command`` are the harness
``Shell`` acting on ``ctx.workspace`` (``tools/home_workspace.py``), so they run in the
conversation's machine, which mounts the home's folders at their home paths. What a
command reaches on the network is that machine's egress allowlist. ``cd`` carries over
between the commands of one turn.

``run_on_host`` runs on the operator's machine inside the host fence
(``services/sandbox/fence.py``), starting in the host folder of the thread's project or
else in the host scratch directory. It always asks first, and its changes are not
recorded for revert.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast, override

from pydantic_ai import AbstractToolset, RunContext, ToolDefinition, ToolsetTool
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai_harness import LLM_API_KEY_ENV_PATTERNS
from pydantic_ai_harness.shell import ShellToolset

from core.config import get_settings
from services.home.paths import HomePathError, parse
from services.home.table import RunHome
from services.projects.worktree import WorktreeBusyError
from services.sandbox import HostExecutionError, host_scratch_dir, resolve_confinement, run_on_host
from services.sandbox.refusals import host_refusal

from .code import exec_result
from .deps import RunDeps
from .home import resolve_run_home

#: Long enough for a real build or test suite; anything longer belongs in `start_command`.
SHELL_TIMEOUT_S = 300.0

#: What one result keeps, from the tail; a larger result is spilled to a file
#: (``tools/spill.py``).
SHELL_MAX_OUTPUT_CHARS = 100_000

#: A background command's log is emptied past this, so one that logs without end cannot
#: fill the disk it shares with the thread's files.
_MAX_LOG_BYTES = 100_000_000

#: The tools that execute something new; `check_command` and `stop_command` act on a process
#: already approved into existence.
EXECUTING_TOOLS = frozenset({"run_command", "start_command"})

#: The same, namespaced. A name absent from `app.state.gated_tools` cannot be granted for the
#: conversation, so the operator would be asked on every command.
GATED_TOOLS = frozenset(f"shell_{name}" for name in EXECUTING_TOOLS)


def _host_root(home: RunHome | None) -> Path:
    """Where a host command starts: the host folder of the thread's project or worktree,
    else the host scratch directory."""
    if home is None:
        return host_scratch_dir()
    try:
        target = parse(home.default.path)
    except HomePathError:
        return host_scratch_dir()
    mount = home.mount(target.place)
    if mount is None or target.place.kind not in ("projects", "worktrees"):
        return host_scratch_dir()
    return mount.root


async def run_on_host_tool(
    ctx: RunContext[RunDeps], command: str, explanation: str, timeout_s: float = 120.0
) -> dict[str, object]:
    """Runs a command on the operator's own computer, not your VM, inside a confining
    fence. Use it only when the host itself has to change or be inspected: a system
    setting, an installed application, the operator's own checkout outside your VM. For
    anything your VM can do, use `shell_run_command`, which cannot touch the host.

    It starts in the host folder of this conversation's project, or else in a scratch
    directory of its own, so name other host paths in full. Its changes cannot be
    reverted. The fence keeps it from the operator's credentials and this application's
    data directory, and gives it no network beyond the domains the installation allows. A
    permission error on one of those paths is the fence doing its job: report it rather
    than working around it.

    Returns `ok`, `exit_code`, `stdout`, `stderr` and `timed_out`, and `fenced`; when it
    is false, `unfenced_reason` says why. A `fence_note` explains a refusal.

    Args:
        command: The command, as you would type it in a terminal on the host.
        explanation: What the command does and what it changes on the operator's
            computer.
        timeout_s: Seconds before the command is killed. Default 120.
    """
    del explanation  # The approval card reads it from the call's arguments.
    settings = get_settings()
    # The installation-wide allowlist, not this conversation's grants: one proxy serves
    # every confined host process (`services/sandbox/fence.confine`).
    confinement = await resolve_confinement(settings)
    try:
        home = await resolve_run_home(ctx.deps)
    except WorktreeBusyError:
        home = None
    try:
        result = await run_on_host(
            command, timeout_s=timeout_s, confinement=confinement, cwd=_host_root(home)
        )
    except HostExecutionError as exc:
        return {"ok": False, "error": f"The host command could not be launched: {exc}"}
    payload = exec_result(result, sandboxed=False)
    payload["fenced"] = confinement.active
    payload["reach"] = "host"
    if not confinement.active:
        payload["unfenced_reason"] = f"the host fence is not applied: {confinement.reason}"
    refusal = host_refusal(fence_active=confinement.active, ok=result.ok, stderr=result.stderr)
    if refusal is not None:
        payload["fence_note"] = refusal.note
    return payload


async def _only_where_there_is_someone_to_ask(
    ctx: RunContext[RunDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Withhold the host tool from a delegated run, which has nobody to ask."""
    return None if ctx.deps.delegated_approved else tool_def


def _add_host_tool(toolset: ShellToolset[RunDeps]) -> None:
    _ = toolset.add_function(
        run_on_host_tool,
        name="run_on_host",
        requires_approval=True,
        prepare=_only_where_there_is_someone_to_ask,
        metadata={"code_arg_name": "command", "code_arg_language": "shell"},
    )


class ShellCategory(ShellToolset[RunDeps]):
    """The harness shell plus the host tool. Every executing call is settled by the
    permission layer (``agent/gating.py``), at every level, before it runs."""

    def __init__(self) -> None:
        super().__init__(
            allowed_commands=[],
            # The VM is the fence; a denylist of command names would only get in the way.
            denied_commands=[],
            denied_operators=[],
            default_timeout=SHELL_TIMEOUT_S,
            max_output_chars=SHELL_MAX_OUTPUT_CHARS,
            persist_cwd=True,
            allow_interactive=False,
            max_log_bytes=_MAX_LOG_BYTES,
            denied_env_patterns=LLM_API_KEY_ENV_PATTERNS,
            id="shell",
        )
        _add_host_tool(self)

    @override
    async def for_run(self, ctx: RunContext[RunDeps]) -> AbstractToolset[RunDeps]:
        # The base class would hand back a plain `ShellToolset`, without the host tool or
        # the approval check below; each run gets its own working directory either way.
        return ShellCategory()

    @override
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, object],
        ctx: RunContext[RunDeps],
        tool: ToolsetTool[RunDeps],
    ) -> object:
        approved = ctx.tool_call_approved or ctx.deps.delegated_approved
        if name in EXECUTING_TOOLS and not approved:
            raise ApprovalRequired()
        try:
            return cast(object, await super().call_tool(name, tool_args, ctx, tool))
        finally:
            # Where the next command starts, which the guard and the judge place paths from.
            ctx.deps.shell_cwd = self._cwd


def shell_toolset() -> AbstractToolset[RunDeps]:
    """The `shell` category, built once at app assembly; each run gets its own copy."""
    return ShellCategory()
