"""Fingerprinting each request's cacheable prefix, and saying where the last one stopped
matching it.

The instrument behind the caching work, and the reason it comes before any fix. A slow local
turn has three candidate causes and they are indistinguishable from the outside:

1. something in the **head** moved — a dormant group revealed, the permission level narrowed
   mid-turn, a skill published, the project's own brief edited by the agent in its own
   worktree — and the whole history behind it was re-read;
2. a **batch of reviews** ran with no shared prefix between them;
3. a **background call** on the same server evicted the chat's cached state.

From a ``ttft_ms`` spike alone all three look identical. This says which: cause 1 announces
itself as a head change naming the block that moved, and its absence is what leaves the other
two standing. Deterministic, needs no cooperation from the server, and costs one pass of
BLAKE2b over a request we are already assembling.

**Where the reading comes from.** ``before_model_request`` is handed the exact
``ModelRequestParameters`` about to go out — the instruction parts with the name each was
contributed under, the tool definitions in offered order, and the outgoing message list. Same
public seam :mod:`agent.overhead` measures from, and a separate capability for the same reason
that one is separate from :mod:`agent.injections`: it answers a different question and would
change for a different reason. Three passes over the same parts is three hashes of a few tens
of kilobytes, against a request the model is about to spend seconds on.

**Digests, never copies** — see :mod:`runs.prefix`, which owns the value types and the
cross-turn ledger.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import cast, override

from pydantic_ai import InstructionPart, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from runs import FIRST_REQUEST, PrefixDigest, PrefixLedger, PrefixVerdict
from runs.prefix import DivergenceKind, HeadChange
from tools import RunDeps

from .assembled import declared_function_tools
from .emit import PrefixWatched, is_side_run

#: Width of every fingerprint here, in bytes. This decides whether a *diagnostic* reports a
#: change, never whether anything is trusted or authorized, and 128 bits will not collide
#: before the heat death — the same reasoning, and the same width, as the injection
#: announcer's dedup key.
_DIGEST_BYTES = 16

#: Field separators. Distinct byte values so that a part whose content is ``"a"`` with no tool
#: name cannot hash the same as one with no content and the tool name ``"a"`` — a fingerprint
#: that can be confused by rearranging fields is a fingerprint that reports "unchanged" on a
#: request that changed.
_FIELD = b"\x1f"
_ITEM = b"\x1e"
_PAIR = b"\x1d"

#: The attributes of a message part that decide what the model actually reads. Walked in this
#: fixed order on every part, skipping the ones a given part type does not have, so the same
#: content always hashes the same way regardless of a dataclass's own field order.
#:
#: Deliberately **not** every attribute: a part also carries a ``timestamp``, and a freshly
#: constructed part stamps it with the wall clock. Hashing that would report a divergence on
#: every request for a reason no inference engine can see, which is worse than no reading —
#: the instrument would blame the head for its own noise.
_PAYLOAD_FIELDS = ("tool_name", "tool_call_id", "id", "content", "args", "signature")


def _hasher() -> hashlib.blake2b:
    return hashlib.blake2b(digest_size=_DIGEST_BYTES)


def _absorb(digest: hashlib.blake2b, value: object) -> None:
    """Fold one value into a hash, structurally, without ever building a copy of it.

    Recursive rather than ``json.dumps`` for one load-bearing reason: a user prompt's content
    is a list that can hold ``BinaryContent``, and serialising that through ``default=str``
    would render a two-megabyte image as a six-megabyte repr — on every request, for a
    diagnostic. Bytes go into the hash as bytes.

    Mappings are absorbed in sorted key order so a tool's arguments fingerprint the same
    whichever order the model happened to emit them in. That is the right call *here* — this
    measures whether the prefix changed, and a dict is not what goes on the wire; the
    provider's own serialisation of it is, and reordering that is the provider's business.
    """
    match value:
        case str():
            digest.update(value.encode("utf-8", "replace"))
        case bytes() | bytearray():
            digest.update(value)
        case bool() | int() | float() | None:
            digest.update(repr(value).encode())
        case list() | tuple():
            for item in cast(Sequence[object], value):
                digest.update(_ITEM)
                _absorb(digest, item)
        case dict():
            mapping = cast(dict[object, object], value)
            for key in sorted(mapping, key=str):
                digest.update(_PAIR)
                _absorb(digest, str(key))
                _absorb(digest, mapping[key])
        case _:
            # A library object — ``BinaryContent``, a nested model, an enum. Its own field
            # mapping where it has one (both pydantic models and dataclasses do), whose byte
            # payloads then land in the ``bytes`` branch above; its repr otherwise.
            fields: object = getattr(value, "__dict__", None)
            if isinstance(fields, dict):
                _absorb(digest, cast(dict[object, object], fields))
            else:
                _absorb(digest, repr(value))


def _message_digest(message: ModelMessage) -> str:
    """One message's fingerprint: its type, then each part's type and payload, in order."""
    digest = _hasher()
    digest.update(type(message).__name__.encode())
    for part in message.parts:
        digest.update(_FIELD)
        digest.update(type(part).__name__.encode())
        for name in _PAYLOAD_FIELDS:
            value: object = getattr(part, name, None)
            if value is None:
                continue
            digest.update(_FIELD)
            _absorb(digest, name)
            _absorb(digest, value)
    return digest.hexdigest()


def _tools_digest(function_tools: list[ToolDefinition]) -> str:
    """The tool array's fingerprint — name, description and schema, **in offered order**.

    Order is the point, and it is measured rather than assumed: on a local 27B at ~5.9k
    tokens, reordering four otherwise-identical tools cost a full re-prefill (10.19s) while
    returning to the original order was a cache hit (0.39s). A set-based fingerprint would
    call that unchanged and send the search for a cause somewhere else entirely.
    """
    digest = _hasher()
    for tool_def in function_tools:
        digest.update(_ITEM)
        _absorb(digest, tool_def.name)
        digest.update(_FIELD)
        _absorb(digest, tool_def.description)
        digest.update(_FIELD)
        _absorb(digest, tool_def.parameters_json_schema)
    return digest.hexdigest()


def digest_request(
    instruction_parts: list[InstructionPart] | None,
    function_tools: list[ToolDefinition],
    messages: list[ModelMessage],
) -> PrefixDigest:
    """Fingerprint one assembled request, layer by layer.

    ``instructions`` is taken from the **joined** brief rather than from the blocks, because
    that is the string the provider sends: it includes the separators the library puts
    between parts and our own unnamed literal, and a reading assembled from the named blocks
    alone would miss a change in either.
    """
    parts = instruction_parts or []
    joined = InstructionPart.join(parts) or ""
    brief = _hasher()
    _absorb(brief, joined)
    blocks = tuple(
        (part.id.name, _block_digest(part.content))
        for part in parts
        if part.id is not None and part.id.name
    )
    return PrefixDigest(
        instructions=brief.hexdigest(),
        blocks=blocks,
        tools=_tools_digest(function_tools),
        tool_names=tuple(tool_def.name for tool_def in function_tools),
        messages=tuple(_message_digest(message) for message in messages),
    )


def _block_digest(content: str) -> str:
    digest = _hasher()
    _absorb(digest, content)
    return digest.hexdigest()


def _changed_blocks(
    previous: tuple[tuple[str, str], ...], current: tuple[tuple[str, str], ...]
) -> tuple[str, ...]:
    """The named blocks whose text moved, in the order the current brief renders them, with
    any block that vanished appended.

    A block that *appeared* counts as changed, and so does one that disappeared: the head is
    a byte sequence, not a set of features, and a block arriving is the same event to a cache
    as a block being rewritten. Naming the vanished ones is what keeps a disappearance from
    reading as "nothing changed but the joined digest" — which is the shape that would send
    the next reader looking at the separators.
    """
    was = dict(previous)
    now = dict(current)
    changed = [name for name, digest in current if was.get(name) != digest]
    changed.extend(name for name, _ in previous if name not in now)
    return tuple(changed)


def _head_change(previous: PrefixDigest, current: PrefixDigest) -> HeadChange:
    instructions = previous.instructions != current.instructions
    tools = previous.tools != current.tools
    if instructions and tools:
        return "both"
    if instructions:
        return "instructions"
    if tools:
        return "tools"
    return "none"


def _common(previous: tuple[str, ...], current: tuple[str, ...]) -> int:
    matched = 0
    for before, after in zip(previous, current, strict=False):
        if before != after:
            break
        matched += 1
    return matched


def _divergence(previous: tuple[str, ...], current: tuple[str, ...]) -> tuple[DivergenceKind, int]:
    """Classify the first message mismatch. See :data:`runs.prefix.DivergenceKind` for what
    each name means and which of them this codebase accepts by design.

    ``tail`` is tested before ``fold`` because ``matched == len(previous) - 1`` is the exact
    signature of the turn-boundary seam, and a fold cannot produce it: a fold shortens the
    history by replacing its front, so its divergence sits near index 0, never one short of
    the end.
    """
    matched = _common(previous, current)
    if matched == len(previous):
        return "append", matched
    if matched == len(current):
        # Everything this request carries matched; the previous one simply carried more.
        return "clean_drop", matched
    if matched == len(previous) - 1:
        return "tail", matched
    if matched <= 1 and len(current) < len(previous):
        return "fold", matched
    return "rewrite", matched


def compare_prefix(previous: PrefixDigest | None, current: PrefixDigest) -> PrefixVerdict:
    """How much of ``previous``'s prefix ``current`` could have reused.

    **A head change zeroes the reusable count** rather than reporting the message match
    anyway. The message figures are still computed and still reported as
    ``divergence_kind``/``divergence_index`` — they are structural facts about the two lists,
    and worth having — but there is no reading under which an engine reuses a history that
    sits behind a brief or a tool array that moved. Reporting "reused 40 messages" on a turn
    that re-read every token would be the one failure this instrument exists to prevent.
    """
    if previous is None:
        return replace(FIRST_REQUEST, total_messages=len(current.messages))
    head = _head_change(previous, current)
    kind, matched = _divergence(previous.messages, current.messages)
    had = set(previous.tool_names)
    has = set(current.tool_names)
    return PrefixVerdict(
        head_changed=head,
        divergence_kind=kind,
        divergence_index=None if kind in ("append", "clean_drop") else matched,
        reused_messages=0 if head != "none" else matched,
        total_messages=len(current.messages),
        changed_blocks=_changed_blocks(previous.blocks, current.blocks) if head != "none" else (),
        tools_added=tuple(name for name in current.tool_names if name not in had),
        tools_removed=tuple(name for name in previous.tool_names if name not in has),
    )


@dataclass
class WatchPrefix(AbstractCapability[RunDeps]):
    """Fingerprint each request's prefix as it goes out, and report what it cost.

    Two comparisons from one fingerprint, because the two expensive cases live on different
    timescales. **Within a turn** the previous request is the one this capability itself
    measured — it lives as long as the agent, so a park that stashes the agent keeps the
    reading intact across an approval. **Across turns** there is no per-turn object to hold
    anything, and the turn boundary is where the costly invalidations happen, so the first
    request of a turn compares against what :class:`runs.prefix.PrefixLedger` recorded for
    this conversation. Without the ledger this still reports every within-turn event (a
    reveal, a level move) and reports the first request of each turn as ``first``.

    Observes only: the request context is returned exactly as it arrived.
    """

    #: Named rather than left to the library's auto-minted handle, which differs per run. An
    #: id is how two instances of the same capability are recognised as one thing to merge,
    #: and how anything outside the run can name this one at all.
    id: str | None = "watch_prefix"

    #: The last request this capability measured, for the within-turn comparison.
    previous: PrefixDigest | None = field(default=None, repr=False)

    @override
    async def before_model_request(
        self, ctx: RunContext[RunDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        # A side run (the compaction summary) is not one of the turn's requests: its verdict
        # would reach nobody, and remembering it would make the turn's next request report
        # against a request the operator never saw sent.
        if is_side_run(ctx):
            return request_context
        params = request_context.model_request_parameters
        # The array the provider will render, which is neither of the two lists the request
        # carries — see `agent/assembled.py`. Output tools are appended because they are in
        # the same `tools` collection on the wire, and a prefix is a byte sequence: they are
        # stable across every request of a turn, so they never move the verdict, and leaving
        # them out would make the fingerprint a fingerprint of something else.
        current = digest_request(
            params.instruction_parts,
            [*declared_function_tools(params), *params.output_tools],
            request_context.messages,
        )
        conversation_id = getattr(ctx.deps, "conversation_id", None)
        ledger = ctx.deps.caps.get_optional(PrefixLedger)
        previous = self.previous
        if previous is None and ledger is not None:
            previous = ledger.recall(conversation_id)
        verdict = compare_prefix(previous, current)
        self.previous = current
        if ledger is not None:
            ledger.remember(conversation_id, current)
        _ = await ctx.emit(PrefixWatched(verdict=verdict))
        return request_context


def watch_prefix_enabled() -> bool:
    """Whether to register the watch at all.

    On by default. It is cheap, it is the only thing in this codebase that can attribute a
    prefill spike to a cause, and a diagnostic nobody has switched on is a diagnostic that is
    not there the one time it was needed. The escape hatch is an environment variable rather
    than a settings field because turning an instrument off is an operator's debugging act,
    not a property of their installation — and a settings field would have to be carried
    through the factory, which knows nothing about settings by design.
    """
    return os.environ.get("HEPHAESTUS_PREFIX_WATCH", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )
