"""Callsigns: the four-letter names the operator knows spawned agents by (``docs/voice.md`` §3).

A callsign is drawn from the agent's class pool and is unique among the live agents of one
delegation tree. Models address agents by handle, but any reference an agent tool takes
may be either, because the operator names agents by callsign and Talos passes that on.
"""

from __future__ import annotations

import random
from collections.abc import Collection, Sequence

from core.diagnostics import render_tool_error, suggest
from models.subagent import SubagentRecord
from prompts.voice import CLASSES
from services.subagent_store import LIVE_STATUSES
from services.subagents.launcher import SubagentUnavailableError


def fold(name: str) -> str:
    """``name`` as references compare: case-insensitive, with ``Ē`` read as ``E``."""
    return name.strip().casefold().replace("ē", "e")


def class_display(agent_class: str) -> str:
    """The class's display name (``Tripodes``), or empty for an unknown or empty key."""
    spec = CLASSES.get(agent_class)
    return spec.display if spec is not None else ""


def designation(callsign: str, agent_class: str) -> str:
    """``BRAX · Tripodes``, or empty for an agent with no callsign."""
    if not callsign:
        return ""
    display = class_display(agent_class)
    return f"{callsign} · {display}" if display else callsign


def allocate(
    agent_class: str,
    taken: Collection[str],
    *,
    avoid: Collection[str] = (),
    rng: random.Random | None = None,
) -> str:
    """A callsign from ``agent_class``'s pool that is not in ``taken``.

    Entries in ``avoid`` are passed over while any other entry is free. Once every entry is
    taken, an entry gets the first free suffix (``BRAX-2``). A class with no pool (Talos,
    the helper voices) gets an empty callsign.
    """
    spec = CLASSES.get(agent_class)
    pool = spec.callsigns if spec is not None else ()
    if not pool:
        return ""
    pick = rng or random.Random()
    blocked = {fold(name) for name in taken}
    avoided = {fold(name) for name in avoid}
    free = [entry for entry in pool if fold(entry) not in blocked]
    fresh = [entry for entry in free if fold(entry) not in avoided]
    if free:
        return pick.choice(fresh or free)
    suffix = 2
    while True:
        suffixed = [f"{entry}-{suffix}" for entry in pool]
        free = [name for name in suffixed if fold(name) not in blocked]
        if free:
            return pick.choice(free)
        suffix += 1


def resolve(reference: str, agents: Sequence[SubagentRecord]) -> SubagentRecord:
    """The agent ``reference`` names among ``agents``, by handle or by callsign.

    A handle wins over a callsign, since handles are unique in a tree. A callsign shared by
    a finished agent and a live one names the live one.

    Raises:
        SubagentUnavailableError: nothing matches, or the callsign fits more than one live
            agent. The message is a ``render_tool_error`` block with help lines.
    """
    wanted = fold(reference)
    keys = {wanted, wanted.replace(" ", "-")}
    by_handle = [row for row in agents if fold(row.handle or row.spec_name) in keys]
    if by_handle:
        return max(by_handle, key=lambda row: row.started_at)
    by_callsign = [row for row in agents if row.callsign and fold(row.callsign) in keys]
    live = [row for row in by_callsign if row.status in LIVE_STATUSES]
    matches = live or by_callsign
    if len(matches) == 1:
        return matches[0]
    if matches:
        named = ", ".join(f"`{row.handle}` ({row.status})" for row in matches)
        raise SubagentUnavailableError(
            render_tool_error(
                f"{reference!r} names more than one agent in your tree: {named}",
                hints=["call again with the handle of the one you mean"],
            )
        )
    names = [name for row in agents for name in (row.handle, row.callsign) if name]
    hints = [hint for hint in (suggest(reference, names),) if hint]
    raise SubagentUnavailableError(
        render_tool_error(
            f"nothing in your delegation tree is called {reference!r}",
            hints=[*hints, "call agents_list for every handle and callsign in your tree"],
        )
    )
