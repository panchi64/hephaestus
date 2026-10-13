"""The shared layer under both external tool sources: how a tool is *named* and what the
operator has *decided* about it.

One policy store behind both external sources — MCP servers (`MCP-*`) and third-party
connectors (`INTEG-*`) — because the operator's two decisions about such a tool are the
same in both cases:

- **enabled** — is it offered to the agent at all (`MCP-1`, `AE-3.3`);
- **trusted** — may it run without pausing for approval (`AE-3.6`).

Both are keyed **per tool**. Enabling a server or configuring a connector says nothing
about what any individual tool on it does, so neither may imply trust; a server-level
flag would auto-approve tools the operator has never seen. Trust is granted one tool at a
time and revoked the same way.

A *missing* row is the safe default — ``enabled=True, trusted=False`` — so a tool the
server only just started exposing is usable but approval-gated, never silently trusted.

Policy, not content: nothing here is vault-sealed, exactly as ``ApprovalGrant`` isn't, so
it stays indexable and readable while the vault is locked. Raises domain errors only.

This module also holds :class:`ExternalTools` — the one handle carrying both sources and
this store, which is what the wiring hangs on ``app.state.external`` and what the tool
layer reads off ``RunDeps``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from services.integrations import IntegrationService
    from services.mcp import McpRegistry

from sqlalchemy import Engine, delete
from sqlmodel import Session, select

from core.db import in_session, upsert
from core.exceptions import NotFoundError
from core.vault import Vault
from models._fields import new_id, utcnow
from models.external_tool import ExternalToolPolicy

SourceKind = Literal["mcp", "integration"]

# The default an unseen tool gets: offered to the agent, but approval-gated (`AE-3.6`).
DEFAULT_ENABLED = True
DEFAULT_TRUSTED = False

_NON_NAME_CHARS = re.compile(r"[^a-z0-9]+")


def tool_slug(label: str) -> str:
    """The operator's label reduced to a tool-name-safe prefix.

    Every external tool the model sees is named ``external_{slug}_{tool}``, so the slug
    has to survive being pasted into an identifier: lowercase, alphanumerics and
    underscores, never leading with a digit. A label that reduces to nothing (emoji,
    CJK-only) falls back to ``source`` and the caller's uniqueness check disambiguates it.
    """
    slug = _NON_NAME_CHARS.sub("_", label.strip().lower()).strip("_")
    if not slug:
        return "source"
    return f"s_{slug}" if slug[0].isdigit() else slug


@dataclass(frozen=True)
class ToolPolicy:
    """The operator's decision about one external tool."""

    enabled: bool = DEFAULT_ENABLED
    trusted: bool = DEFAULT_TRUSTED


