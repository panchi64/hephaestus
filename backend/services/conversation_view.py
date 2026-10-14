"""Project a conversation's ``ModelMessage`` history into a render-ready view.

The durable record stores full-fidelity Pydantic AI ``ModelMessage`` blobs so a
cold session rehydrates exactly. The frontend needs a flat shape instead: an
ordered list of user/assistant turns, each assistant turn carrying its reasoning,
answer and tool calls in the order they were emitted, the calls stitched to their
results.

This is the static-history counterpart to the live translator in
``agent/translate.py`` — the same part→domain mapping, applied to a settled
message list rather than a stream. Both share ``core.serde.jsonable`` for the
tool-result coercion; the shared helper lives in ``core`` so this lower layer
need not import the orchestrator.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic_ai import (
    BinaryContent,
    ModelRequest,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.messages import ModelMessage, ModelRequestPart, ModelResponsePart

from core.compaction_sections import SummarySection, summary_sections
from core.serde import jsonable
from core.text import chars_to_tokens
from services.answers import ASK_USER_TOOL, AnsweredQuestion, parse_answer, questions_of
from services.commands.spec import Invocation
from services.subagents.report import direction_body, message_body, report_body, report_prose

if TYPE_CHECKING:  # a type, not a dependency — nothing here calls into the run substrate
    from runs import TurnOverhead

#: Where a compaction checkpoint records **why** it folded, on its own
#: ``ModelRequest.metadata``. Written by ``ConversationStore.record_compaction`` and read
#: back here, so a cold read reports the same cause the live ``conversation.compacted``
#: event did. It rides the sealed message blob rather than a column: the reason is a fact
#: about one message, and a schema migration for a three-value string the projection is
#: the only reader of would buy nothing. A checkpoint written before this existed simply
#: has no such key, which the projection reports as "unknown" rather than guessing.
COMPACTION_REASON_KEY = "compaction_reason"

#: Where a turn's `@` file references ride — the same metadata channel, chosen for the
#: same reasons. The marker naming them is already in the persisted prompt, so this is not
#: how the *model* learns about them; it is the structured form the operator's chips are
#: drawn from, which reading back out of the marker's prose would be a parser nobody wants.
FILE_REFS_KEY = "file_refs"

#: How the `@` marker opens. Declared here, in the module that has to *remove* it, and
#: imported by the one that writes it (`agent/file_refs.py`) — never the other way round,
#: since `services` sits below `agent`.
#:
#: The marker has to persist, so that a regenerate replaying this turn still tells the
#: model which files were meant. It must not be *rendered*, because the bubble it lands in
#: is captioned with the operator's name and they did not write it. Those two pull in
#: opposite directions and a sentinel is what settles them: one exact string, owned by the
#: pair of functions on either side of it, rather than a parser trying to recognise prose.
FILE_REFS_MARKER_OPEN = "[The operator referenced these files with @:"

#: Where the slash command a turn was sent with rides — the same metadata channel again,
#: and the only one of the three the **backend** reads back rather than the operator.
#:
#: A command's expansion is never persisted (``services/commands/expand``), so a regenerate
#: replaying this turn's history would otherwise hand the model the bare ``/reviewer …``
#: token and nothing else. Writing down the *invocation* instead of the expansion is what
#: closes that without the stale copy: the name and the argument are facts about what the
#: operator did, and the block is built again from whatever that name resolves to now.
#:
#: Read by ``ConversationStore.turn_stamps`` and re-expanded by the chat route, which is
#: the layer that holds the registry. Nothing in ``agent/`` resolves a command name.
COMMAND_KEY = "command"

_NO_IDS: frozenset[str] = frozenset()


@dataclass
class ToolImageView:
    """An image a tool handed back — a browser screenshot, today.

    A tool that returns pixels returns them *for the model*: Pydantic AI puts the bytes
    in a user-role part so a vision model can see them, since no provider accepts an
    image inside a tool result. That is a wire detail, not something the operator took
    part in — so the image belongs to the call that produced it, in the work log, and
    this is what carries it there.

    Base64 rather than a URL because a tool result is already inline in this projection
    (``ToolView.result``), and a second convention for the same turn's output would mean
    a second lifetime to reason about."""

    media_type: str
    data: str  # base64, no data-URI scheme — the renderer adds it


@dataclass
class ToolView:
    id: str
    name: str
    args: dict[str, object]
    status: str = "running"  # "running" | "ok" | "error"
    result: object = None
    error: str | None = None
    images: list[ToolImageView] = field(default_factory=list)
    # `ask_user` only — the questions this call asked and what the operator said to each,
    # taken apart from the prose the call returned (`services/answers.py`). The live stream
    # carries the same list on `question.answered`, built from the replies before they were
    # rendered, so the card a reload draws is the card the operator watched arrive. Empty
    # for every other tool, and for an asking call the operator never answered.
    answers: list[AnsweredQuestion] = field(default_factory=list)
    # The `run_code` call this one was made from, when a script made it; None for a call
    # the model made directly. Rebuilt from that call's result (`_script_calls`).
    parent_tool_call_id: str | None = None


def _as_dict(value: object) -> dict[str, object] | None:
    """``value`` as a string-keyed dict when it is a dict. Keys are not inspected."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _as_list(value: object) -> list[object] | None:
    """``value`` as a list when it is a list or tuple."""
    return list(cast("Sequence[object]", value)) if isinstance(value, list | tuple) else None


