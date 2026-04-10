"""Add knowledge_base_id to group_lightrag_config

Revision ID: g2h3i4j5k6l7
Revises: f1a2b3c4d5e6
Create Date: 2026-02-03 10:56:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "g2h3i4j5k6l7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add knowledge_base_id column to group_lightrag_config table."""
    # op.add_column(
    #     "group_lightrag_config",
    #     sa.Column("knowledge_base_id", sa.Text(), nullable=True),
    # )


def downgrade() -> None:
    """Remove knowledge_base_id column from group_lightrag_config table."""
    op.drop_column("group_lightrag_config", "knowledge_base_id")
