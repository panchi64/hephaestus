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
leave it. After a function is called, or ``popd``, it is not known.

What it cannot read is recorded as ``unreadable`` rather than skipped, so the caller can
fail closed. A word built at run time is :data:`OPAQUE` in the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from services.permissions.shell_acts import name_of
from services.permissions.shell_ast import (
    OPAQUE,
    ShellCommand,
    decode_word,
    node_text,
    operands,
    parse,
    unwrap,
)
from services.permissions.shell_feeds import (
    INPUT_WRAPPERS,
    braces,
    feed_words,
    wrapped_by_input,
)

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

#: The directories a command may run in, each as the ``cd`` words applied in order.
type Cwds = frozenset[tuple[str, ...]]

_MAX_NESTING = 4
_MAX_STATES = 16
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
_ALL_DIGITS = frozenset("0123456789-")
_READ_VALUE_FLAGS = frozenset({"-a", "-d", "-i", "-n", "-N", "-p", "-t", "-u", "-O", "-C", "-c"})
#: A backslash-newline the shell removes before it reads a word, unless the backslash is
#: itself escaped. The grammar reads it as a space, which would split `r\<newline>m`.
_LINE_JOIN = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
_UNKNOWN: Cwds = frozenset({(OPAQUE,)})


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
    #: The text of each pipeline, redirect or substitution that gives a shell a script
    #: not seen here.
    piped_shells: list[str] = field(default_factory=list)
    #: Whether the line changes the shell's directory, which later calls then start in.
    changed_directory: bool = False


def scan(command: str) -> Scan:
    result = Scan()
    _Scanner(result).run(command, 0)
    return result


def script_of(arguments: tuple[str, ...]) -> str | None:
    """The script a shell's ``-c`` option is given, or None when it has none."""
    for index, argument in enumerate(arguments):
        if argument.startswith("-") and not argument.startswith("--") and "c" in argument:
            rest = [a for a in arguments[index + 1 :] if a != "--"]
            return rest[0] if rest else None
    return None


def _bound_by(name: str, arguments: tuple[str, ...]) -> list[str]:
    """The variables a builtin sets to something read at run time."""
    found = operands(arguments, _READ_VALUE_FLAGS)
    if name == "read":
        return found or ["REPLY"]
    if name == "printf":
        return [arguments[i + 1] for i, word in enumerate(arguments[:-1]) if word == "-v"]
    if name in ("mapfile", "readarray"):
        return found[-1:] or ["MAPFILE"]
    if name == "getopts":
        return found[1:2]
    if name in ("declare", "typeset", "local", "export", "readonly", "unset"):
        return [word.partition("=")[0] for word in found if "=" not in word or OPAQUE in word]
    return []