def _field(item: object, name: str) -> object:
    """One field of a part a script's result recorded — the library object while the
    thread is still in memory, the plain dict it serialized to once it has been stored."""
    as_dict = _as_dict(item)
    if as_dict is not None:
        return as_dict.get(name)
    value: object = getattr(item, name, None)
    return value


def _script_calls(part: ToolReturnPart) -> list[ToolView]:
    """The calls a ``run_code`` script made, rebuilt as rows of their own.

    A script's calls never appear as ``ToolCallPart``s in the history — the model made one
    call, and that call made the rest — so the only record of them is what the code-mode
    capability wrote on the script's own result: ``tool_calls`` in the order they started
    and ``tool_returns`` for those that returned or were refused
    (``pydantic_ai_harness.code_mode``). A call with no return raised; the record keeps no
    account of why, so the row says only that it did not complete. The live stream showed
    the reason (``tool.failed``), and a reload is the one place that is not kept.
    """
    raw_metadata: object = getattr(part, "metadata", None)
    metadata = _as_dict(raw_metadata)
    if metadata is None or not metadata.get("code_mode"):
        return []
    calls = _as_dict(metadata.get("tool_calls") or {})
    returns = _as_dict(metadata.get("tool_returns") or {})
    if calls is None or returns is None:
        return []
    views: list[ToolView] = []
    for call_id, call in calls.items():
        raw_args = _field(call, "args")
        if isinstance(raw_args, str):
            args: dict[str, object] = ToolCallPart(tool_name="", args=raw_args).args_as_dict()
        else:
            args = _as_dict(raw_args) or {}
        view = ToolView(
            id=str(call_id),
            name=str(_field(call, "tool_name") or ""),
            args=args,
            parent_tool_call_id=part.tool_call_id,
        )
        returned = returns.get(call_id)
        if returned is None:
            view.status = "error"
            view.error = "The call raised before it returned."
        elif _field(returned, "outcome") == "denied":
            view.status = "error"
            view.error = flatten_content(_field(returned, "content"))
        else:
            view.status = "ok"
            view.result = jsonable(_field(returned, "content"))
        views.append(view)
    return views


@dataclass
class MessageView:
    # "compaction" is a chassis-authored divider, not a turn either party took: the
    # conversation was folded into a summary here (`content`), and the turns above it are
    # what the model now replays as that summary.
    #
    # "subagent" is the other turn nobody in the room took: a sub-agent the agent launched
    # has reported back, and the report reached the thread as the one shape the model has
    # for a message from outside itself. Left as "user" it would claim the operator typed
    # something they have not even read.
    role: str  # "user" | "assistant" | "compaction" | "subagent"
    content: str = ""
    # On a "subagent" turn that is a message rather than a report, the handle of the agent
    # that sent it.
    author: str | None = None
    tools: list[ToolView] = field(default_factory=list)
    timestamp: datetime | None = None
    # The model that produced this assistant turn (the last response's model_name —
    # the one that wrote the answer). None for user turns and turns older than this
    # projection. Surfaced so the UI can show what a chat actually last ran on.
    model: str | None = None
    # The id of the tree node that *defines this turn's branch point* — the user
    # request for a user turn, the first response for an assistant turn. It is what
    # the frontend addresses to regenerate / edit / delete / switch this turn.
    id: str = ""
    # Position among this turn's sibling versions (0-based) and how many there are.
    # 1 ⇒ no alternatives; >1 ⇒ the turn has been regenerated/edited.
    version_index: int = 0
    version_count: int = 1
    # The operator's durable pin on this turn (a bookmark), from the branch node.
    pinned: bool = False
    # Upload ids the operator attached to this (user) turn — the frontend renders them
    # as file chips. Empty for assistant turns and turns sent without attachments.
    attachment_ids: list[str] = field(default_factory=list)
    # Workspace-relative paths the operator named with `@` on this (user) turn — chips
    # again, beside the attachments. Read off the request's metadata rather than a column;
    # empty for assistant turns and for turns that referenced nothing.
    file_refs: list[str] = field(default_factory=list)
    # Set when the run behind this assistant turn ended `outcome: "blocked"` (a
    # usage/loop/context/time bound) — the human-readable reason. Filled in by the
    # store from the branch node, like `pinned`; None for every other turn.
    blocked_reason: str | None = None
    # `role="compaction"` only — what this divider actually cost, so the UI can say
    # "14 messages folded, ~62k → ~4k" without counting or estimating anything itself.
    # `messages_compacted` is how many messages the summary stands in for;
    # `tokens_before`/`tokens_after` are coarse `estimate_tokens` figures over the folded
    # messages and over the summary. The same three values ride the live
    # `conversation.compacted` event, computed the same way, so a live divider and the
    # one a reload draws report identical numbers. 0 on every other role.
    messages_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    # `role="compaction"` only — what triggered the fold (`threshold`/`overflow`/`manual`),
    # read off the checkpoint's own metadata. `None` on every other role, and on a
    # checkpoint recorded before the reason was stored: a fold whose cause was never
    # written down has to read as absent, not as the most common one.
    compaction_reason: str | None = None
    # `role="compaction"` only — `content` split into the roster sections a divider
    # renders, by the same `summary_sections` the live `conversation.compacted` event
    # derives its own from. Shared rather than mirrored in the client because the roster
    # is a security boundary: only a heading naming one of *our* sections ends a section,
    # so a fetched page's own heading cannot open one and carry what follows it out of the
    # untrusted fence. Empty on every other role.
    sections: list[SummarySection] = field(default_factory=list)
    # `role="assistant"` only — the turn's parts in the order the model emitted them,
    # which `content`/`tools` flatten away. A reload replays this so the
    # transcript keeps the think → call → text → … sequence the live stream drew,
    # instead of regrouping it into one lane per kind. Adjacent passages of one kind
    # merge, the same rule the live fold applies to deltas. Empty on every other role.
    segments: list[Segment] = field(default_factory=list)


