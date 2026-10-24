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
from typing import TypedDict

from pydantic import TypeAdapter
from sqlalchemy import Engine, update
from sqlmodel import Session, col, select

from core.db import in_session
from core.exceptions import AgentValidationError, NotFoundError
from core.vault import Vault
from models.agent import Agent
from models.conversation import Conversation
from services.agents.definition import (
    DEFAULT_AGENT,
    DEFAULT_CLASS,
    UNASSIGNABLE_CLASSES,
    WORKSPACE_POLICIES,
    AgentDefinition,
    ToolAccess,
    class_key,
    workspace_policy,
)
from services.agents.files import (
    DESCRIPTION_MAX_CHARS,
    AgentFileError,
    AgentNameError,
    normalize_agent_name,
    parse_agent_file,
    render_agent_file,
)
from services.permissions import PERMISSION_LEVELS
from services.revisions import Bundle, RevisionStore, records

INSTRUCTIONS_MAX_CHARS = 32_000

#: A version's files: the agent file, and the model it runs on, which that file leaves out.
AGENT_FILE = "agent.md"
MODEL_FILE = "model.json"

#: The fields a caller may set. The rest (``id``, ``source``, ``published``, and the
#: built-in-only ``required`` and ``delegable``) are the store's to decide.
EDITABLE: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "agent_class",
        "instructions",
        "endpoint_id",
        "model",
        "tools",
        "permission_ceiling",
        "workspace",
        "wall_clock_timeout_s",
        "can_delegate",
        "max_depth",
        "max_requests",
    }
)


class _Columns(TypedDict):
    name: str
    description_enc: str
    agent_class: str
    instructions_enc: str
    endpoint_id: str | None
    model: str | None
    tools_allow_json: str | None
    tools_deny_json: str | None
    permission_ceiling: str | None
    workspace: str
    wall_clock_timeout_s: float | None
    can_delegate: bool
    max_depth: int | None
    max_requests: int | None


_NAMES = TypeAdapter(list[str])
_OBJECT = TypeAdapter(dict[str, object])