class _Scanner:
    def __init__(self, into: Scan) -> None:
        self._out: Scan = into
        self._states: Cwds = frozenset({()})
        self._vars: dict[str, str] = {}
        #: How many conditional or repeated constructs enclose the node being visited.
        self._cond: int = 0
        #: Every variable assigned or bound so far, in order.
        self._assigned: list[str] = []
        self._functions: set[str] = set()
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
        return states if len(states) <= _MAX_STATES else _UNKNOWN

    def _word(self, node: Node) -> str:
        return decode_word(node, self._vars)

    def _visit(self, node: Node, depth: int) -> None:
        kind = node.type
        if kind in _SCOPED:
            self._scoped(node, depth)
        elif kind == "for_statement":
            variable = node.child_by_field_name("variable")
            if variable is not None:
                self._bind(node_text(variable))
            self._conditionally(node, depth)
        elif kind == "declaration_command" or kind == "unset_command":
            for child in node.children:
                if child.type == "variable_name":
                    self._bind(node_text(child))
            self._children(node, depth)
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
            self._out.unreadable.append(node_text(node))
            self._children(node, depth)
        elif kind in _BRANCHING:
            self._conditionally(node, depth)
        else:
            self._children(node, depth)

    def _scoped(self, node: Node, depth: int) -> None:
        """A construct whose directory and variables stay inside it. A function body runs
        later, in this shell, so what it assigns is unknown after it; a subshell's
        variables should not leave it, and are treated the same way."""
        saved = (self._states, dict(self._vars))
        mark = len(self._assigned)
        if node.type == "function_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                self._functions.add(node_text(name))
        self._cond += 1
        self._children(node, depth)
        self._cond -= 1
        self._states, self._vars = saved
        for name in self._assigned[mark:]:
            self._vars[name] = OPAQUE

    def _conditionally(self, node: Node, depth: int) -> None:
        """Visit a construct whose body may or may not run, or may run more than once."""
        entry = self._states
        self._cond += 1
        self._children(node, depth)
        self._cond -= 1
        self._states = self._cap(entry | self._states)

    def _bind(self, name: str) -> None:
        """``name`` now holds something this cannot know."""
        self._vars[name] = OPAQUE
        self._assigned.append(name)

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
        self._cond += 1
        self._visit(right, depth)
        self._cond -= 1

    def _assignment(self, node: Node) -> None:
        parent = node.parent
        if parent is not None and parent.type == "command":
            return
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None:
            return
        key = node_text(name)
        text = self._word(value) if value is not None else ""
        if self._cond > 0 or OPAQUE in text:
            self._bind(key)
        else:
            self._vars[key] = text
            self._assigned.append(key)

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
        self._handle(one, node_text(node), depth, stdin=stdin, feed=feed)

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
        name = name_of(one)
        args = one.arguments
        script: str | None = None
        if name in SHELLS:
            script = script_of(args)
            if script is None and stdin is not None:
                script = stdin
            if script is None and any(OPAQUE in a for a in args):
                self._unseen(source)
        elif name == "eval":
            script = " ".join(args)
        elif name in ("source", "."):
            if any(OPAQUE in a for a in args):
                self._unseen(source)
        elif name == "su":
            script = script_of(args)
        elif name == "trap":
            found = operands(args)
            script = found[0] if found else None
        if script is not None:
            self._run_script(script, name, source, depth)
            return
        for bound in _bound_by(name, args):
            self._bind(bound)
        if name in ("cd", "pushd", "popd"):
            self._change_directory(name, args)
            return
        if name in INPUT_WRAPPERS:
            inner = wrapped_by_input(one, feed)
            if inner is not None:
                self._handle(inner, source, depth)
            return
        if name == "find":
            self._find_execs(args, source, depth)
        self._emit(one)
        if name in self._functions:
            self._out.changed_directory = True
            self._states = _UNKNOWN

    def _unseen(self, source: str) -> None:
        self._out.unreadable.append(source)
        self._out.piped_shells.append(source)

    def _run_script(self, script: str, name: str, source: str, depth: int) -> None:
        if OPAQUE in script:
            self._unseen(source)
        elif name == "eval":
            # The script runs in this shell, so what it changes stays changed.
            self.run(script, depth + 1)
        else:
            self._nested(script, depth)

    def _change_directory(self, name: str, args: tuple[str, ...]) -> None:
        self._out.changed_directory = True
        if name == "popd":
            self._states = _UNKNOWN
            return
        found = operands(args)
        target = OPAQUE if found[:1] == ["-"] else (found[0] if found else "~")
        self._states = self._cap(frozenset(state + (target,) for state in self._states))

    def _emit(self, one: ShellCommand) -> None:
        self._out.steps.append(Step(one, self._states))

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
        if one is None or name_of(one) not in SHELLS:
            return False
        has_operand = any(not a.startswith("-") for a in one.arguments)
        return script_of(one.arguments) is None and not has_operand

    def _pipeline(self, node: Node, depth: int) -> None:
        stages = [c for c in node.children if c.type in ("command", "redirected_statement")]
        for earlier, stage in zip(stages, stages[1:], strict=False):
            body = _body_of(stage)
            if self._reads_stdin_as_script(body):
                self._unseen(node_text(node))
            elif body is not None:
                before = _body_of(earlier)
                producer = self._parts(before)[0] if before is not None else None
                self._feeds[body.id] = None if producer is None else feed_words(producer)
        for child in node.children:
            saved = (self._states, dict(self._vars))
            mark = len(self._assigned)
            self._cond += 1
            self._visit(child, depth)
            self._cond -= 1
            self._states, self._vars = saved
            for name in self._assigned[mark:]:
                self._vars[name] = OPAQUE

    def _redirected(self, node: Node, depth: int) -> None:
        if not self._reads_stdin_as_script(_body_of(node)):
            return
        for child in node.children:
            if child.type == "heredoc_redirect":
                for part in child.children:
                    if part.type == "heredoc_body":
                        self._nested(node_text(part), depth)
            elif child.type == "file_redirect" and any(c.type == "<" for c in child.children):
                self._unseen(node_text(node))


def _body_of(node: Node) -> Node | None:
    if node.type == "command":
        return node
    return next((c for c in node.children if c.type == "command"), None)
