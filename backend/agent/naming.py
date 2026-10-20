"""Naming a fresh conversation, and getting out of the way when it can't.

``title.py`` is the model call — given some messages, produce a short name. This is
everything around it: when a thread is eligible to be named, the two paths that name one,
persisting the result without ever clobbering a name the operator chose, and announcing it
on the run's stream while that stream is still open.

Two paths, because the two orchestrators reach completion differently:

- A **fresh chat turn** starts the namer up front (:func:`start_title`), concurrently with
  the answer, and announces the moment the name lands. Abandoning it needs care: before
  the name exists, cancelling costs nothing; past that point the persist and the emit must
  not be split, or the name reaches the database but never the client. :class:`Namer`
  carries exactly that distinction.
- A **resumed turn** has no prompt in hand and no window to overlap with, so it names
  afterwards from the just-persisted history (:func:`maybe_title`).

**The run never waits for its name.** An endpoint that serves one request at a time
queues the title behind the answer, so a run that awaited it would end late, and a budget
counted from the send would be spent in that queue. The namer's budget starts when the
answer is written, and a namer still working then finishes as the run's afterwork
(:func:`release_title`); its name is persisted, and a client that saw the run end without
one waits up to :func:`late_title_wait_s` for it. The namer runs outside the run's
endpoint lease, so the gate counts its request even after the lease moves on.

Best-effort throughout: a failure here leaves a thread untitled, which is a cosmetic loss,
and must never disturb a finished turn.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.config import get_settings
from runs import ConversationTitled, Run, outside_lease
from services.conversations import ConversationStore

from .title import generate_title, title_from_history

logger = logging.getLogger(__name__)

# Past the title call's own budget: reading the history before it and persisting the
# name after it need a moment too.
_LATE_TITLE_SLACK_S = 5.0


@dataclass(frozen=True)
class TitleContext:
    """What auto-titling needs, bundled so it can ride a parked turn to its resume.
    The model + its reasoning-off settings come resolved together from the registry
    (titling is a fast, no-reasoning pass). Absent ⇒ titling is off for this run."""

    model: Model
    settings: ModelSettings


async def approval_conversation_title(
    store: ConversationStore | None, owner_id: str, conversation_id: str | None
) -> str:
    """A short, human name for the conversation a park's notification names — the
    conversation's own title when one exists (auto-titling may not have run yet on a
    fresh thread), else a plain fallback. Never raises: a lookup failure degrades to
    the fallback rather than losing the notification over a cosmetic detail."""
    if store is not None and conversation_id is not None:
        try:
            summary = await store.get_summary(conversation_id, owner_id)
        except Exception:  # a title lookup must not block the notify
            logger.debug("title lookup failed for %s", conversation_id, exc_info=True)
            summary = None
        if summary is not None and summary.title:
            return summary.title
    return "this conversation"


def late_title_wait_s() -> float:
    """How long a namer still running after its run can take to land or give up."""
    return get_settings().title_timeout_s + _LATE_TITLE_SLACK_S


def maybe_title(
    run: Run,
    *,
    title: TitleContext | None,
    store: ConversationStore | None,
    conversation_id: str | None,
    is_first_turn: bool,
) -> None:
    """Name a first turn that parked for approval, once it has resumed to completion.

    Reads the operator's opening message from the just-persisted history, because the
    resume has no ``prompt`` in hand. ``is_first_turn`` is the cheap pre-filter that
    skips the model call on continuation turns. Runs detached, so the resumed run ends
    without waiting for it."""
    if not is_first_turn or title is None or store is None or conversation_id is None:
        return

    async def _name() -> None:
        try:
            name = await title_from_history(
                title.model,
                await store.history(conversation_id),
                reasoning_off=title.settings,
                timeout_s=get_settings().title_timeout_s,
                max_tokens=get_settings().title_max_tokens,
            )
            await announce_title(run, name, store=store, conversation_id=conversation_id)
        except TimeoutError:
            logger.warning("auto-titling timed out for %s", conversation_id)
        except Exception:  # titling is best-effort, not turn-critical
            logger.warning("auto-titling failed for %s", conversation_id, exc_info=True)

    run.outlive(asyncio.create_task(_name(), context=outside_lease()))


async def announce_title(
    run: Run,
    name: str | None,
    *,
    store: ConversationStore | None,
    conversation_id: str | None,
) -> None:
    """Persist a generated title (fill-only-if-blank) and announce it on success.

    :meth:`ConversationStore.set_title_if_absent` is the authoritative guard — it
    fills only a blank title, so an operator-named thread is never clobbered, and
    ``conversation.titled`` is announced only when it actually set the name, and only
    while the run's stream is open. A name that lands after the run ended is read
    from the store instead."""
    if not name or store is None or conversation_id is None:
        return
    if await store.set_title_if_absent(conversation_id, name) and not run.stream.closed:
        _ = run.emit(ConversationTitled(conversation_id=conversation_id, title=name))


@dataclass
class _Budget:
    """The title call's time limit. It runs only once the turn has stopped using the
    endpoint, because a one-slot server serves the title after the turn's own requests;
    until then the run's own bounds cover it."""

    seconds: float
    running: bool = False
    scope: asyncio.Timeout | None = None

    def deadline(self) -> float | None:
        return asyncio.get_running_loop().time() + self.seconds if self.running else None

    def start(self) -> None:
        self.running = True
        if self.scope is not None and not self.scope.expired():
            self.scope.reschedule(self.deadline())


