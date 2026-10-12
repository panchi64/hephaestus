"""HostOpen — hand a path on the operator's own machine to whatever opens it there.

Why this is a backend service at all: a browser cannot open a local file. ``file://``
navigation is blocked from a page, and it would be the wrong thing even if it weren't —
the operator wants the file in their editor, not rendered in a tab. The process that can
do it is the one already running on their machine, so a click on a path in an answer
becomes a REST call and the OS picks the application.

**Operator-initiated, never agent-initiated.** This is the mirror image of
``services/host_picker``: that one reads a path off the desktop, this one acts on one,
and both are reachable only from a control the operator pressed. The same guard enforces
it (``tests/test_host_surface_guard.py``) — nothing under ``tools/`` or ``agent/`` may
reference either. A model that could launch applications on the host would be acting
outside every approval gate, and here it would do it by *naming a path in its own prose*,
which is the only input this feature takes.

That is why :func:`resolve_within` exists and why the route hands it the operator's own
project roots. The path arrives from a click on model-written text, so it is untrusted
content and containment is half the gate — the same ``contained_path`` every file tool is
fenced by, not a second check written out again here. The other half is
``services/host_open_policy``: the agent *writes into* those roots, so a contained path
can still be a program, and what the host would run rather than show is refused by type.

**Portable by construction** (`XC-PORT-1`): ``open`` on macOS, ``explorer`` on Windows,
``xdg-open`` or ``gio`` elsewhere, and nothing at all on a headless host — where the call
fails with a sentence saying so, because a path shown in prose is still worth reading as
text when nothing can open it.

Each helper is spawned with a **fixed argv and no shell**: the path is one argument and
is never interpolated into a command line.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.exceptions import InvalidInputError, NotFoundError, PermissionDeniedError
from services.host_open_policy import ensure_openable
from services.sandbox.base import SandboxError, contained_path

logger = logging.getLogger(__name__)

# How long to wait on the opener before assuming it did its job. Bounded because a
# request may not hang on a desktop helper; generous because a cold editor launch is
# seconds, not milliseconds.
_LAUNCH_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class _Opener:
    """One host's "open this with whatever handles it" program."""

    name: str
    kind: Literal["open", "explorer", "xdg-open", "gio"]


def _resolve() -> _Opener | None:
    """The opener available here, or ``None`` on a host that has none.

    Deliberately the *desktop's* choice of application rather than ``$EDITOR``: the
    operator's editor is whatever they already registered for that file type, and a
    terminal editor launched detached from a web request would open nowhere at all.
    """
    system = platform.system()
    if system == "Darwin":
        return _Opener("open", "open") if shutil.which("open") else None
    if system == "Windows":
        # `explorer <path>` is the shell's own "open with the registered handler", and it
        # is always present — unlike a chooser, there is no fallback to look for.
        return _Opener("explorer", "explorer") if shutil.which("explorer") else None
    for name, kind in (("xdg-open", "xdg-open"), ("gio", "gio")):
        if shutil.which(name):
            return _Opener(name, kind)  # type: ignore[arg-type]
    return None


def _unavailable_reason() -> str:
    """Why this host can't open a file, phrased for the operator."""
    if platform.system() in ("Darwin", "Windows"):
        return "This machine has no program to open files with."
    return (
        "No desktop opener was found (xdg-open or gio), so this machine can't open the "
        "file for you."
    )


