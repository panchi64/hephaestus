"""ICS import/export (`CAL-2`) — round-trip fidelity and merge behaviour."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.db import init_db, make_engine
from core.vault import Vault
from services.calendar import CalendarService
from services.calendar.ics import export_calendar, export_ics, import_into, parse_ics

OWNER = "operator"
MADRID = ZoneInfo("Europe/Madrid")


async def _service() -> CalendarService:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return CalendarService(engine, vault)


async def test_round_trip_is_lossless_for_every_field_we_model():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    original = await service.create_event(
        OWNER,
        calendar.id,
        title="Sprint planning",
        starts_at=datetime(2026, 6, 8, 14, 0, tzinfo=MADRID).astimezone(UTC),
        ends_at=datetime(2026, 6, 8, 15, 30, tzinfo=MADRID).astimezone(UTC),
        timezone="Europe/Madrid",
        description="Review the backlog.",
        location="Zoom",
        rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=6",
    )
    await service.cancel_occurrence(
        OWNER, original.id, datetime(2026, 6, 15, 14, 0, tzinfo=MADRID).astimezone(UTC)
    )
    original = await service.get_event(OWNER, original.id)

    document = await export_calendar(service, OWNER, calendar.id)

    other = await _service()
    target = await other.create_calendar(OWNER, "Imported")
    result = await import_into(other, OWNER, target.id, document)
    assert (result.created, result.updated, result.skipped) == (1, 0, 0)

    imported = (await other.list_events(OWNER, target.id))[0]
    assert imported.uid == original.uid
    assert imported.title == original.title
    assert imported.description == original.description
    assert imported.location == original.location
    assert imported.starts_at == original.starts_at
    assert imported.ends_at == original.ends_at
    assert imported.timezone == "Europe/Madrid"
    assert imported.all_day is False
    assert imported.rrule == original.rrule
    assert imported.exdates == original.exdates


async def test_all_day_round_trips_as_dates_and_keeps_its_day():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Personal")
    await service.create_event(
        OWNER,
        calendar.id,
        title="On-call",
        starts_at=datetime(2026, 6, 9, tzinfo=UTC),
        ends_at=datetime(2026, 6, 11, tzinfo=UTC),
        all_day=True,
    )

    document = await export_calendar(service, OWNER, calendar.id)
    text = document.decode()
    assert "DTSTART;VALUE=DATE:20260609" in text
    assert "DTEND;VALUE=DATE:20260611" in text

    other = await _service()
    target = await other.create_calendar(OWNER, "Imported")
    await import_into(other, OWNER, target.id, document)
    imported = (await other.list_events(OWNER, target.id))[0]
    assert imported.all_day is True
    assert imported.starts_at == datetime(2026, 6, 9, tzinfo=UTC)
    assert imported.ends_at == datetime(2026, 6, 11, tzinfo=UTC)


async def test_timed_events_are_written_with_their_tzid_not_flattened_to_utc():
    """The instant alone would round-trip; the zone would not — and the zone is what a
    recurrence has to be expanded in."""
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    event = await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 6, 8, 9, 0, tzinfo=MADRID).astimezone(UTC),
        timezone="Europe/Madrid",
    )
    text = export_ics([event]).decode()
    assert "DTSTART;TZID=Europe/Madrid:20260608T090000" in text


async def test_reimporting_the_same_file_updates_instead_of_duplicating():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 6, 8, 9, tzinfo=UTC),
    )
    document = await export_calendar(service, OWNER, calendar.id)

    first = await import_into(service, OWNER, calendar.id, document)
    second = await import_into(service, OWNER, calendar.id, document)
    assert first.updated == 1 and second.updated == 1
    assert len(await service.list_events(OWNER, calendar.id)) == 1


async def test_an_import_updates_a_matching_uid_in_place():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    event = await service.create_event(
        OWNER, calendar.id, title="Old title", starts_at=datetime(2026, 6, 8, 9, tzinfo=UTC)
    )

    document = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:{event.uid}
SUMMARY:New title
DTSTART:20260608T110000Z
DTEND:20260608T120000Z
END:VEVENT
END:VCALENDAR
"""
    result = await import_into(service, OWNER, calendar.id, document)
    assert (result.created, result.updated) == (0, 1)

    stored = await service.get_event(OWNER, event.id)
    assert stored.title == "New title"
    assert stored.starts_at == datetime(2026, 6, 8, 11, tzinfo=UTC)


async def test_duration_stands_in_for_a_missing_dtend():
    parsed = parse_ics(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:duration@test
SUMMARY:Ninety minutes
DTSTART:20260608T090000Z
DURATION:PT1H30M
END:VEVENT
END:VCALENDAR
"""
    )
    assert len(parsed) == 1
    assert parsed[0].ends_at - parsed[0].starts_at == timedelta(minutes=90)


async def test_a_floating_datetime_is_read_as_utc():
    parsed = parse_ics(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:floating@test
SUMMARY:No zone at all
DTSTART:20260608T090000
DTEND:20260608T100000
END:VEVENT
END:VCALENDAR
"""
    )
    assert parsed[0].starts_at == datetime(2026, 6, 8, 9, tzinfo=UTC)
    assert parsed[0].timezone == "UTC"


async def test_an_unknown_tzid_falls_back_to_utc_rather_than_losing_the_event():
    parsed = parse_ics(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:weird@test
SUMMARY:Proprietary zone
DTSTART;TZID=Customized Time Zone:20260608T090000
DTEND;TZID=Customized Time Zone:20260608T100000
END:VEVENT
END:VCALENDAR
"""
    )
    assert len(parsed) == 1
    assert parsed[0].timezone == "UTC"


async def test_a_malformed_event_is_skipped_not_fatal():
    """One bad entry in a file the operator didn't write must not cost them the rest."""
    parsed = parse_ics(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:no-start@test
SUMMARY:Missing DTSTART
END:VEVENT
BEGIN:VEVENT
UID:fine@test
SUMMARY:Perfectly fine
DTSTART:20260608T090000Z
DTEND:20260608T100000Z
END:VEVENT
END:VCALENDAR
"""
    )
    assert [event.uid for event in parsed] == ["fine@test"]


async def test_an_event_the_store_rejects_is_counted_as_skipped():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Work")
    result = await import_into(
        service,
        OWNER,
        calendar.id,
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:bad-rule@test
SUMMARY:Impossible rule
DTSTART:20260608T090000Z
DTEND:20260608T100000Z
RRULE:FREQ=NEVER
END:VEVENT
BEGIN:VEVENT
UID:good@test
SUMMARY:Fine
DTSTART:20260608T090000Z
DTEND:20260608T100000Z
END:VEVENT
END:VCALENDAR
""",
    )
    assert (result.created, result.skipped) == (1, 1)
    assert [event.uid for event in await service.list_events(OWNER, calendar.id)] == ["good@test"]


async def test_an_unreadable_document_is_rejected():
    with pytest.raises(ValueError):
        parse_ics("this is not a calendar at all")


async def test_export_names_the_calendar():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Ops / On-call")
    document = (await export_calendar(service, OWNER, calendar.id)).decode()
    assert "X-WR-CALNAME:Ops / On-call" in document
    assert "PRODID:-//Hephaestus//Calendar//EN" in document
