"""The OS-level fence for commands that run on the operator's machine rather than in the VM.

Two paths execute there, and both run inside :func:`confine`: ``code_run_host_command``, the
approval-gated exception for when the host itself must change, and the host shell working in
a project's folder or worktree.

The fence is ``sandbox-runtime`` (seatbelt on macOS, bubblewrap on Linux). Writes are
deny-by-default, and the deny floor in :mod:`services.sandbox.floor` (shell rc files, launch
agents, ``~/.claude``, git hooks and config; credentials, browser profiles, this
application's dotenv file, database and data directory for reads) applies to every command;
settings lists can add to it and never remove from it. The floor's docstring states which
git paths are covered on which platform.

The two callers differ on the network. ``code_run_host_command`` reaches only the
``egress_allowed_domains`` allowlist. The host shell's network is open, because it runs
``brew``, ``git push`` and package installs on the operator's own machine; it is bounded by
what it can write and read, not by where it can connect. It reads less than the hatch:
only the floor is read-denied, so SSH keys, ``gh``, docker and kube credentials stay usable,
while ``host_command_deny_read`` (applied to ``code_run_host_command``) is write-denied for
both. On Linux unix sockets are blocked inside the fence, so ssh-agent and docker are
unavailable there; on macOS seatbelt's open network includes unix sockets.

**Loopback is outside the allowlist, deliberately.** A dev server and its tests are compute,
so the allowlisted profile permits ``localhost``, and with it whatever else listens there,
including the loopback ports this application publishes for its VM services.

**The fence degrades rather than failing closed.** When it is disabled by setting, or its
platform primitive is missing, a command runs unfenced. :func:`confinement_state` reports
which, so the permission layer can make an unfenced host command ask first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import signal
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from core.config import Settings

from .base import HostExecutionError
from .floor import hooks_floor, merged, read_floor, write_floor

logger = logging.getLogger(__name__)

_UNAVAILABLE_FIX = (
    "install the missing dependency (bubblewrap on Linux, ripgrep everywhere) "
    + "or disable the host command fence in Settings"
)


@dataclass(frozen=True)
class HostConfinement:
    """The fence a host command runs under: whether it applies, why not when it
    doesn't, and what it lets through.

    Resolved before the run rather than reported after it, so the tool can tell the
    model — and through it the operator — what actually held, instead of implying a fence
    that was never applied.
    """

    active: bool
    reason: str = ""
    allowed_domains: tuple[str, ...] = ()
    allow_write: tuple[str, ...] = ()
    deny_read: tuple[str, ...] = ()
    deny_write: tuple[str, ...] = ()
    open_network: bool = False
    disabled: bool = False


@dataclass(frozen=True)
class ConfinementState:
    """Where the host fence stands: ``active``, ``disabled_by_setting`` (the operator's
    choice) or ``unavailable`` (wanted but not possible; ``reason`` says how to fix it)."""

    state: Literal["active", "disabled_by_setting", "unavailable"]
    reason: str = ""


# The confinement primitive is a process-global singleton, so it is configured once and
# the outcome cached: the answer cannot change between commands, and re-deciding per call
# would re-run the dependency probe on a hot path.
_resolved: HostConfinement | None = None
# Guards the configure-once: `configure` awaits, so two approvals resolving at the same
# moment would both pass a bare `is None` check and initialize the singleton twice.
_resolve_lock = asyncio.Lock()


def host_scratch_dir() -> Path:
    """The directory an approved host command starts in.

    Neither obvious answer is usable. The server process's own working directory is the
    application's source tree — its `.env`, its config, its database url — so a command
    approved to read "a file here" would be reading those, and every relative path the
    model wrote would mean something in a directory nobody described to it. The run's
    workspace is no better: a sandbox thread's is inside ``data_dir``, which
    the fence below denies outright, so a command started there cannot resolve its own
    working directory, let alone write in it.

    So host commands get a scratch directory of their own: under the OS temp root, which
    the fence allows writing to; holding nothing the operator did not put there through
    this tool; and stable across calls, so one command's output is still there for the
    next.

    **Stable and predictable under a shared root is a squattable pair**, so the path is
    claimed rather than merely created — see :func:`_claim_scratch`. This matters on Linux,
    where ``gettempdir()`` is the world-writable ``/tmp`` (macOS gives each user a private
    ``TMPDIR`` under ``/var/folders``), and it matters twice over: the directory is both
    where every approved command starts *and* an entry in the fence's write allowlist, so
    whoever controls it controls what a relative path in an approved command resolves to.
    """
    scratch = scratch_path()
    _claim_scratch(scratch)
    return scratch


def scratch_path() -> Path:
    """Where the scratch directory sits, without creating or checking anything.

    Split from the claim so a *profile* can name the path without a squatted directory
    taking confinement resolution down with it: the refusal belongs to the command that
    would have run there, not to the process that configures the fence for every later
    command. This module's profile allows writing it (it is where an approved host command
    starts).
    """
    return Path(tempfile.gettempdir()) / f"hephaestus-host-{os.getuid()}"


def floor_read_paths(settings: Settings) -> tuple[str, ...]:
    """The read floor alone: what the host shell may not read.

    The data directory is the entry that matters most: it holds the vault, the sealed
    workspaces and the database, so exposing it would undo at-rest encryption wholesale.
    """
    return read_floor(Path.home(), Path(settings.data_dir), settings.db_url)


def denied_read_paths(settings: Settings) -> tuple[str, ...]:
    """What ``code_run_host_command`` may not read: the floor, then ``host_command_deny_read``.

    The host shell reads less (:func:`floor_read_paths`), so ``git push`` over SSH, ``gh``,
    ``docker`` and ``kubectl`` keep their credentials. Both are write-denied the full list.
    """
    return merged(floor_read_paths(settings), settings.host_command_deny_read)


def _denied_write_paths(deny_read: Iterable[str], allow_write: Iterable[str]) -> list[str]:
    """Everything read-denied is write-denied too, plus the write floor and each writable
    root's ``.git/hooks``."""
    roots = [os.path.expanduser(p) for p in allow_write]
    return list(merged(merged(deny_read, write_floor(Path.home())), hooks_floor(roots)))


