"""Secrets put into a plugin's HTTPS requests at the egress gateway."""

from __future__ import annotations

import asyncio
import base64
import datetime
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import services.vm.egress as egress_mod
from services.vm.egress import ALLOW_FILE, WORKSPACE, EgressGateway, Secret
from services.vm.intercept import Interceptor

HOST = "api.example.com"
PLACEHOLDER = "hephaestus-secret-0123456789abcdef"


def _server_cert(tmp_path: Path) -> tuple[Path, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    path = tmp_path / "server.pem"
    _ = path.write_bytes(
        cert_pem
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path, cert_pem


def _port_of(server: asyncio.Server) -> int:
    return cast(tuple[str, int], server.sockets[0].getsockname())[1]


@pytest.fixture
async def upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[int, bytes, list[bytes]]]:
    """An HTTPS server for ``HOST`` that records the request head it is sent."""
    pem, cert_pem = _server_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem)
    heads: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)

    async def resolve(_host: str, _port: int) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(egress_mod, "_resolve_public_ips", resolve)
    yield _port_of(server), cert_pem, heads
    server.close()


async def _through_gateway(
    gateway: EgressGateway, credentials: str, port: int, trust: str, request: bytes
) -> bytes:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await gateway.serve(WORKSPACE, reader, writer)

    proxy = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", _port_of(proxy))
        auth = base64.b64encode(credentials.encode()).decode()
        writer.write(
            f"CONNECT {HOST}:{port} HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode()
        )
        await writer.drain()
        assert b" 200 " in await reader.readuntil(b"\r\n\r\n")
        client = ssl.create_default_context(cadata=trust)
        await writer.start_tls(client, server_hostname=HOST)
        writer.write(request)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), 10)
        writer.close()
        return reply
    finally:
        proxy.close()


def _allow(tmp_path: Path) -> Path:
    directory = tmp_path / "allow"
    directory.mkdir()
    _ = (directory / ALLOW_FILE).write_text(f"{HOST}\n")
    return directory


async def test_a_placeholder_becomes_the_secret_in_requests_to_its_host(
    tmp_path: Path, upstream: tuple[int, bytes, list[bytes]]
):
    port, cert_pem, heads = upstream
    interceptor = Interceptor(lambda: ssl.create_default_context(cadata=cert_pem.decode()))
    gateway = EgressGateway(interceptor)
    credentials = gateway.grant(_allow(tmp_path), [Secret(PLACEHOLDER, "s3cret", HOST)])

    reply = await _through_gateway(
        gateway,
        credentials,
        port,
        interceptor.ca_pem,
        (
            f"GET /v1?key={PLACEHOLDER} HTTP/1.1\r\nHost: elsewhere.example.org\r\n"
            + f"Authorization: Bearer {PLACEHOLDER}\r\n\r\n"
        ).encode(),
    )

    assert reply.endswith(b"ok")
    head = heads[0].decode()
    assert "GET /v1?key=s3cret HTTP/1.1" in head
    assert "Authorization: Bearer s3cret" in head
    assert f"Host: {HOST}" in head
    assert "elsewhere.example.org" not in head
    assert PLACEHOLDER not in head
    await interceptor.close()


async def test_a_grant_without_secrets_tunnels_untouched(
    tmp_path: Path, upstream: tuple[int, bytes, list[bytes]]
):
    port, cert_pem, heads = upstream
    interceptor = Interceptor()
    gateway = EgressGateway(interceptor)
    credentials = gateway.grant(_allow(tmp_path))

    reply = await _through_gateway(
        gateway,
        credentials,
        port,
        cert_pem.decode(),
        f"GET / HTTP/1.1\r\nHost: {HOST}\r\nX-Key: {PLACEHOLDER}\r\n\r\n".encode(),
    )

    assert reply.endswith(b"ok")
    assert PLACEHOLDER in heads[0].decode()
    await interceptor.close()


async def test_a_secret_is_never_sent_to_another_host(tmp_path: Path):
    grant_secrets = [Secret(PLACEHOLDER, "s3cret", HOST)]
    gateway = EgressGateway(Interceptor())
    credentials = gateway.grant(_allow(tmp_path), grant_secrets)
    grant = gateway.grant_for(credentials)
    assert grant is not None
    assert grant.secrets_for("API.example.com.") == tuple(grant_secrets)
    assert grant.secrets_for("other.example.com") == ()
