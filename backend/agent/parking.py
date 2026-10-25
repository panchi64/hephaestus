"""Suspending a turn on the operator: the :class:`ParkedTurn` payload and the park itself.

A turn reaching a call nobody authorised, or a question only the operator can answer,
stops with everything needed to continue held on the run. :func:`park_for_input` is the
one place the continuation is built.

One park for both reasons: Pydantic AI returns ``approvals`` and ``calls`` (``ask_user``,
``tools/builtin.py``) on a single ``DeferredToolRequests``. Each park is one resume, so
parking twice would run the approved call against a history the second resume had moved past.

A payload rather than a re-run: the sensitive call isn't executed, so the agent, history
and requests are in hand, and a resume continues the same turn instead of replaying a
prompt against whatever the world looks like later. Each field exists because re-deriving
it on resume would be a second, disagreeing source for a settled fact.

An unwatched park notifies before it parks: ``RunRegistry.cancel`` reads ``awaiting_input``
as "the task has fully exited", so nothing may await after ``run.park(...)``. A watched park
parks first and hands its notice to ``ParkNotices`` until the operator leaves.

``gating.py`` decides which approvals get here; every question does, since nothing can
answer one in the operator's place.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from pydantic_ai import (
    DeferredToolRequests,
    ModelMessage,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

from core.config import get_settings
from core.text import clip
from runs import ApprovalRequired, EventBody, QuestionAsked, Run
from services.answers import questions_of
from services.conversations import ConversationBinding, ConversationStore
from services.notification_view import NotificationView
from services.permissions.guards import GUARD_KEY, unconfined

from .compaction_context import CompactionContext
from .factory import TurnAgent
from .naming import TitleContext, approval_conversation_title
from .park_notice import ParkNotices

logger = logging.getLogger(__name__)


class ParkNotifier(Protocol):
    """The slice of the notification service a park announces itself through."""

    async def notify(
        self,
        owner_id: str,
        kind: str,
        title: str,
        body: str | None = None,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> NotificationView: ...


# A stateless turn's binding: an unfiled Normal thread, which never reaches the host, so a
# caller that forgets to resolve one can't hand a run the operator's files.
DEFAULT_BINDING = ConversationBinding()


@dataclass
class ParkedTurn:
    """The continuation of a run parked awaiting the operator. Opaque to the substrate;
    held on ``run.parked_payload`` and consumed by the approve route."""

    #: None on a park restored after a restart, until its kind's recompose hook rebuilds it.
    agent: TurnAgent | None
    message_history: list[ModelMessage]
    requests: DeferredToolRequests
    announced: set[str] = field(default_factory=set)
    # Deferred calls that never reached the operator (auto-approved by a grant, or refused
    # by the permission level): no approval.required event, but merged into the resume's
    # decisions so the single DeferredToolResults covers every deferred call.
    settled: dict[str, ToolApproved | ToolDenied] = field(default_factory=dict)
    # Set by the orchestrator: the conversation and the index from which messages are still
    # unpersisted, so the resume records the parked turn's messages on completion.
    conversation_id: str | None = None
    persist_from: int = 0
    # How many leading parts of the message at that index belong to the history before the
    # turn; non-zero only when an overflow fold rebuilt the replay under this turn and the
    # boundary collapsed into one message.
    persist_from_parts: int = 0
    # When a verifier correction parked, the [start, end] range (rejected answer plus the
    # synthetic nudge) to drop on the eventual persist.
    clean_drop: tuple[int, int] | None = None
    # Carried so a first turn that parked is still named when it completes (titling lives at
    # the shared finalize point). None ⇒ don't title on resume.
    title: TitleContext | None = None
    # The store's id for the prompt, written when the turn began, so the resume records only
    # what follows and a stop marker has a node to land on. Attachments, `@` references and
    # command were stamped on that write. None for a turn opened on a request already in the
    # tree (a regenerate).
    prompt_message_id: str | None = None
    # The thread's binding, carried not re-read: defaulting to Normal would give a parked
    # code turn a different filesystem. The permission level rides along because the
    # operator may raise it while parked, and a call about to be approved must be judged by
    # the rules that deferred it.
    binding: ConversationBinding = field(default_factory=ConversationBinding)
    # Whether the turn's model reads images, carried so the resume offers the same tools. A
    # re-read would resolve whatever is bound now, not the model the parked agent holds.
    vision: bool = True
    # What the turn may fold with. A resume continues a turn already near the ceiling, and
    # the resume orchestrator has no settings store or policy to re-derive it. None ⇒ cannot fold.
    compaction: CompactionContext | None = None
    # The workspace the turn worked in, carried like `binding`: a sub-agent's default would
    # be its own conversation's workspace, a different (for a delegated child, empty) filesystem.
    workspace_key: str = ""
    # The endpoint and chain-head model name the parked agent held, so a restart can ask the
    # registry for the same one; None when it can't be read off.
    endpoint_id: str | None = None
    model: str | None = None
    # Whether the turn was to name its thread. `title` holds model clients a restart can't
    # carry, so a restored park keeps this and has `title` rebuilt by its recompose hook.
    titled: bool = False
    # The agent's request cap and requests spent so far, so the cap bounds the whole turn
    # across a park and resume.
    max_requests: int | None = None
    requests_used: int = 0
    # Where a spawned agent's file changes are filed (`RunDeps.ledger_prompt_message_id`).
    ledger_prompt_message_id: str | None = None


def primary_model_name(agent: TurnAgent) -> str | None:
    """The model name at the head of the agent's fallback chain."""
    model = agent.model
    if isinstance(model, FallbackModel):
        model = model.models[0]
    return model.model_name if isinstance(model, Model) else None