def _claim_scratch(scratch: Path) -> None:
    """Make ``scratch`` a directory this user owns and nobody else can read, or refuse it.

    ``mkdir(exist_ok=True)`` is not enough on its own: it swallows the "already there" case
    whenever :meth:`Path.is_dir` is true, and ``is_dir`` follows symlinks — so a symlink
    planted at this path by another local user is silently accepted, and everything below
    lands wherever it points. The name carries the uid so two accounts on one host do not
    contend for the same path in the first place; this then checks what is actually there
    with :func:`os.lstat`, which does not follow the link.

    A refusal raises rather than falling back to some other directory. There is no safe
    second choice — the alternatives are the source tree and the denied data directory —
    and a squatted scratch path is a fact the operator needs to see, not route around.
    """
    try:
        scratch.mkdir(mode=0o700, exist_ok=True)
        info = os.lstat(scratch)
    except OSError as exc:
        raise HostExecutionError(
            f"the host scratch directory {scratch} is unusable: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HostExecutionError(
            f"the host scratch directory {scratch} is a symlink or not a directory — "
            + "refusing to run an approved command in a path someone else redirected"
        )
    if info.st_uid != os.getuid():
        raise HostExecutionError(
            f"the host scratch directory {scratch} is owned by uid {info.st_uid}, "
            + "not this process — refusing to run an approved command in it"
        )
    if info.st_mode & 0o077:
        # Ours, but wider than it should be — a directory this process created under an
        # older, laxer version of this function, or under a permissive umask. Tighten it:
        # the command's output is the operator's, and on a shared host the temp root is not.
        os.chmod(scratch, 0o700)


async def resolve_confinement(settings: Settings) -> HostConfinement:
    """Configure OS-level confinement for host commands, once per process."""
    global _resolved
    async with _resolve_lock:
        if _resolved is not None:
            return _resolved
        resolved = await configure(settings)
        if not resolved.active:
            # Worth one line: an operator who believes their host commands are fenced
            # should not have to read a tool result to discover otherwise.
            logger.warning("host commands are running unconfined: %s", resolved.reason)
        _resolved = resolved
        return resolved


