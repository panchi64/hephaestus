"""The context-window readout (thresholds, fold point, composition) and injected-context event."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import Field, model_validator

from .base import Body

logger = logging.getLogger(__name__)


class ContextThresholds(Body):
    """Where a filling context window stops being unremarkable and starts being a
    problem — the two boundaries the gauge changes colour on.

    **Operator-tunable**, because the point at which the remaining room stops being
    enough is a property of how someone works rather than of the model: a thread of
    long tool results can spend the last quarter of a window in a single turn, while a
    short back-and-forth has a dozen turns left at the same fullness. A fixed boundary
    is therefore either early enough to be noise for one operator or late enough to be
    useless for the other.

    Fractions, not percentages — the same 0-1 quantity :attr:`ContextWindow.fraction`
    and auto-compaction's own threshold already carry, so nothing here has to agree on
    a second convention.

    ``warn`` strictly below ``alert`` is an invariant, not a preference: equal
    boundaries make the amber band unreachable, and inverted ones walk the gauge
    backwards through severity as it fills. Enforced here so that the one construction
    path is also the one check."""

    warn: float = Field(gt=0, lt=1)
    alert: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> ContextThresholds:
        if self.warn >= self.alert:
            raise ValueError("the warn threshold must be below the alert threshold")
        return self


#: The boundaries in force when the operator hasn't moved them. 75/90 leaves roughly a
#: turn or two of warning at typical turn sizes before the window is genuinely tight.
#:
#: **These do not relate to the fold, and the gauge says so rather than pretending they
#: do.** They were chosen when compaction fired at 0.95, so both sat below it and the ring
#: reddened while there was still something a fold could do. Compaction now fires at 0.80
#: (the trigger measures the turn *about to run*), which puts `alert` **above** the fold
#: point: on a thread with folding on, the window is emptied before it can ever reach 0.90,
#: so the red band is effectively unreachable. That is not a bug in either number — a
#: warning and a fold want different moments, and the operator can move them
#: independently, which is the whole reason they are two dials. It does mean the gauge has
#: to show where the fold is (:class:`FoldPoint`) instead of leaving the operator to infer
#: it from a band that never lights.
DEFAULT_CONTEXT_THRESHOLDS = ContextThresholds(warn=0.75, alert=0.9)


class FoldPoint(Body):
    """Where conversation compaction will fold this thread, and whether it is armed.

    Rides on :class:`ContextWindow` rather than being fetched separately, because it is
    only meaningful against the same window the fullness is measured in — and because it is
    **per-thread**: the operator's global threshold, with that conversation's on/off
    override folded in. A client reading the global setting would draw the wrong mark on a
    thread whose folding the operator switched off.

    Deliberately *not* part of the severity derivation. ``level`` stays a function of
    ``warn``/``alert`` alone: an alert is a notification and a fold is an act on the
    thread, the right moment for the two differs, and collapsing them into one number would
    mean every future adjustment to one silently retuned the other.

    ``active`` false still carries a ``fraction``: a thread with folding paused should show
    where the fold *would* fire, dimmed, rather than dropping the mark and leaving the
    operator with no sense of the room they are spending."""

    fraction: float = Field(gt=0, le=1)
    active: bool

    @classmethod
    def of(cls, fraction: float, *, active: bool) -> FoldPoint | None:
        """A fold mark for ``fraction``, or ``None`` when there is no sensible mark to draw.

        **Constructed through here rather than directly, because this is a readout and a
        readout must not be able to stop the thing it describes.** The bounds above are
        real — a fold at 0 would fire on an empty thread and one above 1 could never fire —
        but the threshold reaching them is not always a validated value: ``_float_or``
        bounds what the *settings store* holds and then falls back to
        ``Settings.auto_compact_threshold``, which is a plain unbounded float. An operator
        running with ``HEPHAESTUS_AUTO_COMPACT_THRESHOLD=0`` would otherwise have a gauge
        decoration raise inside the orchestrator — killing every turn — and 500 every
        conversation load, over a mark on a bar.

        So an out-of-range threshold draws no mark, which is the honest reading: there is
        no share of the window this thread meaningfully folds at. The misconfiguration is
        still wrong and still changes what ``should_compact`` does; it just stops being
        fatal on the way to the screen."""
        if not 0 < fraction <= 1:
            logger.warning("no fold mark: auto_compact_threshold %r is outside (0, 1]", fraction)
            return None
        return cls(fraction=fraction, active=active)


class ContextSegment(Body):
    """One line item inside a group — a tool category, a contributor to the standing
    brief, a class of message content.

    **Present only when it weighs something.** The segment list is not a fixed roster
    with zeros in it: a thread that has called no tools carries no `tool_results` row, a
    catalog with no MCP servers connected carries no `external` row, and both appear the
    moment they start costing the window. That is the difference between a readout the
    operator scans and a form they have to read — the rows that are there are the rows
    that matter.

    ``id`` is a slug, not a label: the tool category as the operator's own settings page
    names it, the instruction provider's slug, or the message class. The wording is the
    client's — a readout row is presentation, and the backend has no business choosing
    sentence case. ``count`` is the population behind the figure where one exists (tools
    in a category, `null` elsewhere), because "22k of schemas" and "22k of schemas across
    68 tools" lead to different decisions."""

    id: str
    group: Literal["brief", "tools", "messages"]
    tokens: int
    count: int | None = None


