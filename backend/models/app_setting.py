"""Owner-scoped key/value app settings.

A tiny persisted store for small operator preferences that aren't worth a bespoke table
— the active model-quality source, say. Plain string values (not secret, not relational);
structured config still lives in ``core/config.py`` (env). Owner-scoped
like every record, so a per-user split later needs no rewrite.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from models._backup import BackupSpec
from models._fields import new_id, utcnow
from models._table import Table


class AppSetting(Table, table=True):
    __tablename__ = "app_settings"
    # One value per (owner, key): a write upserts.
    __table_args__ = (UniqueConstraint("owner_id", "key", name="uq_app_setting_owner_key"),)
    # The operator's preferences (`BACKUP-1`), merged on the key they are stored under —
    # the same identity the store itself upserts on.
    __backup__ = BackupSpec(section="preferences", natural_key=("key",))

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    key: str
    value: str
    updated_at: datetime = Field(default_factory=utcnow)
