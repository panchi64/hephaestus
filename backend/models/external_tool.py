"""External tool schema — MCP servers, connectors, and per-tool trust (`MCP-*`, `INTEG-*`,
`AE-3.6`).

Three entities, one idea: the system can reach tools it did not write, so what it stores
about them splits cleanly into **connection** (how to reach the server/connector, whose
secrets are sealed) and **policy** (what the operator has allowed, which must stay
queryable).

- :class:`McpServer` — a registered external tool server. Its address/transport are
  structural and stay in the clear so the surface can list and index them; anything that
  authenticates (``env``, the auth credentials) is vault-sealed.
- :class:`Integration` — a connector instantiated from a static preset. Same split: the
  base URL is structural, the credentials are sealed.
- :class:`ExternalToolPolicy` — the operator's decision about **one tool**: whether it is
  offered to the agent at all (`MCP-1`'s per-tool enable/disable) and whether it runs
  without pausing for approval (`AE-3.6` trust). Like ``ApprovalGrant`` this is *policy
  rather than content*, so it is stored in the clear and indexed.

Trust is deliberately keyed **per tool, not per server**: registering or enabling a server
says nothing about what any tool on it may do, so a server-level trust flag would blanket-
approve tools the operator has never seen. A missing policy row means the safe default —
offered to the agent, but approval-gated.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class McpServer(Table, table=True):
    __tablename__ = "mcp_servers"
    # One server per (owner, name): the name is the operator-visible label and the seed
    # of `slug`, so duplicates would make two servers indistinguishable in the UI.
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_mcp_servers_owner_name"),
        UniqueConstraint("owner_id", "slug", name="uq_mcp_servers_owner_slug"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # Operator-chosen label, in the clear (listed and sorted by the surface).
    name: str = Field(index=True)
    # The stable, name-safe prefix this server's tools carry inside the `external`
    # category, so two servers exposing the same tool name never collide. Derived from
    # `name` at registration and then frozen — renaming a server must not silently
    # re-key the tools the model already knows.
    slug: str
    # "stdio" | "sse" | "http" (Streamable HTTP). Chooses which Pydantic AI MCP client
    # class connects the server.
    transport: str
    # stdio only: the executable and its argv (JSON list, in the clear — a command line
    # is structural, and the operator must be able to read back what they registered).
    command: str | None = None
    args_json: str = "[]"
    # stdio only: sealed JSON object of environment variables. Environment is where a
    # stdio server's secrets live (API keys, tokens), so the whole map is encrypted.
    env_enc: str | None = None
    # sse/http only: the endpoint URL, in the clear (structural, shown by the surface).
    url: str | None = None
    # Whether this server needs third-party authorization before it will answer (`MCP-3`).
    # A flag, not a secret — it drives the "needs auth" affordance on the surface.
    auth_required: bool = False
    # Sealed JSON of the credentials that satisfy that authorization ({"method": ...,
    # "token"/"username"/"password": ...}). None ⇒ none supplied yet.
    auth_enc: str | None = None
    # Operator enable/disable for the whole server (`MCP-3`). A disabled server is never
    # connected and contributes no tools; its per-tool policy survives the round trip.
    enabled: bool = True
    # Last known connection outcome: "disconnected" | "connected" | "error".
    status: str = "disconnected"
    # The last connection failure, in operator-legible form, plus when it happened.
    last_error: str | None = None
    last_error_at: datetime | None = None
    # The tools discovered on the last successful connect, as clear JSON
    # ([{"name": ..., "description": ...}]). This is the server's own public catalog —
    # metadata about capabilities, not operator content — and the surface needs it to
    # render the per-tool enable/trust controls without re-dialling the server.
    tools_json: str = "[]"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Integration(Table, table=True):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_integrations_owner_name"),
        UniqueConstraint("owner_id", "slug", name="uq_integrations_owner_slug"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The static preset this connector was instantiated from (`INTEG-1`). The preset owns
    # the display metadata and the action catalog; the row owns the operator's choices.
    preset: str = Field(index=True)
    name: str = Field(index=True)
    # Tool-name prefix, exactly as on `McpServer.slug` and frozen for the same reason.
    slug: str
    # The connector's root URL, in the clear — structural, and the operator edits it
    # (a self-hosted instance of a preset service points somewhere else).
    base_url: str
    # Sealed JSON of the supplied credentials ({"token"/"username"/"password": ...}).
    credentials_enc: str | None = None
    enabled: bool = True
    # Last credential-test outcome (`INTEG-3`): "untested" | "ok" | "error".
    status: str = "untested"
    last_error: str | None = None
    last_tested_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ExternalToolPolicy(Table, table=True):
    __tablename__ = "external_tool_policies"
    # One row per (owner, source kind, source, tool): a write upserts.
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "source_kind",
            "source_id",
            "tool_name",
            name="uq_external_tool_policy_scope",
        ),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # "mcp" (a tool on a registered server) | "integration" (an action on a connector).
    source_kind: str = Field(index=True)
    # The `McpServer.id` / `Integration.id` this tool belongs to.
    source_id: str = Field(index=True)
    # The tool's own name as its source exposes it — *not* the namespaced name the model
    # sees, so a change to the prefix can't orphan the policy.
    tool_name: str
    # `MCP-1`/`AE-3.3` — a disabled tool is never offered to the agent.
    enabled: bool = True
    # `AE-3.6` — an untrusted external tool pauses for approval on every call; the
    # operator marks specific tools trusted, and that is revocable (set back to False).
    trusted: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
