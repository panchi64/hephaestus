"""What one command does to the filesystem, read off its program and flags.

Takes the commands :mod:`shell_scan` found and names the paths each deletes, writes,
copies or moves. It knows a short list of programs; one it does not know contributes
nothing, so this reads the common shapes of a destructive command and is not a complete
account of one.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass
from typing import Literal

from services.permissions.shell_ast import ShellCommand, operands

#: A stand-in for "every untracked and ignored file in the working tree".
WORKTREE = "<working tree>"

GLOB_CHARS = frozenset("*?[")


def _without_brackets(text: str) -> str | None:
    """``text`` with its bracket expressions removed, following the shell's rules, or
    None when one cannot be accounted for.

    An expression is `[`, an optional `!` or `^`, a `]` that is a literal when first, then
    members up to the next `]`, where `[:class:]`, `[=x=]` and `[.x.]` are single members.
    A backslash or quote inside one is not accounted for either: the words reaching here
    are already decoded, so an escaped `]` can no longer be told from a closing one.
    """
    kept: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "[":
            kept.append(text[index])
            index += 1
            continue
        index += 1
        if index < len(text) and text[index] in "!^":
            index += 1
        if index < len(text) and text[index] == "]":
            index += 1
        while True:
            if index >= len(text):
                return None
            if text[index] in "\\'\"":
                return None
            if text[index] == "]":
                index += 1
                break
            if text[index] == "[" and index + 1 < len(text) and text[index + 1] in ":=.":
                close = text.find(text[index + 1] + "]", index + 2)
                if close == -1:
                    return None
                index = close + 2
            else:
                index += 1
    return "".join(kept)


def names_whole_folder(segment: str) -> bool:
    """Whether a path segment is a pattern that reaches every entry of its folder.

    It is unless it holds a literal anchor: something other than the metacharacters once
    a leading dot is set aside. `*`, `.*`, `?*`, `[a-z]*` and `.[!.]*` reach everything;
    `*.log`, `test_*` and `foo*` reach only some of it. A segment with no metacharacter
    is a name, not a pattern. A bracket the segment does not close reaches everything.
    """
    if not GLOB_CHARS & set(segment):
        return False
    rest = _without_brackets(segment[1:] if segment.startswith(".") else segment)
    return rest is None or not re.sub(r"[*?]", "", rest)


REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred", "srm"})

_WRITES_ALL = frozenset({"tee", "touch", "truncate"})
_COPIERS = frozenset({"cp", "mv", "install", "ln", "rsync"})
_COPY_VALUE_FLAGS = frozenset(
    {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group"}
)
_RSYNC_VALUE_FLAGS = frozenset(
    {
        "-e",
        "--rsh",
        "-f",
        "--filter",
        "--exclude",
        "--include",
        "--exclude-from",
        "--include-from",
        "--files-from",
        "--port",
        "--backup-dir",
        "--suffix",
        "--partial-dir",
        "--temp-dir",
        "--bwlimit",
        "--chmod",
        "--chown",
        "--compare-dest",
        "--link-dest",
        "-T",
    }
)
_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
_TOP_PATHSPECS = frozenset({".", "./", ":/", ":(top)", ":(top)."})
_FIND_FILTERS = frozenset(
    {
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-regex",
        "-iregex",
        "-type",
        "-newer",
        "-mtime",
        "-atime",
        "-ctime",
        "-mmin",
        "-amin",
        "-cmin",
        "-size",
        "-user",
        "-group",
        "-perm",
        "-empty",
        "-links",
        "-inum",
        "-samefile",
        "-lname",
    }
)
_CONFIG_READS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin"}
)
_CONFIG_UNSETS = frozenset({"--unset", "--unset-all", "--remove-section"})
_CONFIG_WRITES = _CONFIG_UNSETS | {"--add", "--replace-all", "--edit", "-e", "--rename-section"}
#: Configuration keys whose value git runs as a command or loads as more configuration.
_EXECUTING_KEYS = (
    "core.hookspath",
    "core.fsmonitor",
    "core.pager",
    "core.editor",
    "core.sshcommand",
    "core.askpass",
    "include.path",
    "includeif.*",
    "filter.*",
    "diff.*.textconv",
    "diff.*.command",
    "credential.helper",
    "credential.*.helper",
    "sequence.editor",
)

type Kind = Literal["write", "dir", "remove"]


@dataclass(frozen=True)
class Target:
    path: str
    kind: Kind


@dataclass(frozen=True)
class Transfer:
    """A copy, move, link or sync of ``sources`` to ``dest``."""

    dest: str
    sources: tuple[str, ...]
    #: Whether ``dest`` must be a directory, by the way the command is written.
    into_directory: bool
    #: Whether the sources are removed (``mv``, ``rsync --remove-source-files``).
    moves: bool
    #: Whether files in ``dest`` that are not in the sources are deleted (``rsync --delete``).
    prunes: bool
    #: Whether a source written ``dir/`` or ``dir/.`` copies the contents of the directory.
    trailing_slash_copies_contents: bool


def name_of(one: ShellCommand) -> str:
    return posixpath.basename(one.program)


def git_subcommand(arguments: tuple[str, ...]) -> tuple[str | None, list[str]]:
    """The subcommand of a ``git`` call and the words after it, past the global options."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in _GIT_VALUE_FLAGS:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            return word, list(arguments[index + 1 :])
    return None, []


