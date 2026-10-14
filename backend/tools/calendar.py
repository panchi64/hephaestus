"""Calendar tools — the agent's thin adapter over the calendar capability (`CAL-1..3`).

Thin pass-throughs to :class:`~services.calendar.CalendarService` reached via ``RunDeps``;
every rule about zones, recurrence expansion, and validation lives in the service, which is
the same one the REST routes call.

**Not in the sensitive set.** Creating, editing, and deleting the operator's own calendar
entries on their behalf is ordinary assistant work — unlike the `AE-3.1` set (shell, code
execution, filesystem writes, sending mail, config, the vault), which reaches outside the
operator's own data or is hard to undo. A calendar entry is neither: it is visible on the
surface the operator is looking at and one call to remove. Marking it sensitive would train
them to click through approvals, which is what makes a gate stop working where it matters.

If the calendar isn't wired into the run, each tool says so rather than failing — the model
adapts (graceful degradation), the same posture the memory tools take.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from core.exceptions import NotFoundError
from models.calendar import DEFAULT_TIMEZONE
from services.calendar import CalendarService, EventView, OccurrenceView

from .deps import RunDeps

_UNAVAILABLE = "The calendar is unavailable."
# How far ahead `agenda` looks when the caller names no end — a fortnight answers "what's
# coming up" without expanding a decade of a daily rule.
_DEFAULT_AGENDA_DAYS = 14
_MAX_AGENDA_DAYS = 366


def calendar_toolset() -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset()

    @toolset.tool
    async def agenda(
        ctx: RunContext[RunDeps], start: str | None = None, end: str | None = None
    ) -> list[dict[str, object]]:
        """List calendar events between two moments, with recurring events expanded.

        `start` and `end` are ISO-8601 timestamps (UTC unless they carry an offset).
        Defaults to now through two weeks ahead.
        """
        service = ctx.deps.caps.get_optional(CalendarService)
        if service is None:
            return [{"error": _UNAVAILABLE}]
        window_start = _moment(start) if start else datetime.now(UTC)
        window_end = _moment(end) if end else window_start + timedelta(days=_DEFAULT_AGENDA_DAYS)
        if window_end < window_start:
            raise ModelRetry("end must not be before start.")
        if window_end - window_start > timedelta(days=_MAX_AGENDA_DAYS):
            raise ModelRetry(
                f"Ask for a window of at most {_MAX_AGENDA_DAYS} days and page through it."
            )
        found = await service.occurrences(ctx.deps.owner_id, window_start, window_end)
        return [_occurrence(occurrence) for occurrence in found]

    @toolset.tool
    async def list_calendars(ctx: RunContext[RunDeps]) -> list[dict[str, object]]:
        """List the operator's calendars — `calendar_create_event` needs one to write to."""
        service = ctx.deps.caps.get_optional(CalendarService)
        if service is None:
            return [{"error": _UNAVAILABLE}]
        return [
            {"calendar_id": row.id, "name": row.name, "read_only": row.read_only}
            for row in await service.list_calendars(ctx.deps.owner_id)
        ]

    @toolset.tool
    async def create_event(
        ctx: RunContext[RunDeps],
        title: str,
        start: str,
        end: str | None = None,
        calendar_id: str | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        all_day: bool = False,
        location: str | None = None,
        description: str | None = None,
        rrule: str | None = None,
    ) -> dict[str, object]:
        """Add an event to a calendar.

        `start`/`end` are ISO-8601 timestamps and `timezone` is the IANA zone the event
        belongs to (e.g. `Europe/Madrid`) — pass it for a timed event so a repeating one
        keeps its local time across daylight-saving changes. `rrule` is a bare RFC 5545
        rule such as `FREQ=WEEKLY;BYDAY=MO`. Omitting `calendar_id` uses the operator's
        first writable calendar; `calendar_list_calendars` names the rest.
        """
        service = ctx.deps.caps.get_optional(CalendarService)
        if service is None:
            return {"error": _UNAVAILABLE}
        target = calendar_id or await _default_calendar(ctx)
        if target is None:
            raise ModelRetry("There are no writable calendars; ask the operator to add one.")
        try:
            created = await service.create_event(
                ctx.deps.owner_id,
                target,
                title=title,
                starts_at=_moment(start),
                ends_at=_moment(end) if end else None,
                timezone=timezone,
                all_day=all_day,
                location=location,
                description=description,
                rrule=rrule,
            )
        except NotFoundError as exc:
            raise ModelRetry(f"No such calendar: {exc}") from exc
        except ValueError as exc:
            raise ModelRetry(f"That event couldn't be created: {exc}") from exc
        return _event(created)

    @toolset.tool
    async def update_event(
        ctx: RunContext[RunDeps],
        event_id: str,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        timezone: str | None = None,
        location: str | None = None,
        description: str | None = None,
        rrule: str | None = None,
    ) -> dict[str, object]:
        """Change an existing event, by an `event_id` from `calendar_agenda`. Only the
        fields you pass are altered; for a recurring event this changes the whole
        series."""
        service = ctx.deps.caps.get_optional(CalendarService)
        if service is None:
            return {"error": _UNAVAILABLE}
        try:
            updated = await service.update_event(
                ctx.deps.owner_id,
                event_id,
                title=title,
                starts_at=_moment(start) if start else None,
                ends_at=_moment(end) if end else None,
                timezone=timezone,
                location=location,
                description=description,
                rrule=rrule,
            )
        except NotFoundError as exc:
            raise ModelRetry(f"No such event: {exc}") from exc
        except ValueError as exc:
            raise ModelRetry(f"That change couldn't be applied: {exc}") from exc
        return _event(updated)

    @toolset.tool
    async def delete_event(
        ctx: RunContext[RunDeps], event_id: str, occurrence_start: str | None = None
    ) -> str:
        """Delete an event. Pass `occurrence_start` (the ISO start of one instance) to
        cancel just that instance of a recurring event and leave the rest of the series in
        place; omit it to delete the event outright."""
        service = ctx.deps.caps.get_optional(CalendarService)
        if service is None:
            return _UNAVAILABLE
        try:
            if occurrence_start:
                _ = await service.cancel_occurrence(
                    ctx.deps.owner_id, event_id, _moment(occurrence_start)
                )
                return "Cancelled that occurrence; the rest of the series is unchanged."
            await service.delete_event(ctx.deps.owner_id, event_id)
        except NotFoundError as exc:
            raise ModelRetry(f"No such event: {exc}") from exc
        except ValueError as exc:
            raise ModelRetry(f"That deletion couldn't be applied: {exc}") from exc
        return "Deleted."

    return toolset


