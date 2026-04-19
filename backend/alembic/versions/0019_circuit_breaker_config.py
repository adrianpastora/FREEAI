"""Configurable circuit breaker for provider failures.

Extends the existing consecutive-failures quarantine with:
  - configurable threshold / base cooldown / max cooldown in app_config
  - a sliding failure window so stale failures don't accumulate forever
    (provider_stats.recent_failures_started_at)
  - cooldown_level so repeated trips escalate exponentially

Defaults keep prior behavior approximately intact (threshold 3, base 30s,
max 600s, window 300s).

Revision ID: 0019
Revises: 0018
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column(
            "circuit_breaker_threshold", sa.Integer,
            nullable=False, server_default="3",
        ),
    )
    op.add_column(
        "app_config",
        sa.Column(
            "circuit_breaker_window_s", sa.Integer,
            nullable=False, server_default="300",
        ),
    )
    op.add_column(
        "app_config",
        sa.Column(
            "circuit_breaker_base_cooldown_s", sa.Integer,
            nullable=False, server_default="30",
        ),
    )
    op.add_column(
        "app_config",
        sa.Column(
            "circuit_breaker_max_cooldown_s", sa.Integer,
            nullable=False, server_default="3600",
        ),
    )
    op.add_column(
        "provider_stats",
        sa.Column(
            "recent_failures_started_at", sa.Float,
            nullable=False, server_default="0",
        ),
    )
    op.add_column(
        "provider_stats",
        sa.Column(
            "cooldown_level", sa.SmallInteger,
            nullable=False, server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("provider_stats", "cooldown_level")
    op.drop_column("provider_stats", "recent_failures_started_at")
    op.drop_column("app_config", "circuit_breaker_max_cooldown_s")
    op.drop_column("app_config", "circuit_breaker_base_cooldown_s")
    op.drop_column("app_config", "circuit_breaker_window_s")
    op.drop_column("app_config", "circuit_breaker_threshold")
