"""CalDAV sync: reconciling a local calendar with a remote collection (`CAL-2`).

A CalDAV object is an ICS document, so this module owns no parsing: it moves documents
between the server (`dav.py`) and `ics.py`, and merges on ``UID`` exactly as an ICS import
does.

The server URL is operator-supplied, so it is SSRF-checked at the start of every sync, not
trusted from when it was saved. Credentials are decrypted into locals for one sync and are
never logged, returned or written back.

**Scope.** Sync is pull-authoritative with a first-time push: remote changes win on
conflict, local events that were never pushed are uploaded, and an event the server no
longer has is removed locally, but only when the listing came back whole
(`MAX_REMOTE_OBJECTS`). A local deletion is not propagated: telling a deleted row from one
never created needs a tombstone the schema doesn't carry, and guessing would delete the
operator's data off a server shared with other clients.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Protocol

import httpx

from core.ssrf import assert_public_url
from services.calendar.dav import DavCollection, DavError, RemoteListing
from services.calendar.ics import export_ics, parse_ics
from services.calendar.service import CalendarService

logger = logging.getLogger(__name__)

# A collection larger than this is synced only up to the cap, and nothing is removed
# locally that pass.
MAX_REMOTE_OBJECTS = 5000


class RemoteCalendar(Protocol):
    """What the reconciler needs from a remote collection; narrow so tests need no server."""

    async def objects(self, limit: int) -> RemoteListing: ...
    async def put(self, uid: str, ics: str) -> tuple[str, str | None]: ...


Connector = Callable[[str, str | None, str | None], AbstractAsyncContextManager[RemoteCalendar]]


@dataclass(frozen=True)
class SyncResult:
    """What one reconcile did, in the operator's terms."""

    pulled_created: int = 0
    pulled_updated: int = 0
    pushed: int = 0
    removed_locally: int = 0
    skipped: int = 0


class CalDavSync:
    """Reconciles one calendar against its remote collection."""

    def __init__(self, calendars: CalendarService, *, connector: Connector = DavCollection) -> None:
        self._calendars: CalendarService = calendars
        self._connector: Connector = connector

    async def sync(self, owner_id: str, calendar_id: str) -> SyncResult:
        """Pull the remote collection into the local calendar, then push anything local
        that has never been uploaded. Raises :class:`ValueError` when the calendar isn't
        bound to a server and :class:`core.exceptions.SSRFError` when its URL resolves
        somewhere the system refuses to reach."""
        calendar = await self._calendars.get_calendar(owner_id, calendar_id)
        url, username, password = await self._calendars.caldav_credentials(owner_id, calendar_id)
        await assert_public_url(url)

        async with self._connector(url, username, password) as remote:
            listing = await remote.objects(MAX_REMOTE_OBJECTS)
            result = await self._apply_remote(owner_id, calendar_id, listing)
            if not calendar.read_only:
                result = await self._push_local(owner_id, calendar_id, remote, result)

        await self._calendars.mark_synced(owner_id, calendar_id)
        return result

    async def _apply_remote(
        self,
        owner_id: str,
        calendar_id: str,
        remote: RemoteListing,
    ) -> SyncResult:
        """Merge the server's objects in, then drop local events the server no longer has,
        unless the listing was truncated. An object whose etag matches the recorded one is
        not re-parsed."""
        created = updated = skipped = 0
        seen_uids: set[str] = set()

        for document, href, etag in remote.objects:
            for entry in parse_ics(document):
                if entry.uid is None:
                    skipped += 1
                    continue
                seen_uids.add(entry.uid)
                existing = await self._calendars.find_by_uid(owner_id, calendar_id, entry.uid)
                if existing is not None and etag is not None and existing.remote_etag == etag:
                    continue
                try:
                    if existing is None:
                        _ = await self._calendars.create_event(
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
                            remote_href=href,
                            remote_etag=etag,
                        )
                        created += 1
                    else:
                        _ = await self._calendars.update_event(
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
                            remote_href=href,
                            remote_etag=etag,
                        )
                        updated += 1
                except ValueError:
                    logger.warning("calendar: remote event rejected locally", exc_info=True)
                    skipped += 1

        if remote.truncated:
            # The listing covers whichever objects the server sent first, so no slice of
            # the local calendar can be judged gone; skip the vanished pass whole.
            logger.warning(
                "calendar: the remote listing stopped at the %d-object cap, so no local "
                + "event was removed this sync",
                MAX_REMOTE_OBJECTS,
            )
            removed = 0
        else:
            removed = await self._remove_vanished(owner_id, calendar_id, seen_uids)
        return SyncResult(
            pulled_created=created,
            pulled_updated=updated,
            removed_locally=removed,
            skipped=skipped,
        )

    async def _remove_vanished(self, owner_id: str, calendar_id: str, seen_uids: set[str]) -> int:
        """Delete local events that came from this server and are no longer on it. An event
        with no ``remote_href`` was never pushed, so it is left alone."""
        removed = 0
        for event in await self._calendars.list_events(owner_id, calendar_id):
            if event.remote_href and event.uid not in seen_uids:
                await self._calendars.delete_event(owner_id, event.id)
                removed += 1
        return removed

    async def _push_local(
        self,
        owner_id: str,
        calendar_id: str,
        remote: RemoteCalendar,
        result: SyncResult,
    ) -> SyncResult:
        """Upload local events the server has never seen, and record where they landed so
        the next sync doesn't push them twice. One refused upload doesn't stop the rest."""
        pushed = 0
        for event in await self._calendars.list_events(owner_id, calendar_id):
            if event.remote_href:
                continue
            try:
                href, etag = await remote.put(event.uid, export_ics([event]).decode())
            except DavError, httpx.HTTPError:
                logger.warning("calendar: remote refused an event during push", exc_info=True)
                continue
            _ = await self._calendars.update_event(
                owner_id, event.id, remote_href=href, remote_etag=etag
            )
            pushed += 1
        return replace(result, pushed=pushed)
