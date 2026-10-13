"""ICS import/export — the interchange format (`CAL-2`).

Standard calendar files are how a calendar leaves and enters the system: an operator
exports a calendar to hand it to something else, or imports the file another tool gave
them. Both directions run through ``icalendar``; nothing here talks to the DB, so the same
parser serves the ICS upload route *and* the CalDAV sync (a CalDAV object **is** an ICS
document — see `caldav.py`).

**Round-trip is lossless for the fields we model.** Everything the store keeps —
``uid``/``summary``/``description``/``location``, the span, the zone, all-day-ness, the
``RRULE``, and the ``EXDATE`` set — survives an export→import cycle unchanged. That falls
out of writing timed events with a ``TZID`` parameter (so the zone name comes back, not
just the instant) and all-day events as ``DATE`` values (so they stay dates and can't be
re-read as midnight-in-some-zone).

Properties we don't model — attendees, alarms, attachments, per-occurrence overrides
(``RECURRENCE-ID``) — are **dropped on import rather than half-kept**. A half-parsed
attendee list is worse than none: it would look authoritative on screen and be wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar.prop import vRecur

from core.serde import as_utc
from models.calendar import DEFAULT_TIMEZONE, UTC_TIMEZONE
from services.calendar.recurrence import parse_zone
from services.calendar.service import CalendarService, EventView

logger = logging.getLogger(__name__)

PRODID = "-//Hephaestus//Calendar//EN"
ICS_MEDIA_TYPE = "text/calendar"


@dataclass(frozen=True)
class ParsedEvent:
    """One ``VEVENT``, in the store's own vocabulary — the shape ``create_event`` takes,
    so an importer is a loop and not a translation layer."""

    uid: str | None
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str = DEFAULT_TIMEZONE
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    rrule: str | None = None
    exdates: list[str] = field(default_factory=list)


def export_ics(events: Sequence[EventView], *, calendar_name: str | None = None) -> bytes:
    """Serialize events as one ``VCALENDAR`` document.

    A timed event is written in its **own** zone with a ``TZID`` parameter rather than as a
    UTC stamp: the instant alone would round-trip, but the zone — the thing a recurrence has
    to be expanded in — would be lost, and the series would start drifting at the next DST
    boundary in whatever tool read the file.
    """
    document = ICalendar()
    document.add("prodid", PRODID)
    document.add("version", "2.0")
    document.add("calscale", "GREGORIAN")
    if calendar_name:
        # The de-facto name property; every consumer that shows a calendar name reads it.
        document.add("x-wr-calname", calendar_name)

    for event in events:
        document.add_component(_to_component(event))
    return document.to_ical()


def parse_ics(data: bytes | str) -> list[ParsedEvent]:
    """Every ``VEVENT`` in an ICS document, normalized.

    A component that can't be understood at all (no start, unparseable dates) is **skipped
    with a log line** rather than failing the import — one malformed entry in a
    thousand-event file the operator didn't write shouldn't cost them the other 999.
    """
    try:
        document = ICalendar.from_ical(data)
    except ValueError as exc:
        raise ValueError(f"not a readable calendar file: {exc}") from exc

    parsed: list[ParsedEvent] = []
    for component in document.walk("VEVENT"):
        try:
            parsed.append(_from_component(component))
        except ValueError, TypeError, AttributeError:
            logger.warning("calendar: skipping an unreadable VEVENT during import", exc_info=True)
    return parsed


@dataclass(frozen=True)
class ImportResult:
    """What an import did, per event — so the operator is told *"12 new, 3 updated"* rather
    than a bare success."""

    created: int = 0
    updated: int = 0
    skipped: int = 0


async def import_into(
    service: CalendarService, owner_id: str, calendar_id: str, data: bytes | str
) -> ImportResult:
    """Merge an ICS document into a calendar, matching on ``UID``.

    Matching on the UID is what makes re-importing the same file **idempotent** — the
    second run updates the same rows instead of doubling the calendar. An entry the store
    rejects (an unparseable rule, an impossible zone) is counted as skipped, never allowed
    to abort the rest of the file.
    """
    # Checked up front rather than discovered per event: an import into a calendar that
    # doesn't exist is a bad request, not an empty result.
    await service.get_calendar(owner_id, calendar_id)

    created = updated = skipped = 0
    for entry in parse_ics(data):
        try:
            existing = (
                await service.find_by_uid(owner_id, calendar_id, entry.uid) if entry.uid else None
            )
            if existing is None:
                await service.create_event(
                    owner_id,
                    calendar_id,
                    title=entry.title,
                    starts_at=entry.starts_at,
                    ends_at=entry.ends_at,
                    timezone=entry.timezone,
                    all_day=entry.all_day,
                    description=entry.description,
                    location=entry.location,
                    rrule=entry.rrule,
                    uid=entry.uid,
                )
                created += 1
            else:
                await service.update_event(
                    owner_id,
                    existing.id,
                    title=entry.title,
                    starts_at=entry.starts_at,
                    ends_at=entry.ends_at,
                    timezone=entry.timezone,
                    all_day=entry.all_day,
                    description=entry.description or "",
                    location=entry.location or "",
                    rrule=entry.rrule,
                    clear_rrule=entry.rrule is None,
                )
                updated += 1
        except ValueError:
            logger.warning("calendar: skipping an event the store rejected", exc_info=True)
            skipped += 1
            continue
        await _restore_exdates(service, owner_id, calendar_id, entry)
    return ImportResult(created=created, updated=updated, skipped=skipped)


async def export_calendar(service: CalendarService, owner_id: str, calendar_id: str) -> bytes:
    """One calendar as an ICS document (its stored rules — not their expansions, which is
    the whole point of shipping a standard file rather than a list of dates)."""
    calendar = await service.get_calendar(owner_id, calendar_id)
    events = await service.list_events(owner_id, calendar_id)
    return export_ics(events, calendar_name=calendar.name)


async def _restore_exdates(
    service: CalendarService, owner_id: str, calendar_id: str, entry: ParsedEvent
) -> None:
    """Re-apply the imported cancellations. They ride the ordinary ``cancel_occurrence``
    path (rather than being written straight onto the row) so an ``EXDATE`` on a
    non-recurring event — which some exporters emit — is rejected the same way it would be
    coming from the UI, instead of persisting an EXDATE that can never match anything."""
    if not entry.exdates or entry.uid is None:
        return
    stored = await service.find_by_uid(owner_id, calendar_id, entry.uid)
    if stored is None or not stored.rrule:
        return
    for stamp in entry.exdates:
        try:
            await service.cancel_occurrence(owner_id, stored.id, datetime.fromisoformat(stamp))
        except ValueError:
            continue


# --- export ---------------------------------------------------------------


def _to_component(event: EventView) -> IEvent:
    component = IEvent()
    component.add("uid", event.uid)
    component.add("summary", event.title)
    component.add("dtstamp", event.updated_at)
    if event.description:
        component.add("description", event.description)
    if event.location:
        component.add("location", event.location)

    if event.all_day:
        # DATE values, so the day stays a day. DTEND is exclusive, which is what the
        # store already holds.
        component.add("dtstart", event.starts_at.date())
        component.add("dtend", event.ends_at.date())
    else:
        zone = parse_zone(event.timezone)
        component.add("dtstart", event.starts_at.astimezone(zone))
        component.add("dtend", event.ends_at.astimezone(zone))

    if event.rrule:
        component.add("rrule", vRecur.from_ical(event.rrule))
    if event.exdates:
        cancelled = [as_utc(datetime.fromisoformat(stamp)) for stamp in event.exdates]
        if event.all_day:
            component.add("exdate", [moment.date() for moment in cancelled])
        else:
            zone = parse_zone(event.timezone)
            component.add("exdate", [moment.astimezone(zone) for moment in cancelled])
    return component


# --- import ---------------------------------------------------------------


def _from_component(component: IEvent) -> ParsedEvent:
    start_prop = component.get("dtstart")
    if start_prop is None:
        raise ValueError("VEVENT has no DTSTART")
    raw_start = start_prop.dt
    all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)

    zone_name = _zone_name(start_prop, raw_start, all_day)
    start = _instant(raw_start, all_day)
    end = _end_instant(component, start, all_day)

    rrule = component.get("rrule")
    return ParsedEvent(
        uid=str(component.get("uid")) if component.get("uid") else None,
        title=str(component.get("summary") or "(untitled)"),
        starts_at=start,
        ends_at=end,
        timezone=zone_name,
        all_day=all_day,
        description=_text(component.get("description")),
        location=_text(component.get("location")),
        rrule=rrule.to_ical().decode() if rrule is not None else None,
        exdates=_exdates(component, all_day),
    )


def _zone_name(start_prop, raw_start: date | datetime, all_day: bool) -> str:
    """The event's IANA zone. The ``TZID`` parameter is preferred over the parsed
    ``tzinfo`` — it is the name the file's author *wrote*, and it is the thing a recurrence
    has to be expanded in. All-day events are date-based and are always stored in UTC."""
    if all_day:
        return UTC_TIMEZONE
    tzid = start_prop.params.get("TZID")
    if tzid:
        try:
            parse_zone(str(tzid))
        except ValueError:
            # An unknown/proprietary TZID (some exporters emit their own names) is not
            # worth losing the event over — keep the instant, fall back to UTC.
            logger.warning("calendar: unknown TZID %r on import; falling back to UTC", tzid)
            return UTC_TIMEZONE
        return str(tzid)
    tzinfo = getattr(raw_start, "tzinfo", None)
    key = getattr(tzinfo, "key", None)
    return str(key) if key else UTC_TIMEZONE


def _instant(value: date | datetime, all_day: bool) -> datetime:
    """A ``DATE`` becomes UTC midnight; a floating ``DATE-TIME`` (no zone at all) is read
    as UTC, matching how the store treats a naive timestamp everywhere else."""
    if all_day:
        return datetime.combine(value, time.min, tzinfo=UTC)
    return as_utc(value)  # type: ignore[arg-type]


def _end_instant(component: IEvent, start: datetime, all_day: bool) -> datetime:
    """``DTEND`` when present, else ``DTSTART + DURATION``, else the RFC 5545 default: a
    whole day for a ``DATE`` event, a zero-length instant for a timed one (which the store
    then widens to an hour)."""
    end_prop = component.get("dtend")
    if end_prop is not None:
        return _instant(end_prop.dt, all_day)
    duration = component.get("duration")
    if duration is not None and isinstance(duration.dt, timedelta):
        return start + duration.dt
    return start + timedelta(days=1) if all_day else start


def _exdates(component: IEvent, all_day: bool) -> list[str]:
    """The ``EXDATE`` set as ISO UTC strings. The property may appear more than once and
    each occurrence may carry a list, so both shapes are flattened."""
    raw = component.get("exdate")
    if raw is None:
        return []
    groups = raw if isinstance(raw, list) else [raw]
    stamps: list[str] = []
    for group in groups:
        for value in getattr(group, "dts", []):
            stamps.append(_instant(value.dt, all_day).isoformat())
    return stamps


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
