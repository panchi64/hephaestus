"""What a capability says on the operator's stream.

The two capabilities this codebase registers both observe a request as it is assembled —
what it weighs (``agent/overhead.py``) and which named contributor put what in front of
the model (``agent/injections.py``). Both then have to get their finding *out*, and until
Pydantic AI grew an event seam the only way was to reach through ``ctx.deps`` for the
``Run``. That made a capability — a library-owned hook, reusable by construction —
depend on our deps object, and every hook had to guard the reach in case an agent was
built without them.

Now the hook awaits ``ctx.emit`` and the event arrives in ``agent/translate.py``'s node
walk like any other, at the head of the request stream it was measured from, where it is
translated to our wire protocol in the one place that already does that translation.

**A capability's family is not a tool's.** Application code emits a ``CustomEvent`` (see
``tools/emit.py``); a capability hook emits a ``CapabilityEvent``, and emitting the wrong
family raises. That — plus the layer map, which puts ``tools`` *below* ``agent`` — is why
the two wrappers live in two files rather than one.

**And why ``runs`` does not simply subclass these.** ``runs/`` has no ``pydantic_ai``
import and should keep it that way: it is the protocol the frontend reads, and the day it
inherits from the engine is the day the wire contract moves when the engine does. The
dependency points this way — the agent layer knows about both — and not back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.messages import ToolCallPart

from runs import EventBody, PrefixVerdict, TurnOverhead

#: Namespace for every capability event this codebase defines. The library requires one
#: and uses it to prefix the event's kind (``hephaestus.chassis_event``), which is what
#: keeps our events distinguishable from a harness capability's on a shared stream.
NAMESPACE = "hephaestus"


@dataclass(kw_only=True)
class ChassisEvent(CapabilityEvent, namespace=NAMESPACE):
    """A ``runs.events`` body a capability put on the stream.

    One wrapper rather than an event class per message, for the reason
    ``tools/emit.py``'s twin gives: ``runs/events/`` already *is* the wire contract.
    """

    body: EventBody


@dataclass(kw_only=True)
class OverheadMeasured(CapabilityEvent, namespace=NAMESPACE):
    """What the request about to go out carries besides the conversation.

    Not a ``runs.events`` body: this is state the run holds for the rest of the turn
    (``Run.context_overhead``, which the metrics frame reads at step end), not something
    the operator is shown as it happens. It rides the stream anyway so the capability
    that measures it needs nothing but the run context — the translator is what knows
    there is a ``Run`` to write it to.

    Default dispatch is enough: a capability event emitted from ``before_model_request``
    is delivered at the head of that request's stream, well before the step ends and the
    frame is built.
    """

    overhead: TurnOverhead


@dataclass(kw_only=True)
class PrefixWatched(CapabilityEvent, namespace=NAMESPACE):
    """How much of the previous request's cacheable prefix the outgoing one could reuse.

    State the run holds rather than something the operator is shown, exactly like
    :class:`OverheadMeasured` above — it is read at step end into the diagnostic line, not
    rendered in the work log. A prefill spike is a question about the *engine*, and the
    operator's answer to it is a server flag or a thread they stop editing skills in the
    middle of; neither is served by a row in the transcript.
    """

    verdict: PrefixVerdict


@dataclass(kw_only=True)
class NestedToolStarted(CapabilityEvent, namespace=NAMESPACE):
    """A call a ``run_code`` script made has begun (``agent/code_mode.py``).

    The library streams a ``FunctionToolCallEvent`` for every call the *model* makes and
    nothing at all for the calls a script makes from inside one, so the code-mode
    capability says so itself, and ``translate.py`` turns it into the same ``tool.started``
    frame a direct call gets — carrying the script's ``parent_tool_call_id``. Raw library
    objects rather than a finished frame, so the one translator stays the only place a
    tool call becomes wire.
    """

    parent_tool_call_id: str
    part: ToolCallPart


@dataclass(kw_only=True)
class NestedToolFinished(CapabilityEvent, namespace=NAMESPACE):
    """A call a ``run_code`` script made has ended — with ``content`` when it returned, or
    ``error`` when it failed or was refused. ``tool_call_id`` and ``tool_name`` (the
    library's own fields) name the nested call; ``user_content`` is what a
    ``ToolReturn`` handed back *for the model* beside its result, the pixels a direct
    call's ``FunctionToolResultEvent`` carries as ``content``."""

    parent_tool_call_id: str
    content: Any = None
    user_content: Any = None
    error: str | None = None


#: The run metadata key that marks a **side run** — a request made on a turn's own agent
#: for the chassis's purposes rather than the operator's, today only the compaction summary
#: (``agent/compaction_summary.py``). Its event stream goes nowhere, so nothing it emits
#: reaches the run; what has to be kept out as well is the state an observing capability
#: holds *on itself*, which lives as long as the agent and so would carry a side run's
#: request into the turn's own readings.
SIDE_RUN = "hephaestus.side_run"


def is_side_run(ctx: RunContext[Any]) -> bool:
    """Whether this request belongs to a side run, so an observer should leave it be."""
    return bool((ctx.metadata or {}).get(SIDE_RUN))