def git_overrides(arguments: tuple[str, ...]) -> list[str]:
    """The ``key=value`` pairs given to git with ``-c`` for this one call."""
    found: list[str] = []
    for index, word in enumerate(arguments[:-1]):
        if word == "-c":
            found.append(arguments[index + 1])
        elif not word.startswith("-"):
            break
    return found


def removal_targets(one: ShellCommand) -> list[str]:
    """The paths a deleting command removes; :data:`WORKTREE` for a forced ``git clean``."""
    name = name_of(one)
    args = one.arguments
    if name in REMOVERS:
        return operands(args)
    if name == "find":
        return _find_targets(args)
    if name == "git":
        sub, rest = git_subcommand(args)
        if sub == "clean":
            return _clean_targets(rest)
        if sub == "worktree" and rest[:1] == ["remove"]:
            return operands(rest[1:])
    return []


def _find_targets(args: tuple[str, ...]) -> list[str]:
    """The roots a ``find`` deletes, when nothing narrows what it deletes to part of them."""
    deleting = None
    for index, word in enumerate(args):
        if word == "-delete" or (
            word in ("-exec", "-execdir")
            and index + 1 < len(args)
            and posixpath.basename(args[index + 1]) in REMOVERS
        ):
            deleting = index
            break
    if deleting is None or any(word in _FIND_FILTERS for word in args[:deleting]):
        return []
    return _find_roots(args)


def _find_roots(args: tuple[str, ...]) -> list[str]:
    roots: list[str] = []
    for word in args:
        if word in ("-H", "-L", "-P"):
            continue
        if word.startswith(("-", "(", "!")):
            break
        roots.append(word)
    return roots or ["."]


def find_feed(args: tuple[str, ...]) -> list[str]:
    """The paths a ``find`` prints that this can name: its roots when nothing narrows what
    it matches, and none when a filter makes it print names inside them."""
    if any(word in _FIND_FILTERS for word in args):
        return []
    return _find_roots(args)


def _clean_targets(rest: list[str]) -> list[str]:
    flags = {
        letter
        for word in rest
        if word.startswith("-") and not word.startswith("--")
        for letter in word[1:]
    }
    if "--force" in rest:
        flags.add("f")
    if "--dry-run" in rest:
        flags.add("n")
    if not {"x", "d", "f"} <= flags or "n" in flags:
        return []
    specs = operands(rest, frozenset({"-e", "--exclude"}))
    if not specs or any(spec in _TOP_PATHSPECS for spec in specs):
        return [WORKTREE]
    return specs


def option_values(
    arguments: tuple[str, ...], short: str | None, long: tuple[str, ...]
) -> list[str]:
    """The values given to one option, in every spelling: ``-o F``, ``-oF``, ``-so F``,
    ``--output F`` and ``--output=F``."""
    values: list[str] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--":
            break
        matched = False
        for flag in long:
            if word == flag and index + 1 < len(arguments):
                values.append(arguments[index + 1])
                index += 1
                matched = True
                break
            if word.startswith(flag + "="):
                values.append(word[len(flag) + 1 :])
                matched = True
                break
        if not matched and short and word.startswith("-") and not word.startswith("--"):
            body = word[1:]
            position = body.find(short)
            if position != -1:
                attached = body[position + 1 :]
                if attached:
                    values.append(attached)
                elif index + 1 < len(arguments):
                    values.append(arguments[index + 1])
                    index += 1
        index += 1
    return values


