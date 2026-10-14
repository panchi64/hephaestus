"""merged the egress-grant and scoped-approval-grant heads

Revision ID: 82cdb76f67d8
Revises: c7e2a91f0d34, c9a4e07b31d5
Create Date: 2026-09-07 09:22:56.517453

"""
from collections.abc import Sequence


# revision identifiers, used by Alembic.
revision: str = '82cdb76f67d8'
down_revision: str | Sequence[str] | None = ('c7e2a91f0d34', 'c9a4e07b31d5')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