SegmentKind = Literal["thinking", "text", "tool"]


@dataclass
class Segment:
    """One step of an assistant turn in emission order: a reasoning or answer passage
    carrying its text, or a tool call naming the ``ToolView`` it refers to (by id,
    so the call's data is not carried twice)."""

    kind: SegmentKind
    text: str = ""
    tool_call_id: str | None = None


def _append_passage(view: MessageView, kind: SegmentKind, text: str) -> None:
    """Add a passage to the turn's ordered segments, extending the trailing one when it
    is the same kind — each kind change starts a new segment, as live deltas do.

    An empty passage records nothing, because the live translator emits no delta for
    one: kept, it would split two passages of one kind that the stream drew as one."""
    if not text:
        return
    last = view.segments[-1] if view.segments else None
    if last is not None and last.kind == kind:
        last.text += text
    else:
        view.segments.append(Segment(kind=kind, text=text))


#: The class of message content measured for the readout but not counted in the footprint
#: wholesale. Reasoning is a model's own scratch work, and whether it comes back is
#: **template-dependent** rather than never: see :func:`_tag_mode_reasoning_chars`, which
#: adds back the half that does. Excluding the class here and adding one half back is not a
#: roundabout way of writing a filter — the breakdown answers a different question ("what is
#: this thread spending on reasoning?", which has its own lever) and wants the whole figure.
_UNSENT_CLASSES = frozenset({"reasoning"})

#: The ``ThinkingPart.id`` the library uses for thinking it read out of the response's own
#: ``content`` rather than out of a provider field. See :func:`_tag_mode_reasoning_chars`.
_TAG_MODE_ID = "content"


def estimate_tokens(messages: Sequence[ModelMessage]) -> int:
    """A coarse token estimate for what ``messages`` cost **on the wire**.

    The fallback for endpoints that report no usage — local servers commonly return
    ``input_tokens=0``, which ``services.conversations.context_footprint`` (rightly) treats
    as unmeasured rather than as a real zero. Without an estimate, conversation compaction
    would be dead on exactly the self-hosted endpoints this workspace is built for.

    Two things it deliberately does not count. **The standing brief**, because a
    ``SystemPromptPart`` at the head of the history is already measured by
    ``agent/overhead.py`` and :func:`estimate_footprint` adds it back once, not twice.
    And **binary parts**: a retained inline image is base64 in the blob, and measuring it
    by character length would read a single screenshot as hundreds of thousands of phantom
    tokens. Ignoring image tokens under-counts instead — the safe direction, since the
    run's own context-overflow stop is still there behind this.

    **Reasoning is counted in half**, which used to be none at all on the claim that it is
    never re-sent. It is: the library maps a ``ThinkingPart`` back onto an outgoing request,
    and in tag mode it becomes ordinary assistant *text* that no chat template can remove.
    What genuinely vanishes is the field-mode half, which Qwen, DeepSeek and GLM templates
    strip from the rendered prompt. Counting both would be the worse error of the two, and
    the reason is :func:`agent.summarize.should_compact`: it takes the **larger** of the
    provider's reported prompt size and this estimate, so an inflated estimate would override
    an accurate server-reported figure on exactly the templates that do the stripping, and
    fold those threads early.

    Prose and serialized structure convert at their own rates (``core.text``) rather than
    one shared divisor, so this figure and the context readout's split are the same
    measurement rather than two that drift apart.

    It lives here rather than beside the compaction code that first needed it because the
    projection needs the same number: a compaction divider reports what it folded, and a
    cold read must report the same figure the live event did."""
    sent = sum(
        chars_to_tokens(chars.prose, chars.structured)
        for name, chars in message_class_chars(messages).items()
        if name not in _UNSENT_CLASSES
    )
    return int(sent + chars_to_tokens(_tag_mode_reasoning_chars(messages), 0))


