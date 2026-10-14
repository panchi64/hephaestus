"""The summary itself — how the turn's own model writes it, and how the text that comes
back is handled.

**The summary is the conversation continuing.** It is written by the agent the turn runs
on, against exactly the replay that agent would be sent — the same brief, the same tool
array, the same messages, normalised the same way — with one user message appended asking
for the briefing. So the request shares its whole prefix with the one the model last
served, and a local engine reads the thread from the KV cache it already holds instead of
prefilling it again for a different model. It is also the model that did the work, reading
the work in its own format rather than a re-rendered transcript of it.

Three things keep it a *side* run rather than a turn:

- **``tool_choice='none'``**, so the tool definitions stay in the request (the prefix is
  unchanged) while the model is told to answer in text. Some local servers ignore the
  setting; a reply that calls a tool anyway is a failed fold, and the call never runs —
  the node that would execute it is never reached.
- **It is marked a side run** (``agent.emit.SIDE_RUN``), and its stream goes nowhere but
  the summary deltas. The capabilities that observe a request each handle that on their
  own terms: ``MeasureOverhead`` measures and emits into nothing, so ``Run.context_overhead``
  is untouched; ``AnnounceInjections`` and ``WatchPrefix`` hold state on themselves for the
  life of the agent, so they step aside rather than mark the brief announced or remember a
  request the turn never sent; ``ReinjectSystemPrompt`` and the tool-search and code-mode
  capabilities run as they would on any request, because they shape the prefix that has to
  match; the harness's ``WarnOnCacheBusts`` keeps its state per run and may usefully warn.
- **Only the answer text is kept.** The model may think — its normal reasoning settings
  apply — but a ``ThinkingPart`` is never part of the summary, and a ``<think>`` block
  inlined into the content is stripped before the text can become the thread's memory.

No call is bounded by time or output length: the summary is the thread's only memory of
what it replaces.

**Handling what comes back is text-only.** The summary is asked for in fixed sections
(``prompts/utility.py``'s ``COMPACT_INSTRUCTIONS``) for two reasons that need the text to
be *parseable*, not merely readable:

- **Anchors survive every fold.** Exact paths, ids, names and numbers are what a
  re-summarized summary loses first — each pass paraphrases a little more until the file
  path the thread was working on is "the config file". So a second fold carries the
  previous checkpoint's Anchors section forward **verbatim** instead of asking a model to
  restate it.
- **Tool-sourced facts stay marked as data.** The checkpoint is replayed as a
  user-shaped message, which is the most authoritative voice in the history; the one
  section that repeats what a web page or a document said is fenced before it is stored,
  so a fold cannot promote fetched text into an instruction the model trusts.

Both operations are text-in/text-out and model-free: a summary that comes back without the
headings degrades to "no carry-forward, nothing fenced" rather than failing the fold.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai import (
    Agent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.settings import ModelSettings

from core.compaction_sections import (
    replace_section,
    section_body,
    section_key,
    without_fenced,
)
from core.text import strip_think_blocks
from core.untrusted import new_nonce, untrusted_fence, untrusted_preamble
from prompts.compaction import (
    COMPACT_ANCHORS_SECTION,
    COMPACT_INSTRUCTIONS,
    COMPACT_MARKER,
    COMPACT_TOOLS_SECTION,
)
from services.conversation_view import flatten_content
from tools import RunDeps

from .emit import SIDE_RUN
from .factory import TurnAgent
from .history import drop_dangling_tool_calls, merge_consecutive_requests

#: Where a fold's summary text goes while it is being written. Synchronous and
#: fire-and-forget: it is a progress signal, so it must not be able to fail a fold or slow
#: the model call down, and the caller that supplies one is the caller that owns a stream.
type DeltaSink = Callable[[str], None]

#: The run-level settings a summary is written under, merged over the agent's own. Only
#: the tool choice: the model's reasoning settings are the session's, and there is
#: deliberately no ``max_tokens`` — the endpoint's own ceiling is the only one.
FOLD_SETTINGS: ModelSettings = {"tool_choice": "none"}


class SummaryCalledTool(Exception):
    """The model answered the request for a summary with a tool call.

    ``tool_choice='none'`` asks it not to, and some local servers ignore that. The call is
    never executed — a side run that acted on the world would be a turn nobody asked for —
    so the fold fails instead."""


def summary_request(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The history a summary is written against: ``messages`` normalised exactly as a
    turn's replay is (``agent/prelude.py``), then the compaction instructions as one more
    user message.

    The normalisation is what makes the prefix match. A turn strips a trailing call that
    never got its result and merges consecutive requests before the model sees them, so a
    summary request that skipped either would diverge from the replay the engine has
    cached at the first place they differ. The final merge is for a stretch that ends on a
    request (a turn recorded before it was answered): the instructions join it, as the
    library would join them anyway."""
    replay = merge_consecutive_requests(drop_dangling_tool_calls(messages))
    return merge_consecutive_requests(
        [*replay, ModelRequest(parts=[UserPromptPart(COMPACT_INSTRUCTIONS)])]
    )


