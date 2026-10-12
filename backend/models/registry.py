"""Model role→endpoint registry schema.

A **role** (``main``, ``utility``, ``embedding``, later ``vision``/``image-gen``)
binds to an **ordered fallback chain** of endpoints. The chain is wrapped in
Pydantic AI's ``FallbackModel`` at resolution time, so a dead endpoint falls
through to the next.

An **endpoint** is one connection to a model API: a ``provider`` (which adapter in
``services/providers`` speaks its wire protocol — OpenAI-compatible, Anthropic, or
Google), a ``base_url`` + ``model`` name + optional key, plus the metadata the engine
consumes — the ``context_window`` (feeds context reduction) and capability flags (native
tool-calling is required for the tool-driving roles, plus vision/thinking).

The **API key is the only sensitive field**: it is stored application-layer
encrypted (the chosen at-rest posture — whole-DB SQLCipher has no portable
3.14 wheels). Everything else is structural metadata the DB indexes in the clear.
The per-conversation ``main`` override is a runtime argument, not stored here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

from models._backup import BackupSpec
from models._fields import new_id, utcnow


class ModelEndpoint(SQLModel, table=True):
    __tablename__ = "model_endpoints"
    # An operator's endpoint names are unique, so a chain can refer to them
    # stably and a re-import can't silently duplicate one.
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_endpoint_owner_name"),)
    # Exported under "settings" (`BACKUP-1`), before the roles that bind to it. The unique
    # name is already the "same endpoint" test a merge-import needs.
    __backup__ = BackupSpec(section="settings", natural_key=("name",), order=0)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    name: str
    # Which adapter (`services/providers`) speaks this endpoint's wire protocol.
    # "openai-compatible" is the universal default; "anthropic"/"google" are native.
    provider: str = "openai-compatible"
    base_url: str
    # The endpoint is a provider connection; ``model`` is the default/fallback used
    # when the chat picker doesn't override it and the provider's models API isn't
    # available. Optional — the picker discovers models from the provider at runtime.
    model: str | None = None
    # App-layer AEAD ciphertext of the API key; None ⇒ no key (an endpoint on the
    # operator's own network usually needs none).
    api_key_enc: str | None = None
    context_window: int | None = None
    # AE-8.1: native tool-calling is required of the tool-driving roles. vision
    # and thinking gate other features (scanned-PDF extraction, reasoning split).
    native_tools: bool = True
    vision: bool = False
    thinking: bool = False
    # Disable-without-delete: a benched endpoint keeps its config (key, capability
    # flags, role memberships) but is skipped by resolution and hidden from the
    # picker — so a flaky provider can be parked, then restored, without re-setup.
    enabled: bool = True
    # How many runs may use this endpoint at once; the rest queue (`runs/endpoint_gate.py`).
    # None ⇒ the server's reported slot count, or unlimited when it reports none.
    max_concurrent_runs: int | None = None
    # Last connection-test outcome — operator-facing health, all **cleartext**
    # structural metadata (never the key). ``last_status`` is ``"untested"`` until
    # the first probe; ``last_error_category`` is a stable machine token the UI maps
    # to an icon; ``last_error_detail`` is the plain-language sentence rendered
    # verbatim (the backend owns the wording, the frontend never categorizes).
    last_status: str | None = None  # "ok" | "error" | "untested"
    last_error_category: str | None = None  # connection-test category (see _categorize_probe)
    last_error_detail: str | None = None
    last_checked_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ModelRole(SQLModel, table=True):
    __tablename__ = "model_roles"
    __table_args__ = (UniqueConstraint("owner_id", "role", name="uq_role_owner_role"),)
    # One binding per role, so the role name is its identity; imported after the endpoints
    # its chain refers to (ids survive an import, so the chain stays intact).
    __backup__ = BackupSpec(section="settings", natural_key=("role",), order=1)

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    role: str  # main | utility | embedding | vision | image-gen
    # The ordered fallback chain, by endpoint id. First is primary; the rest are
    # tried in order. Stored as JSON so order and length are one row, one write.
    endpoint_ids: list[str] = Field(sa_column=Column(JSON, nullable=False, default=list))
    # An explicit model on the bound endpoint, when the role needs to pin one that
    # differs from the endpoint's default ``model``. Used by ``embedding`` (no
    # per-conversation picker, unlike ``main``); ``None`` ⇒ the endpoint's default.
    model: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)