def confinement_state(settings: Settings) -> ConfinementState:
    """The resolved state of the host fence. :func:`resolve_confinement` runs at startup;
    before that, ``settings`` decides between disabled and not-yet-resolved."""
    if _resolved is None:
        if not settings.host_command_sandbox_enabled:
            return ConfinementState("disabled_by_setting", "disabled by configuration")
        return ConfinementState("unavailable", "the host fence has not been resolved yet")
    if _resolved.active:
        return ConfinementState("active")
    if _resolved.disabled:
        return ConfinementState("disabled_by_setting", _resolved.reason)
    return ConfinementState("unavailable", _resolved.reason)


async def shell_confinement(settings: Settings) -> HostConfinement:
    """The fence for the host shell: writes under ``~``, temp and the scratch directory
    plus ``host_shell_allow_write``, and an open network."""
    resolved = await resolve_confinement(settings)
    if not resolved.active:
        return resolved
    # The runtime drops `/tmp` as written (a symlink on macOS), so its real path is named too.
    shared_tmp = ("/tmp", "/var/tmp", os.path.realpath("/tmp"), os.path.realpath("/var/tmp"))
    allow_write = merged(
        ("~", tempfile.gettempdir(), *shared_tmp, str(scratch_path())),
        settings.host_shell_allow_write,
    )
    return replace(
        resolved,
        allow_write=allow_write,
        allowed_domains=(),
        deny_read=floor_read_paths(settings),
        open_network=True,
    )


async def fence_command(command: str, confinement: HostConfinement | None) -> str:
    """``command`` wrapped for ``confinement``, or unchanged when no fence applies."""
    if confinement is None or not confinement.active:
        return command
    try:
        return await confine(
            command,
            allowed_domains=confinement.allowed_domains,
            allow_write=confinement.allow_write,
            deny_read=confinement.deny_read,
            deny_write=confinement.deny_write,
            open_network=confinement.open_network,
        )
    except Exception as exc:  # wrapping must never lose the command
        raise HostExecutionError(f"failed to confine host command: {exc}") from exc


