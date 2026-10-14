"""Web-search provider registry schema.

A **search provider** is an operator-run search endpoint the agent queries — today
a self-hosted **SearXNG** instance (``base_url`` + its JSON API). Mirrors the model
registry: the operator's catalog of providers, owner-scoped, the first ``enabled``
one used for a query. SearXNG usually needs no credential, but the encrypted
``api_key`` seam is kept so a guarded instance (or a future provider) fits without
a schema change — the same at-rest posture as model endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field

from models._backup import BackupSpec
from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class SearchProvider(Table, table=True):
    __tablename__: ClassVar[str] = "search_providers"
    # An operator's provider names are unique, so a re-import can't silently
    # duplicate one (same guard the model endpoints use).
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "name", name="uq_search_provider_owner_name"),
    )
    # Part of the operator's configuration, so it exports under "settings" (`BACKUP-1`),
    # merged on that same unique name.
    __backup__: ClassVar[BackupSpec] = BackupSpec(
        section="settings", natural_key=("name",), order=2
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    name: str
    # The instance root; the service appends SearXNG's ``/search`` JSON endpoint.
    base_url: str
    # Disabled providers stay configured but are skipped when picking the active one.
    enabled: bool = True
    # Optional SearXNG engine filter (e.g. ["google", "duckduckgo"]); empty ⇒ default.
    engines: list[str] = Field(sa_column=Column(JSON, nullable=False, default=list))
    # Extra query params passed through verbatim (e.g. {"language": "en"}).
    params: dict[str, object] = Field(sa_column=Column(JSON, nullable=False, default=dict))
    # App-layer AEAD ciphertext of the API key; None ⇒ no key (the SearXNG default).
    api_key_enc: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
