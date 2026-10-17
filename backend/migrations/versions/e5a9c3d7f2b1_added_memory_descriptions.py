"""Added memory descriptions

Revision ID: e5a9c3d7f2b1
Revises: 3d32511362d6
Create Date: 2026-10-15 23:30:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5a9c3d7f2b1"
down_revision: str | Sequence[str] | None = "3d32511362d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.add_column(sa.Column("description_enc", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_column("description_enc")
