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

from services.permissions.shell_ast import ShellCommand, operands, option_values
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


def config_write(one: ShellCommand) -> ConfigWrite | None:
    """The configuration change a ``git config`` call makes, or None for a read."""
    if one.program.rsplit("/", 1)[-1] != "git":
        return None
    sub, rest = git_subcommand(one.arguments)
    if sub != "config" or any(word in _CONFIG_READS for word in rest):
        return None
    found = operands(rest, frozenset({"--file", "-f", "--blob", "--default", "--type"}))
    verb = found[0] if found and found[0] in _CONFIG_VERBS else None
    if verb in ("get", "list"):
        return None
    if verb is not None:
        found = found[1:]
    writes = verb is not None or any(word in _CONFIG_WRITES for word in rest)
    if len(found) < 2 and not writes:
        return None
    key = found[0].lower() if found else None
    value = found[1] if len(found) > 1 else None
    removes = verb == "unset" or any(word in _CONFIG_UNSETS for word in rest)
    files = option_values(tuple(rest), "f", ("--file",))
    if files:
        return ConfigWrite(key, value, "file", removes, files[0])
    if "--global" in rest:
        return ConfigWrite(key, value, "global", removes)
    if "--system" in rest:
        return ConfigWrite(key, value, "system", removes)
    return ConfigWrite(key, value, "local", removes)