async def write_summary(
    agent: TurnAgent,
    deps: RunDeps,
    messages: list[ModelMessage],
    *,
    on_delta: DeltaSink | None = None,
) -> str | None:
    """The briefing the turn's own model writes for ``messages``, or ``None`` when it wrote
    none. Raises :class:`SummaryCalledTool` for a reply that called a tool, and lets a
    model error propagate for the caller to report.

    Driven node by node rather than with ``run_stream``: the request node is streamed for
    its text and the walk stops there, so a tool call in the reply is inspected and never
    handed to the node that would execute it.

    ``on_delta`` receives the answer text as it arrives. **The deltas are handed over
    raw** — unstripped, not yet merged with carried anchors and not yet fenced — because
    they are for a human watching a pause go by. Everything that makes the text *safe to
    store* is done to the settled string, never to a delta: a fence cannot be applied to
    half a section, and a ``<think>`` block cannot be recognised until it closes."""
    async with agent.iter(
        None,
        deps=deps,
        message_history=summary_request(messages),
        model_settings=FOLD_SETTINGS,
        metadata={SIDE_RUN: True},
    ) as agent_run:
        node = agent_run.next_node
        while not Agent.is_model_request_node(node):
            if Agent.is_end_node(node):
                return None
            node = await agent_run.next(node)
        async with node.stream(agent_run.ctx) as stream:
            async for event in stream:
                if on_delta is None:
                    continue
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    if event.part.content:
                        on_delta(event.part.content)
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    on_delta(event.delta.content_delta)
        response = agent_run.ctx.state.message_history[-1]
    if not isinstance(response, ModelResponse):
        return None
    if any(isinstance(part, ToolCallPart) for part in response.parts):
        raise SummaryCalledTool
    # Only the answer. A `ThinkingPart` is the model's reasoning about the summary, not the
    # summary; and a runtime that inlines its chain-of-thought as a `<think>…</think>` block
    # in the content would otherwise store that scratch work as the thread's memory, to be
    # replayed as established fact for the rest of the conversation.
    text = "".join(part.content for part in response.parts if isinstance(part, TextPart))
    return strip_think_blocks(text).strip() or None


def fence_tool_facts(summary: str) -> str:
    """Wrap the "From tools and documents" section's body in an untrusted fence.

    A no-op when the summarizer didn't emit that section (or emitted it empty) — the rest
    of the summary is the operator's and the assistant's own words, which are exactly what
    the checkpoint is supposed to speak with."""
    body = section_body(summary, COMPACT_TOOLS_SECTION)
    if not body:
        return summary
    nonce = new_nonce()
    fenced = f"{untrusted_preamble(nonce)}\n{untrusted_fence(body, nonce, source='tools')}"
    return replace_section(summary, COMPACT_TOOLS_SECTION, fenced)


def carried_anchors(messages: list[ModelMessage]) -> list[str]:
    """The Anchors lines of any checkpoint among the messages being folded.

    A fold's input contains the previous checkpoint whenever the thread has compacted
    before; it is recognised by the label the store wrote in front of it, the same marker
    the reviewer and the operator's transcript key on."""
    lines: list[str] = []
    for text in _checkpoint_texts(messages):
        lines.extend(_bullets(section_body(text, COMPACT_ANCHORS_SECTION)))
    return _dedupe(lines)


def merge_anchors(summary: str, carried: list[str]) -> str:
    """Fold ``carried`` anchor lines into the summary's Anchors section, keeping the new
    ones first and dropping duplicates. Appends the section when the summary has none."""
    if not carried:
        return summary
    existing = _bullets(section_body(summary, COMPACT_ANCHORS_SECTION))
    merged = _dedupe(existing + carried)
    if merged == existing:
        return summary
    body = "\n".join(merged)
    if section_body(summary, COMPACT_ANCHORS_SECTION) is None:
        return f"{summary.rstrip()}\n\n## {COMPACT_ANCHORS_SECTION}\n{body}"
    return replace_section(summary, COMPACT_ANCHORS_SECTION, body)


def _bullets(body: str | None) -> list[str]:
    """A section's non-empty lines, as written."""
    if not body:
        return []
    return [line.rstrip() for line in body.splitlines() if line.strip()]


def _dedupe(lines: list[str]) -> list[str]:
    """Order-preserving dedupe, comparing on the line's words rather than its bullet
    marker or spacing — the same anchor rewritten as "- x" and "* x" is one anchor."""
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = section_key(line)
        if key and key not in seen:
            seen.add(key)
            out.append(line)
    return out


def _checkpoint_texts(messages: list[ModelMessage]) -> list[str]:
    """The stored checkpoint texts among a fold's messages, newest last — **with every
    fenced region removed**, so what is carried forward is only what the summarizer wrote
    in its own voice."""
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                text = flatten_content(part.content).strip()
                if text.startswith(COMPACT_MARKER):
                    texts.append(without_fenced(text))
    return texts
