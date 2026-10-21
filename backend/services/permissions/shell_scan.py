"""Every command a shell line would run, at any depth of its grammar.

``shell_ast.shell_reach`` describes what a command *reaches* and refuses the constructs it
has no rule for, so a command inside a subshell, a loop, a function or a ``!`` is not in
its ``commands``. This walk is the other reading: it visits every ``command`` node wherever
it sits, opens the scripts handed to a nested shell (``sh -c``, ``eval``, ``trap``, ``su
-c``, ``find -exec``, a here-string, a here-document), fills in variables assigned earlier
in the line, and returns each command with its quoting and backslashes removed.

**Where the shell stands.** Each command carries the set of directories it may run in,
as the ``cd`` words that may have preceded it. A ``cd`` binds the commands joined after it
by ``&&``; after ``;``, ``||``, a newline or ``&`` the directory may be either, because the
``cd`` may have failed. A ``cd`` in a subshell, a substitution or a pipeline stage does not
leave it.

What it cannot read is recorded as ``unreadable`` rather than skipped, so the caller can
fail closed. A word built at run time is :data:`OPAQUE` in the text.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from tree_sitter import Node

from services.permissions.shell_acts import find_feed
from services.permissions.shell_ast import (
    OPAQUE,
    ShellCommand,
    decode_word,
    operands,
    parse,
    unwrap,
)

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

#: The directories a command may run in, each as the ``cd`` words applied in order.
type Cwds = frozenset[tuple[str, ...]]

_MAX_NESTING = 4
_MAX_STATES = 16
_MAX_BRACE_RESULTS = 64
_WRITE_OPERATORS = frozenset({">", ">>", "&>", "&>>", ">|"})
_SCOPED = frozenset(
    {"subshell", "command_substitution", "process_substitution", "function_definition"}
)
_BRANCHING = frozenset(
    {
        "if_statement",
        "while_statement",
        "until_statement",
        "for_statement",
        "c_style_for_statement",
        "case_statement",
    }
)
_XARGS_VALUE_FLAGS = frozenset(
    {"-I", "-J", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--max-args", "--max-procs"}
)
_PARALLEL_VALUE_FLAGS = frozenset({"-j", "-n", "-N", "-L", "-S", "-a", "--jobs"})
_ALL_DIGITS = frozenset("0123456789-")
#: A backslash-newline the shell removes before it reads a word, unless the backslash is
#: itself escaped. The grammar reads it as a space, which would split `r\<newline>m`.
_LINE_JOIN = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")


@dataclass(frozen=True)
class Step:
    command: ShellCommand
    cwds: Cwds


@dataclass
class Scan:
    steps: list[Step] = field(default_factory=list)
    #: Destinations of output redirects, with the directories they may be relative to.
    writes: list[tuple[str, Cwds]] = field(default_factory=list)
    #: The text of each part that could not be read.
    unreadable: list[str] = field(default_factory=list)
    #: The text of each pipeline or here-string that feeds a shell a script not seen here.
    piped_shells: list[str] = field(default_factory=list)


def scan(command: str) -> Scan:
    result = Scan()
    _Scanner(result).run(command, 0)
    return result


def _text(node: Node) -> str:
    return node.text.decode(errors="replace") if node.text else ""


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


def _command_words(arguments: tuple[str, ...], valued: frozenset[str]) -> tuple[str, ...]:
    """The arguments from the first operand on, past the options (``xargs -n1 rm -rf``)."""
    skip = False
    for index, word in enumerate(arguments):
        if skip:
            skip = False
        elif word in valued:
            skip = True
        elif word == "--":
            return arguments[index + 1 :]
        elif not word.startswith("-"):
            return arguments[index:]
    return ()


class _Scanner:
    def __init__(self, into: Scan) -> None:
        self._out: Scan = into
        self._states: Cwds = frozenset({()})
        self._vars: dict[str, str] = {}
        self._feeds: dict[int, list[str] | None] = {}

    def run(self, text: str, depth: int) -> None:
        if depth > _MAX_NESTING:
            self._out.unreadable.append(text)
            return
        text = _LINE_JOIN.sub(r"\1", text)
        root = parse(text)
        if root.has_error:
            self._out.unreadable.append(text)
        self._visit(root, depth)

    def _nested(self, text: str, depth: int) -> None:
        saved = (self._states, dict(self._vars))
        self.run(text, depth + 1)
        self._states, self._vars = saved[0], saved[1]

    def _cap(self, states: Cwds) -> Cwds:
        return states if len(states) <= _MAX_STATES else frozenset({(OPAQUE,)})

    def _word(self, node: Node) -> str:
        return decode_word(node, self._vars)

    def _visit(self, node: Node, depth: int) -> None:
        kind = node.type
        if kind in _SCOPED:
            saved = (self._states, dict(self._vars))
            self._children(node, depth)
            self._states, self._vars = saved
        elif kind == "command":
            self._command(node, depth)
            self._children(node, depth)
        elif kind == "list":
            self._list(node, depth)
        elif kind == "pipeline":
            self._pipeline(node, depth)
        elif kind == "redirected_statement":
            self._redirected(node, depth)
            self._children(node, depth)
        elif kind == "file_redirect":
            self._redirect(node)
        elif kind == "variable_assignment":
            self._assignment(node)
            self._children(node, depth)
        elif kind == "ERROR":
            self._out.unreadable.append(_text(node))
            self._children(node, depth)
        elif kind in _BRANCHING:
            entry = self._states
            self._children(node, depth)
            self._states = self._cap(entry | self._states)
        else:
            self._children(node, depth)

    def _children(self, node: Node, depth: int) -> None:
        for child in node.children:
            before = self._states
            self._visit(child, depth)
            self._states = self._cap(before | self._states)

    def _list(self, node: Node, depth: int) -> None:
        left, operator, right = node.children[0], node.children[1], node.children[-1]
        entry = self._states
        self._visit(left, depth)
        if operator.type != "&&":
            self._states = self._cap(entry | self._states)
        self._visit(right, depth)

    def _assignment(self, node: Node) -> None:
        parent = node.parent
        if parent is not None and parent.type == "command":
            return
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None:
            return
        key = _text(name)
        text = self._word(value) if value is not None else ""
        if OPAQUE in text:
            _ = self._vars.pop(key, None)
        else:
            self._vars[key] = text

    def _redirect(self, node: Node) -> None:
        target = node.child_by_field_name("destination")
        if target is None:
            return
        operators = {child.type for child in node.children}
        written = self._word(target)
        if operators & _WRITE_OPERATORS or (">&" in operators and not set(written) <= _ALL_DIGITS):
            self._out.writes.append((written, self._states))

    def _parts(self, node: Node) -> tuple[ShellCommand | None, Node | None]:
        """The command a ``command`` node runs, unwrapped, and its here-string if any."""
        name: str | None = None
        arguments: list[str] = []
        herestring: Node | None = None
        for child in node.children:
            if child.type == "command_name":
                name = self._word(child.children[0]) if child.child_count else OPAQUE
            elif child.type == "herestring_redirect":
                herestring = child
            elif child.type not in ("variable_assignment", "file_redirect", "heredoc_redirect"):
                arguments.extend(braces(self._word(child)))
        if name is None:
            return None, herestring
        return unwrap(ShellCommand(name, tuple(arguments))), herestring

    def _command(self, node: Node, depth: int) -> None:
        one, herestring = self._parts(node)
        if one is None:
            return
        stdin = None
        if herestring is not None and herestring.named_child_count:
            stdin = self._word(herestring.named_children[-1])
        feed = self._feeds.get(node.id)
        if feed is None and stdin is not None and OPAQUE not in stdin:
            feed = stdin.split()
        self._handle(one, _text(node), depth, stdin=stdin, feed=feed)

    def _handle(
        self,
        one: ShellCommand,
        source: str,
        depth: int,
        *,
        stdin: str | None = None,
        feed: list[str] | None = None,
    ) -> None:
        if OPAQUE in one.program:
            self._emit(one)
            self._out.unreadable.append(source)
            return
        name = posixpath.basename(one.program)
        args = one.arguments
        script: str | None = None
        if name in SHELLS:
            script = script_of(args)
            if script is None and stdin is not None:
                script = stdin
                if OPAQUE in stdin:
                    self._out.piped_shells.append(source)
        elif name == "eval":
            script = " ".join(args)
        elif name == "su":
            script = script_of(args)
        elif name == "trap":
            found = operands(args)
            script = found[0] if found else None
        if script is not None:
            if OPAQUE in script:
                self._out.unreadable.append(source)
            else:
                self._nested(script, depth)
            return
        if name in ("cd", "pushd"):
            found = operands(args)
            target = OPAQUE if found[:1] == ["-"] else (found[0] if found else "~")
            self._states = self._cap(frozenset(state + (target,) for state in self._states))
            return
        if name in ("xargs", "parallel"):
            self._wrapped_by_input(one, source, depth, feed)
            return
        if name == "find":
            self._find_execs(args, source, depth)
        self._emit(one)

    def _emit(self, one: ShellCommand) -> None:
        self._out.steps.append(Step(one, self._states))

    def _wrapped_by_input(
        self, one: ShellCommand, source: str, depth: int, feed: list[str] | None
    ) -> None:
        """``xargs`` and ``parallel`` run their command on words read at run time, so the
        targets are the words of the producer when it can be read and unknown otherwise."""
        parallel = posixpath.basename(one.program) == "parallel"
        valued = _PARALLEL_VALUE_FLAGS if parallel else _XARGS_VALUE_FLAGS
        words = _command_words(one.arguments, valued)
        extra: list[str] = []
        if parallel:
            for marker in (":::", "::::"):
                if marker in words:
                    split = words.index(marker)
                    extra, words = list(words[split + 1 :]), words[:split]
                    break
            else:
                extra = [OPAQUE] if feed is None else list(feed)
        else:
            extra = [OPAQUE] if feed is None else list(feed)
        if not words:
            return
        inner = unwrap(ShellCommand(words[0], (*words[1:], *extra)))
        self._handle(inner, source, depth)

    def _find_execs(self, args: tuple[str, ...], source: str, depth: int) -> None:
        index = 0
        while index < len(args):
            if args[index] in ("-exec", "-execdir", "-ok", "-okdir"):
                end = index + 1
                while end < len(args) and args[end] not in (";", "+"):
                    end += 1
                words = args[index + 1 : end]
                if words:
                    self._handle(unwrap(ShellCommand(words[0], words[1:])), source, depth + 1)
                index = end
            index += 1

    def _reads_stdin_as_script(self, node: Node | None) -> bool:
        if node is None:
            return False
        one, _ = self._parts(node)
        if one is None or posixpath.basename(one.program) not in SHELLS:
            return False
        has_operand = any(not a.startswith("-") for a in one.arguments)
        return script_of(one.arguments) is None and not has_operand

    def _feed_of(self, node: Node | None) -> list[str] | None:
        """The words a pipeline stage writes, when they can be read: ``echo``, ``printf``
        and ``find``."""
        if node is None:
            return None
        one, _ = self._parts(node)
        if one is None:
            return None
        name = posixpath.basename(one.program)
        words = operands(one.arguments)
        if name == "echo":
            return words
        if name == "printf":
            return words[1:]
        if name == "find":
            return find_feed(one.arguments)
        return None

    def _pipeline(self, node: Node, depth: int) -> None:
        stages = [c for c in node.children if c.type in ("command", "redirected_statement")]
        for earlier, stage in zip(stages, stages[1:], strict=False):
            body = _body_of(stage)
            if self._reads_stdin_as_script(body):
                self._out.unreadable.append(_text(node))
                self._out.piped_shells.append(_text(node))
            elif body is not None:
                self._feeds[body.id] = self._feed_of(_body_of(earlier))
        for child in node.children:
            saved = (self._states, dict(self._vars))
            self._visit(child, depth)
            self._states, self._vars = saved

    def _redirected(self, node: Node, depth: int) -> None:
        if not self._reads_stdin_as_script(_body_of(node)):
            return
        for child in node.children:
            if child.type == "heredoc_redirect":
                for part in child.children:
                    if part.type == "heredoc_body":
                        self._nested(_text(part), depth)


def _body_of(node: Node) -> Node | None:
    if node.type == "command":
        return node
    return next((c for c in node.children if c.type == "command"), None)
