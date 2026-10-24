"""Whether a shell command installs packages or clones a repository.

Those acts need the network while writing no address, so the grammar walk alone reads
them as offline. This table only ever widens a command toward the network and the
reviewer; nothing it matches is cleared, so a missing row costs a failed install and a
fence note, never an unreviewed act.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from services.permissions.shell_ast import ShellCommand, operands, unwrap

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
    "pnpm": (*_INSTALLS, ("dlx",)),
    "yarn": (*_INSTALLS, ("dlx",)),
    "bun": (*_INSTALLS, ("x",)),
    "cargo": (("install",), ("fetch",), ("add",), ("update",)),
    "go": (("get",), ("install",), ("mod", "download"), ("mod", "tidy")),
    "poetry": (("install",), ("add",), ("update",), ("lock",)),
    "pipenv": (("install",), ("sync",), ("update",), ("lock",)),
    "conda": (("install",), ("create",), ("update",)),
    "mamba": (("install",), ("create",), ("update",)),
    "micromamba": (("install",), ("create",), ("update",)),
    "gem": (("install",),),
    "bundle": (("install",),),
    "composer": (("install",), ("require",), ("update",)),
    "apt": (("install",), ("update",)),
    "apt-get": (("install",), ("update",)),
    "git": (("clone",), ("fetch",), ("pull",), ("ls-remote",), ("submodule", "update")),
}

#: Programs that install when run with no operand at all.
_BARE_INSTALLS = frozenset({"yarn"})

#: Programs that download what they run on any invocation.
_ALWAYS_FETCH = frozenset({"npx", "bunx"})

#: Flags that tell a package manager to use only what is already on disk.
_LOCAL_ONLY = frozenset({"--offline", "--no-index"})

#: Flags whose value is the next word, so that word is not read as the subcommand.
_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "git": frozenset({"-C", "-c"}),
    "npm": frozenset({"--prefix"}),
    "uv": frozenset({"--directory", "--project"}),
}

_VERSIONED = re.compile(r"^(pip|python)\d*(\.\d+)?$")


def fetches(commands: Sequence[ShellCommand]) -> bool:
    """Whether any of ``commands`` installs packages or clones a repository."""
    return any(_fetches(command.program, command.arguments) for command in commands)


def _fetches(program: str, arguments: tuple[str, ...]) -> bool:
    inner = unwrap(ShellCommand(program, arguments))
    if (inner.program, inner.arguments) != (program, arguments):
        return _fetches(inner.program, inner.arguments)
    name = _name(program)
    found = operands(arguments, _VALUE_FLAGS.get(name, frozenset()))
    if any(word in _LOCAL_ONLY for word in arguments):
        return False
    if name in _ALWAYS_FETCH:
        return True
    if name == "python":
        if arguments[:2] != ("-m", "pip"):
            return False
        name, found = "pip", operands(arguments[2:], _VALUE_FLAGS.get("pip", frozenset()))
    rows = _FETCHING.get(name)
    if rows is None:
        return False
    if not found:
        return name in _BARE_INSTALLS
    return any(tuple(found[: len(row)]) == row for row in rows)


def _name(program: str) -> str:
    """A program's basename, with a version suffix (`pip3.12`, `python3`) dropped."""
    base = program.rsplit("/", 1)[-1]
    versioned = _VERSIONED.match(base)
    return versioned.group(1) if versioned else base
