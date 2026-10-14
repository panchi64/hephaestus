"""Plugins: tools written as Python that run in the platform VM.

A plugin's files are sealed as one bundle. ``active_enc`` is the published version, the
one its tools run; ``draft_enc`` is the working copy an agent or the operator edits,
which runs only when tested until the operator publishes it. Earlier published versions
live in ``revisions``.

A secret's value is sealed here and is sent only by the egress gateway, to ``domain``.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, UniqueConstraint

from models._fields import new_id, utcnow
from models._table import Table


class Plugin(Table, table=True):
    __tablename__ = "plugins"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_plugins_owner_name"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    name: str = Field(index=True)
    # user | agent
    source: str = Field(default="user")
    published: bool = Field(default=False, index=True)
    active_enc: bytes | None = Field(default=None)
    draft_enc: bytes | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class PluginSecret(Table, table=True):
    __tablename__ = "plugin_secrets"
    __table_args__ = (UniqueConstraint("plugin_id", "name", name="uq_plugin_secrets_plugin_name"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    plugin_id: str = Field(index=True)
    name: str
    domain: str
    value_enc: str
    updated_at: datetime = Field(default_factory=utcnow)
