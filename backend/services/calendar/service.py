"""The calendar capability (`CAL-1`) — calendars, events, and the expanded read.

CRUD over an at-rest-sealed store, plus the one read that matters: **what is on the
calendar between two instants**. That read is where recurrence lives — a stored ``RRULE``
is expanded on the way out (`recurrence.py`), never on the way in, so a series is one row
and an open-ended rule is bounded by the window the caller asked for.

Three invariants the store keeps:

- **An event is normalized before it is stored.** An all-day event is floored to UTC
  midnights with an exclusive end, a timed event's zone is validated, and a recurrence rule
  is parsed once at write time — so a rule that can never expand is rejected at the point
  the operator can fix it, not on some later read.
- **A single occurrence is cancelled, never deleted.** Removing one instance of a series
  appends an ``EXDATE``; the rule itself is untouched, which is what keeps "delete this
  one" and "delete the series" genuinely different operations.
- **Content comes back decrypted; structure never needed decrypting.** Listing a window
  ranges over clear ``starts_at``/``ends_at`` columns and only opens the vault for the
  events that survive the range — so a year-wide calendar costs no decryption for the
  eleven months nobody asked about.

Raises domain errors (`NotFoundError`) and :class:`ValueError` for malformed input only —
the route maps both to HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Engine, or_
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import NotFoundError
from core.serde import as_utc
from core.vault import Vault
from models.calendar import DEFAULT_TIMEZONE, UTC_TIMEZONE, Calendar, CalendarEvent, new_uid
from services.calendar.recurrence import (
    canonical_rrule,
    expand,
    next_occurrence,
    parse_zone,
)

if TYPE_CHECKING:
    # Only ever an annotation here — the store never parses anything, it just carries the
    # parser for its callers (see `CalendarService.nl`), so importing it lazily keeps the
    # store's import free of the model layer.
    from services.calendar.nl import CalendarNaturalLanguage

# How far a "what's next" lookahead searches before giving up (see `upcoming`).
_UPCOMING_HORIZON_DAYS = 366


@dataclass(frozen=True)
class CalendarView:
    """A decrypted calendar (content in the clear to its owner)."""

    id: str
    name: str
    tone: str
    synced: bool
    sync_url: str | None
    read_only: bool
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventView:
    """A decrypted event — the stored *rule*, not an occurrence of it."""

    id: str
    calendar_id: str
    uid: str
    title: str
    description: str | None
    location: str | None
    starts_at: datetime
    ends_at: datetime
    timezone: str
    all_day: bool
    rrule: str | None
    exdates: list[str]
    created_at: datetime
    updated_at: datetime
    # Remote-sync bookkeeping — where this event lives on its CalDAV server and the
    # server's version stamp. Both null for a purely local event; the sync capability
    # reads them to tell "never pushed" from "already there, unchanged".
    remote_href: str | None = None
    remote_etag: str | None = None


@dataclass(frozen=True)
class OccurrenceView:
    """One dated instance of an event — what a calendar grid actually renders.

    ``occurrence_id`` addresses this instance (``{event id}@{UTC start}``) so a caller can
    cancel exactly this one out of a series without inventing its own encoding; ``recurring``
    tells the UI whether "delete" needs to ask *this one or all of them*.
    """

    occurrence_id: str
    event_id: str
    calendar_id: str
    title: str
    description: str | None
    location: str | None
    starts_at: datetime
    ends_at: datetime
    timezone: str
    all_day: bool
    recurring: bool
    rrule: str | None


class CalendarService:
    def __init__(
        self,
        engine: Engine,
        vault: Vault,
        *,
        nl: CalendarNaturalLanguage | None = None,
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        # Natural-language event entry (`CAL-3`). It hangs off the service rather than
        # standing beside it because the calendar reaches its callers as ONE capability
        # handle — a tool that has the calendar has everything the calendar can do. The
        # service itself never calls it: parsing needs a model resolver, storing doesn't.
        # None ⇒ no utility model was wired, and the parse entry points say so.
        self.nl: CalendarNaturalLanguage | None = nl

    @property
    def engine(self) -> Engine:
        return self._engine

    # --- calendars --------------------------------------------------------

    async def create_calendar(
        self,
        owner_id: str,
        name: str,
        *,
        tone: str = "nominal",
        caldav_url: str | None = None,
        caldav_username: str | None = None,
        caldav_password: str | None = None,
        read_only: bool = False,
    ) -> CalendarView:
        if not name.strip():
            raise ValueError("calendar name must not be empty")
        calendar = Calendar(
            owner_id=owner_id,
            name_enc=self._vault.encrypt_str(name.strip()),
            tone=tone,
            caldav_url=caldav_url,
            caldav_username_enc=self._seal(caldav_username),
            caldav_password_enc=self._seal(caldav_password),
            read_only=read_only,
        )

        def work(session: Session) -> CalendarView:
            session.add(calendar)
            session.flush()
            return self._calendar_view(calendar)

        return await in_session(self._engine, work)

    async def list_calendars(self, owner_id: str) -> list[CalendarView]:
        def work(session: Session) -> list[CalendarView]:
            rows = session.exec(
                select(Calendar)
                .where(Calendar.owner_id == owner_id)
                .order_by(col(Calendar.created_at))
            ).all()
            return [self._calendar_view(row) for row in rows]

        return await in_session(self._engine, work)

    async def has_calendars(self, owner_id: str) -> bool:
        """Whether a calendar has been added at all — one id, and nothing opened.

        Asked on every turn to decide whether the calendar tools are worth offering,
        which is a yes/no about existence and must be answerable while the vault is
        locked. ``list_calendars`` builds views, and a view decrypts the name.
        """

        def work(session: Session) -> bool:
            query = select(Calendar.id).where(Calendar.owner_id == owner_id).limit(1)
            return session.exec(query).first() is not None

        return await in_session(self._engine, work)

    async def get_calendar(self, owner_id: str, calendar_id: str) -> CalendarView:
        return self._calendar_view(await self._require_calendar(owner_id, calendar_id))

    async def update_calendar(
        self,
        owner_id: str,
        calendar_id: str,
        *,
        name: str | None = None,
        tone: str | None = None,
        caldav_url: str | None = None,
        caldav_username: str | None = None,
        caldav_password: str | None = None,
        read_only: bool | None = None,
    ) -> CalendarView:
        """Apply a partial update. A ``caldav_*`` value of ``""`` clears that field —
        the only way to unbind a calendar from its server through a partial patch, where
        ``None`` already means "leave alone"."""
        _ = await self._require_calendar(owner_id, calendar_id)

        def work(session: Session) -> CalendarView:
            calendar = session.get(Calendar, calendar_id)
            assert calendar is not None
            if name is not None:
                if not name.strip():
                    raise ValueError("calendar name must not be empty")
                calendar.name_enc = self._vault.encrypt_str(name.strip())
            if tone is not None:
                calendar.tone = tone
            if caldav_url is not None:
                calendar.caldav_url = caldav_url or None
            if caldav_username is not None:
                calendar.caldav_username_enc = self._seal(caldav_username or None)
            if caldav_password is not None:
                calendar.caldav_password_enc = self._seal(caldav_password or None)
            if read_only is not None:
                calendar.read_only = read_only
            calendar.updated_at = datetime.now(UTC)
            session.add(calendar)
            session.flush()
            return self._calendar_view(calendar)

        return await in_session(self._engine, work)

    async def delete_calendar(self, owner_id: str, calendar_id: str) -> None:
        """Delete a calendar and every event on it (the FK cascades)."""
        _ = await self._require_calendar(owner_id, calendar_id)

        def work(session: Session) -> None:
            for event in session.exec(
                select(CalendarEvent).where(CalendarEvent.calendar_id == calendar_id)
            ).all():
                session.delete(event)
            calendar = session.get(Calendar, calendar_id)
            if calendar is not None:
                session.delete(calendar)

        await in_session(self._engine, work)

    async def caldav_credentials(
        self, owner_id: str, calendar_id: str
    ) -> tuple[str, str | None, str | None]:
        """The calendar's server URL and decrypted credentials. Raises
        :class:`ValueError` when the calendar isn't bound to a server at all — the sync
        capability's precondition, checked here so it needn't reach into the row itself."""
        calendar = await self._require_calendar(owner_id, calendar_id)
        if not calendar.caldav_url:
            raise ValueError("calendar is not bound to a remote server")
        return (
            calendar.caldav_url,
            self._open(calendar.caldav_username_enc),
            self._open(calendar.caldav_password_enc),
        )

    async def mark_synced(self, owner_id: str, calendar_id: str) -> None:
        """Stamp the calendar as synced *now* — called by the sync capability once a run
        settles, so the surface can show when the remote was last reconciled."""

        def work(session: Session) -> None:
            calendar = session.get(Calendar, calendar_id)
            if calendar is not None and calendar.owner_id == owner_id:
                calendar.last_synced_at = datetime.now(UTC)
                session.add(calendar)

        await in_session(self._engine, work)

    # --- events -----------------------------------------------------------

    async def create_event(
        self,
        owner_id: str,
        calendar_id: str,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        all_day: bool = False,
        description: str | None = None,
        location: str | None = None,
        rrule: str | None = None,
        uid: str | None = None,
        remote_href: str | None = None,
        remote_etag: str | None = None,
    ) -> EventView:
        _ = await self._require_calendar(owner_id, calendar_id)
        start, end, zone_name = _normalize_span(starts_at, ends_at, timezone, all_day)
        rule = _validated_rrule(rrule, start, end, zone_name, all_day)
        if not title.strip():
            raise ValueError("event title must not be empty")

        event = CalendarEvent(
            owner_id=owner_id,
            calendar_id=calendar_id,
            uid=uid or new_uid(),
            title_enc=self._vault.encrypt_str(title.strip()),
            description_enc=self._seal(description),
            location_enc=self._seal(location),
            starts_at=start,
            ends_at=end,
            timezone=zone_name,
            all_day=all_day,
            rrule=rule,
            exdates=[],
            remote_href=remote_href,
            remote_etag=remote_etag,
        )

        def work(session: Session) -> EventView:
            session.add(event)
            session.flush()
            return self._event_view(event)

        return await in_session(self._engine, work)

    async def update_event(
        self,
        owner_id: str,
        event_id: str,
        *,
        calendar_id: str | None = None,
        title: str | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        timezone: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        location: str | None = None,
        rrule: str | None = None,
        clear_rrule: bool = False,
        remote_href: str | None = None,
        remote_etag: str | None = None,
    ) -> EventView:
        """Apply a partial edit to the whole event (the series, when it recurs).

        Timing fields are re-normalized **together** — moving an event's start without its
        end, or flipping ``all_day``, changes what the other fields mean, so the effective
        span is recomputed from the merged values rather than patched field by field.
        """
        existing = await self._require_event(owner_id, event_id)
        if calendar_id is not None:
            _ = await self._require_calendar(owner_id, calendar_id)
        if title is not None and not title.strip():
            raise ValueError("event title must not be empty")

        merged_all_day = existing.all_day if all_day is None else all_day
        merged_zone = timezone if timezone is not None else existing.timezone
        merged_start = starts_at if starts_at is not None else as_utc(existing.starts_at)
        merged_end = ends_at if ends_at is not None else as_utc(existing.ends_at)
        start, end, zone_name = _normalize_span(
            merged_start, merged_end, merged_zone, merged_all_day
        )
        merged_rule = None if clear_rrule else (rrule if rrule is not None else existing.rrule)
        rule = _validated_rrule(merged_rule, start, end, zone_name, merged_all_day)

        def work(session: Session) -> EventView:
            event = session.get(CalendarEvent, event_id)
            assert event is not None
            if calendar_id is not None:
                event.calendar_id = calendar_id
            if title is not None:
                event.title_enc = self._vault.encrypt_str(title.strip())
            if description is not None:
                event.description_enc = self._seal(description or None)
            if location is not None:
                event.location_enc = self._seal(location or None)
            if remote_href is not None:
                event.remote_href = remote_href
            if remote_etag is not None:
                event.remote_etag = remote_etag
            event.starts_at = start
            event.ends_at = end
            event.timezone = zone_name
            event.all_day = merged_all_day
            # A rule change invalidates the cancellations taken against the old rule —
            # their instants no longer land on any occurrence, so keeping them would
            # silently suppress unrelated dates later.
            if rule != event.rrule:
                event.exdates = []
            event.rrule = rule
            event.updated_at = datetime.now(UTC)
            session.add(event)
            session.flush()
            return self._event_view(event)

        return await in_session(self._engine, work)

    async def delete_event(self, owner_id: str, event_id: str) -> None:
        """Delete the event outright — the whole series when it recurs."""
        _ = await self._require_event(owner_id, event_id)

        def work(session: Session) -> None:
            event = session.get(CalendarEvent, event_id)
            if event is not None:
                session.delete(event)

        await in_session(self._engine, work)

    async def cancel_occurrence(
        self, owner_id: str, event_id: str, occurrence_start: datetime
    ) -> EventView:
        """Cancel one instance of a series (RFC 5545 ``EXDATE``) — the rule stays intact,
        so "delete this one" never quietly ends the series."""
        event = await self._require_event(owner_id, event_id)
        if not event.rrule:
            raise ValueError("event does not recur; delete it instead")
        stamp = as_utc(occurrence_start).isoformat()

        def work(session: Session) -> EventView:
            row = session.get(CalendarEvent, event_id)
            assert row is not None
            if stamp not in row.exdates:
                # Reassigned, not appended in place — SQLAlchemy only tracks a JSON
                # column's mutation when the attribute itself is set.
                row.exdates = [*row.exdates, stamp]
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._event_view(row)

        return await in_session(self._engine, work)

    async def get_event(self, owner_id: str, event_id: str) -> EventView:
        return self._event_view(await self._require_event(owner_id, event_id))

    async def find_by_uid(self, owner_id: str, calendar_id: str, uid: str) -> EventView | None:
        """The event with this iCalendar ``UID`` on this calendar, if any — how ICS import
        and CalDAV sync decide *update* vs *insert*."""

        def work(session: Session) -> EventView | None:
            row = session.exec(
                select(CalendarEvent).where(
                    CalendarEvent.owner_id == owner_id,
                    CalendarEvent.calendar_id == calendar_id,
                    CalendarEvent.uid == uid,
                )
            ).first()
            return self._event_view(row) if row is not None else None

        return await in_session(self._engine, work)

    async def list_events(self, owner_id: str, calendar_id: str | None = None) -> list[EventView]:
        """The stored events (rules, not occurrences), oldest first. The ICS export and
        the sync push read this; a calendar *view* wants `occurrences` instead."""

        def work(session: Session) -> list[EventView]:
            query = select(CalendarEvent).where(CalendarEvent.owner_id == owner_id)
            if calendar_id is not None:
                query = query.where(CalendarEvent.calendar_id == calendar_id)
            rows = session.exec(query.order_by(col(CalendarEvent.starts_at))).all()
            return [self._event_view(row) for row in rows]

        return await in_session(self._engine, work)

    # --- the expanded read ------------------------------------------------

    async def occurrences(
        self,
        owner_id: str,
        window_start: datetime,
        window_end: datetime,
        *,
        calendar_ids: list[str] | None = None,
    ) -> list[OccurrenceView]:
        """Everything on the calendar between two instants, recurrences expanded.

        The DB narrows first on the clear timestamp columns: a non-recurring event has to
        overlap the window outright, while a **recurring** one only has to have *started*
        before the window ends — its own end is the first occurrence's, which says nothing
        about where the series reaches. Those survivors are then expanded in Python.
        """
        lo = as_utc(window_start)
        hi = as_utc(window_end)

        def work(session: Session) -> list[CalendarEvent]:
            query = select(CalendarEvent).where(
                CalendarEvent.owner_id == owner_id,
                CalendarEvent.starts_at <= hi,
                or_(
                    col(CalendarEvent.rrule).is_not(None),
                    col(CalendarEvent.ends_at) >= lo,
                ),
            )
            if calendar_ids:
                query = query.where(col(CalendarEvent.calendar_id).in_(calendar_ids))
            return list(session.exec(query).all())

        rows = await in_session(self._engine, work)

        expanded: list[OccurrenceView] = []
        for row in rows:
            view = self._event_view(row)
            for start, end in expand(
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                timezone=row.timezone,
                all_day=row.all_day,
                rrule=row.rrule,
                exdates=row.exdates,
                window_start=lo,
                window_end=hi,
            ):
                expanded.append(_occurrence(view, start, end))
        expanded.sort(key=lambda occurrence: (occurrence.starts_at, occurrence.occurrence_id))
        return expanded

    async def upcoming(
        self, owner_id: str, *, after: datetime | None = None, limit: int = 10
    ) -> list[OccurrenceView]:
        """The next ``limit`` occurrences from ``after`` (default: now) — the read behind
        "what's on today" and the agent's agenda tool. Each event contributes only its own
        next occurrence before the merge, so one dense daily series can't crowd out
        everything else."""
        moment = as_utc(after) if after is not None else datetime.now(UTC)
        events = await self.list_events(owner_id)
        candidates: list[OccurrenceView] = []
        for view in events:
            found = next_occurrence(
                starts_at=view.starts_at,
                ends_at=view.ends_at,
                timezone=view.timezone,
                all_day=view.all_day,
                rrule=view.rrule,
                exdates=view.exdates,
                after=moment,
                horizon_days=_UPCOMING_HORIZON_DAYS,
            )
            if found is not None:
                candidates.append(_occurrence(view, *found))
        candidates.sort(key=lambda occurrence: (occurrence.starts_at, occurrence.occurrence_id))
        return candidates[:limit]

    # --- internals --------------------------------------------------------

    async def _require_calendar(self, owner_id: str, calendar_id: str) -> Calendar:
        def work(session: Session) -> Calendar | None:
            row = session.get(Calendar, calendar_id)
            return row if row is not None and row.owner_id == owner_id else None

        row = await in_session(self._engine, work)
        if row is None:
            raise NotFoundError(f"calendar {calendar_id!r} not found")
        return row

    async def _require_event(self, owner_id: str, event_id: str) -> CalendarEvent:
        def work(session: Session) -> CalendarEvent | None:
            row = session.get(CalendarEvent, event_id)
            return row if row is not None and row.owner_id == owner_id else None

        row = await in_session(self._engine, work)
        if row is None:
            raise NotFoundError(f"event {event_id!r} not found")
        return row

    def _seal(self, value: str | None) -> str | None:
        return self._vault.encrypt_str(value) if value else None

    def _open(self, token: str | None) -> str | None:
        return self._vault.decrypt_str(token) if token else None

    def _calendar_view(self, row: Calendar) -> CalendarView:
        return CalendarView(
            id=row.id,
            name=self._vault.decrypt_str(row.name_enc),
            tone=row.tone,
            synced=row.caldav_url is not None,
            sync_url=row.caldav_url,
            read_only=row.read_only,
            last_synced_at=row.last_synced_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _event_view(self, row: CalendarEvent) -> EventView:
        return EventView(
            id=row.id,
            calendar_id=row.calendar_id,
            uid=row.uid,
            title=self._vault.decrypt_str(row.title_enc),
            description=self._open(row.description_enc),
            location=self._open(row.location_enc),
            starts_at=as_utc(row.starts_at),
            ends_at=as_utc(row.ends_at),
            timezone=row.timezone,
            all_day=row.all_day,
            rrule=row.rrule,
            exdates=list(row.exdates),
            created_at=row.created_at,
            updated_at=row.updated_at,
            remote_href=row.remote_href,
            remote_etag=row.remote_etag,
        )


# --- normalization (pure, shared by create/update and the importers) ---------


def _normalize_span(
    starts_at: datetime,
    ends_at: datetime | None,
    timezone: str,
    all_day: bool,
) -> tuple[datetime, datetime, str]:
    """The stored ``(start, end, zone)`` for a submitted span.

    All-day is where the care goes: the dates are floored to **UTC** midnight with an
    exclusive end (the RFC 5545 ``DATE`` convention), never localized, so the day an event
    lands on is the same day for every reader. A timed event keeps its instants and only has
    its zone validated. A missing or inverted end becomes a one-hour (or one-day) span
    rather than an error — the operator meant an event, not a paradox.
    """
    zone_name = UTC_TIMEZONE if all_day else (timezone or DEFAULT_TIMEZONE)
    _ = parse_zone(zone_name)  # validate — raises ValueError on an unknown IANA name
    start = as_utc(starts_at)
    end = as_utc(ends_at) if ends_at is not None else None

    if all_day:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = (
            end.replace(hour=0, minute=0, second=0, microsecond=0)
            if end is not None
            else start + timedelta(days=1)
        )
        if end <= start:
            end = start + timedelta(days=1)
        return start, end, zone_name

    if end is None or end < start:
        end = start + timedelta(hours=1)
    return start, end, zone_name


def _validated_rrule(
    rrule: str | None,
    start: datetime,
    end: datetime,
    timezone: str,
    all_day: bool,
) -> str | None:
    """Normalize and parse-check a recurrence rule at **write** time. A rule that can't
    expand is rejected where the operator can still fix it, rather than silently producing
    an empty calendar on every later read."""
    if not rrule or not rrule.strip():
        return None
    rule = canonical_rrule(rrule)
    # Expanding over a zero-width window exercises the parser without walking the series.
    _ = expand(
        starts_at=start,
        ends_at=end,
        timezone=timezone,
        all_day=all_day,
        rrule=rule,
        window_start=start,
        window_end=start,
    )
    return rule


def _occurrence(view: EventView, start: datetime, end: datetime) -> OccurrenceView:
    return OccurrenceView(
        occurrence_id=f"{view.id}@{start.isoformat()}",
        event_id=view.id,
        calendar_id=view.calendar_id,
        title=view.title,
        description=view.description,
        location=view.location,
        starts_at=start,
        ends_at=end,
        timezone=view.timezone,
        all_day=view.all_day,
        recurring=view.rrule is not None,
        rrule=view.rrule,
    )
