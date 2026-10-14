"""Working directly in a project's own checkout, one thread at a time.

Two threads editing one checkout at once would each work against a tree the other keeps
changing, so a checkout has one holder. Unlike a worktree's, the hold lasts only while the
holder has unfinished work: a claim is released by asking whether its run has ended
(``LiveWork``), never by a hand-back the run might not live to make.
"""

from __future__ import annotations

import asyncio

from services.sandbox.session import LiveWork

from .worktree import WorktreeBusyError


class CheckoutBusyError(WorktreeBusyError):
    """Another conversation is working in this project's checkout right now."""


class CheckoutHolders:
    """Which conversation is working in each project's checkout, and with which runs."""

    def __init__(self) -> None:
        self._holders: dict[str, tuple[str, list[LiveWork]]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self, project_id: str, conversation_id: str, claim: LiveWork | None) -> None:
        """Hold the checkout for ``conversation_id``, refusing while another one holds it.

        A conversation's own delegated runs pass under its id, so a sub-agent sharing its
        parent's checkout is not a rival for it.
        """
        async with self._lock:
            holder = self._holders.get(project_id)
            if holder is not None:
                owner, claims = holder
                live = [work for work in claims if not work.is_terminal]
                if owner != conversation_id and live:
                    raise CheckoutBusyError(
                        "Another conversation is working in this project's folder right now. "
                        + "Open a worktree to work on your own branch beside it, or wait for "
                        + "that one to finish."
                    )
                claims = live if owner == conversation_id else []
            else:
                claims = []
            if claim is not None and claim not in claims:
                claims.append(claim)
            self._holders[project_id] = (conversation_id, claims)

    def holder(self, project_id: str) -> str | None:
        """The conversation with unfinished work in this checkout, if any."""
        entry = self._holders.get(project_id)
        if entry is None or all(work.is_terminal for work in entry[1]):
            return None
        return entry[0]