class AgentStore:
    revision_kind: str = "agent"

    def __init__(
        self, engine: Engine, vault: Vault, revisions: RevisionStore | None = None
    ) -> None:
        self._engine: Engine = engine
        self._vault: Vault = vault
        self.revisions: RevisionStore | None = revisions

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
        values: Mapping[str, object],
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

    @records("edited")
    async def update(
        self, owner_id: str, agent_id: str, changes: Mapping[str, object]
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
                _ = session.exec(
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
        return await self.create(owner_id, _file_values(text, Path(filename).stem))

    async def replace_document(self, owner_id: str, agent_id: str, text: str) -> AgentDefinition:
        """Rewrite the agent from an edited agent file. Its model is kept: the file format
        does not carry one."""
        current = await self.get(owner_id, agent_id)
        return await self.update(owner_id, agent_id, _file_values(text, current.name))

    @records("published")
    async def set_published(self, owner_id: str, agent_id: str, published: bool) -> AgentDefinition:
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
        if self.revisions is not None:
            await self.revisions.forget(owner_id, self.revision_kind, agent_id)

    async def snapshot(self, owner_id: str, agent_id: str) -> Bundle:
        agent = await self.get(owner_id, agent_id)
        model = {"endpoint_id": agent.endpoint_id, "model": agent.model}
        return {
            AGENT_FILE: render_agent_file(agent).encode(),
            MODEL_FILE: json.dumps(model, indent=2).encode(),
        }

    async def working_copy(self, owner_id: str, agent_id: str) -> Bundle:
        return await self.snapshot(owner_id, agent_id)

    async def restore_snapshot(
        self, owner_id: str, agent_id: str, bundle: Bundle
    ) -> AgentDefinition:
        document = bundle.get(AGENT_FILE)
        if document is None:
            raise AgentValidationError("file", f"that version has no {AGENT_FILE}")
        current = await self.get(owner_id, agent_id)
        model = _OBJECT.validate_json(bundle.get(MODEL_FILE) or b"{}")
        values = _file_values(document.decode(), current.name)
        values.update({key: model.get(key) for key in ("endpoint_id", "model")})
        return await self.update(owner_id, agent_id, values)

    def _columns(self, agent: AgentDefinition) -> _Columns:
        return {
            "name": agent.name,
            "description_enc": self._vault.encrypt_str(agent.description),
            "agent_class": agent.agent_class,
            "instructions_enc": self._vault.encrypt_str(agent.instructions),
            "endpoint_id": agent.endpoint_id,
            "model": agent.model,
            "tools_allow_json": (
                None if agent.tools.allow is None else json.dumps(sorted(agent.tools.allow))
            ),
            "tools_deny_json": json.dumps(sorted(agent.tools.deny)) if agent.tools.deny else None,
            "permission_ceiling": agent.permission_ceiling,
            "workspace": agent.workspace,
            "wall_clock_timeout_s": agent.wall_clock_timeout_s,
            "can_delegate": agent.can_delegate,
            "max_depth": agent.max_depth,
            "max_requests": agent.max_requests,
        }

    def _definition(self, row: Agent) -> AgentDefinition:
        allow = None if row.tools_allow_json is None else _NAMES.validate_json(row.tools_allow_json)
        deny = [] if row.tools_deny_json is None else _NAMES.validate_json(row.tools_deny_json)
        return AgentDefinition(
            id=row.id,
            name=row.name,
            description=self._vault.decrypt_str(row.description_enc),
            agent_class=class_key(row.agent_class) or DEFAULT_CLASS,
            instructions=self._vault.decrypt_str(row.instructions_enc),
            endpoint_id=row.endpoint_id,
            model=row.model,
            tools=ToolAccess(
                allow=None if allow is None else frozenset(allow), deny=frozenset(deny)
            ),
            permission_ceiling=row.permission_ceiling,
            workspace=workspace_policy(row.workspace) or "shared",
            wall_clock_timeout_s=row.wall_clock_timeout_s,
            can_delegate=row.can_delegate,
            max_depth=row.max_depth,
            max_requests=row.max_requests,
            source="agent" if row.source == "agent" else "user",
            published=row.published,
        )


def _file_values(text: str, name: str) -> dict[str, object]:
    try:
        parsed = parse_agent_file(text, name=name, clip=False)
    except AgentFileError as exc:
        raise AgentValidationError("file", str(exc)) from None
    return {key: getattr(parsed, key) for key in EDITABLE - {"endpoint_id", "model"}}


def _apply(agent: AgentDefinition, values: Mapping[str, object]) -> AgentDefinition:
    unknown = set(values) - EDITABLE
    if unknown:
        raise AgentValidationError(sorted(unknown)[0], "not a field an agent can set")
    known = {f.name for f in fields(AgentDefinition)}
    changes = {key: value for key, value in values.items() if key in known}
    if "agent_class" in changes:
        key = class_key(str(changes["agent_class"] or ""))
        if key is None:
            raise AgentValidationError("agent_class", "not an agent class")
        if key in UNASSIGNABLE_CLASSES:
            raise AgentValidationError("agent_class", "not a class an agent you write can take")
        changes["agent_class"] = key
    cap = changes.get("max_requests")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0):
        raise AgentValidationError("max_requests", "must be a positive integer")
    return replace(agent, **changes)


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
    if agent.wall_clock_timeout_s is not None and agent.wall_clock_timeout_s <= 0:
        raise AgentValidationError("wall_clock_timeout_s", "must be positive")
    if agent.max_depth is not None and agent.max_depth < 0:
        raise AgentValidationError("max_depth", "must not be negative")
    if agent.max_requests is not None and agent.max_requests <= 0:
        raise AgentValidationError("max_requests", "must be positive")
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
