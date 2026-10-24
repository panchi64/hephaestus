"""Code tools: Python in the conversation's VM, and widening what that VM may reach.

``code_execute`` runs Python in the thread's scratch (``~/scratch``, also ``/work``)
through the run's workspace, so it runs in the conversation's VM. Shell commands,
including the one fenced escape to the host, belong to the ``shell`` category.

``code_request_egress`` is asked per call: reaching a host the allowlist does not name is
an exit from the box. A grant restarts the VM on its next command, with the host allowed.

Without a VM ``code_execute`` reports the capability unavailable and never falls back to
the host.
"""

from __future__ import annotations

import re
import secrets
from contextlib import suppress
from dataclasses import replace

from pydantic_ai import FunctionToolset, RunContext, ToolDefinition
from pydantic_ai.workspaces import WorkspaceError, WorkspaceTimeoutError

from core.config import Settings, get_settings
from core.diagnostics import render_tool_error
from core.exceptions import InvalidInputError
from runs import ToolProgress
from services.egress import EgressPolicy
from services.home.paths import SCRATCH
from services.sandbox import SandboxResult
from services.sandbox.refusals import is_egress_denied, vm_refusal
from services.vm import ConversationMachines

from .deps import RunDeps
from .emit import RunEventEmitted
from .home_workspace import home_workspace

_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")

#: Where a run's program and stdin are written for the VM to read. Files rather than the
#: environment, which Linux caps at 128 KiB per variable.
_RUN_DIR = f"{SCRATCH.path}/.hephaestus"


def _run_python(token: str, *, has_stdin: bool) -> str:
    stdin = f"{_RUN_DIR}/{token}.stdin" if has_stdin else "/dev/null"
    return f"cd {SCRATCH.path} && python3 {_RUN_DIR}/{token}.py < {stdin}"


def _failure_hint(
    exit_code: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    *,
    memory_mb: int,
    sandboxed: bool,
    delegated: bool = False,
) -> str:
    """A short reason for a failed run, naming the cap most likely hit (stderr is often
    empty for a hard kill). A missing package or a refused host gets the exact next call,
    since the fix is another call, not different code."""
    if timed_out:
        return "It exceeded the time limit and was killed; reduce the work or raise timeout_s."
    if exit_code == 137:  # SIGKILL
        return (
            f"The process was killed (exit 137, SIGKILL) — most likely the VM's "
            f"{memory_mb} MB of memory ran out; reduce memory use or process data in "
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
            f"The Python package providing `{module}` is not installed in your VM. "
            f"Install it first with the shell — `pip install {module}` (or the PyPI "
            "package that provides that module) — then re-run this code unchanged."
        )
    if sandboxed and is_egress_denied(stderr + "\n" + stdout):
        if delegated:
            # `code_request_egress` is withheld from a delegate; naming it would cost turns.
            return (
                "That host is not on your egress allowlist, so the connection never left "
                "your VM — the code itself is fine. You cannot widen it from here: name the "
                "host in your report and let the agent that delegated to you ask for it."
            )
        return (
            "That host is not on your egress allowlist, so the connection never left your "
            "VM — the code itself is fine. Call `code_request_egress(domains=[...], "
            "reason=...)` naming the hosts you need and why, and once it is approved retry "
            "this same code unchanged."
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
    result: SandboxResult, *, sandboxed: bool, delegated: bool = False
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
            memory_mb=get_settings().sandbox_memory_mb,
            sandboxed=sandboxed,
            delegated=delegated,
        )
        refusal = (
            vm_refusal(ok=False, stdout=result.stdout, stderr=result.stderr) if sandboxed else None
        )
        if refusal is not None and refusal.denied_host is not None:
            payload["denied_host"] = refusal.denied_host
            payload["fence_note"] = refusal.note
            payload["fenced"] = True
            payload["reach"] = "network"
    return payload


