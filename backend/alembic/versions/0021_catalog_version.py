"""Track which provider catalog version has been applied.

Adds ``app_config.catalog_version``. ``ConfigRepository.sync_catalog`` runs
at startup and, when the stored value differs from
``app.providers.catalog.CATALOG_VERSION``, pushes the catalog defaults
(default models, limits, tags, seed prices) into the database. Future
catalog refreshes therefore need a version bump, not a new migration.

NULL on existing installs → the first boot after upgrading applies the
2026-10 catalog refresh (retired Groq/OpenRouter/HF models, Gemini 3.5,
NVIDIA NIM).

Revision ID: 0021
Revises: 0020
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("catalog_version", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_config", "catalog_version")
