"""Add group_lightrag_config table

Revision ID: f1a2b3c4d5e6
Revises: 37f288994c47
Create Date: 2026-02-03 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "c440947495f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create group_lightrag_config table for linking groups to LightRAG workspaces."""
    op.create_table(
        "group_lightrag_config",
        sa.Column("id", sa.Text(), primary_key=True, unique=True, nullable=False),
        sa.Column(
            "group_id",
            sa.Text(),
            sa.ForeignKey("group.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        # Nullable for graceful migration - existing groups won't have these
        sa.Column("lightrag_url", sa.Text(), nullable=True),
        sa.Column("lightrag_workspace_name", sa.Text(), nullable=True),
        sa.Column("knowledge_base_id", sa.Text(), nullable=True),  # Link to auto-created KB
        sa.Column("created_at", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
    )
    
    # Create index on group_id for faster lookups
    op.create_index(
        "ix_group_lightrag_config_group_id",
        "group_lightrag_config",
        ["group_id"],
    )


def downgrade() -> None:
    """Drop group_lightrag_config table."""
    op.drop_index("ix_group_lightrag_config_group_id", table_name="group_lightrag_config")
    op.drop_table("group_lightrag_config")
