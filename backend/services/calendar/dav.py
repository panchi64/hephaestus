"""A minimal async CalDAV client: list a collection's events and upload new ones (RFC 4791).

The collection URL is operator-supplied, so every request re-runs the SSRF guard and no
redirect is followed: a public host that answers with a 3xx to a LAN address would
otherwise be reached.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from urllib.parse import quote, urljoin

import httpx

from core.ssrf import assert_public_url

# A listing is held whole in memory to be parsed, so its size is capped.
MAX_LISTING_BYTES = 64 * 1024 * 1024

_TIMEOUT_S = 30.0
_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
_CALENDAR_QUERY = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter>
  </c:filter>
</c:calendar-query>"""


class DavError(Exception):
    """The CalDAV server refused a request or answered with something unusable."""


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

    Authenticates with Basic, and switches to Digest for the rest of the session when the
    server's 401 challenge asks for it.
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
        self._auth: httpx.Auth | None = httpx.BasicAuth(username, secret) if username else None
        self._digest: httpx.Auth | None = httpx.DigestAuth(username, secret) if username else None

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
        """Every event object in the collection, up to ``limit``."""
        status, _, body = await self._send(
            "REPORT",
            self._url,
            {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            _CALENDAR_QUERY,
        )
        if status != 207:
            raise DavError(f"listing the calendar failed: HTTP {status}")
        # The body is untrusted. Expat resolves no external entity and caps entity
        # amplification, so XXE and billion-laughs both surface as ParseError.
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise DavError(f"the server's listing is not valid XML: {exc}") from exc

        objects: list[tuple[str, str, str | None]] = []
        for response in root.iterfind("d:response", _NS):
            href = response.findtext("d:href", namespaces=_NS)
            data, etag = _ok_props(response)
            if not href or not data:
                continue
            if len(objects) >= limit:
                return RemoteListing(objects, truncated=True)
            objects.append((data, urljoin(self._url, href.strip()), etag))
        return RemoteListing(objects)

    async def put(self, uid: str, ics: str) -> tuple[str, str | None]:
        """Create the event at ``<collection>/<uid>.ics`` and return its href and etag.

        ``If-None-Match: *`` makes the server refuse to overwrite an existing object.
        """
        href = urljoin(self._url, f"{quote(uid, safe='')}.ics")
        status, headers, _ = await self._send(
            "PUT",
            href,
            {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            ics.encode(),
        )
        if not 200 <= status < 300:
            raise DavError(f"the server refused the event: HTTP {status}")
        return href, _header(headers, "etag") or None

    async def _send(
        self, method: str, url: str, headers: dict[str, str], content: bytes
    ) -> tuple[int, httpx.Headers, bytes]:
        await assert_public_url(url)
        status, response_headers, body = await self._exchange(method, url, headers, content)
        if (
            status == 401
            and self._digest is not None
            and self._auth is not self._digest
            and _header(response_headers, "www-authenticate").lower().startswith("digest")
        ):
            self._auth = self._digest
            status, response_headers, body = await self._exchange(method, url, headers, content)
        if 300 <= status < 400:
            target = _header(response_headers, "location") or "elsewhere"
            raise DavError(f"the server redirected to {target}; save that URL instead")
        return status, response_headers, body

    async def _exchange(
        self, method: str, url: str, headers: dict[str, str], content: bytes
    ) -> tuple[int, httpx.Headers, bytes]:
        request = self._client.build_request(method, url, headers=headers, content=content)
        response = await self._client.send(request, auth=self._auth, stream=True)
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_LISTING_BYTES:
                    raise DavError(f"the server's response exceeds {MAX_LISTING_BYTES} bytes")
        finally:
            await response.aclose()
        return response.status_code, response.headers, bytes(body)


def _header(headers: httpx.Headers, name: str) -> str:
    return headers[name] if name in headers else ""


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
