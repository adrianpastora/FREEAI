"""Backfill usage_events.user_id for historical events.

Migration 0014 added the column but didn't backfill existing rows.
This assigns all NULL user_id events to the first admin user.

Revision ID: 0015
Revises: 0014
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    admin_id = conn.execute(sa.text(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    )).scalar_one_or_none()

    if admin_id is not None:
        conn.execute(sa.text(
            "UPDATE usage_events SET user_id = :uid WHERE user_id IS NULL"
        ), {"uid": admin_id})


def downgrade() -> None:
    pass  # No undo needed — the column stays
