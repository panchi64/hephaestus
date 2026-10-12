"""Added plugins

Revision ID: e8b3c6d2f4a1
Revises: d5f1a7c3e9b2
Create Date: 2026-10-13 01:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = "e8b3c6d2f4a1"
down_revision: Union[str, Sequence[str], None] = "d5f1a7c3e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "plugins",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("owner_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("active_enc", sa.LargeBinary(), nullable=True),
        sa.Column("draft_enc", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_plugins_owner_name"),
    )
    with op.batch_alter_table("plugins", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plugins_name"), ["name"], unique=False)
        batch_op.create_index(batch_op.f("ix_plugins_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_plugins_published"), ["published"], unique=False)
    op.create_table(
        "plugin_secrets",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("owner_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("plugin_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("domain", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("value_enc", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "name", name="uq_plugin_secrets_plugin_name"),
    )
    with op.batch_alter_table("plugin_secrets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plugin_secrets_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_plugin_secrets_plugin_id"), ["plugin_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("plugin_secrets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugin_secrets_plugin_id"))
        batch_op.drop_index(batch_op.f("ix_plugin_secrets_owner_id"))
    op.drop_table("plugin_secrets")
    with op.batch_alter_table("plugins", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugins_published"))
        batch_op.drop_index(batch_op.f("ix_plugins_owner_id"))
        batch_op.drop_index(batch_op.f("ix_plugins_name"))
    op.drop_table("plugins")
