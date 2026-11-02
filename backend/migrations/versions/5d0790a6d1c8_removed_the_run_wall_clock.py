"""removed the run wall clock

Revision ID: 5d0790a6d1c8
Revises: a7c3e91d24b0
Create Date: 2026-11-01 23:30:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d0790a6d1c8'
down_revision: str | Sequence[str] | None = 'a7c3e91d24b0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.drop_column('wall_clock_timeout_s')

    op.execute(sa.text("DELETE FROM app_settings WHERE key = 'chat.wall_clock_timeout_s'"))


def downgrade() -> None:
    """Downgrade schema. The operator's deleted setting is not restored."""
    with op.batch_alter_table('agents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('wall_clock_timeout_s', sa.Float(), nullable=True))
