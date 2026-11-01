"""The shell grammar the guards read commands with, and the words it yields.

**Why a real parser.** Regexes over shell syntax are wrong on the cases that matter: a `;`
inside a quoted string, a `$(…)` inside a `"…"`, a redirect glued to a word.
`tree-sitter-bash` recovers from errors instead of throwing, so a command it cannot parse
comes back as a tree with error nodes, which the scan treats as unreadable
(``shell_scan.py``). `bashlex` is unmaintained and breaks on ordinary bashisms.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from services.permissions.shell_flags import is_flag

_PARSER = Parser(Language(tree_sitter_bash.language()))


@dataclass(frozen=True)
class ShellCommand:
    """One command inside a shell action: the program and the words it was given.

    ``arguments`` is every literal argument in order, flags included: a flag separates
    `find .` from `find . -delete`, and is where a path hides.
    """

    program: str
    arguments: tuple[str, ...] = ()


#: Programs that run the command written after their own options.
WRAPPERS = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "command",
        "exec",
        "nohup",
        "nice",
        "time",
        "builtin",
        "timeout",
        "setsid",
        "stdbuf",
        "caffeinate",
        "arch",
        "chronic",
        "watch",
        "flock",
    }
)

#: Options of a wrapper that consume the next word.
WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
    "exec": frozenset({"-a"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "watch": frozenset({"-n", "-d"}),
    "flock": frozenset({"-w", "-E"}),
}

#: Wrappers whose first operand is not the command: the duration, the lock file.
_LEADING_OPERAND = frozenset({"timeout", "flock"})

#: Stands for a word the shell fills in at run time.
OPAQUE = "￼"

_UNWRAP_DEPTH = 4


def operands(arguments: Sequence[str], value_flags: frozenset[str] | None = None) -> list[str]:
    """The non-flag words of ``arguments``: the value of each flag in ``value_flags`` is
    skipped, and every word after a bare ``--`` is an operand."""
    skipped = value_flags or frozenset()
    found: list[str] = []
    skip = False
    literal = False
    for word in arguments:
        if skip:
            skip = False
        elif literal:
            found.append(word)
        elif word == "--":
            literal = True
        elif word in skipped:
            skip = True
        elif not is_flag(word):
            found.append(word)
    return found


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


def unwrap(one: ShellCommand) -> ShellCommand:
    """The command a wrapper such as ``sudo`` or ``env`` runs, however deeply wrapped.

    ``env -S 'string'`` is returned as a shell running the string. Wrappers nested past
    the depth limit come back as an unknown program, so a deep chain cannot hide one.
    """
    for _ in range(_UNWRAP_DEPTH):
        name = one.program.rsplit("/", 1)[-1]
        if name not in WRAPPERS:
            return one
        words = list(one.arguments)
        value_flags = WRAPPER_VALUE_FLAGS.get(name, frozenset())
        index = 0
        while index < len(words):
            word = words[index]
            if name == "env" and word in ("-S", "--split-string") and index + 1 < len(words):
                return ShellCommand("sh", ("-c", words[index + 1]))
            if word in value_flags:
                index += 2
            elif is_flag(word) or (name == "env" and "=" in word):
                index += 1
            else:
                break
        if name in _LEADING_OPERAND:
            index += 1
        rest = words[index:]
        if not rest:
            return one
        one = ShellCommand(program=rest[0], arguments=tuple(rest[1:]))
    if one.program.rsplit("/", 1)[-1] in WRAPPERS:
        return ShellCommand(OPAQUE, one.arguments)
    return one


def parse(command: str) -> Node:
    """The root of ``command``'s syntax tree."""
    return _PARSER.parse(command.encode()).root_node


_ESCAPE = re.compile(r"\\(.)", re.DOTALL)
#: Only a bare `$NAME` or `${NAME}` is the variable's value; any operator, modifier, index,
#: length or default changes it.
_VARIABLE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
#: The two modified forms of HOME that can only yield HOME or abort the command.
_HOME_ONLY = frozenset({"${HOME:?}", "${HOME%/}"})


def node_text(node: Node) -> str:
    return node.text.decode(errors="replace") if node.text else ""


def decode_word(node: Node, variables: Mapping[str, str] | None = None) -> str:
    """The text the shell would hand a program for this word, with :data:`OPAQUE` where
    it cannot be known: quoting and backslashes are removed, a backslash-newline joins the
    lines, ``$HOME`` is ``~`` and any other variable is its value in ``variables`` if it
    has one."""
    kind = node.type
    if kind in ("word", "number"):
        return _ESCAPE.sub(r"\1", node_text(node).replace("\\\n", ""))
    if kind == "raw_string":
        return node_text(node)[1:-1]
    if kind == "ansi_c_string":
        return node_text(node)[2:-1]
    if kind in ("simple_expansion", "expansion"):
        text = node_text(node)
        if text in _HOME_ONLY:
            return "~"
        found = _VARIABLE.fullmatch(text)
        name = (found.group(1) or found.group(2)) if found else None
        if name is not None and variables is not None and name in variables:
            return variables[name]
        return "~" if name == "HOME" else OPAQUE
    if kind in ("string", "concatenation"):
        return "".join(decode_word(c, variables) for c in node.children if c.type != '"')
    if kind == "string_content":
        return node_text(node).replace("\\\n", "")
    return OPAQUE