class ContextComposition(Body):
    """What the occupied part of the window is actually holding.

    Two resolutions of one measurement. The three totals are exhaustive by construction —
    they are scaled to sum to :attr:`ContextWindow.used` (see ``services.context_budget``)
    — so the operator can read them as a whole rather than wondering what the remainder
    is. ``segments`` itemises those same tokens without adding any: each segment belongs
    to exactly one group, and a group's segments sum to its total.

    The itemisation is what makes the readout answer the *next* question. "Tools are 40%
    of your window" is where the three-way split stops and where the operator's actual
    decision starts — which tools, and can they be switched off. Empty when a
    measurement could reach the totals but not the detail, so the coarse reading never
    depends on the fine one.

    Every figure is an **estimate anchored to the provider's total**: the split is ours,
    measured from what we assembled, because no provider reports one. Surfaces render
    these with a `~`."""

    system: int  # the standing brief: instructions + system prompt
    tools: int  # every tool name, description and JSON schema handed to the model
    messages: int  # the conversation itself
    segments: tuple[ContextSegment, ...] = ()


class ContextWindow(Body):
    """How full a model's context window is after a turn — the single owner of
    the fullness derivation and its severity thresholds. Built by
    :meth:`from_usage` and emitted both live (run metrics) and on load
    (conversation detail), so clients render one shape from either source."""

    used: int  # tokens occupying the window: last response's prompt + generation
    window: int  # the model's context window
    fraction: float  # used / window, clamped to 0–1
    level: Literal["nominal", "warn", "alert"]
    # What `used` is made of, when it could be measured. Null on a thread whose turns all
    # predate the measurement, and on a cold load of one — the split is captured while a
    # request is being assembled, and a reload has no request to look at.
    parts: ContextComposition | None = None
    # Where this thread's fold will fire, and whether it is armed. Null when the caller
    # had no compaction policy to hand — which is a different statement from "folding is
    # off": off is `active=False` with the fraction still on it. A client draws no mark
    # for null and a dimmed one for inactive.
    fold: FoldPoint | None = None

    @classmethod
    def from_used(
        cls,
        used: int | None,
        window: int | None,
        thresholds: ContextThresholds = DEFAULT_CONTEXT_THRESHOLDS,
        parts: ContextComposition | None = None,
        fold: FoldPoint | None = None,
    ) -> ContextWindow | None:
        """Derive the window state from the context footprint (``used``), or None
        when there's no ceiling to measure against or no footprint was reported.

        ``thresholds`` are the operator's severity boundaries; the default is what a
        caller with no settings store to consult gets. The *level* is resolved here and
        travels on the wire, so that the gauge, the overflow warning, and anything else
        keying off severity read one boundary rather than each re-deriving it — the
        client renders a level, it never decides one."""
        if not window or used is None:
            return None
        fraction = min(1.0, used / window)
        level = (
            "alert"
            if fraction >= thresholds.alert
            else "warn"
            if fraction >= thresholds.warn
            else "nominal"
        )
        return cls(used=used, window=window, fraction=fraction, level=level, parts=parts, fold=fold)


# --- Context -----------------------------------------------------------------
#: The most text one injection puts on the wire. The token figure is always the whole
#: block's, so a capped ``text`` costs the operator the tail of a long file and nothing
#: else — while an uncapped one would put a 60KB instruction file into the run's replay
#: buffer on every turn that reads it, to be re-sent in full to every reattaching client.
INJECTED_TEXT_LIMIT = 8_000

InjectionPlacement = Literal["instructions", "prompt", "summary"]


class ContextInjected(Body):
    """Something the chassis put in front of the model that nobody in the conversation
    wrote — a project's instruction files, the skill catalog, the plan reminder, the
    date.

    The gauge already says these cost the window; this says *when they arrived and what
    they said*. Those are different questions, and only the second one answers "why did
    the model act as if it had been told that" — which, on a thread where the operator
    reads every message and still cannot account for the model's behaviour, is the whole
    question. Emitted as the turn is assembled, so it lands in the work log ahead of the
    work it shaped.

    ``contributor`` is the same slug :class:`ContextSegment` uses for the standing
    brief's rows, and deliberately so: the popover's "Skill catalog · ~4k" and this
    event's row are the same block seen from two distances, and a client that named them
    differently would make the operator work out that they are one thing. The wording
    stays the client's, for the reason stated there.

    ``placement`` is where in the request it landed — ``instructions`` at the head
    (re-sent every turn, never retained in history) or ``prompt`` at the tail of the
    turn's own user message (volatile content kept out of the cacheable prefix), or
    ``summary`` on a compaction checkpoint, after the briefing. It is on the wire because
    it is the difference between a block that costs the operator a cache invalidation and
    one that does not.

    ``tokens`` is the coarse estimate the rest of the readout uses, over the **whole**
    block — never over the possibly-truncated ``text``. Additive to v1; no bump."""

    type: Literal["context.injected"] = "context.injected"
    contributor: str
    placement: InjectionPlacement
    tokens: int
    # The resolved block, capped at `INJECTED_TEXT_LIMIT` characters. `truncated` says so
    # rather than leaving the operator to guess whether a file simply ends there.
    text: str
    truncated: bool = False
