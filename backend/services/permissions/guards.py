"""Actions that ask the operator at every level, Yolo included, and that no grant or
reviewer can answer.

A guard is a pure reading of one call: :func:`guard` returns the reason the call must be
put to the operator, or None. It looks for three things: a command that deletes a critical
path, a write to a protected path (the sandbox floor, the data folder), and a host command
whose fence was wanted but cannot be built. A fence the operator switched off is their
choice and is not a guard, though the card still carries a warning (:func:`unconfined`).

The paths are read off the command's syntax, so a path built at run time cannot be seen.
That is why this is a backstop for the common shapes of the mistake, not a fence.
"""

from __future__ import annotations

import posixpath
import re
import sys
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Protocol, cast

from starlette.requests import Request

from core.auth import SESSION_COOKIE, AuthManager, token_from_headers
from core.config import Settings
from core.exceptions import PermissionDeniedError
from services.home.paths import HOME, absolute
from services.sandbox.fence import confinement_state
from services.sandbox.floor import is_protected_write

from .shell_ast import ShellCommand, shell_reach

if TYPE_CHECKING:
    from services.home.table import RunHome

#: The key a guarded call carries in its approval metadata, and in a parked turn's.
GUARD_KEY = "guard"

FILE_WRITE_TOOLS = frozenset({"files_write_file", "files_edit_file", "files_create_directory"})
SHELL_TOOLS = frozenset({"shell_run_command", "shell_start_command"})
HOST_COMMAND_TOOL = "code_run_host_command"

#: Every tool :func:`guard` has a rule for.
GUARDED_TOOLS = FILE_WRITE_TOOLS | SHELL_TOOLS | {HOST_COMMAND_TOOL}

_REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_WRAPPERS = frozenset(
    {"sudo", "doas", "env", "command", "exec", "nohup", "nice", "time", "builtin", "timeout"}
)
#: Options of a wrapper that consume the next word.
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
}
_MAX_NESTING = 3
_HOME_VARIABLE = re.compile(r"\$\{HOME\}|\$HOME\b")
_REDIRECT = re.compile(r"""(?<![<>&|\d])>>?\|?\s*["']?([^\s;&|"')<>]+)""")
_GLOB_CHARS = frozenset("*?[")
#: Where a command writes, as the words after the flags, by program: every operand, only
#: the last (the destination), or none that this reads.
_WRITES_ALL = frozenset({"tee", "touch", "truncate", "mkdir", "mv"})
_WRITES_LAST = frozenset({"cp", "install", "ln", "rsync"})

_WORKTREE = "<working tree>"

_GRANT_FREE = "It always asks, whatever the permission level, and no standing approval covers it."


class _OperatorGate(Protocol):
    """The slice of ``app.state`` :func:`require_operator` reads."""

    @property
    def settings(self) -> _AuthSettings: ...

    @property
    def auth_manager(self) -> AuthManager: ...


class _AuthSettings(Protocol):
    @property
    def auth_enabled(self) -> bool: ...


class _OperatorApp(Protocol):
    @property
    def state(self) -> _OperatorGate: ...


def require_operator(request: Request) -> None:
    """Refuse a request that did not come from the operator's own session.

    An API token never answers a guarded call, whatever scopes it holds. With
    authentication switched off every caller is the operator.
    """
    state = cast("_OperatorApp", request.scope["app"]).state
    if not state.settings.auth_enabled:
        return
    presented = token_from_headers(
        request.headers.get("authorization"), request.cookies.get(SESSION_COOKIE)
    )
    if not state.auth_manager.verify(presented):
        raise PermissionDeniedError(
            "only the operator can answer this call; an API token cannot approve it"
        )


def is_guarded(metadata: Mapping[str, object] | None) -> bool:
    """Whether a parked call's metadata marks it as one only the operator answers, once."""
    return bool(metadata and metadata.get(GUARD_KEY))


#: Reasons found while settling a batch, held until the park that announces them reads
#: them. Bounded because a nested ``run_code`` call is settled and never parked.
_REASONS_KEPT = 512
_reasons: OrderedDict[tuple[str, str], str] = OrderedDict()


def remember(run_id: str, tool_call_id: str, reason: str) -> None:
    _reasons[(run_id, tool_call_id)] = reason
    while len(_reasons) > _REASONS_KEPT:
        _ = _reasons.popitem(last=False)


def take(run_id: str, tool_call_id: str) -> str | None:
    return _reasons.pop((run_id, tool_call_id), None)


