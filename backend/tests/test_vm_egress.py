"""The allowlisting gateway every connection out of the platform VM ends at.

Requests are driven through :meth:`EgressGateway.serve` over loopback, the way the
tunnel hands them over, so the predicate, the credentials and the pinned dial are tested
as one path.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path
from typing import cast

import pytest

import services.vm.egress as gw_mod
from services.vm.egress import (
    ALLOW_FILE,
    DENIED_MARKER,
    PLATFORM,
    WORKSPACE,
    Allowlist,
    EgressGateway,
    allowed,
    parse_allowlist,
)

from .conftest import egress_policy


# --- the predicate ----------------------------------------------------------
def test_a_plain_domain_covers_that_host_and_nothing_under_it():
    # A plain entry that admitted every subdomain would turn one innocuous-looking host
    # into anything anyone can publish under it.
    assert allowed("pypi.org", ["pypi.org"])
    assert not allowed("files.pypi.org", ["pypi.org"])
    assert not allowed("a.b.pypi.org", ["pypi.org"])
    assert allowed("files.pypi.org", ["pypi.org", "files.pypi.org"])


def test_a_plain_domain_does_not_cover_a_lookalike_suffix():
    # `notpypi.org` ends with `pypi.org` as a *string*; a naive suffix test would hand an
    # approval for one host to a domain anyone can buy.
    assert not allowed("notpypi.org", ["pypi.org"])
    assert not allowed("pypi.org.evil.com", ["pypi.org"])
    assert not allowed("example.com", ["pypi.org"])


def test_a_wildcard_covers_subdomains_only():
    assert allowed("cdn.example.com", ["*.example.com"])
    assert allowed("a.b.example.com", ["*.example.com"])
    assert not allowed("example.com", ["*.example.com"])  # the apex was not approved


def test_matching_ignores_case_and_the_dns_root_dot():
    assert allowed("PyPI.ORG.", ["pypi.org"])


def test_an_empty_allowlist_allows_nothing():
    assert not allowed("pypi.org", [])


# --- the file ---------------------------------------------------------------
def test_parse_allowlist_takes_one_domain_per_line():
    assert parse_allowlist("pypi.org\n*.example.com\n") == ("pypi.org", "*.example.com")
    assert parse_allowlist("\n  pypi.org  \n\n") == ("pypi.org",)
    assert parse_allowlist("PyPI.org\n") == ("pypi.org",)
    assert parse_allowlist("") == ()


def test_the_allowlist_reloads_when_the_file_changes(tmp_path: Path):
    # An approval exists to unblock a retry that is about to happen; a gateway still
    # holding the list as it stood earlier would refuse it.
    path = tmp_path / ALLOW_FILE
    _ = path.write_text("pypi.org\n")
    allow = Allowlist(tmp_path)
    assert allow.domains() == ("pypi.org",)

    _ = path.write_text("pypi.org\napi.example.com\n")
    os.utime(path, (1, 1))  # a distinct mtime, whatever the filesystem's resolution
    assert allow.domains() == ("pypi.org", "api.example.com")


def test_a_missing_allowlist_fails_closed(tmp_path: Path):
    assert Allowlist(tmp_path).domains() == ()
    assert not Allowlist(tmp_path).permits("pypi.org")


async def test_the_gateway_and_the_policy_name_the_same_file(tmp_path: Path):
    policy = egress_policy(tmp_path, ("pypi.org",))
    _ = await policy.materialise("conv-1")
    assert (policy.allow_dir("conv-1") / ALLOW_FILE).is_file()


async def test_the_policys_own_output_parses_and_matches(tmp_path: Path):
    policy = egress_policy(tmp_path, ("pypi.org", "*.example.com"))
    _ = await policy.materialise("conv-1")
    domains = Allowlist(policy.allow_dir("conv-1")).domains()

    assert allowed("pypi.org", domains)
    assert allowed("cdn.example.com", domains)
    assert not allowed("files.pypi.org", domains)  # the plain entry is that host alone
    assert not allowed("evil.com", domains)


def test_the_denial_marker_names_this_fence():
    # It is what an agent sees in the body of a refusal, so it has to identify the fence
    # rather than read as an anonymous proxy error it would try to work around.
    assert DENIED_MARKER.startswith("hephaestus-egress")


# --- serving requests -------------------------------------------------------
def _basic(credentials: str) -> bytes:
    return b"Proxy-Authorization: Basic " + base64.b64encode(credentials.encode()) + b"\r\n"


async def _serve_once(gateway: EgressGateway, channel: bytes, raw: bytes) -> bytes:
    """One request through the gateway over loopback; the reply it wrote."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await gateway.serve(channel, reader, writer)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = _port_of(server)
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(raw)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(65536), timeout=5.0)
        writer.close()
    return reply


def _port_of(server: asyncio.Server) -> int:
    return cast(tuple[str, int], server.sockets[0].getsockname())[1]


async def _upstream(seen: list[bytes]) -> asyncio.Server:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0)


