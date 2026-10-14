"""The calendar (`CAL-1..3`). Nothing to start or stop: no worker, no held
connections — CalDAV sync runs per request. Its natural-language parser resolves
the background model per call, so a role rebound at runtime takes effect without
rebuilding anything."""

from __future__ import annotations

from harness.manifest import (
    DormantCategory,
    FeatureManifest,
    FeatureRuntime,
    HarnessContext,
    ServiceContainer,
)
from routes import calendar as calendar_routes
from routes.deps import OPERATOR_ID
from services.calendar import CalendarService
from services.calendar.nl import CalendarNaturalLanguage
from services.registry import ModelRegistry
from tools.calendar import calendar_toolset


async def _available(caps: ServiceContainer, owner_id: str) -> bool:
    """Whether the operator keeps a calendar here yet.

    There is no implicit calendar — an event has to be filed on one, so with none added
    every one of the six tools can only answer that there is nothing to read and nowhere
    to write. Cheap enough to ask each turn, and answerable while the app is locked: one
    id off an indexed column, nothing decrypted.
    """
    return await caps.get(CalendarService).has_calendars(owner_id)


async def _build(ctx: HarnessContext) -> FeatureRuntime:
    registry = ctx.services.get(ModelRegistry)

    async def _resolve_model():
        resolved = await registry.resolve_background(owner_id=OPERATOR_ID)
        return resolved.model, resolved.reasoning_off

    calendar = CalendarService(
        ctx.engine, ctx.vault, nl=CalendarNaturalLanguage(resolve_model=_resolve_model)
    )
    return FeatureRuntime(
        services=(calendar,), capabilities=(calendar,), state={"calendar": calendar}
    )


MANIFEST = FeatureManifest(
    name="calendar",
    routers=(calendar_routes.router,),
    toolsets=(("calendar", calendar_toolset),),
    dormant=(
        DormantCategory(
            "calendar",
            "read and change the operator's calendars — what is scheduled, when they "
            + "are free, creating and moving events",
        ),
    ),
    available=_available,
    build=_build,
)
