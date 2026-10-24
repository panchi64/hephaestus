"""The read ledger's rule for shell and host commands (``read_ledger.py``).

A command that would overwrite or rewrite an existing file of the home is refused before
any of it runs, unless the file was read in this conversation and has not changed since.
A command that prints a file to its own output counts as reading it. Once a foreground
command finishes, the files it printed and wrote are recorded at their new hashes, except
an unread file it only appended to or touched. A part of a command the parser cannot read
is not checked: an edit after it still meets the harness's hash check.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from core.config import get_settings
from services.home.table import RunHome
from services.permissions.guard_paths import Places
from services.permissions.guards import COMMAND_TOOLS, FILE_REWRITE_TOOLS, HOST_COMMAND_TOOL
from services.permissions.shell_acts import (
    landing_paths,
    printed_files,
    rewrite_targets,
    transfer,
    write_targets,
)
from services.permissions.shell_scan import scan

from .deps import RunDeps
from .files import unread_refusal
from .read_ledger import BINARY, ReadLedger, ledger_of, stale_message, thread_of, unread_message

#: The namespaced tools whose calls the read ledger may refuse.
LEDGER_TOOLS = FILE_REWRITE_TOOLS | COMMAND_TOOLS


@dataclass
class CommandCheck:
    """What a command reads and changes among the files the ledger knows by host path."""

    ledger: ReadLedger
    thread: str
    refusal: str | None = None
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    #: Written files that existed unread before the command, which it did not rewrite.
    unread: set[str] = field(default_factory=set)

    def record(self) -> None:
        """Record the files at their hashes now, after the command has run. A refused
        command never ran, so records nothing."""
        if self.refusal is not None:
            return
        for key in self.reads | (self.writes - self.unread):
            digest = _hash_of(Path(key))
            if digest is not None:
                self.ledger.record(self.thread, key, digest)


def ledger_check(
    deps: RunDeps,
    tool: str,
    args: Mapping[str, object],
    home: RunHome | None,
    cwd: str | None,
    *,
    cwd_known: bool = True,
) -> CommandCheck | None:
    """The ledger's view of a command about to run, or None when it keeps none of it:
    not a command tool, no ledger or home, a working directory an earlier call in the
    batch may have moved, or a command too deeply nested to read."""
    command = args.get("command")
    ledger = ledger_of(deps.caps)
    if tool not in COMMAND_TOOLS or not isinstance(command, str) or not cwd_known:
        return None
    if ledger is None or home is None:
        return None
    try:
        found = scan(command)
    except RecursionError, UnicodeError:
        return None
    host = tool == HOST_COMMAND_TOOL or args.get("reach") == "host"
    settings = get_settings()
    if tool == HOST_COMMAND_TOOL:
        start = Places.for_host_command(home, Path.home(), settings)
    else:
        start = Places.at(home, Path.home(), settings, cwd)
    check = CommandCheck(ledger, thread_of(deps))
    rewrites: dict[str, str] = {}

    def place(text: str, directories: tuple[str, ...]) -> str | None:
        return start.walk(directories).ledger_key(text, host=host)

    for step in found.steps:
        one = step.command
        moved = transfer(one)
        landing = landing_paths(moved) if moved is not None else []
        rewritten = [*rewrite_targets(one), *landing]
        written = [*(t.path for t in write_targets(one) if t.kind == "write"), *landing]
        printed = printed_files(one) if step.shown else []
        for directories in step.cwds:
            for text in rewritten:
                if (key := place(text, directories)) is not None:
                    _ = rewrites.setdefault(key, text)
            check.writes.update(k for t in written if (k := place(t, directories)) is not None)
            check.reads.update(k for t in printed if (k := place(t, directories)) is not None)
    for redirect in found.writes:
        for directories in redirect.cwds:
            key = place(redirect.path, directories)
            if key is None:
                continue
            check.writes.add(key)
            if not redirect.append:
                _ = rewrites.setdefault(key, redirect.path)
    for key in check.writes - rewrites.keys():
        if Path(key).is_file() and ledger.get(check.thread, key) is None:
            check.unread.add(key)
    check.refusal = _refusal(check, rewrites)
    return check


def command_refusal(
    deps: RunDeps,
    tool: str,
    args: Mapping[str, object],
    home: RunHome | None,
    cwd: str | None,
    *,
    cwd_known: bool = True,
) -> str | None:
    """The refusal :func:`ledger_check` gives, for a caller deciding before anyone is asked."""
    check = ledger_check(deps, tool, args, home, cwd, cwd_known=cwd_known)
    return check.refusal if check is not None else None


async def ledger_refusal(
    deps: RunDeps,
    tool: str,
    args: Mapping[str, object],
    home: RunHome | None,
    cwd: str | None,
    *,
    cwd_known: bool = True,
) -> str | None:
    """The refusal the read ledger gives a file write or a command, for a caller deciding
    before the operator is asked. ``tool`` is the namespaced name."""
    if tool in FILE_REWRITE_TOOLS:
        return await unread_refusal(deps, tool, dict(args), home)
    return command_refusal(deps, tool, args, home, cwd, cwd_known=cwd_known)


def _refusal(check: CommandCheck, rewrites: Mapping[str, str]) -> str | None:
    for key, text in rewrites.items():
        path = Path(key)
        if not path.is_file():
            continue
        known = check.ledger.get(check.thread, key)
        if known is None:
            return unread_message(text)
        if known != BINARY and known != _hash_of(path):
            return stale_message(text)
    return None


def _hash_of(path: Path) -> str | None:
    """The content hash the file tools report (SHA-256 of the raw bytes, first 12 hex
    characters), or None for a path that is not a readable file."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None
