"""Fix: freeai_try_reserve heals quarantine when the window has expired.

Revision ID: 0003
Revises: 0002

Problem (see REVIEW § 1.2): the v1 function had two branches that kept a
provider permanently dead after the first quarantine:

  1. `IF NOT v_healthy AND v_quarantined = 0 THEN RETURN NULL` — once a call
     happened to leave `healthy=false` with `quarantined_until=0`, every
     future reservation returned NULL. This could happen because reset_health
     clears quarantined_until but the reserve path never re-checks the row.
  2. Quarantine expiring (quarantined_until <= now) never actually cleared
     `healthy` or reset `consecutive_failures`, so the next reservation read
     `v_healthy=false` and failed the above check.

Fix: make the function self-healing.

  - When quarantine has expired, UPDATE the row in place to reset
    healthy=true, consecutive_failures=0, quarantined_until=0 before deciding.
  - Remove the paranoid "shouldn't happen but be safe" branch — the state it
    guarded against is now correctly handled.

The function body is otherwise unchanged — same CREATE OR REPLACE, so live
traffic sees the fix atomically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
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
    -- Lock the stats row if it exists; create it otherwise
    SELECT quarantined_until, healthy INTO v_quarantined, v_healthy
    FROM provider_stats WHERE provider_name = p_name FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO provider_stats (provider_name) VALUES (p_name)
        ON CONFLICT (provider_name) DO NOTHING;
        v_quarantined := 0;
        v_healthy := TRUE;
    END IF;

    -- Still inside an active quarantine window? Block without touching state.
    IF v_quarantined > v_now THEN
        RETURN NULL;
    END IF;

    -- Quarantine has expired (or there never was one). If we're still marked
    -- unhealthy because of an old streak, heal the row so subsequent callers
    -- don't have to re-run this logic and so snapshot() is accurate.
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


# Original function body from migration 0001 — we restore it on downgrade
# so a rollback of 0003 puts the bug back rather than dropping the function
# entirely.
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
    IF NOT v_healthy AND v_quarantined = 0 THEN
        RETURN NULL;
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
