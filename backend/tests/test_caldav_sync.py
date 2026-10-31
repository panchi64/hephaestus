"""CalDAV sync (`CAL-2`): reconcile behaviour over a faked collection.

The protocol client, including its SSRF guard, is covered in `test_caldav_client.py`.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self, override

import pytest

from core.db import init_db, make_engine
from core.vault import Vault
from services.calendar import CalendarService
from services.calendar.caldav import CalDavSync, RemoteCalendar
from services.calendar.dav import DavError, DavRefused, RemoteListing
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
    """A stand-in CalDAV collection that records the credentials it was opened with."""

    def __init__(self, objects: list[FakeRemoteObject]) -> None:
        self.stored: list[FakeRemoteObject] = objects
        self.opened_with: list[tuple[str, str | None, str | None]] = []

    def connector(self, url: str, username: str | None, password: str | None) -> Self:
        self.opened_with.append((url, username, password))
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    @override
    async def objects(self, limit: int) -> RemoteListing:
        listed = [(obj.data, obj.url, obj.etag) for obj in self.stored]
        return RemoteListing(listed[:limit], truncated=len(listed) > limit)

    @override
    async def put(self, uid: str, ics: str) -> tuple[str, str | None]:
        saved = FakeRemoteObject(ics, f"{REMOTE_URL}{uid}.ics", "etag-pushed")
        self.stored.append(saved)
        return saved.url, saved.etag


class RefusingRemoteCalendar(FakeRemoteCalendar):
    @override
    async def put(self, uid: str, ics: str) -> tuple[str, str | None]:
        raise DavRefused("the server said no")


class UnreachableOnPushRemoteCalendar(FakeRemoteCalendar):
    def __init__(self, objects: list[FakeRemoteObject]) -> None:
        super().__init__(objects)
        self.puts: int = 0

    @override
    async def put(self, uid: str, ics: str) -> tuple[str, str | None]:
        self.puts += 1
        raise DavError("could not reach the CalDAV server: timed out")


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


async def _local_event(service: CalendarService, calendar_id: str, title: str) -> None:
    _ = await service.create_event(
        OWNER, calendar_id, title=title, starts_at=datetime(2026, 6, 8, 9, tzinfo=UTC)
    )


async def test_pull_creates_local_events():
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar(
        [FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")]
    )

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.pulled_created == 1
    stored = await service.list_events(OWNER, calendar.id)
    assert [event.title for event in stored] == ["Kickoff"]
    assert stored[0].remote_href == f"{REMOTE_URL}a.ics"
    assert stored[0].remote_etag == "etag-1"


async def test_the_remote_is_opened_once_with_the_decrypted_credentials():
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    await _local_event(service, calendar.id, "Local only")

    _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert remote.opened_with == [(REMOTE_URL, "operator", "hunter2")]


async def test_a_second_sync_skips_objects_whose_etag_is_unchanged():
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar(
        [FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")]
    )
    sync = CalDavSync(service, connector=remote.connector)

    _ = await sync.sync(OWNER, calendar.id)
    second = await sync.sync(OWNER, calendar.id)

    assert (second.pulled_created, second.pulled_updated) == (0, 0)


async def test_a_changed_etag_pulls_the_update_in():
    service = await _service()
    calendar = await _bound_calendar(service)
    obj = FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")
    remote = FakeRemoteCalendar([obj])
    sync = CalDavSync(service, connector=remote.connector)
    _ = await sync.sync(OWNER, calendar.id)

    obj.data = _remote_document("a@remote", "Kickoff (moved)", hour=14)
    obj.etag = "etag-2"
    result = await sync.sync(OWNER, calendar.id)

    assert result.pulled_updated == 1
    stored = (await service.list_events(OWNER, calendar.id))[0]
    assert stored.title == "Kickoff (moved)"
    assert stored.starts_at == datetime(2026, 6, 8, 14, tzinfo=UTC)


async def test_local_events_are_pushed_once_and_then_recognized():
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    await _local_event(service, calendar.id, "Local only")
    sync = CalDavSync(service, connector=remote.connector)

    first = await sync.sync(OWNER, calendar.id)
    assert first.pushed == 1
    assert (await service.list_events(OWNER, calendar.id))[0].remote_href is not None

    second = await sync.sync(OWNER, calendar.id)
    assert second.pushed == 0
    assert len(remote.stored) == 1


async def test_a_read_only_calendar_never_pushes():
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    remote = FakeRemoteCalendar([])
    await _local_event(service, calendar.id, "Local only")

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.pushed == 0
    assert remote.stored == []


async def test_an_event_the_server_dropped_is_removed_locally():
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    obj = FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")
    remote = FakeRemoteCalendar([obj])
    sync = CalDavSync(service, connector=remote.connector)
    _ = await sync.sync(OWNER, calendar.id)

    remote.stored.clear()
    result = await sync.sync(OWNER, calendar.id)

    assert result.removed_locally == 1
    assert await service.list_events(OWNER, calendar.id) == []


async def test_a_truncated_listing_removes_nothing(monkeypatch: pytest.MonkeyPatch):
    """A listing cut at the cap covers only the front of the collection, so every local
    event past the cut would otherwise be deleted on every sync."""
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    remote = FakeRemoteCalendar(
        [
            FakeRemoteObject(_remote_document(f"{uid}@remote", uid), f"{REMOTE_URL}{uid}.ics")
            for uid in ("a", "b", "c")
        ]
    )
    sync = CalDavSync(service, connector=remote.connector)
    _ = await sync.sync(OWNER, calendar.id)
    assert len(await service.list_events(OWNER, calendar.id)) == 3

    # The server still has all three; only the cap shrinks.
    monkeypatch.setattr("services.calendar.caldav.MAX_REMOTE_OBJECTS", 2)
    result = await sync.sync(OWNER, calendar.id)

    assert result.removed_locally == 0
    assert len(await service.list_events(OWNER, calendar.id)) == 3


async def test_an_unreadable_object_is_skipped_and_removes_nothing():
    """Its UIDs are unknown, so a local copy of it can't be judged gone from the server."""
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    good = FakeRemoteObject(_remote_document("a@remote", "Kickoff"), f"{REMOTE_URL}a.ics")
    remote = FakeRemoteCalendar([good])
    sync = CalDavSync(service, connector=remote.connector)
    _ = await sync.sync(OWNER, calendar.id)

    good.data = "not a calendar"
    late = FakeRemoteObject(_remote_document("b@remote", "Review"), f"{REMOTE_URL}b.ics")
    remote.stored.append(late)
    result = await sync.sync(OWNER, calendar.id)

    assert (result.skipped, result.pulled_created, result.removed_locally) == (1, 1, 0)
    titles = sorted(event.title for event in await service.list_events(OWNER, calendar.id))
    assert titles == ["Kickoff", "Review"]


