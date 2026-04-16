"""usage_daily_rollup — long-retention daily aggregates of usage_events.

Pre-aggregated daily summaries keyed by (user_id, day, provider, model, strategy)
so analytics over long windows (>30d) don't re-scan the raw events table.

  • Detailed events (usage_events) keep 90-day retention as before.
  • Rollups keep 730 days (2 years) — small footprint, enables year-over-year.
  • Populated hourly by the rollup_daily background task in main.py.

Revision ID: 0017
Revises: 0016
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_daily_rollup",
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("model", sa.String(256), nullable=False, server_default=""),
        sa.Column("strategy", sa.String(32), nullable=False),
        sa.Column("total_calls", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("success_calls", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("failed_calls", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("sum_latency_ms", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("p50_latency_ms", sa.Integer, nullable=True),
        sa.Column("p95_latency_ms", sa.Integer, nullable=True),
        sa.Column("p99_latency_ms", sa.Integer, nullable=True),
        sa.Column("avg_ttfb_ms", sa.Integer, nullable=True),
        sa.Column("prompt_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("errors_by_kind", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("fallback_position_hist", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "updated_at",
            sa.Float,
            nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "day", "provider_name", "model", "strategy",
            name="pk_usage_daily_rollup",
        ),
    )
    op.create_index(
        "ix_usage_daily_rollup_user_day",
        "usage_daily_rollup",
        ["user_id", "day"],
    )
    op.create_index(
        "ix_usage_daily_rollup_day",
        "usage_daily_rollup",
        ["day"],
    )
    op.create_index(
        "ix_usage_daily_rollup_user_provider_day",
        "usage_daily_rollup",
        ["user_id", "provider_name", "day"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_daily_rollup_user_provider_day", table_name="usage_daily_rollup")
    op.drop_index("ix_usage_daily_rollup_day", table_name="usage_daily_rollup")
    op.drop_index("ix_usage_daily_rollup_user_day", table_name="usage_daily_rollup")
    op.drop_table("usage_daily_rollup")
