"""Every command a shell line would run, at any depth of its grammar.

``shell_ast.shell_reach`` describes what a command *reaches* and refuses the constructs it
has no rule for, so a command inside a subshell, a loop, a function or a ``!`` is not in
its ``commands``. This walk is the other reading: it visits every ``command`` node wherever
it sits, opens the scripts handed to a nested shell (``sh -c``, ``eval``, a here-string,
a here-document), and returns each command with its quoting and backslashes removed.

What it cannot read is recorded as ``unreadable`` rather than skipped, so the caller can
fail closed. A word built at run time is :data:`OPAQUE` in the text.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from tree_sitter import Node

from services.permissions.shell_ast import ShellCommand, parse, unwrap

#: Stands for the part of a word the shell fills in at run time.
OPAQUE = "￼"

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

_MAX_NESTING = 4
_MAX_BRACE_RESULTS = 64
_HOME_VARIABLE = re.compile(r"\$(?:HOME|\{HOME(?:[^A-Za-z0-9_}][^}]*)?\})")
_WRITE_OPERATORS = frozenset({">", ">>", "&>", "&>>", ">|"})
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)


@dataclass
class Scan:
    commands: list[ShellCommand] = field(default_factory=list)
    #: Destinations of output redirects, each with how many commands precede it, so a
    #: relative one can be placed after the ``cd`` that came before.
    writes: list[tuple[str, int]] = field(default_factory=list)
    #: The text of each part that could not be read.
    unreadable: list[str] = field(default_factory=list)


def scan(command: str) -> Scan:
    result = Scan()
    _Scanner(result).run(command, 0)
    return result


def _text(node: Node) -> str:
    return node.text.decode(errors="replace") if node.text else ""


def word(node: Node) -> str:
    """The text the shell would hand a program for this word, with :data:`OPAQUE` where
    it cannot be known."""
    kind = node.type
    if kind in ("word", "number"):
        return _ESCAPE.sub(r"\1", _text(node))
    if kind == "raw_string":
        return _text(node)[1:-1]
    if kind == "ansi_c_string":
        return _text(node)[2:-1]
    if kind in ("simple_expansion", "expansion"):
        return "~" if _HOME_VARIABLE.fullmatch(_text(node)) else OPAQUE
    if kind in ("string", "concatenation"):
        return "".join(word(child) for child in node.children if child.type != '"')
    if kind == "string_content":
        return _text(node)
    return OPAQUE


def braces(text: str) -> list[str]:
    """``text`` with ``{a,b}`` brace expansion applied, recursively and bounded."""
    start = text.find("{")
    while start != -1:
        depth = 0
        end = -1
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            return [text]
        parts = _split_top(text[start + 1 : end])
        if len(parts) > 1:
            out: list[str] = []
            for part in parts:
                out.extend(braces(text[:start] + part + text[end + 1 :]))
                if len(out) >= _MAX_BRACE_RESULTS:
                    break
            return out[:_MAX_BRACE_RESULTS]
        start = text.find("{", start + 1)
    return [text]


def _split_top(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in body:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def script_of(arguments: tuple[str, ...]) -> str | None:
    """The script a shell's ``-c`` option is given, or None when it has none."""
    for index, argument in enumerate(arguments):
        if argument.startswith("-") and not argument.startswith("--") and "c" in argument:
            rest = [a for a in arguments[index + 1 :] if a != "--"]
            return rest[0] if rest else None
    return None


class _Scanner:
    def __init__(self, into: Scan) -> None:
        self._out: Scan = into

    def run(self, text: str, depth: int) -> None:
        if depth > _MAX_NESTING:
            self._out.unreadable.append(text)
            return
        root = parse(text)
        if root.has_error:
            self._out.unreadable.append(text)
        self._visit(root, depth)

    def _visit(self, node: Node, depth: int) -> None:
        kind = node.type
        if kind == "command":
            self._command(node, depth)
        elif kind == "file_redirect":
            self._redirect(node)
        elif kind == "pipeline":
            self._pipeline(node)
        elif kind == "redirected_statement":
            self._redirected(node, depth)
        elif kind == "ERROR":
            self._out.unreadable.append(_text(node))
        for child in node.children:
            self._visit(child, depth)

    def _redirect(self, node: Node) -> None:
        if not any(child.type in _WRITE_OPERATORS for child in node.children):
            return
        target = node.child_by_field_name("destination")
        if target is not None:
            self._out.writes.append((word(target), len(self._out.commands)))

    def _parts(self, node: Node) -> tuple[ShellCommand | None, Node | None]:
        """The command a ``command`` node runs, unwrapped, and its here-string if any."""
        name: str | None = None
        arguments: list[str] = []
        herestring: Node | None = None
        for child in node.children:
            if child.type == "command_name":
                name = word(child.children[0]) if child.child_count else OPAQUE
            elif child.type == "herestring_redirect":
                herestring = child
            elif child.type not in ("variable_assignment", "file_redirect", "heredoc_redirect"):
                arguments.extend(braces(word(child)))
        if name is None:
            return None, herestring
        return unwrap(ShellCommand(name, tuple(arguments))), herestring

    def _command(self, node: Node, depth: int) -> None:
        one, herestring = self._parts(node)
        if one is None:
            return
        if OPAQUE in one.program:
            self._out.unreadable.append(_text(node))
            return
        name = posixpath.basename(one.program)
        script: str | None = None
        if name in SHELLS:
            script = script_of(one.arguments)
            if script is None and herestring is not None and herestring.named_child_count:
                script = word(herestring.named_children[-1])
        elif name == "eval":
            script = " ".join(one.arguments)
        if script is not None:
            if OPAQUE in script:
                self._out.unreadable.append(_text(node))
            else:
                self.run(script, depth + 1)
            return
        self._out.commands.append(one)

    def _reads_stdin_as_script(self, node: Node | None) -> bool:
        if node is None:
            return False
        one, _ = self._parts(node)
        if one is None or posixpath.basename(one.program) not in SHELLS:
            return False
        has_operand = any(not a.startswith("-") for a in one.arguments)
        return script_of(one.arguments) is None and not has_operand

    def _pipeline(self, node: Node) -> None:
        stages = [c for c in node.children if c.type in ("command", "redirected_statement")]
        for stage in stages[1:]:
            if self._reads_stdin_as_script(_body_of(stage)):
                self._out.unreadable.append(_text(node))
                return

    def _redirected(self, node: Node, depth: int) -> None:
        if not self._reads_stdin_as_script(_body_of(node)):
            return
        for child in node.children:
            if child.type == "heredoc_redirect":
                for part in child.children:
                    if part.type == "heredoc_body":
                        self.run(_text(part), depth + 1)


def _body_of(node: Node) -> Node | None:
    if node.type == "command":
        return node
    return next((c for c in node.children if c.type == "command"), None)
