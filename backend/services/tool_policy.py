"""The operator's tool-enablement policy (`AE-3.3`).

The enforcement point already exists: ``tools/toolsets.py``'s enabled gate drops a
disabled tool from the catalog the model is offered, so the agent can neither see nor
invoke it. This module is the **operator's half** — which tools they turned off, made
durable, and composed with the suspensions the system decides on its own.

Five independent sources feed one set:

- **the operator's explicit choices**, persisted through ``settings_store`` under
  ``tools.disabled`` as a plain JSON list of namespaced tool names. Policy, not content,
  so it stays in the clear like every other app preference;
- **offline mode's automatic web-tool suspension** (``services/offline``), derived from
  connectivity rather than chosen;
- **the model's own reach** — a tool that answers with an image is withheld from a model
  that cannot see one;
- **whether anyone is watching** — a tool that suspends the turn on the operator is
  withheld from a run that has no operator in front of it (``runs/lanes.py``);
- **whether the capability exists at all** — a category whose backing service the
  operator has never set up (no mailbox connected, no calendar, no secrets vault) is
  withheld rather than offered so that every call can fail.

They **union**: :func:`effective_disabled_tools` is the one place that rule lives, so no
run path can apply one source and silently drop the others. Every site that fills
``RunDeps.disabled_tools`` calls it — the live chat turn, the approval-resume path, and
the scheduler's unattended task executor.

**The permission level and the workspace kind are deliberately not among them**, though
both withhold too (:func:`permission_disabled_tools`, Plan only, and
``services/workspace_kind.workspace_disabled_tools``). They are applied *live*, at the
enabled gate, because unlike the five above they can **move inside a turn**:
``tools/plan.py`` narrows a thread to Plan and an approved plan widens it again, both
taking effect on the next model request. A union cannot be undone — nothing in a set of
names records *which* source put each one there — so a level folded in here could be added
but never correctly removed, and taking the Plan set back out would lift every other
source's hold on the same names with it: a tool the operator switched off by hand would
come back the moment a plan was approved.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.container import ServiceContainer
from runs.lanes import lane_for
from services.permissions import (
    ApprovalPolicy,
    permission_spec,
    tools_beyond_scope,
)
from services.settings_store import DISABLED_TOOLS_KEY, SettingsStore

if TYPE_CHECKING:
    from services.offline import OfflineModeService

logger = logging.getLogger(__name__)


def _decode(raw: str | None) -> frozenset[str]:
    """The stored JSON list as a name set. A missing, malformed, or wrongly-shaped value
    reads as "nothing disabled" rather than raising — a corrupted preference must never
    take the whole tool catalog away from the agent (the same fail-open-to-the-default
    posture ``settings_store``'s own coercers take)."""
    if not raw:
        return frozenset()
    try:
        names = json.loads(raw)
    except ValueError:
        logger.warning("tool policy: ignoring unparseable %s value", DISABLED_TOOLS_KEY)
        return frozenset()
    if not isinstance(names, list):
        logger.warning("tool policy: ignoring non-list %s value", DISABLED_TOOLS_KEY)
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str) and n)


async def get_disabled_tools(settings: SettingsStore, owner_id: str) -> frozenset[str]:
    """The tools the operator has explicitly turned off, by namespaced name."""
    return _decode(await settings.get(owner_id, DISABLED_TOOLS_KEY))


async def set_tool_enabled(
    settings: SettingsStore, owner_id: str, name: str, enabled: bool
) -> frozenset[str]:
    """Flip one tool on or off; returns the operator's full disabled set afterwards.

    Read-modify-write on a single-operator store — two flips racing each other is not a
    case that arises, and the surface that drives this is one operator's settings screen.
    The name is validated against the live catalog by the route, so an unknown name never
    reaches here to rot in the stored set.
    """
    current = await get_disabled_tools(settings, owner_id)
    updated = current - {name} if enabled else current | {name}
    if updated != current:
        await _store(settings, owner_id, updated)
    return updated


async def _store(settings: SettingsStore, owner_id: str, names: Iterable[str]) -> None:
    """Persist the set, sorted so the stored value is stable across equal sets."""
    await settings.set(owner_id, DISABLED_TOOLS_KEY, json.dumps(sorted(names)))


