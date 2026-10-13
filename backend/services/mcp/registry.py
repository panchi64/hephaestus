"""The registry of external MCP tool servers (`MCP-1`, `MCP-3`).

Owns the operator's side of an MCP connection: register a server, dial it and discover
what it exposes, keep the last outcome so the surface can say *why* something is down,
reconnect, disable, and remove. It does **not** own the protocol — ``client.py`` hands
back a Pydantic AI MCP toolset and that library speaks MCP.

Two properties worth keeping in mind when changing this:

- **Discovery writes catalog, never policy.** A connect refreshes the cached tool list; it
  never creates or flips an :class:`~models.external_tool.ExternalToolPolicy` row. Trust
  is only ever an operator action (`AE-3.6`), so a server that starts exposing a new tool
  gets that tool approval-gated by default rather than inheriting anything.
- **A dead server is a status, not an exception.** ``connect`` records the failure on the
  row and returns; nothing about one unreachable server may break the surface, and the
  agent path skips servers that aren't currently connected.

Raises domain errors only (`NotFoundError`, `InvalidInputError`,
`DegradedCapabilityError`); `core.http_errors` maps them to HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic_ai.mcp import MCPToolset
from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import in_session
from core.exceptions import DegradedCapabilityError, InvalidInputError, NotFoundError
from core.vault import Vault, VaultLocked
from models._fields import utcnow
from models.external_tool import McpServer
from services.external_tools import ExternalPolicyStore, ToolPolicy, tool_slug

from .client import DEFAULT_CONNECT_TIMEOUT_S, TRANSPORTS, build_client

logger = logging.getLogger(__name__)

STATUS_CONNECTED = "connected"
STATUS_DISCONNECTED = "disconnected"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class McpToolView:
    """One tool a server exposes, with the operator's decision about it."""

    name: str
    description: str
    enabled: bool
    trusted: bool


@dataclass(frozen=True)
class McpServerView:
    """A registered server as the surface sees it — never carrying a secret. Credentials
    and environment reduce to ``has_credentials``/``env_keys``: the operator can see that
    something is configured, and which variables are set, without the values coming back
    out of the vault on a list call."""

    id: str
    name: str
    slug: str
    transport: str
    url: str | None
    command: str | None
    args: list[str]
    env_keys: list[str]
    enabled: bool
    status: str
    auth_required: bool
    has_credentials: bool
    last_error: str | None
    last_error_at: datetime | None
    tools: list[McpToolView]
    created_at: datetime
    updated_at: datetime


