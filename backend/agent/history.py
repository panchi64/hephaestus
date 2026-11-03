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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from pydantic_ai import (
    InstructionBaselinePart,
    InstructionDeltaPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelSettingsDeltaPart,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.messages import (
    ModelRequestPart,
    NativeToolSearchReturnPart,
    ToolAvailabilityDeltaPart,
    ToolSearchReturnPart,
)

from services.chassis_tail import merge_requests, parts_from, with_tail

#: Parts Pydantic AI adds to an outgoing request after the capability hooks have read it:
#: the instruction window's baseline, and the instruction, effort and tool changes.
RECORDED_ON_SEND = (
    InstructionBaselinePart,
    InstructionDeltaPart,
    ModelSettingsDeltaPart,
    ToolAvailabilityDeltaPart,
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


#: What a stopped turn's call that never reported back returns, so the next turn checks
#: before redoing it.
UNFINISHED_CALL = (
    "Stopped by the operator before this call reported back. It may or may not have taken "
    "effect, so look before doing it again."
)


def close_tool_calls(
    messages: list[ModelMessage], outcomes: Mapping[str, object]
) -> list[ModelMessage]:
    """Answer the last response's open tool calls from ``outcomes`` (by ``tool_call_id``),
    with :data:`UNFINISHED_CALL` for any it lacks, for a turn stopped mid-batch.

    The history may end on the response itself, or on the library's interrupted request
    holding only the calls that finished. Either way a call left open would otherwise be
    answered on replay with the library's "interrupted before a result was produced",
    which a model reads as "nothing happened" and redoes, even when the write landed."""
    if messages and isinstance(messages[-1], ModelResponse):
        response, answered = messages[-1], None
    elif (
        len(messages) >= 2
        and isinstance(messages[-1], ModelRequest)
        and isinstance(messages[-2], ModelResponse)
    ):
        response, answered = messages[-2], messages[-1]
    else:
        return messages
    parts: Sequence[ModelRequestPart] = answered.parts if answered is not None else ()
    done = {p.tool_call_id for p in parts if isinstance(p, ToolReturnPart)}
    returns: list[ModelRequestPart] = [
        ToolReturnPart(
            tool_name=call.tool_name,
            content=outcomes.get(call.tool_call_id, UNFINISHED_CALL),
            tool_call_id=call.tool_call_id,
        )
        for call in response.parts
        if isinstance(call, ToolCallPart) and call.tool_call_id not in done
    ]
    if not returns:
        return messages
    if answered is None:
        return [*messages, ModelRequest(parts=returns)]
    return [*messages[:-1], replace(answered, parts=[*answered.parts, *returns])]


def with_tail_context(messages: list[ModelMessage], texts: list[str]) -> list[ModelMessage]:
    """Replace the trailing user request's per-turn context with ``texts`` — the *model's
    view only*, for a regenerate, whose stored prompt still carries the context it was
    first sent with. The swap never reaches the durable history.

    The request is always a copy: the library records its parts on it as it sends, and
    the store's in-memory tree must not pick them up."""
    if not messages or not isinstance(messages[-1], ModelRequest):
        return messages
    return [*messages[:-1], replace(with_tail(messages[-1], texts))]


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
            out[-1] = merge_requests(out[-1], message)
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

    So the boundary is a message index *and* how many of that message's leading parts
    belong to the history (:func:`history_part_count`). They are counted from the
    **front**, because the library and steering both append to the request being sent.
    System parts are left out of the count: the library prepends them to a replay that
    opens without one, and they are never the turn's own. ``history_parts`` is zero for
    every turn that never folded (nearly all of them) and :meth:`slice` is then the plain
    list slice. Mutable, because an in-turn fold moves it and every reader holds this one
    object.

    ``prompt_message_id`` is the store's id for the operator's prompt when the turn wrote it
    down as it started — the first message :meth:`slice` yields is then already persisted,
    and everything that records the turn records only what follows it
    (:func:`split_recorded_opening`). It rides the boundary rather than the persistence
    context because the two are one fact: *where the turn begins* and *how much of it is
    already on record* are both questions about its opening message. A regenerate sets it to
    the re-answered request. ``None`` for a turn with nowhere to persist.
    """

    index: int = 0
    history_parts: int = 0
    prompt_message_id: str | None = None

    def slice(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        """``messages`` from this boundary on — the turn's own messages, with anything in
        the boundary message that belongs to the history in front of the turn removed."""
        turn = messages[self.index :]
        if not self.history_parts or not turn or not isinstance(turn[0], ModelRequest):
            return turn
        parts = turn[0].parts
        start, counted = 0, 0
        while start < len(parts) and counted < self.history_parts:
            counted += history_part_count(parts[start : start + 1])
            start += 1
        opening = parts_from(turn[0], start)
        return [opening, *turn[1:]] if opening.parts else turn[1:]


def history_part_count(parts: Sequence[object]) -> int:
    """How many of ``parts`` :attr:`TurnStart.history_parts` counts: all but system parts."""
    return sum(1 for p in parts if not isinstance(p, SystemPromptPart))


def split_recorded_opening(
    turn: list[ModelMessage],
) -> tuple[ModelRequest | None, list[ModelMessage]]:
    """``turn`` — a :meth:`TurnStart.slice` — split around its opening request, which the
    store already holds from the moment the turn began: that prompt as sent, to write back
    over the stored one, and the messages to record after it.

    The sent prompt can differ from the stored one: a regenerate swaps its tail
    (:func:`with_tail_context`), and the library records parts on it as it sends
    (:data:`RECORDED_ON_SEND`; without them the next turn finds no instruction baseline
    and rewrites the cached head). Steering queued before the model's first request
    (`drive_turn`'s ``_inject_queued``) also lands on it; from the first steering message
    on, the parts are recorded as requests in their sent order, so replay merges them back
    byte for byte."""
    if not turn:
        return None, turn
    opening, rest = turn[0], turn[1:]
    if not isinstance(opening, ModelRequest):
        return None, rest
    prompt_at = next(
        (i for i, p in enumerate(opening.parts) if isinstance(p, UserPromptPart)), None
    )
    if prompt_at is None:
        return None, rest
    sent = parts_from(opening, prompt_at)
    after: list[ModelRequestPart] = [
        p for p in sent.parts[1:] if isinstance(p, (UserPromptPart, *RECORDED_ON_SEND))
    ]
    steering_at = next(
        (i for i, p in enumerate(after) if isinstance(p, UserPromptPart)), len(after)
    )
    prompt = replace(sent, parts=[sent.parts[0], *after[:steering_at]])
    return prompt, [*_in_sent_order(after[steering_at:]), *rest]


def split_injected_requests(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Give each mid-run injected operator message its own persisted request.

    Mid-run steering appends ``UserPromptPart``s onto the tool-return request the
    model was about to receive (`drive_turn`'s ``_inject_queued``). Persisting that
    merged request as-is would bury the operator's message inside a tool exchange —
    no tree node of its own, so no user bubble, no edit/regenerate anchor. Split it in
    sent order (:func:`_in_sent_order`), so replay re-merges it byte for byte."""
    out: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            out.append(message)
            continue
        has_user = any(isinstance(p, UserPromptPart) for p in message.parts)
        has_return = any(isinstance(p, ToolReturnPart) for p in message.parts)
        if not has_user or not has_return:
            out.append(message)
            continue
        first, *later = _in_sent_order(message.parts)
        out.extend([replace(message, parts=first.parts), *later])
    return out


def _in_sent_order(parts: Sequence[ModelRequestPart]) -> list[ModelRequest]:
    """``parts`` as requests that merge back to them: user parts alone, other runs together."""
    out: list[ModelRequest] = []
    pending: list[ModelRequestPart] = []
    for part in parts:
        if not isinstance(part, UserPromptPart):
            pending.append(part)
            continue
        if pending:
            out.append(ModelRequest(parts=pending))
            pending = []
        out.append(ModelRequest(parts=[part]))
    if pending:
        out.append(ModelRequest(parts=pending))
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
