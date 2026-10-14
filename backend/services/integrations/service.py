"""Third-party connectors configured from presets (`INTEG-1`…`INTEG-3`).

Owns the operator's side of a connector: instantiate one from a preset with credentials
stored encrypted (`INTEG-1`), prove those credentials work before anything depends on them
(`INTEG-3`), and perform one of the preset's actions on the operator's behalf (`INTEG-2`).
Whether the *agent* may perform an action without asking is not decided here — that is the
per-tool trust gate in the toolset layer (`AE-3.6`).

Every outbound request is SSRF-guarded (`core/ssrf`) on the fully-resolved URL, and the
resolved URL must still sit under the operator's configured base URL — the model supplies
path parameters, so nothing it passes may be allowed to walk the request somewhere else.

Raises domain errors only (`NotFoundError`, `InvalidInputError`,
`DegradedCapabilityError`, `SSRFError`); `core.http_errors` maps them to HTTP and the
tool decides retry vs degrade.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import DegradedCapabilityError, InvalidInputError, NotFoundError
from core.ssrf import assert_public_url
from core.vault import Vault, VaultLocked
from models._fields import utcnow
from models.external_tool import Integration
from services.external_policy import ExternalPolicyStore, ToolPolicy, tool_slug

from .presets import PRESETS, IntegrationAction, IntegrationPreset, action, preset

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_UNTESTED = "untested"
STATUS_ERROR = "error"

DEFAULT_TIMEOUT_S = 20.0
# How much of a connector's response is handed back. A connector is a data source, not a
# file store; an unbounded body would blow a turn's context on one call.
MAX_BODY_CHARS = 20_000


@dataclass(frozen=True)
class IntegrationActionView:
    """One action, with the operator's decision about it."""

    name: str
    method: str
    path: str
    description: str
    takes_body: bool
    enabled: bool
    trusted: bool


@dataclass(frozen=True)
class IntegrationView:
    """A configured connector as the surface sees it — never carrying the credential."""

    id: str
    name: str
    slug: str
    preset: str
    category: str
    description: str
    base_url: str
    credential_required: bool
    configured: bool
    enabled: bool
    status: str
    last_error: str | None
    last_tested_at: datetime | None
    actions: list[IntegrationActionView]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IntegrationResponse:
    """What one action returned. A non-2xx is reported, not raised — the status and body
    are exactly what tells the operator (or the model) what went wrong."""

    status: int
    ok: bool
    body: str


