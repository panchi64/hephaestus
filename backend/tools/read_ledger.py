"""What the agent last saw of each file, so an edit can be checked against it.

An edit or an overwrite of an existing file needs a read behind it in the same
conversation, and the harness then refuses it if the file changed since (``expected_hash``).
Losing the ledger on a restart only makes the model read again. The ledger is bounded, and
the oldest conversation is forgotten first.
"""

from __future__ import annotations

import re
from collections import OrderedDict

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


class ReadLedger:
    """``{conversation: {absolute path: content hash}}``, in memory."""

    def __init__(self) -> None:
        self._seen: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()

    def get(self, conversation: str, path: str) -> str | None:
        paths = self._seen.get(conversation)
        if paths is None:
            return None
        self._seen.move_to_end(conversation)
        return paths.get(path)

    def record(self, conversation: str, path: str, content_hash: str) -> None:
        paths = self._seen.setdefault(conversation, OrderedDict())
        self._seen.move_to_end(conversation)
        paths[path] = content_hash
        paths.move_to_end(path)
        while len(paths) > _MAX_PATHS:
            _ = paths.popitem(last=False)
        while len(self._seen) > _MAX_CONVERSATIONS:
            _ = self._seen.popitem(last=False)


def ledger_of(caps: ServiceContainer) -> ReadLedger | None:
    """The app's ledger, or None for a run assembled without one."""
    return caps.get_optional(ReadLedger)


def thread_of(deps: RunDeps) -> str:
    return deps.conversation_id or deps.workspace_key


def unread_message(path: str) -> str:
    return render_tool_error(
        f"Read {path} before changing it.",
        hints=["read it with files_read_file in this conversation, then retry"],
    )


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