async def configure(settings: Settings) -> HostConfinement:
    if not settings.host_command_sandbox_enabled:
        return HostConfinement(False, "disabled by configuration", disabled=True)
    try:
        from sandbox_runtime import (
            FilesystemConfig,
            NetworkConfig,
            SandboxManager,
            SandboxRuntimeConfig,
            get_default_write_paths,
        )
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        return HostConfinement(
            False, f"sandbox-runtime is not installed ({exc}); {_UNAVAILABLE_FIX}"
        )
    if not SandboxManager.check_dependencies():
        # Name what is actually missing. A generic "unavailable" reads as "your OS can't
        # do this" and gets ignored, when in practice the usual cause is a single absent
        # binary the operator can install in one command — and until they do, every
        # approved host command runs unfenced.
        from sandbox_runtime.utils.platform import get_platform
        from sandbox_runtime.utils.ripgrep import has_ripgrep_sync

        if not SandboxManager.is_supported_platform(get_platform()):
            return HostConfinement(
                False,
                f"{get_platform()} has no supported sandbox primitive; "
                + "disable the host command fence in Settings",
            )
        if not has_ripgrep_sync():
            return HostConfinement(
                False,
                "ripgrep (`rg`) is not installed — sandbox-runtime needs it to resolve "
                + "the filesystem deny rules; install it or disable the host command fence "
                + "in Settings",
            )
        return HostConfinement(
            False, f"the platform's sandbox dependencies are unavailable; {_UNAVAILABLE_FIX}"
        )
    deny_read = list(denied_read_paths(settings))
    # Writes are deny-by-default in this runtime, so the allow list is not a hardening knob
    # — it is what keeps an approved command able to do the thing it was approved for. The
    # runtime's own defaults (`/dev/null`, `/dev/stdout`, the tty) come first: without them
    # even `echo` into a pipe fails, which would read as the tool being broken.
    # `gettempdir()` rather than a literal `/tmp`: macOS gives each user a private
    # `TMPDIR` under `/var/folders`, so a hardcoded path would miss where temp files
    # actually land (`XC-PORT-1`). The scratch directory is under it and named anyway,
    # because it is the one path a host command *has* to be able to write: it is where the
    # command starts. The application's own source tree (`os.getcwd()`) is neither where a
    # command runs nor anywhere one should write.
    # Named, not claimed: `host_scratch_dir` does the claiming, on the call that will
    # actually run there, so a squatted path refuses one command rather than leaving every
    # later one unfenced.
    allow_write = [
        *get_default_write_paths(),
        *settings.host_command_allow_write,
        tempfile.gettempdir(),
        str(scratch_path()),
    ]
    # Imported here rather than at module scope: `services.egress` reads `safe_key` out of
    # this package, so the two only meet at call time.
    from services.egress import normalise_domain

    # Through the same funnel an approved domain goes through, so what an operator wrote
    # in the setting means the same thing at both fences. Left raw, `Files.PythonHosted.org`
    # or a pasted URL would be allowed by the egress gateway and refused here.
    domains = [normalise_domain(d) for d in settings.egress_allowed_domains]
    # The runtime installs its own SIGINT/SIGTERM handlers on first use, which would
    # replace the server's and leave Ctrl+C unable to stop it.
    saved_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        await SandboxManager.initialize(
            SandboxRuntimeConfig(
                # The same allowlist the VM's egress gateway reads. An approved command is
                # still only approved for what the operator read; where it may *reach* is
                # one installation-wide policy, not a second list that drifts from it.
                # `allow_local_binding` because a dev server or a test suite opening a
                # localhost socket is compute, not egress — and it is read off this global
                # config at wrap time, so a per-call config could not supply it. It is
                # all-or-nothing (the profile emits one `localhost:*` rule), which is why
                # the module docstring states the residual rather than the allowlist
                # covering everything a command can reach.
                network=NetworkConfig(allowed_domains=domains, allow_local_binding=True),
                filesystem=FilesystemConfig(
                    deny_read=deny_read,
                    allow_write=allow_write,
                    deny_write=_denied_write_paths(deny_read, allow_write),
                ),
            )
        )
    except Exception as exc:  # any init failure means "not confined"
        logger.warning("sandbox-runtime could not initialize", exc_info=True)
        return HostConfinement(
            False, f"sandbox-runtime could not initialize ({exc}); {_UNAVAILABLE_FIX}"
        )
    finally:
        for sig, handler in saved_handlers.items():
            if handler is not None:
                with contextlib.suppress(ValueError):
                    _ = signal.signal(sig, handler)
    # Carried on the resolution rather than re-derived at the run: `confine` fences each
    # command individually, and these are the settings half of what it needs — the same
    # values the global init above was given, so the two cannot drift.
    return HostConfinement(
        True,
        allowed_domains=tuple(domains),
        allow_write=tuple(allow_write),
        deny_read=tuple(deny_read),
        deny_write=tuple(deny_read),
    )


async def shutdown_confinement() -> None:
    """Tear down whatever `resolve_confinement` started, and forget the decision.

    Configuring the confinement starts long-lived proxy listeners inside
    ``sandbox-runtime``; without this they outlive the app, and under the reloading dev
    server they accumulate a pair per restart. Registered with the app's lifecycle
    registry like every other background unit, rather than left to process exit.
    """
    global _resolved
    if _resolved is None:
        return
    was_active = _resolved.active
    _resolved = None
    if not was_active:
        return  # nothing was ever initialized
    try:
        from sandbox_runtime import SandboxManager

        await SandboxManager.reset()
    except Exception:  # shutdown is best-effort, like every other unit's
        logger.warning("host-command confinement did not shut down cleanly", exc_info=True)