@dataclass(frozen=True)
class Namer:
    """The concurrent auto-namer: its task, plus the point past which cancelling would
    do damage. ``announcing`` is set the instant generation yields a name, immediately
    before the persist+emit — so an abandoning caller can tell "still waiting on the
    title model" (cancel freely) from "committing the name" (let it finish)."""

    task: asyncio.Task[None]
    announcing: asyncio.Event
    budget: _Budget


def start_title(
    title: TitleContext | None,
    prompt: str | None,
    *,
    run: Run,
    store: ConversationStore | None,
    conversation_id: str | None,
) -> Namer | None:
    """Begin naming the thread concurrently with the turn's answer, announcing the
    name the moment it lands.

    Titling needs only the operator's opening message, which a first turn already has
    in ``prompt`` — so there's no need to wait for the answer (or persistence) first.
    The task persists and emits ``conversation.titled`` **itself**: the utility call
    typically resolves in a second or two, and deferring the announcement to the end of
    a long tool-using turn would leave the thread unnamed for the whole run.

    Returns ``None`` when titling is off or there is nothing to name from. The call is
    bounded by ``title_timeout_s`` from :func:`release_title` on, and best-effort
    throughout: any failure leaves the thread untitled without disturbing the turn."""
    if title is None or not prompt:
        return None
    announcing = asyncio.Event()
    budget = _Budget(get_settings().title_timeout_s)

    async def _name() -> None:
        try:
            try:
                async with asyncio.timeout_at(budget.deadline()) as scope:
                    budget.scope = scope
                    name = await generate_title(
                        title.model,
                        prompt,
                        reasoning_off=title.settings,
                        max_tokens=get_settings().title_max_tokens,
                    )
            finally:
                budget.scope = None
            # Set synchronously with the announce that follows — no await between, so an
            # abandoning caller either sees this before the persist begins (safe to
            # cancel) or waits the announce out. Torn in half instead — cancelled between
            # `set_title_if_absent` committing and the emit — the thread would be named in
            # the database but never announced on the stream, and the resume's own namer
            # would then find the name already there, return False, and emit nothing
            # either: a thread that stays "Untitled" until a reload.
            announcing.set()
            await announce_title(run, name, store=store, conversation_id=conversation_id)
        except asyncio.CancelledError:
            raise  # a park/cancel abandoning the name (see `discard_title`)
        except TimeoutError:
            logger.warning("auto-titling timed out for %s", conversation_id)
        except Exception:  # titling is best-effort, not turn-critical
            logger.warning("auto-titling failed for %s", conversation_id, exc_info=True)

    return Namer(
        task=asyncio.create_task(_name(), context=outside_lease()),
        announcing=announcing,
        budget=budget,
    )


def release_title(run: Run, namer: Namer | None) -> None:
    """Let a namer still running when the turn's answer is written finish as ``run``'s
    afterwork.

    Its budget starts here: on an endpoint that serves one request at a time, the title
    has been queued behind this turn's own requests until now."""
    if namer is None or namer.task.done():
        return
    namer.budget.start()
    run.outlive(namer.task)


async def discard_title(namer: Namer | None) -> None:
    """Abandon a concurrently-started title (the turn parked, raised, or was cancelled):
    cancel it and drain the cancellation so the title-model *call* does not outlive the
    run. Past ``announcing`` there is no model call left to abandon — only a local write
    and an emit — so that one is waited out instead: cancelling there is precisely what
    would strand a persisted name with no event to announce it."""
    if namer is None or namer.task.done():
        return
    if namer.announcing.is_set():
        with suppress(Exception):  # already logged by the namer
            await namer.task
        return
    _ = namer.task.cancel()
    with suppress(asyncio.CancelledError):
        await namer.task


async def reap_title(namer: Namer | None) -> None:
    """Safety net: if the turn raised or was cancelled before the title was
    consumed above, don't let the detached title-model call outlive the run."""
    if namer is not None and not namer.task.done():
        if not namer.announcing.is_set():
            # Still waiting on the title model — nothing committed yet, so a
            # bare cancel is safe and this path is unwinding anyway.
            _ = namer.task.cancel()
        else:
            # Past `announcing` there is no model call left to abandon, only the
            # write and the emit, which must not be split. Left detached, it
            # would race the stream close in `RunRegistry._execute`'s finally and
            # lose `conversation.titled` — the name reaching the database but
            # never the client. Shielded so an unwinding *cancellation* still
            # leaves the task alive to finish its write; on that path the await
            # itself aborts and the event is genuinely lost, which is the
            # accepted cost of a Stop landing in this exact window (the title is
            # in the database and appears on reload). On the error path — not
            # cancelled — the await completes and the frame rides the open
            # stream as it should.
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(namer.task)