async def test_a_purely_local_event_survives_a_sync_that_does_not_mention_it():
    """Absence upstream says nothing about an event that was never pushed there."""
    service = await _service()
    calendar = await _bound_calendar(service, read_only=True)
    remote = FakeRemoteCalendar([])
    await _local_event(service, calendar.id, "Mine alone")

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.removed_locally == 0
    assert len(await service.list_events(OWNER, calendar.id)) == 1


async def test_a_refused_upload_is_skipped_not_fatal():
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = RefusingRemoteCalendar([])
    await _local_event(service, calendar.id, "Unwanted")

    result = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert result.pushed == 0
    assert len(await service.list_events(OWNER, calendar.id)) == 1


async def test_an_unreachable_server_stops_the_push_at_the_first_event():
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = UnreachableOnPushRemoteCalendar([])
    for title in ("One", "Two", "Three"):
        await _local_event(service, calendar.id, title)

    with pytest.raises(DavError, match="could not reach"):
        _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert remote.puts == 1
    assert (await service.get_calendar(OWNER, calendar.id)).last_synced_at is None


async def test_a_pushed_event_round_trips_through_the_ics_layer():
    """A pushed recurring event carries its rule and its zone, not a flattened instant."""
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    event = await service.create_event(
        OWNER,
        calendar.id,
        title="Standup",
        starts_at=datetime(2026, 6, 8, 7, tzinfo=UTC),
        timezone="Europe/Madrid",
        rrule="FREQ=DAILY",
    )

    _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    uploaded = remote.stored[0].data
    assert "RRULE:FREQ=DAILY" in uploaded
    assert "TZID=Europe/Madrid" in uploaded
    assert export_ics([event]).decode().splitlines()[0] == uploaded.splitlines()[0]


async def test_an_unbound_calendar_cannot_sync():
    service = await _service()
    calendar = await service.create_calendar(OWNER, "Local only")
    remote = FakeRemoteCalendar([])

    with pytest.raises(ValueError):
        _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)


async def test_sync_stamps_the_calendar():
    service = await _service()
    calendar = await _bound_calendar(service)
    remote = FakeRemoteCalendar([])
    assert calendar.last_synced_at is None

    _ = await CalDavSync(service, connector=remote.connector).sync(OWNER, calendar.id)

    assert (await service.get_calendar(OWNER, calendar.id)).last_synced_at is not None
