"""Outbound service-credential schema.

A **service credential** is an API key the system uses to call an *outbound* third-party
service on the operator's behalf — today the mail OAuth client secrets. Owner-scoped,
one row per service id, with
the key sealed application-layer like every other secret (the same at-rest posture as the
model-endpoint / search-provider `api_key`). This is **not** inbound auth — issuing access
tokens to clients is a separate concern.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class ServiceCredential(Table, table=True):
    __tablename__: ClassVar[str] = "service_credentials"
    # One credential per (owner, service): a write upserts rather than duplicating.
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "service", name="uq_service_credential_owner_service"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # The outbound service id this key authenticates to (e.g. "google_oauth").
    # The human label / purpose / docs live in the static catalog, not the DB.
    service: str
    # App-layer AEAD ciphertext of the API key/token; None ⇒ no key stored.
    api_key_enc: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
