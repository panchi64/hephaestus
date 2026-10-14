"""HostPicker — a native file/folder chooser on the operator's own machine.

Why this is a backend service at all: a browser cannot produce an absolute host path.
``<input type="file">`` hands over bytes with no location, and the File System Access API
is Chromium-only and still path-less. But some fields need a real path — a project
directory on the operator's own disk, read *where it is*. So the dialog is opened by the
process that runs on their machine, and the chosen path comes back as data.

**Progressive enhancement, never a requirement.** Every surface that uses this also takes
a typed path, and :func:`probe` tells the UI whether to offer the button at all — a
headless host simply doesn't show it. That keeps the platform rule intact (`XC-PORT-1`):
no OS-specific facility is load-bearing for any core function, and the helpers below are
a convenience layer that degrades to typing.

**Agent-unreachable by construction**: imported only by its route and the app wiring,
never by ``tools/`` or ``agent/`` (enforced by
``tests/test_host_surface_guard.py``, which guards ``services/host_open`` the same way).
A model that could open host dialogs — or read back arbitrary paths — would be reaching
outside every approval gate.

Each helper is spawned with a **fixed argv and no shell**, and the path it prints is
treated as data: it is returned to the caller and never interpolated into a command.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import weakref
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

PickMode = Literal["file", "directory"]

# A dialog is a human waiting, so the budget is generous — but bounded, because a dialog
# nobody ever answers must not hold a slot forever.
_DIALOG_TIMEOUT_S = 300.0

# Only one dialog at a time. Two native choosers racing for focus is a worse experience
# than a queue, and a wedged one would otherwise stack up invisibly.
#
# Created per running loop rather than at import: an `asyncio.Lock()` built at module
# scope binds to whichever loop first *contends* on it, and every later loop that
# contends raises. That is not hypothetical here — each test case runs its own loop,
# and so does a reloading dev server.
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _dialog_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


class PickerAvailability(BaseModel):
    """Whether this host can open a native chooser, and what it would use."""

    available: bool
    tool: str | None = None
    reason: str | None = None  # set when unavailable, phrased for the operator


@dataclass(frozen=True)
class _Helper:
    """One host's dialog program and how to drive it."""

    name: str
    kind: Literal["osascript", "zenity", "kdialog", "powershell"]


def _resolve() -> _Helper | None:
    """The dialog program available here, or ``None``.

    Ordered by how native the result looks on each platform. macOS always has
    ``osascript``; a Linux desktop usually has one of the GTK/Qt choosers, while a server
    has none — which is the case that degrades.
    """
    system = platform.system()
    if system == "Darwin":
        if shutil.which("osascript"):
            return _Helper("osascript", "osascript")
        return None
    if system == "Windows":
        for name in ("pwsh", "powershell"):
            if shutil.which(name):
                return _Helper(name, "powershell")
        return None
    candidates: tuple[tuple[str, Literal["zenity", "kdialog"]], ...] = (
        ("zenity", "zenity"),
        ("kdialog", "kdialog"),
        ("qarma", "zenity"),
    )
    for name, kind in candidates:
        if shutil.which(name):
            return _Helper(name, kind)
    return None


def probe() -> PickerAvailability:
    """Whether to offer a BROWSE control. Cheap (``shutil.which``), so a surface can ask
    on load without caching."""
    helper = _resolve()
    if helper is not None:
        return PickerAvailability(available=True, tool=helper.name)
    if platform.system() == "Darwin":
        reason = "This machine has no osascript, so a native chooser can't be opened."
    elif platform.system() == "Windows":
        reason = "PowerShell wasn't found, so a native chooser can't be opened."
    else:
        reason = (
            "No desktop file chooser was found (zenity or kdialog). Type the path "
            "instead, or install one."
        )
    return PickerAvailability(available=False, reason=reason)


async def pick(
    mode: PickMode,
    *,
    title: str = "Choose",
    start_dir: str | None = None,
    extensions: list[str] | None = None,
) -> str | None:
    """Open a native chooser and return the absolute path, or ``None`` if the operator
    cancelled. Raises ``RuntimeError`` when no chooser is available — callers check
    :func:`probe` first, so that is a race, not the normal path.

    ``extensions`` (bare, e.g. ``["gguf"]``) filters a file dialog where the helper
    supports it; it is a convenience, never the validation — the adapter re-checks the
    artifact regardless of how the path was chosen.
    """
    helper = _resolve()
    if helper is None:
        raise RuntimeError(probe().reason or "no native file chooser on this host")
    argv = _build_argv(helper, mode, title, start_dir, extensions)
    async with _dialog_lock():
        path = await _run(argv)
    if not path:
        return None
    return path