class ExternalPolicyStore:
    def __init__(self, db_engine: Engine) -> None:
        self._db = db_engine

    async def snapshot(
        self, owner_id: str, source_kind: SourceKind, source_id: str
    ) -> dict[str, ToolPolicy]:
        """Every recorded decision for one source, keyed by the tool's own name. Tools
        with no row simply aren't in the map — the caller applies the default."""

        def work(session: Session) -> dict[str, ToolPolicy]:
            rows = session.exec(
                select(ExternalToolPolicy)
                .where(ExternalToolPolicy.owner_id == owner_id)
                .where(ExternalToolPolicy.source_kind == source_kind)
                .where(ExternalToolPolicy.source_id == source_id)
            ).all()
            return {r.tool_name: ToolPolicy(enabled=r.enabled, trusted=r.trusted) for r in rows}

        return await in_session(self._db, work)

    async def snapshots(
        self, owner_id: str, source_kind: SourceKind, source_ids: Sequence[str]
    ) -> dict[str, dict[str, ToolPolicy]]:
        """:meth:`snapshot` for several sources at once, keyed by source id then tool name.
        Sources with no recorded decision map to an empty dict, so a caller can index the
        result unconditionally.

        Listing connectors or MCP servers means one snapshot per row, and that listing sits
        on the agent's toolset-assembly path — a query per row there is a query per row on
        every run. The whole set is one ``IN``.
        """
        wanted = list(dict.fromkeys(source_ids))
        if not wanted:
            return {}

        def work(session: Session) -> dict[str, dict[str, ToolPolicy]]:
            rows = session.exec(
                select(ExternalToolPolicy)
                .where(ExternalToolPolicy.owner_id == owner_id)
                .where(ExternalToolPolicy.source_kind == source_kind)
                .where(ExternalToolPolicy.source_id.in_(wanted))  # type: ignore[attr-defined]
            ).all()
            by_source: dict[str, dict[str, ToolPolicy]] = {sid: {} for sid in wanted}
            for r in rows:
                by_source[r.source_id][r.tool_name] = ToolPolicy(
                    enabled=r.enabled, trusted=r.trusted
                )
            return by_source

        return await in_session(self._db, work)

    async def get(
        self, owner_id: str, source_kind: SourceKind, source_id: str, tool_name: str
    ) -> ToolPolicy:
        """One tool's policy, falling back to the default when it has no row yet. This is
        the read the trust gate makes on every external tool call, so it is deliberately a
        fresh read: revoking trust must take effect on the next call, not the next run."""

        def work(session: Session) -> ToolPolicy:
            row = session.exec(
                select(ExternalToolPolicy)
                .where(ExternalToolPolicy.owner_id == owner_id)
                .where(ExternalToolPolicy.source_kind == source_kind)
                .where(ExternalToolPolicy.source_id == source_id)
                .where(ExternalToolPolicy.tool_name == tool_name)
            ).first()
            if row is None:
                return ToolPolicy()
            return ToolPolicy(enabled=row.enabled, trusted=row.trusted)

        return await in_session(self._db, work)

    async def set(
        self,
        owner_id: str,
        source_kind: SourceKind,
        source_id: str,
        tool_name: str,
        *,
        enabled: bool | None = None,
        trusted: bool | None = None,
    ) -> ToolPolicy:
        """Record (or amend) the operator's decision about one tool. Only the fields
        passed are changed, so toggling ``enabled`` never disturbs ``trusted``.

        Marking a tool trusted is an operator action — never the agent's — and revoking
        it is the same call with ``trusted=False``, which puts the tool straight back to
        per-call approval.
        """
        if enabled is None and trusted is None:
            return await self.get(owner_id, source_kind, source_id, tool_name)
        now = utcnow()

        def work(session: Session) -> ToolPolicy:
            # Atomic upsert over uq_external_tool_policy_scope — a plain
            # select-then-insert races two concurrent toggles of the same tool into a
            # duplicate-insert IntegrityError, so the conflict is resolved in the DB.
            values = {
                "id": new_id(),
                "owner_id": owner_id,
                "source_kind": source_kind,
                "source_id": source_id,
                "tool_name": tool_name,
                "enabled": DEFAULT_ENABLED if enabled is None else enabled,
                "trusted": DEFAULT_TRUSTED if trusted is None else trusted,
                "created_at": now,
                "updated_at": now,
            }
            updates: dict[str, object] = {"updated_at": now}
            if enabled is not None:
                updates["enabled"] = enabled
            if trusted is not None:
                updates["trusted"] = trusted
            session.execute(
                upsert(self._db, ExternalToolPolicy)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["owner_id", "source_kind", "source_id", "tool_name"],
                    set_=updates,
                )
            )
            row = session.exec(
                select(ExternalToolPolicy)
                .where(ExternalToolPolicy.owner_id == owner_id)
                .where(ExternalToolPolicy.source_kind == source_kind)
                .where(ExternalToolPolicy.source_id == source_id)
                .where(ExternalToolPolicy.tool_name == tool_name)
            ).first()
            if row is None:  # pragma: no cover - the upsert above just wrote it
                raise NotFoundError(f"policy for tool {tool_name!r} could not be written")
            return ToolPolicy(enabled=row.enabled, trusted=row.trusted)

        return await in_session(self._db, work)

    async def forget_source(self, owner_id: str, source_kind: SourceKind, source_id: str) -> None:
        """Drop every decision for a source — called when its server/connector is
        removed, so a later registration at the same id can't inherit stale trust."""

        def work(session: Session) -> None:
            session.execute(
                delete(ExternalToolPolicy)
                .where(ExternalToolPolicy.owner_id == owner_id)
                .where(ExternalToolPolicy.source_kind == source_kind)
                .where(ExternalToolPolicy.source_id == source_id)
            )

        await in_session(self._db, work)


@dataclass(frozen=True)
class ExternalTools:
    """Both external tool sources and the policy they share, as one handle.

    The `external` capability is a *pair* of services — registered MCP servers and
    configured connectors — that the agent sees as a single category, so it travels as
    one object rather than two capability fields. The tool layer takes it off
    ``RunDeps`` like every other capability and never reaches a module global; the two
    REST surfaces reach the same instance for their own halves, which is what keeps a
    server the operator just registered visible to the very next run.

    ``policy`` is held here as well as inside each service so the trust gate can read a
    decision without having to pick which service a tool came from.
    """

    policy: ExternalPolicyStore
    mcp: McpRegistry
    integrations: IntegrationService


def build_external_tools(db_engine: Engine, vault: Vault) -> ExternalTools:
    """Construct the whole external-tools capability from the two things it needs.

    One factory so the wiring can't compose it two different ways: both services must be
    handed the *same* policy store, or the operator's per-tool decisions would fork by
    source and a tool trusted through one path would still gate through the other.

    The two services are imported here rather than at module scope because each imports
    this module for the policy store — the factory is the one place that depends on both
    directions.
    """
    from services.integrations import IntegrationService
    from services.mcp import McpRegistry

    policy = ExternalPolicyStore(db_engine)
    return ExternalTools(
        policy=policy,
        mcp=McpRegistry(db_engine, vault, policy),
        integrations=IntegrationService(db_engine, vault, policy),
    )
