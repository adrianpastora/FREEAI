"""Fix orphaned user_providers from placeholder migration.

When a user registered via /api/auth/register instead of /api/auth/migrate-token,
the placeholder user (with password '__placeholder_needs_migration__') kept the
provider keys while the real admin got none. This migration:
1. Finds the placeholder user (if any)
2. Finds the first real admin
3. Transfers all user_providers, clients, rate_events, usage_events
4. Deletes the placeholder

Revision ID: 0016
Revises: 0015
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Find the placeholder user
    placeholder = conn.execute(sa.text(
        "SELECT id FROM users WHERE password_hash = '__placeholder_needs_migration__' LIMIT 1"
    )).scalar_one_or_none()

    if placeholder is None:
        return  # No placeholder — nothing to fix

    # Find the first real admin (not the placeholder)
    real_admin = conn.execute(sa.text(
        "SELECT id FROM users WHERE role = 'admin' AND id != :pid ORDER BY id LIMIT 1"
    ), {"pid": placeholder}).scalar_one_or_none()

    if real_admin is None:
        # No real admin exists yet — placeholder IS the only user, leave it
        return

    # Transfer user_providers (only those not already owned by real_admin)
    conn.execute(sa.text("""
        UPDATE user_providers SET user_id = :new_uid
        WHERE user_id = :old_uid
          AND provider_name NOT IN (
              SELECT provider_name FROM user_providers WHERE user_id = :new_uid
          )
    """), {"old_uid": placeholder, "new_uid": real_admin})
    # Delete remaining duplicates
    conn.execute(sa.text(
        "DELETE FROM user_providers WHERE user_id = :old_uid"
    ), {"old_uid": placeholder})

    # Transfer clients
    conn.execute(sa.text(
        "UPDATE clients SET user_id = :new_uid WHERE user_id = :old_uid"
    ), {"old_uid": placeholder, "new_uid": real_admin})

    # Transfer rate_events
    conn.execute(sa.text(
        "UPDATE rate_events SET user_id = :new_uid WHERE user_id = :old_uid"
    ), {"old_uid": placeholder, "new_uid": real_admin})

    # Transfer usage_events
    conn.execute(sa.text(
        "UPDATE usage_events SET user_id = :new_uid WHERE user_id = :old_uid"
    ), {"old_uid": placeholder, "new_uid": real_admin})

    # Delete provider_stats for placeholder (will be recreated)
    conn.execute(sa.text(
        "DELETE FROM provider_stats WHERE user_id = :old_uid"
    ), {"old_uid": placeholder})

    # Delete refresh tokens for placeholder
    conn.execute(sa.text(
        "DELETE FROM refresh_tokens WHERE user_id = :old_uid"
    ), {"old_uid": placeholder})

    # Delete the placeholder user
    conn.execute(sa.text(
        "DELETE FROM users WHERE id = :pid"
    ), {"pid": placeholder})


def downgrade() -> None:
    pass  # Cannot undo — the placeholder is gone
