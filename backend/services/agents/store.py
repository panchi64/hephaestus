"""Persistence for the agents the operator and agents write.

The one implementation of every stored-agent rule: the REST routes and the agent's own
authoring tools are both callers, so an agent written by either is validated the same way.

**Agent-written agents land as drafts.** An agent is instructions and reach the model will
act under, so ``published`` is the trust boundary: nothing runs a definition the operator
has not seen.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, update
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import AgentValidationError, NotFoundError
from core.vault import Vault
from models.agent import Agent
from models.conversation import Conversation
from services.agents.definition import (
    DEFAULT_AGENT,
    WORKSPACE_POLICIES,
    AgentDefinition,
    ToolAccess,
    workspace_policy,
)
from services.agents.files import (
    DESCRIPTION_MAX_CHARS,
    AgentFileError,
    AgentNameError,
    normalize_agent_name,
    parse_agent_file,
)
from services.permissions import PERMISSION_LEVELS

INSTRUCTIONS_MAX_CHARS = 32_000

#: The fields a caller may set. The rest (``id``, ``source``, ``published``, and the
#: built-in-only ``required`` and ``delegable``) are the store's to decide.
EDITABLE: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "instructions",
        "endpoint_id",
        "model",
        "tools",
        "permission_ceiling",
        "workspace",
        "request_limit",
        "wall_clock_timeout_s",
        "can_delegate",
        "max_depth",
    }
)


class AgentStore:
    def __init__(self, engine: Engine, vault: Vault) -> None:
        self._engine = engine
        self._vault = vault

    async def list(self, owner_id: str, *, published_only: bool = False) -> list[AgentDefinition]:
        def work(session: Session) -> list[AgentDefinition]:
            query = select(Agent).where(Agent.owner_id == owner_id)
            if published_only:
                query = query.where(col(Agent.published).is_(True))
            rows = session.exec(query.order_by(col(Agent.name))).all()
            return [self._definition(row) for row in rows]

        return await in_session(self._engine, work)

    async def get(self, owner_id: str, agent_id: str) -> AgentDefinition:
        def work(session: Session) -> AgentDefinition:
            return self._definition(_require(session, owner_id, agent_id))

        return await in_session(self._engine, work)

    async def get_by_name(self, owner_id: str, name: str) -> AgentDefinition | None:
        def work(session: Session) -> AgentDefinition | None:
            row = _find_by_name(session, owner_id, name)
            return None if row is None else self._definition(row)

        return await in_session(self._engine, work)

    async def create(
        self,
        owner_id: str,
        values: Mapping[str, Any],
        *,
        source: str = "user",
        published: bool = False,
    ) -> AgentDefinition:
        """Store a new agent from ``values`` (any of :data:`EDITABLE`). A draft unless
        ``published`` says otherwise."""
        if not str(values.get("name") or "").strip():
            raise AgentValidationError("name", "an agent needs a name")
        agent = _validated(_apply(AgentDefinition(name="new", description=""), values))

        def work(session: Session) -> AgentDefinition:
            if _find_by_name(session, owner_id, agent.name) is not None:
                raise AgentValidationError("name", f"an agent named {agent.name!r} already exists")
            row = Agent(
                owner_id=owner_id, source=source, published=published, **self._columns(agent)
            )
            session.add(row)
            session.flush()
            return self._definition(row)

        return await in_session(self._engine, work)

    async def update(
        self, owner_id: str, agent_id: str, changes: Mapping[str, Any]
    ) -> AgentDefinition:
        """Apply ``changes`` (any of :data:`EDITABLE`). A rename carries every thread that
        ran the agent along with it."""

        def work(session: Session) -> AgentDefinition:
            row = _require(session, owner_id, agent_id)
            before = self._definition(row)
            agent = _validated(_apply(before, changes))
            if agent.name != before.name:
                if _find_by_name(session, owner_id, agent.name) is not None:
                    raise AgentValidationError(
                        "name", f"an agent named {agent.name!r} already exists"
                    )
                session.exec(
                    update(Conversation)
                    .where(col(Conversation.owner_id) == owner_id)
                    .where(col(Conversation.agent_name) == before.name)
                    .values(agent_name=agent.name)
                )
            for column, value in self._columns(agent).items():
                setattr(row, column, value)
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._definition(row)

        return await in_session(self._engine, work)

    async def import_file(self, owner_id: str, text: str, *, filename: str) -> AgentDefinition:
        """Store an agent file (``.claude/agents`` format) as a draft."""
        try:
            parsed = parse_agent_file(text, name=Path(filename).stem)
        except AgentFileError as exc:
            raise AgentValidationError("file", str(exc)) from None
        values = {
            name: getattr(parsed, name) for name in EDITABLE if name not in {"endpoint_id", "model"}
        }
        return await self.create(owner_id, values)

    async def set_published(
        self, owner_id: str, agent_id: str, published: bool
    ) -> AgentDefinition:
        def work(session: Session) -> AgentDefinition:
            row = _require(session, owner_id, agent_id)
            row.published = published
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.flush()
            return self._definition(row)

        return await in_session(self._engine, work)

    async def delete(self, owner_id: str, agent_id: str) -> None:
        """Delete the agent. Threads that ran it fall back to the default agent."""

        def work(session: Session) -> None:
            session.delete(_require(session, owner_id, agent_id))

        await in_session(self._engine, work)

    def _columns(self, agent: AgentDefinition) -> dict[str, Any]:
        return {
            "name": agent.name,
            "description_enc": self._vault.encrypt_str(agent.description),
            "instructions_enc": self._vault.encrypt_str(agent.instructions),
            "endpoint_id": agent.endpoint_id,
            "model": agent.model,
            "tools_allow_json": (
                None if agent.tools.allow is None else json.dumps(sorted(agent.tools.allow))
            ),
            "tools_deny_json": json.dumps(sorted(agent.tools.deny)) if agent.tools.deny else None,
            "permission_ceiling": agent.permission_ceiling,
            "workspace": agent.workspace,
            "request_limit": agent.request_limit,
            "wall_clock_timeout_s": agent.wall_clock_timeout_s,
            "can_delegate": agent.can_delegate,
            "max_depth": agent.max_depth,
        }

    def _definition(self, row: Agent) -> AgentDefinition:
        allow = None if row.tools_allow_json is None else json.loads(row.tools_allow_json)
        deny = [] if row.tools_deny_json is None else json.loads(row.tools_deny_json)
        return AgentDefinition(
            id=row.id,
            name=row.name,
            description=self._vault.decrypt_str(row.description_enc),
            instructions=self._vault.decrypt_str(row.instructions_enc),
            endpoint_id=row.endpoint_id,
            model=row.model,
            tools=ToolAccess(
                allow=None if allow is None else frozenset(allow), deny=frozenset(deny)
            ),
            permission_ceiling=row.permission_ceiling,
            workspace=workspace_policy(row.workspace) or "shared",
            request_limit=row.request_limit,
            wall_clock_timeout_s=row.wall_clock_timeout_s,
            can_delegate=row.can_delegate,
            max_depth=row.max_depth,
            source="agent" if row.source == "agent" else "user",
            published=row.published,
        )


def _apply(agent: AgentDefinition, values: Mapping[str, Any]) -> AgentDefinition:
    unknown = set(values) - EDITABLE
    if unknown:
        raise AgentValidationError(sorted(unknown)[0], "not a field an agent can set")
    known = {f.name for f in fields(AgentDefinition)}
    return replace(agent, **{key: value for key, value in values.items() if key in known})


def _validated(agent: AgentDefinition) -> AgentDefinition:
    try:
        name = normalize_agent_name(agent.name)
    except AgentNameError as exc:
        raise AgentValidationError("name", str(exc)) from None
    if name == DEFAULT_AGENT:
        raise AgentValidationError("name", f"{DEFAULT_AGENT!r} is the built-in default agent")
    description = " ".join(agent.description.split())
    if not description:
        raise AgentValidationError("description", "an agent needs a description")
    if len(description) > DESCRIPTION_MAX_CHARS:
        raise AgentValidationError(
            "description", f"keep the description under {DESCRIPTION_MAX_CHARS} characters"
        )
    if len(agent.instructions) > INSTRUCTIONS_MAX_CHARS:
        raise AgentValidationError(
            "instructions", f"keep the instructions under {INSTRUCTIONS_MAX_CHARS} characters"
        )
    if agent.permission_ceiling is not None and agent.permission_ceiling not in PERMISSION_LEVELS:
        raise AgentValidationError(
            "permission_ceiling", f"{agent.permission_ceiling!r} is not a permission level"
        )
    if agent.workspace not in WORKSPACE_POLICIES:
        raise AgentValidationError("workspace", "workspace is one of shared, isolated, own")
    for field_name in ("request_limit", "wall_clock_timeout_s"):
        value = getattr(agent, field_name)
        if value is not None and value <= 0:
            raise AgentValidationError(field_name, "must be positive")
    if agent.max_depth is not None and agent.max_depth < 0:
        raise AgentValidationError("max_depth", "must not be negative")
    return replace(
        agent,
        name=name,
        description=description,
        instructions=agent.instructions.strip(),
        endpoint_id=agent.endpoint_id or None,
        model=agent.model or None,
        tools=_clean_tools(agent.tools),
    )


def _clean_tools(tools: ToolAccess) -> ToolAccess:
    def clean(names: frozenset[str]) -> frozenset[str]:
        return frozenset(name.strip().lower() for name in names if name.strip())

    return ToolAccess(
        allow=None if tools.allow is None else clean(tools.allow), deny=clean(tools.deny)
    )


def _require(session: Session, owner_id: str, agent_id: str) -> Agent:
    row = session.get(Agent, agent_id)
    if row is None or row.owner_id != owner_id:
        raise NotFoundError("agent not found")
    return row


def _find_by_name(session: Session, owner_id: str, name: str) -> Agent | None:
    return session.exec(
        select(Agent).where(Agent.owner_id == owner_id).where(Agent.name == name)
    ).first()
