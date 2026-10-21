"""Rebuilding what a restored park cannot carry across a restart.

A park written to disk holds data: the history, the pending calls, the binding. It cannot
hold the agent (a model client, a toolset stack) or the compaction context (a store, the
settings). Each composer of a turn registers a hook for its run ``kind`` that rebuilds them
from the stored facts using the inputs it composed the turn from, so the resume runs the
same turn it parked and not a lookalike.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from runs import Endpoint, Run, RunRegistry
from services.conversations import ConversationBinding

from .compaction_context import CompactionContext
from .factory import TurnAgent
from .naming import TitleContext
from .parking import ParkedTurn


class RecomposeError(Exception):
    """What the parked turn ran on is gone. The message is for the operator."""


@dataclass(frozen=True)
class ParkedFacts:
    """The stored fields a hook rebuilds from."""

    run: Run
    conversation_id: str | None
    binding: ConversationBinding
    workspace_key: str
    vision: bool
    #: The endpoint and model the parked agent held, when they could be read off it.
    endpoint_id: str | None
    model: str | None
    #: Whether the turn was to name its thread when it finished.
    titled: bool


@dataclass(frozen=True)
class Recomposed:
    """What a hook returns: the agent, and the compaction context that carries the deps
    the turn runs it with."""

    agent: TurnAgent
    compaction: CompactionContext | None
    title: TitleContext | None = None
    #: The endpoint the run should lease, as the registry resolves it now.
    endpoint: Endpoint | None = None


type RecomposeHook = Callable[[ParkedFacts], Awaitable[Recomposed]]


@dataclass(frozen=True)
class _Entry:
    hook: RecomposeHook


def register_recompose(registry: RunRegistry, kind: str, hook: RecomposeHook) -> None:
    """Make ``hook`` rebuild parked runs of ``kind``. The registry keeps it opaquely."""
    registry.set_recomposer(kind, _Entry(hook))


async def recompose(registry: RunRegistry, run: Run, parked: ParkedTurn) -> None:
    """Give a restored park its agent and compaction context, in place. A park that still
    holds its agent is left alone. Raises :class:`RecomposeError`."""
    if parked.agent is not None:
        return
    entry = registry.recomposer(run.kind)
    if not isinstance(entry, _Entry):
        raise RecomposeError(f"nothing can rebuild a {run.kind} run after a restart")
    facts = ParkedFacts(
        run=run,
        conversation_id=parked.conversation_id,
        binding=parked.binding,
        workspace_key=parked.workspace_key,
        vision=parked.vision,
        endpoint_id=parked.endpoint_id,
        model=parked.model,
        titled=parked.titled,
    )
    try:
        rebuilt = await entry.hook(facts)
    except RecomposeError:
        raise
    except Exception as exc:
        raise RecomposeError(str(exc) or type(exc).__name__) from exc
    parked.agent = rebuilt.agent
    parked.compaction = rebuilt.compaction
    parked.title = rebuilt.title
    if rebuilt.endpoint is not None:
        run.endpoint = rebuilt.endpoint
