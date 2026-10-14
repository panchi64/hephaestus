"""The one handle carrying both external tool sources and the policy store they share.

:class:`ExternalTools` is what the wiring hangs on ``app.state.external`` and what the tool
layer reads off ``RunDeps``. The policy store itself (:mod:`services.external_policy`) is
re-exported here for callers that reach it through the handle's module.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from core.vault import Vault
from services.external_policy import ExternalPolicyStore, SourceKind, ToolPolicy, tool_slug
from services.integrations import IntegrationService
from services.mcp import McpRegistry

__all__ = [
    "ExternalPolicyStore",
    "ExternalTools",
    "SourceKind",
    "ToolPolicy",
    "build_external_tools",
    "tool_slug",
]


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
    """
    policy = ExternalPolicyStore(db_engine)
    return ExternalTools(
        policy=policy,
        mcp=McpRegistry(db_engine, vault, policy),
        integrations=IntegrationService(db_engine, vault, policy),
    )
