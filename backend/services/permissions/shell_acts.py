"""What one command does to the filesystem, read off its program and flags.

Takes the commands :mod:`shell_scan` found and names the paths each deletes or writes. It
knows a short list of programs; one it does not know contributes nothing, so this reads
the common shapes of a destructive command and is not a complete account of one.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Literal

from services.permissions.shell_ast import ShellCommand, operands

#: A bare name for "every untracked and ignored file in the working tree".
WORKTREE = "<working tree>"

REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred"})

_WRITES_ALL = frozenset({"tee", "touch", "truncate", "mv"})
_WRITES_LAST = frozenset({"cp", "install", "ln", "rsync"})
_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
_TOP_PATHSPECS = frozenset({".", "./", ":/", ":(top)", ":(top)."})
_CONFIG_READS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin"}
)
_CONFIG_WRITES = frozenset(
    {
        "--unset",
        "--unset-all",
        "--add",
        "--replace-all",
        "--edit",
        "-e",
        "--remove-section",
        "--rename-section",
    }
)

type Kind = Literal["write", "dir", "remove"]


@dataclass(frozen=True)
class Target:
    path: str
    kind: Kind


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


def removal_targets(one: ShellCommand) -> list[str]:
    """The paths a deleting command removes; :data:`WORKTREE` for a forced ``git clean``."""
    name = name_of(one)
    args = one.arguments
    if name in REMOVERS:
        return operands(args)
    if name == "find" and _find_deletes(args):
        roots: list[str] = []
        for word in args:
            if word in ("-H", "-L", "-P"):
                continue
            if word.startswith(("-", "(", "!")):
                break
            roots.append(word)
        return roots or ["."]
    if name == "git":
        sub, rest = git_subcommand(args)
        if sub == "clean":
            return _clean_targets(rest)
    return []


def _find_deletes(args: tuple[str, ...]) -> bool:
    if "-delete" in args:
        return True
    return any(
        word in ("-exec", "-execdir") and posixpath.basename(after) in REMOVERS
        for word, after in zip(args, args[1:], strict=False)
    )


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
    if name not in ("sed", "perl"):
        return False
    return any(
        word.startswith("--in-place")
        or (word.startswith("-") and not word.startswith("--") and "i" in word[1:])
        for word in args
    )


def write_targets(one: ShellCommand) -> list[Target]:
    """The paths a command writes, creates, replaces or deletes."""
    name = name_of(one)
    args = one.arguments
    if name in REMOVERS:
        return [Target(path, "remove") for path in operands(args)]
    if name == "mkdir":
        return [Target(path, "dir") for path in operands(args)]
    if name in _WRITES_ALL:
        return [Target(path, "write") for path in operands(args)]
    if name in _WRITES_LAST:
        directory = option_values(args, "t", ("--target-directory",))
        if directory:
            return [Target(path, "dir") for path in directory]
        return [Target(path, "write") for path in operands(args)[-1:]]
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


@dataclass(frozen=True)
class ConfigWrite:
    """A ``git config`` that changes configuration."""

    key: str | None
    scope: Literal["global", "system", "file", "local"]
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
    files = option_values(tuple(rest), "f", ("--file",))
    if files:
        return ConfigWrite(key, "file", files[0])
    if "--global" in rest:
        return ConfigWrite(key, "global")
    if "--system" in rest:
        return ConfigWrite(key, "system")
    return ConfigWrite(key, "local")