class IntegrationService:
    def __init__(
        self,
        db_engine: Engine,
        vault: Vault,
        policy: ExternalPolicyStore,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._db = db_engine
        self._vault = vault
        self._policy = policy
        self._http = http_client
        self._timeout = timeout_s

    @property
    def policy(self) -> ExternalPolicyStore:
        return self._policy

    @staticmethod
    def presets() -> tuple[IntegrationPreset, ...]:
        return PRESETS

    # --- reads ---------------------------------------------------------------------

    async def list(self, owner_id: str) -> list[IntegrationView]:
        rows = await self._rows(owner_id)
        # One policy read for the whole listing — see the same call in the MCP registry:
        # this sits on the agent's toolset-assembly path, so a query per row is a query
        # per row on every run.
        policies = await self._policy.snapshots(owner_id, "integration", [r.id for r in rows])
        views = (self._view(row, policies.get(row.id, {})) for row in rows)
        return [view for view in views if view is not None]

    async def get(self, owner_id: str, integration_id: str) -> IntegrationView:
        row = await self._row(owner_id, integration_id)
        view = self._view(row, await self._policy.snapshot(owner_id, "integration", row.id))
        if view is None:
            raise NotFoundError(f"connector {row.name!r} names an unknown preset")
        return view

    # --- writes --------------------------------------------------------------------

    async def configure(
        self,
        owner_id: str,
        preset_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> IntegrationView:
        """Instantiate a connector from a preset, sealing whatever credential it was
        given (`INTEG-1`). Nothing is tested here — the operator decides when to prove the
        credential, and the connector reads ``untested`` until they do."""
        found = preset(preset_id)
        if found is None:
            raise NotFoundError(f"unknown connector preset {preset_id!r}")
        label = (name or found.name).strip()
        if not label:
            raise InvalidInputError("a connector name is required")
        row = Integration(
            owner_id=owner_id,
            preset=preset_id,
            name=label,
            slug=await self._unique_slug(owner_id, label),
            base_url=(base_url or found.base_url).rstrip("/"),
            credentials_enc=self._seal(credentials),
        )

        def work(session: Session) -> str:
            session.add(row)
            session.flush()
            return row.id

        return await self.get(owner_id, await in_session(self._db, work))

    async def update(
        self,
        owner_id: str,
        integration_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        credentials: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> IntegrationView:
        """Amend a connector. Changing the base URL or the credential invalidates the
        last test result — what was proven was proven about the old configuration."""
        credentials_enc = self._seal(credentials) if credentials is not None else None

        def work(session: Session) -> None:
            row = self._select(session, owner_id, integration_id)
            if name is not None and name.strip():
                row.name = name.strip()
            if base_url is not None:
                row.base_url = base_url.rstrip("/")
            if credentials is not None:
                row.credentials_enc = credentials_enc
            if base_url is not None or credentials is not None:
                row.status = STATUS_UNTESTED
                row.last_error = None
                row.last_tested_at = None
            if enabled is not None:
                row.enabled = enabled
            row.updated_at = utcnow()
            session.add(row)

        await in_session(self._db, work)
        return await self.get(owner_id, integration_id)

    async def remove(self, owner_id: str, integration_id: str) -> None:
        """Delete a connector and every per-action decision made about it."""

        def work(session: Session) -> None:
            session.delete(self._select(session, owner_id, integration_id))

        await in_session(self._db, work)
        await self._policy.forget_source(owner_id, "integration", integration_id)

    async def set_action_policy(
        self,
        owner_id: str,
        integration_id: str,
        action_name: str,
        *,
        enabled: bool | None = None,
        trusted: bool | None = None,
    ) -> ToolPolicy:
        """Enable/disable an action, or mark it trusted / revoke that trust (`AE-3.6`).
        Per action, never per connector."""
        row = await self._row(owner_id, integration_id)
        if action(row.preset, action_name) is None:
            raise NotFoundError(f"connector {row.name!r} has no action named {action_name!r}")
        return await self._policy.set(
            owner_id, "integration", integration_id, action_name, enabled=enabled, trusted=trusted
        )

    # --- outbound ------------------------------------------------------------------

    async def test(self, owner_id: str, integration_id: str) -> IntegrationView:
        """Prove the credential works before anything relies on it (`INTEG-3`).

        Like an MCP connect, a failure is recorded on the row rather than raised: "it
        doesn't work, and here is what the service said" is the answer the operator asked
        for.
        """
        row = await self._row(owner_id, integration_id)
        found = self._preset_for(row)
        try:
            response = await self._request(row, found, found.test_path, "GET", {}, None)
            error = None if response.ok else f"HTTP {response.status}: {_first_line(response.body)}"
        except Exception as exc:  # noqa: BLE001 - any outbound failure is a status
            logger.info("integrations: test of %r failed: %s", row.name, exc)
            error = _reason(exc)
        await self._record_test(integration_id, error)
        return await self.get(owner_id, integration_id)

    async def call(
        self,
        owner_id: str,
        integration_id: str,
        action_name: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> IntegrationResponse:
        """Perform one of the preset's actions on the operator's behalf (`INTEG-2`).

        Whether this call needed approval was decided before it got here (`AE-3.6`); this
        layer only refuses what is structurally impossible — a disabled connector or
        action, an unknown action, a body on a GET.
        """
        row = await self._row(owner_id, integration_id)
        if not row.enabled:
            raise DegradedCapabilityError(f"connector {row.name!r} is disabled")
        found = self._preset_for(row)
        chosen = action(row.preset, action_name)
        if chosen is None:
            raise NotFoundError(f"connector {row.name!r} has no action named {action_name!r}")
        decision = await self._policy.get(owner_id, "integration", integration_id, action_name)
        if not decision.enabled:
            raise DegradedCapabilityError(f"action {action_name!r} on {row.name!r} is disabled")
        if body and not chosen.takes_body:
            raise InvalidInputError(f"action {action_name!r} does not take a body")
        path, query = _fill_path(chosen, params or {})
        return await self._request(row, found, path, chosen.method, query, body)

    async def live_connectors(self, owner_id: str) -> list[IntegrationView]:
        """The enabled connectors the agent may be offered. A connector with a required
        credential it hasn't been given contributes nothing — offering the model a tool
        that can only 401 wastes a turn."""
        return [
            view
            for view in await self.list(owner_id)
            if view.enabled and (view.configured or not view.credential_required)
        ]

    # --- internals -----------------------------------------------------------------

    async def _request(
        self,
        row: Integration,
        found: IntegrationPreset,
        path: str,
        method: str,
        query: dict[str, str],
        body: dict[str, Any] | None,
    ) -> IntegrationResponse:
        url = _join(row.base_url, path)
        # The model supplies path parameters, so the *resolved* URL is what must be
        # checked — both that it still belongs to the operator's connector and that it
        # points somewhere publicly routable.
        _assert_within(row.base_url, url)
        await assert_public_url(url)

        headers = {"Accept": "application/json", **dict(found.extra_headers)}
        headers.update(self._auth_headers(found, self._open(row.credentials_enc)))
        client = self._http or httpx.AsyncClient()
        try:
            response = await client.request(
                method,
                url,
                params=query or None,
                json=body if body else None,
                headers=headers,
                timeout=self._timeout,
                # A redirect could leave the operator's connector entirely, and the
                # credential would ride along; refuse rather than follow.
                follow_redirects=False,
            )
        finally:
            if self._http is None:
                await client.aclose()
        text = response.text or ""
        return IntegrationResponse(
            status=response.status_code,
            ok=200 <= response.status_code < 300,
            body=text[:MAX_BODY_CHARS],
        )

    @staticmethod
    def _auth_headers(
        found: IntegrationPreset, credentials: dict[str, Any] | None
    ) -> dict[str, str]:
        """The preset decides *where* a credential goes; the operator supplied *what*."""
        if not credentials:
            return {}
        token = credentials.get("token")
        if found.auth == "bearer" and token:
            return {"Authorization": f"Bearer {token}"}
        if found.auth == "header" and token and found.header_name:
            return {found.header_name: str(token)}
        if found.auth == "basic":
            username = credentials.get("username") or ""
            password = credentials.get("password") or token or ""
            if username or password:
                raw = f"{username}:{password}".encode()
                return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
        return {}

    async def _unique_slug(self, owner_id: str, label: str) -> str:
        base = tool_slug(label)
        taken = {row.slug for row in await self._rows(owner_id)}
        if base not in taken:
            return base
        for suffix in range(2, 100):
            candidate = f"{base}_{suffix}"
            if candidate not in taken:
                return candidate
        raise InvalidInputError(f"too many connectors named like {label!r}")

    async def _rows(self, owner_id: str) -> list[Integration]:
        def work(session: Session) -> list[Integration]:
            return list(
                session.exec(
                    select(Integration)
                    .where(Integration.owner_id == owner_id)
                    .order_by(col(Integration.created_at))
                ).all()
            )

        return await in_session(self._db, work)

    async def _row(self, owner_id: str, integration_id: str) -> Integration:
        def work(session: Session) -> Integration:
            return self._select(session, owner_id, integration_id)

        return await in_session(self._db, work)

    @staticmethod
    def _select(session: Session, owner_id: str, integration_id: str) -> Integration:
        row = session.exec(
            select(Integration)
            .where(Integration.owner_id == owner_id)
            .where(Integration.id == integration_id)
        ).first()
        if row is None:
            raise NotFoundError(f"connector {integration_id!r} not found")
        return row

    @staticmethod
    def _preset_for(row: Integration) -> IntegrationPreset:
        found = preset(row.preset)
        if found is None:
            raise NotFoundError(f"connector {row.name!r} names an unknown preset")
        return found

    async def _record_test(self, integration_id: str, error: str | None) -> None:
        now = utcnow()

        def work(session: Session) -> None:
            row = session.exec(select(Integration).where(Integration.id == integration_id)).first()
            if row is None:  # pragma: no cover - removed mid-test
                return
            row.status = STATUS_OK if error is None else STATUS_ERROR
            row.last_error = error
            row.last_tested_at = now
            row.updated_at = now
            session.add(row)

        await in_session(self._db, work)

    def _seal(self, payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        return self._vault.encrypt_str(json.dumps(payload))

    def _open(self, sealed: str | None) -> dict[str, Any] | None:
        if not sealed:
            return None
        try:
            parsed = json.loads(self._vault.decrypt_str(sealed))
        except VaultLocked:
            return None
        except TypeError, ValueError:
            logger.warning("integrations: stored credentials could not be read")
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _view(row: Integration, policies: dict[str, ToolPolicy]) -> IntegrationView | None:
        found = preset(row.preset)
        if found is None:
            return None
        return IntegrationView(
            id=row.id,
            name=row.name,
            slug=row.slug,
            preset=row.preset,
            category=found.category,
            description=found.description,
            base_url=row.base_url,
            credential_required=found.credential_required,
            configured=row.credentials_enc is not None or not found.credential_required,
            enabled=row.enabled,
            status=row.status,
            last_error=row.last_error,
            last_tested_at=row.last_tested_at,
            actions=[
                IntegrationActionView(
                    name=a.name,
                    method=a.method,
                    path=a.path,
                    description=a.description,
                    takes_body=a.takes_body,
                    enabled=policies.get(a.name, ToolPolicy()).enabled,
                    trusted=policies.get(a.name, ToolPolicy()).trusted,
                )
                for a in found.actions
            ],
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _fill_path(chosen: IntegrationAction, params: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Substitute the action's ``{placeholder}`` segments from ``params``; whatever is
    left over becomes the query string.

    Values are percent-encoded with ``/`` reserved, so a parameter can never add a path
    segment — the action's shape is fixed by the preset, not by what the model passes.
    """
    path = chosen.path
    remaining = dict(params)
    for key, value in params.items():
        token = "{" + key + "}"
        if token in path:
            path = path.replace(token, quote(str(value), safe=""))
            remaining.pop(key, None)
    if "{" in path:
        missing = path[path.index("{") + 1 : path.index("}")] if "}" in path else path
        raise InvalidInputError(f"action {chosen.name!r} needs a {missing!r} parameter")
    return path, {k: str(v) for k, v in remaining.items()}


def _join(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _assert_within(base_url: str, url: str) -> None:
    """The resolved URL must still be the operator's connector — same scheme, host and
    port, and still under its path prefix. Path parameters come from the model, so this
    is the check that keeps one from walking the request off the configured service."""
    base, target = urlsplit(base_url.rstrip("/")), urlsplit(url)
    if (base.scheme, base.netloc) != (target.scheme, target.netloc):
        raise InvalidInputError(f"refused to call {url!r}: outside {base_url!r}")
    prefix = base.path or ""
    if prefix and not target.path.startswith(prefix):
        raise InvalidInputError(f"refused to call {url!r}: outside {base_url!r}")
    if ".." in target.path.split("/"):
        raise InvalidInputError(f"refused to call {url!r}: path traversal")


def _first_line(body: str) -> str:
    return (body.strip().splitlines() or [""])[0][:200]


def _reason(exc: Exception) -> str:
    """An outbound failure in one operator-legible line — never a traceback, and never
    the credential a client's repr might carry."""
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "the service did not answer in time"
    text = str(exc).strip()
    return text or exc.__class__.__name__
