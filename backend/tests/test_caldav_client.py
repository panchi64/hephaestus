"""The CalDAV protocol client (`services/calendar/dav.py`), driven over a mock transport."""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest

from core import ssrf
from core.exceptions import SSRFError
from services.calendar.dav import DavCollection, DavError, DavRefused

COLLECTION = "https://dav.example.com/calendars/operator/personal/"

Handler = Callable[[httpx.Request], httpx.Response]


def _multistatus(*responses: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        + '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + "".join(responses)
        + "</d:multistatus>"
    ).encode()


def _object(href: str, data: str | None, etag: str = '"e1"', status: str = "200 OK") -> str:
    props = f"<d:getetag>{etag}</d:getetag>"
    if data is not None:
        props += f"<c:calendar-data>{data}</c:calendar-data>"
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
        + f"<d:status>HTTP/1.1 {status}</d:status></d:propstat></d:response>"
    )


@pytest.fixture(autouse=True)
def checked_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records each SSRF check in place of DNS, which these tests don't exercise."""
    seen: list[str] = []

    async def record(url: str) -> None:
        seen.append(url)

    monkeypatch.setattr("services.calendar.dav.assert_public_url", record)
    return seen


def _collection(handler: Handler, *, username: str | None = "operator") -> DavCollection:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return DavCollection(COLLECTION, username, "hunter2", client=client)


async def test_the_listing_resolves_hrefs_and_keeps_etags(checked_urls: list[str]):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = _multistatus(
            _object("/calendars/operator/personal/a.ics", "BEGIN:VCALENDAR", etag='"abc"'),
            _object("/calendars/operator/personal/", None),
        )
        return httpx.Response(207, content=body)

    async with _collection(handler) as dav:
        listing = await dav.objects(10)

    assert listing.objects == [("BEGIN:VCALENDAR", f"{COLLECTION}a.ics", '"abc"')]
    assert not listing.truncated
    assert requests[0].method == "REPORT"
    assert requests[0].headers["depth"] == "1"
    assert checked_urls == [COLLECTION]


async def test_a_response_whose_props_are_not_found_is_skipped():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, content=_multistatus(_object("x.ics", "DATA", status="404")))

    async with _collection(handler) as dav:
        listing = await dav.objects(10)

    assert listing.objects == []


async def test_the_listing_is_flagged_truncated_past_the_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _multistatus(*(_object(f"{n}.ics", f"DATA{n}") for n in range(3)))
        return httpx.Response(207, content=body)

    async with _collection(handler) as dav:
        listing = await dav.objects(2)

    assert [href for _, href, _ in listing.objects] == [f"{COLLECTION}0.ics", f"{COLLECTION}1.ics"]
    assert listing.truncated


async def test_a_listing_failure_raises():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async with _collection(handler) as dav:
        with pytest.raises(DavError, match="HTTP 403"):
            _ = await dav.objects(10)


async def test_an_oversized_response_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("services.calendar.dav.MAX_LISTING_BYTES", 100)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, content=_multistatus(_object("a.ics", "X" * 200)))

    async with _collection(handler) as dav:
        with pytest.raises(DavError, match="too large"):
            _ = await dav.objects(10)


async def test_an_external_entity_is_never_resolved():
    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            b'<?xml version="1.0"?><!DOCTYPE d:multistatus '
            + b'[<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            + b'<d:multistatus xmlns:d="DAV:">&x;</d:multistatus>'
        )
        return httpx.Response(207, content=body)

    async with _collection(handler) as dav:
        with pytest.raises(DavError, match="not valid XML"):
            _ = await dav.objects(10)


async def test_put_creates_the_object_without_overwriting(checked_urls: list[str]):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, headers={"ETag": '"new"'})

    async with _collection(handler) as dav:
        href, etag = await dav.put("a1@local", "BEGIN:VCALENDAR")

    assert href == f"{COLLECTION}a1@local.ics"
    assert etag == '"new"'
    assert requests[0].method == "PUT"
    assert requests[0].headers["if-none-match"] == "*"
    assert requests[0].content == b"BEGIN:VCALENDAR"
    assert checked_urls == [href]


