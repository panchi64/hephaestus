"""CalDAV sync (`CAL-2`) — reconcile behaviour, SSRF guarding, and off-the-loop I/O.

The `caldav` package is synchronous, so the remote is faked here through the same narrow
connector seam the real client plugs into: the reconcile logic is what's under test, not
someone else's HTTP stack.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import override

import pytest

from core.db import init_db, make_engine
from core.exceptions import SSRFError
from core.vault import Vault
from services.calendar import CalendarService
from services.calendar.caldav import CalDavSync, RemoteCalendar
from services.calendar.ics import export_ics

OWNER = "operator"
REMOTE_URL = "https://dav.example.com/calendars/operator/personal/"


async def _service() -> CalendarService:
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return CalendarService(engine, vault)


class FakeRemoteObject:
    def __init__(self, data: str, href: str, etag: str | None = "etag-1") -> None:
        self.data: str = data
        self.url: str = href
        self.etag: str | None = etag


class FakeRemoteCalendar(RemoteCalendar):
    """A stand-in CalDAV collection. Records the credentials it was opened with and the
    thread it ran on, so the tests can assert both without a server."""

    def __init__(self, objects: list[FakeRemoteObject]) -> None:
        self.objects: list[FakeRemoteObject] = objects
        self.opened_with: list[tuple[str, str | None, str | None]] = []
        self.threads: set[int] = set()

    def connector(self, url: str, username: str | None, password: str | None) -> FakeRemoteCalendar:
        self.opened_with.append((url, username, password))
        self.threads.add(threading.get_ident())
        return self

    @override
    def events(self) -> list[FakeRemoteObject]:
        return list(self.objects)

    @override
    def save_event(self, ical: str) -> FakeRemoteObject:
        saved = FakeRemoteObject(ical, f"{REMOTE_URL}{len(self.objects)}.ics", "etag-pushed")
        self.objects.append(saved)
        return saved


class RefusingRemoteCalendar(FakeRemoteCalendar):
    @override
    def save_event(self, ical: str) -> FakeRemoteObject:
        raise RuntimeError("the server said no")


def _remote_document(uid: str, summary: str, hour: int = 9) -> str:
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Remote//EN
BEGIN:VEVENT
UID:{uid}
SUMMARY:{summary}
DTSTART:20260608T{hour:02d}0000Z
DTEND:20260608T{hour + 1:02d}0000Z
END:VEVENT
END:VCALENDAR
"""


async def _bound_calendar(service: CalendarService, *, read_only: bool = False):
    return await service.create_calendar(
        OWNER,
        "Remote",
        caldav_url=REMOTE_URL,
        caldav_username="operator",
        caldav_password="hunter2",
        read_only=read_only,
    )


