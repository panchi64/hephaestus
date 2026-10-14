"""Recorded whether an approval grant answers or only informs

Revision ID: a7c31e9b5d04
Revises: 6ff96c574e3b
Create Date: 2026-09-17 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c31e9b5d04'
down_revision: str | Sequence[str] | None = '6ff96c574e3b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # The operator can now give a standing yes to a whole tool on purpose — every command
    # it runs, for the rest of the thread, taken as their authorization by the level that
    # reviews rather than merely informing it. This column is the difference between that
    # and the narrow yes a single approval records.
    #
    # NOT NULL DEFAULT 0, so every row already in the table keeps exactly the meaning it
    # was written with: a grant nobody chose the wider option for informs, as it always
    # did. That includes a scheduled task's pre-authorization seeds, which are whole-tool
    # by construction and must not be promoted by a column arriving under them.
    with op.batch_alter_table('approval_grants', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'decisive',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    # Every grant goes back to informing. That is the narrower reading, so a thread
    # downgraded mid-flight asks again rather than running something unasked.
    with op.batch_alter_table('approval_grants', schema=None) as batch_op:
        batch_op.drop_column('decisive')