@pytest.fixture
def gateway_to_upstream(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, int], list[str]]:
    """Point the pinned dial at a local stub, recording the host it was asked for."""
    dialled: list[str] = []
    state: dict[str, int] = {}

    async def fake_dial(
        host: str, _port: int, _client_w: asyncio.StreamWriter
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        dialled.append(host)
        return await asyncio.open_connection("127.0.0.1", state["port"])

    monkeypatch.setattr(gw_mod, "dial_pinned", fake_dial)
    return state, dialled


async def test_an_endless_request_head_is_refused_rather_than_buffered(tmp_path: Path):
    # The head is held whole so `Host:` can be re-stated from the authority, and what
    # sends it is code the agent wrote; unbounded, it would grow the gateway's memory.
    gateway = EgressGateway()
    credentials = gateway.grant(tmp_path)
    header = b"X-Padding: " + b"a" * 1000 + b"\r\n"
    oversized = (
        b"GET http://pypi.org/simple HTTP/1.1\r\n" + _basic(credentials) + header * 70 + b"\r\n"
    )
    reply = await _serve_once(gateway, WORKSPACE, oversized)
    assert reply.startswith(b"HTTP/1.1 431")


async def test_the_forwarded_host_header_names_the_authority_that_was_allowed(
    tmp_path: Path, gateway_to_upstream: tuple[dict[str, int], list[str]]
):
    # The allowlist decision and the pinned dial are both made on the request line's
    # authority; a `Host:` the agent chose freely would reach a different origin on any
    # edge that routes by it.
    state, dialled = gateway_to_upstream
    seen: list[bytes] = []
    up = await _upstream(seen)
    state["port"] = _port_of(up)
    _ = (tmp_path / ALLOW_FILE).write_text("pypi.org\n")
    gateway = EgressGateway()
    credentials = gateway.grant(tmp_path)
    async with up:
        _ = await _serve_once(
            gateway,
            WORKSPACE,
            b"GET http://pypi.org/simple HTTP/1.1\r\nHost: attacker.example\r\n"
            + _basic(credentials)
            + b"\r\n",
        )

    head = seen[0].decode()
    assert dialled == ["pypi.org"]
    assert "Host: pypi.org\r\n" in head
    assert "attacker.example" not in head
    assert "Proxy-Authorization" not in head


async def test_a_workspace_reaches_only_its_allowlist(
    tmp_path: Path, gateway_to_upstream: tuple[dict[str, int], list[str]]
):
    _state, dialled = gateway_to_upstream
    _ = (tmp_path / ALLOW_FILE).write_text("pypi.org\n")
    gateway = EgressGateway()
    credentials = gateway.grant(tmp_path)

    reply = await _serve_once(
        gateway,
        WORKSPACE,
        b"GET http://evil.example/ HTTP/1.1\r\n" + _basic(credentials) + b"\r\n",
    )

    assert reply.startswith(b"HTTP/1.1 403")
    assert DENIED_MARKER.encode() in reply and b"evil.example" in reply
    assert dialled == []


async def test_a_refused_tunnel_is_remembered_for_its_workspace(tmp_path: Path):
    _ = (tmp_path / ALLOW_FILE).write_text("pypi.org\n")
    gateway = EgressGateway()
    credentials = gateway.grant(tmp_path)
    before = time.monotonic()

    reply = await _serve_once(
        gateway,
        WORKSPACE,
        b"CONNECT evil.example:443 HTTP/1.1\r\n" + _basic(credentials) + b"\r\n",
    )

    assert reply.startswith(b"HTTP/1.1 403")
    assert gateway.latest_denial(credentials, before) == "evil.example"
    assert gateway.latest_denial(credentials, time.monotonic() + 1) is None
    assert gateway.latest_denial("workspace:unknown", before) is None


@pytest.mark.parametrize(
    "auth",
    [b"", _basic("workspace:wrong"), b"Proxy-Authorization: Bearer abc\r\n"],
    ids=["missing", "wrong", "not-basic"],
)
async def test_a_workspace_without_its_credentials_gets_407(tmp_path: Path, auth: bytes):
    _ = (tmp_path / ALLOW_FILE).write_text("pypi.org\n")
    gateway = EgressGateway()
    _ = gateway.grant(tmp_path)
    reply = await _serve_once(
        gateway, WORKSPACE, b"GET http://pypi.org/ HTTP/1.1\r\n" + auth + b"\r\n"
    )
    assert reply.startswith(b"HTTP/1.1 407")
    assert b"Proxy-Authenticate" in reply


async def test_revoked_credentials_get_407(tmp_path: Path):
    _ = (tmp_path / ALLOW_FILE).write_text("pypi.org\n")
    gateway = EgressGateway()
    credentials = gateway.grant(tmp_path)
    gateway.revoke(credentials)
    reply = await _serve_once(
        gateway,
        WORKSPACE,
        b"GET http://pypi.org/ HTTP/1.1\r\n" + _basic(credentials) + b"\r\n",
    )
    assert reply.startswith(b"HTTP/1.1 407")


async def test_an_unknown_channel_gets_407():
    reply = await _serve_once(EgressGateway(), b"X", b"GET http://pypi.org/ HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 407")


async def test_the_platform_channel_allows_any_host_but_still_pins(
    gateway_to_upstream: tuple[dict[str, int], list[str]],
):
    state, dialled = gateway_to_upstream
    seen: list[bytes] = []
    up = await _upstream(seen)
    state["port"] = _port_of(up)
    async with up:
        reply = await _serve_once(
            EgressGateway(), PLATFORM, b"GET http://anything.example/ HTTP/1.1\r\n\r\n"
        )
    assert reply.startswith(b"HTTP/1.1 200")
    assert dialled == ["anything.example"]


async def test_the_platform_channel_cannot_reach_a_private_address():
    reply = await _serve_once(
        EgressGateway(), PLATFORM, b"GET http://127.0.0.1:9/ HTTP/1.1\r\n\r\n"
    )
    assert reply.startswith(b"HTTP/1.1 403")
    assert DENIED_MARKER.encode() in reply
