"""Add latency EMA and incremental token counters to provider_stats.

Supports three scoring optimizations:
  1. latency_ema_ms — exponential moving average replaces single-sample latency
  2. tokens_today — incremental counter avoids SUM() over usage_events on hot path
  3. tokens_day_start — epoch timestamp for daily reset of tokens_today

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_stats",
        sa.Column("latency_ema_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "provider_stats",
        sa.Column("tokens_today", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "provider_stats",
        sa.Column("tokens_day_start", sa.Float(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("provider_stats", "tokens_day_start")
    op.drop_column("provider_stats", "tokens_today")
    op.drop_column("provider_stats", "latency_ema_ms")
