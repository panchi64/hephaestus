"""Reshaping a message list before it is handed to a model, or reading a fact back out of
one, before either reaches the store.

Surgeries the chat engine performs on ``list[ModelMessage]``, none of which needs a Run, a
store, or anything else the orchestrators carry. They are here rather than inline because
each encodes a fact about how the library or a provider behaves — a dangling tool call is
a provider error, adjacent requests get merged at wire-prep, a revealed tool is remembered
only by the history that reveals it — and those facts are worth finding in one place when
one of them changes.

Every one returns new objects. The store's in-memory tree hands out its messages by
reference, so mutating one in place would corrupt the durable history of every later read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.messages import (
    NativeToolSearchReturnPart,
    ToolAvailabilityDeltaPart,
    ToolSearchReturnPart,
)


def drop_dangling_tool_calls(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Strip a trailing tool call that never received its result.

    A turn stopped at a bound (usage limit, loop guard, timeout) can leave history ending
    on a ``ModelResponse`` whose ``ToolCallPart`` has no matching ``ToolReturnPart`` — the
    call was requested but the bound tripped before it ran. Persisting that and replaying it
    on the next turn is a provider error (an assistant tool call with no following tool
    result → HTTP 400), which would break every later turn in the thread. Since this is the
    final message, any tool call in it is necessarily unanswered: drop those parts, and the
    whole message if nothing else remains."""
    if not messages or not isinstance(messages[-1], ModelResponse):
        return messages
    last = messages[-1]
    kept = [p for p in last.parts if not isinstance(p, ToolCallPart)]
    if len(kept) == len(last.parts):
        return messages
    if kept:
        return [*messages[:-1], replace(last, parts=kept)]
    return messages[:-1]


def with_tail_context(messages: list[ModelMessage], texts: list[str]) -> list[ModelMessage]:
    """Append per-turn context to the trailing user request — the *model's view only*.

    The regenerate path re-runs a history that already ends in the user request, so
    there is no fresh ``user_prompt`` to carry the prompt-context providers' output;
    instead it rides on that trailing request here. Rebuilds via ``replace`` (never
    mutates — the store's in-memory tree shares these objects), and since the caller
    persists only ``messages[start:]``, a message replaced *before* ``start`` never
    reaches the durable history. No trailing user request (defensive) ⇒ unchanged."""
    if not messages or not isinstance(messages[-1], ModelRequest):
        return messages
    last = messages[-1]
    for index, part in enumerate(last.parts):
        if isinstance(part, UserPromptPart):
            content = part.content if isinstance(part.content, list) else [part.content]
            parts = list(last.parts)
            parts[index] = replace(part, content=[*content, *texts])
            return [*messages[:-1], replace(last, parts=parts)]
    return messages


