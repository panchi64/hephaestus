"""Actions that ask the operator at every level, Yolo included, and that no grant or
reviewer can answer.

A guard is a pure reading of one call: :func:`guard` returns the reason the call must be
put to the operator, or None. It looks for four things: a command that deletes a critical
path, a write to a protected path, a git configuration that makes git run code, and a host
command whose fence was wanted but cannot be built. A fence the operator switched off is
their choice and is not a guard, though the card still carries a warning
(:func:`unconfined`).

**A backstop behind the fence, not a replacement for it.** The host fence lets a command
write anywhere under ``~``, so for deleting files this reading is the only line of defence,
and it works on syntax: paths are read off the command's grammar (:mod:`shell_scan`), so a
path built at run time is invisible to it. A command it cannot parse that contains a
deleting word is therefore guarded as a whole. Also outside its reach: ``GIT_DIR=`` and
``--git-dir`` pointing a repository somewhere else, and a ``cd`` inside a subshell leaking
into the line after it.

**Why ``.git`` is limited to hooks, config and the gitlink.** Those are the places git
reads code or settings that run commands. Guarding all of ``.git`` would ask on every
commit's internals.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from core.config import Settings
from services.home.paths import HOME
from services.home.table import RunHome
from services.permissions.guard_paths import Places
from services.permissions.shell_acts import (
    config_write,
    name_of,
    removal_targets,
    write_targets,
)
from services.permissions.shell_ast import ShellCommand, operands
from services.permissions.shell_scan import OPAQUE, scan
from services.sandbox.fence import confinement_state
from services.sandbox.floor import is_protected_write

#: The key a guarded call carries in its approval metadata, and in a parked turn's.
GUARD_KEY = "guard"

FILE_WRITE_TOOLS = frozenset({"files_write_file", "files_edit_file", "files_create_directory"})
SHELL_TOOLS = frozenset({"shell_run_command", "shell_start_command"})
HOST_COMMAND_TOOL = "code_run_host_command"

#: Every tool :func:`guard` has a rule for.
GUARDED_TOOLS = FILE_WRITE_TOOLS | SHELL_TOOLS | {HOST_COMMAND_TOOL}

_GRANT_FREE = "It always asks, whatever the permission level, and no standing approval covers it."
_REMOVER_WORDS = re.compile(
    r"(?<![\w.-])(?:rm|rmdir|unlink|shred)(?![\w-])"
    + r"|\bfind\b[^;|&]*-delete|\bgit\b[^;|&]*\bclean\b"
)
_GLOB = frozenset("*?[")
_HOOKS_PATH = "core.hookspath"


def is_guarded(metadata: Mapping[str, object] | None) -> bool:
    """Whether a parked call's metadata marks it as one only the operator answers, once."""
    return bool(metadata and metadata.get(GUARD_KEY))


def guard(
    tool: str,
    args: Mapping[str, object],
    *,
    home: RunHome | None,
    settings: Settings,
    cwd: str | None = None,
) -> str | None:
    """Why this call must go to the operator whatever the level says, or None.

    ``cwd`` is the shell's working directory as a home path; it defaults to the home's
    default place.
    """
    os_home = Path.home()
    if tool in FILE_WRITE_TOOLS:
        reason = _file_write(
            args.get("path"), home, os_home, directory=tool == "files_create_directory"
        )
        return None if reason is None else f"{reason} {_GRANT_FREE}"
    if tool not in SHELL_TOOLS and tool != HOST_COMMAND_TOOL:
        return None
    host = tool == HOST_COMMAND_TOOL or args.get("reach") == "host"
    command = args.get("command")
    if isinstance(command, str):
        places = Places.at(home, os_home, settings.data_dir, cwd)
        reason = _command_reason(command, places, host=host)
        if reason is not None:
            return f"{reason} {_GRANT_FREE}"
    if host:
        state = confinement_state(settings)
        if state.state == "unavailable":
            return (
                "This command would run on your computer with no confinement: "
                + f"{state.reason}. {_GRANT_FREE}"
            )
    return None


