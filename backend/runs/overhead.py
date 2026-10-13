"""What a model request carries besides the conversation.

Sits in ``runs`` rather than beside the code that measures it (``agent.overhead``) for
the same reason ``runs.timings`` does: the :class:`Run` holds one across a turn, and
``runs`` cannot import from ``agent``. The measurement is the agent layer's; the value
is the run's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BriefBlock:
    """One contributor to the standing brief, in characters.

    ``id`` is a stable slug the readout groups by — ``base`` for the fixed prompt this
    codebase always sends, and one per dynamic instruction provider (the skill catalog,
    the project's own instruction files, the delegate listing), derived from the
    provider's own name. The operator-facing wording is the client's; what travels is
    which block it was.
    """

    id: str
    chars: int


@dataclass(frozen=True)
class ToolGroupOverhead:
    """One tool category's share of the schemas, in characters, and how many tools it
    put there.

    The category is the namespace prefix the tool was registered under
    (``tools/toolsets.py`` builds every name as ``category_tool``), so this is the same
    grouping the operator's own tool settings page uses — which is the point: a category
    that is eating the window is a category they can switch off in one place.
    """

    category: str
    tools: int
    chars: int


@dataclass(frozen=True)
class TurnOverhead:
    """The standing brief and the tool schemas, in characters.

    Characters rather than tokens because these are only ever used as proportions
    against the message estimate (``services.context_budget``), and rounding each to
    tokens before comparing them would throw away precision for nothing.

    ``system`` is the whole standing brief as one figure and ``tools`` every tool name,
    description and JSON schema the model was handed. ``blocks`` and ``groups`` are the
    same two numbers itemised — they sum to their totals by construction, and the totals
    stay because they are what the gauge's three-part bar is drawn from.

    The itemisation is what makes the readout *actionable* rather than merely accurate:
    "your brief is 5k" is a fact, "the skill catalog is 4k of it" is a decision. Both
    default to empty, so a measurement that could only reach the totals (an older
    cached one, a library upgrade that moved an internal) still reports them.
    """

    system: int
    tools: int
    blocks: tuple[BriefBlock, ...] = ()
    groups: tuple[ToolGroupOverhead, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The measurement as plain JSON-able data, for the conversation column that
        carries it between turns (``services.conversations``). Written out field by
        field rather than via ``dataclasses.asdict`` so the stored shape is a
        deliberate contract rather than whatever the dataclass happens to look like —
        renaming an attribute should break a test here, not silently orphan every
        stored blob."""
        return {
            "system": self.system,
            "tools": self.tools,
            "blocks": [{"id": b.id, "chars": b.chars} for b in self.blocks],
            "groups": [
                {"category": g.category, "tools": g.tools, "chars": g.chars} for g in self.groups
            ],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> TurnOverhead | None:
        """Rebuild a stored measurement, or ``None`` if it can't be read.

        Total-tolerant on the way in for the same reason the totals exist at all: a blob
        written by an older version carries no ``blocks``/``groups``, and the coarse
        three-way reading is still worth having without them. Anything malformed enough
        to raise reads as absent — a thread whose stored overhead is unreadable should
        show no breakdown, never a wrong one.

        A negative figure is malformed in exactly that sense and is rejected too. These
        are character counts, so there is no reading under which one is below zero; left
        in, it would flow through the composer's proportional scaling as a *negative
        share* and draw a bar segment of negative width — a wrong breakdown, which is the
        one outcome this is here to prevent."""
        if not isinstance(raw, dict):
            return None
        try:
            overhead = cls(
                system=int(raw["system"]),
                tools=int(raw["tools"]),
                blocks=tuple(
                    BriefBlock(id=str(b["id"]), chars=int(b["chars"]))
                    for b in raw.get("blocks") or ()
                ),
                groups=tuple(
                    ToolGroupOverhead(
                        category=str(g["category"]), tools=int(g["tools"]), chars=int(g["chars"])
                    )
                    for g in raw.get("groups") or ()
                ),
            )
        except KeyError, TypeError, ValueError:
            return None
        counts = (
            overhead.system,
            overhead.tools,
            *(b.chars for b in overhead.blocks),
            *(g.chars for g in overhead.groups),
            *(g.tools for g in overhead.groups),
        )
        return overhead if all(c >= 0 for c in counts) else None
