"""Scope clients, provider_stats, rate_events, and usage_events to users.

- clients: add user_id FK
- rate_events: add user_id, update index
- provider_stats: recreate with composite PK (user_id, provider_name)
- usage_events: add user_id (nullable for historical data)
- Rewrite freeai_try_reserve plpgsql to accept user_id

Revision ID: 0014
Revises: 0013
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. clients: add user_id ──
    op.add_column(
        "clients",
        sa.Column("user_id", sa.Integer, nullable=True),
    )
    # Assign existing clients to the bootstrap admin (created in 0013 if keys existed)
    conn = op.get_bind()
    admin_id = conn.execute(sa.text(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    )).scalar_one_or_none()
    if admin_id is not None:
        conn.execute(sa.text(
            "UPDATE clients SET user_id = :uid WHERE user_id IS NULL"
        ), {"uid": admin_id})
    # Delete orphan clients that have no user (shouldn't happen, but be safe)
    conn.execute(sa.text("DELETE FROM clients WHERE user_id IS NULL"))
    op.alter_column("clients", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_clients_user_id", "clients", "users",
        ["user_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_clients_user_id", "clients", ["user_id"])

    # ── 2. usage_events: add user_id ──
    op.add_column(
        "usage_events",
        sa.Column("user_id", sa.Integer, nullable=True),
    )
    # Backfill existing events to the admin user
    if admin_id is not None:
        conn.execute(sa.text(
            "UPDATE usage_events SET user_id = :uid WHERE user_id IS NULL"
        ), {"uid": admin_id})
    op.create_index("ix_usage_events_user_id", "usage_events", ["user_id"])

    # ── 3. rate_events: add user_id ──
    op.add_column(
        "rate_events",
        sa.Column("user_id", sa.Integer, nullable=True),
    )
    # Backfill existing events
    if admin_id is not None:
        conn.execute(sa.text(
            "UPDATE rate_events SET user_id = :uid WHERE user_id IS NULL"
        ), {"uid": admin_id})
    conn.execute(sa.text("DELETE FROM rate_events WHERE user_id IS NULL"))
    op.alter_column("rate_events", "user_id", nullable=False)
    # Replace old index with user-scoped one
    op.drop_index("ix_rate_events_provider_time", table_name="rate_events")
    op.create_index(
        "ix_rate_events_user_provider_time",
        "rate_events",
        ["user_id", "provider_name", "occurred_at"],
    )

    # ── 4. provider_stats: recreate with composite PK (user_id, provider_name) ──
    op.rename_table("provider_stats", "provider_stats_old")

    op.create_table(
        "provider_stats",
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column(
            "provider_name", sa.String(64),
            sa.ForeignKey("providers.name", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("healthy", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("quarantined_until", sa.Float, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_error_kind", sa.String(32), nullable=True),
        sa.Column("last_latency_ms", sa.Integer, nullable=True),
        sa.Column("latency_ema_ms", sa.Float, nullable=True),
        sa.Column("total_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_today", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("tokens_day_start", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.Float, nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
        sa.PrimaryKeyConstraint("user_id", "provider_name"),
    )

    # Migrate existing data (assign to bootstrap admin if one exists)
    if admin_id is not None:
        conn.execute(sa.text("""
            INSERT INTO provider_stats (
                user_id, provider_name, healthy, consecutive_failures,
                quarantined_until, last_error, last_error_kind, last_latency_ms,
                latency_ema_ms, total_calls, total_failures,
                tokens_today, tokens_day_start, updated_at
            )
            SELECT
                :uid,
                provider_name, healthy, consecutive_failures,
                quarantined_until, last_error, last_error_kind, last_latency_ms,
                latency_ema_ms, total_calls, total_failures,
                tokens_today, tokens_day_start, updated_at
            FROM provider_stats_old
        """), {"uid": admin_id})
    op.drop_table("provider_stats_old")

    # ── 5. Rewrite plpgsql function with user_id parameter ──
    op.execute("DROP FUNCTION IF EXISTS freeai_try_reserve(TEXT, INTEGER, INTEGER)")
    op.execute("""
    CREATE OR REPLACE FUNCTION freeai_try_reserve(
        p_user_id INTEGER,
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
        SELECT quarantined_until, healthy INTO v_quarantined, v_healthy
        FROM provider_stats
        WHERE user_id = p_user_id AND provider_name = p_name
        FOR UPDATE;

        IF NOT FOUND THEN
            INSERT INTO provider_stats (user_id, provider_name)
            VALUES (p_user_id, p_name)
            ON CONFLICT (user_id, provider_name) DO NOTHING;
            v_quarantined := 0;
            v_healthy := TRUE;
        END IF;

        IF v_quarantined > v_now THEN
            RETURN NULL;
        END IF;
        IF NOT v_healthy AND v_quarantined = 0 THEN
            RETURN NULL;
        END IF;

        IF p_rpm IS NOT NULL THEN
            SELECT COUNT(*) INTO v_minute_count FROM rate_events
                WHERE user_id = p_user_id
                  AND provider_name = p_name
                  AND occurred_at >= v_now - 60;
            IF v_minute_count >= p_rpm THEN
                RETURN NULL;
            END IF;
        END IF;
        IF p_rpd IS NOT NULL THEN
            SELECT COUNT(*) INTO v_day_count FROM rate_events
                WHERE user_id = p_user_id
                  AND provider_name = p_name
                  AND occurred_at >= v_now - 86400;
            IF v_day_count >= p_rpd THEN
                RETURN NULL;
            END IF;
        END IF;

        INSERT INTO rate_events (user_id, provider_name, occurred_at)
        VALUES (p_user_id, p_name, v_now)
        RETURNING id INTO v_id;
        RETURN v_id;
    END;
    $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    # Revert plpgsql function
    op.execute("DROP FUNCTION IF EXISTS freeai_try_reserve(INTEGER, TEXT, INTEGER, INTEGER)")
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
        SELECT quarantined_until, healthy INTO v_quarantined, v_healthy
        FROM provider_stats WHERE provider_name = p_name FOR UPDATE;
        IF NOT FOUND THEN
            INSERT INTO provider_stats (provider_name) VALUES (p_name)
            ON CONFLICT (provider_name) DO NOTHING;
            v_quarantined := 0;
            v_healthy := TRUE;
        END IF;
        IF v_quarantined > v_now THEN RETURN NULL; END IF;
        IF NOT v_healthy AND v_quarantined = 0 THEN RETURN NULL; END IF;
        IF p_rpm IS NOT NULL THEN
            SELECT COUNT(*) INTO v_minute_count FROM rate_events
                WHERE provider_name = p_name AND occurred_at >= v_now - 60;
            IF v_minute_count >= p_rpm THEN RETURN NULL; END IF;
        END IF;
        IF p_rpd IS NOT NULL THEN
            SELECT COUNT(*) INTO v_day_count FROM rate_events
                WHERE provider_name = p_name AND occurred_at >= v_now - 86400;
            IF v_day_count >= p_rpd THEN RETURN NULL; END IF;
        END IF;
        INSERT INTO rate_events (provider_name, occurred_at) VALUES (p_name, v_now)
        RETURNING id INTO v_id;
        RETURN v_id;
    END;
    $$ LANGUAGE plpgsql;
    """)

    # Revert provider_stats to single PK
    op.rename_table("provider_stats", "provider_stats_new")
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
        sa.Column("latency_ema_ms", sa.Float, nullable=True),
        sa.Column("total_calls", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_today", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("tokens_day_start", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.Float, nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
    )
    conn = op.get_bind()
    conn.execute(sa.text("""
        INSERT INTO provider_stats (
            provider_name, healthy, consecutive_failures, quarantined_until,
            last_error, last_error_kind, last_latency_ms, latency_ema_ms,
            total_calls, total_failures, tokens_today, tokens_day_start, updated_at
        )
        SELECT DISTINCT ON (provider_name)
            provider_name, healthy, consecutive_failures, quarantined_until,
            last_error, last_error_kind, last_latency_ms, latency_ema_ms,
            total_calls, total_failures, tokens_today, tokens_day_start, updated_at
        FROM provider_stats_new
        ORDER BY provider_name, user_id
    """))
    op.drop_table("provider_stats_new")

    # Revert rate_events
    op.drop_index("ix_rate_events_user_provider_time", table_name="rate_events")
    op.create_index("ix_rate_events_provider_time", "rate_events", ["provider_name", "occurred_at"])
    op.drop_column("rate_events", "user_id")

    # Revert usage_events
    op.drop_index("ix_usage_events_user_id", table_name="usage_events")
    op.drop_column("usage_events", "user_id")

    # Revert clients
    op.drop_index("ix_clients_user_id", table_name="clients")
    op.drop_constraint("fk_clients_user_id", "clients", type_="foreignkey")
    op.drop_column("clients", "user_id")
