"""Added per-conversation context overhead

Revision ID: a1f39d47c2be
Revises: b7ad41c9e208
Create Date: 2026-08-29 11:04:18.226104

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f39d47c2be'
down_revision: str | Sequence[str] | None = 'b7ad41c9e208'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # What the thread's last turn carried besides the conversation — the standing brief
    # and the tool schemas, itemised, in characters. Additive and nullable: an existing
    # thread reads null and simply shows no context breakdown until its next turn writes
    # one, which is what it did before this column existed.
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('context_overhead', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_column('context_overhead')