def _build_argv(
    helper: _Helper,
    mode: PickMode,
    title: str,
    start_dir: str | None,
    extensions: list[str] | None,
) -> list[str]:
    directory = mode == "directory"
    if helper.kind == "osascript":
        # AppleScript returns an alias; `POSIX path of` renders it as a plain path. The
        # title is the only interpolation and it is quoted — see `_applescript_string`.
        chooser = "choose folder" if directory else "choose file"
        script = f"POSIX path of ({chooser} with prompt {_applescript_string(title)}"
        if start_dir:
            script += f" default location POSIX file {_applescript_string(start_dir)}"
        if not directory and extensions:
            types = ", ".join(_applescript_string(e.lstrip(".")) for e in extensions)
            script += f" of type {{{types}}}"
        script += ")"
        return [helper.name, "-e", script]
    if helper.kind == "zenity":
        argv = [helper.name, "--file-selection", f"--title={title}"]
        if directory:
            argv.append("--directory")
        if start_dir:
            # A trailing separator is how zenity is told to open *in* the directory
            # rather than preselect it as an entry.
            argv.append(f"--filename={start_dir.rstrip('/')}/")
        if not directory and extensions:
            patterns = " ".join(f"*.{e.lstrip('.')}" for e in extensions)
            argv.append(f"--file-filter={patterns}")
        return argv
    if helper.kind == "kdialog":
        flag = "--getexistingdirectory" if directory else "--getopenfilename"
        argv = [helper.name, flag, start_dir or "."]
        if not directory and extensions:
            argv.append(" ".join(f"*.{e.lstrip('.')}" for e in extensions))
        argv += ["--title", title]
        return argv
    # PowerShell. WinForms dialogs are the built-in ones; the script writes the selected
    # path to stdout and nothing on cancel, matching the other helpers' contract.
    if directory:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            f"$d.Description = {_powershell_string(title)};"
            + (f"$d.SelectedPath = {_powershell_string(start_dir)};" if start_dir else "")
            + "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"
        )
    else:
        filt = (
            "|".join(f"{e.lstrip('.')} files|*.{e.lstrip('.')}" for e in (extensions or []))
            or "All files|*.*"
        )
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.OpenFileDialog;"
            f"$d.Title = {_powershell_string(title)};"
            f"$d.Filter = {_powershell_string(filt)};"
            + (f"$d.InitialDirectory = {_powershell_string(start_dir)};" if start_dir else "")
            + "if ($d.ShowDialog() -eq 'OK') { $d.FileName }"
        )
    return [helper.name, "-NoProfile", "-NonInteractive", "-Command", script]


def _applescript_string(value: str) -> str:
    """An AppleScript string literal. Backslashes and quotes are the only metacharacters
    inside one, and the title/start dir are the only values we ever put there."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _powershell_string(value: str) -> str:
    """A PowerShell single-quoted literal — no expansion happens inside one, so doubling
    the quote is the whole escape."""
    return "'" + value.replace("'", "''") + "'"


# The exit code every one of these helpers uses for "the operator closed the dialog".
# Anything else is a real failure and must not be reported as a cancellation — a chooser
# that can't open would otherwise leave a BROWSE button that silently does nothing.
_CANCEL_EXIT = 1
# osascript's cancel is an *error* (-128) carried in its message, not a distinct code.
_OSASCRIPT_CANCEL = "-128"


async def _run(argv: list[str]) -> str | None:
    """Run a chooser and return the path it printed, or ``None`` when the operator
    cancelled. Raises ``RuntimeError`` when the chooser itself failed, so the caller can
    say *why* instead of showing a button that appears to do nothing.

    The distinction matters most on a macOS host with no GUI session (SSH, launchd):
    ``osascript`` is on PATH so the picker advertises itself as available, but the dialog
    can't be shown. That has to surface as an error, not a silent no-op."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(f"could not open a file chooser: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DIALOG_TIMEOUT_S)
    except TimeoutError:
        # An unanswered dialog must not hold the lock (or a process) indefinitely.
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        logger.info("host picker: the file chooser timed out with no selection")
        return None
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        if proc.returncode == _CANCEL_EXIT and _OSASCRIPT_CANCEL not in detail:
            return None  # the ordinary "closed the dialog" path
        if _OSASCRIPT_CANCEL in detail:
            return None  # osascript spells cancellation as error -128
        logger.warning(
            "host picker: the file chooser failed (exit %s): %s", proc.returncode, detail
        )
        raise RuntimeError(f"the file chooser couldn't be opened{f': {detail}' if detail else ''}")
    return stdout.decode(errors="replace").strip() or None