def _tag_mode_reasoning_chars(messages: Sequence[ModelMessage]) -> int:
    """Characters of thinking that will be re-sent as assistant text, whatever the template.

    The library decides per part. Thinking it read out of a **provider field** goes back into
    that same field (``reasoning_content`` and its kin), which the Qwen, DeepSeek and GLM
    chat templates drop when they render the prompt — so it costs the next request nothing.
    Thinking it read out of the response's own ``content`` — marked with
    ``id == 'content'``, or carrying no id at all — is re-wrapped in thinking tags and
    appended to the assistant message's **text**. There is no template hook that removes
    that: it is indistinguishable from anything else the assistant said.

    A part whose id names a *different* provider's field takes the tag path too, and this
    deliberately does not try to reproduce that case. It needs the model the next request
    will go to, which nothing at this seam knows; it arises only on a history moved between
    providers; and the direction of the error is the safe one — undercounting, behind the
    run's own overflow stop, rather than overriding a server-reported figure.

    Prose, never structured: this content is English going into a text field.
    """
    return sum(
        len(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ThinkingPart) and part.id in (None, "", _TAG_MODE_ID)
    )


def estimate_footprint(
    messages: Sequence[ModelMessage],
    overhead: TurnOverhead | None,
    *,
    fallback_overhead_tokens: int,
) -> int:
    """What a request carrying ``messages`` would weigh, brief and tool schemas included.

    :func:`estimate_tokens` measures the conversation; a request is the conversation
    *plus* the standing brief and every tool schema, which on a full catalog is a five-
    figure number all by itself. Measuring only the messages is what let a thread sit
    comfortably under the compaction threshold and still overflow on the next turn.

    ``overhead`` is the thread's last recorded ``TurnOverhead`` (characters, split system
    vs. tools) — converted at the prose and JSON rates respectively, since a brief is
    prose and a schema is JSON. When the thread has none — every turn it ever ran predates
    the per-thread record, or it has not run one yet — ``fallback_overhead_tokens`` stands
    in. **A guess, not a zero**: assuming no overhead is assuming the request is smaller
    than it can possibly be, which is the one error a limit guard must not make."""
    overhead_tokens = (
        fallback_overhead_tokens
        if overhead is None
        else int(chars_to_tokens(overhead.system, overhead.tools))
    )
    return estimate_tokens(messages) + overhead_tokens


@dataclass(frozen=True)
class ContentChars:
    """Message characters, split by how they tokenize.

    Prose and JSON do not tokenize at the same rate — measured against cl100k, prose runs
    about 4.8 characters per token and serialized JSON about 4.1, because JSON spends a
    third of its characters on punctuation and short repeated keys. Keeping the two apart
    is what lets every measurement built on this — the context readout's split, the
    footprint the compaction trigger projects — apply the right rate to each rather than
    inflating whichever part is prose by about a fifth. The rates themselves live in
    ``core.text``, so there is one pair of them for the whole codebase."""

    prose: int
    structured: int


#: What each kind of message part counts as, for the context readout's message split.
#:
#: The four classes are the ones an operator can *do* something about. A thread that is
#: mostly ``tool_results`` is a thread whose tools return too much (or are called too
#: often); one that is mostly ``conversation`` wants a compaction or a fresh thread; one
#: that is mostly ``reasoning`` wants a lower thinking budget. Splitting any finer — by
#: role, by turn — would name things the operator has no separate lever for.
#:
#: ``SystemPromptPart`` is deliberately **absent**: it lives at the head of the history,
#: but it is the standing brief, and ``agent/overhead.py`` already counts it there.
#: Counting it in both places was a real double-count — small, but it made the parts sum
#: to more than the whole before scaling pulled them back, quietly shrinking every other
#: row to pay for it.
_PART_CLASSES: tuple[tuple[type, str], ...] = (
    (ThinkingPart, "reasoning"),
    (ToolCallPart, "tool_calls"),
    (ToolReturnPart, "tool_results"),
    (RetryPromptPart, "tool_results"),
    (UserPromptPart, "conversation"),
    (TextPart, "conversation"),
)

