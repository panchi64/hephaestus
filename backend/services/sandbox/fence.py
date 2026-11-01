"""The OS-level fence for commands that run on the operator's machine rather than in the VM.

One path executes there, inside :func:`confine`: ``shell_run_on_host``, the
approval-gated exception for when the host itself must change.

The fence is ``sandbox-runtime`` (seatbelt on macOS, bubblewrap on Linux). Writes are
deny-by-default, and the deny floor in :mod:`services.sandbox.floor` (shell rc files, launch
agents, ``~/.claude``, git hooks and config; credentials, browser profiles, this
application's dotenv file, database and data directory for reads) applies to every command;
settings lists can add to it and never remove from it. The floor's docstring states which
git paths are covered on which platform.

It reaches only the ``egress_allowed_domains`` allowlist, and ``host_command_deny_read``
is read- and write-denied. On Linux unix sockets are blocked inside the fence, so
ssh-agent and docker are unavailable there.

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
from dataclasses import dataclass
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
    doesn't, and what it lets through."""

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

    Neither the server's working directory (the source tree, with its `.env`) nor the run's
    workspace (inside the fence-denied ``data_dir``) is usable, so host commands get a
    stable scratch directory under the OS temp root, which the fence allows writing to.

    A stable, predictable path under a shared root is squattable, so it is claimed rather
    than merely created (:func:`_claim_scratch`). It matters on Linux, where
    ``gettempdir()`` is the world-writable ``/tmp``: the directory is both where every
    approved command starts and an entry in the fence's write allowlist.
    """
    scratch = scratch_path()
    _claim_scratch(scratch)
    return scratch


def scratch_path() -> Path:
    """Where the scratch directory sits, without creating or checking anything.

    Split from the claim so a squatted directory refuses the command that would run there
    instead of taking down confinement resolution for every later command.
    """
    return Path(tempfile.gettempdir()) / f"hephaestus-host-{os.getuid()}"


def floor_read_paths(settings: Settings) -> tuple[str, ...]:
    """The read floor alone: what the host shell may not read.

    The data directory is the entry that matters most: it holds the vault, the sealed
    workspaces and the database, so exposing it would undo at-rest encryption wholesale.
    """
    return read_floor(Path.home(), Path(settings.data_dir), settings.db_url)


def denied_read_paths(settings: Settings) -> tuple[str, ...]:
    """What ``shell_run_on_host`` may not read: the floor, then ``host_command_deny_read``.

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

    ``mkdir(exist_ok=True)`` follows symlinks, so a symlink planted at this path would be
    accepted; :func:`os.lstat` checks what is actually there. The name carries the uid so
    two accounts don't contend for one path.

    A refusal raises instead of falling back: the only alternatives are the source tree and
    the denied data directory.
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
        # Ours but too wide (e.g. created under a permissive umask).
        os.chmod(scratch, 0o700)


async def resolve_confinement(settings: Settings) -> HostConfinement:
    """Configure OS-level confinement for host commands, once per process."""
    global _resolved
    async with _resolve_lock:
        if _resolved is not None:
            return _resolved
        resolved = await configure(settings)
        if not resolved.active:
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
    # Writes are deny-by-default. The runtime's defaults (`/dev/null`, `/dev/stdout`, the
    # tty) come first: without them even `echo` into a pipe fails.
    # `gettempdir()` not `/tmp`: macOS uses a private `TMPDIR` under `/var/folders`.
    # The scratch path is named, not claimed: `host_scratch_dir` claims it on the call that
    # runs there, so a squatted path refuses one command rather than leaving all unfenced.
    allow_write = [
        *get_default_write_paths(),
        *settings.host_command_allow_write,
        tempfile.gettempdir(),
        str(scratch_path()),
    ]
    # Function-local: `services.egress` reads `safe_key` out of this package.
    from services.egress import normalise_domain

    # Normalised like an approved domain, so `Files.PythonHosted.org` or a pasted URL
    # means the same at both fences.
    domains = [normalise_domain(d) for d in settings.egress_allowed_domains]
    # The runtime installs its own SIGINT/SIGTERM handlers on first use, which would
    # replace the server's and leave Ctrl+C unable to stop it.
    saved_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        await SandboxManager.initialize(
            SandboxRuntimeConfig(
                # The installation-wide egress allowlist, with no per-conversation grants.
                # `allow_local_binding` is read off this global config at wrap time, so a
                # per-call config cannot supply it; it is all-or-nothing (one
                # `localhost:*` rule), hence the loopback residual in the module docstring.
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
    # The same values the global init was given, so per-command `confine` cannot drift.
    return HostConfinement(
        True,
        allowed_domains=tuple(domains),
        allow_write=tuple(allow_write),
        deny_read=tuple(deny_read),
        deny_write=tuple(deny_read),
    )


async def shutdown_confinement() -> None:
    """Tear down whatever `resolve_confinement` started, and forget the decision.

    Configuring starts long-lived proxy listeners inside ``sandbox-runtime`` that otherwise
    outlive the app, accumulating a pair per restart under the reloading dev server.
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

    Write rules are per-call because they go into the wrapper the OS enforces;
    :func:`resolve_confinement` settles only the once-per-process machinery.

    **The network half is coarser, a property of the runtime.** One proxy serves the whole
    process and filters against the configuration installed at initialisation, so
    ``allowed_domains`` decides only whether this command gets a route out at all (an empty
    set gets no proxy and no network). Folding a conversation's grants into that shared
    configuration would widen the fence for every concurrent command and outlive the
    granting one, so on the host only ``egress_allowed_domains`` moves the line.
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