async def confine(
    command: str,
    *,
    allowed_domains: Iterable[str],
    allow_write: Iterable[str],
    deny_read: Iterable[str],
    deny_write: Iterable[str] = (),
    open_network: bool = False,
) -> str:
    """``command`` rewritten to run under the platform's sandbox, fenced for *this* call.

    The write floor and each writable root's ``.git/hooks`` are denied here whatever the
    caller passes. ``open_network`` skips network filtering entirely: the runtime's config
    cannot express "every host" (a bare ``*`` is rejected and an empty list still isolates
    the network), so the platform wrapper is called with network restriction off.

    :func:`resolve_confinement` settles the machinery once per process — the platform
    probe, the proxy listeners — but not where a particular command may write, because
    callers differ: the approved host command writes wherever the operator allowed, and the
    host shell writes into one conversation's project folder. Those
    rules are genuinely per-call, because they go into the wrapper the OS itself enforces.

    **The network half is coarser, and that is a property of the runtime, not a choice
    here.** One proxy serves the whole process and filters every request against the
    configuration installed at initialisation, so ``allowed_domains`` decides only whether
    this command gets a route out *at all* — an empty set gets no proxy and no network.
    Which hosts it may then reach is the installation-wide allowlist, identical for every
    command. Folding a conversation's own grants in would mean writing them into that one
    shared configuration, where they would widen the fence around every other command
    running at that moment — a background dev server started by another thread included —
    and stay there after the granting command exited. That is a per-call approval turned
    into a process-wide standing grant, so the host fence does not honour them: on the
    host only ``egress_allowed_domains`` moves the line.
    """
    from sandbox_runtime import (
        FilesystemConfig,
        NetworkConfig,
        SandboxManager,
        SandboxRuntimeConfig,
    )

    # The runtime exports TMPDIR=/tmp/claude, a directory that may not exist; the real
    # temp root is on the write list.
    command = f"export TMPDIR={shlex.quote(tempfile.gettempdir())}; {command}"
    allowed = list(allow_write)
    denied_read = list(deny_read)
    denied_write = _denied_write_paths(merged(denied_read, deny_write), allowed)
    if open_network:
        return await _wrap_open_network(command, allowed, denied_read, denied_write)
    return await SandboxManager.wrap_with_sandbox(
        command,
        custom_config=SandboxRuntimeConfig(
            network=NetworkConfig(allowed_domains=sorted(set(allowed_domains))),
            filesystem=FilesystemConfig(
                deny_read=denied_read, allow_write=allowed, deny_write=denied_write
            ),
        ),
    )


async def _wrap_open_network(
    command: str, allow_write: list[str], deny_read: list[str], deny_write: list[str]
) -> str:
    from sandbox_runtime import SandboxManager, get_default_write_paths
    from sandbox_runtime.linux_sandbox import LinuxSandboxParams, wrap_command_linux
    from sandbox_runtime.macos_sandbox import MacOSSandboxParams, wrap_command_macos
    from sandbox_runtime.schemas import FsReadRestrictionConfig, FsWriteRestrictionConfig
    from sandbox_runtime.utils.platform import get_platform

    read_config = FsReadRestrictionConfig(deny_only=deny_read)
    write_config = FsWriteRestrictionConfig(
        allow_only=[*get_default_write_paths(), *allow_write], deny_within_allow=deny_write
    )
    platform = get_platform()
    if platform == "macos":
        return wrap_command_macos(
            MacOSSandboxParams(
                command=command,
                needs_network_restriction=False,
                read_config=read_config,
                write_config=write_config,
                ignore_violations=SandboxManager.get_ignore_violations(),
            )
        )
    if platform == "linux":
        return await wrap_command_linux(
            LinuxSandboxParams(
                command=command,
                needs_network_restriction=False,
                read_config=read_config,
                write_config=write_config,
                enable_weaker_nested_sandbox=SandboxManager.get_enable_weaker_nested_sandbox(),
                ripgrep_config=SandboxManager.get_ripgrep_config(),
                mandatory_deny_search_depth=SandboxManager.get_mandatory_deny_search_depth(),
            )
        )
    raise RuntimeError(f"Sandbox configuration is not supported on platform: {platform}")
