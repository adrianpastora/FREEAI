"""Replace strategies.tags with strategies.definition (DSL).

Revision ID: 0008
Revises: 0007

Sprint 7 — Strategy DSL rework, commit 2.

Why: the old `tags: text[]` column let users create strategies whose
tags didn't match any provider, silently degrading to baseline scoring.
The new `definition jsonb` column carries a structured DSL document
(see app.strategy_dsl) where every clause has a defined effect on
routing, and bad definitions are rejected at save time.

This migration:

  1. Adds `strategies.definition jsonb` (nullable — `auto` carries NULL).
  2. Rewrites every existing row's `tags` array into the equivalent DSL
     dict and writes it into `definition`. The translation is lossless:
     each tag becomes a `prefer.contains` clause with weight 5, which
     reproduces the old "5 / (i+1)" scoring behavior closely enough.
     The exact built-in definitions are restored on first app boot by
     `seed_builtins_if_missing()` — they OVERWRITE whatever this
     migration writes for built-in rows, which is fine because the
     migration's translation is conservative.
  3. Drops the `tags` column. No parallel universes — there's a clean
     break here that the legacy `tags` shim in /api/strategies bridges
     during commit 2 only.

Downgrade is best-effort: rebuilds `tags` from any `prefer.contains`
clauses found in `definition` and drops `definition`. Strategies that
used non-tag fields lose information. Documented as one-way for new
strategies.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the new column, nullable so existing rows don't fail.
    op.add_column(
        "strategies",
        sa.Column("definition", JSONB(), nullable=True),
    )

    # 2. Translate existing tags into the new DSL shape.
    #    Done in SQL with a CTE so we don't need to load anything into
    #    Python — works against any installation, regardless of how many
    #    custom strategies the operator has.
    #
    #    For each row:
    #      - if name = 'auto'        -> definition stays NULL
    #      - else                    -> definition = {require: [], prefer: [
    #                                     {field: tags, op: contains, value: <t>, weight: 5}
    #                                     for t in tags
    #                                   ]}
    op.execute(
        """
        UPDATE strategies
        SET definition = CASE
            WHEN name = 'auto' THEN NULL
            ELSE jsonb_build_object(
                'require', '[]'::jsonb,
                'prefer', COALESCE(
                    (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'field', 'tags',
                                'op', 'contains',
                                'value', t,
                                'weight', 5
                            )
                        )
                        FROM unnest(tags) AS t
                    ),
                    '[]'::jsonb
                )
            )
        END
        """
    )

    # 3. Drop the legacy column.
    op.drop_column("strategies", "tags")


def downgrade() -> None:
    # Re-add the tags column.
    op.add_column(
        "strategies",
        sa.Column(
            "tags",
            sa.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )

    # Best-effort: pull tag values back out of any prefer.contains clauses.
    # Strategies that used non-tag fields lose information here.
    op.execute(
        """
        UPDATE strategies
        SET tags = COALESCE(
            (
                SELECT array_agg(elem->>'value')
                FROM jsonb_array_elements(definition->'prefer') AS elem
                WHERE elem->>'field' = 'tags'
                  AND elem->>'op' = 'contains'
            ),
            '{}'::text[]
        )
        WHERE definition IS NOT NULL
        """
    )

    op.drop_column("strategies", "definition")
