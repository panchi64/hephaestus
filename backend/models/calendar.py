"""Calendar schema (`CAL-1..3`) — calendars and their events.

Two entities. A **`Calendar`** is a container the operator owns; it may be purely local
or bound to a remote CalDAV collection (`CAL-2`), in which case it carries that server's
URL plus sealed credentials. A **`CalendarEvent`** is one entry on it.

At-rest posture mirrors the rest of the app: the operator's own words — `title`,
`description`, `location`, the calendar's `name`, and the CalDAV username/password — are
sealed under the vault; **structural** fields stay in the clear because the DB has to
order and range-query them (`starts_at`/`ends_at`), match them on sync (`uid`,
`remote_href`), and expand them without decrypting (`rrule`, `timezone`, `all_day`).

## Recurrence is a rule, never rows

A recurring event is **one** row carrying its RFC 5545 ``RRULE`` string; occurrences are
expanded at read time (`services/calendar/recurrence.py`). Materializing occurrences
would make an open-ended rule unbounded and turn every edit into a rewrite of the series.
Per-occurrence deletions ride along as ``exdates`` (an RFC 5545 ``EXDATE`` set).

## Time zones are stored beside the instants, not folded into them

``starts_at``/``ends_at`` are UTC instants; ``timezone`` is the event's IANA zone. Both
are needed: the instant orders the event globally, and the zone is what a recurrence must
be expanded in, so a daily 09:00 meeting stays at 09:00 local **across a DST boundary**
rather than sliding an hour. An **all-day** event (``all_day``) is a *date* range, not an
instant range — it is stored as UTC midnights with an exclusive end (the RFC 5545
``DTEND`` convention for ``DATE`` values) and must never be zone-converted, so the 9th
is the 9th in Madrid and in Los Angeles alike.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, Column, ForeignKey, String
from sqlmodel import Field, SQLModel

from core.timezone import local_zone_key
from models._fields import new_id, utcnow


def _default_timezone() -> str:
    """The zone an event whose creator named none is written in — the operator's own,
    where the host will say what that is.

    This used to be UTC unconditionally, on the reasoning that a host-derived default makes
    the same payload mean different things on two machines. That is the right rule for a
    multi-tenant service and the wrong one here: there is one operator, on one machine,
    and the events they create are for their own calendar. UTC meant "3pm" from an agent
    that had just been told the local time landed in the afternoon somewhere else.

    Portability is kept where it actually lives — the key is read from the host in a
    POSIX-portable way (:mod:`core.timezone`) and *validated* against the tz database here,
    so a `TZ` holding a POSIX rule string, a system with no zoneinfo link, or a bare
    offset falls back to UTC rather than reaching a caller that will try to construct a
    `ZoneInfo` from it. Resolved once at import: the host's zone does not change under a
    running process, and this is a column default.
    """
    key = local_zone_key()
    try:
        ZoneInfo(key)
    except KeyError, ValueError, OSError:
        return "UTC"
    return key


#: The default IANA zone for an event whose creator named none.
DEFAULT_TIMEZONE = _default_timezone()

#: The zone for a timestamp that genuinely *is* UTC — an all-day date (RFC 5545 dates are
#: days, not instants, so localizing one would put the same event on two different days for
#: two readers) and an imported event whose file named no zone we could read. Deliberately
#: **not** :data:`DEFAULT_TIMEZONE`: the two were the same string while the default was UTC,
#: and following it now would silently relabel data whose zone was never in question.
UTC_TIMEZONE = "UTC"


def new_uid() -> str:
    """A fresh iCalendar ``UID``. Globally unique and stable for the life of the event —
    it is the identity ICS export/import and CalDAV sync all match on, which is why it is
    minted here rather than derived from the row id at export time."""
    return f"{uuid.uuid4().hex}@odysseus"


class Calendar(SQLModel, table=True):
    __tablename__ = "calendars"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)

    # AEAD ciphertext — the operator's own label for this calendar.
    name_enc: str
    # Presentation accent (`nominal`/`info`/`warn`/`alert`) — a display hint, not
    # content, so it stays in the clear like a document's `doc_type`.
    tone: str = Field(default="nominal")

    # --- remote sync (`CAL-2`) ---
    # The CalDAV collection URL. Structural and SSRF-checked on every use, so it is
    # stored in the clear; the credentials that reach it are not.
    caldav_url: str | None = Field(default=None)
    caldav_username_enc: str | None = Field(default=None)
    caldav_password_enc: str | None = Field(default=None)
    # A remote calendar the server marks read-only: sync pulls, never pushes.
    read_only: bool = Field(default=False)
    last_synced_at: datetime | None = Field(default=None)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CalendarEvent(SQLModel, table=True):
    __tablename__ = "calendar_events"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Cascades: an event has no meaning without its calendar (the same posture
    # `TaskRun` takes toward its task).
    calendar_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("calendars.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    # The iCalendar identity. In the clear and indexed — ICS import and CalDAV sync
    # both match an incoming entry to an existing row by it.
    uid: str = Field(default_factory=new_uid, index=True)

    # AEAD ciphertext — the operator's own content.
    title_enc: str
    description_enc: str | None = Field(default=None)
    location_enc: str | None = Field(default=None)

    # Structural, in the clear: the DB ranges over these to answer "what is on this
    # week", and the recurrence expander reads them without touching the vault.
    starts_at: datetime = Field(index=True)
    ends_at: datetime = Field(index=True)
    timezone: str = Field(default=DEFAULT_TIMEZONE)
    all_day: bool = Field(default=False)

    # The RFC 5545 recurrence rule (e.g. `FREQ=WEEKLY;BYDAY=MO,WE`), without the
    # `RRULE:` prefix. Null ⇒ a single occurrence.
    rrule: str | None = Field(default=None)
    # Occurrences the operator deleted out of a series — RFC 5545 `EXDATE`, held as
    # ISO-8601 UTC instants (dates at UTC midnight for an all-day series).
    exdates: list[str] = Field(sa_column=Column(JSON, nullable=False, default=list))

    # --- remote sync bookkeeping (`CAL-2`) ---
    # Where this event lives on the CalDAV server, and the server's version stamp for
    # it. Both null for a purely local event.
    remote_href: str | None = Field(default=None)
    remote_etag: str | None = Field(default=None)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
