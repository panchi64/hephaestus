"""Naming a fresh conversation — a fast, best-effort-reasoning-off utility call.

After a brand-new conversation's first turn lands, the chassis asks the utility
model for a short descriptive title so the operator never has to name a thread.
The thread is named for what the *operator* asked — only the first user message is
fed to the namer, never the assistant's reply — so the title mirrors the request,
not the answer. The task is trivial, so the call is deliberately cheap. The engine
emits the result as ``conversation.titled`` and persists it; the frontend reveals
it with a typing animation.

Reasoning is requested *off* — provider-shaped, decided in :mod:`services.reasoning`;
the caller resolves the model and its reasoning-off
:class:`~pydantic_ai.settings.ModelSettings` together (the registry does this) and
hands both in, so this module stays free of per-lab levers. But some runtimes ignore
that lever (e.g. LM Studio drops OpenAI ``chat_template_kwargs``, and the Qwen 2507+
line dropped the ``/no_think`` soft-switch), so a model can reason anyway. We don't
fight that here — we *tolerate* it: the caller hands in a generous ``max_tokens`` so a
``<think>`` block has room to clear *and* still emit the title, and :func:`_clean` runs
``core.text.strip_think_blocks`` over the output — the same call the compaction summarizer
makes, since both face the identical runtime. A strict endpoint with no off-switch simply
reasons within that budget.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import cast

from pydantic_ai import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.text import strip_think_blocks
from prompts.utility import TITLE_INSTRUCTIONS

from .meta import make_utility_agent

logger = logging.getLogger(__name__)

# Output-capped base settings; the caller's reasoning-off settings (and per-call
# ``max_tokens``) are merged on top. ``max_tokens`` is response-level (it bounds the
# model's output, never the prompt), so it must fit everything the model emits before
# the title — including a ``<think>`` block when a runtime ignores the reasoning-off
# lever. The cap is a ceiling, not a cost: when reasoning is genuinely off the model
# emits the handful of title words and stops early, so the headroom is free; when it
# reasons anyway the think block is response tokens too, so the caller raises the cap
# (see :data:`core.config` ``title_max_tokens`` / ``retitle_max_tokens``) to clear it.
# This base value is the floor for callers that pass none (e.g. tests).
_BASE_SETTINGS: ModelSettings = {"max_tokens": 1024, "temperature": 0.3}

# Trim the user message fed to the namer — the topic is in the opening, and a
# long body only slows the call without sharpening the title.
_EXCERPT = 600
# A manual re-title feeds every operator turn (not just the opening), so it needs a
# wider budget to span the conversation's arc — still bounded so the call stays cheap.
FULL_EXCERPT = 2400
# The hard ceiling on a stored title. The prompt asks for 3-6 words, so this is the
# backstop for a model that ignored the budget rather than the normal path — and it
# sits at the width the sidebar can actually show, because a name that renders
# ellipsized is a name the operator has to hover to read.
_MAX_TITLE_LEN = 48

_USER_PARTS = frozenset({"UserPromptPart"})


def _part_text(message: ModelMessage, part_names: frozenset[str]) -> str:
    """Join the text of the named parts. A part's content is usually a string, but
    a multimodal user prompt carries a sequence (text + images/files); pull the
    text out of those too so a regenerate verifier / auto-title still sees the
    words rather than an empty string."""
    chunks: list[str] = []
    for part in message.parts:
        if type(part).__name__ not in part_names:
            continue
        content: object = getattr(part, "content", None)
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list | tuple):
            chunks.extend(item for item in cast(Sequence[object], content) if isinstance(item, str))
    return " ".join(chunks).strip()


def first_user_text(messages: list[ModelMessage]) -> str:
    """The first user prompt in a history — the topic the thread is named for."""
    for message in messages:
        text = _part_text(message, _USER_PARTS)
        if text:
            return text
    return ""


def all_user_text(messages: list[ModelMessage]) -> str:
    """Every user prompt in a history, joined in order — the whole arc of what the
    operator asked. Feeds a manual re-title so the name reflects where the thread
    actually went, not just its opening line. Assistant replies and tool output are
    never included, keeping the small title model off injectable content (the same
    trusted-channel discipline as the first-turn namer, just over more turns)."""
    chunks: list[str] = []
    for message in messages:
        text = _part_text(message, _USER_PARTS)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def last_user_text(messages: list[ModelMessage]) -> str:
    """The latest user prompt in a history — the request a regenerate re-answers
    (so the verifier still has the prompt to judge against when no new one was sent)."""
    for message in reversed(messages):
        text = _part_text(message, _USER_PARTS)
        if text:
            return text
    return ""


def _clean(raw: str) -> str | None:
    """Sanitize the model's reply into a single-line title, or None if empty.

    Models tend to wrap titles in quotes, prepend ``Title:``, or add a trailing
    period; strip those so the stored/animated name is clean. A reasoning model the
    runtime didn't keep off prepends a ``<think>…</think>`` block — drop it first so
    the title is read from the words after it, not from the reasoning.

    An over-long reply is cut at the last word boundary that fits rather than at the
    character cap: a name is read at a glance in a list, and a chopped-off word reads
    as a bug in a way a shorter name never does. A single word longer than the cap has
    no boundary to fall back to, so that one is cut hard.

    The three strips run **to a fixed point**, because they feed each other and a
    single pass leaves the combinations behind. ``Title: "Some Thread".`` is the one
    that showed up: the quote strip runs first and finds a ``T`` and a ``.`` at the
    ends, the prefix strip then exposes the quotes it would have taken, and the title
    reaches the sidebar still wearing them. Looping costs one extra pass on the
    ordinary reply, which already has nothing to strip."""
    raw = strip_think_blocks(raw)
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    while True:
        before = line
        line = line.strip("\"'`").strip()
        for prefix in ("title:", "title -", "thread:"):
            if line.lower().startswith(prefix):
                line = line[len(prefix) :].strip()
        line = line.rstrip(".").strip()
        if line == before:
            break
    if not line:
        return None
    if len(line) <= _MAX_TITLE_LEN:
        return line
    # One char past the cap, so a boundary landing exactly on it still counts.
    head = line[: _MAX_TITLE_LEN + 1]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else line[:_MAX_TITLE_LEN]).strip()


async def generate_title(
    model: Model,
    prompt: str,
    *,
    reasoning_off: ModelSettings | None = None,
    timeout_s: float | None = None,
    excerpt: int = _EXCERPT,
    max_tokens: int | None = None,
) -> str | None:
    """Name a conversation from the user's opening message, or None on any failure.

    The thread is named for what the operator asked — ``prompt`` is the first user
    message and nothing else is fed in, so the assistant's reply never colours the
    title. Best-effort and isolated: titling is a cosmetic nicety, so a model error,
    timeout, or empty reply degrades to "no auto-title" rather than disturbing the
    turn that produced the answer. ``reasoning_off`` is merged over the base caps
    (its source — :mod:`services.reasoning` — owns the per-provider lever);
    ``timeout_s`` bounds how long the call may run so a slow utility model cannot
    hold the run open. ``excerpt`` caps the prompt fed in — the opening message for
    the auto-titler, a wider span for a manual re-title over every operator turn.
    ``max_tokens`` overrides the base output cap so a runtime that ignores the
    reasoning-off lever still has room to think *and* emit the title."""
    settings: ModelSettings = {**_BASE_SETTINGS, **(reasoning_off or {})}
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    agent = make_utility_agent(model, output_type=str, instructions=TITLE_INSTRUCTIONS)
    user = prompt[:excerpt]
    try:
        run = agent.run(user, model_settings=settings)
        # asyncio.TimeoutError is an Exception subclass (caught below); CancelledError
        # is not, so a cancelled run still propagates rather than degrading to a title.
        result = await (asyncio.wait_for(run, timeout_s) if timeout_s else run)
    except Exception as exc:  # noqa: BLE001 — titling is best-effort, never fails a turn
        logger.warning("conversation title generation failed: %s", exc)
        return None
    return _clean(result.output)


async def title_from_history(
    model: Model,
    history: list[ModelMessage],
    *,
    full: bool = False,
    reasoning_off: ModelSettings | None = None,
    timeout_s: float | None = None,
    max_tokens: int | None = None,
) -> str | None:
    """Name a conversation from its stored history — the one place that pairs *which*
    operator turns feed the namer with *how much* of them. ``full`` selects the
    manual re-title's scope (every operator turn, the wider :data:`FULL_EXCERPT`
    budget); the default is the auto-titler's (opening message, :data:`_EXCERPT`).
    Either way only the operator's turns are read — never the assistant's replies or
    tool output. Returns None when there's nothing to name from or the model call
    fails, so both the auto-titler and the manual route share one extraction→generate
    step and only differ in how they persist/announce the result."""
    prompt = all_user_text(history) if full else first_user_text(history)
    if not prompt:
        return None
    return await generate_title(
        model,
        prompt,
        reasoning_off=reasoning_off,
        timeout_s=timeout_s,
        excerpt=FULL_EXCERPT if full else _EXCERPT,
        max_tokens=max_tokens,
    )
