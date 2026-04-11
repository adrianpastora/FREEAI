# Database schema

> Postgres 14+. The schema is managed by Alembic in
> [backend/alembic/versions/](../backend/alembic/versions/). `FREEAI_AUTO_MIGRATE=true`
> (the default) runs `alembic upgrade head` on startup.

## 1. Tables (7)

```
┌──────────────┐                    ┌──────────────────┐
│  app_config  │                    │    strategies    │
│  (singleton) │                    │                  │
└──────────────┘                    └──────────────────┘

┌──────────────┐ 1 :: 1 ┌──────────────────┐
│  providers   │────────│  provider_stats  │
└──────────────┘        └──────────────────┘
       │ 1
       │
       │ N
       ▼
┌──────────────┐
│ rate_events  │
└──────────────┘

┌──────────────┐                    ┌──────────────────┐
│   clients    │                    │   usage_events   │
└──────────────┘                    └──────────────────┘
(no FKs — client_hash + provider_name in usage_events are loose references)
```

### `app_config` — singleton

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | Always 1. One row. |
| `default_strategy` | `varchar(32)` | Name of the default strategy (matches `strategies.name`). |
| `enable_fallback` | bool | Global kill switch for the fallback chain. |
| `updated_at` | float (epoch) | |

This exists as a table instead of an env var because strategy and fallback
are flipped from the UI at runtime, and env vars need a restart.

### `providers` — provider config

