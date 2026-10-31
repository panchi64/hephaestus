"""A minimal async CalDAV client: list a collection's events and upload new ones (RFC 4791).

The collection URL is operator-supplied, so every request re-runs the SSRF guard and no
redirect is followed: a public host that answers with a 3xx to a LAN address would
otherwise be reached.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from urllib.parse import urljoin

import httpx

from core.net import ResponseTooLarge, header, iter_capped
from core.ssrf import assert_public_url

# Read up to the object limit, so this only bounds a pathological listing.
MAX_LISTING_BYTES = 64 * 1024 * 1024

_TIMEOUT_S = 30.0
_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
_RESPONSE = "{DAV:}response"
_CALENDAR_QUERY = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter>
  </c:filter>
</c:calendar-query>"""
# Apache-fronted servers reject an encoded `/` in a path by default, so a UID outside this
# set is named by its hash. The UID inside the ICS is what identifies the event.
_SAFE_NAME = re.compile(r"[A-Za-z0-9._@-]{1,200}")
_OFFERS_BASIC = re.compile(r"(?:^|,)\s*basic\b", re.IGNORECASE)


class DavError(Exception):
    """The CalDAV server couldn't be used: unreachable, redirected, or an unusable answer."""


class DavRefused(DavError):
    """The server answered, and refused this one request."""


@dataclass(frozen=True)
class RemoteListing:
    """The collection's objects as ``(ics, href, etag)``.

    ``truncated`` means the listing stopped at its limit, so the UIDs it holds are not the
    server's whole inventory and must not drive local deletions.
    """

    objects: list[tuple[str, str, str | None]]
    truncated: bool = False


class DavCollection:
    """One CalDAV calendar collection, opened at its URL with no principal discovery.

    No credential is sent until the server challenges. Digest is answered when offered,
    otherwise Basic, and the chosen scheme is kept for the rest of the session.
    """

    def __init__(
        self,
        url: str,
        username: str | None,
        password: str | None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url: str = url if url.endswith("/") else f"{url}/"
        self._owns_client: bool = client is None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            timeout=_TIMEOUT_S, follow_redirects=False
        )
        secret = password or ""
        # DigestAuth only sends credentials in answer to a Digest challenge.
        self._auth: httpx.Auth | None = httpx.DigestAuth(username, secret) if username else None
        self._basic: httpx.Auth | None = httpx.BasicAuth(username, secret) if username else None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def objects(self, limit: int) -> RemoteListing:
        """Every event object in the collection, up to ``limit``.

        Parsed as it streams, so reading stops at the limit and no single parse holds the
        event loop for the whole listing.
        """
        headers = {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
        objects: list[tuple[str, str, str | None]] = []
        async with self._open("REPORT", self._url, headers, _CALENDAR_QUERY) as response:
            if response.status_code != 207:
                raise DavError(f"listing the calendar failed: HTTP {response.status_code}")
            # The body is untrusted. Expat resolves no external entity and caps entity
            # amplification, so XXE and billion-laughs both surface as ParseError.
            parser: ElementTree.XMLPullParser[ElementTree.Element] = ElementTree.XMLPullParser(
                events=("end",)
            )
            try:
                async with aclosing(iter_capped(response, MAX_LISTING_BYTES)) as chunks:
                    async for chunk in chunks:
                        parser.feed(chunk)
                        for event in parser.read_events():
                            element = event[-1]
                            if not isinstance(element, ElementTree.Element):
                                continue
                            if element.tag != _RESPONSE:
                                continue
                            entry = self._entry(element)
                            element.clear()
                            if entry is None:
                                continue
                            if len(objects) >= limit:
                                return RemoteListing(objects, truncated=True)
                            objects.append(entry)
                parser.close()
            except ElementTree.ParseError as exc:
                raise DavError(f"the server's listing is not valid XML: {exc}") from exc
            except ResponseTooLarge as exc:
                raise DavError(f"the server's listing is too large: {exc}") from exc
        return RemoteListing(objects)

    async def put(self, uid: str, ics: str) -> tuple[str, str | None]:
        """Create the event in the collection and return its href and etag.

        ``If-None-Match: *`` makes the server refuse to overwrite an existing object.
        Raises :class:`DavRefused` when the server declines this event.
        """
        href = urljoin(self._url, _resource_name(uid))
        headers = {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"}
        async with self._open("PUT", href, headers, ics.encode()) as response:
            if not 200 <= response.status_code < 300:
                raise DavRefused(f"the server refused the event: HTTP {response.status_code}")
            return href, header(response.headers, "etag")

    def _entry(self, response: ElementTree.Element) -> tuple[str, str, str | None] | None:
        href = response.findtext("d:href", namespaces=_NS)
        data, etag = _ok_props(response)
        if not href or not data:
            return None
        return data, urljoin(self._url, href.strip()), etag

    @asynccontextmanager
    async def _open(
        self, method: str, url: str, headers: dict[str, str], content: bytes
    ) -> AsyncGenerator[httpx.Response]:
        """Send one request and yield its unread response; transport failures become DavError."""
        await assert_public_url(url)
        try:
            response = await self._send(method, url, headers, content)
            try:
                if (
                    response.status_code == 401
                    and self._basic is not None
                    and self._auth is not self._basic
                    and _OFFERS_BASIC.search(header(response.headers, "www-authenticate") or "")
                ):
                    await response.aclose()
                    self._auth = self._basic
                    response = await self._send(method, url, headers, content)
                if 300 <= response.status_code < 400:
                    target = header(response.headers, "location") or "elsewhere"
                    raise DavError(f"the server redirected to {target}; save that URL instead")
                yield response
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            raise DavError(f"could not reach the CalDAV server: {exc}") from exc

    async def _send(
        self, method: str, url: str, headers: dict[str, str], content: bytes
    ) -> httpx.Response:
        request = self._client.build_request(method, url, headers=headers, content=content)
        return await self._client.send(request, auth=self._auth, stream=True)


def _resource_name(uid: str) -> str:
    if _SAFE_NAME.fullmatch(uid):
        return f"{uid}.ics"
    return f"{hashlib.sha256(uid.encode()).hexdigest()}.ics"


def _ok_props(response: ElementTree.Element) -> tuple[str | None, str | None]:
    """The ``calendar-data`` and ``getetag`` a response reports under a 200 status."""
    data: str | None = None
    etag: str | None = None
    for propstat in response.iterfind("d:propstat", _NS):
        status = propstat.findtext("d:status", default="", namespaces=_NS).split()
        if status[1:2] != ["200"]:
            continue
        data = propstat.findtext("d:prop/c:calendar-data", namespaces=_NS) or data
        etag = propstat.findtext("d:prop/d:getetag", namespaces=_NS) or etag
    return data, etag.strip() if etag else None
