"""Agents the operator or an agent wrote.

Built-in agents live in code and project agents in the project's files; this table holds
the rest. The prose an agent is — its description and instructions — is sealed under the
vault. What the catalog and the gate read without a decrypt stays clear: the name (the
uniqueness key and the handle the model addresses it by), the model it runs on, its tool
lists, limits, and the ``published`` flag that is its trust boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field, UniqueConstraint

from models._backup import BackupSpec
from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class Agent(Table, table=True):
    __tablename__: ClassVar[str] = "agents"
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "name", name="uq_agents_owner_name"),
    )
    __backup__: ClassVar[BackupSpec] = BackupSpec(section="agents", natural_key=("name",))

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    name: str = Field(index=True)
    description_enc: str
    # A key of ``prompts.voice.CLASSES``.
    agent_class: str = Field(default="tripodes")
    instructions_enc: str
    endpoint_id: str | None = Field(default=None)
    model: str | None = Field(default=None)
    # JSON arrays of tool or category names. A null allow-list offers every tool.
    tools_allow_json: str | None = Field(default=None)
    tools_deny_json: str | None = Field(default=None)
    permission_ceiling: str | None = Field(default=None)
    workspace: str = Field(default="shared")
    request_limit: int | None = Field(default=None)
    wall_clock_timeout_s: float | None = Field(default=None)
    can_delegate: bool = Field(default=False)
    max_depth: int | None = Field(default=None)
    # Nothing runs a draft: an agent an agent wrote is inert until the operator publishes it.
    published: bool = Field(default=False, index=True)
    # user | agent
    source: str = Field(default="user", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