def guard(
    tool: str, args: Mapping[str, object], *, home: RunHome | None, settings: Settings
) -> str | None:
    """Why this call must go to the operator whatever the level says, or None."""
    os_home = Path.home()
    if tool in FILE_WRITE_TOOLS:
        reason = _file_write(args.get("path"), home, os_home)
        return None if reason is None else f"{reason} {_GRANT_FREE}"
    if tool not in SHELL_TOOLS and tool != HOST_COMMAND_TOOL:
        return None
    command = args.get("command")
    if isinstance(command, str):
        reason = _command_reason(command, home, settings, os_home)
        if reason is not None:
            return f"{reason} {_GRANT_FREE}"
    if tool == HOST_COMMAND_TOOL or args.get("reach") == "host":
        state = confinement_state(settings)
        if state.state == "unavailable":
            return (
                "This command would run on your computer with no confinement: "
                + f"{state.reason}. {_GRANT_FREE}"
            )
    return None


def unconfined(tool: str, args: Mapping[str, object], *, settings: Settings) -> str | None:
    """The warning a host command's card carries when its fence is not in force."""
    if tool != HOST_COMMAND_TOOL and not (tool in SHELL_TOOLS and args.get("reach") == "host"):
        return None
    state = confinement_state(settings)
    if state.state == "active":
        return None
    return state.reason or state.state.replace("_", " ")


def _command_reason(
    command: str, home: RunHome | None, settings: Settings, os_home: Path
) -> str | None:
    text = _HOME_VARIABLE.sub("~", command)
    commands, redirects = _analyse(text)
    for one in commands:
        for target in _removal_targets(one):
            what = _critical(target, home, settings, os_home)
            if what is not None:
                return f"This command deletes {what} ({target}), which cannot be undone."
    for target in [*redirects, *(t for one in commands for t in _write_targets(one))]:
        why = _protected(target, home, settings, os_home)
        if why is not None:
            return f"This command writes {target}: {why}."
    return None


def _analyse(command: str, depth: int = 0) -> tuple[list[ShellCommand], list[str]]:
    """Every command in ``command`` with its wrappers and ``sh -c`` scripts opened up, and
    every redirect target."""
    reach = shell_reach(command, root=None)
    commands: list[ShellCommand] = []
    redirects = [*reach.writes, *_REDIRECT.findall(command)]
    for one in reach.commands:
        opened = _unwrapped(one)
        script = _script_of(opened) if depth < _MAX_NESTING else None
        if script is None:
            commands.append(opened)
            continue
        inner, inner_redirects = _analyse(script, depth + 1)
        commands.extend(inner)
        redirects.extend(inner_redirects)
    return commands, redirects


def _unwrapped(one: ShellCommand) -> ShellCommand:
    """The command a wrapper such as ``sudo`` or ``env`` runs, however deeply wrapped."""
    for _ in range(_MAX_NESTING):
        program = posixpath.basename(one.program)
        if program not in _WRAPPERS:
            return one
        valued = _WRAPPER_VALUE_FLAGS.get(program, frozenset())
        words = list(one.arguments)
        index = 0
        while index < len(words):
            word = words[index]
            if word in valued:
                index += 2
            elif word.startswith("-") or (program == "env" and "=" in word):
                index += 1
            else:
                break
        if program == "timeout":
            index += 1
        rest = words[index:]
        if not rest:
            return one
        one = ShellCommand(program=rest[0], arguments=tuple(rest[1:]))
    return one


def _script_of(one: ShellCommand) -> str | None:
    if posixpath.basename(one.program) not in _SHELLS:
        return None
    args = one.arguments
    for index, word in enumerate(args[:-1]):
        if word.startswith("-") and not word.startswith("--") and "c" in word:
            return args[index + 1]
    return None


def _operands(args: Sequence[str]) -> list[str]:
    found: list[str] = []
    literal = False
    for word in args:
        if literal or not word.startswith("-") or word == "-":
            found.append(word)
        elif word == "--":
            literal = True
    return found


def _removal_targets(one: ShellCommand) -> list[str]:
    program = posixpath.basename(one.program)
    args = one.arguments
    if program in _REMOVERS:
        return _operands(args)
    if program == "find" and _find_deletes(args):
        roots: list[str] = []
        for word in args:
            if word in ("-H", "-L", "-P"):
                continue
            if word.startswith(("-", "(", "!")):
                break
            roots.append(word)
        return roots
    if program == "git":
        return _clean_targets(args)
    return []


