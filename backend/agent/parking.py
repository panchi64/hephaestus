"""Suspending a turn on the operator — the payload and the park itself.

A turn that reaches a call nobody has authorised does not fail and does not guess: it
*stops*, with everything needed to carry on later held on the run. The same is true of a
turn that reaches a question only the operator can answer. That continuation is
:class:`ParkedTurn`, and :func:`park_for_input` is the one place it is built.

**Two reasons to stop, one park.** Pydantic AI hands back both piles on a single
``DeferredToolRequests`` — ``approvals`` for the calls awaiting permission, ``calls`` for
the ones awaiting a value (``tools/builtin.py``'s ``ask_user``) — and a turn can arrive
here holding both. Parking once for the pair is not a convenience: each park is one
resume, and a turn that parked twice would resume twice, running the approved call against
a history the second resume had already moved past.

**Why a payload rather than a re-run.** Pydantic AI ends the turn with
``DeferredToolRequests`` *without executing* the sensitive call, so the agent, the message
history and the deferred requests are all still in hand. Keeping them means a resume
continues the same turn — same agent, same tools, same history — instead of replaying a
prompt against whatever the world looks like when the operator finally answers. Every
field on the payload exists because re-deriving it on resume would be a second, disagreeing
source for a fact the parked turn already settled.

**Why the notify happens before the park.** ``RunRegistry.cancel`` reads
``awaiting_input`` as "the task has already fully exited" and skips its hard-cancel path on
that assumption. Anything that awaits *after* ``run.park(...)`` would break it, so the one
await this module does before parking is deliberately ordered ahead of it.

The rules deciding which *approvals* get here at all are ``gating.py``'s; this module only
knows what to do once a call needs the operator. Questions have no such rules — nothing can
answer one in the operator's place — so every deferred call reaches them.
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

from runs import ApprovalRequired, QuestionAsked, Run
from services.answers import questions_of
from services.conversations import ConversationBinding, ConversationStore
from services.notification_view import NotificationView

from .compaction_context import CompactionContext
from .factory import TurnAgent
from .naming import TitleContext, approval_conversation_title

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


# A stateless turn's workspace binding: an unfiled Normal thread. The conservative
# default — Normal never reaches the host — so a caller that forgets to resolve one
# cannot accidentally hand a run the operator's own files.
DEFAULT_BINDING = ConversationBinding()


@dataclass
class ParkedTurn:
    """The continuation of a run parked awaiting approval. Opaque to the
    substrate; held on ``run.parked_payload`` and consumed by the approve route."""

    agent: TurnAgent
    message_history: list[ModelMessage]
    requests: DeferredToolRequests
    announced: set[str] = field(default_factory=set)
    # Deferred calls that never reached the operator: auto-approved by an active
    # conversation grant, or refused outright by the thread's permission level. Not
    # surfaced (no approval.required event for them) but merged back into the resume's
    # decisions so the single DeferredToolResults still covers every deferred call.
    settled: dict[str, ToolApproved | ToolDenied] = field(default_factory=dict)
    # Persistence context, attached by the orchestrator: the conversation and
    # the index from which messages are still unpersisted (so a resume records
    # the parked turn's messages too, once it finally completes).
    conversation_id: str | None = None
    persist_from: int = 0
    # And how many leading parts of the message *at* that index belong to the history in
    # front of the turn rather than to the turn — non-zero only when an overflow fold
    # rebuilt the replay underneath this turn and the boundary collapsed into one message.
    persist_from_parts: int = 0
    # When a *verifier* correction is what parked, the [start, end] message range
    # to drop on the eventual persist (the rejected answer + the synthetic nudge),
    # so the resume records a clean history too.
    clean_drop: tuple[int, int] | None = None
    # Auto-title context, carried so a first turn that parked for approval is still
    # named once it resumes and completes (titling lives at the shared finalize
    # point, not only in the initial chat turn). None ⇒ don't title on resume.
    title: TitleContext | None = None
    # The store's id for the operator's prompt, which was written down when the turn began
    # — so the resume records only what follows it, and a stop marker with nothing after
    # the prompt has the node to land on. Its attachments, `@` references and command were
    # stamped on that write; there is nothing left for the resume to carry. None for a
    # turn that opened on a request already in the tree (a regenerate).
    prompt_message_id: str | None = None
    # The thread's binding, carried rather than re-read on resume. A resume that
    # defaulted it to Normal would hand a parked code turn a different filesystem than the
    # one it parked in — and the permission level rides along for the same reason it is
    # read once per turn rather than per call: the operator may raise the level while this
    # is parked, and a call they are about to approve must be judged by the rules that
    # deferred it, not by rules that arrived afterwards.
    binding: ConversationBinding = field(default_factory=ConversationBinding)
    # Whether the model this turn runs on can read an image, carried for the same reason
    # `binding` is: the resume must offer the tools the parked turn was offered. Re-reading
    # it would resolve whatever is bound *now*, which need not be the model the parked
    # agent still holds — so a re-read would be a second, disagreeing source for one fact.
    vision: bool = True
    # What this turn may fold with, carried for the same reason `binding` is: a resume
    # continues a turn that was already near the model's ceiling, and the recovery it needs
    # when a request overruns cannot be re-derived here — the resume orchestrator has no
    # settings store and no policy. None ⇒ this turn cannot fold.
    compaction: CompactionContext | None = None
    # Which workspace the turn was working in, carried for exactly the reason `binding` is:
    # a sub-agent parks on an approval like any other turn, and a resume that let this
    # default would hand it its own conversation's workspace instead of the one it parked
    # in — a different filesystem, and for a delegated child an empty one.
    workspace_key: str = ""


#: How much of one argument, and of the whole line, survives into the summary.
#:
#: The values are chosen so that the calls the operator actually reads stay whole: a shell
#: command, a path, a URL, a patch's target — all of them fit inside 200 characters with
#: room to spare, and the line they make fits inside 400. What does not fit is the one
#: shape that made this cap necessary: ``plan_submit`` carries an entire markdown plan in a
#: single argument, and a tool that hands over a file's new contents carries the file.
VALUE_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 400


def summarize_call(name: str, args: Mapping[str, object]) -> str:
    """The one-line rendering of a call the operator is being asked about.

    **A rendering, not a record.** This string rides the ``approval.required`` event beside
    the call's real ``args`` — which are untouched and complete — and from there into a
    notification body, where an unbounded value is not merely long but actively harmful: it
    is the text of a push notification, and the operator scrolling past a thousand lines of
    markdown to find out *which* call they are being asked about has been told less than a
    truncated line would have told them. Anything that needs the whole call reads ``args``;
    nothing should ever read this back to reconstruct one.

    So each value is elided on its own before the line is assembled, rather than the line
    simply being cut at the end: one enormous argument would otherwise push every argument
    after it off the summary, and the last argument is as likely as the first to be the one
    that says what this call does. The whole-line cap is the backstop for the other shape —
    a call with many small arguments, each innocent by itself.
    """
    rendered = ", ".join(f"{k}={_clip(repr(v), VALUE_MAX_CHARS)}" for k, v in args.items())
    return _clip(f"{name}({rendered})", SUMMARY_MAX_CHARS)


def _clip(value: str, limit: int) -> str:
    """``value`` at no more than ``limit`` characters, ending in an ellipsis if it was cut.

    The ellipsis is the whole point: a summary that was silently truncated reads as a call
    with a shorter argument than the one actually being approved, which is the one lie this
    function must not tell.
    """
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


async def park_for_input(
    run: Run,
    agent: TurnAgent,
    messages: list[ModelMessage],
    requests: DeferredToolRequests,
    announced: set[str],
    *,
    settled: dict[str, ToolApproved | ToolDenied] | None = None,
    notifications: ParkNotifier | None = None,
    store: ConversationStore | None = None,
    conversation_id: str | None = None,
    binding: ConversationBinding = DEFAULT_BINDING,
    vision: bool = True,
    compaction: CompactionContext | None = None,
    workspace_key: str = "",
) -> None:
    # Only the calls still awaiting the operator are announced; the ones a grant or the
    # thread's level already settled ride silently on the parked payload and merge into
    # the resume.
    settled = settled or {}
    pending_names: set[str] = set()
    for call in requests.approvals:
        if call.tool_call_id in settled:
            continue
        pending_names.add(call.tool_name)
        args = call.args_as_dict()
        # A plain-language explanation comes from an `explanation` argument (the
        # host-execution path requires one) or, for a tool that writes its own from
        # stored state, from the `ApprovalRequired` metadata.
        explanation = args.get("explanation")
        if not isinstance(explanation, str):
            explanation = requests.metadata.get(call.tool_call_id, {}).get("explanation")
        _ = run.emit(
            ApprovalRequired(
                tool_call_id=call.tool_call_id,
                name=call.tool_name,
                args=args,
                summary=summarize_call(call.tool_name, args),
                explanation=explanation if isinstance(explanation, str) else None,
            )
        )
    # The other pile: calls deferred for a *value* rather than a permission. Nothing
    # settles these ahead of the operator — there is no grant, no level and no reviewer
    # that can answer a question in their place — so every one of them is announced.
    asked = False
    for call in requests.calls:
        asked = True
        _ = run.emit(
            QuestionAsked(
                tool_call_id=call.tool_call_id,
                questions=questions_of(call.args_as_dict()),
            )
        )
    # Fire the ALWAYS-notify policy *before* `run.park(...)` makes the parked status
    # externally visible — not after. This is the one await this function does before
    # parking, and it must land first: `RunRegistry.cancel`'s parked branch assumes
    # "awaiting_input ⇒ the task has already fully exited" and skips the hard-cancel
    # path on that assumption. If the notify (and the conversation-title lookup it may
    # need) instead ran *after* parking, a concurrent cancel/approve landing in that
    # window would see the parked status while this coroutine is still suspended on a
    # real await — violating that assumption and racing the run's own finalize.
    if notifications is not None and (pending_names or asked):
        title = await approval_conversation_title(store, run.owner_id, conversation_id)
        # One kind for both reasons. `approval_needed` is the routing key for "a parked
        # run is waiting on you", which is exactly as true of a question as of an
        # approval — and it is the key five separate client behaviours already hang off
        # (the bell's accent, the deep link, and the age-filter exemption that keeps a
        # pending park from quietly expiring). A second kind would have to restate every
        # one of them, and the pair would drift. What differs is the sentence, so that is
        # what differs.
        wants: list[str] = []
        if pending_names:
            wants.append(f"approval for {', '.join(sorted(pending_names))}")
        if asked:
            wants.append("an answer from you")
        try:
            _ = await notifications.notify(
                run.owner_id,
                "approval_needed",
                f'"{title}" needs {" and ".join(wants)}',
                conversation_id=conversation_id,
                run_id=run.id,
            )
        except Exception:  # noqa: BLE001 — a notify failure must not break the park
            logger.warning("approval_needed notification failed for run %s", run.id, exc_info=True)
    run.park(
        ParkedTurn(
            agent,
            messages,
            requests,
            announced,
            settled=settled,
            binding=binding,
            vision=vision,
            compaction=compaction,
            workspace_key=workspace_key,
        )
    )
