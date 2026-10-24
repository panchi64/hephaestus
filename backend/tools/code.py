"""Code tools: Python in the agent's own machine, a fenced command on the host, and
widening what the agent's machine may reach.

``code_execute`` runs Python in the thread's scratch (``/work``, which is ``~/scratch``),
contained in the platform VM. Shell commands belong to the ``shell`` category.

``code_run_host_command`` runs on the real host under the host fence; its request carries
a plain-language ``explanation`` the operator can judge without reading the command.

``code_request_egress`` is asked per call: reaching a host the allowlist does not name is
an exit from the box.

Execution mechanics live in ``services/sandbox``. Without a sandbox runtime ``code_execute``
reports the capability disabled and never falls back to the host.
"""

from __future__ import annotations

import re
from dataclasses import replace

from pydantic_ai import FunctionToolset, RunContext, ToolDefinition

from core.config import Settings, get_settings
from core.diagnostics import render_tool_error
from core.exceptions import InvalidInputError
from runs import ToolProgress
from services.egress import EgressPolicy
from services.permissions.guards import HOST_COMMAND_TOOL
from services.sandbox import (
    HostExecutionError,
    SandboxError,
    SandboxResult,
    SandboxSessionManager,
    SandboxSpec,
    host_scratch_dir,
    resolve_confinement,
    run_on_host,
)
from services.sandbox.refusals import host_refusal, is_egress_denied, vm_refusal

from .command_ledger import ledger_check
from .deps import RunDeps
from .emit import RunEventEmitted
from .run_home import home_of

# The pids limit surfaces as a failed fork inside the process with no distinct signal, so
# this heuristic matches the OS errors it typically prints.
_PID_CAP_MARKERS = (
    "resource temporarily unavailable",
    "cannot allocate memory",
    "can't fork",
    "fork failed",
)

_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")


def _looks_like_pid_cap(stderr: str) -> bool:
    low = stderr.lower()
    return "fork" in low and any(marker in low for marker in _PID_CAP_MARKERS)


def _failure_hint(
    exit_code: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    *,
    memory: str,
    pids_limit: int,
    sandboxed: bool,
    delegated: bool = False,
) -> str:
    """A short reason for a failed run, naming the configured cap most likely hit (stderr
    is often empty for a hard kill). A missing package or a host off the allowlist gets the
    exact next call, since the fix is another call, not different code."""
    if timed_out:
        return "It exceeded the time limit and was killed; reduce the work or raise timeout_s."
    if exit_code == 137:  # SIGKILL
        return (
            f"The process was killed (exit 137, SIGKILL) — most likely the "
            f"{memory} memory cap was hit; reduce memory use or process data in "
            "smaller chunks."
        )
    if exit_code == 139:  # SIGSEGV
        return (
            "The process crashed (exit 139, SIGSEGV) — a fault in the code itself, "
            "not a resource cap."
        )
    missing = _MISSING_MODULE.search(stderr) if sandboxed else None
    if missing:
        module = missing.group(1).split(".")[0]
        return (
            f"The Python package providing `{module}` is not installed on your machine. "
            f"Install it first with the shell — `pip install {module}` (or the PyPI "
            "package that provides that module) with reach `network` — then re-run this "
            "code unchanged."
        )
    if sandboxed and is_egress_denied(stderr + "\n" + stdout):
        if delegated:
            # `code_request_egress` is withheld from a delegate; naming it would cost turns.
            return (
                "That host is not on your egress allowlist, so the connection was refused "
                "before it left your machine — the code itself is fine. You cannot widen "
                "it from here: name the host in your report and let the agent that "
                "delegated to you ask for it."
            )
        return (
            "That host is not on your egress allowlist, so the connection was refused "
            "before it left your machine — the code itself is fine. Call "
            "`code_request_egress(domains=[...], reason=...)` naming the hosts you need "
            "and why, and once it is approved retry this same code unchanged."
        )
    if _looks_like_pid_cap(stderr):
        return (
            f"The process could not create more processes/threads (capped at "
            f"{pids_limit}); reduce concurrency (fewer workers/threads/subprocesses)."
        )
    if stderr.strip():
        return f"It exited with a non-zero status ({exit_code}); see stderr for the error."
    if stdout.strip():
        return (
            f"It exited with a non-zero status ({exit_code}); stderr is empty — "
            + "the error text is in stdout."
        )
    return f"It exited with a non-zero status ({exit_code}) and produced no output."


