"""Add user_providers table for per-user provider credentials.

Migrates existing provider API keys into user_providers for user_id=1
(a placeholder — the migrate-token wizard will assign the real admin user).
The providers table becomes a key-less catalog.

Revision ID: 0013
Revises: 0012
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_providers",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_name",
            sa.String(64),
            sa.ForeignKey("providers.name", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("api_key_encrypted", sa.Text, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("rpm_limit", sa.Integer, nullable=True),
        sa.Column("rpd_limit", sa.Integer, nullable=True),
        sa.Column("tpd_limit", sa.Integer, nullable=True),
        sa.Column("weight", sa.Float, nullable=True),
        sa.Column("default_model", sa.String(256), nullable=True),
        sa.Column(
            "created_at",
            sa.Float,
            nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
        sa.Column(
            "updated_at",
            sa.Float,
            nullable=False,
            server_default=sa.text("EXTRACT(EPOCH FROM NOW())"),
        ),
        sa.UniqueConstraint("user_id", "provider_name", name="uq_user_provider"),
    )
    op.create_index(
        "ix_user_providers_user_id", "user_providers", ["user_id"]
    )

    # ── Data migration ──
    # Move existing provider API keys to user_providers for placeholder user_id=1.
    # This works even if no user exists yet — the migrate-token wizard will
    # create the admin user with id=1 and the FK will be satisfied.
    # If users table already has rows (fresh setup), use the first admin.
    conn = op.get_bind()
    conn.execute(sa.text("""
        INSERT INTO user_providers (user_id, provider_name, api_key_encrypted, enabled,
                                     rpm_limit, rpd_limit, tpd_limit, weight, default_model)
        SELECT COALESCE(
                   (SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1),
                   1
               ),
               p.name, p.api_key_encrypted, p.enabled,
               p.rpm_limit, p.rpd_limit, p.tpd_limit, p.weight, p.default_model
        FROM providers p
        WHERE p.api_key_encrypted IS NOT NULL
          AND p.api_key_encrypted != ''
    """))

    # Clear keys from the catalog (providers table becomes key-less)
    conn.execute(sa.text("""
        UPDATE providers SET api_key_encrypted = NULL
    """))


def downgrade() -> None:
    # Restore keys back to providers from user_providers (best-effort: first user's keys)
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE providers p
        SET api_key_encrypted = up.api_key_encrypted
        FROM user_providers up
        WHERE up.provider_name = p.name
          AND up.user_id = (SELECT MIN(user_id) FROM user_providers)
    """))
    op.drop_index("ix_user_providers_user_id", table_name="user_providers")
    op.drop_table("user_providers")
