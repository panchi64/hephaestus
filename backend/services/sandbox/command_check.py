"""The refusals a shell command meets before anything is spawned."""

from __future__ import annotations

import os
import re
import shlex

from pydantic_ai.exceptions import ModelRetry
from pydantic_ai_harness.shell import Shell

from core.diagnostics import render_tool_error

#: Destructive programs (`rm`, `dd`, `mkfs`, `shutdown`, …) refused by name. Taken from the
#: harness rather than restated: it is a guardrail against a slip, not a boundary, and two
#: copies of a guardrail is one copy that stops being updated when the other grows an entry.
_DENIED_COMMANDS: frozenset[str] = frozenset(Shell().denied_commands)

#: Programs that want a terminal. Here they would not fail — they would hang until the
#: timeout, spending the turn on nothing.
_INTERACTIVE = re.compile(
    r"^(vi|vim|nano|emacs|less|more|top|htop|man)\b|^sudo\s|^(passwd|ssh|telnet|ftp)\b"
)


def check_command(command: str) -> None:
    """Refuse what this shell will not run at all, before anything is spawned. Best-effort,
    and not the boundary: a determined agent walks around a name check with an interpreter.
    The boundary is the permission layer in front of it."""
    if "\x00" in command:
        raise ModelRetry(
            render_tool_error(
                "the command contains a NUL byte",
                cause="a process cannot be passed one",
                hints=["remove it and run the command again"],
            )
        )
    try:
        # `os.fsencode`, not `str.encode`: the spawn encodes with the filesystem encoding
        # and `surrogateescape`, so plain UTF-8 here would reject commands the OS runs.
        _ = os.fsencode(command)
    except UnicodeEncodeError as exc:
        raise ModelRetry(
            "The command contains characters that cannot be encoded for the operating system."
        ) from exc
    if _INTERACTIVE.match(command.strip()):
        raise PermissionError(f"Interactive commands are not allowed. Command: {command!r}")
    try:
        tokens = shlex.split(command)
    except ValueError:
        return
    if tokens and tokens[0] in _DENIED_COMMANDS:
        raise PermissionError(f"Command {tokens[0]!r} is denied.")
