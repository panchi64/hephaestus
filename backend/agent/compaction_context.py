"""Everything a fold needs, settled once per turn.

Compaction can fire from three places inside one turn — the prelude's projected trigger,
the in-turn recovery after a provider refuses an over-long request, and the same recovery
on a resumed (previously parked) turn — and from the operator's own button. Each needs the
same facts, and none of them is derivable where the in-turn recovery fires: ``drive_turn``
has no settings store and no policy, and a parked turn resumes minutes later in a
different orchestrator entirely.

So the facts travel as one frozen value, built where they are known and carried to where
they are used — including onto :class:`~agent.parking.ParkedTurn`, so an approval resume
can recover from an overflow exactly as the original turn would have. A ``None`` context
means "this turn cannot fold" (a stateless run), which is a state the recovery path has to
handle anyway.

**The summary is written by the turn's own agent**, so the context carries that agent and
the deps it runs with rather than a model of its own: the summarizer's request is the
turn's replay plus one message, sent on the same brief and the same tool array, which is
what lets a local engine reuse the prefix it already holds (``agent/compaction_summary.py``).

It lives in its own module rather than in the engine because ``parking.py`` holds one and
the engine imports ``parking``; the other direction would be a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import Settings
from services.conversations import ConversationStore
from tools import RunDeps

from .factory import TurnAgent
from .summarize import AutoCompactPolicy


@dataclass(frozen=True)
class CompactionContext:
    """The resources and policy one turn's folds run under."""

    store: ConversationStore
    conversation_id: str
    # The agent the turn runs on and the deps it runs it with — the summary is that agent
    # continuing its own conversation, so its request renders the brief and offers the
    # tools a turn's would. The in-turn recovery swaps in the live segment's pair, since a
    # context that came off a park holds the deps of the run that parked.
    agent: TurnAgent
    deps: RunDeps
    settings: Settings
    # When the automatic triggers fire — whether compaction is on for this thread, and at
    # what share of the window. Read only by the triggers, never by the fold itself, so the
    # operator's own "compact now" (which no trigger decides) carries none, and a trigger
    # handed a context without one does not fire.
    policy: AutoCompactPolicy | None = None


def build_compaction_context(
    *,
    store: ConversationStore | None,
    conversation_id: str | None,
    agent: TurnAgent,
    deps: RunDeps,
    settings: Settings,
    policy: AutoCompactPolicy | None = None,
) -> CompactionContext | None:
    """The context a fold runs under, or ``None`` when this thread cannot fold at all — no
    conversation to fold (a stateless run).

    **Every trigger builds its context here**, so a caller cannot assemble one from the
    fields it happens to know about — which is how the manual fold once came to run on a
    different model than an automatic one."""
    if store is None or conversation_id is None:
        return None
    return CompactionContext(
        store=store,
        conversation_id=conversation_id,
        agent=agent,
        deps=deps,
        settings=settings,
        policy=policy,
    )
