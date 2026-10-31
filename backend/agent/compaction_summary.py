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

- **The tool calls never run.** The tool definitions stay in the request so the prefix is
  unchanged, and the instructions ask for text. A reply that calls a tool anyway is retried
  once under ``tool_choice='none'``; the node that would execute a call is never reached.
- **It is marked a side run** (``agent.emit.SIDE_RUN``), and its stream goes nowhere but
  the summary deltas. The capabilities that observe a request each handle that on their
  own terms: ``MeasureOverhead`` measures and emits into nothing, so ``Run.context_overhead``
  is untouched; ``AnnounceInjections`` and ``WatchPrefix`` hold state on themselves for the
  life of the agent, so they step aside rather than mark the brief announced or remember a
  request the turn never sent; ``ReinjectSystemPrompt`` and the tool-search and code-mode
  capabilities run as they would on any request, because they shape the prefix that has to
  match; the harness's ``WarnOnCacheBusts`` keeps its state per run and may usefully warn.
- **Only the briefing is kept.** The model may think — its normal reasoning settings
  apply — but a ``ThinkingPart`` is never part of the summary, and a ``<think>`` block
  inlined into the content is stripped before the text can become the thread's memory. So
  is the ``<analysis>`` pass the instructions ask for ahead of the briefing.

No call is bounded by time or output length: the summary is the thread's only memory of
what it replaces.

**Tool-sourced facts stay marked as data.** The checkpoint is replayed as a user-shaped
message, which is the most authoritative voice in the history, so the one section that
repeats what a web page or a document said is fenced before it is stored. What is copied
in verbatim lives in :mod:`agent.compaction_carry`.
"""

from __future__ import annotations

from typing import Protocol

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

from core.compaction_sections import first_section_start, replace_section, section_body
from core.text import strip_think_blocks
from core.untrusted import neutralize_tags, new_nonce, untrusted_fence, untrusted_preamble
from prompts.compaction import (
    COMPACT_ANALYSIS_CLOSE,
    COMPACT_ANALYSIS_OPEN,
    COMPACT_FOCUS_LEAD,
    COMPACT_FOCUS_TAG,
    COMPACT_INSTRUCTIONS,
    COMPACT_TOOLS_SECTION,
)
from tools import RunDeps

from .emit import SIDE_RUN
from .factory import TurnAgent
from .history import drop_dangling_tool_calls, merge_consecutive_requests


class DeltaSink(Protocol):
    """Where a fold's summary text goes while it is being written. Synchronous and
    fire-and-forget: it is a progress signal, so it must not be able to fail a fold or slow
    the model call down. ``restart`` discards the text sent so far."""

    def __call__(self, text: str, *, restart: bool = False) -> None: ...


#: The settings a summary is retried under when the first answer called a tool. Only the
#: tool choice, and no ``max_tokens``: the endpoint's own ceiling is the only one.
FOLD_SETTINGS: ModelSettings = {"tool_choice": "none"}


class SummaryCalledTool(Exception):
    """The model answered the request for a summary with a tool call.

    The call is never executed: a side run that acted on the world would be a turn nobody
    asked for. Some local servers ignore ``tool_choice='none'`` too, so a retry under it
    can still raise this, and the fold fails."""


def compact_instructions(focus: str | None = None) -> str:
    """The request a fold appends, with the operator's focus when they gave one."""
    if not focus or not focus.strip():
        return COMPACT_INSTRUCTIONS
    safe = neutralize_tags(focus.strip(), (COMPACT_FOCUS_TAG,))
    return (
        f"{COMPACT_INSTRUCTIONS}\n\n{COMPACT_FOCUS_LEAD}\n"
        f"<{COMPACT_FOCUS_TAG}>\n{safe}\n</{COMPACT_FOCUS_TAG}>"
    )


def summary_request(
    messages: list[ModelMessage], *, focus: str | None = None
) -> list[ModelMessage]:
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
        [*replay, ModelRequest(parts=[UserPromptPart(compact_instructions(focus))])]
    )


async def write_summary(
    agent: TurnAgent,
    deps: RunDeps,
    messages: list[ModelMessage],
    *,
    focus: str | None = None,
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
    half a section, and a ``<think>`` block cannot be recognised until it closes.

    Asked first with the turn's own settings, so the request reads the conversation from
    the provider's cache (a ``tool_choice`` change invalidates the messages cache). A
    model that answers with a tool call is asked once more under :data:`FOLD_SETTINGS`."""
    history = summary_request(messages, focus=focus)
    try:
        return await _write_once(agent, deps, history, settings=None, on_delta=on_delta)
    except SummaryCalledTool:
        if on_delta is not None:
            on_delta("", restart=True)
        return await _write_once(agent, deps, history, settings=FOLD_SETTINGS, on_delta=on_delta)


async def _write_once(
    agent: TurnAgent,
    deps: RunDeps,
    history: list[ModelMessage],
    *,
    settings: ModelSettings | None,
    on_delta: DeltaSink | None,
) -> str | None:
    async with agent.iter(
        None,
        deps=deps,
        message_history=history,
        model_settings=settings,
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
    return strip_analysis(strip_think_blocks(text)).strip() or None


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


def strip_analysis(text: str) -> str:
    """``text`` without the leading ``<analysis>`` pass.

    Only a block that opens the text is the pass we asked for; one further down is quoted
    from somewhere. An unclosed block runs to the first heading of ours after it, and with
    none the whole text was analysis."""
    stripped = text.lstrip()
    if not stripped.lower().startswith(COMPACT_ANALYSIS_OPEN):
        return text
    rest = stripped[len(COMPACT_ANALYSIS_OPEN) :]
    close = rest.lower().find(COMPACT_ANALYSIS_CLOSE)
    if close >= 0:
        return rest[close + len(COMPACT_ANALYSIS_CLOSE) :].strip()
    start = first_section_start(rest)
    return "" if start is None else rest[start:].strip()
