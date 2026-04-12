"""Rename DSL field latency_p50_ms -> last_latency_ms in strategies.definition.

Revision ID: 0009
Revises: 0008

Why: the DSL field was named `latency_p50_ms` in commits 1-4 of the
strategy DSL rework, but the implementation only ever read
`ProviderSnapshot.last_latency_ms` (a single sample, not a percentile).
A user writing `latency_p50_ms < 1000` reasonably expected a windowed
percentile and would have been misled into thinking they were filtering
on aggregate behavior. Renaming the field to match what it actually is
removes that footgun.

This migration rewrites every `strategies.definition` row in place,
swapping the `field` value of any clause whose field is `latency_p50_ms`
to `last_latency_ms`. The change is purely textual inside the JSONB —
operators, values, and weights are preserved. Built-in strategies will
be re-overwritten by `seed_builtins_if_missing()` on the next app boot
with the new field name (already updated in strategy_repo.py); this
migration's job is to fix any user-created strategy that referenced the
old name.

The translation is done with a single SQL UPDATE that uses
`jsonb_agg(jsonb_set(...))` so it works regardless of how many clauses
each row has and doesn't need to load anything into Python.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rewrite require[*] and prefer[*] clauses where field == 'latency_p50_ms'.
    # The two sections are independent, so we update them with two passes for
    # readability — one CTE per section. NULL definitions ('auto') are skipped.
    op.execute(
        """
        UPDATE strategies
        SET definition = jsonb_set(
            definition,
            '{require}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        CASE
                            WHEN clause->>'field' = 'latency_p50_ms'
                                THEN jsonb_set(clause, '{field}', '"last_latency_ms"'::jsonb)
                            ELSE clause
                        END
                    )
                    FROM jsonb_array_elements(definition->'require') AS clause
                ),
                '[]'::jsonb
            )
        )
        WHERE definition IS NOT NULL
          AND definition ? 'require'
        """
    )
    op.execute(
        """
        UPDATE strategies
        SET definition = jsonb_set(
            definition,
            '{prefer}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        CASE
                            WHEN clause->>'field' = 'latency_p50_ms'
                                THEN jsonb_set(clause, '{field}', '"last_latency_ms"'::jsonb)
                            ELSE clause
                        END
                    )
                    FROM jsonb_array_elements(definition->'prefer') AS clause
                ),
                '[]'::jsonb
            )
        )
        WHERE definition IS NOT NULL
          AND definition ? 'prefer'
        """
    )


def downgrade() -> None:
    # Symmetric — flip the field name back. Only useful for rolling back the
    # commit; new strategies created after the upgrade will lose meaning if
    # downgraded, because the DSL parser at that point only knows
    # last_latency_ms. Documented as one-way for forward migration; the
    # downgrade exists so a panicked rollback doesn't blow up.
    op.execute(
        """
        UPDATE strategies
        SET definition = jsonb_set(
            definition,
            '{require}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        CASE
                            WHEN clause->>'field' = 'last_latency_ms'
                                THEN jsonb_set(clause, '{field}', '"latency_p50_ms"'::jsonb)
                            ELSE clause
                        END
                    )
                    FROM jsonb_array_elements(definition->'require') AS clause
                ),
                '[]'::jsonb
            )
        )
        WHERE definition IS NOT NULL
          AND definition ? 'require'
        """
    )
    op.execute(
        """
        UPDATE strategies
        SET definition = jsonb_set(
            definition,
            '{prefer}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        CASE
                            WHEN clause->>'field' = 'last_latency_ms'
                                THEN jsonb_set(clause, '{field}', '"latency_p50_ms"'::jsonb)
                            ELSE clause
                        END
                    )
                    FROM jsonb_array_elements(definition->'prefer') AS clause
                ),
                '[]'::jsonb
            )
        )
        WHERE definition IS NOT NULL
          AND definition ? 'prefer'
        """
    )
