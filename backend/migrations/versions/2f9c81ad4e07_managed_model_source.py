"""managed model source

Revision ID: 2f9c81ad4e07
Revises: 7b1c4e0a92df
Create Date: 2026-08-18 20:05:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f9c81ad4e07'
down_revision: str | Sequence[str] | None = '7b1c4e0a92df'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Where the artifact came from, which decides whether deleting the row may remove the
    # weights. Every existing row is something we downloaded into the models dir, so the
    # default keeps their current (removable) behaviour; only a model imported from a path
    # the operator chose is marked "local" and left alone on disk.
    with op.batch_alter_table('managed_models', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'source',
                sa.String(),
                nullable=False,
                server_default='huggingface',
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('managed_models', schema=None) as batch_op:
        batch_op.drop_column('source')
