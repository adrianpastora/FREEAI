"""Add users and refresh_tokens tables for multi-user JWT auth.

Revision ID: 0012
Revises: 0011
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column(
            "role",
            sa.String(16),
            nullable=False,
            server_default="user",
        ),
        sa.Column("max_clients", sa.Integer, nullable=False, server_default="5"),
        sa.Column(
            "created_at",
            sa.Float,
            nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
        sa.Column(
            "updated_at",
            sa.Float,
            nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column(
            "created_at",
            sa.Float,
            nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
    )
    op.create_index(
        "ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
