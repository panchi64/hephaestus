"""managed model launch options

Revision ID: 7b1c4e0a92df
Revises: fd15ea21e8dd
Create Date: 2026-08-17 19:20:11.284915

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b1c4e0a92df'
down_revision: str | Sequence[str] | None = 'fd15ea21e8dd'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Existing rows get an empty object, which the service reads as "every engine default
    # stands" — the behaviour they were already served with.
    with op.batch_alter_table('managed_models', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'launch_options',
                sa.JSON(),
                nullable=False,
                server_default='{}',
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('managed_models', schema=None) as batch_op:
        batch_op.drop_column('launch_options')
