"""Store admin token as bcrypt hash in app_config for UI initial setup.

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("admin_token_hash", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_config", "admin_token_hash")
