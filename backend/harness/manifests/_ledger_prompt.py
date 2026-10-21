"""The prompt a spawned agent's file changes are filed under."""

from __future__ import annotations

from runs import RunRegistry
from services.subagent_store import SubagentStore

_MAX_DEPTH = 32


async def ledger_prompt_of(
    runs: RunRegistry,
    records: SubagentStore | None,
    owner_id: str,
    conversation_id: str | None,
    *,
    run_id: str | None = None,
) -> str | None:
    """The prompt of the turn now running in the root thread above ``conversation_id``, or
    None when that thread is idle.

    A spawned agent's edits land in its launcher's ledger, so a rewind of the launching
    turn covers them. The walk goes up through the spawn records, so a grandchild files
    under the root thread's turn rather than its own parent's. ``run_id`` names the
    launching run, which is preferred over the thread's newest live run."""
    current = conversation_id
    for _ in range(_MAX_DEPTH):
        if not current:
            return None
        row = await records.by_child(current) if records is not None else None
        if row is None:
            run = runs.get(run_id) if run_id is not None else None
            if run is None or run.conversation_id != current:
                run = runs.active_run_for(current, owner_id)
            return run.prompt_message_id if run is not None else None
        current = row.parent_conversation_id
    return None