# Tools whose *result* only a vision model can read — they return image content rather
# than text. Written out here because `services/` sits
# below `tools/`, so importing the category that owns them would invert the dependency
# order. `tests/test_tool_policy.py` checks the literal against the real catalog.
VISION_ONLY_TOOLS = frozenset({"browse_screenshot"})


def vision_disabled_tools(vision: bool) -> frozenset[str]:
    """The tools a model that cannot see should not be offered.

    Not a safety gate — a courtesy and a cost one. A text-only model handed a screenshot
    tool spends a call producing image content its provider will reject or silently drop,
    and learns nothing either way. It loses nothing by the withholding: reading a page
    through `snapshot`/`get_text` is the cheaper path the browser capability recommends
    first regardless, and the operator still watches the live page in the panel.
    """
    return frozenset() if vision else VISION_ONLY_TOOLS


# Tools that need a person sitting in front of the run to work at all — they suspend the
# turn on the operator and have no answer without one. Written out for the same reason
# `VISION_ONLY_TOOLS` is, and pinned against the real catalog by `tests/test_tool_policy.py`.
#
# `plan_submit` parks exactly as `builtin_ask_user` does, and `plan_enter` is worse than a
# park: it takes every mutating tool away and leaves `plan_submit` as the only way back,
# so an unattended run that entered plan mode could neither act nor ever be released.
# Plan mode is a conversation with the operator about work not yet done; a run nobody is
# watching has no one to have it with.
ATTENDED_ONLY_TOOLS = frozenset({"builtin_ask_user", "plan_enter", "plan_submit"})


def lane_disabled_tools(kind: str) -> frozenset[str]:
    """The tools a run nobody is watching should not be offered.

    A scheduled task's whole point is that it runs while the operator is elsewhere
    (``runs/lanes.py``), so a question asked from one has no one to answer it: the turn
    parks, and — since a parked turn lives in the process, not the database — it waits
    there until the operator happens to look or the process restarts. Withholding is the
    honest form of that fact. The model is told the capability does not exist rather than
    being offered one that silently strands the run, and it does what it would have done
    anyway: decide, and say what it assumed.

    ``linked`` is treated as unattended too. Those threads are the agent's own, opened
    from inside another conversation; they are visible, and the operator *could* answer
    one, but nothing says they are looking — and the cost of guessing wrong here is a run
    that hangs rather than one that proceeds.
    """
    return frozenset() if lane_for(kind) == "interactive" else ATTENDED_ONLY_TOOLS


def permission_disabled_tools(level: str) -> frozenset[str]:
    """The tools a run at ``level`` must not even be offered.

    Applied at the enabled gate from the run's live level rather than folded into
    :func:`effective_disabled_tools` — see the module note on why a level that can move
    mid-turn must not enter that union.

    Only **Plan** narrows the catalog, and it is the reason the sensitivity classes exist:
    a read-only turn is the one case where withholding beats asking. Every other level
    decides *at the call* — it lets the model see a tool and then gates its execution — so
    they withhold nothing here; taking a tool out of their catalog would tell the model the
    capability does not exist rather than that it needs permission, and it would answer as
    if the operator had never had the option. At the top level there is nothing to withhold
    in any case: its ceiling is above every class, so the set below comes back empty
    through the same comparison rather than through a second rule about that level.

    Withholding rather than prompt-toggling is deliberate. A level that only *asks* the
    model to stay read-only is enforcement by cooperation, which stops working precisely
    when it matters. A tool that is not in the catalog cannot be called by a model that
    decides otherwise.

    Read off the level's two knobs rather than off its name (``services/permissions``): a
    level withholds when its approval policy is to withhold, and what it withholds is what
    it does not permit — the same question the toolset's approval gate asks about one
    tool, asked here about the whole catalog, so the two cannot draw the line in different
    places. A fifth preset is then a row in that registry and nothing here.

    An unrecognised level lands on Plan (``services/permissions``), so a corrupt stored
    value reads as "this thread may only look", not as free rein.
    """
    if permission_spec(level).approval_policy is not ApprovalPolicy.WITHHOLD:
        return frozenset()
    return tools_beyond_scope(level)


