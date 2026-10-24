"""The per-conversation work summary — what this thread actually did, in two or three
sentences, for the operator coming back to it an hour or a day later.

It hangs off a background sweep (:mod:`services.work_summaries`) rather than off a turn:
the account is only worth writing once the work has stopped moving, and a summary
regenerated per turn would be a utility-model call per turn for a band nobody is looking
at while they are still typing in the thread.

**The input is where the design is.** The titler next door (:mod:`agent.title`) feeds its
model *only* the operator's own turns, which keeps a small, unguarded model entirely off
injectable content. This cannot do that and still answer its question: what the *agent*
did lives in the agent's own answers and tool calls, which is model-derived text, and a
page it fetched may be quoted inside either. So the trusted-channel discipline is replaced
with a fence rather than dropped — the operator's turns stand in the clear because they are
the one voice in the thread that is theirs, and everything model-authored goes inside
:func:`core.untrusted.untrusted_fence` under one shared nonce announced once at the top,
for the reason a stored summary always needs it: the output is stored
and shown to the operator as the workspace's own account of the thread, so a fetched page
must not be able to speak in that voice.

**Fenced per turn, not once for the whole transcript.** One block would be cheaper, but the
summary's whole job is to say what was asked *and then* what was done about it, and a
transcript that lists every operator turn and then every model turn has thrown away the
pairing the summary is made of.

**Tool *returns* are excluded outright.** They are simultaneously the bulkiest part of a
thread and the most injectable, and they are redundant here: each call carries the model's
own ``narration`` — one sentence saying why it made that call (``tools/describe.py``) — and
the answer text says what came of it. A fold has to preserve what a tool returned because the thread
continues on it; a re-entry band does not.

Best-effort throughout, like the titler: a model error, a timeout or an empty reply degrades
to "no summary this sweep" and the band simply shows the last one (or nothing). Nothing here
raises.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from core.text import strip_think_blocks, truncate_on_boundary
from core.untrusted import new_nonce, untrusted_fence, untrusted_preamble
from prompts.utility import WORK_SUMMARY_INSTRUCTIONS
from services.conversation_view import flatten_content
from tools.describe import NARRATION_ARG

from .meta import make_utility_agent

logger = logging.getLogger(__name__)

# Output-capped base settings, merged under the caller's reasoning-off settings exactly as
# the titler's are. The generous `max_tokens` is the same tolerance for the same runtime:
# reasoning is requested off, some runtimes ignore the lever, and a `<think>` block has to
# have room to clear *and* still leave the two or three sentences behind it.
_BASE_SETTINGS: ModelSettings = {"max_tokens": 2048, "temperature": 0.3}

# The ceiling on the transcript handed to the model. A re-entry band is a cheap background
# call, not a fold: a thread long enough to overflow this has almost certainly been folded
# already, and the summary is better served by the whole arc rendered thinly than by the
# first half rendered in full. The cut is at a turn boundary (see `_render`), keeping the
# **latest** turns, because where the work stands is the half of the answer that decays.
_EXCERPT = 12000

# The hard ceiling on a stored summary, against a model that ignored the prompt's two-or-
# three-sentence budget. Not a display limit: the recap band collapses long text itself.
MAX_SUMMARY_LEN = 2000


def _call_line(part: ToolCallPart) -> str:
    """One tool call as a line: its namespaced name, plus the model's own reason for it.

    The ``narration`` is offered in the schema and stripped again before validation
    (``tools/narration.py``), which strips it from the *arguments a tool function sees* and
    not from the call part in the history — so the sentence the model wrote is still here,
    which is the whole reason this reads args at all. Arguments themselves are left out:
    they are the tool's business, and a path or a query already appears in the narration
    when it matters."""
    narration = part.args_as_dict().get(NARRATION_ARG)
    reason = f": {narration}" if isinstance(narration, str) and narration.strip() else ""
    return f"called {part.tool_name}{reason}"


def _turn_lines(message: ModelMessage) -> tuple[list[str], list[str]]:
    """One message split into the two channels the transcript keeps apart: the operator's
    own words, and everything the model authored.

    Tool returns fall out here rather than being rendered and dropped later — a return is
    neither of these two channels, and there is nowhere in this transcript it belongs."""
    trusted: list[str] = []
    untrusted: list[str] = []
    if isinstance(message, ModelRequest):
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                text = flatten_content(part.content).strip()
                if text:
                    trusted.append(f"OPERATOR: {text}")
    else:
        for part in message.parts:
            if isinstance(part, TextPart):
                text = part.content.strip()
                if text:
                    untrusted.append(f"ASSISTANT: {text}")
            elif isinstance(part, ToolCallPart):
                untrusted.append(f"ASSISTANT {_call_line(part)}")
    return trusted, untrusted


def work_transcript(history: list[ModelMessage], *, excerpt: int = _EXCERPT) -> str:
    """The thread rendered for the summariser: one untrusted preamble, then the operator's
    turns in the clear with each stretch of model-authored text fenced under that
    preamble's nonce. Empty when there was nothing worth rendering.

    ``excerpt`` bounds the whole thing. Over budget, whole leading blocks go: the opening of
    a thread is recoverable from its title, where it ended up is not."""
    nonce = new_nonce()
    preamble = untrusted_preamble(nonce)
    blocks: list[str] = []
    for message in history:
        trusted, untrusted = _turn_lines(message)
        blocks.extend(trusted)
        if untrusted:
            blocks.append(untrusted_fence("\n".join(untrusted), nonce))
    if not blocks:
        return ""
    # Walk back from the end summing lengths, rather than re-joining the whole list once
    # per dropped block: a long thread is hundreds of blocks and tens of thousands of
    # characters, and the naive loop rebuilds the entire transcript on every iteration to
    # learn one number. One pass over the lengths gives the same cut.
    budget = max(excerpt - len(preamble) - 2, 0)
    kept = 0
    used = 0
    for block in reversed(blocks):
        # `+ 2` for the separator this block will be joined with, charged for every block
        # but the first — the same arithmetic the join below performs.
        cost = len(block) + (2 if kept else 0)
        if used + cost > budget:
            break
        used += cost
        kept += 1
    if not kept:
        return ""
    return f"{preamble}\n\n" + "\n\n".join(blocks[len(blocks) - kept :])


def clean_summary(raw: str) -> str | None:
    """The model's reply as a storable summary, or None when it said nothing.

    A leaked ``<think>`` block goes first, for the reason the titler strips one: the
    reasoning-off lever is best-effort, and a runtime that ignored it would otherwise have
    the model's scratch work stored as the thread's account of itself.

    An over-long reply is cut at a word boundary and marked with an ellipsis. The titler
    cuts silently because a name is read at a glance and a trailing "..." would read as part
    of it; a summary is prose, and prose that stops mid-thought with no marker reads as the
    feature having broken rather than as the model having overrun its budget."""
    text = " ".join(strip_think_blocks(raw).split()).strip()
    if not text:
        return None
    if len(text) <= MAX_SUMMARY_LEN:
        return text
    return truncate_on_boundary(text, MAX_SUMMARY_LEN - 1) + "…"


async def summarize_work(
    model: Model,
    history: list[ModelMessage],
    *,
    reasoning_off: ModelSettings | None = None,
    timeout_s: float | None = None,
    max_tokens: int | None = None,
) -> str | None:
    """What this thread did, in two or three sentences — or None on any failure.

    ``reasoning_off`` is merged over the base caps (its source, :mod:`services.reasoning`,
    owns the per-provider lever); ``timeout_s`` bounds the call so a stuck utility model
    cannot hold a sweep open; ``max_tokens`` overrides the output cap so a runtime that
    ignores the reasoning-off lever has room to think *and* still answer.

    Best-effort and isolated: the band is a convenience, so every failure — a model error,
    a timeout, an empty reply, a thread with nothing in it — degrades to None and is logged
    at warning, never raised at the sweep."""
    transcript = work_transcript(history)
    if not transcript:
        return None
    settings: ModelSettings = {**_BASE_SETTINGS}
    if reasoning_off:
        settings.update(reasoning_off)
    if max_tokens is not None:
        settings["max_tokens"] = max_tokens
    agent = make_utility_agent(model, output_type=str, instructions=WORK_SUMMARY_INSTRUCTIONS)
    try:
        run = agent.run(transcript, model_settings=settings)
        # asyncio.TimeoutError is an Exception subclass (caught below); CancelledError is
        # not, so a sweep cancelled at shutdown still propagates rather than degrading to
        # a summary.
        result = await (asyncio.wait_for(run, timeout_s) if timeout_s else run)
    except Exception:  # the summary is best-effort, never fails a sweep
        logger.warning("work summary generation failed", exc_info=True)
        return None
    return clean_summary(result.output)
