"""What a model request's cacheable prefix looked like, and where the last one stopped
matching it.

Every inference engine worth caching against — llama.cpp's per-slot longest-common-prefix,
vLLM's chained block hashes, OpenAI's single implicit breakpoint — reuses a request's
leading tokens only while they are **byte-identical** to what it saw before, and re-reads
everything from the first difference onward. So the question "why did that turn take
fourteen seconds to start" almost always has a structural answer: something near the front
of the request moved, and the whole history behind it was re-read at full price.

That answer is not visible from any figure we already collect. ``ttft_ms`` says a request
was slow; ``cache_read_tokens`` is ``None`` on every local server; the composition readout
says what the brief *weighed*, not whether it was the **same** brief. This module is the
missing half: a fingerprint of each request's prefix, and a verdict on how far the previous
one still matched.

Sits in ``runs`` rather than beside the code that computes it (``agent.prefix_watch``) for
the reason :mod:`runs.overhead` does: the :class:`~runs.run.Run` carries one across a turn
and ``runs`` cannot import from ``agent``. The comparison is the agent layer's; the value is
the run's.

**Digests, never copies.** A prefix fingerprint outlives the request it describes — the
ledger below holds one per conversation for the life of the process, and a turn parked for
approval holds its own for as long as the operator takes to answer. Retaining the text would
pin a copy of every brief and every replayed history for that whole time, a repo brief alone
being budgeted at 16 KB. Sixteen bytes per block answers the only question a fingerprint is
asked: is this the same as last time. Tool *names* are the one exception and are kept in
full — they are a fixed vocabulary this codebase authored, they are what makes "the model
revealed the browser group" legible instead of "the tool array changed", and they are not
anybody's content.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

#: How the head of a request moved since the last one. The head is the brief and the tool
#: array — everything an engine sees *before* the first message — and on llama.cpp, vLLM and
#: the OpenAI API alike a change anywhere in it invalidates the entire history behind it,
#: because each has a single monotone cache boundary rather than the additive breakpoints
#: Anthropic exposes. That is why this is one verdict over the whole head rather than a
#: per-block cost: the blocks do not pay separately.
HeadChange = Literal["first", "none", "instructions", "tools", "both"]

#: The *shape* of the first mismatch in the message list, which is what says whether a
#: divergence was expected. Named rather than numeric because the index alone cannot
#: distinguish the per-turn-boundary cost this codebase has accepted from a bug:
#:
#: - ``first`` — nothing to compare against; this is the conversation's first measured request.
#: - ``append`` — every message previously sent is still there, byte for byte, with more after
#:   it. The ideal: only the new part is read. Every step *within* a turn should be this.
#: - ``tail`` — exactly the previously-last message changed, and the history continued past
#:   it. The per-turn-boundary divergence the tail-context seam accepts by design: turn N's
#:   request ends with the volatile context appended to the operator's prompt, and turn N+1
#:   replays that prompt without it. Bounded at one turn's assistant work, and it does not
#:   grow with thread length.
#: - ``clean_drop`` — the current history is a strict prefix of the previous one. A rewind, a
#:   regenerate or an edit. Costs nothing: a shorter matching prefix is still a matching prefix.
#: - ``fold`` — the divergence is at or beside the head of the history and the history shrank.
#:   Compaction. A full re-read, and the price of the room it bought.
#: - ``rewrite`` — the divergence is strictly inside the history, with the history continuing
#:   past it. Nothing in this codebase should produce one; a history processor that edited an
#:   earlier turn would. (The design note this was drafted against called this case
#:   ``dangling``, after the dangling tool round trip that motivated it — renamed because the
#:   shape is what is observable here and a dangling call is only one way to reach it.)
#: - ``unknown`` — a shape none of the above describes. Kept so the classifier can be honest
#:   rather than forcing every reading into a bucket.
DivergenceKind = Literal["first", "append", "tail", "clean_drop", "fold", "rewrite", "unknown"]

#: How many conversations' fingerprints the ledger keeps. One entry is a few hundred bytes
#: (a digest per replayed message), so this is a bound on nothing that matters; what it
#: actually prevents is a process that has served ten thousand threads holding a fingerprint
#: for each. Generous enough that the threads an operator is switching between all stay warm,
#: which is the only case the cross-turn reading needs.
LEDGER_LIMIT = 64


@dataclass(frozen=True)
class PrefixDigest:
    """A fingerprint of one model request's cacheable prefix, in order.

    Three layers, because they invalidate for different reasons and an engine reads them in
    this order: the standing brief, the tool array, then the replayed messages.

    ``blocks`` is the brief itemised by the name each contributor was registered under — the
    same names the composition readout groups by, from the same ``InstructionPart.id.name``.
    ``instructions`` is the digest of the *joined* brief rather than of the blocks, so it
    also moves when the separators or the unnamed literal do; the itemisation is what says
    **which** contributor moved, which is the whole diagnostic value.
    """

    #: Digest of the whole joined brief, exactly as the library assembled it.
    instructions: str
    #: ``(contributor, digest)`` per named block, in brief order. Order is part of the
    #: reading: two requests whose blocks carry the same digests in a different order have
    #: the same set and a different prefix.
    blocks: tuple[tuple[str, str], ...] = ()
    #: Digest of the tool definitions — name, description and schema — in the order offered.
    tools: str = ""
    #: The tool names offered, in order. Kept in full (see the module docstring).
    tool_names: tuple[str, ...] = ()
    #: One digest per replayed message, in order.
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrefixVerdict:
    """How much of the previous request's prefix this one could have reused.

    Read two ways. ``head_changed`` is the expensive verdict: a head change re-reads
    *everything*, so the message figures below it describe a cache that was already thrown
    away. When the head held, ``divergence_kind`` and ``reused_messages`` say how far into
    the history the match ran.
    """

    head_changed: HeadChange
    divergence_kind: DivergenceKind
    #: Index of the first message that did not match, or ``None`` when every previously-sent
    #: message did. Not the same as ``reused_messages`` when the head changed: the index is a
    #: structural fact about the two message lists, reusability is what the engine gets.
    divergence_index: int | None = None
    #: Messages an engine could actually have reused — zero whenever the head changed,
    #: because there is no reading under which a request with a different brief reuses the
    #: history that followed the old one.
    reused_messages: int = 0
    #: Messages this request carries.
    total_messages: int = 0
    #: The named brief blocks whose text moved, in brief order. A block that appeared or
    #: disappeared is listed too — the head is a byte sequence, and a block arriving is the
    #: same event as a block changing.
    changed_blocks: tuple[str, ...] = ()
    #: Tools this request offers that the last one did not, and the reverse. A group reveal
    #: shows up here as a dozen-odd additions, which is what makes it identifiable at all.
    tools_added: tuple[str, ...] = ()
    tools_removed: tuple[str, ...] = ()

    @property
    def reused_everything(self) -> bool:
        """Whether the previous request's whole prefix carried over — the only reading
        under which a turn pays nothing for what came before it."""
        return self.head_changed == "none" and self.divergence_kind in ("append", "clean_drop")

    def summary(self) -> str:
        """One line for the diagnostic log, ordered most-expensive fact first.

        Terse on purpose: this is read by eye against a ``ttft_ms`` spike, in a log where it
        sits beside half a dozen other figures. Names rather than counts wherever a name
        fits, because ``head=instructions(skill_catalog)`` identifies a cause and
        ``head=instructions`` only identifies a category."""
        head = self.head_changed
        if self.changed_blocks:
            head = f"{head}({','.join(self.changed_blocks)})"
        parts = [f"head={head}", f"history={self.divergence_kind}"]
        if self.divergence_index is not None:
            parts.append(f"at={self.divergence_index}")
        parts.append(f"reused={self.reused_messages}/{self.total_messages}")
        if self.tools_added:
            parts.append(f"tools+={len(self.tools_added)}")
        if self.tools_removed:
            parts.append(f"tools-={len(self.tools_removed)}")
        return " ".join(parts)


#: The verdict for a request with nothing to compare against. A constant rather than a
#: construction at each site, so "we have never measured this conversation" reads the same
#: everywhere it is produced.
FIRST_REQUEST = PrefixVerdict(head_changed="first", divergence_kind="first")


@dataclass
class PrefixLedger:
    """The last prefix fingerprint each conversation sent, across turns.

    A turn's own steps compare against each other inside the capability that measures them —
    it lives as long as the agent does. **What no per-turn object can hold is the turn
    boundary**, and that is where the expensive invalidations happen: a skill published, a
    ``CLAUDE.md`` the agent edited in its own worktree, an operator tool toggle, the midnight
    roll. Turn N+1's first request is the one that pays for those, and it needs turn N's
    fingerprint to know.

    **In-process, bounded, and lost on restart** — deliberately, rather than a column beside
    the stored context overhead. This is an instrument: it answers "which of these three
    causes fired on that slow turn" while an operator is watching, and it answers it from the
    first turn after a restart onward. A migration to carry a diagnostic's fingerprint across
    a process boundary would buy one extra reading per restart at the price of a schema
    change and a stored blob nothing else reads. The per-run event replay buffer is already
    in-memory for the same single-host reason.

    Registered as an agent capability handle so the code that measures a request resolves it
    from the run's bag like everything else, and degrades to within-turn comparison when it
    is not wired — which is what a stateless eval or a test that builds its own agent gets.
    """

    limit: int = LEDGER_LIMIT
    _seen: OrderedDict[str, PrefixDigest] = field(default_factory=OrderedDict)

    def recall(self, conversation_id: str | None) -> PrefixDigest | None:
        """The last fingerprint recorded for this conversation, if it is still held."""
        if not conversation_id:
            return None
        digest = self._seen.get(conversation_id)
        if digest is not None:
            self._seen.move_to_end(conversation_id)
        return digest

    def remember(self, conversation_id: str | None, digest: PrefixDigest) -> None:
        """Record this request as the one the next turn will be compared against.

        Called on **every** request, not once per turn, so what a turn leaves behind is its
        last request — which is exactly what an engine's cache is holding when the next turn
        arrives. A turn that ends parked for approval leaves its pre-park request, which is
        also right: that is what was last sent.
        """
        if not conversation_id:
            return
        self._seen[conversation_id] = digest
        self._seen.move_to_end(conversation_id)
        while len(self._seen) > self.limit:
            self._seen.popitem(last=False)
