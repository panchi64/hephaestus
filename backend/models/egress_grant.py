"""Conversation-scoped network egress grants.

A grant records that the operator approved one domain for one conversation's workspace,
on top of the installation-wide allowlist. Unlike an approval grant this has no TTL: it
names *where* the workspace may reach, and a package registry the operator opened for a
piece of work stays open for the rest of that work. The tool that asks for one is
approved per call regardless, so the row is the record of a decision already made rather
than a standing permission to make more.

Like ``ApprovalGrant`` this is operator policy, not content — a domain name is the thing
the operator read and agreed to — so nothing here is vault-sealed, and it is owner-scoped
like every record.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class EgressGrant(Table, table=True):
    __tablename__: ClassVar[str] = "egress_grants"
    # One row per (owner, conversation, domain): re-approving a domain is a no-op.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "conversation_id", "domain", name="uq_egress_grant_scope"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The workspace key the grant applies to — the conversation, or a stateless run's own
    # id. Named for the conversation because that is the case with a lifetime long enough
    # to be worth deleting; a run-keyed row is emptied by nothing and outlives nothing.
    conversation_id: str = Field(index=True)
    # The normalised host, exactly as the allowlist file spells it: `example.com` or
    # `*.example.com`, lowercase, no scheme, port or path.
    domain: str
    created_at: datetime = Field(default_factory=utcnow)
