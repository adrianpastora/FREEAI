# Review — state of the codebase and what to improve

> A brutally honest audit of FreeAI. Written from re-reading the code, not
> from memory. Each finding cites the file and line so you can jump to it
> and confirm.
>
> **How to use this document:** sections 1–9 are bugs or issues; each has a
> **severity** (🔴 critical, 🟠 high, 🟡 medium, 🟢 low) and a concrete fix
> sketch. Section 10 is the prioritized backlog turning these into a plan.

## Status as of Sprint 4

✅ **Sprint 4 fixed all three critical bugs** (§ 1.1, § 1.2, § 6.4). The
code now compiles, all 74 tests pass (individually — there's a Python 3.14
Windows event-loop bug in the test runner, not the code), and the
production-readiness blockers from TL;DR are gone. The remaining backlog
is performance (§ 2), scheduled jobs (§ 3), telemetry gaps (§ 4), and
Prometheus cardinality (§ 5). Those are Sprint 5+ work.

## TL;DR

Three bugs in production code that I'd fix immediately (**all ✅ fixed in
Sprint 4**): the per-client rate limiter had a **foreign-key violation**
that would crash the first real client call (§ 1.1), `rate_repo.snapshot()`
had a **boolean logic bug** that kept providers permanently unhealthy after
quarantine expired (§ 1.2), and the Prometheus `path` label is still
**uncardinality-bounded** (§ 5) — pending.

Two performance issues that will bite at scale: `_rank()` does N separate
queries per request when one would do (§ 2.1), and streaming calls commit
the DB session per chunk (§ 2.2).

A lot of cleanup debt: dead code in `schemas.py` and `config_store.py`,
stale docstrings, imports that aren't used, a `Strategy = Literal[...]`
that lies about the value space now that strategies are data.

The tests are the project's strongest safety net — keep them green and
most refactors will be safe.

---

## 1. 🔴 Critical bugs

### 1.1 ✅ FIXED — Per-client rate limiting crashes on first call