#: The order the classes are reported in — largest-typical first, and stable regardless
#: of what a given thread happens to contain, so two threads' breakdowns read the same
#: way. A class with nothing in it is dropped by the caller rather than shown as a zero.
MESSAGE_CLASSES = ("conversation", "tool_results", "tool_calls", "reasoning")


def _part_class(part: ModelRequestPart | ModelResponsePart) -> str | None:
    """Which class a part's characters belong to, or None to leave it uncounted."""
    if type(part).__name__ == "SystemPromptPart":
        return None
    for kind, name in _PART_CLASSES:
        if isinstance(part, kind):
            return name
    # Anything the library grows later still lands somewhere real rather than vanishing
    # from a total that is supposed to be exhaustive.
    return "conversation"


def _part_chars(part: ModelRequestPart | ModelResponsePart) -> tuple[int, int]:
    """One part as (prose, structured) characters.

    The classification is by content shape, not by part type, because that is exactly the
    distinction that tokenizes differently: a string is prose wherever it appears, and a
    dict is JSON by the time the model sees it. Binary content contributes nothing, on
    the same grounds as :func:`estimate_tokens`.

    A tool call is the exception that has to be read off another attribute — its payload
    is ``args``, not ``content`` — and it used to be scored as nothing at all. On a
    thread of many small calls that is a real slice of the window, and it was landing in
    the one place guaranteed to mislead: unattributed, so scaling spread it across the
    parts that *were* measured."""
    if isinstance(part, ToolCallPart):
        return 0, len(_content_text(part.args))
    content: object = getattr(part, "content", None)
    if isinstance(content, str):
        return len(content), 0
    items = _as_list(content)
    if items is not None:
        strings = [item for item in items if isinstance(item, str)]
        if len(strings) == len(items):
            return sum(len(item) for item in strings), 0
    return 0, len(_content_text(content))


def message_class_chars(messages: Sequence[ModelMessage]) -> dict[str, ContentChars]:
    """Characters across ``messages``, split by class and by how each tokenizes.

    Keyed by the classes in :data:`MESSAGE_CLASSES`; a class with no characters is
    omitted rather than reported as zero, so a thread that has never called a tool grows
    no empty rows."""
    prose: dict[str, int] = {}
    structured: dict[str, int] = {}
    for message in messages:
        for part in message.parts:
            name = _part_class(part)
            if name is None:
                continue
            text, json_text = _part_chars(part)
            prose[name] = prose.get(name, 0) + text
            structured[name] = structured.get(name, 0) + json_text
    return {
        name: ContentChars(prose=prose.get(name, 0), structured=structured.get(name, 0))
        for name in MESSAGE_CLASSES
        if prose.get(name, 0) or structured.get(name, 0)
    }


def message_chars(messages: Sequence[ModelMessage]) -> ContentChars:
    """Characters across ``messages``, split prose vs. serialized-structure.

    The whole-conversation figure, summed from :func:`message_class_chars` so the total
    and the per-class rows can never disagree about what the thread weighs."""
    by_class = message_class_chars(messages).values()
    return ContentChars(
        prose=sum(part.prose for part in by_class),
        structured=sum(part.structured for part in by_class),
    )


def _content_text(content: object) -> str:
    """A part's content as the text a model would actually be sent.

    **Structured content counts.** A tool result is usually a dict — a search's hits, a
    file listing, a page of rows — and it reaches the model as serialized JSON, keys
    included. Reading only `str` content scored every one of those as zero, which on a
    tool-heavy thread is most of the window: the footprint fallback under-reported by
    multiples, so auto-compaction held off on precisely the threads filling up fastest,
    and the context breakdown credited the whole weight to the tool *schemas* instead of
    to the results they returned.

    **Anything unrecognised still counts as nothing**, which is what keeps the binary
    rule intact. A `BinaryContent` — a retained screenshot, base64 in the blob — falls
    through to `""` rather than being measured by its character length, because a single
    image would otherwise read as hundreds of thousands of phantom tokens. Under-counting
    is the safe direction and the run's own context-overflow stop is still behind this."""
    if isinstance(content, str):
        return content
    # `bool` before the numbers: it is an `int` subclass, and "True" is not what a model
    # is sent for a boolean field anyway.
    if isinstance(content, bool):
        return str(content)
    if isinstance(content, int | float):
        return str(content)
    mapping = _as_dict(content)
    if mapping is not None:
        # Keys as well as values: they are serialized alongside the data and are a real
        # share of a wide row's tokens.
        return " ".join(f"{key} {_content_text(value)}" for key, value in mapping.items())
    items = _as_list(content)
    if items is not None:
        return " ".join(_content_text(item) for item in items)
    return ""


