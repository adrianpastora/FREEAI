# Database schema

> Postgres 14+. The schema is managed by Alembic in
> [backend/alembic/versions/](../backend/alembic/versions/). `FREEAI_AUTO_MIGRATE=true`
> (the default) runs `alembic upgrade head` on startup.

## 1. Tables (13)

```
┌──────────────┐         ┌──────────────────┐         ┌──────────────┐
│  app_config  │         │    strategies    │         │ model_prices │
│  (singleton) │         │                  │         │              │
└──────────────┘         └──────────────────┘         └──────────────┘

┌─────────┐ 1 :: N ┌─────────────────┐ 1 :: N ┌──────────────────┐
│  users  │────────│ user_providers  │────────│  provider_stats  │
│         │        │ (per-user keys) │        │  (per-user)      │
└─────────┘        └─────────────────┘        └──────────────────┘
    │ 1                   │ N (provider_name FK)
    │                     ▼
    │ N           ┌──────────────┐ 1 :: N ┌──────────────┐
    ▼             │  providers   │────────│ rate_events  │
┌────────────────┐│  (catalog)   │        └──────────────┘
│ refresh_tokens │└──────────────┘
└────────────────┘

┌──────────┐ 1 :: N ┌────────────────────┐
│ clients  │────────│ client_rate_events │
└──────────┘        └────────────────────┘

┌──────────────────┐         ┌──────────────────────┐
│   usage_events   │ rolled  │  usage_daily_rollup  │
│ (90d retention)  │ ──────▶ │  (730d retention)    │
└──────────────────┘         └──────────────────────┘
(no FKs on usage_events — client_hash + provider_name are loose references
so retention is independent from the catalog rows.)
```

### `app_config` — singleton

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | Always 1. One row. |
| `default_strategy` | `varchar(32)` | Name of the default strategy (matches `strategies.name`). |
| `enable_fallback` | bool | Global kill switch for the fallback chain. |
| `admin_token_hash` | text nullable | SHA-256 of the admin token (migration 0005). |
| `provider_max_retries` | int | Default retry budget per provider for transient errors. Overridable per user+provider via `user_providers.max_retries`. Default `1`. (migration 0018) |
| `stream_idle_timeout_s` | float | Seconds without a chunk before the orchestrator treats a streaming upstream as stalled. Default `45.0`. (migration 0018) |
| `circuit_breaker_threshold` | int | Consecutive non-benign failures that trip the breaker. Default `3`. (migration 0019) |
| `circuit_breaker_window_s` | int | Sliding window for the streak; older failures are forgotten. Default `300`. (migration 0019) |
| `circuit_breaker_base_cooldown_s` | int | First cooldown after a trip. Default `30`. (migration 0019) |
| `circuit_breaker_max_cooldown_s` | int | Upper bound. Effective cooldown is `min(base * 2^level, max)`. Default `3600`. (migration 0019) |
| `updated_at` | float (epoch) | |

This exists as a table instead of an env var because strategy, fallback
and the robustness tunables are flipped from the UI (or a future admin
API) at runtime, and env vars need a restart.

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

Per-user-provider state (composite PK `(user_id, provider_name)` since
migration 0014 added multi-user isolation).

| Column | Type | Notes |
|---|---|---|
| `user_id` | int PK (part 1) | Added by migration 0014 to isolate health/quarantine per user. |
| `provider_name` | `varchar(64)` PK (part 2), FK providers(name) CASCADE | |
| `healthy` | bool | Cleared to false when the circuit breaker trips. |
| `consecutive_failures` | int | Streak counter; resets on success. Also reset to 0 when the breaker actually trips, so the next failure starts a fresh window. |
| `recent_failures_started_at` | float | Epoch timestamp of the first failure in the current streak. If `now - this > circuit_breaker_window_s`, the streak resets before counting a new failure. (migration 0019) |
| `cooldown_level` | smallint | How many times the breaker has tripped without a successful call in between. Cooldown is `min(base * 2^level, max)`. Reset to 0 on any success. (migration 0019) |
| `quarantined_until` | float | Epoch timestamp. 0 = not quarantined. `try_reserve` rejects reservations while `quarantined_until > now`. |
| `last_error` | text | Human-readable last error message |
| `last_error_kind` | `varchar(32)` | `ErrorKind.value` from the last failure |
| `last_latency_ms` | int | Single most recent observed call |
| `latency_ema_ms` | float | Exponential moving average (alpha=0.3). Computed atomically in SQL on each success. Used by `baseline_score()` for stable latency scoring. (migration 0011) |
| `total_calls`, `total_failures` | int | Lifetime counters — used for dashboards and reliability penalty |
| `tokens_today` | bigint | Incremental token counter. Updated atomically in SQL on each success. Replaces the `SUM()` query that ran on `usage_events` per ranking call. (migration 0011) |
| `tokens_day_start` | float | Epoch timestamp of the current counter day. When `now - day_start >= 86400`, the counter resets. (migration 0011) |
| `updated_at` | float | |