class McpRegistry:
    def __init__(
        self,
        db_engine: Engine,
        vault: Vault,
        policy: ExternalPolicyStore,
        *,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    ) -> None:
        self._db = db_engine
        self._vault = vault
        self._policy = policy
        self._timeout = connect_timeout_s

    @property
    def policy(self) -> ExternalPolicyStore:
        """The shared per-tool enable/trust store, so callers that already hold the
        registry don't need a second handle wired alongside it."""
        return self._policy

    # --- reads ---------------------------------------------------------------------

    async def list(self, owner_id: str) -> list[McpServerView]:
        rows = await self._rows(owner_id)
        # One policy read for the whole listing. This runs while the agent's toolset is
        # being assembled, so a snapshot per server was a query per server per run.
        policies = await self._policy.snapshots(owner_id, "mcp", [r.id for r in rows])
        return [self._view(row, policies.get(row.id, {})) for row in rows]

    async def get(self, owner_id: str, server_id: str) -> McpServerView:
        row = await self._row(owner_id, server_id)
        return self._view(row, await self._policy.snapshot(owner_id, "mcp", row.id))

    # --- writes --------------------------------------------------------------------

    async def register(
        self,
        owner_id: str,
        *,
        name: str,
        transport: str,
        command: str | None = None,
        args: Sequence[str] = (),
        env: dict[str, str] | None = None,
        url: str | None = None,
        auth_required: bool = False,
        credentials: dict[str, Any] | None = None,
    ) -> McpServerView:
        """Register a server and immediately try to connect it, so the operator sees the
        discovered tools (or the reason there are none) from the one action (`MCP-1`)."""
        name = name.strip()
        if not name:
            raise InvalidInputError("a server name is required")
        self._validate_shape(transport, command, url)
        slug = await self._unique_slug(owner_id, name)
        row = McpServer(
            owner_id=owner_id,
            name=name,
            slug=slug,
            transport=transport,
            command=command,
            args_json=json.dumps(list(args)),
            env_enc=self._seal(env),
            url=url,
            auth_required=auth_required,
            auth_enc=self._seal(credentials),
        )

        def work(session: Session) -> str:
            session.add(row)
            session.flush()
            return row.id

        server_id = await in_session(self._db, work)
        return await self.connect(owner_id, server_id)

    async def update(
        self,
        owner_id: str,
        server_id: str,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        url: str | None = None,
        command: str | None = None,
        args: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
        auth_required: bool | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> McpServerView:
        """Amend a registration (`MCP-3`). ``slug`` is deliberately *not* re-derived from
        a new name — the tools the model and the policy rows already know are keyed by it,
        and silently re-keying them would drop the operator's trust decisions."""
        env_enc = self._seal(env) if env is not None else None
        auth_enc = self._seal(credentials) if credentials is not None else None

        def work(session: Session) -> None:
            row = self._select(session, owner_id, server_id)
            if name is not None and name.strip():
                row.name = name.strip()
            if enabled is not None:
                row.enabled = enabled
                if not enabled:
                    # A disabled server is not connected, so its status must not keep
                    # claiming it is — the surface would show a live server the agent
                    # can't see.
                    row.status = STATUS_DISCONNECTED
            if url is not None:
                row.url = url
            if command is not None:
                row.command = command
            if args is not None:
                row.args_json = json.dumps(list(args))
            if env is not None:
                row.env_enc = env_enc
            if auth_required is not None:
                row.auth_required = auth_required
            if credentials is not None:
                row.auth_enc = auth_enc
            row.updated_at = utcnow()
            session.add(row)

        await in_session(self._db, work)
        return await self.get(owner_id, server_id)

    async def remove(self, owner_id: str, server_id: str) -> None:
        """Delete a server and every per-tool decision made about it, so a later
        registration can never inherit trust granted to the server it replaced."""

        def work(session: Session) -> None:
            session.delete(self._select(session, owner_id, server_id))

        await in_session(self._db, work)
        await self._policy.forget_source(owner_id, "mcp", server_id)

    async def set_tool_policy(
        self,
        owner_id: str,
        server_id: str,
        tool_name: str,
        *,
        enabled: bool | None = None,
        trusted: bool | None = None,
    ) -> ToolPolicy:
        """Enable/disable (`MCP-1`) or trust/untrust (`AE-3.6`) one tool. The server must
        exist and must actually expose the tool, so a typo can't leave a trust record
        floating against nothing."""
        row = await self._row(owner_id, server_id)
        if tool_name not in {t["name"] for t in _discovered(row)}:
            raise NotFoundError(f"server {row.name!r} exposes no tool named {tool_name!r}")
        return await self._policy.set(
            owner_id, "mcp", server_id, tool_name, enabled=enabled, trusted=trusted
        )

    # --- connection ----------------------------------------------------------------

    async def connect(self, owner_id: str, server_id: str) -> McpServerView:
        """Dial the server, discover its tools, and record the outcome (`MCP-1`, `MCP-3`).

        Always returns a view: a failure is written to ``status``/``last_error`` rather
        than raised, because "this server is down and here is why" is exactly what the
        operator asked for by pressing reconnect.
        """
        row = await self._row(owner_id, server_id)
        try:
            discovered = await self._discover(row)
        except Exception as exc:  # noqa: BLE001 - any transport/protocol failure is a status
            logger.info("mcp: connect to %r failed: %s", row.name, exc)
            await self._record(server_id, error=_reason(exc))
            return await self.get(owner_id, server_id)
        await self._record(server_id, tools=discovered)
        return await self.get(owner_id, server_id)

    async def _discover(self, row: McpServer) -> list[dict[str, str]]:
        """Open the connection just long enough to list what the server exposes."""
        client = build_client(
            row,
            env=self._open(row.env_enc),
            credentials=self._open(row.auth_enc),
            timeout_s=self._timeout,
        )
        async with asyncio.timeout(self._timeout):
            async with client:
                tools = await client.list_tools()
        return [{"name": t.name, "description": (t.description or "").strip()} for t in tools]

    async def live_toolsets(self, owner_id: str) -> list[tuple[McpServerView, MCPToolset[Any]]]:
        """The connected, enabled servers as ready-to-compose Pydantic AI toolsets, each
        paired with its view so the caller can map a tool back to its server.

        Only servers whose last connect succeeded are offered. A server the operator has
        registered but never connected contributes nothing until they connect it — which
        keeps an agent run from paying a dial timeout for a server that has never worked,
        while a server that has since gone down simply fails to enter and is skipped by
        the caller.
        """
        pairs: list[tuple[McpServerView, MCPToolset[Any]]] = []
        for row in await self._rows(owner_id):
            if not row.enabled or row.status != STATUS_CONNECTED:
                continue
            try:
                client = build_client(
                    row,
                    env=self._open(row.env_enc),
                    credentials=self._open(row.auth_enc),
                    timeout_s=self._timeout,
                )
            except DegradedCapabilityError as exc:
                logger.info("mcp: skipping %r — %s", row.name, exc)
                continue
            policies = await self._policy.snapshot(owner_id, "mcp", row.id)
            pairs.append((self._view(row, policies), client))
        return pairs

    # --- internals -----------------------------------------------------------------

    @staticmethod
    def _validate_shape(transport: str, command: str | None, url: str | None) -> None:
        if transport not in TRANSPORTS:
            raise InvalidInputError(
                f"unknown transport {transport!r} (expected one of {', '.join(TRANSPORTS)})"
            )
        if transport == "stdio" and not command:
            raise InvalidInputError("a stdio server needs a command to run")
        if transport != "stdio" and not url:
            raise InvalidInputError(f"an {transport} server needs a URL")

    async def _unique_slug(self, owner_id: str, name: str) -> str:
        """A slug no other of this owner's servers holds — two servers both called
        "Search" must not produce two ``search_*`` tool namespaces."""
        base = tool_slug(name)
        taken = {row.slug for row in await self._rows(owner_id)}
        if base not in taken:
            return base
        for suffix in range(2, 100):
            candidate = f"{base}_{suffix}"
            if candidate not in taken:
                return candidate
        raise InvalidInputError(f"too many servers named like {name!r}")

    async def _rows(self, owner_id: str) -> list[McpServer]:
        def work(session: Session) -> list[McpServer]:
            return list(
                session.exec(
                    select(McpServer)
                    .where(McpServer.owner_id == owner_id)
                    .order_by(McpServer.created_at)
                ).all()
            )

        return await in_session(self._db, work)

    async def _row(self, owner_id: str, server_id: str) -> McpServer:
        def work(session: Session) -> McpServer:
            return self._select(session, owner_id, server_id)

        return await in_session(self._db, work)

    @staticmethod
    def _select(session: Session, owner_id: str, server_id: str) -> McpServer:
        row = session.exec(
            select(McpServer).where(McpServer.owner_id == owner_id).where(McpServer.id == server_id)
        ).first()
        if row is None:
            raise NotFoundError(f"MCP server {server_id!r} not found")
        return row

    async def _record(
        self,
        server_id: str,
        *,
        tools: list[dict[str, str]] | None = None,
        error: str | None = None,
    ) -> None:
        now = utcnow()

        def work(session: Session) -> None:
            row = session.exec(select(McpServer).where(McpServer.id == server_id)).first()
            if row is None:  # pragma: no cover - removed between connect and record
                return
            if error is None:
                row.status = STATUS_CONNECTED
                row.tools_json = json.dumps(tools or [])
                row.last_error = None
                row.last_error_at = None
            else:
                row.status = STATUS_ERROR
                row.last_error = error
                row.last_error_at = now
            row.updated_at = now
            session.add(row)

        await in_session(self._db, work)

    def _seal(self, payload: dict[str, Any] | None) -> str | None:
        """Encrypt a credential/environment map, or clear it when the operator sent an
        empty one."""
        if not payload:
            return None
        return self._vault.encrypt_str(json.dumps(payload))

    def _open(self, sealed: str | None) -> dict[str, Any] | None:
        """Decrypt a sealed map, or ``None`` when there is none — or when the vault is
        locked, in which case the connection is simply attempted unauthenticated and the
        server's own 401 becomes the operator-legible failure."""
        if not sealed:
            return None
        try:
            parsed = json.loads(self._vault.decrypt_str(sealed))
        except VaultLocked:
            return None
        except TypeError, ValueError:
            logger.warning("mcp: stored credentials could not be read")
            return None
        return parsed if isinstance(parsed, dict) else None

    def _view(self, row: McpServer, policies: dict[str, ToolPolicy]) -> McpServerView:
        tools = [
            McpToolView(
                name=tool["name"],
                description=tool.get("description", ""),
                enabled=policies.get(tool["name"], ToolPolicy()).enabled,
                trusted=policies.get(tool["name"], ToolPolicy()).trusted,
            )
            for tool in _discovered(row)
        ]
        return McpServerView(
            id=row.id,
            name=row.name,
            slug=row.slug,
            transport=row.transport,
            url=row.url,
            command=row.command,
            args=json.loads(row.args_json) if row.args_json else [],
            # Names only — the operator needs to see *which* variables are set without
            # their values leaving the vault on a list call.
            env_keys=sorted(self._open(row.env_enc) or {}),
            enabled=row.enabled,
            status=row.status,
            auth_required=row.auth_required,
            has_credentials=row.auth_enc is not None,
            last_error=row.last_error,
            last_error_at=row.last_error_at,
            tools=tools,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _discovered(row: McpServer) -> list[dict[str, str]]:
    """The cached tool catalog, defensively decoded."""
    try:
        parsed = json.loads(row.tools_json or "[]")
    except TypeError, ValueError:
        return []
    return [t for t in parsed if isinstance(t, dict) and "name" in t]


def _reason(exc: Exception) -> str:
    """A connect failure in one operator-legible line — never a traceback, and never the
    credentials that may appear in a client's repr."""
    if isinstance(exc, TimeoutError):
        return "the server did not answer in time"
    text = str(exc).strip()
    return text or exc.__class__.__name__