#: Asking one feature whether the operator has actually set it up. Takes the run's
#: capability bag rather than a concrete service, so a check is declarable on a manifest
#: (``harness/manifest.py``) without this layer importing anything above it.
type AvailabilityCheck = Callable[[ServiceContainer, str], Awaitable[bool]]


@dataclass(frozen=True)
class CategoryAvailability:
    """A tool category, the tools inside it, and how to ask whether its service exists.

    Assembled once at startup (``app.py``) from the manifests that declare a check: the
    tool names are only knowable where the catalog is built, and whether a mailbox is
    connected is only answerable by the feature that ships the mailbox.
    """

    category: str
    # Namespaced (`category_tool`) names — the vocabulary a disabled set speaks.
    tools: frozenset[str]
    check: AvailabilityCheck


async def unavailable_tools(
    availability: Sequence[CategoryAvailability],
    caps: ServiceContainer | None,
    owner_id: str,
) -> frozenset[str]:
    """Every tool belonging to a category whose backing service the operator has not
    configured — no mailbox connected, no calendar added, no secrets vault created.

    The other axes all answer from data this process already holds; this one has to ask
    a live service, so the checks run **concurrently** and each one is a cheap read the
    feature already exposes to its own routes.

    **A check that raises withholds its category**, and that direction is deliberate.
    Unconfigured is the ordinary state of these features, not an exception — the operator
    who never connected a mailbox is the common case, so a failing check is far more
    likely to mean "there is nothing there" than "this would have worked". Offering a
    category on the strength of a check that just failed spends the model a call and
    hands back an error it can do nothing with. The warning is the loud half: a wiring
    bug is a line in the log, not a broken tool in the catalog.

    A caller with nothing to ask with (``caps`` unset) is treated the same way for the
    same reason — but only when something *did* declare a check. A caller that passes no
    ``availability`` at all is saying nothing about availability and loses nothing.
    """
    if not availability:
        return frozenset()
    if caps is None:
        logger.warning(
            "tool policy: no capability bag to check availability with; "
            "withholding %d categor%s",
            len(availability),
            "y" if len(availability) == 1 else "ies",
        )
        return frozenset(name for entry in availability for name in entry.tools)

    results = await asyncio.gather(
        *(entry.check(caps, owner_id) for entry in availability),
        return_exceptions=True,
    )
    withheld: set[str] = set()
    for entry, result in zip(availability, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "tool policy: availability check for %r failed; withholding the category",
                entry.category,
                exc_info=result,
            )
        elif result:
            continue
        withheld |= entry.tools
    return frozenset(withheld)


async def effective_disabled_tools(
    settings: SettingsStore,
    offline: OfflineModeService,
    owner_id: str,
    *,
    vision: bool = True,
    kind: str = "chat",
    availability: Sequence[CategoryAvailability] = (),
    caps: ServiceContainer | None = None,
) -> frozenset[str]:
    """Everything hidden from the agent this run *for reasons that cannot change mid-turn*:
    the operator's set **unioned** with offline mode's automatic web suspension, the ones
    whose results this run's model cannot read, the ones that need an operator this run
    doesn't have, and the ones whose feature the operator has never set up.

    A union, never a replacement: any one source alone is enough to withhold a tool. The
    permission level and the workspace kind are not here and are applied live at the gate;
    anything that can move inside a turn does not belong in a union, because a union cannot
    be taken apart again.

    ``vision`` defaults to True — permissive — because the callers that cannot know (a
    background agent that resolves its own model) should not have tools taken away by an
    assumption; the interactive paths, which do know, pass the resolved answer. ``kind``
    defaults to the operator's own turn for the same reason, and the two unattended
    composers (the scheduler, the agent's own threads) name themselves. ``availability`` and
    ``caps`` travel together and default to asking nothing.
    """
    return (
        await get_disabled_tools(settings, owner_id)
        | offline.web_tools_disabled()
        | vision_disabled_tools(vision)
        | lane_disabled_tools(kind)
        | await unavailable_tools(availability, caps, owner_id)
    )
