"""Discovery against a real MCP server (`MCP-1`) — and the REST surface over it.

These dial an actual stdio MCP server (``tests/mcp_sample_server.py``) through Pydantic
AI's client, so what is proven is the thing that matters: we speak MCP because the library
does, not because a mock said so.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from core.db import init_db, make_engine
from core.vault import Vault
from services.external_tools import ExternalPolicyStore
from services.mcp import McpRegistry
from services.mcp.registry import STATUS_CONNECTED

from ._helpers import client_app
from ._json import arr, json_body, json_list, obj, s

OWNER = "operator"
SAMPLE_SERVER = str(Path(__file__).with_name("mcp_sample_server.py"))


async def _registry():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(Path(tempfile.mkdtemp()) / "keyfile.json")
    await vault.setup("pw")
    return McpRegistry(engine, vault, ExternalPolicyStore(engine))


async def test_registering_a_stdio_server_discovers_its_tools():
    registry = await _registry()

    view = await registry.register(
        OWNER, name="Sample", transport="stdio", command=sys.executable, args=[SAMPLE_SERVER]
    )

    assert view.status == STATUS_CONNECTED, view.last_error
    assert view.last_error is None
    assert sorted(t.name for t in view.tools) == ["add", "echo"]
    assert next(t for t in view.tools if t.name == "echo").description
    # Discovery writes the catalog, never policy: a newly discovered tool is offered to
    # the agent but stays approval-gated until the operator says otherwise (`AE-3.6`).
    assert all(t.enabled and not t.trusted for t in view.tools)


async def test_reconnect_recovers_a_server_that_was_down():
    """`MCP-3` — a server registered while broken reconnects without re-registration."""
    registry = await _registry()
    view = await registry.register(
        OWNER, name="Sample", transport="stdio", command="/nonexistent-binary", args=[]
    )
    assert view.status != STATUS_CONNECTED
    assert view.last_error

    fixed = await registry.update(OWNER, view.id, command=sys.executable, args=[SAMPLE_SERVER])
    assert fixed.status != STATUS_CONNECTED  # an edit alone doesn't dial

    reconnected = await registry.connect(OWNER, view.id)
    assert reconnected.status == STATUS_CONNECTED, reconnected.last_error
    assert reconnected.last_error is None
    assert sorted(t.name for t in reconnected.tools) == ["add", "echo"]


async def test_live_toolsets_hand_back_composable_pydantic_ai_toolsets():
    """The payoff of not hand-rolling a client: a connected server *is* an
    ``AbstractToolset``, so it drops straight into the agent's stack."""
    from pydantic_ai.toolsets import AbstractToolset

    registry = await _registry()
    view = await registry.register(
        OWNER, name="Sample", transport="stdio", command=sys.executable, args=[SAMPLE_SERVER]
    )
    assert view.status == STATUS_CONNECTED, view.last_error

    live = await registry.live_toolsets(OWNER)

    assert len(live) == 1
    server_view, client = live[0]
    assert server_view.id == view.id
    assert isinstance(client, AbstractToolset)
    # Un-prefixed here: the raw names are what discovery and the policy rows key on. The
    # agent-facing `{slug}_` prefix is applied where the tools are composed, so two
    # servers exposing `echo` still can't collide.
    assert client.id == view.slug
    async with client:
        assert sorted(t.name for t in await client.list_tools()) == ["add", "echo"]


async def test_the_rest_surface_registers_lists_and_gates_a_tool():
    async with client_app() as (client, _app):
        created = await client.post(
            "/mcp/servers",
            json={
                "name": "Sample",
                "transport": "stdio",
                "command": sys.executable,
                "args": [SAMPLE_SERVER],
            },
        )
        assert created.status_code == 201, created.text
        server = json_body(created)
        assert server["status"] == "connected", server["lastError"]
        assert sorted(s(obj(t)["name"]) for t in arr(server["tools"])) == ["add", "echo"]
        # The surface speaks camelCase, like every other wired seam — pinned here so a
        # field can't quietly revert to snake_case and strand the screen reading it.
        assert server["authRequired"] is False
        assert server["hasCredentials"] is False

        listed = await client.get("/mcp/servers")
        assert [obj(row)["id"] for row in json_list(listed)] == [server["id"]]

        # `MCP-1` — individual tools can be disabled; `AE-3.6` — and individually trusted.
        disabled = await client.patch(
            f"/mcp/servers/{server['id']}/tools/echo", json={"enabled": False}
        )
        assert json_body(disabled) == {
            "name": "echo",
            "description": json_body(disabled)["description"],
            "enabled": False,
            "trusted": False,
        }
        trusted = await client.patch(
            f"/mcp/servers/{server['id']}/tools/add", json={"trusted": True}
        )
        assert json_body(trusted)["trusted"] is True

        after = obj(json_list(await client.get("/mcp/servers"))[0])
        by_name = {s(obj(t)["name"]): obj(t) for t in arr(after["tools"])}
        # Trusting `add` left `echo` exactly as it was — trust is per tool, not per server.
        assert by_name["add"]["trusted"] is True
        assert by_name["echo"]["trusted"] is False
        assert by_name["echo"]["enabled"] is False

        missing = await client.patch(
            f"/mcp/servers/{server['id']}/tools/nope", json={"trusted": True}
        )
        assert missing.status_code == 404

        assert (await client.delete(f"/mcp/servers/{server['id']}")).status_code == 204
        assert (await client.get("/mcp/servers")).json() == []
