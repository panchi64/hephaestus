"""Dropped the per-conversation tool-result compaction override

Tool-result compaction is gone — a large tool result now rides into context whole, and the
one reduction left (folding a thread's older turns into a summary) keeps its own
`auto_compact_override`. This column has no reader.

Revision ID: a2f60d9b4c31
Revises: c8e41a5b2f07
Create Date: 2026-08-22 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2f60d9b4c31'
down_revision: str | Sequence[str] | None = 'c8e41a5b2f07'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_column('compaction_override')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('compaction_override', sa.Boolean(), nullable=True))