This row is created lazily by `freeai_try_reserve` the first time a provider
is used. It's a separate table from `providers` because it's mutated on every
request — keeping it separate avoids row-level contention on the config row
and lets operators edit provider config without blocking the hot path.

**Benign error kinds** (`rate_limited`, `client_error`, `auth`, `content_filtered`)
update `last_error*` but **do not** tick `consecutive_failures` — they're not
provider health failures, just request-specific refusals. See
[rate_repo.py:`_BENIGN_ERRORS`](../backend/app/repositories/rate_repo.py).

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
[REVIEW.md § 2](REVIEW.md#2-known-limitations).

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
| `cost_usd` | float nullable | Frozen cost at write time so future price edits don't rewrite history. `NULL` = no price on file when billed; `0` = explicit free-tier / `:free` route. (migration 0020) |

Two indexes: `ix_usage_events_time` on `occurred_at`, and
`ix_usage_events_provider_time` on `(provider_name, occurred_at)`. They
service the aggregate queries in
[usage_repo.summary()](../backend/app/repositories/usage_repo.py).

### `users` — registered accounts (migration 0012)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK autoincrement | |
| `username` | `varchar(64)` unique | Login name |
| `password_hash` | text | bcrypt |
| `role` | `varchar(16)` | `admin` or `user` |
| `max_clients` | int | Cap on how many API clients this user can own. Default 5. |
| `created_at`, `updated_at` | float | |

Seeded empty; the first admin is created via the setup wizard or the
migrate-token CLI.

### `refresh_tokens` — JWT refresh (migration 0012)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK autoincrement | |
| `user_id` | int FK users(id) CASCADE | |
| `token_hash` | `varchar(64)` unique | SHA-256 of the raw refresh token |
| `expires_at` | float | Epoch timestamp |
| `created_at` | float | |

Raw tokens are handed to the client once at login and never re-derivable.

### `user_providers` — per-user credentials (migration 0013)

Per-user overrides for provider config. The global `providers` table became
a catalog without keys; real credentials live here.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK autoincrement | |
| `user_id` | int FK users(id) CASCADE, indexed | |
| `provider_name` | `varchar(64)` FK providers(name) CASCADE | |
| `api_key_encrypted` | text nullable | Fernet-encrypted |
| `enabled` | bool | Per-user kill switch |
| `rpm_limit`, `rpd_limit`, `tpd_limit` | int nullable | Per-user overrides. NULL = use the catalog default. |
| `weight` | float nullable | |
| `default_model` | `varchar(256)` nullable | |
| `max_retries` | int nullable | Per-user override of `app_config.provider_max_retries`. NULL = use the global. (migration 0018) |
| `created_at`, `updated_at` | float | |
| `UNIQUE(user_id, provider_name)` | | |

### `client_rate_events` — inbound rate limit log (migration 0004)

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK autoincrement | |
| `client_hash` | `varchar(64)` indexed | |
| `occurred_at` | float | |

No FK on `client_hash` — revoked clients may still have lingering events
until the purge job runs. Consumed by the
`freeai_try_reserve_client` plpgsql function.

### `usage_daily_rollup` — analytics long-retention (migration 0017)

Pre-aggregated daily summaries of `usage_events` keyed by
`(user_id, day, provider_name, model, strategy)`. Keeps 730 days so the
analytics dashboard can run year-over-year queries without scanning the
raw events table (which rotates out at 90 days).

| Column | Type | Notes |
|---|---|---|
| `user_id`, `day`, `provider_name`, `model`, `strategy` | composite PK | |
| `total_calls`, `success_calls`, `failed_calls` | bigint | |
| `sum_latency_ms` | bigint | For computing average. |
| `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms` | int | |
| `avg_ttfb_ms` | int | |
| `prompt_tokens`, `completion_tokens` | bigint | |
| `sum_cost_usd` | float | Sum of `usage_events.cost_usd` for the day. Lets the long-window analytics view (≥30d) carry cost alongside tokens without scanning raw events. (migration 0020) |
| `errors_by_kind` | JSONB | Map of `ErrorKind → count` for that day. |
| `fallback_position_hist` | JSONB | Histogram of how often each fallback slot was hit. |
| `updated_at` | float | |

Populated hourly by the `rollup_daily` background task (see `main.py`
lifespan).

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

### `model_prices` — per-model unit cost (migration 0020)

| Column | Type | Notes |
|---|---|---|
| `provider_name` | `varchar(64)` PK (part 1) | Matches `providers.name`. No FK — admins can seed prices for models on providers that aren't enabled yet. |
| `model` | `varchar(256)` PK (part 2) | The model identifier as it appears in `usage_events.model`. |
| `input_price_per_million_usd` | float | Price per million prompt tokens. Per-million is the industry-standard unit and avoids `Numeric` precision games. |
| `output_price_per_million_usd` | float | Same for completion tokens. |
| `currency` | `varchar(8)` | Always `"USD"` today; column exists so a future multi-currency mode doesn't need a migration. |
| `updated_at` | float | |

Seeded with the public price lists for `KNOWN_MODELS` as of 2026-05. Rows
are `ON CONFLICT DO NOTHING`, so admin-tuned prices survive a re-run of
the migration. Free-tier SKUs and OpenRouter `:free` routes are inserted
as `0.0/0.0`. Maintained via `/api/pricing/*` endpoints.

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
0003 — fix quarantine heal logic.
0004 — client_rate_events table + freeai_try_reserve_client plpgsql.
0005 — admin_token_hash column on app_config.
0006 — ttfb_ms column on usage_events + bigint IDs.
0007 — fix reserve race condition in plpgsql.
0008 — strategy DSL: definition JSONB column, drops tags.
0009 — rename latency_p50_ms to last_latency_ms.
0010 — tpd_limit column on providers.
0011 — scoring optimizations: latency_ema_ms, tokens_today, tokens_day_start
       on provider_stats.
0012 — users + refresh_tokens tables; JWT auth.
0013 — user_providers table; per-user credentials. Existing provider keys
       migrate to user_id=1 (placeholder admin).
0014 — multi-user scoping: composite PK (user_id, provider_name) on
       provider_stats + rate_events.user_id.
0015 — backfill user_id on legacy usage_events.
0016 — data-only fix for orphaned provider keys from 0013.
0017 — usage_daily_rollup table for long-retention analytics.
0018 — app_config.provider_max_retries (default 1),
       app_config.stream_idle_timeout_s (default 45.0),
       user_providers.max_retries (nullable per-user override).
0019 — app_config.circuit_breaker_threshold / window_s /
       base_cooldown_s / max_cooldown_s, plus
       provider_stats.recent_failures_started_at and
       provider_stats.cooldown_level for exponential backoff.
0020 — model_prices table for cost tracking; usage_events.cost_usd
       (frozen at write time); usage_daily_rollup.sum_cost_usd.
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
- An in-process TTL cache fronts `.get(name)` so `_resolve_strategy` doesn't
  hit the DB on every chat completion.

## 5. Backups

At minimum, back up the configuration tables: `users`, `refresh_tokens`,
`user_providers`, `providers`, `clients`, `strategies`, `app_config`, and
`model_prices`. Losing them costs you accounts, encrypted provider keys
and pricing — none of which is regenerable. The event tables
(`rate_events`, `usage_events`, `provider_stats`, `client_rate_events`,
`usage_daily_rollup`) are regenerable; losing them costs analytics history
and a brief window where providers look "cold".

```bash
pg_dump -U freeai -d freeai \
  --table=users --table=refresh_tokens --table=user_providers \
  --table=providers --table=clients --table=strategies \
  --table=app_config --table=model_prices \
  --data-only --format=custom \
  -f freeai-config-$(date +%F).dump
```

> Provider keys in `user_providers.api_key_encrypted` are Fernet-encrypted
> with the master key at `backend/data/.master_key`. **Back up that file
> too** — without it the dump above is unusable on restore.

See [OPERATIONS.md § 4](OPERATIONS.md#4-backup-and-restore) for the full
backup playbook.

## 6. Connecting for ad-hoc queries

```bash
# docker compose (recomendado: Postgres no expone puerto en el host por defecto)
docker compose exec postgres psql -U freeai -d freeai

# Solo si mapeas un puerto en docker-compose.override.yml, p. ej. 15432:
# psql "postgresql://freeai:freeai@localhost:15432/freeai"
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
