"""message wall-clock timings

Revision ID: a3c81f402e77
Revises: c4d1f7a90b62
Create Date: 2026-08-28 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c81f402e77'
down_revision: str | Sequence[str] | None = 'c4d1f7a90b62'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('messages', schema=None) as batch_op:
        # Nullable with no server_default, deliberately: null means "not measured",
        # which is exactly what every existing row is and what a response from an
        # endpoint that streamed no content still is. A 0 default would turn an
        # unmeasured backlog into a thread that appears to have cost no time at all.
        batch_op.add_column(sa.Column('llm_ms', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('ttft_ms', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tool_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('messages', schema=None) as batch_op:
        batch_op.drop_column('tool_ms')
        batch_op.drop_column('ttft_ms')
        batch_op.drop_column('llm_ms')
