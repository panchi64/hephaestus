"""The MCP server registry: registration, credentials at rest, per-tool policy, lifecycle.

Covers `MCP-1` (register, enable/disable individual tools) and `MCP-3` (reconnect,
disable, remove, servers needing third-party authorization) at the service layer; the
live protocol handshake is exercised separately in ``test_mcp_discovery``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select

from core.db import init_db, make_engine
from core.exceptions import InvalidInputError, NotFoundError
from core.vault import Vault
from models.external_tool import McpServer
from services.external_tools import ExternalPolicyStore, tool_slug
from services.mcp import McpRegistry, McpServerView, auth_headers
from services.mcp.registry import STATUS_DISCONNECTED, STATUS_ERROR

OWNER = "operator"


async def _registry() -> tuple[Engine, Vault, McpRegistry]:
    """A registry over a throwaway in-memory DB with an unlocked vault."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return engine, vault, McpRegistry(engine, vault, ExternalPolicyStore(engine))


async def _register_unreachable(registry: McpRegistry, name: str = "Remote") -> McpServerView:
    """Register an http server pointed at a closed port — registration always attempts a
    connect, and here it is expected to fail, which is the state most tests want."""
    return await registry.register(
        OWNER,
        name=name,
        transport="http",
        url="http://127.0.0.1:1/mcp",
    )


async def test_register_records_the_server_and_its_failed_connect():
    _engine, _vault, registry = await _registry()

    view = await _register_unreachable(registry)

    assert view.name == "Remote"
    assert view.transport == "http"
    assert view.enabled is True
    # Registration dials immediately, so the operator sees the outcome from one action;
    # an unreachable server is a status carrying a reason, never a raised error.
    assert view.status == STATUS_ERROR
    assert view.last_error
    assert view.last_error_at is not None
    assert view.tools == []
    assert [v.id for v in await registry.list(OWNER)] == [view.id]


async def test_register_rejects_a_shape_that_cannot_be_dialled():
    _engine, _vault, registry = await _registry()

    with pytest.raises(InvalidInputError):
        _ = await registry.register(OWNER, name="No command", transport="stdio")
    with pytest.raises(InvalidInputError):
        _ = await registry.register(OWNER, name="No url", transport="http")
    with pytest.raises(InvalidInputError):
        _ = await registry.register(OWNER, name="Bad", transport="carrier-pigeon", url="x")
    with pytest.raises(InvalidInputError):
        _ = await registry.register(OWNER, name="  ", transport="http", url="http://x/mcp")


async def test_slugs_stay_unique_so_two_servers_never_share_a_tool_namespace():
    _engine, _vault, registry = await _registry()

    first = await _register_unreachable(registry, name="Search")
    second = await _register_unreachable(registry, name="search")

    assert first.slug == tool_slug("Search")
    assert second.slug != first.slug
    assert second.slug.startswith(first.slug)


async def test_credentials_and_env_are_sealed_at_rest_and_never_returned():
    """`MCP-3` covers servers needing third-party authorization: the token is stored, but
    it is stored encrypted and the surface only ever learns that it exists."""
    engine, vault, registry = await _registry()

    view = await registry.register(
        OWNER,
        name="Authed",
        transport="http",
        url="http://127.0.0.1:1/mcp",
        auth_required=True,
        credentials={"method": "bearer", "token": "s3cret-token"},
    )

    assert view.auth_required is True
    assert view.has_credentials is True
    with Session(engine) as session:
        row = session.exec(select(McpServer).where(McpServer.id == view.id)).one()
    assert row.auth_enc is not None
    assert "s3cret-token" not in row.auth_enc
    assert json.loads(vault.decrypt_str(row.auth_enc))["token"] == "s3cret-token"
    # The view carries no field that could leak the value.
    assert "s3cret-token" not in json.dumps(view.__dict__, default=str)


