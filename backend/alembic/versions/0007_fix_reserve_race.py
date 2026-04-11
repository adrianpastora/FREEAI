"""Fix race in freeai_try_reserve when provider_stats row does not exist.

Revision ID: 0007
Revises: 0006

Problem: when the provider_stats row doesn't exist yet, N concurrent sessions
all see NOT FOUND on the SELECT ... FOR UPDATE, then all attempt INSERT ON
CONFLICT DO NOTHING. Only one inserts; the rest get DO NOTHING and proceed
*without* the FOR UPDATE lock. They count rate_events in parallel and can
over-admit.

Fix: replace the SELECT-FOR-UPDATE + INSERT-ON-CONFLICT pattern with a single
INSERT ... ON CONFLICT DO UPDATE SET provider_name = EXCLUDED.provider_name.
This upsert atomically creates-or-locks the row in one statement: if the row
already exists the DO UPDATE acquires the same exclusive row lock as FOR UPDATE.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels = None
depends_on = None


_NEW_FN = """
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
    -- Upsert: creates the row if missing, otherwise acquires an exclusive
    -- row lock (same as FOR UPDATE) via the DO UPDATE clause.
    INSERT INTO provider_stats (provider_name)
    VALUES (p_name)
    ON CONFLICT (provider_name)
    DO UPDATE SET provider_name = EXCLUDED.provider_name
    RETURNING quarantined_until, healthy
    INTO v_quarantined, v_healthy;

    -- Still inside an active quarantine window? Block without touching state.
    IF v_quarantined > v_now THEN
        RETURN NULL;
    END IF;

    -- Quarantine has expired (or there never was one). Heal if needed.
    IF NOT v_healthy OR v_quarantined > 0 THEN
        UPDATE provider_stats
        SET healthy = TRUE,
            quarantined_until = 0,
            consecutive_failures = 0
        WHERE provider_name = p_name;
        v_healthy := TRUE;
        v_quarantined := 0;
    END IF;

    -- Rolling window checks
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
"""

_OLD_FN = """
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

    IF v_quarantined > v_now THEN
        RETURN NULL;
    END IF;

    IF NOT v_healthy OR v_quarantined > 0 THEN
        UPDATE provider_stats
        SET healthy = TRUE,
            quarantined_until = 0,
            consecutive_failures = 0
        WHERE provider_name = p_name;
        v_healthy := TRUE;
        v_quarantined := 0;
    END IF;

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
"""


def upgrade() -> None:
    op.execute(_NEW_FN)


def downgrade() -> None:
    op.execute(_OLD_FN)
