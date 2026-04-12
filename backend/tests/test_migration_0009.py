"""Migration 0009 — rename DSL field latency_p50_ms -> last_latency_ms.

These tests do NOT call alembic upgrade/downgrade. The session fixture
already runs alembic to head once, and tampering with revision state
inside a test would corrupt every other DB-backed test in the session.

Instead, we import the SQL constants from the migration module and
execute them directly against the live test Postgres. The migration
file's upgrade()/downgrade() are thin wrappers that call op.execute on
those exact same constants, so this test covers the SQL the migration
runs in production — no test/migration drift possible.

What we cover:
  - Custom strategies with latency_p50_ms in `require` are rewritten.
  - Custom strategies with latency_p50_ms in `prefer` are rewritten.
  - Other clauses (different field, different op) are passed through
    untouched — operator, value, and weight survive the rewrite.
  - The 'auto' row (definition IS NULL) is left alone.
  - Rows that already use the new name are no-ops on upgrade.
  - downgrade() reverses the rewrite — symmetric round trip.
  - A row with one section missing (e.g. only `prefer`, no `require`)
    is handled without an error.

What we do NOT cover here:
  - End-to-end alembic upgrade chain. That runs every test session via
    conftest.engine and is implicitly validated by the rest of the suite.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text


def _load_migration():
    """Import the 0009 migration as a plain module — alembic doesn't
    expose its versions on sys.path, so we load by file path."""
    path = Path(__file__).parent.parent / "alembic" / "versions" / "0009_rename_latency_field.py"
    spec = importlib.util.spec_from_file_location("migration_0009", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_migration()


async def _insert_strategy(session, name: str, definition: dict | None) -> None:
    """Insert a strategy row directly via raw SQL — bypasses the repo's
    DSL parser so we can store the legacy field name that the parser
    would now reject."""
    import json
    await session.execute(
        text(
            "INSERT INTO strategies (name, definition, description, is_builtin) "
            "VALUES (:n, CAST(:d AS jsonb), '', false)"
        ),
        {"n": name, "d": json.dumps(definition) if definition is not None else None},
    )


async def _read_definition(session, name: str) -> dict | None:
    row = await session.execute(
        text("SELECT definition FROM strategies WHERE name = :n"),
        {"n": name},
    )
    return row.scalar_one()


async def _run_upgrade(session) -> None:
    await session.execute(text(M.UPGRADE_REQUIRE_SQL))
    await session.execute(text(M.UPGRADE_PREFER_SQL))


async def _run_downgrade(session) -> None:
    await session.execute(text(M.DOWNGRADE_REQUIRE_SQL))
    await session.execute(text(M.DOWNGRADE_PREFER_SQL))


# ─────────────────────────────────────────────────────────────────────
#                              upgrade
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upgrade_rewrites_latency_p50_ms_in_require(session):
    await _insert_strategy(session, "test_req", {
        "require": [{"field": "latency_p50_ms", "op": "<", "value": 1500}],
        "prefer": [],
    })
    await session.commit()

    await _run_upgrade(session)
    await session.commit()

    defn = await _read_definition(session, "test_req")
    assert defn["require"][0]["field"] == "last_latency_ms"
    # Op and value preserved exactly.
    assert defn["require"][0]["op"] == "<"
    assert defn["require"][0]["value"] == 1500


@pytest.mark.asyncio
async def test_upgrade_rewrites_latency_p50_ms_in_prefer(session):
    await _insert_strategy(session, "test_pref", {
        "require": [],
        "prefer": [{"field": "latency_p50_ms", "op": "<", "value": 800, "weight": 4}],
    })
    await session.commit()

    await _run_upgrade(session)
    await session.commit()

    defn = await _read_definition(session, "test_pref")
    clause = defn["prefer"][0]
    assert clause["field"] == "last_latency_ms"
    assert clause["op"] == "<"
    assert clause["value"] == 800
    # Weight survives — this is the part most likely to be silently lost
    # by a careless jsonb_set, so it gets its own assertion.
    assert clause["weight"] == 4


@pytest.mark.asyncio
async def test_upgrade_leaves_other_fields_untouched(session):
    await _insert_strategy(session, "test_mixed", {
        "require": [{"field": "tags", "op": "contains", "value": "coding"}],
        "prefer": [
            {"field": "latency_p50_ms", "op": "<", "value": 2000, "weight": 3},
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
            {"field": "rpd_remaining", "op": ">", "value": 0.5, "weight": 2},
        ],
    })
    await session.commit()

    await _run_upgrade(session)
    await session.commit()

    defn = await _read_definition(session, "test_mixed")
    # require unchanged (no latency_p50_ms in it)
    assert defn["require"][0]["field"] == "tags"
    # prefer: only the latency clause was renamed; the other two are intact
    fields = [c["field"] for c in defn["prefer"]]
    assert fields == ["last_latency_ms", "tags", "rpd_remaining"]
    # Weights preserved across the rewrite
    weights = [c["weight"] for c in defn["prefer"]]
    assert weights == [3, 5, 2]


@pytest.mark.asyncio
async def test_upgrade_leaves_null_definition_untouched(session):
    """The 'auto' row carries definition=NULL and must not be touched."""
    await _insert_strategy(session, "test_null", None)
    await session.commit()

    await _run_upgrade(session)
    await session.commit()

    defn = await _read_definition(session, "test_null")
    assert defn is None


@pytest.mark.asyncio
async def test_upgrade_is_noop_on_already_renamed_rows(session):
    """A row that already uses last_latency_ms must come out unchanged."""
    original = {
        "require": [],
        "prefer": [{"field": "last_latency_ms", "op": "<", "value": 1000, "weight": 3}],
    }
    await _insert_strategy(session, "test_noop", original)
    await session.commit()

    await _run_upgrade(session)
    await session.commit()

    defn = await _read_definition(session, "test_noop")
    assert defn == original


@pytest.mark.asyncio
async def test_upgrade_handles_missing_section(session):
    """A row that has 'prefer' but no 'require' must not crash. The DSL
    serializer always emits both sections, but a hand-crafted row in the
    DB might not — and the migration must be defensive."""
    await _insert_strategy(session, "test_partial", {
        "prefer": [{"field": "latency_p50_ms", "op": "<", "value": 500, "weight": 2}],
    })
    await session.commit()

    await _run_upgrade(session)
    await session.commit()

    defn = await _read_definition(session, "test_partial")
    assert defn["prefer"][0]["field"] == "last_latency_ms"
    # The require section was never present and must not have been added.
    assert "require" not in defn


# ─────────────────────────────────────────────────────────────────────
#                            downgrade
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_downgrade_reverses_upgrade(session):
    """Round trip: legacy -> upgrade -> downgrade should produce the
    original document. This is the test that catches asymmetric SQL
    bugs in the rewrite — anything that survives upgrade but not
    downgrade (or vice versa) shows up here."""
    original = {
        "require": [{"field": "latency_p50_ms", "op": "<", "value": 1500}],
        "prefer": [
            {"field": "latency_p50_ms", "op": "<", "value": 800, "weight": 4},
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
        ],
    }
    await _insert_strategy(session, "test_round", original)
    await session.commit()

    await _run_upgrade(session)
    await session.commit()

    after_up = await _read_definition(session, "test_round")
    assert after_up["require"][0]["field"] == "last_latency_ms"

    await _run_downgrade(session)
    await session.commit()

    after_down = await _read_definition(session, "test_round")
    assert after_down == original