async def test_stdio_env_is_sealed_but_its_key_names_stay_visible():
    engine, _vault, registry = await _registry()

    view = await registry.register(
        OWNER,
        name="Local",
        transport="stdio",
        command="/bin/false",
        args=["--serve"],
        env={"API_TOKEN": "abc123", "MODE": "test"},
    )

    assert view.env_keys == ["API_TOKEN", "MODE"]
    assert view.args == ["--serve"]
    with Session(engine) as session:
        row = session.exec(select(McpServer).where(McpServer.id == view.id)).one()
    assert row.env_enc is not None and "abc123" not in row.env_enc


async def test_update_can_disable_a_server_without_re_keying_its_tools():
    _engine, _vault, registry = await _registry()
    view = await _register_unreachable(registry, name="Remote")

    renamed = await registry.update(OWNER, view.id, name="Renamed", enabled=False)

    assert renamed.name == "Renamed"
    assert renamed.enabled is False
    # A disabled server can't still claim to be connected...
    assert renamed.status == STATUS_DISCONNECTED
    # ...and the slug is frozen, so the operator's per-tool decisions keep their key.
    assert renamed.slug == view.slug


async def test_remove_drops_the_server_and_every_decision_made_about_it():
    engine, _vault, registry = await _registry()
    policy = ExternalPolicyStore(engine)
    view = await _register_unreachable(registry)
    _ = await policy.set(OWNER, "mcp", view.id, "echo", trusted=True)

    await registry.remove(OWNER, view.id)

    assert await registry.list(OWNER) == []
    # Trust must not survive its server — a later registration reusing the id would
    # otherwise inherit an approval the operator granted to something else.
    assert await policy.snapshot(OWNER, "mcp", view.id) == {}
    with pytest.raises(NotFoundError):
        _ = await registry.get(OWNER, view.id)


async def test_tool_policy_only_applies_to_a_tool_the_server_actually_exposes():
    engine, _vault, registry = await _registry()
    view = await _register_unreachable(registry)
    # Stand in for a successful discovery without needing a live server.
    with Session(engine) as session:
        row = session.exec(select(McpServer).where(McpServer.id == view.id)).one()
        row.tools_json = json.dumps([{"name": "echo", "description": "Echo."}])
        session.add(row)
        session.commit()

    policy = await registry.set_tool_policy(OWNER, view.id, "echo", enabled=False)
    assert policy.enabled is False
    # Toggling `enabled` leaves the (untrusted) default alone.
    assert policy.trusted is False

    with pytest.raises(NotFoundError):
        _ = await registry.set_tool_policy(OWNER, view.id, "not-a-tool", trusted=True)


async def test_a_disabled_or_unconnected_server_offers_the_agent_nothing():
    engine, _vault, registry = await _registry()
    view = await _register_unreachable(registry)

    # Never-connected (the registration attempt failed) ⇒ not offered.
    assert await registry.live_toolsets(OWNER) == []

    # Connected but disabled ⇒ still not offered (`MCP-3`).
    with Session(engine) as session:
        row = session.exec(select(McpServer).where(McpServer.id == view.id)).one()
        row.status = "connected"
        row.enabled = False
        session.add(row)
        session.commit()
    assert await registry.live_toolsets(OWNER) == []

    with Session(engine) as session:
        row = session.exec(select(McpServer).where(McpServer.id == view.id)).one()
        row.enabled = True
        session.add(row)
        session.commit()
    live = await registry.live_toolsets(OWNER)
    assert [v.id for v, _client in live] == [view.id]


def test_credentials_become_transport_auth():
    assert auth_headers({"method": "bearer", "token": "t"}) == {"Authorization": "Bearer t"}
    assert auth_headers({"method": "api_key", "token": "k"}) == {"X-API-Key": "k"}
    assert auth_headers({"method": "basic", "username": "u", "password": "p"}) == {
        "Authorization": "Basic dTpw"
    }
    # Incomplete credentials produce no header rather than a malformed one.
    assert auth_headers({"method": "bearer"}) == {}
    assert auth_headers(None) == {}
