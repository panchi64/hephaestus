"""What the agent last saw of each file, so an edit can be checked against it.

An edit or an overwrite of an existing file needs a read behind it in the same
conversation, and the harness then refuses it if the file changed since (``expected_hash``).
Losing the ledger on a restart only makes the model read again.
"""

from __future__ import annotations

import re

from core.container import ServiceContainer

#: The hash a read reports in its header line, and the one a write or edit reports last.
_READ_HASH = re.compile(r"\A\[[^\n]*\| hash:([0-9a-f]{12})\]\n")
_WRITE_HASH = re.compile(r"\A(?:Wrote|Edited) .*\[hash:([0-9a-f]{12})\]\Z", re.DOTALL)


class ReadLedger:
    """``{conversation: {absolute path: content hash}}``, in memory."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, str]] = {}

    def get(self, conversation: str, path: str) -> str | None:
        return self._seen.get(conversation, {}).get(path)

    def record(self, conversation: str, path: str, content_hash: str) -> None:
        self._seen.setdefault(conversation, {})[path] = content_hash


def ledger_of(caps: ServiceContainer) -> ReadLedger:
    """The run's ledger, registered on first use when the bag has none."""
    ledger = caps.get_optional(ReadLedger)
    if ledger is None:
        ledger = ReadLedger()
        caps.add(ledger)
    return ledger


def hash_reported(tool: str, result: object) -> str | None:
    """The content hash a successful ``read_file``, ``write_file`` or ``edit_file`` result
    reports, or None for a failure or any other tool."""
    if not isinstance(result, str):
        return None
    if tool == "read_file":
        found = _READ_HASH.match(result)
    elif tool in ("write_file", "edit_file"):
        found = _WRITE_HASH.match(result)
    else:
        return None
    return found.group(1) if found else None