def merge_consecutive_requests(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Collapse adjacent ``ModelRequest``s the way Pydantic AI will anyway, **before**
    ``start`` is measured against the list.

    ``finalize`` persists ``result.all_messages()[start:]`` with ``start`` the length of
    the history we handed in. That only holds while the library gives the history back at
    the length we supplied it — and it does not: preparing the wire format merges
    consecutive requests (most chat APIs cannot carry two user messages in a row), so
    ``all_messages()`` comes back *shorter* than what went in and ``start`` silently points
    one message too far, dropping the operator's own message from the turn it persists.

    Two things produce adjacent requests here, and both are load-bearing: a compaction
    checkpoint followed by the user prompt of the turn after it, and
    :func:`split_injected_requests`' own output replayed on the *next* turn. Normalizing
    up front costs nothing (the library was going to do exactly this) and makes the index
    honest in both cases. New objects throughout — the store's in-memory tree shares these
    messages, so nothing here may mutate one in place.

    **Why not use the library's own boundary.** ``AgentRunResult.new_messages()`` slices at
    an index Pydantic AI maintains for exactly this hazard, and it is correct — for one
    agent run. Ours is not one agent run. A single operator-facing turn can span several:
    a continuation after the operator queued more text mid-answer, an approval resume, a
    verifier correction. Each re-enters with a rebuilt history, so the library's index
    marks the start of the *last* run's new messages, not of the turn — on a corrective
    re-attempt it points two messages into a turn that began at zero. On top of that, four
    of the five things measured against ``start`` (the timeout flush, the cancel flush, the
    error flush, the park) happen where no result exists to ask. So ``start`` stays ours,
    and this keeps it honest."""
    out: list[ModelMessage] = []
    for message in messages:
        if isinstance(message, ModelRequest) and out and isinstance(out[-1], ModelRequest):
            out[-1] = replace(out[-1], parts=[*out[-1].parts, *message.parts])
            continue
        out.append(message)
    return out


@dataclass
class TurnStart:
    """Where *this turn's own* content begins in the replayed history.

    An index alone is not enough. A history rebuilt mid-turn — an overflow fold hoisting a
    checkpoint in front of a turn that opens on the operator's prompt — has two consecutive
    requests at the boundary, and both the library and :func:`merge_consecutive_requests`
    collapse those into **one** message that belongs half to the history and half to the
    turn. Slicing at a message index there either re-persists the checkpoint (and, once the
    library writes its history-processor output back, the whole reinjected brief) as new
    operator messages, or drops the operator's own prompt from the turn it records.

    So the boundary is a message index *and* how many of that message's parts are the
    turn's own. It is counted from the **end**, because the front of a shared message is
    not stable: the library prepends the run's system prompt to the first request it sends
    and hoists tool results ahead of user-facing parts when it merges. The turn's own
    prompt is what the merge appended, so it stays at the tail — and the tail is what can
    be counted. ``parts`` is zero for every turn that never folded (nearly all of them) and
    :meth:`slice` is then exactly the plain list slice it always was. Mutable, because an
    in-turn fold moves it and every reader holds this one object.

    ``prompt_message_id`` is the store's id for the operator's prompt when the turn wrote it
    down as it started — the first message :meth:`slice` yields is then already persisted,
    and everything that records the turn records only what follows it
    (:func:`after_recorded_prompt`). It rides the boundary rather than the persistence
    context because the two are one fact: *where the turn begins* and *how much of it is
    already on record* are both questions about its opening message. ``None`` for a turn
    that opens on a request already in the tree (a regenerate) or has nowhere to persist.
    """

    index: int = 0
    parts: int = 0
    prompt_message_id: str | None = None

    def slice(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """``messages`` from this boundary on — the turn's own messages, with anything in
        the boundary message that belongs to the history in front of the turn removed."""
        turn = messages[self.index :]
        if not self.parts or not turn or not isinstance(turn[0], ModelRequest):
            return turn
        kept = turn[0].parts[-self.parts :]
        return [replace(turn[0], parts=kept), *turn[1:]]


def after_recorded_prompt(turn: list[ModelMessage]) -> list[ModelMessage]:
    """``turn`` — a :meth:`TurnStart.slice` — without its opening request, which the store
    already holds from the moment the turn began.

    Not a plain ``turn[1:]``. Steering queued before the model's first request is amended
    onto *that* request (`drive_turn`'s ``_inject_queued`` rebinds its parts), so the
    opening message can carry user parts the early write never saw. Each one past the
    prompt's own part is handed back as a request of its own — the shape
    :func:`split_injected_requests` gives a mid-run aside — so a message the operator typed
    while the turn was starting is recorded exactly once, right behind the prompt it
    followed."""
    if not turn:
        return turn
    opening, rest = turn[0], turn[1:]
    if not isinstance(opening, ModelRequest):
        return rest
    parts = [p for p in opening.parts if isinstance(p, UserPromptPart)]
    return [*(ModelRequest(parts=[part]) for part in parts[1:]), *rest]


def split_injected_requests(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Give each mid-run injected operator message its own persisted request.

    Mid-run steering appends ``UserPromptPart``s onto the tool-return request the
    model was about to receive (`drive_turn`'s ``_inject_queued``). Persisting that
    merged request as-is would bury the operator's message inside a tool exchange —
    no tree node of its own, so no user bubble, no edit/regenerate anchor. Split it:
    the tool returns keep their request, and each injected part becomes its own
    ``ModelRequest`` right after. Replay stays wire-identical — the library re-merges
    consecutive requests (tool returns first) when preparing the model's input."""
    out: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            out.append(message)
            continue
        user_parts = [p for p in message.parts if isinstance(p, UserPromptPart)]
        tool_parts = [p for p in message.parts if isinstance(p, ToolReturnPart | RetryPromptPart)]
        if not user_parts or not tool_parts:
            out.append(message)
            continue
        rest = [p for p in message.parts if not isinstance(p, UserPromptPart)]
        out.append(replace(message, parts=rest))
        out.extend(ModelRequest(parts=[part]) for part in user_parts)
    return out


def revealed_tools(messages: Sequence[ModelMessage]) -> tuple[str, ...]:
    """Every tool the model was *shown* across ``messages``, in first-appearance order.

    A dormant group is not a setting the chassis remembers; it is a fact the history
    carries. Pydantic AI re-derives the revealed set from the outgoing messages on every
    single request, so a tool the model loaded stays loaded exactly as long as the messages
    that loaded it are still being replayed — and vanishes the moment they aren't. That is
    what makes this a *reader* rather than a piece of state: the only honest answer to "what
    is revealed" is the one read off the same messages the library will read.

    Three shapes say it, because the library records the reveal wherever the provider put
    it — a local ``search_tools`` return, a native server-side search return, or a bare
    availability delta (which is also the shape we carry a reveal across a compaction fold
    in, since the fold takes the exchange that revealed it away). Order is
    first-appearance rather than a set: a reveal replayed in a stable order keeps the
    provider's tool array byte-stable, and a shuffled one would cost the cache it exists to
    protect.

    Deliberately name-only and unvalidated against any catalog. A name the current
    installation has no definition for cannot be revealed, and the library drops it for
    exactly that reason on the way out — re-checking here would need a catalog this layer
    has no business holding."""
    names: dict[str, None] = {}
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolAvailabilityDeltaPart):
                names.update(dict.fromkeys(part.tools_added))
            elif isinstance(part, ToolSearchReturnPart | NativeToolSearchReturnPart):
                names.update(dict.fromkeys(match["name"] for match in part.discovered_tools))
    return tuple(names)
