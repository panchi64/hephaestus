"""What the agent last saw of each file, so an edit can be checked against it.

An edit or an overwrite of an existing file needs a read behind it in the same
conversation, and the harness then refuses it if the file changed since (``expected_hash``).
Shell and host commands are held to the same rule (``command_ledger.py``). Losing the
ledger on a restart only makes the model read again. The ledger is bounded, and the oldest
conversation is forgotten first. A fold resets a conversation's record to the files it
re-read (``agent/compaction_restore.py``): the model no longer holds the rest.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart

from core.container import ServiceContainer
from core.diagnostics import render_tool_error

from .deps import RunDeps

#: Recorded for a file the harness read as binary, which it reports no hash for. A change
#: to such a file is permitted once it has been read, and is not pinned.
BINARY = "binary"

_MAX_CONVERSATIONS = 256
_MAX_PATHS = 2048

#: The hash a read reports in its header line, and the one a write or edit reports last.
_READ_HASH = re.compile(r"\A\[[^\n]*\| hash:([0-9a-f]{12})\]\n")
_WRITE_HASH = re.compile(r"\A(?:Wrote|Edited) .*\[hash:([0-9a-f]{12})\]\Z", re.DOTALL)
_BINARY_READ = re.compile(r"\A\[Binary file: ")


@dataclass(frozen=True)
class Seen:
    """One file's record: its content hash, and the home path the model named it by (empty
    for a record seeded from history, which knows only the host path)."""

    content_hash: str
    path: str = ""


class ReadLedger:
    """``{conversation: {absolute path: Seen}}``, in memory, most recently touched last."""

    def __init__(self) -> None:
        self._seen: OrderedDict[str, OrderedDict[str, Seen]] = OrderedDict()

    def get(self, conversation: str, path: str) -> str | None:
        paths = self._seen.get(conversation)
        if paths is None:
            return None
        self._seen.move_to_end(conversation)
        seen = paths.get(path)
        return None if seen is None else seen.content_hash

    def record(self, conversation: str, path: str, content_hash: str, *, named: str = "") -> None:
        paths = self._seen.setdefault(conversation, OrderedDict())
        self._seen.move_to_end(conversation)
        paths[path] = Seen(content_hash, named)
        paths.move_to_end(path)
        while len(paths) > _MAX_PATHS:
            _ = paths.popitem(last=False)
        while len(self._seen) > _MAX_CONVERSATIONS:
            _ = self._seen.popitem(last=False)

    def recent(self, conversation: str, limit: int) -> list[str]:
        """The home paths of the ``limit`` text files most recently read or written, newest
        first."""
        paths = self._seen.get(conversation)
        if paths is None or limit <= 0:
            return []
        named = [
            seen.path
            for seen in reversed(paths.values())
            if seen.path and seen.content_hash != BINARY
        ]
        return named[:limit]

    def reset(self, conversation: str, keep: Mapping[str, Seen]) -> None:
        """Forget every file but ``keep`` (host path to its fresh record), oldest first."""
        _ = self._seen.pop(conversation, None)
        for host, seen in keep.items():
            self.record(conversation, host, seen.content_hash, named=seen.path)


def ledger_of(caps: ServiceContainer) -> ReadLedger | None:
    """The app's ledger, or None for a run assembled without one."""
    return caps.get_optional(ReadLedger)


def thread_of(deps: RunDeps) -> str:
    return deps.conversation_id or deps.workspace_key


def unread_message(*paths: str) -> str:
    it = "it" if len(paths) == 1 else "them"
    return render_tool_error(
        f"Read {', '.join(paths)} before changing {it}.",
        hints=[f"read {it} with files_read_file (or cat) in this conversation, then retry"],
    )


def stale_message(*paths: str) -> str:
    it = "it" if len(paths) == 1 else "them"
    return render_tool_error(
        f"{', '.join(paths)} changed since you read {it}.", hints=[f"read {it} again, then retry"]
    )


def seed_from_history(
    ledger: ReadLedger,
    conversation: str,
    messages: Sequence[ModelMessage],
    host_path: Callable[[str, dict[str, object]], str | None],
) -> None:
    """Record what the file tool results in ``messages`` reported, for a path the ledger has
    no record of. A restart empties the ledger while a parked turn's history still holds the
    reads; a newer record is never overwritten. ``host_path`` maps a call's tool name and
    arguments to the ledger's key for the file."""
    calls: dict[str, tuple[str, dict[str, object]]] = {}
    latest: dict[str, str] = {}
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = (part.tool_name, part.args_as_dict())
            elif isinstance(part, ToolReturnPart) and part.tool_call_id in calls:
                tool, args = calls[part.tool_call_id]
                bare = tool.removeprefix("files_")
                fresh = hash_reported(bare, part.content)
                key = host_path(bare, args) if fresh is not None else None
                if fresh is not None and key is not None:
                    latest[key] = fresh
    for key, fresh in latest.items():
        if ledger.get(conversation, key) is None:
            ledger.record(conversation, key, fresh)


def hash_reported(tool: str, result: object) -> str | None:
    """The content hash a successful ``read_file``, ``write_file`` or ``edit_file`` result
    reports (:data:`BINARY` for a binary read), or None for a failure or any other tool."""
    if not isinstance(result, str):
        return None
    if tool == "read_file":
        if _BINARY_READ.match(result):
            return BINARY
        found = _READ_HASH.match(result)
    elif tool in ("write_file", "edit_file"):
        found = _WRITE_HASH.match(result)
    else:
        return None
    return found.group(1) if found else None