async def test_a_uid_unsafe_in_a_path_is_named_by_its_hash():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201)

    async with _collection(handler) as dav:
        href, _ = await dav.put("abc/123@example.com", "BEGIN:VCALENDAR")
        again, _ = await dav.put("abc/123@example.com", "BEGIN:VCALENDAR")

    name = href.removeprefix(COLLECTION)
    assert re.fullmatch(r"[0-9a-f]{64}\.ics", name)
    assert again == href


async def test_put_without_an_etag_header_returns_none():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    async with _collection(handler) as dav:
        _, etag = await dav.put("a", "BEGIN:VCALENDAR")

    assert etag is None


async def test_a_refused_put_raises():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(412)

    async with _collection(handler) as dav:
        with pytest.raises(DavRefused, match="HTTP 412"):
            _ = await dav.put("a", "BEGIN:VCALENDAR")


def _challenging(*challenges: str, accepts: str) -> tuple[Handler, list[str]]:
    """A server that answers 401 with ``challenges`` until it sees the ``accepts`` scheme."""
    schemes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers["authorization"] if "authorization" in request.headers else ""
        schemes.append(auth.split(" ")[0])
        if auth.startswith(f"{accepts} "):
            return httpx.Response(207, content=_multistatus())
        return httpx.Response(401, headers=[("WWW-Authenticate", c) for c in challenges])

    return handler, schemes


_DIGEST = 'Digest realm="dav", nonce="abc", qop="auth"'


async def test_no_credential_is_sent_before_a_digest_challenge():
    handler, schemes = _challenging(_DIGEST, accepts="Digest")

    async with _collection(handler) as dav:
        _ = await dav.objects(10)
        _ = await dav.objects(10)

    assert schemes == ["", "Digest", "Digest"]


async def test_a_basic_challenge_is_answered_with_basic():
    handler, schemes = _challenging('Basic realm="dav"', accepts="Basic")

    async with _collection(handler) as dav:
        _ = await dav.objects(10)
        _ = await dav.objects(10)

    assert schemes == ["", "Basic", "Basic"]


async def test_digest_is_preferred_when_both_schemes_are_offered():
    handler, schemes = _challenging('Basic realm="dav"', _DIGEST, accepts="Digest")

    async with _collection(handler) as dav:
        _ = await dav.objects(10)

    assert "Basic" not in schemes
    assert schemes[-1] == "Digest"


async def test_both_schemes_in_one_header_still_authenticate():
    handler, schemes = _challenging(f'Basic realm="dav", {_DIGEST}', accepts="Basic")

    async with _collection(handler) as dav:
        listing = await dav.objects(10)

    assert listing.objects == []
    assert schemes[-1] == "Basic"


async def test_a_transport_failure_raises_dav_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with _collection(handler) as dav:
        with pytest.raises(DavError, match="could not reach"):
            _ = await dav.objects(10)


async def test_a_private_address_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("services.calendar.dav.assert_public_url", ssrf.assert_public_url)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(207, content=_multistatus())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with DavCollection("http://127.0.0.1:8080/dav/", "operator", "pw", client=client) as dav:
        with pytest.raises(SSRFError):
            _ = await dav.objects(10)

    assert requests == []


async def test_a_redirect_is_refused_not_followed():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(301, headers={"Location": "http://10.0.0.5/dav/"})

    async with _collection(handler) as dav:
        with pytest.raises(DavError, match="10.0.0.5"):
            _ = await dav.objects(10)

    assert len(requests) == 1


async def test_no_credentials_sends_no_authorization():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(207, content=_multistatus())

    async with _collection(handler, username=None) as dav:
        _ = await dav.objects(10)

    assert "authorization" not in requests[0].headers