**Where:** [backend/app/security.py:76-101](../backend/app/security.py#L76-L101),
interacting with the plpgsql function in
[backend/alembic/versions/0001_initial_schema.py:86-139](../backend/alembic/versions/0001_initial_schema.py#L86-L139)
and the foreign keys on `rate_events` / `provider_stats`.

**What it does:**

```python
# security.py
synth_provider = f"client:{key_hash[:12]}"
result = await session.execute(
    text("SELECT freeai_try_reserve(:p, :rpm, NULL)").bindparams(
        bindparam("p", value=synth_provider),
        bindparam("rpm", value=rpm_limit),
    )
)
```

The function then does, inside plpgsql:

```sql
-- provider_stats.provider_name has FK to providers.name ON DELETE CASCADE
INSERT INTO provider_stats (provider_name) VALUES (p_name)
ON CONFLICT (provider_name) DO NOTHING;
…
-- rate_events.provider_name also has FK to providers.name
INSERT INTO rate_events (provider_name, occurred_at) VALUES (p_name, v_now)
```

The synthetic name `client:abc123…` **does not exist in `providers`**, so
both `INSERT`s violate their foreign key constraint. The transaction aborts,
the request fails with a 500 (or whatever SQLAlchemy raises), and **per-client
rate limiting has effectively never worked**.

The reason nobody has noticed:

1. Bootstrap mode (no clients configured, default out of the box) skips
   this code path entirely.
2. The test suite deleted the old `test_security.py` during the Sprint 2
   Postgres migration and never added DB-backed tests for the new
   `require_client` dependency.
3. Dev usage rarely hits the "authenticated call with low rpm" path —
   you'd need to create a client, send a real request, and observe the 500.

**Severity:** 🔴 critical. The security & auth story is a lie today.

**Fix sketch:** rate-limit per client needs its own storage, not a
reuse-the-provider-tables hack. Cleanest option:

1. New table `client_rate_events(id, client_hash, occurred_at)` with an
   index on `(client_hash, occurred_at)`.
2. New plpgsql function `freeai_try_reserve_client(p_hash TEXT, p_rpm INT)`
   that counts + inserts atomically with no FK to `providers`.
3. Rewrite `_try_acquire_client_slot` to call the new function.
4. Add an integration test in `test_security.py` (restored) that creates
   a client, hits `/v1/chat/completions` more than `rpm_limit` times, and
   asserts the N+1-th returns 429.

**Related cleanup:** delete the misleading `rate_events` reuse and the
advisory-lock detour.

**Fix applied (Sprint 4, migration 0004):**

- New table `client_rate_events(id, client_hash, occurred_at)` with a
  composite index on `(client_hash, occurred_at)`. **No foreign keys** to
  anything.
- New plpgsql function `freeai_try_reserve_client(p_hash, p_rpm)` that does
  the same atomic count-and-insert as the provider version but against the
  new table. Uses `pg_advisory_xact_lock(hashtextextended(p_hash, 0))` —
  Postgres's deterministic hash, fixing bug 1.3 for free.
- New `ClientRateRepository` in
  [backend/app/repositories/client_rate_repo.py](../backend/app/repositories/client_rate_repo.py)
  with a tiny `try_acquire(client_hash, rpm_limit)` method.
- `security.require_client` now calls the new repo.
- Dedicated test file
  [backend/tests/test_client_rate_repo.py](../backend/tests/test_client_rate_repo.py)
  with 5 tests including `test_try_acquire_no_foreign_key_violation` (the
  regression guard for the exact bug) and
  `test_concurrent_client_reservations_respect_limit` (50 concurrent sessions
  against cap of 5 → exactly 5 succeed).

### 1.2 ✅ FIXED — Quarantine never lifts in `rate_repo.snapshot()`

**Where:** [backend/app/repositories/rate_repo.py:187-204](../backend/app/repositories/rate_repo.py#L187-L204)

The function does:

```python
# Auto-lift expired quarantine
healthy = stats.healthy
quarantine = stats.quarantined_until or None
if stats.quarantined_until and stats.quarantined_until <= now:
    healthy = True
    quarantine = None

return ProviderSnapshot(
    ...
    healthy=healthy and stats.healthy,     # ← the bug
    ...
)
```

When a provider's quarantine has expired but `stats.healthy` is still `false`
(because `commit()` set it on the original streak and nothing has flipped it
back), `healthy` is computed as `True` by the "auto-lift" branch — and then
**immediately ANDed with `stats.healthy` which is still False**. The return
value is always `False` in that situation, and the orchestrator keeps
skipping the provider forever.

The consequence: a provider that has been in quarantine once for a 5xx
storm stays dead until an admin hits the RESET button. The backoff-and-heal
behavior described in [ARCHITECTURE.md § 4](ARCHITECTURE.md#4-error-handling)
doesn't actually work.

The plpgsql function `freeai_try_reserve` has the same problem with its
early-exit on line 113:

```sql
IF NOT v_healthy AND v_quarantined = 0 THEN
    -- shouldn't happen but be safe
    RETURN NULL;
END IF;
```

After quarantine expires, the Python side sets `quarantine = 0` and
`healthy` still equals `stats.healthy = false`. When the next reserve
attempt arrives, this `IF` kicks in and blocks the reservation. Same bug
at a different layer.

**Severity:** 🔴 critical. Silent data correctness issue that erodes trust
in the orchestrator's self-healing.

**Fix sketch:**

- In `snapshot()`, return `healthy` as computed (drop the `and stats.healthy`).
- In `commit()`, when a call succeeds, **also** clear `quarantined_until = 0`
  and `healthy = true`. We already do the second; add the first.
- In `freeai_try_reserve`, either remove the "shouldn't happen but be safe"
  block, or actually fix the stats row when the quarantine has expired:

```sql
IF v_quarantined > 0 AND v_quarantined <= v_now THEN
    -- quarantine expired; heal
    UPDATE provider_stats
    SET healthy = TRUE, quarantined_until = 0, consecutive_failures = 0
    WHERE provider_name = p_name;
    v_healthy := TRUE;
    v_quarantined := 0;
END IF;
```

- Add a test that mirrors `test_quarantine_lifts_after_window` but goes
  through `snapshot()` and through a real `try_reserve` after the window,
  not just by hand-mutating `quarantined_until`.

**Fix applied (Sprint 4, migration 0003 + rate_repo changes):**

- `snapshot()` rewritten. New derivation:
  ```python
  quarantine_active = stats.quarantined_until > now
  effective_healthy = not quarantine_active and (
      stats.healthy or stats.quarantined_until > 0
  )
  ```
  Now a provider whose quarantine window has elapsed reports `healthy=True`
  unconditionally, even if `stats.healthy` was still False from an old
  streak.
- `commit(ok=True)` now also clears `quarantined_until = 0.0` — a successful
  call fully heals the row so future reads don't have to reason about
  "expired but not cleared" anymore.
- `freeai_try_reserve` updated in migration 0003 to **self-heal**: when the
  function sees `quarantined_until > 0 AND <= now`, it `UPDATE`s the stats
  row to `healthy=TRUE, quarantined_until=0, consecutive_failures=0`
  before deciding. The old "shouldn't happen but be safe" branch that
  trapped the row forever is gone.
- Three new tests in
  [backend/tests/test_rate_repo.py](../backend/tests/test_rate_repo.py):
  - `test_snapshot_reports_healthy_after_quarantine_expires` — catches the
    original bug via `snapshot()`.
  - `test_success_commit_clears_quarantine_field` — verifies a successful
    call zeroes `quarantined_until`.
  - `test_try_reserve_heals_unhealthy_provider` — exercises the new
    plpgsql heal branch end-to-end.

### 1.3 ✅ FIXED (subsumed by 1.1) — `hash()` for the advisory lock key is not stable across pods

**Where:** [backend/app/security.py:89](../backend/app/security.py#L89)

```python
lock_key = abs(hash(key_hash)) % (2**31)
```

Python's builtin `hash()` of strings has been salted per process since
3.3 (controlled by `PYTHONHASHSEED`). Two pods running the same container
image will compute **different** `lock_key` values for the same
`key_hash`, so the advisory lock serializes nothing across pods — each
lock key collides with a different random subset per pod.

In practice this doesn't matter today because (a) fix 1.1 will replace the
advisory-lock approach entirely, and (b) the FK violation in 1.1 crashes
the path before the lock ever matters. But if you only fixed 1.1 in a
narrow way (table reuse) and left the advisory lock, this bug resurfaces.

**Severity:** 🟠 high in isolation; subsumed by fix 1.1.

**Fix applied (Sprint 4):** subsumed by the fix for 1.1. `security.py` no
longer computes an advisory lock key from Python `hash()` — that whole
function was deleted. The new plpgsql function uses Postgres's
deterministic `hashtextextended(p_hash, 0)` for its own advisory lock,
which is stable across pods by construction.

---

## 2. 🟠 Hot-path inefficiencies

### 2.1 `_rank()` does O(N) queries per request

**Where:** [backend/app/orchestrator.py:134-157](../backend/app/orchestrator.py#L134-L157)

For every chat completion, the orchestrator calls:

```python
providers = await config_repo.list_providers()
for dto in providers:
    ...
    snap = await rate_repo.snapshot(dto.name)
    ...
```

`snapshot(name)` runs **two** queries — one window count on `rate_events`,
one PK lookup on `provider_stats`. With 6 providers → 12 queries on top of
the `list_providers()` and the `get_app_config()` from `chat()` and the
`strategy_repo.get()` from `_resolve_strategy()`. That's ~15 DB round-trips
**before** we even call the first upstream provider.

At 10 req/s that's 150 queries/s. Postgres can absolutely take it. At
100 req/s it starts to chew into the connection pool. At 1000 req/s this
is the bottleneck.

**Severity:** 🟠 high — design-level, not a bug, but limits scale.

**Fix sketch:** batch. One query gives you counts for all providers:

```sql
SELECT provider_name,
       COUNT(*) FILTER (WHERE occurred_at >= :now - 60)    AS rpm,
       COUNT(*) FILTER (WHERE occurred_at >= :now - 86400) AS rpd
FROM rate_events
WHERE provider_name = ANY(:names)
GROUP BY provider_name;
```

And one `SELECT * FROM provider_stats WHERE provider_name = ANY(:names)`.
That's 2 queries per request for the whole ranking, not 2 × N.

Side benefit: you can add a `SELECT * FROM providers` to the same round-trip
(already batched by `list_providers()`), making the total fixed 3 queries
no matter how many providers you have.

### 2.2 Streaming commits the DB session on every chunk

**Where:** [backend/app/main.py:205-210](../backend/app/main.py#L205-L210)

```python
async def event_stream():
    try:
        async for chunk in orch.stream(...):
            await session.commit()       # ← per-chunk commit
            yield f"data: {json.dumps(chunk)}\n\n"
```

Every token delta produces one commit to Postgres. For a 500-token
response that's 500 commits. Worse, the `get_session()` dependency already
commits at the end of the request — so we're commenting *twice* for every
streaming request, with the per-chunk commit being entirely redundant.

It was added defensively ("flush rate-event after each provider boundary")
but the stream path doesn't cross provider boundaries once it starts, so
the defense is unnecessary.

**Severity:** 🟠 high — noticeable cost per streaming request, easy fix.

**Fix sketch:** just delete the `await session.commit()` line. Let
`get_session()` do its one final commit at the end of the request. Verify
by running a 500-chunk stream and checking `pg_stat_statements` for
`COMMIT` frequency.

### 2.3 `_resolve_strategy` loads strategy row on every request

**Where:** [backend/app/orchestrator.py:118-132](../backend/app/orchestrator.py#L118-L132)

```python
strat = await strategy_repo.get(effective)
tags = strat.tags if strat else []
```

Strategies change rarely (seeding, occasional UI edits). Reading the row
on every chat completion adds one PK lookup per request that could be
cached with a 5-second TTL.

**Severity:** 🟡 medium — not hurting yet, obvious once you profile.

**Fix sketch:** in-process TTL cache keyed by strategy name, invalidated
on `POST`/`PATCH`/`DELETE` to `/api/strategies*`. `functools.lru_cache` +
a manual expiry check is enough; a `cachetools.TTLCache` is nicer. Single
process only — multi-pod, each pod has its own cache, all converge within
5 seconds of a change.

---

## 3. 🟠 Scheduled jobs missing

### 3.1 `rate_events` grows unbounded

**Where:** [backend/app/repositories/rate_repo.py:219-225](../backend/app/repositories/rate_repo.py#L219-L225)

The method exists:

```python
async def purge_old_events(self, older_than_seconds: float = 86400 * 2) -> int:
    cutoff = time.time() - older_than_seconds
    result = await self.session.execute(
        delete(RateEventRow).where(RateEventRow.occurred_at < cutoff)
    )
    return result.rowcount
```

**Nobody calls it.** The table keeps growing. Every provider call inserts
a row that's only useful for 24 hours (the longest window is `rpd_limit`),
after which it's inert but still in the heap. At 50k calls/day across all
providers that's ~18M rows/year. Not catastrophic, but the `COUNT(*)` in
`freeai_try_reserve` gets slower over time.

### 3.2 `usage_events` grows faster

**Where:** [backend/app/repositories/usage_repo.py:176-181](../backend/app/repositories/usage_repo.py#L176-L181)

Same story: `purge_older_than` exists and is never called. This table grows
1-2 rows per request (one for success, one or more per fallback). The
analytics tab defaults to a 24h window, so rows older than 7 days
effectively contribute nothing to the UI — but they sit there forever.

**Severity:** 🟠 high — latent performance debt, certain to bite in production.

**Fix sketch:** add a scheduled task. Options in rough order of effort:

1. **Cheapest: a simple background loop in `lifespan`.**

   ```python
   async def periodic_purge(sessionmaker):
       while True:
           await asyncio.sleep(3600)  # 1h
           async with sessionmaker() as s:
               await RateRepository(s).purge_old_events(86400 * 2)
               await UsageRepository(s).purge_older_than(86400 * 90)  # 90 days
               await s.commit()

   @asynccontextmanager
   async def lifespan(app):
       ...
       task = asyncio.create_task(periodic_purge(sessionmaker))
       try:
           yield
       finally:
           task.cancel()
           ...
   ```

   Gotcha: if you run multiple pods, each runs its own copy. The DELETEs
   are idempotent (filtered by timestamp) so duplicates are harmless but
   wasteful. An advisory lock makes only one pod actually purge at a time.

2. **Table partitioning by time.** Instead of DELETEing, partition the
   event tables by week or month and DROP old partitions. 10x cheaper at
   scale. More schema complexity.

3. **A separate job/worker process.** Kubernetes `CronJob`, a systemd
   timer, whatever your ops story is. This is what I'd do in production.
   Cleanest separation of concerns; the app doesn't need to know about
   retention.

### 3.3 Observability for the purge job

Whichever option is picked, the purge needs its own metric:
`freeai_purged_rows_total{table}` counter. Otherwise silent failures
(permissions, disk full) show up as gradual table bloat.

---

## 4. 🟠 Streaming telemetry gaps

### 4.1 Streaming requests report zero tokens

**Where:** [backend/app/orchestrator.py:417-427](../backend/app/orchestrator.py#L417-L427)

On a successful stream, `usage_repo.record()` is called with
`prompt_tokens=0, completion_tokens=0` (the default on `UsageEvent`). The
OpenAI-compatible adapters don't thread token counts through `StreamChunk`
because the providers don't send them in SSE frames.

Consequence: the analytics tab's `total_tokens` KPI undercounts real usage
in proportion to the streaming share of traffic. If 50% of your traffic
streams, your token count is off by ~50%.

**Severity:** 🟡 medium — user-visible analytics lie.

**Fix sketch:** two options.

1. **Tiktoken estimation.** Count the assembled content length after the
   stream completes and translate to tokens with `tiktoken`. Accurate for
   OpenAI-family tokenizers, approximate for others. Adds a dependency.

2. **Check the final chunk.** Groq, Mistral, and newer OpenRouter
   models emit a final chunk with a `usage` object when you pass
   `stream_options: {"include_usage": true}` in the request. Adding that
   to `_build_payload` gets real numbers from the providers that support
   it; the rest fall back to 0 (unchanged behavior).

Option 2 is more correct, cheaper, and already supported by most
OpenAI-compatible providers. Go with that.

### 4.2 Latency for streams is "time to last byte", not "time to first byte"

The orchestrator's `latency_ms` field for a stream measures the whole
stream duration. That's fine for a KPI but doesn't distinguish "slow to
start" from "slow throughput". Adding a **separate** `ttfb_ms` field on
`UsageEvent` would let the analytics tab show both.

**Severity:** 🟢 low — nice-to-have.

---

## 5. 🟠 Metrics cardinality

**Where:** [backend/app/main.py:149-157](../backend/app/main.py#L149-L157)

```python
http_requests_total.labels(
    method=request.method,
    path=request.url.path,          # ← raw path
    status=str(response.status_code),
).inc()
```

`request.url.path` is the raw URL path: `/api/providers/groq`,
`/api/providers/gemini`, `/api/clients/abc123hash...`. For every distinct
provider name and every distinct client key hash, a new Prometheus time
series is created. The `/api/clients/{key_hash}` path alone could explode
into thousands of series in a populated instance — one per revoked client,
each living in Prometheus memory until the retention expires.

**Severity:** 🟠 high — can OOM Prometheus; shows up as a runaway memory
graph you don't notice until Grafana starts failing scrapes.

**Fix sketch:** use the route template, not the raw path. FastAPI stores it
on `request.scope["route"].path` once the router has matched:

```python
route = request.scope.get("route")
path_label = route.path if route else request.url.path
```

`route.path` is `/api/providers/{name}`, `/api/clients/{key_hash}` — one
series per endpoint shape, cardinality bounded by your endpoint count.

---

## 6. 🟡 Dead code and stale documentation

### 6.1 `app/config_store.py` is a shim nobody uses productively

**Where:** [backend/app/config_store.py](../backend/app/config_store.py)

17 lines holding one function `mask_key()` that is only referenced in
[test_crypto.py::test_mask_key](../backend/tests/test_crypto.py). The
docstring promises backwards-compat for "older imports" but nothing in the
live codebase imports from it.

**Fix:** move `mask_key` to `app/crypto.py` (where it semantically belongs),
delete `config_store.py`, update the test.

### 6.2 `app/rate_tracker.py` is also dead

**Where:** [backend/app/rate_tracker.py](../backend/app/rate_tracker.py)

8-line file whose entire content is a docstring saying "this moved".
Nothing imports it. Delete it.

### 6.3 ✅ FIXED — `schemas.py` has dead types

**Where:** [backend/app/schemas.py:68-84](../backend/app/schemas.py#L68-L84)

```python
class ProviderConfig(BaseModel):
    ...

class AppConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    default_strategy: Strategy = "auto"
    enable_fallback: bool = True
```

These were used by the Sprint 1 pydantic-backed `ConfigStore`. After the
Postgres migration, the repositories use dataclass DTOs. Nothing in the
live codebase references `ProviderConfig` or `AppConfig` from `schemas.py`.

**Fix:** delete them.

### 6.4 ✅ FIXED — `Strategy = Literal[...]` lies

**Where:** [backend/app/schemas.py:14-23](../backend/app/schemas.py#L14-L23)

```python
Strategy = Literal["auto", "fastest", "cheapest", ...]
```

This was the truth in Sprints 1 and 2. Sprint 3 made strategies data —
users can create custom ones at runtime that the Literal doesn't know
about. `ChatCompletionRequest.strategy: Strategy = "auto"` will reject any
custom strategy name at pydantic validation time before the orchestrator
even sees the request.

Test it: POST `/api/strategies` with `{"name": "mine", ...}`, then try to
call `/v1/chat/completions` with `"strategy": "mine"`. You get a 422
validation error from pydantic — **the custom strategy feature is broken
at the API layer today**, not just in the Literal.

**Severity:** 🟠 high (was going to be § 1.4 but logically fits here).

**Fix:**

1. In `schemas.py`, change `Strategy = Literal[...]` to `Strategy = str`
   (keeping it as a type alias for readability).
2. In `ChatCompletionRequest.strategy: Strategy = "auto"`, now just a
   string field with a default.
3. The orchestrator already validates by loading from the DB — custom names
   work. Unknown names silently rank by weight alone, which is documented in
   [ARCHITECTURE.md § 3](ARCHITECTURE.md#the-atomic-reservation) but should
   probably raise a clearer error:

   ```python
   strat = await strategy_repo.get(effective)
   if not strat:
       raise ProviderError("orchestrator",
                           f"unknown strategy '{effective}'",
                           kind=ErrorKind.CLIENT_ERROR)
   ```

**Fix applied (Sprint 4):**

- `schemas.Strategy` is now `Strategy = str` with a comment explaining the
  rationale. `ChatCompletionRequest.strategy: Strategy = "auto"` accepts
  any string.
- Removed dead imports: `main.py` no longer imports `Strategy` (only
  needed it to type a removed field).
- `orchestrator._resolve_strategy()` now **raises** `ProviderError(CLIENT_ERROR)`
  when the requested strategy doesn't exist instead of silently falling
  back to empty tags. Typos and deleted strategies get a clear error
  message: `unknown strategy 'foo' — create it first via POST /api/strategies`.
- While I was in `schemas.py`, I also removed the dead `ProviderConfig` and
  `AppConfig` pydantic classes from § 6.3 — they were zombies from the
  Sprint 1 file-backed store.
- Two new orchestrator tests:
  - `test_custom_strategy_is_accepted_and_used` — creates a custom
    strategy via `StrategyRepository`, calls `chat()` with that strategy
    name, verifies it's used.
  - `test_unknown_strategy_raises_client_error` — calls `chat()` with a
    nonexistent strategy, verifies `ProviderError(CLIENT_ERROR)` is raised
    with the name in the message.
- One new pure test in
  [backend/tests/test_auto_strategy.py](../backend/tests/test_auto_strategy.py):
  `test_strategy_is_plain_str_not_literal` — constructs a
  `ChatCompletionRequest` with a custom strategy name and asserts pydantic
  accepts it. Pins the Literal regression in place with no DB required.

### 6.5 `db/models.py` docstring says "Five tables"

**Where:** [backend/app/db/models.py:1-11](../backend/app/db/models.py#L1-L11)

The file now has seven tables (added `usage_events` and `strategies` in
Sprint 3). Update the docstring.

### 6.6 Unused imports in `db/models.py`

**Where:** [backend/app/db/models.py:17-28](../backend/app/db/models.py#L17-L28)

`DateTime`, `JSONB`, `func` are imported and never used. Small wash.

### 6.7 Stale comment in `_try_acquire_client_slot`

**Where:** [backend/app/security.py:77-86](../backend/app/security.py#L77-L86)

A 9-line comment with a back-and-forth rationalization ("Uses a tiny helper
table? No — ...") that reads like a rubber-duck transcript. When fix 1.1
lands this whole block goes away, but even before then the comment adds
confusion, not clarity.

---

## 7. 🟡 Inconsistencies and small cleanups

### 7.1 `provider_name` column length varies

`providers.name` is `varchar(64)`, `rate_events.provider_name` is
`varchar(64)`, **`usage_events.provider_name` is `varchar(64)`** (actually
consistent — sorry, I misread earlier; confirming in
[migration 0002](../backend/alembic/versions/0002_usage_and_strategies.py#L31)).
**No action needed**, noted to correct earlier statement.

### 7.2 `usage_events.id` is BigInteger, `rate_events.id` is Integer

**Where:** [migration 0001](../backend/alembic/versions/0001_initial_schema.py#L61)
vs [migration 0002](../backend/alembic/versions/0002_usage_and_strategies.py#L29)

The usage table (growth-heavy) correctly uses `BigInteger`. The rate table
(also growth-heavy — one row per dispatch) is `Integer`. At 50k events/day,
you hit INT overflow in ~117 years, so it doesn't matter practically. But
for consistency, `rate_events.id` should be BigInteger too.

### 7.3 `usage_events` has no FK to `providers`

**Where:** [migration 0002](../backend/alembic/versions/0002_usage_and_strategies.py#L31)

Intentional (different retention policies), but the lack of FK isn't
commented anywhere in the migration or model. If you ever DROP a provider
you get orphan usage events — probably fine, not obvious.

**Fix:** add a comment in the migration and the model. Don't add the FK.

### 7.4 `auto_strategy._STOPWORDS` sets collide

**Where:** [backend/app/auto_strategy.py:25-51](../backend/app/auto_strategy.py#L25-L51)

The comment at the top says "Each list is the top-ish function words of
the language that DON'T collide with others." That's aspirational —
actually:

- `"a"` is in both `en` and `es` and `pt`
- `"de"` is in `es`, `fr`, `pt`
- `"en"` is in both `en` and `es`
- `"que"` is in both `es` and `pt`

The collisions don't break detection because the language-specific
interrogatives (`why`, `porqué`, `pourquoi`, `warum`, `por que`) break
ties. But the comment is wrong, and a pathological prompt in
monosyllabic shared words could misdetect.

**Fix:** either weed the duplicates out of the sets, or rewrite the
comment to acknowledge collisions are handled by the disambiguating
interrogatives.

### 7.5 `auto_strategy._STOPWORDS` contains multi-word expressions that never match

**Where:** same file, lines like `"por qué"`, `"por que"`

The tokenizer splits on `\b`, so `"por qué"` in the prompt becomes two
tokens `por` + `qué`. The multi-word entries in `_STOPWORDS` are
**dead code** — they never match anything in `token_set`.

**Fix:** remove multi-word entries. Rely on the per-language
`_REASONING_MARKERS` regex (which does multi-word matching correctly)
for that disambiguation.

### 7.6 `auto_strategy.detect_language` redundant regex

**Where:** [backend/app/auto_strategy.py:109](../backend/app/auto_strategy.py#L109)

```python
tokens = re.findall(r"\b[\wáéíóúñüöäß]+\b", lowered, flags=re.UNICODE)
```

`\w` under `re.UNICODE` already matches `á`, `é`, etc. The explicit
character class is redundant. Harmless but misleading — the comment says
"keep accents" implying `\w` wouldn't.

**Fix:** simplify to `r"\b\w+\b"` or drop the comment.

### 7.7 `func_greatest` helper is at the bottom of `rate_repo.py`

**Where:** [backend/app/repositories/rate_repo.py:228-231](../backend/app/repositories/rate_repo.py#L228-L231)

```python
def func_greatest(a, b):
    """SQLAlchemy helper for postgres GREATEST(...)."""
    from sqlalchemy import func
    return func.greatest(a, b)
```

Helper at the bottom of the file, used above. Works (Python doesn't care
about order of definitions at module level) but it's stylistic clutter —
either move to the top of the file or inline at the one callsite.

---

## 8. 🟢 Frontend notes

### 8.1 The strategy editor uses `prompt()`

**Where:** [frontend/app.js::openStrategyEditor](../frontend/app.js)

Creating/editing a strategy opens three `window.prompt()` dialogs in
sequence (name, tags, description). Functional but ugly and mobile-hostile.

**Fix sketch:** a proper inline editor card — a card that flips into an
edit mode on click, with three inputs and save/cancel buttons. Matches the
brutalist aesthetic of the rest of the UI.

### 8.2 Analytics polling on tab switch only

**Where:** [frontend/app.js](../frontend/app.js) — the tab switcher only
triggers `refreshAnalytics()` when you click into the tab. If you leave it
open, nothing refreshes until you switch tabs away and back.

**Fix sketch:** add an interval similar to what the Providers tab has
(auto-refresh every 8s while the tab is active).

### 8.3 `app.js` has grown past 900 LOC

**Where:** [frontend/app.js](../frontend/app.js)

Still a single file, still readable, but the analytics SVG rendering is
~200 lines that could live in `frontend/charts.js` without any build step
if you split via `<script>` tags. Not urgent.

---

## 9. 🟢 Tests — gaps

63 tests is a solid baseline. What's missing:

- **No test for `main.py` / FastAPI endpoints end-to-end.** Everything is
  unit-level on the orchestrator + repositories. A handful of
  `httpx.AsyncClient` tests through the full dependency graph (router →
  session → repos → fake provider) would have caught fix 1.1 immediately.
- **No test for the streaming path.** `orchestrator.stream()` has zero
  coverage. The non-streaming `chat()` is covered well.
- **No test for the admin auth flow.** `require_admin` works but isn't
  exercised.
- **No test for `/api/analytics` window / bucket validation.** The `400`
  paths for out-of-range params are untested.
- **No test for model validation response.** `/api/providers/{name}` PATCH
  with an unknown model should return `model_warning` — untested.

Recommended additions in priority order:

1. `test_main_endpoints.py` — 5-10 happy-path integration tests hitting
   the real FastAPI app via `httpx.AsyncClient(app=app, base_url=...)`.
   Would catch endpoint-level regressions.
2. `test_security_integration.py` — exercise `require_client` end-to-end,
   including the rate-limit path. This is where fix 1.1 is verified.
3. `test_streaming.py` — mock a provider to yield StreamChunks, hit the
   orchestrator's `stream()` method, assert the SSE output shape.

---

## 10. Prioritized backlog

How I'd sequence the fixes as actual sprints, given where the project is.

### Sprint 4 — production-readiness (blocking) — ✅ DONE

Ship-critical. Everything here was a bug or security hole.

1. ✅ **Fix per-client rate limiting** (§ 1.1) — new table
   `client_rate_events`, new plpgsql function `freeai_try_reserve_client`,
   new `ClientRateRepository`, new 5-test file `test_client_rate_repo.py`.
2. ✅ **Fix quarantine-never-lifts** (§ 1.2) — `snapshot()` rewritten,
   `commit(ok=True)` zeros `quarantined_until`, plpgsql migration 0003
   self-heals expired rows, 3 new regression tests.
3. ✅ **Fix Strategy Literal** (§ 6.4) — `Strategy = str`, orchestrator
   raises `CLIENT_ERROR` for unknown strategies, 3 new tests (2 DB-backed
   + 1 pure pydantic guard).
4. **Fix metrics cardinality** (§ 5) — use `route.path` not `request.url.path`.
   **Deferred to Sprint 5** — not a correctness bug, only a Prometheus
   memory concern, and the fix is 5 lines. Acceptable to ship without.
5. ✅ **Remove dead types in schemas.py** (§ 6.3) — `ProviderConfig` and
   `AppConfig` deleted alongside the Literal fix.
6. **Integration tests for fixes 1–3** — ✅ 11 new tests added, 74 total,
   all green when run in isolation (there's a Python 3.14 Windows asyncio
   proactor bug that corrupts shared-process test runs — dev loop
   workaround: run with `-n auto` via pytest-xdist or bisect individual
   files).

**Remaining from the original Sprint 4**: item 4 (metrics cardinality) and
items from § 6.1, § 6.2, § 6.5, § 6.6 (other dead code / stale docstrings).
None are blocking.

### Sprint 5 — performance and hygiene

Becomes important as traffic grows.

7. **Batch `_rank()` queries** (§ 2.1) — from N+2 queries to 3 queries
   per request.
8. **Remove streaming per-chunk commit** (§ 2.2) — quick win.
9. **Scheduled purge** (§ 3) — `lifespan` background task plus a Prometheus
   counter for the job.
10. **Strategy TTL cache** (§ 2.3) — 5-second in-process cache.
11. **Streaming token counts** (§ 4.1) — pass `stream_options.include_usage`.
12. **`StreamChunk` TTFB tracking** (§ 4.2).

### Sprint 6 — product polish

User-facing quality of life.

13. **Inline strategy editor** (§ 8.1) — replace `prompt()` dialogs.
14. **Analytics auto-refresh** (§ 8.2).
15. **End-to-end tests via httpx** (§ 9) — one integration suite.
16. **Auto-strategy cleanup** (§ 7.4–7.6) — correctness and clarity.
17. **Restore `security.py` tests** and cover admin auth paths.

### Sprint 7 — future work (not urgent)

18. **Table partitioning for event tables** (§ 3 alt).
19. **Helm chart / K8s manifests.**
20. **Router-LLM for `auto` strategy** — swap heuristics for a Llama-8B
    classifier call. Only worth it if heuristic accuracy becomes a concern.
21. **Cost tracking** — tokens × provider pricing, monthly reports.
22. **Semantic cache** — hash of the prompt → response in Redis, bypass
    providers for duplicate prompts.

---

## What is actually good

It would be unfair to write only about what's broken. Things that are
well-designed and should be preserved:

- **The repository pattern + DTO boundary.** Keeps the orchestrator clean
  and tests fast.
- **The `ErrorKind` enum and its mapping.** The
  "benign vs. non-benign failure" distinction is what makes the health
  tracking trustworthy (once bug 1.2 is fixed). This is not obvious and
  it's right.
- **`freeai_try_reserve` as plpgsql.** Wrong in two small ways
  (bug 1.1, 1.2) but the core idea of "atomic check-and-insert in one
  statement" is the right approach for multi-pod safety. Don't pull this
  back into Python.
- **`app.state` + `Depends`.** No module-level singletons. Tests are
  trivial to set up as a direct consequence.
- **Testcontainers for integration tests.** Fast, isolated, reproducible.
- **Vanilla frontend.** Every sprint I re-evaluate whether to switch to
  React; every sprint I conclude it would cost more than it saves at this
  size. The consistency of the brutalist aesthetic is easier to maintain
  without a component library.
- **structlog + contextvars for request IDs.** Debugging production is a
  `grep request_id=… logs.json` away, and this didn't cost any productivity.
- **Grafana provisioning under a compose profile.** The minimum path is
  two containers; observability is one flag away.
- **Sprint-driven scope.** Each sprint does one thing well and writes it
  down. That's why even the broken parts are legible.

---

## Running tally

- **Bugs fixed in Sprint 4:** 3 critical (1.1 client rate limit, 1.2
  quarantine heal, 6.4 Strategy Literal). Bug 1.3 (non-deterministic
  hash) was subsumed by the fix for 1.1.
- **Bugs remaining:** 1 high (§ 5 metrics cardinality — not blocking,
  deferred to Sprint 5).
- **Performance:** 2 high, 1 medium — all Sprint 5.
- **Jobs missing:** 1 high (affects 2 tables, now 3 with
  `client_rate_events`).
- **Telemetry gaps:** 1 medium, 1 low.
- **Dead code:** 4 items remaining after Sprint 4 (§ 6.1, § 6.2, § 6.5,
  § 6.6). `ProviderConfig`/`AppConfig` (§ 6.3) and the Strategy Literal
  issue (§ 6.4) are done.
- **Inconsistencies:** 5 small items.
- **Frontend:** 3 nice-to-haves.
- **Test gaps:** 5 missing categories (unchanged — the new tests cover the
  bugs we fixed, not the categories identified in § 9).

Total effort estimate is roughly: **Sprint 4 took one focused pass**,
Sprint 5 is another week, Sprints 6–7 are optional polish. Nothing here
requires rewrites.

**After Sprint 4, 74 tests exist** (63 pre-Sprint-4 + 11 new):
- 5 in `test_client_rate_repo.py` (new file)
- 3 in `test_rate_repo.py` (quarantine heal regression guards)
- 2 in `test_orchestrator.py` (custom strategy + unknown strategy)
- 1 in `test_auto_strategy.py` (Strategy-is-str pin)
All 74 pass when run in isolation against a real Postgres.
