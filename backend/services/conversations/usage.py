"""What a conversation costs, read off its messages: context footprint, the last request's usage
and the active-path totals.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart

from runs.events import LastRequestUsage
from runs.overhead import TurnOverhead
from services.conversation_view import estimate_footprint

from .paths import _is_user_prompt


def context_footprint(messages: list[ModelMessage]) -> int | None:
    """The context footprint after a turn: the most recent model response's prompt
    plus its generation — the tokens the next turn carries forward. None when there
    is no response, or the provider reported no usage (local servers often leave
    ``input_tokens`` at 0, which we can't tell from real and so treat as unmeasured
    rather than render a misleading 0%). Shared by the live run metrics and the
    cold-load conversation detail so both report the same quantity."""
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            usage = message.usage
            return usage.input_tokens + (usage.output_tokens or 0) if usage.input_tokens else None
    return None


def footprint_or_estimate(
    messages: list[ModelMessage],
    overhead: TurnOverhead | None,
    *,
    fallback_overhead_tokens: int,
    reported_from: list[ModelMessage] | None = None,
) -> int | None:
    """The thread's footprint: what the provider reported if it reported anything, and the
    estimate otherwise. ``None`` only for a thread with no messages at all.

    The one place that choice is made, because it is a choice the *live* gauge and a *cold*
    reload have to make identically. An endpoint that reports no usage — the common local
    case, ``input_tokens=0`` — is exactly when the estimate matters, and it used to be the
    case where the live stream showed a figure and reopening the same thread showed a blank
    ring and no breakdown.

    ``reported_from`` narrows which messages a *reported* figure may be read off (the
    replay since this run's last fold — every response before it sized a history that no
    longer exists) while the estimate still covers everything the next request carries."""
    if not messages:
        return None
    footprint = context_footprint(messages if reported_from is None else reported_from)
    if footprint is not None:
        return footprint
    return estimate_footprint(messages, overhead, fallback_overhead_tokens=fallback_overhead_tokens)


def last_request_usage(messages: list[ModelMessage]) -> LastRequestUsage | None:
    """The most recent model response's own figures — the route it took and what the
    provider's cache did with its prompt.

    The same backwards walk :func:`context_footprint` makes, over the same list, for the
    same reason: everything else the readout reports is cumulative over the path, and
    these two are properties of one request. A fallback chain's second model and a cold
    cache both vanish into a running total, which is precisely when an operator wants to
    know about them.

    None when the path holds no response at all. A response that reported no usage still
    answers with its route, since which model spoke is known whether or not it said what
    that cost — and every token field stays null rather than zero, matching the frame it
    rides on."""
    for message in reversed(messages):
        if not isinstance(message, ModelResponse):
            continue
        usage = message.usage
        provider = getattr(message, "provider_name", None)
        model = getattr(message, "model_name", None)
        return LastRequestUsage(
            route=f"{provider}:{model}" if provider and model else model,
            # Zero is what a local server reports when it means "not measured", which is
            # indistinguishable from a real zero and so is treated as absent here exactly
            # as it is in `context_footprint` above.
            input_tokens=usage.input_tokens or None,
            output_tokens=usage.output_tokens or None,
            cache_read_tokens=usage.cache_read_tokens or None,
            cache_write_tokens=usage.cache_write_tokens or None,
        )
    return None


@dataclass(frozen=True, slots=True)
class ConversationTotals:
    """What a thread has cost, counted off its **active path**.

    Derived from the messages themselves, never from a running counter, and that is
    the whole design. The conversation is a *tree*: a rewind or a version switch moves
    the active leaf, and a stored total would go on reporting tokens spent down a
    branch the operator has walked away from. Counting the path each time means the
    numbers follow navigation for free, and a cold load and a live turn compute the
    same figure from the same function.

    Token fields are None when nothing reported them, matching ``context_footprint``:
    local servers routinely leave ``input_tokens`` at 0, which is indistinguishable
    from a real zero, so an unmeasured thread reports absent rather than free.
    """

    #: Completed operator exchanges (see ``_is_user_prompt``/turn-start semantics).
    turns: int = 0
    #: Model round-trips those turns took between them.
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Provider-reported cached prompt tokens. None when no response reported any —
    #: most OpenAI-compatible and local endpoints never do.
    cache_read_tokens: int | None = None


def conversation_totals(messages: list[ModelMessage]) -> ConversationTotals:
    """Sum a message path into the readout's counts.

    ``steps`` counts model responses and ``turns`` counts the operator's own prompts
    that open an exchange — the same distinction ``_is_turn_start`` draws, applied to a
    flat message list: a prompt directly following another request is a mid-run aside
    the operator sent while the model worked, not a new exchange."""
    turns = steps = tool_calls = 0
    input_tokens = output_tokens = cache_read = 0
    saw_tokens = saw_cache = False
    previous: ModelMessage | None = None

    for message in messages:
        if isinstance(message, ModelResponse):
            steps += 1
            tool_calls += sum(1 for p in message.parts if isinstance(p, ToolCallPart))
            usage = message.usage
            if usage.input_tokens:
                saw_tokens = True
                input_tokens += usage.input_tokens
                output_tokens += usage.output_tokens or 0
            # Distinguished from "cached nothing": a provider that reports the field at
            # all is taken at its word, including when it says zero.
            if usage.cache_read_tokens:
                saw_cache = True
                cache_read += usage.cache_read_tokens
        elif _is_user_prompt(message) and (previous is None or isinstance(previous, ModelResponse)):
            turns += 1
        previous = message

    return ConversationTotals(
        turns=turns,
        steps=steps,
        tool_calls=tool_calls,
        input_tokens=input_tokens if saw_tokens else None,
        output_tokens=output_tokens if saw_tokens else None,
        cache_read_tokens=cache_read if saw_cache else None,
    )
