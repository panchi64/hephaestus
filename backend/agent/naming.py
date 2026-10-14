"""Naming a fresh conversation, and getting out of the way when it can't.

``title.py`` is the model call — given some messages, produce a short name. This is
everything around it: when a thread is eligible to be named, the two paths that name one,
persisting the result without ever clobbering a name the operator chose, and announcing it
on the run's stream.

Two paths, because the two orchestrators reach completion differently:

- A **fresh chat turn** starts the namer up front (:func:`start_title`), concurrently with
  the answer, and announces the moment the name lands — so naming adds no post-answer
  delay. The trade is that the task outlives the awaited turn, and abandoning it needs
  care: before the name exists, cancelling costs nothing; past that point the persist and
  the emit must not be split, or the name reaches the database but never the client.
  :class:`Namer` carries exactly that distinction.
- A **resumed turn** has no prompt in hand and no window to overlap with, so it names
  afterwards from the just-persisted history (:func:`maybe_title`).

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
from runs import ConversationTitled, Run
from services.conversations import ConversationStore

from .title import generate_title, title_from_history

logger = logging.getLogger(__name__)


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
        except Exception:  # noqa: BLE001 — a title lookup must not block the notify
            summary = None
        if summary is not None and summary.title:
            return summary.title
    return "this conversation"


async def maybe_title(
    run: Run,
    *,
    title: TitleContext | None,
    store: ConversationStore | None,
    conversation_id: str | None,
    is_first_turn: bool,
) -> None:
    """Auto-name a fresh conversation from the operator's opening message.

    The resume path's namer: a first turn that parked for approval is named once it
    resumes to completion. The user's first message is read from the just-persisted
    history rather than threaded in (the resume has no ``prompt`` in hand). The
    initial chat orchestrator instead titles concurrently via :func:`start_title` /
    :func:`start_title` so it adds no post-answer delay. The title reflects what the operator
    asked — the assistant's reply is deliberately not fed to the namer. Guards:

    - ``is_first_turn`` (no prior messages) is the cheap pre-filter that skips the
      model call on continuation turns;
    - :meth:`ConversationStore.set_title_if_absent` is the authoritative guard —
      it fills only a blank title, so an operator-named thread is never clobbered,
      and we announce ``conversation.titled`` only when it actually set the name.

    Emitted before the orchestrator returns (before ``run.ended``) so the open
    stream carries it. Best-effort throughout: any failure leaves the thread
    untitled without disturbing the finished turn."""
    if not is_first_turn or title is None or store is None or conversation_id is None:
        return
    try:
        name = await title_from_history(
            title.model,
            await store.history(conversation_id),
            reasoning_off=title.settings,
            timeout_s=get_settings().title_timeout_s,
            max_tokens=get_settings().title_max_tokens,
        )
        await announce_title(run, name, store=store, conversation_id=conversation_id)
    except Exception:  # noqa: BLE001 — titling is best-effort, not turn-critical
        logger.warning("auto-titling failed for %s", conversation_id, exc_info=True)


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
    ``conversation.titled`` is announced only when it actually set the name. Shared
    by both the concurrent (:func:`start_title`) and resume (:func:`maybe_title`)
    paths."""
    if not name or store is None or conversation_id is None:
        return
    if await store.set_title_if_absent(conversation_id, name):
        _ = run.emit(ConversationTitled(conversation_id=conversation_id, title=name))


@dataclass(frozen=True)
class Namer:
    """The concurrent auto-namer: its task, plus the point past which cancelling would
    do damage. ``announcing`` is set the instant generation yields a name, immediately
    before the persist+emit — so an abandoning caller can tell "still waiting on the
    title model" (cancel freely) from "committing the name" (let it finish)."""

    task: asyncio.Task[None]
    announcing: asyncio.Event


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
    The task persists and emits ``conversation.titled`` **itself**, rather than handing
    a name back for the orchestrator to announce after the turn: the utility call
    typically resolves in a second or two, and deferring the announcement to the end of
    a long tool-using turn would leave the operator staring at an unnamed thread for the
    whole run even though the name was ready almost immediately. :func:`_settle_title`
    only waits for this task to finish before ``run.ended``, so the open stream still
    carries the event.

    Returns ``None`` when titling is off or there is nothing to name from. The call stays
    bounded by ``title_timeout_s``, and is best-effort throughout: any failure leaves the
    thread untitled without disturbing the turn."""
    if title is None or not prompt:
        return None
    announcing = asyncio.Event()

    async def _name() -> None:
        try:
            name = await generate_title(
                title.model,
                prompt,
                reasoning_off=title.settings,
                timeout_s=get_settings().title_timeout_s,
                max_tokens=get_settings().title_max_tokens,
            )
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
            raise  # a park/cancel abandoning the name (see `_discard_title`)
        except Exception:  # noqa: BLE001 — titling is best-effort, not turn-critical
            logger.warning("auto-titling failed for %s", conversation_id, exc_info=True)

    return Namer(task=asyncio.create_task(_name()), announcing=announcing)


async def settle_title(namer: Namer | None) -> None:
    """Wait for the concurrent namer to finish before the orchestrator returns, so a
    name that lands in the last moments of the turn still rides the open stream (the
    task announces it itself — see :func:`start_title`). Usually already done by the
    time the answer is; swallows the task's own failure, which it has already logged."""
    if namer is None:
        return
    with suppress(Exception):
        await namer.task


async def discard_title(namer: Namer | None) -> None:
    """Abandon a concurrently-started title (the turn parked, raised, or was cancelled):
    cancel it and drain the cancellation so the title-model *call* does not outlive the
    run. Past ``announcing`` there is no model call left to abandon — only a local write
    and an emit — so that one is waited out instead: cancelling there is precisely what
    would strand a persisted name with no event to announce it."""
    if namer is None or namer.task.done():
        return
    if namer.announcing.is_set():
        await settle_title(namer)
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
            # would race the stream close in `RunRegistry._run`'s finally and
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
