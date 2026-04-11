"""Fix: per-client rate limiting has its own table + function.

Revision ID: 0004
Revises: 0003

Problem (see REVIEW § 1.1): security.py reused the provider rate-limit
tables for per-client rate limiting by calling freeai_try_reserve with a
synthetic provider name like 'client:abc123'. Both `provider_stats` and
`rate_events` have foreign keys to `providers.name`, so the first real
client call would always blow up with a FK violation. The bug never
surfaced in dev because:

  1. Bootstrap mode (default) skips the client-auth path entirely.
  2. The integration tests for security.py were dropped during the Sprint 2
     Postgres migration and never restored.

Fix: give client rate limiting its own minimal table and its own plpgsql
function. No FK to providers, no shared state, no synthetic names, no
advisory-lock hack.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_rate_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("client_hash", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.Float, nullable=False),
    )
    # Queries filter by (client_hash, occurred_at >= now - 60), so a composite
    # index on those two columns serves the window COUNT(*) directly.
    op.create_index(
        "ix_client_rate_events_hash_time",
        "client_rate_events",
        ["client_hash", "occurred_at"],
    )

    op.execute("""
    CREATE OR REPLACE FUNCTION freeai_try_reserve_client(
        p_hash TEXT,
        p_rpm INTEGER
    ) RETURNS BIGINT AS $$
    DECLARE
        v_now DOUBLE PRECISION := EXTRACT(EPOCH FROM NOW());
        v_count INTEGER;
        v_id BIGINT;
    BEGIN
        -- Take a lock keyed on the client hash so concurrent requests for
        -- the same client serialize through the check-and-insert. Unlike
        -- pg_advisory_xact_lock, hashtextextended is deterministic across
        -- processes, which the Python `hash()` version in security.py was
        -- not (see REVIEW § 1.3).
        PERFORM pg_advisory_xact_lock(hashtextextended(p_hash, 0));

        IF p_rpm IS NOT NULL THEN
            SELECT COUNT(*) INTO v_count FROM client_rate_events
                WHERE client_hash = p_hash AND occurred_at >= v_now - 60;
            IF v_count >= p_rpm THEN
                RETURN NULL;
            END IF;
        END IF;

        INSERT INTO client_rate_events (client_hash, occurred_at)
        VALUES (p_hash, v_now)
        RETURNING id INTO v_id;
        RETURN v_id;
    END;
    $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS freeai_try_reserve_client(TEXT, INTEGER)")
    op.drop_index("ix_client_rate_events_hash_time", table_name="client_rate_events")
    op.drop_table("client_rate_events")