| Column | Type | Notes |
|---|---|---|
| `name` | `varchar(64)` PK | Matches a key in `PROVIDER_REGISTRY`. Adding a row for a name not in the registry is harmless (it'll be filtered when ranking). |
| `enabled` | bool | If false, never considered for routing. |
| `api_key_encrypted` | text | Fernet-encrypted via `app.crypto`. Raw keys never touch disk. |
| `rpm_limit` | int nullable | Per-minute cap. NULL = no limit. |
| `rpd_limit` | int nullable | Per-day cap. NULL = no limit. |
| `weight` | float | Operator-tunable preference. Larger = picked first on ties. |
| `tags` | `text[]` | Capability tags (`fast`, `coding`, `vision`…). Strategies score providers by matching these. |
| `default_model` | `varchar(256)` nullable | Used when a request doesn't specify `model`. |
| `updated_at` | float | |

Seeded from
[config_repo.DEFAULT_PROVIDERS](../backend/app/repositories/config_repo.py) on
first run of `seed_defaults_if_empty()`, which is called from `lifespan`.

### `provider_stats` — health and quarantine

| Column | Type | Notes |
|---|---|---|
| `provider_name` | `varchar(64)` PK, FK providers(name) CASCADE | 1:1 with `providers` |
| `healthy` | bool | Cleared to false after 3 consecutive non-benign failures |
| `consecutive_failures` | int | Streak counter, reset on any success |
| `quarantined_until` | float | Epoch timestamp. 0 = not quarantined |
| `last_error` | text | Human-readable last error message |
| `last_error_kind` | `varchar(32)` | `ErrorKind.value` from the last failure |
| `last_latency_ms` | int | |
| `total_calls`, `total_failures` | int | Lifetime counters — used for dashboards |
| `updated_at` | float | |

This row is created lazily by `freeai_try_reserve` the first time a provider
is used. It's a separate table from `providers` because it's mutated on every
request — keeping it separate avoids row-level contention on the config row
and lets operators edit provider config without blocking the hot path.

### `rate_events` — reservation log

| Column | Type | Notes |
|---|---|---|
| `id` | int PK autoincrement | |
| `provider_name` | `varchar(64)`, FK providers(name) CASCADE, indexed | |
| `occurred_at` | float indexed | Epoch timestamp of the reservation |

Index `ix_rate_events_provider_time` on `(provider_name, occurred_at)` so the
rolling window count is a range scan.

**Important**: this table is **append-only for rate limiting**. The orchestrator
reserves a slot by inserting a row and later either keeps it (successful call)
or deletes it (rollback). The table has no retention policy — rows older than
a day contribute nothing to any count, but nothing removes them either. See
[REVIEW.md § 3](REVIEW.md#3-scheduled-jobs-missing).

### `clients` — inbound API keys

| Column | Type | Notes |
|---|---|---|
| `key_hash` | `varchar(64)` PK | SHA-256 of the raw key. Raw never stored. |
| `name` | `varchar(128)` | Human label, shown in the admin UI |
| `rpm_limit` | int | Per-client request cap |
| `enabled` | bool | Soft kill switch (revoke usually deletes) |
| `created_at` | float | |

### `usage_events` — telemetry, analytics source

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK autoincrement | `BigInteger` because this grows fastest |
| `occurred_at` | float indexed | |
| `provider_name` | `varchar(64)` indexed | No FK — retention is independent from the provider row |
| `model` | `varchar(256)` nullable | Set on success, NULL on failure |
| `strategy` | `varchar(32)` | What the orchestrator actually used |
| `outcome` | `varchar(32)` | `success` or an `ErrorKind.value` |
| `latency_ms` | int | Wall-clock for that one provider attempt |
| `prompt_tokens`, `completion_tokens` | int | 0 for streaming (providers don't report tokens in SSE) |
| `fallback_position` | int | 1 = first attempt, 2 = after one fallback, etc. |
| `client_hash` | `varchar(64)` nullable | Which client made the call (in bootstrap mode, NULL) |

Two indexes: `ix_usage_events_time` on `occurred_at`, and
`ix_usage_events_provider_time` on `(provider_name, occurred_at)`. They
service the aggregate queries in
[usage_repo.summary()](../backend/app/repositories/usage_repo.py).

### `strategies` — routing rules as data

| Column | Type | Notes |
|---|---|---|
| `name` | `varchar(32)` PK | Matches the `strategy` field in requests |
| `tags` | `text[]` | Ordered list — first tag is most-weighted in scoring |
| `description` | text | Shown in the UI |
| `is_builtin` | bool | Built-ins can be edited but not deleted |
| `updated_at` | float | |

Seeded from
[strategy_repo.BUILTIN_STRATEGIES](../backend/app/repositories/strategy_repo.py)
on first run of `seed_builtins_if_missing()`.

## 2. Migrations

Alembic is configured in [alembic.ini](../backend/alembic.ini), the env file
lives at [alembic/env.py](../backend/alembic/env.py). The DB URL comes from
`FREEAI_DATABASE_URL` at runtime — `env.py` reads it via `get_settings()` so
you never need to put it in `alembic.ini`.

### Current revisions

```
0001 — initial schema: app_config, providers, provider_stats, rate_events,
       clients, plus the freeai_try_reserve plpgsql function.
0002 — adds usage_events and strategies.
```

### Running migrations

Automatic (default):

```bash
FREEAI_AUTO_MIGRATE=true python run.py
```

Manual:

```bash
cd backend
alembic upgrade head       # apply all pending
alembic current            # show current revision
alembic history            # list revisions
alembic downgrade -1       # back up one
```

### Creating a new migration

```bash
cd backend
alembic revision -m "add indexes on foo"
```

Then edit the generated file in `alembic/versions/`. Autogeneration
(`--autogenerate`) *works* but review the output — it doesn't always pick up
Postgres-specific things (the `freeai_try_reserve` function, array defaults,
server defaults with `EXTRACT(EPOCH FROM NOW())`).

**Do not edit existing migrations** once they've been applied anywhere.
Create a new one that fixes the previous.

## 3. The atomic reservation function

`freeai_try_reserve` lives in migration 0001. It's the reason FreeAI is safe
under concurrency — the function does check-and-insert atomically inside one
Postgres statement under row locks.

```sql
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

    IF v_quarantined > v_now THEN
        RETURN NULL;
    END IF;
    IF NOT v_healthy AND v_quarantined = 0 THEN
        RETURN NULL;
    END IF;

    -- Sliding window counts
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
```

### Concurrency guarantee

Because everything happens inside a single `SELECT freeai_try_reserve(...)`
statement, it runs in one Postgres transaction. The `FOR UPDATE` on
`provider_stats` serializes two concurrent reservers for the same provider
— whichever runs the SELECT first holds the row, the second blocks, so when
it runs its window count it sees the first one's insert. The net effect is
that the minute/day counters never miscount under contention.

The test
[test_concurrent_reservations_respect_limit](../backend/tests/test_rate_repo.py)
verifies this: 50 concurrent sessions against a cap of 5 → exactly 5 succeed.

### Known issues with the function

These are covered in detail in [REVIEW.md](REVIEW.md) — listed here because
they directly affect this schema:

1. **`IF NOT v_healthy AND v_quarantined = 0 THEN RETURN NULL`** (line 113 of
   0001) permanently blocks any provider whose quarantine expired but whose
   `healthy` flag hasn't been reset yet. Should be removed — the quarantine
   column is the authoritative source.
2. **FK violation for per-client rate limiting.** `security._try_acquire_client_slot`
   calls `freeai_try_reserve('client:xxxx', rpm, NULL)` with a synthetic
   provider name that doesn't exist in the `providers` table. Both the initial
   `INSERT INTO provider_stats` (FK `providers.name`) and the
   `INSERT INTO rate_events` (same FK) violate the foreign key. **Per-client
   rate limiting doesn't actually work today** — it crashes on the first real
   client call. Bootstrap mode never triggers it, which is why nobody noticed.
3. **Non-deterministic `hash()` in the advisory lock key** for client rate
   limits ([security.py:89](../backend/app/security.py)). Python's `hash()`
   is salted per-process since 3.3, so the lock key differs across pods,
   defeating the point.

## 4. Access patterns

What the repositories actually do against the schema. Useful when reasoning
about indexes or adding queries.

### ConfigRepository ([config_repo.py](../backend/app/repositories/config_repo.py))

- `list_providers()` — `SELECT * FROM providers ORDER BY name`. Called on every
  `/v1/chat/completions` and `/api/providers`. O(N) where N = number of
  providers, typically 6–10.
- `get_provider(name)` — PK lookup.
- `upsert_provider(dto)` — `INSERT ... ON CONFLICT (name) DO UPDATE`.
- `seed_defaults_if_empty()` — one-time at startup.

### RateRepository ([rate_repo.py](../backend/app/repositories/rate_repo.py))

- `try_reserve(...)` — the plpgsql function above.
- `commit(...)` — `UPDATE provider_stats`. For benign errors updates metadata +
  quarantine; for streak failures, bumps counter and sets quarantine.
- `snapshot(name)` — two statements: a window count over `rate_events`, and a
  PK lookup in `provider_stats`. Used at ranking time so **called once per
  candidate per request**. With 6 providers → 6×2 = 12 queries per request.
  First optimization if ranking becomes a hot spot: batch into one `SELECT
  ... FROM rate_events GROUP BY provider_name` + one `SELECT * FROM
  provider_stats`.
- `reset_health(name)` — `UPDATE provider_stats SET ...`.
- `purge_old_events(seconds)` — `DELETE` older than cutoff. **Not called
  anywhere yet.**

### UsageRepository ([usage_repo.py](../backend/app/repositories/usage_repo.py))

- `record(event)` — `session.add(row)`, flushed on session commit. One INSERT
  per dispatch. With fallback, two or more INSERTS per request.
- `summary(window, buckets)` — four SELECTs over `usage_events`:
  - totals + percentiles (`PERCENTILE_CONT` for p50/p95)
  - by_provider (`GROUP BY provider_name`)
  - by_strategy (`GROUP BY strategy`)
  - by_outcome (`GROUP BY outcome`) + the time bucket series
    (`FLOOR((occurred_at - :since) / :width)`)
  All filtered by `occurred_at >= :since`, which the
  `ix_usage_events_time` index serves.

### ClientRepository ([client_repo.py](../backend/app/repositories/client_repo.py))

- `find_by_raw_key(raw)` — hashes, then PK lookup.
- `has_any()` — `SELECT key_hash LIMIT 1`. Called on every `/v1/*` request in
  `require_client` to decide bootstrap mode. One row cache would be a cheap
  optimization.

### StrategyRepository ([strategy_repo.py](../backend/app/repositories/strategy_repo.py))

- `list_all()`, `get(name)` — `SELECT * FROM strategies`, PK lookup.
- `upsert`, `delete` — straightforward.
- **Note**: `_resolve_strategy` in the orchestrator calls `.get(name)` on every
  chat completion. That's one extra query per request. An in-process TTL cache
  would remove it without changing semantics. See [REVIEW.md § 2](REVIEW.md#2-hot-path-inefficiencies).

## 5. Backups

At minimum, back up `providers`, `clients`, `strategies`, and `app_config`.
Those are your configuration. The event tables (`rate_events`, `usage_events`,
`provider_stats`) are regenerable — losing them means losing analytics
history and a brief window where all providers look "cold".

```bash
pg_dump -U freeai -d freeai \
  --table=providers --table=clients --table=strategies --table=app_config \
  --data-only --format=custom \
  -f freeai-config-$(date +%F).dump
```

See [OPERATIONS.md § 4](OPERATIONS.md#4-backup-and-restore) for the full
backup playbook.

## 6. Connecting for ad-hoc queries

```bash
# docker compose
docker compose exec postgres psql -U freeai -d freeai

# local
psql "postgresql://freeai:freeai@localhost:5433/freeai"
```

Useful queries:

```sql
-- What's the success rate per provider in the last hour?
SELECT provider_name,
       COUNT(*) AS calls,
       COUNT(*) FILTER (WHERE outcome = 'success') AS success,
       ROUND(100.0 * COUNT(*) FILTER (WHERE outcome = 'success') / COUNT(*), 1) AS pct
FROM usage_events
WHERE occurred_at >= EXTRACT(EPOCH FROM NOW()) - 3600
GROUP BY provider_name
ORDER BY calls DESC;

-- Who's in quarantine right now?
SELECT provider_name, healthy, quarantined_until, last_error_kind, last_error
FROM provider_stats
WHERE NOT healthy OR quarantined_until > EXTRACT(EPOCH FROM NOW());

-- Current rpm/rpd for every provider
SELECT provider_name,
       COUNT(*) FILTER (WHERE occurred_at >= EXTRACT(EPOCH FROM NOW()) - 60)    AS rpm,
       COUNT(*) FILTER (WHERE occurred_at >= EXTRACT(EPOCH FROM NOW()) - 86400) AS rpd
FROM rate_events
GROUP BY provider_name;

-- Fallback rate (how often the first choice fails)
SELECT
  ROUND(100.0 * COUNT(*) FILTER (WHERE fallback_position > 1) / NULLIF(COUNT(*), 0), 2) AS fallback_pct
FROM usage_events
WHERE occurred_at >= EXTRACT(EPOCH FROM NOW()) - 86400;
```
