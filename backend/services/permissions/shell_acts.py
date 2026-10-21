"""What one command does to the filesystem, read off its program and flags.

Takes the commands :mod:`shell_scan` found and names the paths each deletes, writes,
copies or moves. It knows a short list of programs; one it does not know contributes
nothing, so this reads the common shapes of a destructive command and is not a complete
account of one. Git configuration is :mod:`git_config`.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Literal

from services.permissions.git_config import git_subcommand
from services.permissions.shell_ast import OPAQUE, ShellCommand, operands, option_values

#: A stand-in for "every untracked and ignored file in the working tree".
WORKTREE = "<working tree>"
#: A stand-in for every repository's `.git` folder, which a `find` can delete by name.
GIT_FOLDERS = "<.git folders>"

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
_TOP_PATHSPECS = frozenset({".", "./", ":/", ":(top)", ":(top)."})
#: What narrows a ``find`` to some of what is under its roots, so that deleting the matches
#: does not delete the roots. `-type` is not one: `find ~ -type f -delete` empties ~.
_FIND_NARROWING = frozenset(
    {
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-regex",
        "-iregex",
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
_FIND_NAMES = frozenset({"-name", "-iname", "-path", "-ipath"})

type Kind = Literal["write", "dir", "remove"]


@dataclass(frozen=True)
class Target:
    path: str
    kind: Kind


@dataclass(frozen=True)
class Transfer:
    """A copy, move, link, sync or extraction of ``sources`` to ``dest``."""

    dest: str
    sources: tuple[str, ...]
    #: Whether ``dest`` must be a directory, by the way the command is written.
    into_directory: bool
    #: Whether the sources are removed (``mv``, ``rsync --remove-source-files``).
    moves: bool
    #: Whether files in ``dest`` that are not in the sources are deleted (``rsync --delete``).
    prunes: bool


def name_of(one: ShellCommand) -> str:
    return posixpath.basename(one.program)


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
    """What a ``find`` deletes: its roots when nothing narrows it, each root's ``.git`` when
    it is asked for by name, and nothing when it narrows to some other names."""
    deleting = None
    for index, word in enumerate(args):
        if word == "-delete" or (
            word in ("-exec", "-execdir")
            and index + 1 < len(args)
            and posixpath.basename(args[index + 1]) in REMOVERS
        ):
            deleting = index
            break
    if deleting is None:
        return []
    before = args[:deleting]
    for index, (word, value) in enumerate(zip(before, before[1:], strict=False)):
        if word in _FIND_NAMES and (value == ".git" or value.endswith("/.git")):
            if not _prunes_git_away(args, index, deleting):
                return [GIT_FOLDERS]
            if _DEPTH_FIRST & set(args):
                # Depth-first traversal visits a folder's contents before the folder, so
                # `-prune` does nothing: judge the branch after `-o` on its own.
                if any(word in _FIND_NARROWING for word in args[index + 4 : deleting]):
                    return []
                return _find_roots(args)
    if any(word in _FIND_NARROWING for word in before):
        return []
    return _find_roots(args)


_FIND_GROUPING = frozenset({"(", "\\(", ")", "\\)", "!", "-not"})
#: What makes `find` walk depth-first, where `-prune` has no effect; `-delete` implies it.
_DEPTH_FIRST = frozenset({"-depth", "-d", "-delete"})


def _prunes_git_away(args: tuple[str, ...], test: int, action: int) -> bool:
    """Whether a ``find`` keeps ``.git`` out of the branch that deletes: the strict idiom
    `-name .git -prune -o … -delete`, with the test followed at once by `-prune` and `-o`,
    the action after that, and no grouping or negation that could turn it around."""
    if any(word in _FIND_GROUPING for word in args):
        return False
    after = args[test + 2 : test + 4]
    return after == ("-prune", "-o") and action > test + 3


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
    if any(word in _FIND_NARROWING or word == "-type" for word in args):
        return []
    return _find_roots(args)


def _clean_targets(rest: list[str]) -> list[str]:
    flags = {
        letter.lower() if letter == "X" else letter
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


def _edits_in_place(name: str, args: tuple[str, ...]) -> bool:
    if name not in ("sed", "gsed", "perl"):
        return False
    return any(
        word.startswith("--in-place")
        or (word.startswith("-") and not word.startswith("--") and "i" in word[1:])
        for word in args
    )


def _extraction(name: str, args: tuple[str, ...]) -> Transfer | None:
    """``tar -x -C DIR`` and ``unzip -d DIR``: names read from an archive land in DIR."""
    if name == "tar":
        extracting = any(
            word in ("--extract", "--get")
            or (word.startswith("-") and not word.startswith("--") and "x" in word[1:])
            for word in args
        ) or (bool(args) and not args[0].startswith("-") and "x" in args[0])
        folders = option_values(args, "C", ("--directory",)) if extracting else []
    elif name == "unzip":
        folders = option_values(args, "d", ())
    else:
        return None
    if not folders:
        return None
    return Transfer(folders[-1], (OPAQUE,), into_directory=True, moves=False, prunes=False)


def transfer(one: ShellCommand) -> Transfer | None:
    """The copy, move, link, sync or extraction a command makes, or None for any other."""
    name = name_of(one)
    args = one.arguments
    if name in ("tar", "unzip"):
        return _extraction(name, args)
    if name not in _COPIERS:
        return None
    rsync = name == "rsync"
    directory = [] if rsync else option_values(args, "t", ("--target-directory",))
    found = operands(args, _RSYNC_VALUE_FLAGS if rsync else _COPY_VALUE_FLAGS)
    if directory:
        dest, sources = directory[0], tuple(found)
    elif len(found) >= 2:
        dest, sources = found[-1], tuple(found[:-1])
    else:
        return None
    return Transfer(
        dest=dest,
        sources=sources,
        into_directory=bool(directory) or len(sources) > 1 or dest.endswith("/"),
        moves=name == "mv" or (rsync and "--remove-source-files" in args),
        prunes=rsync and any(word.startswith(("--delete", "--del")) for word in args),
    )


def installs_crontab(one: ShellCommand) -> bool:
    """Whether the command replaces the crontab from a file or from its input."""
    return name_of(one) == "crontab" and bool(operands(one.arguments, frozenset({"-u"})))


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
