"""Project a conversation's ``ModelMessage`` history into a render-ready view.

The durable record stores full-fidelity ``ModelMessage`` blobs; the frontend needs an
ordered list of user/assistant turns, each assistant turn carrying its reasoning, answer
and tool calls in emission order, with calls stitched to their results.

This is the settled-history counterpart to the live translator in ``agent/translate.py``;
both share ``core.serde.jsonable`` for tool-result coercion, which lives in ``core`` so
this layer need not import the orchestrator.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic_ai import (
    BinaryContent,
    ModelRequest,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.messages import (
    ERROR_OUTCOMES,
    ModelMessage,
    ModelRequestPart,
    ModelResponsePart,
)

from core.compaction_sections import SummarySection, summary_sections
from core.diagnostics import is_platform_failure
from core.envelopes import ContentPart, result_parts, split_envelopes
from core.serde import jsonable
from core.text import chars_to_tokens
from core.untrusted import strip_envelope_note
from services.answers import ASK_USER_TOOL, AnsweredQuestion, parse_answer, questions_of
from services.commands.spec import Invocation
from services.notices import notice_body
from services.shapes import as_dict, as_list
from services.subagents.report import (
    direction_body,
    message_body,
    report_body,
    report_prose,
    report_sender,
)

if TYPE_CHECKING:
    from runs import TurnOverhead

#: Key on a compaction checkpoint's ``ModelRequest.metadata`` recording why it folded.
#: Written by ``ConversationStore.record_compaction``; a checkpoint without it reads as
#: "unknown".
COMPACTION_REASON_KEY = "compaction_reason"
#: Key on the same metadata listing what the fold re-read after the briefing: home paths,
#: and ``"plan"`` when the plan came back.
COMPACTION_RESTORED_KEY = "compaction_restored"

#: Request-metadata key holding a turn's `@` file references, the structured form the
#: operator's chips are drawn from.
FILE_REFS_KEY = "file_refs"

#: How the `@` marker opens. Defined here and imported by its writer
#: (`agent/file_refs.py`), since `services` sits below `agent`.
#:
#: The marker persists so a regenerate still tells the model which files were meant, but is
#: never rendered because the bubble it lands in is captioned with the operator's name. One
#: exact sentinel serves both rather than a parser recognising prose.
FILE_REFS_MARKER_OPEN = "[The operator referenced these files with @:"

#: How the attachment marker opens (`agent/attachments.py`). Same contract as the `@`
#: marker: it persists for regenerate and is never rendered; the files show as chips.
ATTACHMENTS_MARKER_OPEN = "[Attached file(s):"

#: Request-metadata key holding the slash command a turn was sent with; unlike the others,
#: the backend reads it back.
#:
#: A command's expansion is never persisted (``services/commands/expand``), so a regenerate
#: would replay a bare ``/reviewer …`` token. Storing the invocation lets the chat route
#: (which holds the registry; ``agent/`` resolves no command names) re-expand it against
#: whatever the name resolves to now. Read by ``ConversationStore.turn_stamps``.
COMMAND_KEY = "command"

_NO_IDS: frozenset[str] = frozenset()


@dataclass
class ToolImageView:
    """An image a tool handed back (a browser screenshot).

    Pydantic AI puts these bytes in a user-role part for the model, since no provider
    accepts an image inside a tool result; the operator took no part in that, so the image
    is attached to the call that produced it. Base64 rather than a URL, like the rest of
    ``ToolView.result``, to avoid a second lifetime."""

    media_type: str
    data: str  # base64, no data-URI scheme — the renderer adds it


@dataclass
class ToolView:
    id: str
    name: str
    args: dict[str, object]
    status: str = "running"  # "running" | "ok" | "error"
    result: object = None
    # `result` split into text and envelope parts (`core/envelopes.py`), or None when it
    # carries no envelope.
    result_parts: list[ContentPart] | None = None
    error: str | None = None
    images: list[ToolImageView] = field(default_factory=list)
    # `ask_user` only: the questions asked and the operator's reply to each, parsed from
    # the returned prose (`services/answers.py`). Matches the live `question.answered`
    # list. Empty for other tools and for an unanswered call.
    answers: list[AnsweredQuestion] = field(default_factory=list)
    # The `run_code` call that made this one, rebuilt from its result (`_script_calls`);
    # None for a call the model made directly.
    parent_tool_call_id: str | None = None
    #: The failure was the platform's own fault (``core/diagnostics.py``).
    platform: bool = False


def _field(item: object, name: str) -> object:
    """One field of a recorded script part: a library object in memory, a dict once stored."""
    recorded = as_dict(item)
    if recorded is not None:
        return recorded.get(name)
    value: object = getattr(item, name, None)
    return value


def _script_calls(part: ToolReturnPart) -> list[ToolView]:
    """The calls a ``run_code`` script made, rebuilt as rows of their own.

    A script's calls are not ``ToolCallPart``s in the history; the only record is the
    code-mode capability's metadata on the script's result: ``tool_calls`` in start order and
    ``tool_returns`` for those that returned or were refused (``pydantic_ai_harness.code_mode``).
    A call with no return raised, and the record keeps no reason, so a reload shows only
    that it did not complete.
    """
    raw_metadata: object = getattr(part, "metadata", None)
    metadata = as_dict(raw_metadata)
    if metadata is None or not metadata.get("code_mode"):
        return []
    calls = as_dict(metadata.get("tool_calls") or {})
    returns = as_dict(metadata.get("tool_returns") or {})
    if calls is None or returns is None:
        return []
    views: list[ToolView] = []
    for call_id, call in calls.items():
        raw_args = _field(call, "args")
        if isinstance(raw_args, str):
            args: dict[str, object] = ToolCallPart(tool_name="", args=raw_args).args_as_dict()
        else:
            args = as_dict(raw_args) or {}
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
        elif _field(returned, "outcome") in ("denied", "failed"):
            view.status = "error"
            view.error = flatten_content(_field(returned, "content"))
            view.platform = is_platform_failure(view.error)
        else:
            view.status = "ok"
            view.result = jsonable(_field(returned, "content"))
            view.result_parts = result_parts(view.result)
        views.append(view)
    return views


@dataclass
class MessageView:
    # "compaction" is a divider where the conversation was folded into a summary
    # (`content`). "subagent" is a sub-agent's message or report reaching the thread; as
    # "user" it would claim the operator typed it. "notice" is a chassis notice
    # (`services/notices.py`): `notice` names its kind, `content` is the body without the
    # envelope.
    role: str  # "user" | "assistant" | "compaction" | "subagent" | "notice"
    content: str = ""
    # On a "subagent" message, the sending agent's handle.
    author: str | None = None
    # On a "subagent" report, the reporting agent's handle.
    reporter: str | None = None
    notice: str | None = None
    # On a "user" turn carrying untrusted fences (an attachment's fallback text, a
    # routine's previous-run fence): the fences in order, `content` keeping the text around
    # them. None when there were none.
    parts: list[ContentPart] | None = None
    tools: list[ToolView] = field(default_factory=list)
    timestamp: datetime | None = None
    # The last response's model_name; None for user turns.
    model: str | None = None
    # The tree node that defines this turn's branch point (the user request, or an
    # assistant turn's first response); the frontend addresses it to regenerate, edit,
    # delete or switch.
    id: str = ""
    # Position among sibling versions (0-based) and their count; >1 means regenerated/edited.
    version_index: int = 0
    version_count: int = 1
    # The operator's pin (bookmark), from the branch node.
    pinned: bool = False
    # Upload ids attached to this user turn, rendered as file chips.
    attachment_ids: list[str] = field(default_factory=list)
    # Workspace-relative paths named with `@` on this user turn, read from request metadata.
    file_refs: list[str] = field(default_factory=list)
    # Human-readable reason when the run ended `outcome: "blocked"` (a usage, loop, context
    # or time bound); filled in by the store from the branch node, like `pinned`.
    blocked_reason: str | None = None
    # `role="compaction"` only: the divider's cost. `messages_compacted` is how many
    # messages the summary stands in for; `tokens_before`/`tokens_after` are coarse
    # `estimate_tokens` figures over the folded messages and the summary. The live
    # `conversation.compacted` event computes the same values, so live and reload agree.
    messages_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    # `role="compaction"` only: the trigger (`threshold`/`overflow`/`manual`) from the
    # checkpoint's metadata. None for a checkpoint recorded without one, which must not
    # read as the most common cause.
    compaction_reason: str | None = None
    # `role="compaction"` only: what the fold re-read after the briefing.
    restored: list[str] = field(default_factory=list)
    # `role="compaction"` only: `content` split by the same `summary_sections` the live
    # event uses. Server-side because the roster is a security boundary: only a heading
    # naming one of our sections ends a section, so a fetched page's heading cannot open
    # one and carry what follows out of the untrusted fence.
    sections: list[SummarySection] = field(default_factory=list)
    # `role="assistant"` only: the turn's parts in emission order, which `content`/`tools`
    # flatten away, so a reload keeps the think -> call -> text sequence. Adjacent passages
    # of one kind merge, as live deltas do.
    segments: list[Segment] = field(default_factory=list)


SegmentKind = Literal["thinking", "text", "tool"]


@dataclass
class Segment:
    """One step of an assistant turn in emission order: a reasoning or answer passage, or a
    tool call referring to its ``ToolView`` by id."""

    kind: SegmentKind
    text: str = ""
    tool_call_id: str | None = None


def _append_passage(view: MessageView, kind: SegmentKind, text: str) -> None:
    """Add a passage to the turn's segments, extending the trailing one of the same kind.

    An empty passage records nothing: the live translator emits no delta for one, and
    keeping it would split a passage the stream drew as one."""
    if not text:
        return
    last = view.segments[-1] if view.segments else None
    if last is not None and last.kind == kind:
        last.text += text
    else:
        view.segments.append(Segment(kind=kind, text=text))


#: Classes the readout measures but the footprint excludes wholesale; whether reasoning is
#: re-sent depends on the template, so :func:`_tag_mode_reasoning_chars` adds back the half
#: that is. The readout's breakdown still wants the whole figure.
_UNSENT_CLASSES = frozenset({"reasoning"})

#: The ``ThinkingPart.id`` for thinking the library read out of the response's ``content``
#: rather than a provider field. See :func:`_tag_mode_reasoning_chars`.
_TAG_MODE_ID = "content"


def estimate_tokens(messages: Sequence[ModelMessage]) -> int:
    """A coarse token estimate for what ``messages`` cost **on the wire**.

    The fallback for endpoints that report no usage: local servers commonly return
    ``input_tokens=0``, which ``services.conversations.context_footprint`` treats as
    unmeasured, so compaction would otherwise never fire there. Lives here because a cold
    read of a compaction divider must report the figure the live event did.

    Not counted: **the standing brief**, which ``agent/overhead.py`` measures and
    :func:`estimate_footprint` adds back once; and **binary parts**, since a base64 image
    measured by length would read as hundreds of thousands of phantom tokens (the
    under-count is the safe direction, behind the run's context-overflow stop).

    **Reasoning is counted in half.** In tag mode a ``ThinkingPart`` is re-sent as ordinary
    assistant text that no template can remove; the field-mode half is stripped by Qwen,
    DeepSeek and GLM templates. Counting both would inflate the estimate, and
    :func:`agent.summarize.should_compact` takes the larger of the provider's reported
    size and this one, so it would override an accurate figure and fold those threads early.

    Prose and structure convert at their own rates (``core.text``), matching the context
    readout's split."""
    sent = sum(
        chars_to_tokens(chars.prose, chars.structured)
        for name, chars in message_class_chars(messages).items()
        if name not in _UNSENT_CLASSES
    )
    return int(sent + chars_to_tokens(_tag_mode_reasoning_chars(messages), 0))


def _tag_mode_reasoning_chars(messages: Sequence[ModelMessage]) -> int:
    """Characters of thinking that will be re-sent as assistant text, whatever the template.

    The library decides per part. Thinking read from a **provider field** goes back into
    that field (``reasoning_content`` and kin), which Qwen, DeepSeek and GLM templates drop,
    so it costs nothing. Thinking read from the response's ``content`` (``id == 'content'``
    or no id) is re-wrapped in tags and appended to the assistant **text**, which no
    template removes.

    A part whose id names a different provider's field also takes the tag path; this does
    not reproduce that case, since it needs the next request's model and arises only on
    histories moved between providers. The error undercounts, the safe direction.

    Counted as prose, never structured.
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

    A request is the conversation (:func:`estimate_tokens`) plus the standing brief and
    every tool schema, which alone can reach five figures.

    ``overhead`` is the thread's last recorded ``TurnOverhead``, converted at the prose rate
    for the brief and the JSON rate for schemas. Without one, ``fallback_overhead_tokens``
    stands in: a guess, not zero, since a limit guard must not assume a smaller request
    than is possible."""
    overhead_tokens = (
        fallback_overhead_tokens
        if overhead is None
        else int(chars_to_tokens(overhead.system, overhead.tools))
    )
    return estimate_tokens(messages) + overhead_tokens


@dataclass(frozen=True)
class ContentChars:
    """Message characters, split by how they tokenize.

    Against cl100k, prose runs about 4.8 characters per token and serialized JSON about
    4.1. Keeping them apart lets each measurement apply the right rate; the rates live in
    ``core.text``."""

    prose: int
    structured: int


#: What each kind of message part counts as, for the context readout's message split.
#:
#: Each class has its own operator lever: mostly ``tool_results`` means tools return too
#: much, ``conversation`` wants a compaction, ``reasoning`` a lower thinking budget.
#:
#: ``SystemPromptPart`` is absent because ``agent/overhead.py`` already counts the standing
#: brief; counting it twice made the parts sum past the whole.
_PART_CLASSES: tuple[tuple[type, str], ...] = (
    (ThinkingPart, "reasoning"),
    (ToolCallPart, "tool_calls"),
    (ToolReturnPart, "tool_results"),
    (UserPromptPart, "conversation"),
    (TextPart, "conversation"),
)

#: Report order: largest-typical first and stable across threads. The caller drops empty
#: classes.
MESSAGE_CLASSES = ("conversation", "tool_results", "tool_calls", "reasoning")


def _part_class(part: ModelRequestPart | ModelResponsePart) -> str | None:
    """Which class a part's characters belong to, or None to leave it uncounted."""
    if type(part).__name__ == "SystemPromptPart":
        return None
    for kind, name in _PART_CLASSES:
        if isinstance(part, kind):
            return name
    # Parts the library adds later still count toward the exhaustive total.
    return "conversation"


def _part_chars(part: ModelRequestPart | ModelResponsePart) -> tuple[int, int]:
    """One part as (prose, structured) characters.

    Classified by content shape, since that is what tokenizes differently: a string is
    prose, a dict is JSON by the time the model sees it. Binary content contributes nothing
    (see :func:`estimate_tokens`). A tool call's payload is ``args`` rather than
    ``content``, so it is read separately."""
    if isinstance(part, ToolCallPart):
        return 0, len(_content_text(part.args))
    content: object = getattr(part, "content", None)
    if isinstance(content, str):
        return len(content), 0
    items = as_list(content)
    if items is not None:
        strings = [item for item in items if isinstance(item, str)]
        if len(strings) == len(items):
            return sum(len(item) for item in strings), 0
    return 0, len(_content_text(content))


def message_class_chars(messages: Sequence[ModelMessage]) -> dict[str, ContentChars]:
    """Characters across ``messages``, split by class and by how each tokenizes.

    Keyed by :data:`MESSAGE_CLASSES`; a class with no characters is omitted."""
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

    Summed from :func:`message_class_chars` so the total and the per-class rows agree."""
    by_class = message_class_chars(messages).values()
    return ContentChars(
        prose=sum(part.prose for part in by_class),
        structured=sum(part.structured for part in by_class),
    )


def _content_text(content: object) -> str:
    """A part's content as the text a model would actually be sent.

    Structured content counts: a dict tool result reaches the model as serialized JSON,
    keys included, and scoring only `str` content under-reported tool-heavy threads by
    multiples. Anything unrecognised counts as nothing; in particular a `BinaryContent`
    (base64 screenshot) falls through to `""` instead of being measured by length."""
    if isinstance(content, str):
        return content
    if isinstance(content, bool):
        return str(content)
    if isinstance(content, int | float):
        return str(content)
    mapping = as_dict(content)
    if mapping is not None:
        # Keys are serialized alongside the values.
        return " ".join(f"{key} {_content_text(value)}" for key, value in mapping.items())
    items = as_list(content)
    if items is not None:
        return " ".join(_content_text(item) for item in items)
    return ""


def flatten_content(content: object) -> str:
    """Flatten a message part's content (str or multimodal sequence) to text.

    Non-text parts collapse to one ``[attachment]`` marker so an image-only turn does not
    render as an empty bubble."""
    if isinstance(content, str):
        return content
    items = as_list(content)
    if items is not None:
        text = " ".join(part for part in items if isinstance(part, str)).strip()
        if text:
            return text
        return "[attachment]" if items else ""
    return ""


#: The most an image may weigh, in source bytes, before the work log declines to carry it.
#: The browser harness allows 5 MB of PNG (~6.7 MB of base64) on the stream frame and again
#: on every read of the conversation; the call still reports in words.
MAX_TOOL_IMAGE_BYTES = 2_000_000


def tool_images(content: object) -> list[ToolImageView]:
    """The images in a tool's returned content, in order.

    Shared with the live translator. Only images: a work-log card has no use for a returned
    PDF or audio clip. Oversized ones are dropped rather than re-encoded."""
    listed = as_list(content)
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
    returns: list[ToolReturnPart],
    by_call: dict[str, ToolView],
) -> None:
    """Hang a tool-return request's image payloads on the calls that produced them.

    Pydantic AI appends one user part per image-bearing call, in call order, after all the
    tool parts, with no call id, so the pairing is positional. That is exact for one
    image-bearing call per step (a screenshot); with two such calls and one between them,
    an image could land on the neighbouring card. The live path is always exact, since
    ``FunctionToolResultEvent`` carries the call id."""
    for part, tool_return in zip(user_parts, returns, strict=False):
        images = tool_images(part.content)
        if not images:
            continue
        tool = by_call.get(tool_return.tool_call_id)
        if tool is not None:
            tool.images.extend(images)


def _attach_answers(tool: ToolView) -> None:
    """Put a settled ``ask_user`` call's answers back on its row, as structure.

    Live, answers arrive on ``question.answered``; cold, the only record is the prose in the
    call's result, parsed here because this side writes the format. Questions come from the
    call's arguments and answers from its result, so an unanswered or unreadable exchange
    leaves the row unchanged."""
    if tool.name != ASK_USER_TOOL or not isinstance(tool.result, str):
        return
    tool.answers = parse_answer(questions_of(tool.args), tool.result)


def _compaction_reason(message: object) -> str | None:
    """Why this checkpoint folded, from its ``ModelRequest.metadata``, or ``None``.

    ``None`` covers an unrecorded or unrecognised value; a wrong reason would mislead the
    operator about who triggered the fold."""
    raw: object = getattr(message, "metadata", None)
    metadata = as_dict(raw)
    if metadata is None:
        return None
    reason = metadata.get(COMPACTION_REASON_KEY)
    return reason if isinstance(reason, str) and reason else None


def compaction_restored(message: object) -> list[str]:
    """What this checkpoint's fold re-read, from its metadata; empty when unrecorded."""
    raw: object = getattr(message, "metadata", None)
    metadata = as_dict(raw)
    items = as_list(metadata.get(COMPACTION_RESTORED_KEY)) if metadata is not None else None
    return [item for item in items if isinstance(item, str) and item] if items else []


def strip_chassis_markers(text: str) -> str:
    """The operator's text with the chassis's attachment and `@` blocks taken back off.

    Both blocks persist on purpose (see ``FILE_REFS_MARKER_OPEN``); the files show as chips
    from the structured fields. Both close the prompt, so everything from the first exact
    sentinel on is chassis text.
    """
    found = [text.find(marker) for marker in (ATTACHMENTS_MARKER_OPEN, FILE_REFS_MARKER_OPEN)]
    cut = min((at for at in found if at >= 0), default=-1)
    return text[:cut].rstrip() if cut >= 0 else text


def stamped_file_refs(message: object) -> list[str]:
    """The `@` paths stamped on this request, or an empty list.

    Metadata is a free-form dict, so anything unrecognised reads as none rather than
    crashing the projection every transcript load goes through.
    """
    raw: object = getattr(message, "metadata", None)
    metadata = as_dict(raw)
    if metadata is None:
        return []
    refs = metadata.get(FILE_REFS_KEY)
    items = as_list(refs)
    if items is None or isinstance(refs, tuple):
        return []
    return [ref for ref in items if isinstance(ref, str) and ref]


def stamped_command(message: object) -> Invocation | None:
    """The slash command this request was sent with, or ``None``.

    It feeds a resolver on the regenerate path, so a malformed stamp must read as no command
    rather than as a name of the wrong type.
    """
    raw: object = getattr(message, "metadata", None)
    metadata = as_dict(raw)
    if metadata is None:
        return None
    stamp = as_dict(metadata.get(COMMAND_KEY))
    if stamp is None:
        return None
    name = stamp.get("name")
    argument = stamp.get("argument", "")
    if not isinstance(name, str) or not name:
        return None
    return Invocation(name=name, argument=argument if isinstance(argument, str) else "")


def _summary_part(message: ModelMessage) -> UserPromptPart | None:
    """A checkpoint's summary part, the one the divider renders.

    Found by type: a leading ``ToolAvailabilityDeltaPart`` carries revealed tool groups
    forward, so the summary is not reliably ``parts[0]``."""
    return next((part for part in message.parts if isinstance(part, UserPromptPart)), None)


def project_tree(
    nodes: Sequence[tuple[str, ModelMessage]], *, compacted_ids: frozenset[str] = _NO_IDS
) -> list[MessageView]:
    """Fold an active-path ``(node_id, ModelMessage)`` sequence into ordered
    user/assistant view turns.

    ``compacted_ids`` names the compaction checkpoints among ``nodes``; each becomes a
    ``role="compaction"`` divider carrying the summary instead of a user turn. The caller
    supplies the nodes in render order.

    The divider's cost (messages folded, coarse before/after tokens) is derived here: a
    checkpoint sits right after the last node it covers, so the messages since the previous
    checkpoint (included) are the set the fold replaced, matching the live
    ``conversation.compacted`` event. The reason is read from the checkpoint's metadata,
    since it is not derivable from the messages.

    One turn is one view. A user turn is a request carrying a ``UserPromptPart``. An
    assistant turn is everything after it until the next user turn (responses plus
    interleaved tool-return requests), merged into one view whose ``segments`` keep
    emission order, matching the live stream's single bubble.

    A view's ``id`` is the node defining the turn's branch point, whose siblings are the
    turn's versions; the store fills version index/count since it holds the tree.

    A later request's ``ToolReturnPart`` mutates the shared ``ToolView``
    already attached to the issuing turn.
    """
    views: list[MessageView] = []
    by_call: dict[str, ToolView] = {}
    # The turn each call was issued on, so a script's rebuilt rows land on the script's turn.
    owner: dict[str, MessageView] = {}
    assistant: MessageView | None = None  # the open assistant turn, if any
    # Everything since the previous checkpoint, itself included, as `compaction_plan`
    # collects the set a fold summarizes.
    since_checkpoint: list[ModelMessage] = []
    for node_id, message in nodes:
        if node_id in compacted_ids:
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
                    restored=compaction_restored(message),
                )
            )
            since_checkpoint = [message]
            continue
        since_checkpoint.append(message)
        if isinstance(message, ModelRequest):
            user_parts = [p for p in message.parts if isinstance(p, UserPromptPart)]
            returns = [p for p in message.parts if isinstance(p, ToolReturnPart)]
            # A request with tool returns is the agent's own round-trip even when it also
            # has a user part (a screenshot's pixels are appended as user content). Reading
            # it as an operator turn would close the assistant turn mid-work and skip the
            # results to stitch.
            if user_parts and not returns:
                assistant = None
                part = user_parts[0]
                text = flatten_content(part.content)
                notice = notice_body(text)
                if notice is not None:
                    views.append(
                        MessageView(
                            role="notice",
                            notice=notice[0],
                            content=strip_envelope_note(notice[1]),
                            timestamp=part.timestamp,
                            id=node_id,
                        )
                    )
                    continue
                # A sub-agent's report arrives as a user-shaped message and would claim the
                # operator typed it; recognised by the envelope `agent/injected.py` writes.
                # Prose half only: a trailing machine-readable findings block is the
                # panel's copy of the same report.
                envelope = report_body(text)
                report = None if envelope is None else report_prose(envelope)
                # The launching agent's direction inside a sub-agent's thread: only the
                # envelope is stripped, and it stays a `user` turn since nobody else can
                # type in that thread.
                direction = None if report is not None else direction_body(text)
                sent = None if report is not None else message_body(text)
                if sent is not None:
                    views.append(
                        MessageView(
                            role="subagent",
                            author=sent[0],
                            content=strip_envelope_note(sent[1]),
                            timestamp=part.timestamp,
                            id=node_id,
                        )
                    )
                    continue
                body = report if report is not None else direction
                content = strip_chassis_markers(text if body is None else strip_envelope_note(body))
                parts: list[ContentPart] | None = None
                if report is None:
                    split = split_envelopes(content, vouched_only=True)
                    if any(piece.kind != "text" for piece in split):
                        content = "\n\n".join(p.text for p in split if p.kind == "text")
                        parts = [p for p in split if p.kind != "text"]
                views.append(
                    MessageView(
                        role="subagent" if report is not None else "user",
                        reporter=None if report is None else report_sender(report),
                        content=content,
                        parts=parts,
                        timestamp=part.timestamp,
                        id=node_id,
                        file_refs=stamped_file_refs(message),
                    )
                )
                continue
            # A tool-return request: stitch results into the open assistant turn.
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.outcome in ERROR_OUTCOMES:
                    tool = by_call.get(part.tool_call_id)
                    if tool is not None:
                        error = part.model_response_str(wrap_if_error=False)
                        tool.status = "error"
                        tool.error = error
                        tool.platform = is_platform_failure(error)
                elif isinstance(part, ToolReturnPart):
                    tool = by_call.get(part.tool_call_id)
                    if tool is not None:
                        tool.status = "ok"
                        tool.result = jsonable(part.content)
                        tool.result_parts = result_parts(tool.result)
                        _attach_answers(tool)
                        # A script's calls follow it on the turn, each naming it as parent.
                        owner[part.tool_call_id].tools.extend(_script_calls(part))
            _attach_tool_images(user_parts, returns, by_call)
        else:
            if assistant is None:
                assistant = MessageView(role="assistant", timestamp=message.timestamp, id=node_id)
                views.append(assistant)
            # The last response carrying a name wrote the final answer.
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