def exec_result(
    result: SandboxResult, settings: Settings, *, sandboxed: bool, delegated: bool = False
) -> dict[str, object]:
    """Shape an execution result for the model: a success flag, stdout and stderr whole,
    and on failure a hint naming the cap likely hit.

    Output is not trimmed; the turn's context-overflow stop catches a pathological run."""
    payload: dict[str, object] = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
    }
    if result.notes:
        payload["notes"] = list(result.notes)
    if not result.ok:
        payload["error"] = _failure_hint(
            result.exit_code,
            result.timed_out,
            result.stdout,
            result.stderr,
            memory=settings.sandbox_memory,
            pids_limit=settings.sandbox_pids_limit,
            sandboxed=sandboxed,
            delegated=delegated,
        )
        refusal = (
            vm_refusal(network=True, ok=False, stdout=result.stdout, stderr=result.stderr)
            if sandboxed
            else None
        )
        if refusal is not None and refusal.denied_host is not None:
            payload["denied_host"] = refusal.denied_host
            payload["fence_note"] = refusal.note
            payload["fenced"] = True
            payload["reach"] = "network"
    return payload


def _execute_description(settings: Settings, *, delegated: bool = False) -> str:
    """The `execute` tool's description, stating the sandbox's configured resource caps.

    ``delegated`` swaps the two sentences that name other tools: a delegated run's tools
    are not namespaced and the ones that ask the operator are withheld.
    """
    files = (
        "Your file tools and shell reach this same directory as `~/scratch`"
        if delegated
        else "The `files_*` and `shell_*` tools reach this same directory as `~/scratch`"
    )
    ask = (
        "Any other host is refused, and there is nobody here to ask: say in your report "
        + "which host you could not reach, and get on with what you can do without it."
        if delegated
        else "Any other host is refused until you ask for it: call `code_request_egress` "
        + "with the domains you need and why, and once it is approved, run the same code "
        + "again unchanged."
    )
    return (
        "Runs a Python program in your own Linux machine, with an Ubuntu userland, and "
        "returns its output. The machine is yours alone, not the operator's computer.\n\n"
        "Use it for a self-contained computation: analyzing data, math, parsing or "
        "converting a file, generating a chart. To run a project's own code, its tests "
        "or a script it ships, use the shell, because this tool cannot see `~/projects` "
        "at all; copy what a program needs into `~/scratch` first if you want it here.\n\n"
        "The working directory is `/work`, and it persists across calls in this "
        "conversation: files you write and packages you install stay, so you can run "
        "something, hit an error, fix it and run again without starting over. Keep "
        "anything that matters there. `/tmp` is a RAM disk, so what you put there counts "
        "against your memory cap; unpack or build anything sizeable under `/work`. You "
        "are not root, so the system directories stay as the OS left them, and the few "
        "other writable paths are thrown away with the machine. After a long idle "
        "stretch the machine is reclaimed: your files are restored, but installed "
        "packages may need reinstalling.\n\n"
        f"{files}: use them to read, write, edit, search and list files, run commands "
        "and install packages, and use this tool to run Python. A relative path in those "
        "tools lands in the conversation's own place, which in a project is the project "
        "rather than `~/scratch`.\n\n"
        "The package registries and GitHub are reachable already, so fetching from them "
        f"needs no permission. {ask} Install packages with `pip install` from the shell; "
        "they land under `/work/.local`, and a package's command lands in "
        "`/work/.local/bin`, which is not on your `PATH`, so call it by that full path or "
        "as `python -m <pkg>`. System package managers are not an option: `apt` and the "
        "like need root, so reach for the language-level package instead.\n\n"
        f"Each run is capped at {settings.sandbox_memory} of memory, "
        f"{settings.sandbox_cpus} CPU and {settings.sandbox_pids_limit} processes and "
        "threads. Exceeding memory kills the run; exceeding the CPU cap only slows it, "
        "which can then hit the timeout; exceeding the process cap fails the next fork "
        "or thread inside the run. `stdout` and `stderr` come back whole, so print what "
        "you need to read, and write bulky output to a file under `/work` to read back "
        "in slices.\n\n"
        "Returns `ok`, `exit_code`, `stdout`, `stderr` and `timed_out`, and `notes` from "
        "copying your changes back. On failure it "
        "adds an `error` hint naming the cap that was likely hit, if any; read `stderr` "
        "for the cause, fix the code, and run it again."
    )


