"""Recurrence expansion — one stored rule, many occurrences, computed at read time.

A recurring event is a single row carrying an RFC 5545 ``RRULE`` (`models/calendar.py`).
Nothing is ever materialized: a caller asks "what falls inside this window" and this module
runs ``dateutil.rrule`` over the rule, filters the operator's ``EXDATE`` cancellations, and
returns UTC instants. That keeps an open-ended rule finite (the window bounds it), keeps an
edit to a series a single-row update, and keeps the DB free of thousands of rows nobody asked
for.

## Why the expansion runs in the event's own time zone

Expanding in UTC is the classic calendar bug: a daily 09:00 Madrid standup expanded as
"08:00 UTC + 24h" becomes 10:00 local the moment Spain moves to summer time. So a timed
event is expanded against a ``dtstart`` **localized to its own IANA zone**, and
``dateutil`` advances the *wall clock* (it re-attaches the same ``tzinfo`` to each generated
datetime), so 09:00 stays 09:00 and only the UTC instant shifts across the DST boundary.
Each occurrence is converted back to UTC exactly once, at the end.

An **all-day** event is the mirror image: it is a *date* range, so it is expanded in UTC and
never localized at all. Localizing it is what makes "the 9th" become the 8th at 23:00 for a
viewer one zone west — the shift `CAL-1` calls out. Its instants are UTC midnights and stay
that way, whoever reads them.

``UNTIL`` is normalized before the rule is parsed: ``dateutil`` refuses to mix a floating or
date-only ``UNTIL`` with an aware ``dtstart``, and both forms are legal in a file the
operator imported, so they are rewritten into the UTC form the parser accepts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from icalendar.prop import vRecur

from core.serde import as_utc

# The ceiling on how many occurrences one expansion may return. A window is the real
# bound; this is the backstop against a pathological rule (``FREQ=SECONDLY``) or an
# absurd window turning a read into an unbounded loop.
MAX_OCCURRENCES = 1000

_UNTIL_RE = re.compile(r"(?i)(?<![A-Z])UNTIL=([0-9TZ]+)")


def parse_zone(name: str | None) -> ZoneInfo:
    """The event's IANA zone. Raises :class:`ValueError` on an unknown name so the route
    can answer 422 rather than silently pretending the event is in UTC — a wrong zone is a
    wrong time, which is worse than a rejected write."""
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}") from exc


def canonical_rrule(rule: str) -> str:
    """The rule in RFC 5545's own canonical part order, via the same serializer the ICS
    layer writes with. Stored rules are canonicalized on write so an export→import
    round-trip comes back **byte-identical** — otherwise ``FREQ=WEEKLY;BYDAY=MO;COUNT=6``
    returns as ``FREQ=WEEKLY;COUNT=6;BYDAY=MO``, semantically the same rule but a
    gratuitous difference in every comparison and diff."""
    text = rule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :]
    try:
        return vRecur(vRecur.from_ical(text)).to_ical().decode()
    except ValueError, TypeError:
        # Canonicalization is cosmetic; a rule this serializer won't take is left as
        # written and rejected (or accepted) by the parse check that follows.
        return text


def normalize_rrule(rule: str, zone: ZoneInfo, *, all_day: bool) -> str:
    """Rewrite an ``UNTIL`` into the UTC form ``dateutil`` accepts alongside an aware
    ``dtstart``. A date-only ``UNTIL=20260630`` is read as midnight in the expansion zone;
    a floating ``UNTIL=20260630T235900`` likewise. An already-UTC value is left alone."""
    text = rule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :]

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        if raw.endswith(("Z", "z")):
            return match.group(0)
        base = ZoneInfo("UTC") if all_day else zone
        if len(raw) == 8:
            moment = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=base)
        else:
            moment = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=base)
        return f"UNTIL={moment.astimezone(UTC):%Y%m%dT%H%M%SZ}"

    return _UNTIL_RE.sub(replace, text)


def expand(
    *,
    starts_at: datetime,
    ends_at: datetime,
    timezone: str,
    all_day: bool,
    rrule: str | None,
    exdates: Sequence[str] = (),
    window_start: datetime,
    window_end: datetime,
    limit: int = MAX_OCCURRENCES,
) -> list[tuple[datetime, datetime]]:
    """The occurrences of one event that overlap ``[window_start, window_end)``, as UTC
    ``(start, end)`` pairs in chronological order.

    Overlap, not containment: an event that began before the window but is still running
    inside it belongs on the window's view of the calendar. ``exdates`` are the RFC 5545
    cancellations, matched against the occurrence's UTC start.
    """
    start = as_utc(starts_at)
    end = as_utc(ends_at)
    lo = as_utc(window_start)
    hi = as_utc(window_end)
    # A zero/negative span would make every overlap test false; treat it as a point in
    # time with no duration rather than dropping the event from the calendar entirely.
    duration = max(end - start, timedelta(0))

    if not rrule:
        return [(start, end)] if start < hi and end >= lo else []

    # All-day series live on the calendar grid, so they are expanded in UTC and never
    # localized; a timed series is expanded on its own wall clock (see the module docstring).
    zone = ZoneInfo("UTC") if all_day else parse_zone(timezone)
    dtstart = start.astimezone(zone)
    try:
        rule = rrulestr(normalize_rrule(rrule, zone, all_day=all_day), dtstart=dtstart)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid recurrence rule {rrule!r}: {exc}") from exc

    cancelled = _cancelled_instants(exdates)
    # `between` is start-keyed, so widen the left edge by the duration — an occurrence that
    # started before the window can still be running inside it.
    occurrences: list[tuple[datetime, datetime]] = []
    for moment in rule.between(lo - duration, hi, inc=True):
        occ_start = moment.astimezone(UTC)
        if occ_start in cancelled:
            continue
        occ_end = occ_start + duration
        if occ_start < hi and occ_end >= lo:
            occurrences.append((occ_start, occ_end))
        if len(occurrences) >= limit:
            break
    return occurrences


def next_occurrence(
    *,
    starts_at: datetime,
    ends_at: datetime,
    timezone: str,
    all_day: bool,
    rrule: str | None,
    exdates: Sequence[str] = (),
    after: datetime,
    horizon_days: int = 366,
) -> tuple[datetime, datetime] | None:
    """The first occurrence starting at or after ``after``, or ``None`` within
    ``horizon_days``. A bounded window rather than an open-ended walk, so a rule whose next
    hit is decades out (or never) can't spin."""
    moment = as_utc(after)
    found = expand(
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=timezone,
        all_day=all_day,
        rrule=rrule,
        exdates=exdates,
        window_start=moment,
        window_end=moment + timedelta(days=horizon_days),
    )
    return next(((s, e) for s, e in found if s >= moment), None)


def _cancelled_instants(exdates: Iterable[str]) -> set[datetime]:
    """The ``EXDATE`` set as UTC instants. An unparseable entry is skipped rather than
    failing the read — a corrupt cancellation should cost the operator one stray
    occurrence, not their whole calendar."""
    cancelled: set[datetime] = set()
    for raw in exdates:
        try:
            cancelled.add(as_utc(datetime.fromisoformat(raw)))
        except TypeError, ValueError:
            continue
    return cancelled
