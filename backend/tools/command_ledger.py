"""The read ledger's rule for shell and host commands (``read_ledger.py``).

A command that would overwrite or rewrite an existing file of the home is refused before
any of it runs, unless the file was read in this conversation and has not changed since.
A command that prints a file to its own output counts as reading it, at the contents it
printed. Once a foreground command finishes, the files it wrote are recorded at their new
hashes, except where the model cannot know what they hold: an unread file it only appended
to or touched, a download, and a copy of a file it has not read. A part of a command the
parser cannot read is not checked: an edit after it still meets the harness's hash check.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from core.config import get_settings
from services.home.table import RunHome
from services.permissions.guard_paths import Places
from services.permissions.guards import COMMAND_TOOLS, FILE_REWRITE_TOOLS, HOST_COMMAND_TOOL
from services.permissions.shell_acts import (
    landing_paths,
    name_of,
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

#: Their files hold what the network sent, which the model has not seen.
_FETCHERS = frozenset({"curl", "wget"})

#: A file's content hash and size in bytes.
type _Seen = tuple[str, int]


@dataclass
class CommandCheck:
    """What a command reads and changes among the files the ledger knows by host path."""

    ledger: ReadLedger
    thread: str
    refusal: str | None = None
    #: Printed files, at the hashes they had before the command ran.
    reads: dict[str, str] = field(default_factory=dict)
    #: Written files whose contents after the command count as known.
    writes: set[str] = field(default_factory=set)

    async def record(self) -> None:
        """Record what the command read and wrote; a refused command never ran."""
        if self.refusal is not None:
            return
        after = await asyncio.to_thread(_seen, self.writes)
        for key, digest in (self.reads | {k: seen[0] for k, seen in after.items()}).items():
            self.ledger.record(self.thread, key, digest)


@dataclass
class _Paths:
    """A command's files by ledger key, before anything is known about them."""

    #: Overwritten or rewritten files, with the path as the command spells it.
    rewrites: dict[str, str] = field(default_factory=dict)
    written: set[str] = field(default_factory=set)
    printed: set[str] = field(default_factory=set)
    #: Each copy's landing keys and its source keys (None for a source it cannot place).
    copies: list[tuple[list[str], list[str | None]]] = field(default_factory=list)

    def every(self) -> set[str]:
        sources = {s for _, keys in self.copies for s in keys if s is not None}
        landed = {k for keys, _ in self.copies for k in keys}
        return self.rewrites.keys() | self.written | self.printed | sources | landed


async def ledger_check(
    deps: RunDeps,
    tool: str,
    args: Mapping[str, object],
    home: RunHome | None,
    cwd: str | None,
) -> CommandCheck | None:
    """The ledger's view of a command about to run, or None when it keeps none of it:
    not a command tool, no ledger or home, or a command too deeply nested to read."""
    command = args.get("command")
    ledger = ledger_of(deps.caps)
    if tool not in COMMAND_TOOLS or not isinstance(command, str):
        return None
    if ledger is None or home is None:
        return None
    paths = _paths(tool, command, args.get("reach") == "host", home, cwd)
    if paths is None:
        return None
    before = await asyncio.to_thread(_seen, paths.every())
    check = CommandCheck(ledger, thread_of(deps))

    def fresh(key: str | None) -> bool:
        known = ledger.get(check.thread, key) if key is not None else None
        return key in before and known in (BINARY, before[key][0])

    check.refusal = _refusal(check, paths.rewrites, before)
    shown = {key: before[key] for key in paths.printed if key in before}
    # Output past the spill size reaches the model as a preview, so the file was not seen.
    if sum(size for _, size in shown.values()) <= get_settings().tool_output_spill_chars:
        check.reads = {key: digest for key, (digest, _) in shown.items()}

    def settles(key: str) -> bool:
        """Whether the file is new, or its contents before the command were known."""
        return (
            key in paths.rewrites or key not in before or ledger.get(check.thread, key) is not None
        )

    check.writes = {key for key in paths.written if settles(key)}
    for landed, sources in paths.copies:
        if sources and all(fresh(source) for source in sources):
            check.writes.update(key for key in landed if settles(key))
    return check


async def ledger_refusal(
    deps: RunDeps,
    tool: str,
    args: Mapping[str, object],
    home: RunHome | None,
    cwd: str | None,
) -> str | None:
    """The refusal the read ledger gives a file write or a command, for a caller deciding
    before the operator is asked. ``tool`` is the namespaced name."""
    if tool in FILE_REWRITE_TOOLS:
        return await unread_refusal(deps, tool, dict(args), home)
    check = await ledger_check(deps, tool, args, home, cwd)
    return check.refusal if check is not None else None


def _paths(
    tool: str, command: str, reach_host: bool, home: RunHome, cwd: str | None
) -> _Paths | None:
    try:
        found = scan(command)
    except RecursionError, UnicodeError:
        return None
    host = tool == HOST_COMMAND_TOOL or reach_host
    settings = get_settings()
    if tool == HOST_COMMAND_TOOL:
        start = Places.for_host_command(home, Path.home(), settings)
    else:
        start = Places.at(home, Path.home(), settings, cwd)
    paths = _Paths()

    def place(text: str, directories: tuple[str, ...]) -> str | None:
        return start.walk(directories).ledger_key(text, host=host)

    def placed(texts: Iterable[str], directories: tuple[str, ...]) -> list[str]:
        return [key for text in texts if (key := place(text, directories)) is not None]

    for step in found.steps:
        one = step.command
        moved = transfer(one)
        landing = landing_paths(moved) if moved is not None else []
        rewritten = [*rewrite_targets(one), *([] if moved and moved.keeps_existing else landing)]
        fetched = name_of(one) in _FETCHERS
        written = [] if fetched else [t.path for t in write_targets(one) if t.kind == "write"]
        for directories in step.cwds:
            for text in rewritten:
                if (key := place(text, directories)) is not None:
                    _ = paths.rewrites.setdefault(key, text)
            paths.written.update(placed(written, directories))
            if step.shown:
                paths.printed.update(placed(printed_files(one), directories))
            if moved is not None and landing:
                sources = [place(source, directories) for source in moved.sources]
                paths.copies.append((placed(landing, directories), sources))
    for redirect in found.writes:
        for directories in redirect.cwds:
            key = place(redirect.path, directories)
            if key is None:
                continue
            paths.written.add(key)
            if not redirect.append:
                _ = paths.rewrites.setdefault(key, redirect.path)
    return paths


def _refusal(
    check: CommandCheck, rewrites: Mapping[str, str], before: Mapping[str, _Seen]
) -> str | None:
    unread: list[str] = []
    stale: list[str] = []
    for key, text in rewrites.items():
        if key not in before:
            continue
        known = check.ledger.get(check.thread, key)
        if known is None:
            unread.append(text)
        elif known not in (BINARY, before[key][0]):
            stale.append(text)
    if unread:
        return unread_message(*unread, *stale)
    return stale_message(*stale) if stale else None


def _seen(keys: Iterable[str]) -> dict[str, _Seen]:
    """The hash the file tools report (SHA-256, first 12 hex characters) of each readable file."""
    found: dict[str, _Seen] = {}
    for key in keys:
        path = Path(key)
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()[:12]
                found[key] = (digest, handle.tell())
        except OSError:
            continue
    return found
