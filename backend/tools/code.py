"""Code tools: Python in the agent's own machine, a fenced command on the host, and
widening what the agent's machine may reach.

``code_execute`` runs Python in the thread's scratch (``/work``, which is ``~/scratch``),
contained in the platform VM, so the agent computes freely. Shell commands are the
``shell`` category's, in every thread.

``code_run_host_command`` runs on the real host under the host fence, so its request
must carry a plain-language ``explanation`` the operator can judge without reading the
command.

``code_request_egress`` widens the agent's machine: compute is free inside the box, but
reaching a host the allowlist does not name is an exit, so it is asked for by name and
approved per call.

The execution mechanics live in ``services/sandbox``. When no sandbox runtime is
available ``code_execute`` reports the capability is disabled and the model adapts; it
never silently falls back to the host.
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
from services.vm import DENIED_MARKER

from .deps import RunDeps
from .emit import RunEventEmitted

# Hitting the pids-limit surfaces as a failed fork/thread-create *inside* the process —
# unlike the OOM killer, there is no distinct signal for it, so this is
# a crude heuristic over the OS errors that failure mode typically prints.
_PID_CAP_MARKERS = (
    "resource temporarily unavailable",
    "cannot allocate memory",
    "can't fork",
    "fork failed",
)

# A Python import of a package that isn't installed — the single most common sandbox
# failure, and the one worth deterministic install mechanics at the failure point.
_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")

# The other face of the same refusal. A client that never gets as far as the proxy — or
# one that reports a refused CONNECT as a dead connection — prints a resolution or
# connection failure instead, so both spellings have to key the same hint. Only errors
# naming the *connection* belong here: pip's own "no matching distribution" summary is
# what a misspelled package name prints against a registry it reached perfectly well, and
# answering that with "ask the operator for pypi.org" is a loop rather than a hint.
_NO_NETWORK_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "network is unreachable",
    "could not resolve host",
    "failed to establish a new connection",
    # A refused CONNECT. Most fetches are HTTPS, and there the marker never reaches the
    # client — a tunnel refusal carries no body — so the refusal is legible only in these
    # phrasings: urllib, urllib3 and pip say the first, curl one of the other two.
    "tunnel connection failed",
    "connect tunnel failed",
    "from proxy after connect",
)


def _looks_like_pid_cap(stderr: str) -> bool:
    low = stderr.lower()
    return "fork" in low and any(marker in low for marker in _PID_CAP_MARKERS)


def _looks_like_egress_denied(output: str) -> bool:
    low = output.lower()
    # The marker the workspace's proxy writes into a refusal's body, read from the script
    # that writes it — two spellings of it would be one hint that quietly stopped firing.
    if DENIED_MARKER.lower() in low:
        return True
    return any(marker in low for marker in _NO_NETWORK_MARKERS)


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
    """A short, plain reason for a failed run, naming the actual configured cap that
    was most likely hit — stderr is often empty for a hard kill, so being precise
    matters more here than for an ordinary non-zero exit. For the two recoverable
    sandbox failures (a missing package, a host off the allowlist) it states the exact
    next call to make, because the fix lives in *another call*, not in the code the
    model would otherwise keep mutating."""
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
    if sandboxed and _looks_like_egress_denied(stderr + "\n" + stdout):
        if delegated:
            # A delegate has no egress request to make — it is withheld, because there is
            # nobody in its run to answer one. Sending it after the tool anyway costs it
            # turns on a name that does not resolve.
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


def _exec_result(
    result: SandboxResult, settings: Settings, *, sandboxed: bool, delegated: bool = False
) -> dict[str, object]:
    """Shape an execution result for the model: an explicit success flag, stdout and
    stderr **whole**, and on failure a legible hint naming which configured cap was
    likely hit.

    The output is not trimmed. A blanket cap fired on every run whether or not the
    context was under any pressure, and it cost the model the middle of exactly the
    output it had just asked for; the turn's own context-overflow stop is what catches
    a genuinely pathological run."""
    payload: dict[str, object] = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
    }
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
    return payload


def _execute_description(settings: Settings, *, delegated: bool = False) -> str:
    """The `execute` tool's description, generated fresh per toolset build (an
    explicit `description=` override — see `FunctionToolset.tool`) so it always states
    the sandbox's *actual* configured resource caps rather than a guess the model has
    no way to verify, and is precise enough to self-diagnose a 137/pid-cap failure.

    ``delegated`` swaps the two sentences that name *other* tools. A delegated run is
    composed straight from the categories, so nothing is namespaced there and the tools
    that ask the operator something are withheld — the ordinary wording would send a
    worker calling names that do not resolve.
    """
    files = (
        "Your file tools and shell reach this same working directory as `~/scratch`"
        if delegated
        else "The `files_*` and `shell_*` tools reach this same working directory as "
        + "`~/scratch`"
    )
    ask = (
        "Any other host is refused, and there is nobody here to ask: say in your report "
        + "which host you could not reach, and get on with what you can do without it."
        if delegated
        else "Any other host is refused until you ask for it: call `code_request_egress` "
        + "with the domains you need and why, and once it is approved retry the same code "
        + "unchanged."
    )
    return (
        "Run a Python program on your own computer — a private Linux machine that is "
        "yours alone (it is not the operator's host), with an Ubuntu userland.\n\n"
        "Your working directory is `/work`, and it persists "
        "across calls in this conversation: files you write and packages you install "
        "stay there, so you can run something, hit an error, fix it, and re-run without "
        "starting over. `/tmp` is a RAM disk, so what you put there is charged against "
        "your memory cap below rather than to disk — unpack or build anything sizeable "
        "under your working directory instead. You are not root here, so the system "
        "directories belong to the OS and stay as they are; the few scratch paths "
        "outside your working directory that do accept writes are thrown away with the "
        "machine. Keep anything that matters in your working directory. After a long "
        "stretch of inactivity the machine is reclaimed: your files are kept and "
        "restored, but installed packages may need reinstalling.\n\n"
        f"{files}: use them to read, write, edit, search and list it, to run commands "
        "and to install packages, and use this tool to run Python. A relative path in "
        "those tools lands in the conversation's own place, which in a project is the "
        "project rather than `~/scratch`, and this tool cannot see `~/projects` at all: "
        "work on a project's files with the shell, or copy what the program needs into "
        "`~/scratch` first.\n\n"
        "The package registries and GitHub are already reachable from this tool, so "
        f"fetching from them needs no permission. {ask} Packages install under "
        "`/work/.local` with `pip install` from the shell; a package's command lands in "
        "`/work/.local/bin`, which is not on your `PATH`, so invoke it by that full path "
        "or as `python -m <pkg>`. System package managers are not an option — `apt` and "
        "friends need root, which you do not have here; reach for the language-level "
        "package instead.\n\n"
        f"It is capped at {settings.sandbox_memory} memory, {settings.sandbox_cpus} "
        f"CPU, and {settings.sandbox_pids_limit} processes/threads — exceeding memory "
        "gets the run killed, exceeding the CPU cap only throttles it (the run keeps "
        "going, just slower, and may then also hit the timeout), and exceeding the "
        "process cap fails the next fork/thread-create inside the run. `stdout` and "
        "`stderr` come back whole, so print what you actually need — for output you "
        "intend to work through rather than read, redirect it to a file under `/work` "
        "and read that file back in slices instead of printing it all.\n\n"
        "The result has `ok`, `exit_code`, `stdout`, `stderr`, and `timed_out`; on "
        "failure it adds a short `error` hint naming which cap (if any) was likely "
        "hit. When it fails, read `stderr` for the cause, fix the code, and run "
        "again."
    )


def code_toolset() -> FunctionToolset[RunDeps]:
    # Read once per toolset build (this factory runs fresh per turn, see
    # `tools/toolsets.py`) so the `execute` description below states the sandbox's
    # actual configured caps, not a value baked in at import time.
    settings = get_settings()
    toolset: FunctionToolset[RunDeps] = FunctionToolset()
    # Both wordings, built once: which of them a run is offered depends on who is running
    # it, and that is only known per call.
    described = {
        False: _execute_description(settings),
        True: _execute_description(settings, delegated=True),
    }

    async def _worded_for_who_is_running(
        ctx: RunContext[RunDeps], tool_def: ToolDefinition
    ) -> ToolDefinition:
        """Describe this tool in terms of the tools the run actually has."""
        return replace(tool_def, description=described[ctx.deps.delegated_approved])

    # `code_arg_name` marks a tool whose argument *is* a program. It is what renders that
    # argument as code, and it is what keeps the tool out of `run_code`
    # (`agent/code_mode.py`): a script passing a second script as a string literal is
    # two code surfaces nested, where the model should see them side by side.
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
        # The model-facing description is generated by `_execute_description` above
        # (registered via `description=`, swapped per run by the prepare) so it can
        # interpolate the live config caps — a plain docstring can't. Keep this in sync
        # when the shape changes.
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
            # A cold workspace takes a beat to bring into the VM — longer still while
            # the VM itself is starting (the first boot builds its image). Announce that
            # wait so the run reads as the environment starting, not the model stalling;
            # a warm session runs at once and needs no notice.
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
        return _exec_result(result, settings, sandboxed=True, delegated=ctx.deps.delegated_approved)

    async def _only_where_there_is_someone_to_ask(
        ctx: RunContext[RunDeps], tool_def: ToolDefinition
    ) -> ToolDefinition | None:
        """Withhold a tool that has to ask from a delegated run.

        A delegate has nobody to ask: its work was approved as one act, in a conversation
        that belongs to its parent, and a child run has no output type for a deferred
        call, so the question would end it outright. Withheld, the child says in its
        report which host it could not reach, or which change to the operator's own
        machine it could not make.
        """
        return None if ctx.deps.delegated_approved else tool_def

    @toolset.tool(requires_approval=True, prepare=_only_where_there_is_someone_to_ask)
    async def request_egress(
        ctx: RunContext[RunDeps], domains: list[str], reason: str
    ) -> dict[str, object]:
        """Ask the operator to let your machine reach one or more hosts it cannot reach
        yet — a package index, an API, a site you need to download from.

        The package registries and GitHub are already allowed, so you only need this for
        somewhere else. Name the hosts as domains (``api.example.com``); a domain matches
        that host exactly, and ``*.example.com`` is the form that covers its subdomains.
        A URL is accepted and reduced to its host.

        ``reason`` is shown to the operator, in your words, as the whole of what they
        have to go on — say what you are fetching and what it is for.

        Approval is per request: it is never granted standing, so a later call naming a
        different host asks again. What *is* remembered is the domain — once approved it
        stays reachable from your own machine for the rest of this conversation, so retry
        the code that failed rather than asking a second time. The result lists everything
        you may now reach.

        This widens your machine, not the operator's: a command approved onto their host
        reaches only what their installation already allows, and nothing you ask for here
        changes that.
        """
        del reason  # Read by the approval card from the call's arguments, not by this body.
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
            # The operator has already approved by the time a domain is parsed, so a
            # malformed one must come back as something to fix rather than fail the turn.
            return {"ok": False, "error": str(exc)}
        return {"allowed": sorted(allowed)}

    @toolset.tool(
        requires_approval=True,
        prepare=_only_where_there_is_someone_to_ask,
        metadata={"code_arg_name": "command", "code_arg_language": "shell"},
    )
    async def run_host_command(
        _ctx: RunContext[RunDeps],
        command: str,
        explanation: str,
        timeout_s: float = 120.0,
    ) -> dict[str, object]:
        """Run a command directly on the operator's host machine — their real
        computer, not your own.

        Only for when the host itself must change; prefer your shell for anything that
        does not need the real host. ``explanation`` MUST say what the command does and
        its effect on the host. It does **not** start in your home but in a scratch
        directory of its own, so name host paths in full.

        The command is normally confined: it cannot read the operator's credentials or
        this application's own data directory, and it has no network unless a domain was
        allowlisted. The result says whether the fence was actually applied
        (``confined``), so a permission error on one of those paths is the fence doing its
        job rather than something to work around.
        """
        del explanation  # Read by the approval card from the call's arguments, not by this body.
        # The installation-wide allowlist, not this conversation's grants: one proxy
        # serves every confined process on the host, so a grant folded in here would
        # widen the fence around every other command running at the same moment and
        # outlive this one (`services/sandbox/host.confine`).
        confinement = await resolve_confinement(settings)
        # Not the server process's own directory, which is the application's source tree:
        # a relative path the model wrote would land among this backend's own files, and
        # `cat .env` would read its keys. The home is not on the host at all, which is also
        # why the permission layer measures a host command's paths against no root.
        try:
            result = await run_on_host(
                command,
                timeout_s=timeout_s,
                confinement=confinement,
                cwd=host_scratch_dir(),
            )
        except HostExecutionError as exc:
            return {"ok": False, "error": f"The host command could not be launched: {exc}"}
        payload = _exec_result(result, settings, sandboxed=False)
        # State the fence plainly: the operator approved the command, and whether it ran
        # confined changes what that approval actually permitted.
        payload["confined"] = confinement.active
        if not confinement.active:
            payload["confinement_note"] = f"This ran unconfined on the host ({confinement.reason})."
        return payload

    return toolset