def flatten_content(content: object) -> str:
    """Flatten a message part's content (str or multimodal sequence) to text.

    A multimodal turn keeps its text parts; non-text parts (images, files) are
    represented by a single ``[attachment]`` marker so an image-only turn still
    renders as a turn rather than vanishing into an empty bubble. Defensive — the
    composer is text-only today, so this only guards future multimodal input."""
    if isinstance(content, str):
        return content
    items = _as_list(content)
    if items is not None:
        text = " ".join(part for part in items if isinstance(part, str)).strip()
        if text:
            return text
        return "[attachment]" if items else ""
    return ""


#: The most an image may weigh before the work log declines to carry it, in source bytes.
#: The browser harness caps a capture at 5 MB of PNG, which is ~6.7 MB of base64 on a
#: single stream frame *and* again in every read of the conversation thereafter. A
#: viewport screenshot is a fraction of this; what approaches it is a full-page capture of
#: something enormous, and shipping one would spend the transcript's whole load budget on
#: one picture. The call still reports what it did in words — that is the part the model
#: read and the part the operator needs.
MAX_TOOL_IMAGE_BYTES = 2_000_000


def tool_images(content: object) -> list[ToolImageView]:
    """The images in a tool's returned content, in order.

    Shared with the live translator so a screenshot looks the same warm and cold. Only
    images: a tool may hand back a PDF or an audio clip for the model to read, and a
    work-log card has nothing useful to do with either. Oversized ones are dropped rather
    than shrunk — re-encoding here would put an image pipeline in a projection."""
    listed = _as_list(content)
    items = [content] if listed is None else listed
    return [
        ToolImageView(media_type=item.media_type, data=item.base64)
        for item in items
        if isinstance(item, BinaryContent)
        and item.media_type.startswith("image/")
        and len(item.data) <= MAX_TOOL_IMAGE_BYTES
    ]


def _attach_tool_images(
    user_parts: list[UserPromptPart],
    returns: list[ToolReturnPart | RetryPromptPart],
    by_call: dict[str, ToolView],
) -> None:
    """Hang a tool-return request's image payloads on the calls that produced them.

    Pydantic AI appends one user part per image-bearing call, in call order, *after* all
    the tool parts — the pairing is order-preserving but carries no call id, so it can
    only be recovered positionally. With one image-bearing call in the step, which is
    what a screenshot is, that is exact. A step where two calls both return pixels and
    a third sits between them could hang an image on the neighbouring card; the image
    still reaches the work log, and the alternative is persisting an association Pydantic
    AI does not give us for a case the browser toolset does not produce.

    The live path does not have this problem — ``FunctionToolResultEvent`` carries the
    call id and its content together — so a warm render is always exact."""
    candidates = [p for p in returns if isinstance(p, ToolReturnPart)]
    for part, tool_return in zip(user_parts, candidates, strict=False):
        images = tool_images(part.content)
        if not images:
            continue
        tool = by_call.get(tool_return.tool_call_id)
        if tool is not None:
            tool.images.extend(images)


def _attach_answers(tool: ToolView) -> None:
    """Put a settled ``ask_user`` call's answers back on its row, as structure.

    Live, the answers reach the client on ``question.answered`` before they are ever
    rendered. Cold, the only record that survived is the prose in the call's own result —
    so it is read back here rather than by the client, for the same reason the questions
    were parsed here in the first place: one side owns the format, and it is the side that
    writes it.

    The questions come from the **call's** arguments and the answers from its **result**,
    which is what keeps the pair honest: a card is drawn only where both halves of the
    exchange are present, and an unanswered or unreadable one leaves the row as it was."""
    if tool.name != ASK_USER_TOOL or not isinstance(tool.result, str):
        return
    tool.answers = parse_answer(questions_of(tool.args), tool.result)


def _compaction_reason(message: object) -> str | None:
    """Why this checkpoint folded, off its own ``ModelRequest.metadata`` — or ``None``.

    ``None`` covers both a checkpoint recorded before the reason was stored and any
    metadata shape this doesn't recognise. The divider is written to read correctly
    without the segment, so an absent reason costs nothing; a *wrong* one would tell the
    operator the provider forced a fold they asked for themselves."""
    raw: object = getattr(message, "metadata", None)
    metadata = _as_dict(raw)
    if metadata is None:
        return None
    reason = metadata.get(COMPACTION_REASON_KEY)
    return reason if isinstance(reason, str) and reason else None


def strip_file_refs_marker(text: str) -> str:
    """The operator's own words, with the chassis's `@` block taken back off.

    The block is in the persisted turn on purpose — see ``FILE_REFS_MARKER_OPEN`` — and
    the bubble it would otherwise show up in carries the operator's name. The references
    are still reported, as chips, from the structured field beside this.

    Splits on the sentinel rather than matching a shape, so a message that merely *talks*
    about the marker is untouched unless it reproduces it exactly, and a marker whose
    wording changes cannot quietly stop being stripped.
    """
    head, sep, _tail = text.partition(FILE_REFS_MARKER_OPEN)
    return head.rstrip() if sep else text