def _execute_description(settings: Settings, *, delegated: bool = False) -> str:
    """The `execute` tool's description, stating the VM's size.

    ``delegated`` swaps the two sentences that name other tools: a delegated run's tools
    are not namespaced and the ones that ask the operator are withheld.
    """
    files = (
        "Your file tools and shell reach the same folders"
        if delegated
        else "The `files_*` and `shell_*` tools reach the same folders"
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
        "Runs a Python program in your own Linux VM, with an Ubuntu userland, and returns "
        "its output. The VM is yours alone, not the operator's computer.\n\n"
        "Use it for a self-contained computation: analyzing data, math, parsing or "
        "converting a file, generating a chart. To run a project's own code, its tests or "
        "a script it ships, use the shell, which starts in the conversation's own place.\n\n"
        "The program runs in `~/scratch`, which persists across calls in this "
        "conversation: files you write and packages you install stay, so you can run "
        "something, hit an error, fix it and run again without starting over. Keep "
        "anything that matters there; the rest of the VM is thrown away when it is "
        "reclaimed after a long idle stretch or restarted with new access.\n\n"
        f"{files}: use them to read, write, edit, search and list files, run commands "
        "and install packages, and use this tool to run Python.\n\n"
        "The package registries and GitHub are reachable already, so fetching from them "
        f"needs no permission. {ask} Install packages with `pip install` from the shell; "
        "they land under `~/scratch/.local`, so they survive the VM being reclaimed.\n\n"
        f"The VM has {settings.sandbox_cpus} CPUs and {settings.sandbox_memory_mb} MB of "
        "memory. `stdout` and `stderr` come back whole, so print what you need to read, "
        "and write bulky output to a file in `~/scratch` to read back in slices.\n\n"
        "Returns `ok`, `exit_code`, `stdout`, `stderr` and `timed_out`. On failure it adds "
        "an `error` hint naming the cause most likely hit; read `stderr` for the cause, fix "
        "the code, and run it again."
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
        """Run a Python program in the VM.

        Args:
            code: The whole Python program, run with `python -c` in `~/scratch`.
            stdin: Text fed to the program on standard input.
            timeout_s: Seconds before the run is killed. Default 30; raise it for a long
                computation rather than splitting the work.
        """
        # The description is `_execute_description`; this docstring supplies only the
        # parameter text.
        machines = ctx.deps.caps.get_optional(ConversationMachines)
        if machines is None:
            return {
                "ok": False,
                "error": "Your computer is unavailable right now: no VM is configured. "
                + "Computation that would require running code cannot be done, and will "
                + "not run on the operator's host.",
            }
        # Announce a cold start so the wait reads as the VM starting, not the model stalling.
        if ctx.tool_call_id and not machines.running(ctx.deps.workspace_key):
            _ = await ctx.emit(
                RunEventEmitted(
                    body=ToolProgress(tool_call_id=ctx.tool_call_id, partial="Starting your VM…")
                )
            )
        workspace = home_workspace(ctx.deps)
        token = secrets.token_hex(8)
        files = [(f"{_RUN_DIR}/{token}.py", code)]
        if stdin is not None:
            files.append((f"{_RUN_DIR}/{token}.stdin", stdin))
        command = _run_python(token, has_stdin=stdin is not None)
        try:
            await workspace.make_dir(_RUN_DIR)
            for path, text in files:
                await workspace.write_bytes(path, text.encode())
            done = await workspace.run(command, shell=True, timeout=timeout_s)
            result = SandboxResult(done.exit_code, done.stdout, done.stderr)
        except WorkspaceTimeoutError as exc:
            result = SandboxResult(124, exc.stdout, exc.stderr, timed_out=True)
        except WorkspaceError as exc:
            return {"ok": False, "error": render_tool_error("the code did not run", cause=str(exc))}
        finally:
            for path, _ in files:
                with suppress(WorkspaceError, OSError):
                    await workspace.remove(path)
        return exec_result(result, sandboxed=True, delegated=ctx.deps.delegated_approved)

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
        """Asks the operator to let your VM reach hosts it cannot reach yet: a package
        index, an API, a site to download from. Call it after a run or command fails on a
        refused host, or before one you know will need it.

        The package registries and GitHub are allowed already, so you need this only for
        somewhere else. Ask for every host the task needs in one call rather than one per
        failure, since each call is a separate approval.

        Once a domain is approved it stays reachable from your VM for the rest of this
        conversation, so run the failed code or command again rather than asking a second
        time. Your VM restarts with the new access on its next command, which ends any
        background command it was running. A later call naming a different host asks
        again. This widens your VM only: a command run on the operator's host reaches what
        their installation allows, whatever you ask for here.

        Returns `allowed`, every domain your VM may now reach.

        Args:
            domains: The hosts, as domains (`api.example.com`). In your VM a domain also
                covers its subdomains. A URL is reduced to its host.
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

    return toolset