async def test_pull_creates_local_events(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar(
        [FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")]
    )
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.pulled_created == 1
    stored = await service.list_events(OWNER, calendar.id)
    assert [event.title for event in stored] == ["Kickoff"]
    assert stored[0].remote_href == f"{REMOTE_URL}a.ics"
    assert stored[0].remote_etag == "etag-1"


async def test_the_remote_is_opened_with_the_decrypted_credentials_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)

    _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert remote.opened_with[0] == (REMOTE_URL, "operator", "hunter2")
    # The `caldav` package blocks; every call must land on a worker thread, never the
    # event loop's own.
    assert remote.threads and threading.get_ident() not in remote.threads


async def test_a_second_sync_skips_objects_whose_etag_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar(
        [FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")]
    )
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    sync = CalDavSync(service, connector=remote.connector)

    _ = await sync.sync(OWNER, calendar.id)
    second = await sync.sync(OWNER, calendar.id)

    assert (second.pulled_created, second.pulled_updated) == (0, 0)


async def test_a_changed_etag_pulls_the_update_in(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service)
    obj = FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")
    remote = FakeRemoteCalendar([obj])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    sync = CalDavSync(service, connector=remote.connector)
    _ = await sync.sync(OWNER, calendar.id)

    obj.data = _remote_document("a@remote", "Kickoff (moved)", hour=14)
    obj.etag = "etag-2"
    result = await sync.sync(OWNER, calendar.id)

    assert result.pulled_updated == 1
    stored = (await service.list_events(OWNER, calendar.id))[0]
    assert stored.title == "Kickoff (moved)"
    assert stored.starts_at == datetime(2026, 6, 8, 14, tzinfo=UTC)


async def test_local_events_are_pushed_once_and_then_recognized(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    _ = await service.create_event(
        OWNER, calendar.id, title="Local only", starts_at=datetime(2026, 6, 8, 9, tzinfo=UTC)
    )
    sync = CalDavSync(service, connector=remote.connector)

    first = await sync.sync(OWNER, calendar.id)
    assert first.pushed == 1
    assert (await service.list_events(OWNER, calendar.id))[0].remote_href is not None

    second = await sync.sync(OWNER, calendar.id)
    assert second.pushed == 0
    assert len(remote.objects) == 1


async def test_a_read_only_calendar_never_pushes(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    remote = FakeRemoteCalendar([])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    _ = await service.create_event(
        OWNER, calendar.id, title="Local only", starts_at=datetime(2026, 6, 8, 9, tzinfo=UTC)
    )

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.pushed == 0
    assert remote.objects == []


async def test_an_event_the_server_dropped_is_removed_locally(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    obj = FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")
    remote = FakeRemoteCalendar([obj])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    sync = CalDavSync(service, connector=remote.connector)
    _ = await sync.sync(OWNER, calendar.id)

    remote.objects.clear()
    result = await sync.sync(OWNER, calendar.id)

    assert result.removed_locally == 1
    assert await service.list_events(OWNER, calendar.id) == []


async def test_a_truncated_listing_removes_nothing(monkeypatch: pytest.MonkeyPatch):
    """The data-loss case: the listing stops at the cap, so the UIDs it saw cover only the
    front of the collection. Every local event past the cut would look "gone from the
    server" and be deleted — permanently, on every sync, for any calendar bigger than the
    cap. A partial listing is not evidence of an upstream deletion."""
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    remote = FakeRemoteCalendar(
        [
            FakeRemoteObject(_remote_document(f"{uid}@remote", uid), f"{REMOTE_URL}{uid}.ics")
            for uid in ("a", "b", "c")
        ]
    )
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    sync = CalDavSync(service, connector=remote.connector)
    _ = await sync.sync(OWNER, calendar.id)
    assert len(await service.list_events(OWNER, calendar.id)) == 3

    # The cap, not the collection, is what shrinks — the server still has all three.
    monkeypatch.setattr("services.calendar.caldav.MAX_REMOTE_OBJECTS", 2)
    result = await sync.sync(OWNER, calendar.id)

    assert result.removed_locally == 0
    assert len(await service.list_events(OWNER, calendar.id)) == 3


async def test_a_purely_local_event_survives_a_sync_that_does_not_mention_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """Absence upstream says nothing about an event that was never pushed there."""
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    remote = FakeRemoteCalendar([])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    _ = await service.create_event(
        OWNER, calendar.id, title="Mine alone", starts_at=datetime(2026, 6, 8, 9, tzinfo=UTC)
    )

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.removed_locally == 0
    assert len(await service.list_events(OWNER, calendar.id)) == 1


async def test_a_refused_upload_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = RefusingRemoteCalendar([])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    _ = await service.create_event(
        OWNER, calendar.id, title="Unwanted", starts_at=datetime(2026, 6, 8, 9, tzinfo=UTC)
    )

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.pushed == 0
    assert len(await service.list_events(OWNER, calendar.id)) == 1


async def test_a_pushed_event_round_trips_through_the_ics_layer(monkeypatch: pytest.MonkeyPatch):
    """What goes up is what `ics.py` writes — so a pushed recurring event carries its rule
    and its zone, not a flattened instant."""
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    event = await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 6, 8, 7, tzinfo=UTC),
        timezone="Europe/Madrid",
        rrule="FREQ=DAILY",
    )

    _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    uploaded = remote.objects[0].data
    assert "RRULE:FREQ=DAILY" in uploaded
    assert "TZID=Europe/Madrid" in uploaded
    assert export_ics([event]).decode().splitlines()[0] == uploaded.splitlines()[0]


async def test_a_private_server_url_is_refused_before_any_connection():
    service = await _service()
    calendar = await service.create_calendar(
        OWNER, "LAN", caldav_url="http://127.0.0.1:8080/dav/", caldav_username="operator"
    )
    remote = FakeRemoteCalendar([])

    with pytest.raises(SSRFError):
        _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert remote.opened_with == []


async def test_an_unbound_calendar_cannot_sync():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Local only")
    remote = FakeRemoteCalendar([])

    with pytest.raises(ValueError):
        _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)


async def test_sync_stamps_the_calendar(monkeypatch: pytest.MonkeyPatch):
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    monkeypatch.setattr("services.calendar.caldav.assert_public_url", _allow)
    assert calendar.last_synced_at is None

    _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert (await service.get_calendar(OWNER, calendar.id)).last_synced_at is not None


async def _allow(_url: str) -> None:
    """Stand in for the SSRF guard: the fake server's hostname doesn't resolve, and DNS is
    not what these tests are about. The guard's own refusal is covered above, unpatched."""
    return None
