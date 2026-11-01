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
path built at run time is invisible to it, and a command it cannot parse that contains a
deleting word is guarded as a whole. Outside its reach:

- an interpreter given code to run (``python -c``, ``perl -e``, ``node -e``, ``ruby -e``);
- ``GIT_DIR=`` and ``--git-dir`` pointing a repository somewhere else;
- ``ssh`` and other programs that run a command elsewhere;
- a program that deletes or writes on its own account (``make clean``, a script);
- the VM copying content into a repository's ``.git/hooks``, and ``tar -C .git`` on the host;
- ``git config --edit``, which opens an editor on configuration.

On the host a pipeline or here-string that feeds a shell a script this cannot read asks,
and so does ``osascript``, which is a direct line to the operator's machine.

**Why ``.git`` is limited to hooks, config and the gitlink for writes.** Those are the
places git reads code or settings that run commands. A repository's ``.git`` folder is also
critical to delete.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from pathlib import Path

from core.config import Settings
from services.home.table import RunHome
from services.permissions.git_config import (
    ConfigWrite,
    config_write,
    executes_code,
    git_overrides,
    is_executing_key,
)
from services.permissions.guard_paths import Places
from services.permissions.shell_acts import (
    GLOB_CHARS,
    Transfer,
    installs_crontab,
    name_of,
    removal_targets,
    transfer,
    write_targets,
)
from services.permissions.shell_ast import OPAQUE, ShellCommand, operands
from services.permissions.shell_scan import scan
from services.sandbox.fence import confinement_state

#: The key a guarded call carries in its approval metadata, and in a parked turn's.
GUARD_KEY = "guard"

FILE_WRITE_TOOLS = frozenset({"files_write_file", "files_edit_file", "files_create_directory"})
#: The file tools that rewrite what a file holds, which need the file read first.
FILE_REWRITE_TOOLS = frozenset({"files_write_file", "files_edit_file"})
SHELL_TOOLS = frozenset({"shell_run_command", "shell_start_command"})
HOST_COMMAND_TOOL = "shell_run_on_host"
#: The tools that run a command, which must not overlap: each is judged against the
#: directory the one before it left the shell in.
COMMAND_TOOLS = SHELL_TOOLS | {HOST_COMMAND_TOOL}

#: Every tool :func:`guard` has a rule for.
GUARDED_TOOLS = FILE_WRITE_TOOLS | COMMAND_TOOLS

_GRANT_FREE = "It always asks, whatever the permission level, and no standing approval covers it."
_REMOVER_WORDS = re.compile(
    r"(?<![\w.-])(?:rm|rmdir|unlink|shred|srm)(?![\w-])"
    + r"|\bfind\b[^;|&]*-delete|\bgit\b[^;|&]*\bclean\b"
)
_UNREADABLE_SCRIPT = (
    "This command runs a script it can't read, which could do anything on your computer."
)


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
    cwd_known: bool = True,
    strict: bool = False,
) -> str | None:
    """Why this call must go to the operator whatever the level says, or None.

    ``cwd`` is the shell's working directory as a home path; it defaults to the home's
    default place, and ``cwd_known`` false says an earlier call in the batch may have moved
    it. ``strict`` then treats every relative removal or move target as critical, for a
    caller that cannot ask. ``shell_run_on_host`` starts in the host's scratch
    directory whatever ``cwd`` is. A command too deeply nested to read asks.
    """
    try:
        return _guard(
            tool, args, home=home, settings=settings, cwd=cwd, cwd_known=cwd_known, strict=strict
        )
    except RecursionError, UnicodeError:
        return f"This command is too complex to check. {_GRANT_FREE}"


def changes_directory(tool: str, args: Mapping[str, object]) -> bool:
    """Whether a shell call moves the shell, so the calls after it start somewhere else."""
    command = args.get("command")
    if tool not in SHELL_TOOLS or not isinstance(command, str):
        return False
    try:
        return scan(command).changed_directory
    except RecursionError, UnicodeError:
        return True


def _guard(
    tool: str,
    args: Mapping[str, object],
    *,
    home: RunHome | None,
    settings: Settings,
    cwd: str | None,
    cwd_known: bool,
    strict: bool,
) -> str | None:
    os_home = Path.home()
    if tool in FILE_WRITE_TOOLS:
        reason = _file_write(
            args.get("path"), home, os_home, settings, directory=tool == "files_create_directory"
        )
        return None if reason is None else f"{reason} {_GRANT_FREE}"
    if tool not in COMMAND_TOOLS:
        return None
    host = tool == HOST_COMMAND_TOOL
    command = args.get("command")
    if isinstance(command, str):
        if tool == HOST_COMMAND_TOOL:
            places = Places.for_host_command(home, os_home, settings)
        else:
            places = Places.at(home, os_home, settings, cwd, cwd_known=cwd_known, strict=strict)
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


def guard_of_approval(parked_metadata: Mapping[str, object] | None) -> str | None:
    """The reason a parked call was marked guarded, or None for an ordinary one. A guarded
    approval is once-only and may only come from the operator's own session."""
    if not is_guarded(parked_metadata):
        return None
    marked = parked_metadata.get(GUARD_KEY) if parked_metadata else None
    return marked if isinstance(marked, str) else "This call always needs your approval."


