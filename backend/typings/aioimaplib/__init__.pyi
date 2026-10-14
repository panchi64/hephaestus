import asyncio
import ssl
from asyncio import Future
from collections.abc import Callable
from re import Pattern
from typing import NamedTuple

TWENTY_NINE_MINUTES: float
IMAP4_PORT: int
IMAP4_SSL_PORT: int

class Response(NamedTuple):
    result: str
    lines: list[str | bytes | bytearray]

class AioImapException(Exception):
    def __init__(self, reason: str) -> None: ...

class Error(AioImapException):
    def __init__(self, reason: str) -> None: ...

class Abort(Error):
    def __init__(self, reason: str) -> None: ...

class IMAP4ClientProtocol(asyncio.Protocol):
    transport: asyncio.BaseTransport | None
    state: str
    capabilities: set[str]

class IMAP4:
    TIMEOUT_SECONDS: float
    timeout: float
    port: int
    host: str
    protocol: IMAP4ClientProtocol | None
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = ...,
        loop: asyncio.AbstractEventLoop | None = None,
        timeout: float = ...,
        conn_lost_cb: Callable[[Exception | None], None] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None: ...
    async def wait_hello_from_server(self) -> None: ...
    async def login(self, user: str, password: str) -> Response: ...
    async def xoauth2(self, user: str, token: bytes) -> Response: ...
    async def logout(self) -> Response: ...
    async def select(self, mailbox: str = "INBOX") -> Response: ...
    async def uid_search(self, *criteria: str, charset: str | None = "utf-8") -> Response: ...
    async def uid(self, command: str, *criteria: str) -> Response: ...
    async def expunge(self) -> Response: ...
    def idle_done(self) -> None: ...
    async def wait_server_push(self, timeout: float = ...) -> Response: ...
    async def idle_start(self, timeout: float = ...) -> Future[Response]: ...
    async def list(self, reference_name: str, mailbox_pattern: Pattern[str]) -> Response: ...
    def has_capability(self, capability: str) -> bool: ...

class IMAP4_SSL(IMAP4):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = ...,
        loop: asyncio.AbstractEventLoop | None = None,
        timeout: float = ...,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None: ...
