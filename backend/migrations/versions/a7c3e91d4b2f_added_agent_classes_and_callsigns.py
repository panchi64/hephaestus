"""Added agent classes and callsigns

Revision ID: a7c3e91d4b2f
Revises: e5a9c3d7f2b1
Create Date: 2026-10-15 23:45:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c3e91d4b2f"
down_revision: str | Sequence[str] | None = "e5a9c3d7f2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("subagent_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("agent_class", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("callsign", sa.String(), nullable=False, server_default=""))
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("agent_class", sa.String(), nullable=False, server_default="tripodes")
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_column("agent_class")
    with op.batch_alter_table("subagent_records", schema=None) as batch_op:
        batch_op.drop_column("callsign")
        batch_op.drop_column("agent_class")