def guard_of_approval(
    tool_name: str,
    parked_metadata: Mapping[str, object] | None,
    override_args: Mapping[str, object] | None,
    *,
    home: RunHome | None,
    settings: Settings,
    cwd: str | None = None,
) -> str | None:
    """The guard reason that applies to an approval, or None when it is an ordinary one.

    Guarded when the parked call was marked guarded, or when the arguments the operator
    approved in its place (``override_args``) are themselves guarded. A guarded approval
    is once-only and may only come from the operator's own session.
    """
    if is_guarded(parked_metadata):
        marked = parked_metadata.get(GUARD_KEY) if parked_metadata else None
        return marked if isinstance(marked, str) else "This call always needs your approval."
    if override_args is not None:
        return guard(tool_name, override_args, home=home, settings=settings, cwd=cwd)
    return None


def unconfined(tool: str, args: Mapping[str, object], *, settings: Settings) -> str | None:
    """The warning a host command's card carries when its fence is not in force."""
    if tool != HOST_COMMAND_TOOL and not (tool in SHELL_TOOLS and args.get("reach") == "host"):
        return None
    state = confinement_state(settings)
    if state.state == "active":
        return None
    return state.reason or state.state.replace("_", " ")


def _command_reason(command: str, start: Places, *, host: bool) -> str | None:
    found = scan(command)
    places = start
    standing = [start]
    for one in found.commands:
        if name_of(one) in ("cd", "pushd"):
            words = operands(one.arguments)
            places = places.after_cd(OPAQUE if words and words[0] == "-" else _first(words))
        else:
            reason = _act_reason(one, places, host)
            if reason is not None:
                return reason
        standing.append(places)
    for destination, after in found.writes:
        why = standing[min(after, len(standing) - 1)].protected(destination, host=host)
        if why is not None:
            return f"This command writes {destination}: {why}."
    for part in found.unreadable:
        if _REMOVER_WORDS.search(part):
            return "This command cannot be fully read and appears to delete files."
    return None


def _act_reason(one: ShellCommand, places: Places, host: bool) -> str | None:
    for target in removal_targets(one):
        if OPAQUE in target:
            if not _known_tail(target):
                return "This command deletes a path that is only known when it runs."
            continue
        what = places.critical(target)
        if what is not None:
            return f"This command deletes {what} ({target}), which cannot be undone."
    for written in write_targets(one):
        why = places.protected(written.path, host=host, kind=written.kind)
        if why is not None:
            return f"This command writes {written.path}: {why}."
    change = config_write(one)
    if change is None:
        return None
    return _config_reason(change.key, change.scope, change.file, places, host)


def _first(words: list[str]) -> str | None:
    return words[0] if words else None


def _known_tail(target: str) -> bool:
    """Whether an unreadable path still ends in a plain name, such as ``$DIR/build``."""
    tail = target.rstrip("/").rsplit("/", 1)[-1]
    return bool(tail) and OPAQUE not in tail and tail not in (".", "..") and not (_GLOB & set(tail))


def _config_reason(
    key: str | None, scope: str, file: str | None, places: Places, host: bool
) -> str | None:
    if key == _HOOKS_PATH:
        return "This command points git at another hooks folder, which can run arbitrary code."
    if scope in ("global", "system") and host:
        return "This command changes the git configuration for every repository on this computer."
    if scope == "file" and file is not None:
        why = places.protected(file, host=host)
        return None if why is None else f"This command writes {file}: {why}."
    return None


def _file_write(raw: object, home: RunHome | None, os_home: Path, *, directory: bool) -> str | None:
    if home is None or not isinstance(raw, str):
        return None
    spellings = [raw]
    if not raw.strip().startswith(("~", "/")):
        spellings.append(f"{home.default.path}/{raw}")
        spellings.append(f"{HOME}/{raw}")
    for spelling in spellings:
        located = home.locate(spelling)
        if located is None:
            continue
        mount, rel = located
        target = mount.root / rel
        why = is_protected_write(target, home=os_home)
        if why is None and not directory and target.name.lower() == ".git":
            why = "a .git file can point git at a directory whose hooks run"
        if why is not None:
            return f"This writes {raw}: {why}."
    return None
