"""Added a run capacity to model endpoints

Revision ID: b7d2e4a1c905
Revises: a3c1f0d9b7e2
Create Date: 2026-10-12 23:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7d2e4a1c905"
down_revision: Union[str, Sequence[str], None] = "a3c1f0d9b7e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("model_endpoints", schema=None) as batch_op:
        batch_op.add_column(sa.Column("max_concurrent_runs", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("model_endpoints", schema=None) as batch_op:
        batch_op.drop_column("max_concurrent_runs")
