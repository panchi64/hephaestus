"""The seam routes use to stop a thread's sub-agents, declared where both can name it."""

from __future__ import annotations

from typing import Protocol


class SubtreeStopper(Protocol):
    """Stops every live agent below a thread on the operator's behalf."""

    async def stop_below(self, owner_id: str, conversation_id: str) -> list[str]:
        """Mark each live agent below ``conversation_id`` cancelled, then cancel its run
        with the operator as the cause. Returns the handles of the runs that were stopped."""
        ...
