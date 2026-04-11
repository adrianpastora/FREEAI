"""usage_events + strategies tables, plus model validation helper.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ──────────────── usage_events ────────────────
    # Append-only telemetry of every completion that actually dispatched.
    # Used for analytics (provider share, latency histograms, outcome split)
    # and kept separate from rate_events because:
    #   • different retention policies (rate_events is purged aggressively,
    #     usage_events keeps longer history)
    #   • different shape (rate_events is just a timestamp column; usage has
    #     outcome, tokens, latency, strategy, etc.)
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.Float, nullable=False),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("model", sa.String(256), nullable=True),
        sa.Column("strategy", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),  # success | rate_limited | ...
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fallback_position", sa.Integer, nullable=False, server_default="1"),
        sa.Column("client_hash", sa.String(64), nullable=True),
    )
    op.create_index("ix_usage_events_time", "usage_events", ["occurred_at"])
    op.create_index("ix_usage_events_provider_time", "usage_events", ["provider_name", "occurred_at"])

    # ──────────────── strategies ────────────────
    # Routing strategies are now data, not code. Seeded on first run with the
    # defaults that used to live in orchestrator.STRATEGY_TAGS.
    op.create_table(
        "strategies",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("is_builtin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.Float, nullable=False, server_default=sa.text("EXTRACT(EPOCH FROM NOW())")),
    )


def downgrade() -> None:
    op.drop_table("strategies")
    op.drop_index("ix_usage_events_provider_time", table_name="usage_events")
    op.drop_index("ix_usage_events_time", table_name="usage_events")
    op.drop_table("usage_events")
