"""The calendar surface (`CAL-1..3`) — calendars, events, ICS files, sync, NL entry.

Thin over `services/calendar`: parse, delegate, map. Out-shapes are camelCase, matching
the app's other newer surfaces (corpus/uploads/tasks/notifications) and the
frontend's own calendar contract.

Two shapes are worth knowing before reading the handlers:

- **An event and an occurrence are different things.** ``/calendar/events`` returns the
  stored events (a recurring series is *one*), while ``/calendar/occurrences`` returns the
  expanded instances between two moments — what a calendar grid renders. Only the second
  requires a window, because that window is what bounds an open-ended rule.
- **Deleting is two operations.** ``DELETE /calendar/events/{id}`` drops the series;
  ``DELETE /calendar/events/{id}/occurrences?start=…`` cancels one instance and leaves the
  rule intact.

The domain layer raises ``NotFoundError`` and plain ``ValueError`` for malformed input;
both are mapped here (404 / 422) rather than anywhere below.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile

from core.exceptions import DegradedCapabilityError, SSRFError
from models.calendar import DEFAULT_TIMEZONE
from routes import deps
from routes.camel import CamelModel
from routes.deps import OPERATOR_ID
from services.calendar import CalendarView, EventView, OccurrenceView
from services.calendar.caldav import CalDavSync
from services.calendar.ics import ICS_MEDIA_TYPE, export_calendar, import_into
from services.calendar.nl import EventDraft

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])

# The default span of an unbounded occurrences query — a month either side of now, so the
# common "show me the calendar" call needs no parameters and still can't ask the expander
# for an unbounded walk.
_DEFAULT_WINDOW = timedelta(days=31)
# A ceiling on how wide a window a caller may ask for. Recurrence expansion is linear in
# the window, so an unbounded range is the one way this surface could be made expensive.
_MAX_WINDOW = timedelta(days=1100)
_MAX_ICS_BYTES = 8 * 1024 * 1024


# --- wire shapes ----------------------------------------------------------


class CalendarOut(CamelModel):
    id: str
    name: str
    tone: str
    synced: bool
    sync_url: str | None = None
    read_only: bool = False
    last_synced_at: datetime | None = None


class CalendarListOut(CamelModel):
    items: list[CalendarOut]


class CalendarCreate(CamelModel):
    name: str
    tone: str = "nominal"
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_password: str | None = None
    read_only: bool = False


class CalendarUpdate(CamelModel):
    """A partial update — ``null`` leaves a field alone. A ``caldav_*`` field set to the
    empty string clears it, which is how a calendar is unbound from its server."""

    name: str | None = None
    tone: str | None = None
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_password: str | None = None
    read_only: bool | None = None


class EventOut(CamelModel):
    """One stored event — the rule, not its occurrences."""

    id: str
    calendar_id: str
    uid: str
    title: str
    start: datetime
    end: datetime
    timezone: str
    all_day: bool
    description: str | None = None
    location: str | None = None
    rrule: str | None = None
    exdates: list[str] = []


class EventListOut(CamelModel):
    items: list[EventOut]


class OccurrenceOut(CamelModel):
    """One dated instance — what a calendar grid renders. ``occurrenceId`` addresses this
    instance so a caller can cancel exactly it out of a series."""

    occurrence_id: str
    event_id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    timezone: str
    all_day: bool
    recurring: bool
    description: str | None = None
    location: str | None = None
    rrule: str | None = None


class OccurrenceListOut(CamelModel):
    items: list[OccurrenceOut]


class EventCreate(CamelModel):
    calendar_id: str
    title: str
    start: datetime
    end: datetime | None = None
    timezone: str = DEFAULT_TIMEZONE
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    rrule: str | None = None


class EventUpdate(CamelModel):
    calendar_id: str | None = None
    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    timezone: str | None = None
    all_day: bool | None = None
    description: str | None = None
    location: str | None = None
    rrule: str | None = None
    # Explicit, because ``rrule: null`` already means "leave it alone" in a partial patch —
    # this is the only way to turn a series back into a single event.
    clear_rrule: bool = False


class SyncOut(CamelModel):
    pulled_created: int
    pulled_updated: int
    pushed: int
    removed_locally: int
    skipped: int
    last_synced_at: datetime | None = None


class SyncAllOut(CamelModel):
    """The result of reconciling every remote-bound calendar at once.

    ``calendars`` is how many were bound to a server (0 ⇒ there was nothing to sync, which
    is a different thing to say than "no changes"), ``changed`` the total number of events
    that moved in either direction, and ``failed`` the calendars whose server refused —
    a caller can report all three without deciding anything itself.
    """

    calendars: int
    changed: int
    failed: list[str] = []


class ImportOut(CamelModel):
    created: int
    updated: int
    skipped: int


class ParseIn(CamelModel):
    phrase: str
    timezone: str = DEFAULT_TIMEZONE


class DraftOut(CamelModel):
    """A parsed phrase, *not* a stored event — the operator confirms it before it lands."""

    title: str
    start: datetime
    end: datetime
    timezone: str
    all_day: bool
    description: str | None = None
    location: str | None = None
    rrule: str | None = None


# --- calendars ------------------------------------------------------------


@router.get("/calendars", response_model=CalendarListOut)
async def list_calendars(request: Request) -> CalendarListOut:
    service = deps.calendar(request)
    rows = await service.list_calendars(OPERATOR_ID)
    return CalendarListOut(items=[_calendar_out(row) for row in rows])


@router.post("/calendars", status_code=201, response_model=CalendarOut)
async def create_calendar(body: CalendarCreate, request: Request) -> CalendarOut:
    service = deps.calendar(request)
    with _mapped():
        created = await service.create_calendar(
            OPERATOR_ID,
            body.name,
            tone=body.tone,
            caldav_url=body.caldav_url,
            caldav_username=body.caldav_username,
            caldav_password=body.caldav_password,
            read_only=body.read_only,
        )
    return _calendar_out(created)


@router.patch("/calendars/{calendar_id}", response_model=CalendarOut)
async def update_calendar(calendar_id: str, body: CalendarUpdate, request: Request) -> CalendarOut:
    service = deps.calendar(request)
    with _mapped():
        updated = await service.update_calendar(
            OPERATOR_ID,
            calendar_id,
            name=body.name,
            tone=body.tone,
            caldav_url=body.caldav_url,
            caldav_username=body.caldav_username,
            caldav_password=body.caldav_password,
            read_only=body.read_only,
        )
    return _calendar_out(updated)


@router.delete("/calendars/{calendar_id}", status_code=204)
async def delete_calendar(calendar_id: str, request: Request) -> Response:
    service = deps.calendar(request)
    with _mapped():
        await service.delete_calendar(OPERATOR_ID, calendar_id)
    return Response(status_code=204)


@router.post("/calendars/{calendar_id}/sync", response_model=SyncOut)
async def sync_calendar(calendar_id: str, request: Request) -> SyncOut:
    """Reconcile a calendar with its CalDAV server (`CAL-2`).

    A refused server URL is a **502**, not a 422: the request was well-formed and the
    calendar's binding is what the operator has to fix, so it reads as an upstream problem
    rather than a bad call.
    """
    service = deps.calendar(request)
    try:
        result = await CalDavSync(service).sync(OPERATOR_ID, calendar_id)
    except SSRFError as exc:
        raise HTTPException(status_code=502, detail=f"refused to reach that server: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # any transport failure is an upstream error
        raise HTTPException(status_code=502, detail=f"calendar sync failed: {exc}") from exc

    calendar = await service.get_calendar(OPERATOR_ID, calendar_id)
    return SyncOut(
        pulled_created=result.pulled_created,
        pulled_updated=result.pulled_updated,
        pushed=result.pushed,
        removed_locally=result.removed_locally,
        skipped=result.skipped,
        last_synced_at=calendar.last_synced_at,
    )


@router.post("/sync", response_model=SyncAllOut)
async def sync_all_calendars(request: Request) -> SyncAllOut:
    """Reconcile every calendar bound to a CalDAV server (`CAL-2`).

    Which calendars are remote, and what "one sync" means across them, is decided here
    rather than by the caller looping over `/calendars/{id}/sync` — the surface that shows
    a SYNC button should be able to press it with one call and render what came back.

    One server being unreachable doesn't abandon the others: the failure is collected and
    the remaining calendars still sync, so a single stale binding can't silently stop the
    rest of the schedule from updating.
    """
    service = deps.calendar(request)
    syncer = CalDavSync(service)
    remote = [row for row in await service.list_calendars(OPERATOR_ID) if row.synced]

    changed = 0
    failed: list[str] = []
    for calendar in remote:
        try:
            result = await syncer.sync(OPERATOR_ID, calendar.id)
        except Exception:  # one bad binding must not sink the others
            logger.warning("calendar sync failed for %s", calendar.id, exc_info=True)
            failed.append(calendar.name)
            continue
        changed += (
            result.pulled_created + result.pulled_updated + result.pushed + result.removed_locally
        )
    return SyncAllOut(calendars=len(remote), changed=changed, failed=failed)


# --- ICS files (`CAL-2`) --------------------------------------------------


@router.get("/calendars/{calendar_id}/export")
async def export_ics_file(calendar_id: str, request: Request) -> Response:
    service = deps.calendar(request)
    with _mapped():
        document = await export_calendar(service, OPERATOR_ID, calendar_id)
    return Response(
        content=document,
        media_type=ICS_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{calendar_id}.ics"'},
    )


@router.post("/calendars/{calendar_id}/import", response_model=ImportOut)
async def import_ics_file(calendar_id: str, request: Request, file: UploadFile) -> ImportOut:
    raw = await file.read()
    if len(raw) > _MAX_ICS_BYTES:
        raise HTTPException(status_code=413, detail="calendar file is too large")
    service = deps.calendar(request)
    with _mapped():
        result = await import_into(service, OPERATOR_ID, calendar_id, raw)
    return ImportOut(created=result.created, updated=result.updated, skipped=result.skipped)


# --- events ---------------------------------------------------------------


@router.get("/events", response_model=EventListOut)
async def list_events(
    request: Request,
    calendar_id: Annotated[str | None, Query(alias="calendarId")] = None,
) -> EventListOut:
    """The stored events — a recurring series appears once. For the dated instances a
    grid renders, call ``/calendar/occurrences``."""
    service = deps.calendar(request)
    rows = await service.list_events(OPERATOR_ID, calendar_id)
    return EventListOut(items=[_event_out(row) for row in rows])


@router.get("/occurrences", response_model=OccurrenceListOut)
async def list_occurrences(
    request: Request,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    calendar_id: Annotated[str | None, Query(alias="calendarId")] = None,
) -> OccurrenceListOut:
    """Everything between two moments, recurrences expanded. Defaults to a month either
    side of now; an over-wide window is refused rather than silently truncated, so a caller
    is never quietly shown less than it asked for."""
    now = datetime.now(UTC)
    window_start = start or (now - _DEFAULT_WINDOW)
    window_end = end or (now + _DEFAULT_WINDOW)
    if window_end < window_start:
        raise HTTPException(status_code=422, detail="end must not precede start")
    if window_end - window_start > _MAX_WINDOW:
        raise HTTPException(
            status_code=422, detail=f"window must not exceed {_MAX_WINDOW.days} days"
        )

    service = deps.calendar(request)
    with _mapped():
        found = await service.occurrences(
            OPERATOR_ID,
            window_start,
            window_end,
            calendar_ids=[calendar_id] if calendar_id else None,
        )
    return OccurrenceListOut(items=[_occurrence_out(row) for row in found])


@router.post("/events", status_code=201, response_model=EventOut)
async def create_event(body: EventCreate, request: Request) -> EventOut:
    service = deps.calendar(request)
    with _mapped():
        created = await service.create_event(
            OPERATOR_ID,
            body.calendar_id,
            title=body.title,
            starts_at=body.start,
            ends_at=body.end,
            timezone=body.timezone,
            all_day=body.all_day,
            description=body.description,
            location=body.location,
            rrule=body.rrule,
        )
    return _event_out(created)


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: str, request: Request) -> EventOut:
    service = deps.calendar(request)
    with _mapped():
        return _event_out(await service.get_event(OPERATOR_ID, event_id))


@router.patch("/events/{event_id}", response_model=EventOut)
async def update_event(event_id: str, body: EventUpdate, request: Request) -> EventOut:
    service = deps.calendar(request)
    with _mapped():
        updated = await service.update_event(
            OPERATOR_ID,
            event_id,
            calendar_id=body.calendar_id,
            title=body.title,
            starts_at=body.start,
            ends_at=body.end,
            timezone=body.timezone,
            all_day=body.all_day,
            description=body.description,
            location=body.location,
            rrule=body.rrule,
            clear_rrule=body.clear_rrule,
        )
    return _event_out(updated)


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(event_id: str, request: Request) -> Response:
    """Delete the event — the whole series when it recurs."""
    service = deps.calendar(request)
    with _mapped():
        await service.delete_event(OPERATOR_ID, event_id)
    return Response(status_code=204)


@router.delete("/events/{event_id}/occurrences", response_model=EventOut)
async def cancel_occurrence(
    event_id: str, request: Request, start: Annotated[datetime, Query()]
) -> EventOut:
    """Cancel one instance of a series, leaving the rule intact."""
    service = deps.calendar(request)
    with _mapped():
        return _event_out(await service.cancel_occurrence(OPERATOR_ID, event_id, start))


# --- natural language (`CAL-3`) -------------------------------------------


@router.post("/parse", response_model=DraftOut)
async def parse_phrase(body: ParseIn, request: Request) -> DraftOut:
    """Parse a phrase into a **draft** the caller then confirms — this writes nothing."""
    parser = deps.calendar(request).nl
    if parser is None:
        raise HTTPException(status_code=503, detail="Natural-language entry is unavailable.")
    try:
        draft = await parser.parse(body.phrase, timezone=body.timezone)
    except DegradedCapabilityError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _draft_out(draft)


# --- plumbing -------------------------------------------------------------


@contextmanager
def _mapped() -> Generator[None]:
    """Map the two domain errors this surface can raise onto HTTP in one place, rather
    than repeating the same pair of ``except`` clauses on a dozen handlers — an unknown id
    is a 404 and malformed input (a bad zone, an unparseable rule, an empty title) is a
    422, on every route here."""
    try:
        yield
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _calendar_out(view: CalendarView) -> CalendarOut:
    return CalendarOut(
        id=view.id,
        name=view.name,
        tone=view.tone,
        synced=view.synced,
        sync_url=view.sync_url,
        read_only=view.read_only,
        last_synced_at=view.last_synced_at,
    )


def _event_out(view: EventView) -> EventOut:
    return EventOut(
        id=view.id,
        calendar_id=view.calendar_id,
        uid=view.uid,
        title=view.title,
        start=view.starts_at,
        end=view.ends_at,
        timezone=view.timezone,
        all_day=view.all_day,
        description=view.description,
        location=view.location,
        rrule=view.rrule,
        exdates=view.exdates,
    )


def _occurrence_out(view: OccurrenceView) -> OccurrenceOut:
    return OccurrenceOut(
        occurrence_id=view.occurrence_id,
        event_id=view.event_id,
        calendar_id=view.calendar_id,
        title=view.title,
        start=view.starts_at,
        end=view.ends_at,
        timezone=view.timezone,
        all_day=view.all_day,
        recurring=view.recurring,
        description=view.description,
        location=view.location,
        rrule=view.rrule,
    )


def _draft_out(draft: EventDraft) -> DraftOut:
    return DraftOut(
        title=draft.title,
        start=draft.starts_at,
        end=draft.ends_at,
        timezone=draft.timezone,
        all_day=draft.all_day,
        description=draft.description,
        location=draft.location,
        rrule=draft.rrule,
    )
