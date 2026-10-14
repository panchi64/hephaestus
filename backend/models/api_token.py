"""Scoped API token schema (`AUTH-4`).

**Inbound** auth: tokens issued to clients for programmatic access. Distinct from
``ServiceCredential``, which holds the outbound keys this system calls other services with.

A token is minted as ``odyt_<prefix>_<secret>``. The **prefix** is public — stored in the
clear so a presented token can be looked up by index, and shown in listings so the operator
can tell their tokens apart. The **secret** is never stored: only a one-way Argon2id hash of
it, the same posture as the login password (`XC-SEC-3`), so a token is shown once at issue
and is unrecoverable afterwards. Nothing here is vault-sealed, because there is nothing to
recover — a lost token is reissued, not decrypted.

``scopes`` is policy, not content (like ``ApprovalGrant.tool_name``), so it stays in the
clear: the set of API surfaces this token may reach, resolved against the catalog in
``core.api_scopes``. ``revoked_at`` is a tombstone rather than a delete, so a revoked
token's label and last use stay auditable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table


class ApiToken(Table, table=True):
    __tablename__ = "api_tokens"

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The operator's own name for this token ("laptop CLI", "home automation").
    label: str
    # The public half of the minted token — indexed, so verification is one row lookup
    # followed by one constant-time hash comparison rather than a table scan.
    token_prefix: str = Field(index=True, unique=True)
    # Argon2id PHC hash of the secret half. One-way: the token itself is unrecoverable.
    token_hash: str
    # Scope ids from `core.api_scopes`; a request outside them is refused.
    scopes: list[str] = Field(sa_column=Column(JSON, nullable=False, default=list))
    created_at: datetime = Field(default_factory=utcnow)
    # Bumped (at most once a minute) when the token successfully authenticates.
    last_used_at: datetime | None = None
    # Set on revoke; the row is kept so its history stays readable.
    revoked_at: datetime | None = None
