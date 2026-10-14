"""Recurrence expansion and time-zone correctness (`CAL-1`).

The two cases this file exists for: a recurring event must hold its **local wall clock**
across a DST boundary (the instant moves, the meeting doesn't), and an **all-day** event
must land on the same calendar date for every reader, whatever zone they are in.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.db import init_db, make_engine
from core.exceptions import NotFoundError
from core.vault import Vault
from services.calendar import CalendarService
from services.calendar.recurrence import expand, next_occurrence, normalize_rrule

OWNER = "operator"
MADRID = ZoneInfo("Europe/Madrid")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")


async def _service() -> CalendarService:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return CalendarService(engine, vault)


# --- pure expansion -------------------------------------------------------


def test_daily_series_holds_local_time_across_a_dst_boundary():
    """Spain springs forward on 2026-03-29. A 09:00 Madrid standup must stay 09:00 local
    on both sides — which means its UTC instant shifts from 08:00 to 07:00."""
    start = datetime(2026, 3, 27, 9, 0, tzinfo=MADRID).astimezone(UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        timezone="Europe/Madrid",
        all_day=False,
        rrule="FREQ=DAILY",
        window_start=datetime(2026, 3, 27, tzinfo=UTC),
        window_end=datetime(2026, 4, 1, tzinfo=UTC),
    )

    local_hours = {occ.astimezone(MADRID).hour for occ, _ in occurrences}
    assert local_hours == {9}, "the meeting must not drift off 09:00 local"

    utc_by_date = {occ.astimezone(MADRID).date(): occ for occ, _ in occurrences}
    assert utc_by_date[datetime(2026, 3, 28).date()].hour == 8  # CET  (UTC+1)
    assert utc_by_date[datetime(2026, 3, 30).date()].hour == 7  # CEST (UTC+2)


def test_daily_series_holds_local_time_across_a_fall_back_boundary():
    """The same rule in the other direction: Spain falls back on 2026-10-25."""
    start = datetime(2026, 10, 23, 9, 0, tzinfo=MADRID).astimezone(UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        timezone="Europe/Madrid",
        all_day=False,
        rrule="FREQ=DAILY",
        window_start=datetime(2026, 10, 23, tzinfo=UTC),
        window_end=datetime(2026, 10, 28, tzinfo=UTC),
    )

    assert {occ.astimezone(MADRID).hour for occ, _ in occurrences} == {9}
    utc_by_date = {occ.astimezone(MADRID).date(): occ for occ, _ in occurrences}
    assert utc_by_date[datetime(2026, 10, 24).date()].hour == 7  # CEST
    assert utc_by_date[datetime(2026, 10, 26).date()].hour == 8  # CET


def test_expanding_in_utc_would_have_drifted():
    """The control for the two tests above: the same series declared in UTC keeps its UTC
    hour and therefore *does* slide an hour in Madrid — which is exactly the bug the
    zone-aware expansion avoids."""
    start = datetime(2026, 3, 27, 8, 0, tzinfo=UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        timezone="UTC",
        all_day=False,
        rrule="FREQ=DAILY",
        window_start=datetime(2026, 3, 27, tzinfo=UTC),
        window_end=datetime(2026, 4, 1, tzinfo=UTC),
    )
    assert {occ.astimezone(MADRID).hour for occ, _ in occurrences} == {9, 10}


def test_all_day_event_does_not_shift_across_zones():
    """An all-day event on the 9th is the 9th everywhere — no localization is applied, so
    a viewer nine hours west still sees the 9th and not the 8th."""
    start = datetime(2026, 6, 9, tzinfo=UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(days=1),
        timezone="Europe/Madrid",
        all_day=True,
        rrule=None,
        window_start=datetime(2026, 6, 1, tzinfo=UTC),
        window_end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(occurrences) == 1
    occ_start, occ_end = occurrences[0]
    assert occ_start == datetime(2026, 6, 9, tzinfo=UTC)
    assert occ_end == datetime(2026, 6, 10, tzinfo=UTC)
    assert occ_start.date() == datetime(2026, 6, 9).date()


def test_all_day_weekly_series_keeps_its_dates_across_a_dst_boundary():
    """A weekly all-day marker spanning the US spring-forward keeps landing on the same
    weekday at UTC midnight — no hour creeps in to nudge it onto a neighbouring date."""
    start = datetime(2026, 3, 2, tzinfo=UTC)  # a Monday
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(days=1),
        timezone="America/Los_Angeles",
        all_day=True,
        rrule="FREQ=WEEKLY;BYDAY=MO",
        window_start=datetime(2026, 3, 1, tzinfo=UTC),
        window_end=datetime(2026, 4, 1, tzinfo=UTC),
    )
    starts = [occ for occ, _ in occurrences]
    assert starts == [
        datetime(2026, 3, 2, tzinfo=UTC),
        datetime(2026, 3, 9, tzinfo=UTC),
        datetime(2026, 3, 16, tzinfo=UTC),
        datetime(2026, 3, 23, tzinfo=UTC),
        datetime(2026, 3, 30, tzinfo=UTC),
    ]
    assert all(occ.time() == datetime(2026, 1, 1).time() for occ in starts)
    # …and reading them from a zone that changed offset mid-window still shows the 9th
    # as the 9th, not the 8th at 16:00.
    assert starts[1].date() == datetime(2026, 3, 9).date()


def test_window_includes_an_occurrence_that_started_before_it():
    """Overlap, not containment — a long event already running when the window opens is
    still on the calendar."""
    start = datetime(2026, 6, 8, 22, 0, tzinfo=UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(hours=6),
        timezone="UTC",
        all_day=False,
        rrule=None,
        window_start=datetime(2026, 6, 9, tzinfo=UTC),
        window_end=datetime(2026, 6, 10, tzinfo=UTC),
    )
    assert occurrences == [(start, start + timedelta(hours=6))]


def test_exdates_cancel_single_occurrences():
    start = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        timezone="UTC",
        all_day=False,
        rrule="FREQ=DAILY;COUNT=4",
        exdates=["2026-06-02T09:00:00+00:00", "not-a-date"],
        window_start=datetime(2026, 6, 1, tzinfo=UTC),
        window_end=datetime(2026, 6, 10, tzinfo=UTC),
    )
    assert [occ.day for occ, _ in occurrences] == [1, 3, 4]


def test_until_is_normalized_for_every_legal_form():
    """A file the operator imported may carry ``UNTIL`` as UTC, floating, or a bare date;
    all three have to expand rather than blowing up the parser."""
    madrid = ZoneInfo("Europe/Madrid")
    assert normalize_rrule("FREQ=DAILY;UNTIL=20260610T235900Z", madrid, all_day=False) == (
        "FREQ=DAILY;UNTIL=20260610T235900Z"
    )
    # Floating local 23:59 Madrid in June (UTC+2) is 21:59 UTC.
    assert normalize_rrule("FREQ=DAILY;UNTIL=20260610T235900", madrid, all_day=False) == (
        "FREQ=DAILY;UNTIL=20260610T215900Z"
    )
    # A date-only UNTIL on an all-day series is read in UTC, so it stays midnight.
    assert normalize_rrule("RRULE:FREQ=WEEKLY;UNTIL=20260610", madrid, all_day=True) == (
        "FREQ=WEEKLY;UNTIL=20260610T000000Z"
    )

    start = datetime(2026, 6, 1, 9, 0, tzinfo=madrid).astimezone(UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        timezone="Europe/Madrid",
        all_day=False,
        rrule="FREQ=DAILY;UNTIL=20260603",
        window_start=datetime(2026, 6, 1, tzinfo=UTC),
        window_end=datetime(2026, 6, 30, tzinfo=UTC),
    )
    assert len(occurrences) == 2


def test_expansion_is_capped():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    occurrences = expand(
        starts_at=start,
        ends_at=start + timedelta(minutes=1),
        timezone="UTC",
        all_day=False,
        rrule="FREQ=MINUTELY",
        window_start=start,
        window_end=start + timedelta(days=30),
        limit=25,
    )
    assert len(occurrences) == 25


def test_next_occurrence_skips_the_past_and_bounds_its_search():
    start = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    found = next_occurrence(
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        timezone="UTC",
        all_day=False,
        rrule="FREQ=WEEKLY",
        after=datetime(2026, 6, 10, tzinfo=UTC),
    )
    assert found is not None and found[0] == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)

    spent = next_occurrence(
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        timezone="UTC",
        all_day=False,
        rrule="FREQ=DAILY;COUNT=2",
        after=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert spent is None


def test_unknown_time_zone_is_rejected():
    with pytest.raises(ValueError):
        _ = expand(
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
            ends_at=datetime(2026, 6, 1, 1, tzinfo=UTC),
            timezone="Mars/Olympus_Mons",
            all_day=False,
            rrule="FREQ=DAILY",
            window_start=datetime(2026, 6, 1, tzinfo=UTC),
            window_end=datetime(2026, 6, 2, tzinfo=UTC),
        )


# --- the store ------------------------------------------------------------


async def test_crud_round_trip_and_sealed_content():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Personal", tone="info")
    assert calendar.name == "Personal" and calendar.synced is False

    event = await service.create_event(
        OWNER,
        calendar.id,
        title="Dentist",
        starts_at=datetime(2026, 6, 9, 10, 0, tzinfo=UTC),
        ends_at=datetime(2026, 6, 9, 11, 0, tzinfo=UTC),
        location="Clínica Pérez",
        description="Bring the referral",
    )
    assert (await service.get_event(OWNER, event.id)).location == "Clínica Pérez"

    edited = await service.update_event(OWNER, event.id, title="Dentist (moved)")
    assert edited.title == "Dentist (moved)"
    assert edited.starts_at == datetime(2026, 6, 9, 10, 0, tzinfo=UTC)

    await service.delete_event(OWNER, event.id)
    with pytest.raises(NotFoundError):
        _ = await service.get_event(OWNER, event.id)


async def test_content_is_encrypted_at_rest():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Private")
    _ = await service.create_event(
        OWNER,
        calendar.id,
        title="Divorce lawyer",
        starts_at=datetime(2026, 6, 9, 10, 0, tzinfo=UTC),
    )

    from sqlmodel import Session, select

    from models.calendar import CalendarEvent

    with Session(service.engine) as session:
        row = session.exec(select(CalendarEvent)).one()
    assert "Divorce" not in row.title_enc
    assert row.starts_at is not None  # structure stays queryable in the clear


async def test_another_owner_cannot_reach_the_event():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Personal")
    event = await service.create_event(
        OWNER, calendar.id, title="Standup", starts_at=datetime(2026, 6, 9, 9, tzinfo=UTC)
    )
    with pytest.raises(NotFoundError):
        _ = await service.get_event("someone-else", event.id)


async def test_occurrences_expand_a_series_inside_the_window():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    _ = await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 3, 27, 9, 0, tzinfo=MADRID).astimezone(UTC),
        ends_at=datetime(2026, 3, 27, 9, 15, tzinfo=MADRID).astimezone(UTC),
        timezone="Europe/Madrid",
        rrule="FREQ=DAILY",
    )

    found = await service.occurrences(
        OWNER, datetime(2026, 3, 27, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)
    )
    assert len(found) == 5
    assert {occ.starts_at.astimezone(MADRID).hour for occ in found} == {9}
    assert all(occ.recurring for occ in found)
    assert found == sorted(found, key=lambda occ: occ.starts_at)


async def test_cancelling_one_occurrence_leaves_the_series_intact():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    event = await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        ends_at=datetime(2026, 6, 1, 9, 15, tzinfo=UTC),
        rrule="FREQ=DAILY;COUNT=3",
    )
    _ = await service.cancel_occurrence(OWNER, event.id, datetime(2026, 6, 2, 9, 0, tzinfo=UTC))

    found = await service.occurrences(
        OWNER, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 10, tzinfo=UTC)
    )
    assert [occ.starts_at.day for occ in found] == [1, 3]
    assert (await service.get_event(OWNER, event.id)).rrule == "FREQ=DAILY;COUNT=3"


async def test_cancelling_a_non_recurring_event_is_rejected():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    event = await service.create_event(
        OWNER, calendar.id, title="One-off", starts_at=datetime(2026, 6, 1, 9, tzinfo=UTC)
    )
    with pytest.raises(ValueError):
        _ = await service.cancel_occurrence(OWNER, event.id, datetime(2026, 6, 1, 9, tzinfo=UTC))


async def test_all_day_span_is_floored_to_exclusive_utc_midnights():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Personal")
    event = await service.create_event(
        OWNER,
        calendar.id,
        title="On-call",
        starts_at=datetime(2026, 6, 9, 13, 45, tzinfo=UTC),
        all_day=True,
    )
    assert event.starts_at == datetime(2026, 6, 9, tzinfo=UTC)
    assert event.ends_at == datetime(2026, 6, 10, tzinfo=UTC)
    assert event.timezone == "UTC"


async def test_a_missing_end_becomes_a_one_hour_event():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Personal")
    event = await service.create_event(
        OWNER, calendar.id, title="Coffee", starts_at=datetime(2026, 6, 9, 10, tzinfo=UTC)
    )
    assert event.ends_at - event.starts_at == timedelta(hours=1)


async def test_an_unparseable_rule_is_rejected_at_write_time():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Personal")
    with pytest.raises(ValueError):
        _ = await service.create_event(
            OWNER,
            calendar.id,
            title="Nonsense",
            starts_at=datetime(2026, 6, 9, 10, tzinfo=UTC),
            rrule="FREQ=NEVER",
        )


async def test_rules_are_stored_canonically():
    """Canonical on write, so an ICS round-trip is byte-identical rather than merely
    equivalent — and a stored rule can be compared with `==`."""
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    event = await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
        rrule="RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6",
    )
    assert event.rrule == "FREQ=WEEKLY;COUNT=6;BYDAY=MO"


async def test_changing_the_rule_clears_stale_cancellations():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    event = await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
        rrule="FREQ=DAILY",
    )
    _ = await service.cancel_occurrence(OWNER, event.id, datetime(2026, 6, 2, 9, tzinfo=UTC))
    assert (await service.get_event(OWNER, event.id)).exdates

    updated = await service.update_event(OWNER, event.id, rrule="FREQ=WEEKLY")
    assert updated.exdates == []


async def test_deleting_a_calendar_takes_its_events_with_it():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Scratch")
    _ = await service.create_event(
        OWNER, calendar.id, title="Gone", starts_at=datetime(2026, 6, 1, 9, tzinfo=UTC)
    )
    await service.delete_calendar(OWNER, calendar.id)
    assert await service.list_events(OWNER) == []


async def test_upcoming_returns_one_next_hit_per_event_in_order():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    _ = await service.create_event(
        OWNER,
        calendar.id,
        title="Daily",
        starts_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
        rrule="FREQ=DAILY",
    )
    _ = await service.create_event(
        OWNER, calendar.id, title="One-off", starts_at=datetime(2026, 6, 1, 15, tzinfo=UTC)
    )

    found = await service.upcoming(OWNER, after=datetime(2026, 6, 1, 12, tzinfo=UTC), limit=5)
    assert [occ.title for occ in found] == ["One-off", "Daily"]
    assert found[1].starts_at == datetime(2026, 6, 2, 9, tzinfo=UTC)


async def test_caldav_credentials_are_sealed_and_readable_back():
    service = await _service()
    calendar = await service.create_calendar(
        OWNER,
        "Remote",
        caldav_url="https://dav.example.com/cal/",
        caldav_username="operator",
        caldav_password="hunter2",
    )
    url, username, password = await service.caldav_credentials(OWNER, calendar.id)
    assert (url, username, password) == ("https://dav.example.com/cal/", "operator", "hunter2")

    from sqlmodel import Session, select

    from models.calendar import Calendar

    with Session(service.engine) as session:
        row = session.exec(select(Calendar)).one()
    assert row.caldav_password_enc is not None and "hunter2" not in row.caldav_password_enc


async def test_credentials_for_a_local_calendar_are_rejected():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Local only")
    with pytest.raises(ValueError):
        _ = await service.caldav_credentials(OWNER, calendar.id)
