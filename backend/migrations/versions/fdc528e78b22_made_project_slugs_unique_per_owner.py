"""Made project slugs unique per owner

Revision ID: fdc528e78b22
Revises: be6be695a283
Create Date: 2026-10-15 19:14:54.828382

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'fdc528e78b22'
down_revision: str | Sequence[str] | None = 'be6be695a283'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_projects_owner_slug', ['owner_id', 'slug'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_constraint('uq_projects_owner_slug', type_='unique')
