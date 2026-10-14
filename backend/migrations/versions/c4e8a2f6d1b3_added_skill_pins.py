"""Added skill pins

Revision ID: c4e8a2f6d1b3
Revises: b7d2e4a1c905
Create Date: 2026-10-12 23:55:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e8a2f6d1b3"
down_revision: str | Sequence[str] | None = "b7d2e4a1c905"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("skills", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("skills", schema=None) as batch_op:
        batch_op.drop_column("pinned")
