"""Projects — a directory on the operator's machine, and the scope the app filters by.

A `Project` is one host path the operator works in, plus the git facts a worktree
needs to work on it safely. Two things about the shape are load-bearing:

**`root_path_enc` is sealed.** A filesystem path names the operator's clients, employers
and habits as surely as a document's contents do, so it is encrypted at rest like every
other piece of their content. `base_ref` and the flags are structural — the worktree
machinery queries them and they reveal nothing on their own.

**`project_id` on a scoped entity is nullable, and null means *unfiled*, not *orphaned*.**
Unfiled rows are visible in every scope; a project's rows are visible when that project is
active; another project's rows are never visible. That single rule is what makes this
migration safe — every row that already exists is unfiled, so activating a project adds to
what the operator sees and never subtracts.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field

from models._backup import BackupSpec
from models._fields import new_id, utcnow
from models._table import Table


class Project(Table, table=True):
    __tablename__: ClassVar[str] = "projects"
    # The operator's own configuration, so it belongs in an export. Keyed on the path:
    # the same directory *is* the same project, and the importer compares decrypted
    # values, so a re-seal on another host still matches. A restored path may not exist
    # on the new machine — losing the project list would be worse than a path to fix.
    __backup__: ClassVar[BackupSpec] = BackupSpec(
        section="settings", natural_key=("root_path_enc",), order=3
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)

    name: str
    # Where the project sits in the agent's home, as `~/projects/<slug>`. Derived from the
    # name when the project is created and never changed after, so a path written in a
    # transcript still names the same folder after a rename. Unique per owner.
    slug: str = Field(default="", index=True)
    # AEAD ciphertext — a host path is the operator's own content (see the module docstring).
    root_path_enc: str

    # Did *we* run `git init` on this directory? Recorded because it is a real, one-time
    # side effect on the operator's disk that they confirmed, and a later reader deserves
    # to know the repo was ours rather than theirs.
    git_initialized: bool = False
    # What a code conversation's branch is cut from, and what a merge lands back on.
    base_ref: str = "HEAD"

    archived: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_opened_at: datetime = Field(default_factory=utcnow, index=True)
