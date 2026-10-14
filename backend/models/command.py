"""Workflows schema — a prompt the operator wrote once and invokes by name.

The fourth source the command registry merges, and the only one it **owns**. The other
three are views onto things that exist for their own reasons: an action already has a
control in the interface, a skill is know-how the model opens, a sub-agent is a roster
entry. A workflow exists only to be typed — it is the operator saying "when I write
``/standup``, put this in front of the model" — so nothing else in the platform has a row
to lend it.

**Not a skill, though it is the near neighbour.** A skill is written for the *model* to
choose and open, carries a bundle of supporting files, and has to survive a round trip
through the Agent Skills standard. A workflow is a body of text with a name on it that only
the operator can reach. Storing one as a draft skill would put it in the model's catalog
(where it would be chosen by description and read as know-how) or leave it unpublished
(where it is invisible to everything, including the picker). Neither is what a saved prompt
is, which is why this is one small table rather than a flag on that one.

At-rest posture follows skills exactly, including the one deliberate weakening: ``name`` is
clear because it is the uniqueness key *and* the handle the operator types, so it cannot be
ciphertext. Everything that is content — the title, the one-line description the picker
shows, the argument hint, and the body itself — is sealed under the vault.

``enabled`` rather than ``published``, because they are not the same boundary and the word
matters. Publishing a skill hands it to the *model*; there is no equivalent act here, since
a workflow is never offered to anything but the picker. What this flag does is let a
half-written one stay out of the menu until its author is ready — a convenience, not a
trust decision, and calling it ``published`` would imply a gate that does not exist.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlmodel import Field, UniqueConstraint

from models._backup import BackupSpec
from models._fields import new_id, utcnow
from models._table import Table, TableArgs


class Command(Table, table=True):
    __tablename__: ClassVar[str] = "commands"
    __table_args__: ClassVar[TableArgs] = (
        UniqueConstraint("owner_id", "name", name="uq_commands_owner_name"),
    )
    # Its own export section rather than a lodger in ``skills``: the operator chooses what
    # to carry to another host by section, and "my saved prompts" and "my skills" are
    # separate answers to that question. Sections are discovered from these markers, so
    # there is no central list this has to be added to.
    __backup__: ClassVar[BackupSpec] = BackupSpec(section="commands", natural_key=("name",))

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_id: str = Field(index=True)
    # What the operator types after the slash. Clear, for the reason a skill's name is:
    # it is the uniqueness key and the handle at once. The leak is the list of names they
    # invoke, which is the accepted cost of the name being addressable at all.
    name: str = Field(index=True)
    # AEAD ciphertext of everything the operator wrote: the picker's row label, the line
    # under it, the hint about what to type after the name, and the template itself.
    title_enc: str
    description_enc: str
    body_enc: str
    argument_hint_enc: str | None = Field(default=None)
    # Out of the menu without being deleted — see the module docstring on why this is not
    # called ``published``.
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow, index=True)