def _edits_in_place(name: str, args: tuple[str, ...]) -> bool:
    if name not in ("sed", "gsed", "perl"):
        return False
    return any(
        word.startswith("--in-place")
        or (word.startswith("-") and not word.startswith("--") and "i" in word[1:])
        for word in args
    )


def transfer(one: ShellCommand) -> Transfer | None:
    """The copy, move, link or sync a command makes, or None for any other command."""
    name = name_of(one)
    if name not in _COPIERS:
        return None
    args = one.arguments
    rsync = name == "rsync"
    directory = [] if rsync else option_values(args, "t", ("--target-directory",))
    flags = _RSYNC_VALUE_FLAGS if rsync else _COPY_VALUE_FLAGS
    found = operands(args, flags)
    recursive = rsync or any(
        word in ("-r", "-R", "--recursive") or (word.startswith("-a") and not word.startswith("--"))
        for word in args
    )
    if directory:
        dest, sources = directory[0], tuple(found)
    elif len(found) >= 2:
        dest, sources = found[-1], tuple(found[:-1])
    else:
        return None
    pruned = rsync and any(word.startswith(("--delete", "--del")) for word in args)
    moved = name == "mv" or (rsync and "--remove-source-files" in args)
    return Transfer(
        dest=dest,
        sources=sources,
        into_directory=bool(directory) or len(sources) > 1 or dest.endswith("/"),
        moves=moved,
        prunes=pruned,
        trailing_slash_copies_contents=recursive,
    )


def write_targets(one: ShellCommand) -> list[Target]:
    """The paths a command (other than a copy or move, see :func:`transfer`) writes,
    creates or deletes."""
    name = name_of(one)
    args = one.arguments
    if name in REMOVERS:
        return [Target(path, "remove") for path in operands(args)]
    if name == "mkdir":
        return [Target(path, "dir") for path in operands(args)]
    if name in _WRITES_ALL:
        return [Target(path, "write") for path in operands(args)]
    if name == "dd":
        return [Target(word[3:], "write") for word in args if word.startswith("of=")]
    if _edits_in_place(name, args):
        return [Target(path, "write") for path in operands(args)]
    if name == "curl":
        return [Target(path, "write") for path in option_values(args, "o", ("--output",))]
    if name == "wget":
        files = option_values(args, "O", ("--output-document",))
        folders = option_values(args, "P", ("--directory-prefix",))
        return [*(Target(p, "write") for p in files), *(Target(p, "dir") for p in folders)]
    return []


def is_executing_key(key: str | None) -> bool:
    """Whether git runs the value of configuration ``key`` as a command (an alias only
    when its value starts with ``!``, see :func:`executes_code`)."""
    if key is None:
        return False
    return key.startswith("alias.") or any(
        fnmatch.fnmatchcase(key, pattern) for pattern in _EXECUTING_KEYS
    )


def executes_code(key: str | None, value: str | None) -> bool:
    """Whether setting git configuration ``key`` to ``value`` makes git run a command."""
    if key is None:
        return False
    if key.startswith("alias."):
        return value is not None and value.lstrip().startswith("!")
    return is_executing_key(key)


@dataclass(frozen=True)
class ConfigWrite:
    """A ``git config`` that changes configuration."""

    key: str | None
    value: str | None
    scope: Literal["global", "system", "file", "local"]
    removes: bool
    file: str | None = None


def config_write(one: ShellCommand) -> ConfigWrite | None:
    """The configuration change a ``git config`` call makes, or None for a read."""
    if name_of(one) != "git":
        return None
    sub, rest = git_subcommand(one.arguments)
    if sub != "config" or any(word in _CONFIG_READS for word in rest):
        return None
    found = operands(rest, frozenset({"--file", "-f", "--blob", "--default", "--type"}))
    if len(found) < 2 and not any(word in _CONFIG_WRITES for word in rest):
        return None
    key = found[0].lower() if found else None
    value = found[1] if len(found) > 1 else None
    removes = any(word in _CONFIG_UNSETS for word in rest)
    files = option_values(tuple(rest), "f", ("--file",))
    if files:
        return ConfigWrite(key, value, "file", removes, files[0])
    if "--global" in rest:
        return ConfigWrite(key, value, "global", removes)
    if "--system" in rest:
        return ConfigWrite(key, value, "system", removes)
    return ConfigWrite(key, value, "local", removes)
