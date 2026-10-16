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

from core.diagnostics import render_tool_error
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
        """Returns the operator's calendar events between two moments, across every
        calendar, with each occurrence of a recurring event listed separately.

        Use it to answer what is coming up, to find free time, and to get the `event_id`
        that `calendar_update_event` and `calendar_delete_event` need. Each entry carries
        `event_id`, its `occurrence_start` and `occurrence_end`, `title`, `all_day`,
        `recurring`, `location` and `calendar_id`.

        A window can span at most 366 days; page through anything longer.

        Args:
            start: An ISO 8601 timestamp. Default now. Include the operator's UTC offset
                (`2026-06-12T09:00:00+02:00`), because a timestamp without one is read as
                UTC.
            end: An ISO 8601 timestamp, with its offset. Default two weeks after `start`.
        """
        service = ctx.deps.caps.get_optional(CalendarService)
        if service is None:
            return [{"error": _UNAVAILABLE}]
        window_start = _moment(start) if start else datetime.now(UTC)
        window_end = _moment(end) if end else window_start + timedelta(days=_DEFAULT_AGENDA_DAYS)
        if window_end < window_start:
            raise ModelRetry(render_tool_error("`end` is before `start`"))
        if window_end - window_start > timedelta(days=_MAX_AGENDA_DAYS):
            raise ModelRetry(
                render_tool_error(
                    f"the window is longer than {_MAX_AGENDA_DAYS} days",
                    hints=["ask for a shorter window and page through the rest"],
                )
            )
        found = await service.occurrences(ctx.deps.owner_id, window_start, window_end)
        return [_occurrence(occurrence) for occurrence in found]

    @toolset.tool
    async def list_calendars(ctx: RunContext[RunDeps]) -> list[dict[str, object]]:
        """Returns the operator's calendars, each with its `calendar_id`, `name` and
        whether it is `read_only`.

        Call it when the operator names a calendar ("put it on my work calendar"), to
        find the `calendar_id` that `calendar_create_event` takes. Without one, events
        go to their first writable calendar.
        """
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
        """Adds an event to one of the operator's calendars and returns it as stored,
        with its `event_id`.

        Before you create one, check `calendar_agenda` when a clash would matter or the
        event may already exist, since a duplicate is easy to make and annoying to find.
        Resolve relative dates ("next Tuesday") against today's date in the operator's
        zone, and state the date and time you chose in your reply so a misreading shows.

        Args:
            title: The event's name, as the operator would write it.
            start: An ISO 8601 timestamp with its UTC offset, because one without is read
                as UTC. For an all-day event, a bare date (`2026-06-12`): a date with a
                time and offset can land on the day before.
            end: An ISO 8601 timestamp with its offset. Omit for a one-hour event, or a
                one-day all-day event. An all-day `end` is the day after the last day.
            calendar_id: The calendar to write to, from `calendar_list_calendars`. Omit
                for the operator's first writable calendar.
            timezone: The IANA zone the event belongs to (`Europe/Madrid`). Default the
                operator's own zone. It keeps a repeating event at the same local time
                across daylight-saving changes.
            all_day: True for an event with no time of day.
            rrule: An RFC 5545 rule for a repeating event, such as
                `FREQ=WEEKLY;BYDAY=MO`.
        """
        service = ctx.deps.caps.get_optional(CalendarService)
        if service is None:
            return {"error": _UNAVAILABLE}
        target = calendar_id or await _default_calendar(ctx)
        if target is None:
            raise ModelRetry(
                render_tool_error(
                    "the operator has no writable calendar",
                    hints=["ask the operator to add one"],
                )
            )
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
            raise ModelRetry(
                render_tool_error(
                    "no such calendar",
                    cause=str(exc),
                    hints=["take a `calendar_id` from `calendar_list_calendars`"],
                )
            ) from exc
        except ValueError as exc:
            raise ModelRetry(
                render_tool_error("the event could not be created", cause=str(exc))
            ) from exc
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
        """Changes an existing event and returns it as stored. Only the fields you pass
        change.

        For a recurring event this changes the whole series. To move a single
        occurrence, cancel it with `calendar_delete_event` and create a one-off event in
        its place.

        Args:
            event_id: The `event_id` from `calendar_agenda`.
            start: A new ISO 8601 start, with its UTC offset.
            end: A new ISO 8601 end, with its UTC offset.
            rrule: A new RFC 5545 rule, which replaces the series' recurrence.
        """
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
            raise _no_such_event(exc) from exc
        except ValueError as exc:
            raise ModelRetry(
                render_tool_error("the change could not be applied", cause=str(exc))
            ) from exc
        return _event(updated)

    @toolset.tool
    async def delete_event(
        ctx: RunContext[RunDeps], event_id: str, occurrence_start: str | None = None
    ) -> str:
        """Deletes an event, or cancels one occurrence of a recurring event.

        Without `occurrence_start` the whole event goes, every occurrence of a series
        included, so pass it whenever the operator means a single date.

        Args:
            event_id: The `event_id` from `calendar_agenda`.
            occurrence_start: The `occurrence_start` of the one instance to cancel, as
                `calendar_agenda` gave it. The rest of the series stays.
        """
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
            raise _no_such_event(exc) from exc
        except ValueError as exc:
            raise ModelRetry(
                render_tool_error("the deletion could not be applied", cause=str(exc))
            ) from exc
        return "Deleted."

    return toolset


def _no_such_event(exc: NotFoundError) -> ModelRetry:
    return ModelRetry(
        render_tool_error(
            "no such event",
            cause=str(exc),
            hints=["take an `event_id` from `calendar_agenda`"],
        )
    )


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
            render_tool_error(
                f"{raw!r} is not an ISO 8601 timestamp",
                hints=["write it like `2026-06-12T13:00:00+02:00`, with the offset"],
            )
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
