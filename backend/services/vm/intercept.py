"""Puts secrets into a plugin's HTTPS requests at the VM boundary.

A plugin's environment holds a placeholder for each secret, never its value. When a
call that carries secrets opens a tunnel to the one host a secret is bound to, the
gateway hands the tunnel here: TLS ends at this process, under a certificate from a CA
the guest trusts and that lives only in this process's memory, and each request's line
and headers have the placeholder replaced by the value before it goes on to the real
host over verified TLS. ``Host`` is restated as the bound host, so a request cannot
carry the value to another site behind the same address.

One request per connection, HTTP/1.1 only; a value in a request body is not replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import shutil
import ssl
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from services.vm import egress

_VALIDITY = datetime.timedelta(days=30)


@dataclass(frozen=True)
class Secret:
    placeholder: str
    value: str
    host: str


@dataclass(frozen=True)
class _Job:
    host: str
    port: int
    secrets: tuple[Secret, ...]


def _verified() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    return context


class Interceptor:
    def __init__(self, upstream_tls: Callable[[], ssl.SSLContext] = _verified) -> None:
        self._upstream_tls = upstream_tls
        self._dir: Path | None = None
        self._ca: tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None = None
        self._contexts: dict[str, ssl.SSLContext] = {}
        self._server: asyncio.Server | None = None
        self._port = 0
        self._jobs: dict[int, _Job] = {}
        self._expected: dict[str, int] = {}

    @property
    def ca_pem(self) -> str:
        """The CA certificate the guest has to trust."""
        _, cert = self._authority()
        return cert.public_bytes(serialization.Encoding.PEM).decode()

    async def intercept(
        self,
        host: str,
        port: int,
        secrets: Sequence[Secret],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Answer a CONNECT to ``host`` by terminating its TLS here."""
        host = host.strip().rstrip(".").lower()
        await self._listen()
        local_r, local_w = await asyncio.open_connection("127.0.0.1", self._port)
        key = local_w.get_extra_info("sockname")[1]
        self._jobs[key] = _Job(host, port, tuple(secrets))
        self._expected[host] = self._expected.get(host, 0) + 1
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await egress.splice(reader, writer, local_r, local_w)
        finally:
            self._jobs.pop(key, None)
            remaining = self._expected.get(host, 1) - 1
            if remaining:
                self._expected[host] = remaining
            else:
                self._expected.pop(host, None)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._contexts.clear()

    async def _listen(self) -> None:
        if self._server is not None:
            return
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.set_alpn_protocols(["http/1.1"])
        context.sni_callback = self._choose
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=context)
        self._port = self._server.sockets[0].getsockname()[1]

    def _choose(self, connection: ssl.SSLObject, name: str | None, _base: ssl.SSLContext):
        if not name or name not in self._expected:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        connection.context = self._context_for(name)
        return None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        job = self._jobs.pop(writer.get_extra_info("peername")[1], None)
        try:
            if job is None:
                return
            request = await asyncio.wait_for(egress.read_head(reader), egress.HEAD_TIMEOUT_S)
            if request is None or not request.line:
                return
            upstream = await egress.dial_pinned(
                job.host, job.port, writer, tls=self._upstream_tls()
            )
            if upstream is None:
                return
            server_r, server_w = upstream
            authority = job.host if job.port == 443 else f"{job.host}:{job.port}"
            server_w.write(_substituted(request.line, job.secrets))
            server_w.write(f"Host: {authority}\r\n".encode())
            for line in request.headers:
                if not line.lower().startswith((b"proxy-", b"connection:", b"host:")):
                    server_w.write(_substituted(line, job.secrets))
            server_w.write(b"Connection: close\r\n\r\n")
            await server_w.drain()
            await egress.splice(reader, writer, server_r, server_w)
        except Exception:  # one bad request must never stop the interceptor
            egress.logger.debug("egress: an intercepted request failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def _authority(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        if self._ca is None:
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hephaestus egress")])
            now = datetime.datetime.now(datetime.UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(hours=1))
                .not_valid_after(now + _VALIDITY)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
                )
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        key_cert_sign=True,
                        crl_sign=True,
                        content_commitment=False,
                        key_encipherment=False,
                        data_encipherment=False,
                        key_agreement=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .sign(key, hashes.SHA256())
            )
            self._ca = (key, cert)
        return self._ca

    def _context_for(self, host: str) -> ssl.SSLContext:
        context = self._contexts.get(host)
        if context is not None:
            return context
        ca_key, ca_cert = self._authority()
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + _VALIDITY)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )
        # The ssl module loads a certificate and key only from a file.
        if self._dir is None:
            self._dir = Path(tempfile.mkdtemp(prefix="hephaestus-egress-"))
        pem = self._dir / f"{len(self._contexts)}.pem"
        pem.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.set_alpn_protocols(["http/1.1"])
        context.load_cert_chain(pem)
        pem.unlink()
        self._contexts[host] = context
        return context


def _substituted(line: bytes, secrets: Sequence[Secret]) -> bytes:
    for secret in secrets:
        line = line.replace(secret.placeholder.encode(), secret.value.encode())
    return line
