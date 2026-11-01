"""Words a command line does not spell out: brace expansion and what feeds ``xargs``.

``{a,b}`` becomes several words before a program sees it, and ``xargs`` and ``parallel``
run their command on words they read at run time. Both are read here so the walk in
:mod:`shell_scan` can treat the result as ordinary commands: a feed it can read becomes
operands, and one it cannot becomes an unknown word.
"""

from __future__ import annotations

import posixpath

from services.permissions.shell_acts import find_feed
from services.permissions.shell_ast import OPAQUE, ShellCommand, operands, unwrap

_MAX_BRACE_RESULTS = 64
_XARGS_VALUE_FLAGS = frozenset(
    {"-I", "-J", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--max-args", "--max-procs"}
)
_PARALLEL_VALUE_FLAGS = frozenset({"-j", "-n", "-N", "-L", "-S", "-a", "--jobs"})

INPUT_WRAPPERS = frozenset({"xargs", "parallel"})


def braces(text: str) -> list[str]:
    """``text`` with ``{a,b}`` brace expansion applied, recursively. Past the bound the
    rest is one unknown word."""
    words = _expand(text)
    if len(words) > _MAX_BRACE_RESULTS:
        return [*words[:_MAX_BRACE_RESULTS], OPAQUE]
    return words


def _expand(text: str) -> list[str]:
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
                out.extend(_expand(text[:start] + part + text[end + 1 :]))
                if len(out) > _MAX_BRACE_RESULTS:
                    break
            return out
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


def wrapped_by_input(one: ShellCommand, feed: list[str] | None) -> ShellCommand | None:
    """The command ``xargs`` or ``parallel`` runs, given the words it reads (``feed``, or
    None when they cannot be read, which makes them one unknown word)."""
    parallel = posixpath.basename(one.program) == "parallel"
    words = _command_words(one.arguments, _PARALLEL_VALUE_FLAGS if parallel else _XARGS_VALUE_FLAGS)
    extra = [OPAQUE] if feed is None else list(feed)
    if parallel:
        for marker in (":::", "::::"):
            if marker in words:
                split = words.index(marker)
                extra, words = list(words[split + 1 :]), words[:split]
                break
    if not words:
        return None
    return unwrap(ShellCommand(words[0], (*words[1:], *extra)))


def feed_words(one: ShellCommand) -> list[str] | None:
    """The words a pipeline stage writes, when they can be read: ``echo``, ``printf`` and
    ``find``."""
    name = posixpath.basename(one.program)
    words = operands(one.arguments)
    if name == "echo":
        return words
    if name == "printf":
        return words[1:]
    if name == "find":
        return find_feed(one.arguments)
    return None