def stamped_file_refs(message: object) -> list[str]:
    """The `@` paths stamped on this request, or an empty list.

    Read defensively for the same reason ``_compaction_reason`` is: a turn recorded before
    this existed has no such key, and metadata is a free-form dict the library round-trips
    without inspecting — so anything unrecognised reads as "none", never as a crash in the
    projection every transcript load goes through.
    """
    raw: object = getattr(message, "metadata", None)
    metadata = _as_dict(raw)
    if metadata is None:
        return []
    refs = metadata.get(FILE_REFS_KEY)
    items = _as_list(refs)
    if items is None or isinstance(refs, tuple):
        return []
    return [ref for ref in items if isinstance(ref, str) and ref]


def stamped_command(message: object) -> Invocation | None:
    """The slash command this request was sent with, or ``None``.

    Read as defensively as the two above, and for one more reason besides: this one is fed
    straight back into a resolver on the regenerate path, so a malformed stamp must read as
    "no command" rather than as a name made of the wrong type. A turn recorded before this
    existed simply has no key, and replays exactly as it did before — which is the correct
    outcome, not a degraded one.
    """
    raw: object = getattr(message, "metadata", None)
    metadata = _as_dict(raw)
    if metadata is None:
        return None
    stamp = _as_dict(metadata.get(COMMAND_KEY))
    if stamp is None:
        return None
    name = stamp.get("name")
    argument = stamp.get("argument", "")
    if not isinstance(name, str) or not name:
        return None
    return Invocation(name=name, argument=argument if isinstance(argument, str) else "")


def _summary_part(message: ModelMessage) -> UserPromptPart | None:
    """A checkpoint's summary part — the one the divider renders.

    Found by type rather than by position: a fold carries the dormant groups the folded
    stretch had revealed forward on the checkpoint, as a leading
    ``ToolAvailabilityDeltaPart``, so the summary is no longer reliably ``parts[0]``. That
    part is bookkeeping the model reads as a change in what it may call and the operator
    reads as nothing at all, which is why the divider looks past it rather than rendering
    it."""
    return next((part for part in message.parts if isinstance(part, UserPromptPart)), None)


