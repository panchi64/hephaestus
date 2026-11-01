"""Merged the private-uploads and dev migration heads

Revision ID: 58fd0787db4d
Revises: 73e04c51a348, bb4613043c2e
Create Date: 2026-10-31 16:16:22.125399

"""
from collections.abc import Sequence


# revision identifiers, used by Alembic.
revision: str = '58fd0787db4d'
down_revision: str | Sequence[str] | None = ('73e04c51a348', 'bb4613043c2e')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
