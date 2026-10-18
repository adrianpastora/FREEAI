"""Persist /v1/responses conversation state.

Adds ``response_store``: the conversation history behind each response id
(for ``previous_response_id``) and individual output / input items (for
``item_reference``). Until now both lived in process memory, so every
restart or redeploy broke chained conversations. Rows expire after 24 h and
are purged by the periodic cleanup job.

Revision ID: 0022
Revises: 0021
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "response_store",
        sa.Column(
            "user_id", sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
        ),
        # "history" (key = resp_…) or "item" (key = msg_… / fc_… / client ids)
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("ix_response_store_expires_at", "response_store", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_response_store_expires_at", table_name="response_store")
    op.drop_table("response_store")
