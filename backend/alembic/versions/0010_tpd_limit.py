"""Add tpd_limit (tokens per day) to providers table.

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "providers",
        sa.Column("tpd_limit", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("providers", "tpd_limit")