def unconfined(tool: str, *, settings: Settings) -> str | None:
    """The warning a host command's card carries when its fence is not in force."""
    if tool != HOST_COMMAND_TOOL:
        return None
    state = confinement_state(settings)
    if state.state == "active":
        return None
    if state.state == "disabled_by_setting":
        return "The host fence is turned off in Settings."
    why = state.reason or "it is not available"
    return (
        f"The host fence can't start: {why}. "
        + "Install bubblewrap or turn the fence off in Settings."
    )


def _command_reason(command: str, start: Places, *, host: bool) -> str | None:
    found = scan(command)
    for step in found.steps:
        for directories in step.cwds:
            reason = _act_reason(step.command, start.walk(directories), host)
            if reason is not None:
                return reason
    for redirect in found.writes:
        for directories in redirect.cwds:
            why = start.walk(directories).protected(redirect.path, host=host)
            if why is not None:
                return f"This command writes {redirect.path}: {why}."
    if host and found.piped_shells:
        return _UNREADABLE_SCRIPT
    for part in found.unreadable:
        if _REMOVER_WORDS.search(part):
            return "This command cannot be fully read and appears to delete files."
    return None


def _act_reason(one: ShellCommand, places: Places, host: bool) -> str | None:
    name = name_of(one)
    if OPAQUE in one.program:
        return _opaque_program_reason(one, places, host)
    if name == "osascript" and host:
        return "This command runs osascript, which can do anything on your computer."
    if host and installs_crontab(one):
        return "This command replaces your crontab, which runs commands on a schedule."
    for target in removal_targets(one):
        if OPAQUE in target:
            if not _known_tail(target):
                return "This command deletes a path that is only known when it runs."
            continue
        what = places.critical(target, host=host)
        if what is not None:
            return f"This command deletes {what} ({target}), which cannot be undone."
    moved = transfer(one)
    if moved is not None:
        reason = _transfer_reason(moved, places, host)
        if reason is not None:
            return reason
    for written in write_targets(one):
        why = places.protected(written.path, host=host, kind=written.kind)
        if why is not None:
            return f"This command writes {written.path}: {why}."
    if name == "git":
        for pair in git_overrides(one.arguments):
            key, _, value = pair.partition("=")
            if executes_code(key.lower(), value):
                return f"This command makes git run {key} for this call, which can run code."
    change = config_write(one)
    return None if change is None else _config_reason(change, places, host)


def _opaque_program_reason(one: ShellCommand, places: Places, host: bool) -> str | None:
    """A command whose program is only known when it runs is guarded when anything it is
    given is a path that must not be touched."""
    for word in operands(one.arguments):
        if OPAQUE in word:
            continue
        what = places.critical(word, host=host)
        if what is not None:
            return f"This command runs a program it can't read on {what} ({word})."
        why = places.protected(word, host=host)
        if why is not None:
            return f"This command runs a program it can't read on {word}: {why}."
    return None


def _transfer_reason(moved: Transfer, places: Places, host: bool) -> str | None:
    if moved.moves:
        for source in moved.sources:
            if OPAQUE in source:
                continue
            what = places.critical(source, host=host)
            if what is not None:
                return f"This command moves {what} ({source}) away."
            why = places.protected(source, host=host, kind="remove")
            if why is not None:
                return f"This command moves {source} away: {why}."
    if moved.prunes and OPAQUE not in moved.dest:
        what = places.critical(moved.dest, host=host)
        if what is not None:
            return f"This command deletes what {what} ({moved.dest}) holds beyond its sources."
    if not (moved.into_directory or places.is_dir(moved.dest, host=host)):
        why = places.protected(moved.dest, host=host)
        return None if why is None else f"This command writes {moved.dest}: {why}."
    for source in moved.sources:
        contents = OPAQUE in source or source.endswith(("/", "/.")) or source in (".", "..")
        if contents:
            if places.holds_protected(moved.dest, host=host):
                return (
                    f"This command copies names it can't read into {moved.dest}, "
                    + "which holds files that must not be overwritten."
                )
            continue
        target = f"{moved.dest.rstrip('/')}/{posixpath.basename(source.rstrip('/'))}"
        why = places.protected(target, host=host)
        if why is not None:
            return f"This command writes {target}: {why}."
    return None


def _known_tail(target: str) -> bool:
    """Whether an unreadable path still ends in a plain name, such as ``$DIR/build``."""
    tail = target.rstrip("/").rsplit("/", 1)[-1]
    return (
        bool(tail)
        and OPAQUE not in tail
        and tail not in (".", "..")
        and not (GLOB_CHARS & set(tail))
    )


def _config_reason(change: ConfigWrite, places: Places, host: bool) -> str | None:
    if change.unreadable:
        return "This command is a git config call that may set a key that makes git run code."
    if change.removes and is_executing_key(change.key):
        return None
    if executes_code(change.key, change.value):
        return f"This command sets git's {change.key}, which makes git run code."
    if change.scope in ("global", "system") and host:
        return "This command changes the git configuration for every repository on this computer."
    if change.scope == "file" and change.file is not None:
        why = places.protected(change.file, host=host)
        return None if why is None else f"This command writes {change.file}: {why}."
    return None


def _file_write(
    raw: object, home: RunHome | None, os_home: Path, settings: Settings, *, directory: bool
) -> str | None:
    if home is None or not isinstance(raw, str):
        return None
    places = Places.at(home, os_home, settings, None)
    spellings = [raw]
    if not raw.strip().startswith(("~", "/")):
        spellings.append(f"~/{raw}")
    for spelling in spellings:
        why = places.protected(spelling, host=False, kind="dir" if directory else "write")
        if why is not None:
            return f"This writes {raw}: {why}."
    return None