def project_tree(
    nodes: Sequence[tuple[str, ModelMessage]], *, compacted_ids: frozenset[str] = _NO_IDS
) -> list[MessageView]:
    """Fold an active-path ``(node_id, ModelMessage)`` sequence into ordered
    user/assistant view turns.

    ``compacted_ids`` names the conversation-compaction checkpoints among ``nodes``. Each
    becomes its own ``role="compaction"`` view — a divider carrying the summary — instead
    of the user turn its underlying ``UserPromptPart`` would otherwise read as. This stays
    purely order-driven: the caller hands the nodes in the order it wants them rendered.

    A checkpoint's divider also reports **what the fold cost** — how many messages it
    stands in for, and a coarse before/after token estimate — derived here rather than
    persisted. The caller hands the nodes in operator order, where a checkpoint sits
    immediately after the last node it covers, so the messages since the *previous*
    checkpoint (that checkpoint included) are exactly the set the fold replaced — the same
    set the live ``conversation.compacted`` event counted, so the two agree. Its **reason**
    is the one part not re-derived: it is a fact about why the chassis acted, not about the
    messages, so it is read back off the checkpoint's metadata where the fold wrote it.

    One turn = one view. A user turn is a request carrying a ``UserPromptPart``.
    An assistant turn is the run of everything after it until the next user turn —
    one or more ``ModelResponse`` messages plus the interleaved tool-return
    requests — **merged into a single assistant view** whose ``segments`` keep the
    order its reasoning, calls and answer were emitted in, with each call stitched to
    its result. This matches the live stream,
    which renders one assistant bubble per turn, so a cold read and a warm one look
    identical.

    Each view's ``id`` is the node that *defines the turn's branch point*: the user
    request for a user turn, the first response for an assistant turn — i.e. the
    node whose siblings are this turn's versions. Version index/count are filled in
    later by the store, which holds the tree.

    Tool calls surface on the assistant turn that issued them; a later request's
    ``ToolReturnPart``/``RetryPromptPart`` mutates the same (shared) ``ToolView``
    already attached, so results stitch back in place.
    """
    views: list[MessageView] = []
    by_call: dict[str, ToolView] = {}
    # The turn each call was issued on, so the rows a script's result rebuilds land on the
    # same turn as the script, whatever else has happened since.
    owner: dict[str, MessageView] = {}
    assistant: MessageView | None = None  # the open assistant turn, if any
    # The messages a checkpoint would fold: everything since the previous one (which is
    # itself part of the set — a second fold summarizes the first summary plus what
    # followed it, exactly as `compaction_plan` collects them).
    since_checkpoint: list[ModelMessage] = []
    for node_id, message in nodes:
        if node_id in compacted_ids:
            # A conversation-compaction checkpoint: its own turn, not the operator's.
            # It closes any open assistant turn, exactly as a user turn would.
            assistant = None
            summary_part = _summary_part(message)
            summary_text = flatten_content(summary_part.content) if summary_part else ""
            views.append(
                MessageView(
                    role="compaction",
                    content=summary_text,
                    sections=summary_sections(summary_text),
                    timestamp=None if summary_part is None else summary_part.timestamp,
                    id=node_id,
                    messages_compacted=len(since_checkpoint),
                    tokens_before=estimate_tokens(since_checkpoint),
                    tokens_after=estimate_tokens([message]),
                    compaction_reason=_compaction_reason(message),
                )
            )
            since_checkpoint = [message]
            continue
        since_checkpoint.append(message)
        if isinstance(message, ModelRequest):
            user_parts = [p for p in message.parts if isinstance(p, UserPromptPart)]
            returns = [p for p in message.parts if isinstance(p, ToolReturnPart | RetryPromptPart)]
            # A request carrying tool returns is the agent's own round-trip, even when it
            # *also* carries a user part: a tool that hands back pixels (a screenshot)
            # gets them appended here as user content, because no provider accepts an
            # image inside a tool result. Reading that as an operator turn spoke for
            # somebody who never said anything — and cost two more things, since opening
            # a user turn closed the assistant turn mid-work and skipped the very results
            # this branch exists to stitch.
            if user_parts and not returns:
                # A new user turn closes any open assistant turn.
                assistant = None
                part = user_parts[0]
                text = flatten_content(part.content)
                # A sub-agent's report arrives in the one shape the model has for a
                # message from outside itself, so on the way back out it would claim the
                # operator typed it — and they have not even seen it. Recognised by the
                # envelope `agent/injected.py` writes, which is a constant of ours rather
                # than a shape guessed at from the text.
                # The prose half only: a report may end with a machine-readable block of
                # findings, which is the panel's copy of the same report. Both in the
                # bubble would be the operator reading it twice, the second time as JSON.
                envelope = report_body(text)
                report = None if envelope is None else report_prose(envelope)
                # The same link read the other way: the launching agent's mid-flight
                # direction, in a sub-agent's own thread. Only the envelope is stripped —
                # the message stays a `user` turn, because in a thread nobody else can type
                # in, the agent that launched it *is* the one giving it direction, and a
                # role of its own would be a new vocabulary word for a distinction the
                # reader of that transcript cannot act on.
                direction = None if report is not None else direction_body(text)
                sent = None if report is not None else message_body(text)
                if sent is not None:
                    views.append(
                        MessageView(
                            role="subagent",
                            author=sent[0],
                            content=sent[1],
                            timestamp=part.timestamp,
                            id=node_id,
                        )
                    )
                    continue
                body = report if report is not None else direction
                views.append(
                    MessageView(
                        role="subagent" if report is not None else "user",
                        content=strip_file_refs_marker(text if body is None else body),
                        timestamp=part.timestamp,
                        id=node_id,
                        file_refs=stamped_file_refs(message),
                    )
                )
                continue
            # A tool-return request: stitch results into the open assistant turn.
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    tool = by_call.get(part.tool_call_id)
                    if tool is not None:
                        tool.status = "ok"
                        tool.result = jsonable(part.content)
                        _attach_answers(tool)
                        # A script's own calls follow it on the turn, each naming it as
                        # its parent — the order a client nests them in.
                        owner[part.tool_call_id].tools.extend(_script_calls(part))
                elif isinstance(part, RetryPromptPart):
                    tool = by_call.get(part.tool_call_id)
                    if tool is not None:
                        tool.status = "error"
                        tool.error = part.model_response()
            _attach_tool_images(user_parts, returns, by_call)
        else:
            if assistant is None:
                # First response of the turn — its node id is the branch point.
                assistant = MessageView(role="assistant", timestamp=message.timestamp, id=node_id)
                views.append(assistant)
            # A turn can span several responses (tool round-trips); the last one
            # carrying a name is the model that wrote the final answer.
            assistant.model = message.model_name or assistant.model
            for part in message.parts:
                if isinstance(part, TextPart):
                    assistant.content += part.content
                    _append_passage(assistant, "text", part.content)
                elif isinstance(part, ThinkingPart):
                    _append_passage(assistant, "thinking", part.content)
                elif isinstance(part, ToolCallPart):
                    tool = ToolView(
                        id=part.tool_call_id, name=part.tool_name, args=part.args_as_dict()
                    )
                    assistant.segments.append(Segment(kind="tool", tool_call_id=tool.id))
                    assistant.tools.append(tool)
                    by_call[part.tool_call_id] = tool
                    owner[part.tool_call_id] = assistant
    return views