async def _default_calendar(ctx: RunContext[RunDeps]) -> str | None:
    """The calendar a new event lands on when the model named none — the operator's first
    writable one. Chosen here rather than in the service: "where does this go by default"
    is a convenience for the *agent*, not a rule of the capability."""
    service = ctx.deps.caps.get_optional(CalendarService)
    assert service is not None
    calendars = await service.list_calendars(ctx.deps.owner_id)
    writable = next((row for row in calendars if not row.read_only), None)
    return writable.id if writable is not None else None


def _moment(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp the model produced. A bad one is a ``ModelRetry`` — the
    model can fix its own formatting, which is exactly what that signal is for."""
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ModelRetry(
            f"{raw!r} is not an ISO-8601 timestamp; use e.g. 2026-06-12T13:00:00Z."
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _occurrence(view: OccurrenceView) -> dict[str, object]:
    return {
        "event_id": view.event_id,
        "occurrence_start": view.starts_at.isoformat(),
        "occurrence_end": view.ends_at.isoformat(),
        "title": view.title,
        "all_day": view.all_day,
        "recurring": view.recurring,
        "location": view.location,
        "calendar_id": view.calendar_id,
    }


def _event(view: EventView) -> dict[str, object]:
    return {
        "event_id": view.id,
        "calendar_id": view.calendar_id,
        "title": view.title,
        "start": view.starts_at.isoformat(),
        "end": view.ends_at.isoformat(),
        "timezone": view.timezone,
        "all_day": view.all_day,
        "rrule": view.rrule,
    }
