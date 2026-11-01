"""initial

Revision ID: a41c
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "a41c"
down_revision = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
    )
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade():
    op.drop_table("notes")
    op.drop_table("users")
