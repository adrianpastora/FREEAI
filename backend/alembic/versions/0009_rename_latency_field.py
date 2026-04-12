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

The two SQL statements below are exposed as module-level constants so
test_migration_0009.py can execute them against a real Postgres without
spinning Alembic. Keep these as the single source of truth — both
upgrade()/downgrade() and the test reference these exact strings.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None


def _rename_field_sql(section: str, old: str, new: str) -> str:
    """Build the JSONB rewrite SQL for one section (require or prefer).

    Walks the array of clauses in `definition->{section}`, and for any
    clause whose `field` equals `old`, replaces it with `new`. Other
    clauses are passed through untouched. NULL definitions ('auto') are
    skipped via the WHERE.

    The COALESCE handles rows where the section is present but empty —
    `jsonb_agg` over zero rows returns NULL, which would otherwise null
    out the section.
    """
    return f"""
        UPDATE strategies
        SET definition = jsonb_set(
            definition,
            '{{{section}}}',
            COALESCE(
                (
                    SELECT jsonb_agg(
                        CASE
                            WHEN clause->>'field' = '{old}'
                                THEN jsonb_set(clause, '{{field}}', '"{new}"'::jsonb)
                            ELSE clause
                        END
                    )
                    FROM jsonb_array_elements(definition->'{section}') AS clause
                ),
                '[]'::jsonb
            )
        )
        WHERE definition IS NOT NULL
          AND definition ? '{section}'
    """


# Exposed as module constants so the migration test can execute the
# exact same SQL the migration runs — no risk of test/migration drift.
UPGRADE_REQUIRE_SQL = _rename_field_sql("require", "latency_p50_ms", "last_latency_ms")
UPGRADE_PREFER_SQL  = _rename_field_sql("prefer",  "latency_p50_ms", "last_latency_ms")
DOWNGRADE_REQUIRE_SQL = _rename_field_sql("require", "last_latency_ms", "latency_p50_ms")
DOWNGRADE_PREFER_SQL  = _rename_field_sql("prefer",  "last_latency_ms", "latency_p50_ms")


def upgrade() -> None:
    op.execute(UPGRADE_REQUIRE_SQL)
    op.execute(UPGRADE_PREFER_SQL)


def downgrade() -> None:
    # Symmetric — flip the field name back. Only useful for a rollback;
    # strategies created post-upgrade that use the new name will be
    # downgraded too, which means a future re-upgrade restores them
    # correctly. Documented as one-way for forward migration; the
    # downgrade exists so a panicked rollback doesn't blow up.
    op.execute(DOWNGRADE_REQUIRE_SQL)
    op.execute(DOWNGRADE_PREFER_SQL)
