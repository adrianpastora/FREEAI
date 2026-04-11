"""initial schema — providers, app_config, clients, provider_stats, rate_events

Revision ID: 0001
Revises:
Create Date: 2026-04-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("default_strategy", sa.String(32), nullable=False, server_default="auto"),
        sa.Column("enable_fallback", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.Float, nullable=False, server_default=sa.text("EXTRACT(EPOCH FROM NOW())")),
    )

    op.create_table(
        "providers",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("api_key_encrypted", sa.Text, nullable=True),
        sa.Column("rpm_limit", sa.Integer, nullable=True),
        sa.Column("rpd_limit", sa.Integer, nullable=True),
        sa.Column("weight", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("default_model", sa.String(256), nullable=True),
        sa.Column("updated_at", sa.Float, nullable=False, server_default=sa.text("EXTRACT(EPOCH FROM NOW())")),
    )

    op.create_table(
        "provider_stats",
        sa.Column(
            "provider_name", sa.String(64),
            sa.ForeignKey("providers.name", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("healthy", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("quarantined_until", sa.Float, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_error_kind", sa.String(32), nullable=True),
        sa.Column("last_latency_ms", sa.Integer, nullable=True),
        sa.Column("total_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Float, nullable=False, server_default=sa.text("EXTRACT(EPOCH FROM NOW())")),
    )

    op.create_table(
        "rate_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "provider_name", sa.String(64),
            sa.ForeignKey("providers.name", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.Float, nullable=False),
    )
    op.create_index("ix_rate_events_provider_time", "rate_events", ["provider_name", "occurred_at"])

    op.create_table(
        "clients",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("rpm_limit", sa.Integer, nullable=False, server_default="60"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Float, nullable=False, server_default=sa.text("EXTRACT(EPOCH FROM NOW())")),
    )

    # Atomic reservation function:
    # Inserts a rate_event row IFF the rolling rpm/rpd window has room.
    # Returns the new event id, or NULL if the provider is over capacity.
    # Wrapping the check + insert in plpgsql under SERIALIZABLE-like guarantees
    # of a single statement avoids the race the in-memory tracker had.
    op.execute("""
    CREATE OR REPLACE FUNCTION freeai_try_reserve(
        p_name TEXT,
        p_rpm INTEGER,
        p_rpd INTEGER
    ) RETURNS BIGINT AS $$
    DECLARE
        v_now DOUBLE PRECISION := EXTRACT(EPOCH FROM NOW());
        v_minute_count INTEGER;
        v_day_count INTEGER;
        v_id BIGINT;
        v_quarantined DOUBLE PRECISION;
        v_healthy BOOLEAN;
    BEGIN
        -- check quarantine first (cheap, table is tiny)
        SELECT quarantined_until, healthy INTO v_quarantined, v_healthy
        FROM provider_stats WHERE provider_name = p_name FOR UPDATE;
        IF NOT FOUND THEN
            -- first call ever — create the stats row
            INSERT INTO provider_stats (provider_name) VALUES (p_name)
            ON CONFLICT (provider_name) DO NOTHING;
            v_quarantined := 0;
            v_healthy := TRUE;
        END IF;

        IF v_quarantined > v_now THEN
            RETURN NULL;
        END IF;
        IF NOT v_healthy AND v_quarantined = 0 THEN
            -- shouldn't happen but be safe
            RETURN NULL;
        END IF;

        -- count rolling window
        IF p_rpm IS NOT NULL THEN
            SELECT COUNT(*) INTO v_minute_count FROM rate_events
                WHERE provider_name = p_name AND occurred_at >= v_now - 60;
            IF v_minute_count >= p_rpm THEN
                RETURN NULL;
            END IF;
        END IF;
        IF p_rpd IS NOT NULL THEN
            SELECT COUNT(*) INTO v_day_count FROM rate_events
                WHERE provider_name = p_name AND occurred_at >= v_now - 86400;
            IF v_day_count >= p_rpd THEN
                RETURN NULL;
            END IF;
        END IF;

        INSERT INTO rate_events (provider_name, occurred_at) VALUES (p_name, v_now)
        RETURNING id INTO v_id;
        RETURN v_id;
    END;
    $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS freeai_try_reserve(TEXT, INTEGER, INTEGER)")
    op.drop_table("clients")
    op.drop_index("ix_rate_events_provider_time", table_name="rate_events")
    op.drop_table("rate_events")
    op.drop_table("provider_stats")
    op.drop_table("providers")
    op.drop_table("app_config")