def resolve_within(roots: Sequence[Path], path: str) -> Path:
    """The file ``path`` names inside one of ``roots`` **and** may be handed to the host,
    or a refusal.

    ``path`` is whatever the model wrote into its answer, so both spellings it uses have
    to work and neither may be trusted: the absolute path a worktree thread is told its files
    are called, and the workspace-relative one its file tools take. Both go through the
    sandbox's own containment check, which is what makes one loop enough: ``root /
    "/etc/passwd"`` *is* ``/etc/passwd`` and ``root / "../../.ssh/id_rsa"`` resolves out
    of the tree, so the function every file tool is already fenced by refuses both —
    rather than a second check written out here and drifting from the first. It resolves
    symlinks on the way, so a ``notes.md`` inside a root pointing at ``~/.ssh/id_rsa``
    lands outside the tree and is refused like any other escape.

    The first root that both contains the path and has something at it wins, which makes
    the caller's ordering the policy rather than a rule buried here: the route puts the
    active project first, and each project's worktree ahead of the operator's own
    checkout, so a file the agent just wrote opens as the copy it actually wrote.

    The winner is then gated on *type* — see ``host_open_policy`` — because the agent
    writes into these roots, so containment says nothing about whether the thing at the
    end is read or executed. It runs on the resolved path, so a harmless-looking symlink
    is judged as whatever it actually points at.

    Refusals are three answers on purpose. Contained but absent is a stale path in an old
    answer (**404**); contained by nothing is a path outside the operator's projects, and
    contained but executable is a path inside them that would run — both **403**, because
    both are this function refusing rather than failing to find.
    """
    wanted = path.strip()
    if not wanted:
        raise InvalidInputError("no path was given to open")
    contained_anywhere = False
    for root in roots:
        try:
            target = contained_path(root, wanted, what="path")
        except SandboxError:
            continue
        contained_anywhere = True
        if target.exists():
            ensure_openable(target)
            return target
    if contained_anywhere:
        raise NotFoundError(f"nothing is at that path any more: {wanted}")
    raise PermissionDeniedError(
        f"{wanted} isn't inside any of your projects, so it can't be opened from here"
    )


async def open_path(target: Path) -> None:
    """Open ``target`` in whatever this host has registered for it.

    Raises ``RuntimeError`` when there is no opener or the opener itself failed — the
    caller says so in words, because a control that silently does nothing is worse than
    one that explains why it couldn't.
    """
    opener = _resolve()
    if opener is None:
        raise RuntimeError(_unavailable_reason())
    # `gio` is the only one whose verb is a subcommand rather than the program itself.
    verb = ["open"] if opener.kind == "gio" else []
    argv = [opener.name, *verb, str(target)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise RuntimeError(f"could not open {target.name}: {exc}") from exc
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=_LAUNCH_TIMEOUT_S)
    except TimeoutError:
        # Still running is a perfectly ordinary outcome: some handlers stay attached to
        # the application they launched. The file is open; there is nothing to wait for,
        # and killing the process here could take the operator's editor down with it.
        logger.debug("host open: %s is still attached to the application it launched", argv[0])
        _reap_later(proc)
        return
    if code != 0 and opener.kind != "explorer":
        # `explorer` reports a nonzero exit on a perfectly successful open, so its code
        # carries no information and is not read as a failure.
        raise RuntimeError(_failure_reason(opener, code, target))


#: Waits still outliving the request that started them. Held so the tasks are not
#: garbage-collected mid-flight, which is the documented way to keep a fire-and-forget
#: task alive; each one drops itself when the application it is attached to exits.
_reapers: set[asyncio.Task[int]] = set()


def _reap_later(proc: asyncio.subprocess.Process) -> None:
    """Keep waiting on an opener that outlived the request, off the request's back.

    Dropping it here instead would leave the child unreaped and the event loop warning
    about a subprocess still running — a tidiness matter, but one that would show up in
    the log of every long-lived editor the operator opens a file in.
    """
    reaper = asyncio.create_task(proc.wait())
    _reapers.add(reaper)
    reaper.add_done_callback(_reapers.discard)


#: `xdg-open`'s documented exit codes. Worth translating: "exit 3" tells the operator
#: nothing, while "no application is registered for it" tells them what to fix.
_XDG_REASONS = {
    2: "it no longer exists",
    3: "no application is registered to open it",
    4: "the application refused to open it",
}


def _failure_reason(opener: _Opener, code: int, target: Path) -> str:
    detail = _XDG_REASONS.get(code) if opener.kind in ("xdg-open", "gio") else None
    tail = detail or f"{opener.name} exited with {code}"
    return f"couldn't open {target.name}: {tail}"
