"""Add fallback robustness knobs.

- ``user_providers.max_retries`` — per-provider retry budget override (NULL
  defers to the global default in app_config).
- ``app_config.provider_max_retries`` — global default retries per attempt.
- ``app_config.stream_idle_timeout_s`` — max seconds without a chunk before a
  streaming provider is treated as stalled (triggers fallback if no content
  was emitted yet).

Defaults preserve current behavior (1 retry, 45s idle).

Revision ID: 0018
Revises: 0017
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_providers",
        sa.Column("max_retries", sa.Integer, nullable=True),
    )
    op.add_column(
        "app_config",
        sa.Column(
            "provider_max_retries",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "app_config",
        sa.Column(
            "stream_idle_timeout_s",
            sa.Float,
            nullable=False,
            server_default="45.0",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_config", "stream_idle_timeout_s")
    op.drop_column("app_config", "provider_max_retries")
    op.drop_column("user_providers", "max_retries")
