"""Whether a shell command installs packages or clones a repository.

Those acts need the network while writing no address, so the grammar walk alone reads
them as offline. This table only ever widens a command toward the network and the
reviewer; nothing it matches is cleared, so a missing row costs a failed install and a
fence note, never an unreviewed act.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from services.permissions.shell_ast import ShellCommand
from services.permissions.shell_flags import is_flag

_INSTALLS = (("install",), ("i",), ("add",), ("ci",), ("update",), ("upgrade",))

#: Program → the leading operands that make it fetch.
_FETCHING: dict[str, tuple[tuple[str, ...], ...]] = {
    "pip": (("install",), ("download",), ("wheel",)),
    "uv": (
        ("add",),
        ("sync",),
        ("lock",),
        ("pip", "install"),
        ("pip", "sync"),
        ("pip", "compile"),
        ("tool", "install"),
    ),
    "npm": _INSTALLS,
    "pnpm": _INSTALLS,
    "yarn": _INSTALLS,
    "bun": _INSTALLS,
    "cargo": (("install",), ("fetch",), ("add",), ("update",)),
    "go": (("get",), ("install",), ("mod", "download")),
    "gem": (("install",),),
    "bundle": (("install",),),
    "composer": (("install",), ("require",), ("update",)),
    "apt": (("install",), ("update",)),
    "apt-get": (("install",), ("update",)),
    "git": (("clone",), ("fetch",), ("pull",), ("submodule", "update")),
}

#: Programs that install when run with no operand at all.
_BARE_INSTALLS = frozenset({"yarn"})

#: Flags whose value is the next word, so that word is not read as the subcommand.
_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "git": frozenset({"-C", "-c"}),
    "npm": frozenset({"--prefix"}),
    "uv": frozenset({"--directory", "--project"}),
    "sudo": frozenset({"-u", "-g"}),
}

_VERSIONED = re.compile(r"^(pip|python)\d*(\.\d+)?$")


def fetches(commands: Sequence[ShellCommand]) -> bool:
    """Whether any of ``commands`` installs packages or clones a repository."""
    return any(_fetches(command.program, command.arguments) for command in commands)


def _fetches(program: str, arguments: tuple[str, ...]) -> bool:
    name = _name(program)
    if name == "env":
        rest = [a for a in arguments if not is_flag(a) and "=" not in a]
        return bool(rest) and _fetches(rest[0], _after(arguments, rest[0]))
    operands = _operands(name, arguments)
    if name == "sudo":
        return bool(operands) and _fetches(operands[0], _after(arguments, operands[0]))
    if name == "python":
        if arguments[:2] != ("-m", "pip"):
            return False
        name, operands = "pip", _operands("pip", arguments[2:])
    rows = _FETCHING.get(name)
    if rows is None:
        return False
    if not operands:
        return name in _BARE_INSTALLS
    return any(tuple(operands[: len(row)]) == row for row in rows)


def _name(program: str) -> str:
    """A program's basename, with a version suffix (`pip3.12`, `python3`) dropped."""
    base = program.rsplit("/", 1)[-1]
    versioned = _VERSIONED.match(base)
    return versioned.group(1) if versioned else base


def _operands(name: str, arguments: tuple[str, ...]) -> list[str]:
    """The non-flag words, skipping the value of each flag that takes the next word."""
    value_flags = _VALUE_FLAGS.get(name, frozenset())
    operands: list[str] = []
    skip = False
    for word in arguments:
        if skip:
            skip = False
        elif word in value_flags:
            skip = True
        elif not is_flag(word):
            operands.append(word)
    return operands


def _after(arguments: tuple[str, ...], word: str) -> tuple[str, ...]:
    return arguments[arguments.index(word) + 1 :]
