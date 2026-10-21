"""Which git configuration makes git run a command, and who sets it.

Git reads commands out of configuration for hooks, pagers, filters, drivers and a dozen
other things, so setting such a key is running code the next time git does that thing.
:func:`config_write` reads a ``git config`` call, in the old flag syntax and the newer
``set``/``unset``/``get``/``list`` subcommands, and :func:`executes_code` says whether what
it sets is one of those.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Literal

from services.permissions.shell_ast import ShellCommand, option_values
from services.permissions.shell_flags import is_flag

_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
_CONFIG_READS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin"}
)
_CONFIG_UNSETS = frozenset({"--unset", "--unset-all", "--remove-section"})
_CONFIG_WRITES = _CONFIG_UNSETS | {"--add", "--replace-all", "--edit", "-e", "--rename-section"}
_CONFIG_VERBS = frozenset({"set", "unset", "get", "list"})
#: The options that take the next word as their value (`git config -h`, git 2.54).
_CONFIG_VALUE_FLAGS = frozenset(
    {"--file", "-f", "--blob", "--default", "--type", "-t", "--value", "--comment", "--url"}
)
_KNOWN_LONG = _CONFIG_VALUE_FLAGS | {
    "--global",
    "--system",
    "--local",
    "--worktree",
    "--all",
    "--regexp",
    "--fixed-value",
    "--includes",
    "--no-includes",
    "--null",
    "--name-only",
    "--show-names",
    "--show-scope",
    "--bool",
    "--int",
    "--bool-or-int",
    "--path",
    "--expiry-date",
    "--no-type",
    "--add",
    "--replace-all",
    "--unset",
    "--unset-all",
    "--remove-section",
    "--rename-section",
    "--edit",
    "--get-colorbool",
}
_KNOWN_SHORT = frozenset("ftzelnh")
#: Configuration keys whose value git runs as a command or loads as more configuration.
_EXECUTING_KEYS = (
    "core.hookspath",
    "core.fsmonitor",
    "core.pager",
    "core.editor",
    "core.sshcommand",
    "core.askpass",
    "core.gitproxy",
    "include.path",
    "includeif.*",
    "init.templatedir",
    "interactive.difffilter",
    "filter.*",
    "diff.external",
    "diff.*.textconv",
    "diff.*.command",
    "merge.*.driver",
    "mergetool.*.cmd",
    "difftool.*.cmd",
    "pager.*",
    "gpg.program",
    "gpg.*.program",
    "credential.helper",
    "credential.*.helper",
    "sequence.editor",
    "remote.*.uploadpack",
    "remote.*.receivepack",
    "submodule.*.update",
)


def git_subcommand(arguments: tuple[str, ...]) -> tuple[str | None, list[str]]:
    """The subcommand of a ``git`` call and the words after it, past the global options."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in _GIT_VALUE_FLAGS:
            index += 2
        elif is_flag(word):
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
    #: Set when the call could not be read with confidence (an option this does not know,
    #: or an executing key among its operands), so it is treated as setting one.
    unreadable: bool = False


def _unknown_option(options: list[str]) -> bool:
    """Whether ``options`` holds one this parser does not know, so which of the words is
    the key cannot be told."""
    for word in options:
        name = word.split("=", 1)[0]
        if name.startswith("--"):
            if name not in _KNOWN_LONG and name not in _CONFIG_READS:
                return True
        elif any(letter not in _KNOWN_SHORT for letter in name[1:]):
            return True
    return False


@dataclass(frozen=True)
class _Words:
    """The words of a ``git config`` call, told apart: the options, the operands, and
    whether an option's value could not be told from another option."""

    options: list[str]
    operands: list[str]
    ambiguous: bool


def _split(rest: list[str]) -> _Words:
    """Walk ``rest`` once. The word after a value-taking option is its value, so it is
    neither an option nor an operand; a value that starts with ``-`` is ambiguous."""
    options: list[str] = []
    found: list[str] = []
    ambiguous = False
    literal = False
    index = 0
    while index < len(rest):
        word = rest[index]
        if literal:
            found.append(word)
        elif word == "--":
            literal = True
        elif is_flag(word):
            options.append(word)
            if word in _CONFIG_VALUE_FLAGS and index + 1 < len(rest):
                index += 1
                ambiguous = ambiguous or rest[index].startswith("-")
        else:
            found.append(word)
        index += 1
    return _Words(options, found, ambiguous)


def _hides_executing_key(found: list[str]) -> bool:
    """Whether any operand of a write is an executing key, wherever it sits, so that a
    mistake about which operand is the key cannot hide one."""
    lowered = [word.lower() for word in found]
    return any(
        any(fnmatch.fnmatchcase(word, pattern) for pattern in _EXECUTING_KEYS) for word in lowered
    ) or any(executes_code(key, value) for key, value in zip(lowered, found[1:], strict=False))


def config_write(one: ShellCommand) -> ConfigWrite | None:
    """The configuration change a ``git config`` call makes, or None for a read."""
    if one.program.rsplit("/", 1)[-1] != "git":
        return None
    sub, rest = git_subcommand(one.arguments)
    if sub != "config":
        return None
    words = _split(rest)
    found = words.operands
    verb = found[0] if found and found[0] in _CONFIG_VERBS else None
    unreadable = words.ambiguous or _unknown_option(words.options)
    if not unreadable and (verb in ("get", "list") or _CONFIG_READS & set(words.options)):
        return None
    if verb is not None:
        found = found[1:]
    # An unset takes one key, so a second operand is a value to match, or a key that an
    # option's value has pushed out of place: either way it is looked at.
    unreadable = unreadable or (len(found) >= 2 and _hides_executing_key(found))
    removes = verb == "unset" or bool(_CONFIG_UNSETS & set(words.options))
    writes = verb is not None or bool(_CONFIG_WRITES & set(words.options))
    if len(found) < 2 and not writes and not unreadable:
        return None
    key = found[0].lower() if found else None
    value = found[1] if len(found) > 1 else None
    files = option_values(tuple(rest), "f", ("--file",))
    if files:
        return ConfigWrite(key, value, "file", removes, files[0], unreadable)
    if "--global" in words.options:
        return ConfigWrite(key, value, "global", removes, None, unreadable)
    if "--system" in words.options:
        return ConfigWrite(key, value, "system", removes, None, unreadable)
    return ConfigWrite(key, value, "local", removes, None, unreadable)