def announcements(
    requests: DeferredToolRequests, settled: Mapping[str, ToolApproved | ToolDenied]
) -> tuple[list[EventBody], set[str], bool]:
    """The events putting a park's open questions to the operator, the names of tools
    awaiting approval, and whether any question was asked.

    Calls already settled by a grant or the level ride silently on the payload; every
    question is announced, as nothing settles one ahead of the operator."""
    events: list[EventBody] = []
    pending_names: set[str] = set()
    for call in requests.approvals:
        if call.tool_call_id in settled:
            continue
        pending_names.add(call.tool_name)
        args = call.args_as_dict()
        # From an `explanation` argument (the host-execution path requires one) or, for a
        # tool writing its own from stored state, the `ApprovalRequired` metadata.
        metadata = requests.metadata.get(call.tool_call_id, {})
        explanation = args.get("explanation")
        if not isinstance(explanation, str):
            explanation = metadata.get("explanation")
        reason = metadata.get(GUARD_KEY)
        guard_reason = reason if isinstance(reason, str) and reason else None
        events.append(
            ApprovalRequired(
                tool_call_id=call.tool_call_id,
                name=call.tool_name,
                args=args,
                summary=summarize_call(call.tool_name, args),
                explanation=explanation if isinstance(explanation, str) else None,
                guard=guard_reason,
                unconfined=unconfined(call.tool_name, settings=get_settings()),
            )
        )
    for call in requests.calls:
        events.append(
            QuestionAsked(
                tool_call_id=call.tool_call_id,
                questions=questions_of(call.args_as_dict()),
            )
        )
    return events, pending_names, bool(requests.calls)


#: How much of one argument, and of the whole line, survives into the summary. Commands,
#: paths and URLs fit whole; what needed the cap is ``plan_submit``, which carries an entire
#: markdown plan in one argument, and tools that carry a file's new contents.
VALUE_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 400


def summarize_call(name: str, args: Mapping[str, object]) -> str:
    """The one-line rendering of a call the operator is asked about.

    A rendering, not a record: it rides the ``approval.required`` event beside the complete
    ``args`` and into a push-notification body, where an unbounded value buries which call
    is being asked about. Read ``args`` for the whole call; never reconstruct one from this.

    Each value is elided on its own before assembly, so one enormous argument can't push
    later ones off the line; the whole-line cap backstops many small arguments.
    """
    rendered = ", ".join(f"{k}={clip(repr(v), VALUE_MAX_CHARS)}" for k, v in args.items())
    return clip(f"{name}({rendered})", SUMMARY_MAX_CHARS)


async def park_for_input(
    run: Run,
    agent: TurnAgent,
    messages: list[ModelMessage],
    requests: DeferredToolRequests,
    announced: set[str],
    *,
    settled: dict[str, ToolApproved | ToolDenied] | None = None,
    notifications: ParkNotifier | None = None,
    holds: ParkNotices | None = None,
    store: ConversationStore | None = None,
    conversation_id: str | None = None,
    binding: ConversationBinding = DEFAULT_BINDING,
    vision: bool = True,
    compaction: CompactionContext | None = None,
    workspace_key: str = "",
    max_requests: int | None = None,
    requests_used: int = 0,
    ledger_prompt_message_id: str | None = None,
) -> None:
    settled = settled or {}
    events, pending_names, asked = announcements(requests, settled)
    for event in events:
        _ = run.emit(event)
    parked = ParkedTurn(
        agent,
        messages,
        requests,
        announced,
        settled=settled,
        binding=binding,
        vision=vision,
        compaction=compaction,
        workspace_key=workspace_key,
        endpoint_id=run.endpoint.id if run.endpoint is not None else None,
        model=primary_model_name(agent),
        max_requests=max_requests,
        requests_used=requests_used,
        ledger_prompt_message_id=ledger_prompt_message_id,
    )
    if notifications is None or not (pending_names or asked):
        run.park(parked)
        return

    async def send() -> None:
        title = await approval_conversation_title(store, run.owner_id, conversation_id)
        # One kind for both: `approval_needed` is the routing key five client behaviours
        # hang off (bell accent, deep link, the age-filter exemption), equally true of a
        # question. Only the sentence differs.
        wants: list[str] = []
        if pending_names:
            wants.append(f"approval for {', '.join(sorted(pending_names))}")
        if asked:
            wants.append("an answer from you")
        _ = await notifications.notify(
            run.owner_id,
            "approval_needed",
            f'"{title}" needs {" and ".join(wants)}',
            conversation_id=conversation_id,
            run_id=run.id,
        )

    if holds is not None and run.stream.watcher_count > 0:
        run.park(parked)
        holds.hold(run, parked, send)
        return
    try:
        await send()
    except Exception:  # a notify failure must not break the park
        logger.warning("approval_needed notification failed for run %s", run.id, exc_info=True)
    run.park(parked)