def _find_deletes(args: Sequence[str]) -> bool:
    if "-delete" in args:
        return True
    return any(
        word in ("-exec", "-execdir") and posixpath.basename(nxt) in _REMOVERS
        for word, nxt in zip(args, args[1:], strict=False)
    )


def _clean_targets(args: Sequence[str]) -> list[str]:
    """The pathspecs of a ``git clean`` that forces away untracked and ignored directories,
    or the whole working tree when it names none."""
    words = list(args)
    while words and words[0] in ("-C", "-c"):
        words = words[2:]
    if not words or words[0] != "clean":
        return []
    flags = {
        letter
        for word in words[1:]
        if word.startswith("-") and not word.startswith("--")
        for letter in word[1:]
    }
    if "--force" in words:
        flags.add("f")
    if "--dry-run" in words:
        flags.add("n")
    if not {"x", "d", "f"} <= flags or "n" in flags:
        return []
    return _operands(words[1:]) or [_WORKTREE]


def _write_targets(one: ShellCommand) -> list[str]:
    program = posixpath.basename(one.program)
    if program == "dd":
        return [word[3:] for word in one.arguments if word.startswith("of=")]
    operands = _operands(one.arguments)
    if program in _REMOVERS or program in _WRITES_ALL:
        return operands
    if program in _WRITES_LAST:
        return operands[-1:]
    return []


def _folded(path: PurePath) -> tuple[str, ...]:
    return tuple(part.lower() for part in path.parts) if sys.platform == "darwin" else path.parts


def _glob_parent(text: str) -> str | None:
    """``text`` with trailing glob segments dropped, so ``~/*`` names ``~``. None when it
    is a glob with nothing fixed in front of it."""
    path = text.rstrip("/") or text[:1]
    while path and any(c in _GLOB_CHARS for c in posixpath.basename(path)):
        parent = posixpath.dirname(path)
        if not parent:
            return None
        path = parent
    return path or None


def _critical(text: str, home: RunHome | None, settings: Settings, os_home: Path) -> str | None:
    """What deleting ``text`` would take with it, when that is a path nobody deletes by
    accident."""
    if text == _WORKTREE:
        return "every untracked and ignored file in the working tree"
    stripped = _glob_parent(text)
    if stripped is None:
        return None
    path = posixpath.normpath(stripped)
    if path == "/":
        return "the filesystem root"
    if path == "~":
        return "the home folder"
    if path.startswith(("~/", f"{HOME}/", "/work/")) or path in (HOME, "/work"):
        virtual = absolute(path, cwd=HOME)
        places: set[str] = {m.place.path for m in home.mounts} if home is not None else set()
        groups = {f"{HOME}/{group}" for group in ("projects", "worktrees", "shared")}
        if virtual == HOME:
            return "the agent's home folder"
        if virtual in places or virtual in groups:
            return "a project, worktree or workspace folder"
    if not path.startswith("/"):
        return None
    return _host_critical(path, home, settings, os_home)


def _host_critical(
    path: str, home: RunHome | None, settings: Settings, os_home: Path
) -> str | None:
    named: list[tuple[Path, str]] = [
        (os_home, "the home folder"),
        (settings.data_dir, "the application's data folder"),
    ]
    if home is not None:
        named.extend((m.root, "a project, worktree or workspace folder") for m in home.mounts)
    candidate = Path(path)
    forms = {_folded(candidate), _folded(candidate.resolve())}
    for root, what in named:
        if forms & {_folded(root), _folded(root.resolve())}:
            return what
    return None


def _in(candidate: Path, root: Path) -> bool:
    base = _folded(root.resolve())
    return any(_folded(form)[: len(base)] == base for form in (candidate, candidate.resolve()))


def _protected(text: str, home: RunHome | None, settings: Settings, os_home: Path) -> str | None:
    """Why a command may not write ``text`` unasked, or None."""
    target = text.strip()
    if target == "~" or target.startswith("~/"):
        target = str(os_home / target[2:])
    if not target.startswith("/"):
        return is_protected_write(Path("/") / target, home=os_home)
    why = is_protected_write(target, home=os_home)
    if why is not None:
        return why
    path = Path(target)
    if _in(path, settings.data_dir) and not (
        home is not None and any(_in(path, m.root) for m in home.mounts)
    ):
        return "the application's data folder is off limits"
    return None


def _file_write(raw: object, home: RunHome | None, os_home: Path) -> str | None:
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
        why = is_protected_write(mount.root / rel, home=os_home)
        if why is not None:
            return f"This writes {raw}: {why}."
    return None
