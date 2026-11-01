"""drop note titles

Revision ID: b7e2
Revises: a41c
"""

from alembic import op

revision = "b7e2"
down_revision = "a41c"


def upgrade():
    op.execute("UPDATE notes SET body = body")


def downgrade():
    pass