def code_toolset() -> FunctionToolset[RunDeps]:
    settings = get_settings()
    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    # Which wording a run gets depends on who is running it, known only per call.
    described = {
        False: _execute_description(settings),
        True: _execute_description(settings, delegated=True),
    }

    async def _worded_for_who_is_running(
        ctx: RunContext[RunDeps], tool_def: ToolDefinition
    ) -> ToolDefinition:
        """Describe this tool in terms of the tools the run actually has."""
        return replace(tool_def, description=described[ctx.deps.delegated_approved])

    # `code_arg_name` renders the argument as code and keeps the tool out of `run_code`
    # (`agent/code_mode.py`), where a script passing a script would nest two code surfaces.
    @toolset.tool(
        description=described[False],
        prepare=_worded_for_who_is_running,
        metadata={"code_arg_name": "code", "code_arg_language": "python"},
    )
    async def execute(
        ctx: RunContext[RunDeps],
        code: str,
        stdin: str | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, object]:
        """Run a Python program in the sandbox.

        Args:
            code: The whole Python program, run with `python -c` in `/work`.
            stdin: Text fed to the program on standard input.
            timeout_s: Seconds before the run is killed. Default 30; raise it for a long
                computation rather than splitting the work.
        """
        # The description is `_execute_description`; this docstring supplies only the
        # parameter text.
        sessions = ctx.deps.caps.get_optional(SandboxSessionManager)
        if sessions is None:
            return {
                "ok": False,
                "error": "Your computer is unavailable right now: no runtime is "
                + "configured. Computation that would require running code cannot "
                + "be done, and will not run on the operator's host.",
            }
        spec = SandboxSpec(
            command=["python", "-c", code],
            stdin=stdin,
            timeout_s=timeout_s,
        )
        try:
            session = await sessions.acquire(ctx.deps.scratch_key, holder=ctx.deps.run)
            # Announce a cold start so the wait reads as the environment starting, not the
            # model stalling.
            if ctx.tool_call_id and not session.is_warm:
                partial = (
                    "Starting the platform VM, this first run can take a few minutes…"
                    if sessions.vm_starting
                    else "Starting the sandbox environment…"
                )
                _ = await ctx.emit(
                    RunEventEmitted(
                        body=ToolProgress(tool_call_id=ctx.tool_call_id, partial=partial)
                    )
                )
            result = await session.run(spec)
        except SandboxError as exc:
            error = render_tool_error(
                "the code did not run",
                cause=str(exc),
                hints=[exc.hint] if exc.hint else [],
            )
            return {"ok": False, "error": error}
        return exec_result(result, settings, sandboxed=True, delegated=ctx.deps.delegated_approved)

    async def _only_where_there_is_someone_to_ask(
        ctx: RunContext[RunDeps], tool_def: ToolDefinition
    ) -> ToolDefinition | None:
        """Withhold a tool that has to ask from a delegated run.

        A child run has nobody to ask and no output type for a deferred call, so the
        question would end it; it reports what it could not do instead.
        """
        return None if ctx.deps.delegated_approved else tool_def

    @toolset.tool(requires_approval=True, prepare=_only_where_there_is_someone_to_ask)
    async def request_egress(
        ctx: RunContext[RunDeps], domains: list[str], reason: str
    ) -> dict[str, object]:
        """Asks the operator to let your machine reach hosts it cannot reach yet: a
        package index, an API, a site to download from. Call it after a run or command
        fails on a refused host, or before one you know will need it.

        The package registries and GitHub are allowed already, so you need this only for
        somewhere else. Ask for every host the task needs in one call rather than one per
        failure, since each call is a separate approval.

        Once a domain is approved it stays reachable from your machine for the rest of
        this conversation, so run the failed code or command again rather than asking a
        second time. A later call naming a different host asks again. This widens your
        machine only: a command run on the operator's host reaches what their
        installation allows, whatever you ask for here.

        Returns `allowed`, every domain your machine may now reach.

        Args:
            domains: The hosts, as domains (`api.example.com`). A domain matches that host
                exactly; `*.example.com` covers its subdomains. A URL is reduced to its
                host.
            reason: What you are fetching from these hosts and what it is for.
        """
        del reason  # The approval card reads it from the call's arguments.
        policy = ctx.deps.caps.get_optional(EgressPolicy)
        if policy is None:
            return {
                "ok": False,
                "error": "Egress cannot be widened right now: no allowlist is "
                + "configured. Work with the hosts you can already reach.",
            }
        try:
            allowed = await policy.allow(ctx.deps.scratch_key, domains)
        except InvalidInputError as exc:
            # Approval precedes parsing, so a malformed domain must come back as something
            # to fix rather than fail the turn.
            return {"ok": False, "error": str(exc)}
        return {"allowed": sorted(allowed)}

    @toolset.tool(
        requires_approval=True,
        prepare=_only_where_there_is_someone_to_ask,
        metadata={"code_arg_name": "command", "code_arg_language": "shell"},
    )
    async def run_host_command(
        ctx: RunContext[RunDeps],
        command: str,
        explanation: str,
        timeout_s: float = 120.0,
    ) -> dict[str, object]:
        """Runs a command on the operator's own computer, not your machine, inside a
        confining fence. Use it only when the host itself has to change or be inspected:
        a system setting, an installed application, a file outside every project. For
        anything your own machine can do, use `shell_run_command`, which cannot touch the
        host. To act inside a project's folder on the host, use `shell_run_command` with
        `reach` set to `host`.

        It starts in a scratch directory of its own, not in your home, so name host paths
        in full. The fence keeps it from the operator's credentials and this
        application's data directory, and gives it no network beyond the domains the
        installation allows. A permission error on one of those paths is the fence doing
        its job: report it rather than working around it.

        Returns `ok`, `exit_code`, `stdout`, `stderr` and `timed_out`, and `fenced`;
        when it is false, `unfenced_reason` says why. A `fence_note` explains a refusal.

        Args:
            command: The command, as you would type it in a terminal on the host.
            explanation: What the command does and what it changes on the operator's
                computer.
            timeout_s: Seconds before the command is killed. Default 120.
        """
        del explanation  # The approval card reads it from the call's arguments.
        home = await home_of(ctx.deps)
        ledger = await ledger_check(ctx.deps, HOST_COMMAND_TOOL, {"command": command}, home, None)
        if ledger is not None and ledger.refusal is not None:
            return {"ok": False, "error": ledger.refusal}
        # The installation-wide allowlist, not this conversation's grants: one proxy serves
        # every confined host process, so a grant here would widen them all and outlive this
        # command (`services/sandbox/fence.confine`).
        confinement = await resolve_confinement(settings)
        # Not the server's own directory, the application's source tree: `cat .env` would
        # read its keys. The home is not on the host, so the permission layer measures a
        # host command's paths against no root.
        try:
            result = await run_on_host(
                command,
                timeout_s=timeout_s,
                confinement=confinement,
                cwd=host_scratch_dir(),
            )
        except HostExecutionError as exc:
            return {"ok": False, "error": f"The host command could not be launched: {exc}"}
        if ledger is not None and not result.timed_out:
            await ledger.record()
        payload = exec_result(result, settings, sandboxed=False)
        payload["fenced"] = confinement.active
        payload["reach"] = "host"
        if not confinement.active:
            payload["unfenced_reason"] = f"the host fence is not applied: {confinement.reason}"
        refusal = host_refusal(fence_active=confinement.active, ok=result.ok, stderr=result.stderr)
        if refusal is not None:
            payload["fence_note"] = refusal.note
        return payload

    return toolset
