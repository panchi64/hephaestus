"""Published versions of the things the operator publishes: skills, agents and plugins.

One table for every kind. A revision is the whole item as files (``SKILL.md`` and its
bundle, an agent file, a plugin's sources), sealed as one blob, so history, diffs and
restore work the same for each kind. ``digest`` lets a write skip a version identical to
the last one without a decrypt.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, UniqueConstraint

from models._fields import new_id, utcnow
from models._table import Table


class Revision(Table, table=True):
    __tablename__ = "revisions"
    __table_args__ = (
        UniqueConstraint("kind", "subject_id", "number", name="uq_revisions_subject_number"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # skill | agent | plugin
    kind: str
    subject_id: str = Field(index=True)
    number: int
    note: str
    digest: str
    size_bytes: int
    bundle_enc: bytes
    created_at: datetime = Field(default_factory=utcnow)
