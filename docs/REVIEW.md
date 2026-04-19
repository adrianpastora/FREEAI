# Review — current state, known limitations, and backlog

This document is the place we keep honest notes about what's good, what
isn't, and what we'd tackle next. It is meant for anyone thinking of
contributing or running FreeAI in production, not a changelog — the
actual history lives in `git log`.

## 1. Current state

FreeAI is production-ready for a self-hosted, single-team deployment
behind a reverse proxy. The full matrix of what's in the box:

- **Routing & fallback** — multi-provider dispatch, EMA-based latency
  scoring, reliability penalty, concurrency-aware load spreading,
  per-user sliding-window circuit breaker, robust handling of empty
  200s, content-filtered finish reasons, and stream stalls.
- **Reliability** — atomic reservation inside a Postgres plpgsql
  function (multi-pod safe), per-client rate limiting, self-healing
  quarantine, per-user aislamiento of provider keys.
- **Performance** — bounded-cardinality Prometheus metrics, batched
  ranking queries (3 per request instead of 2×N), strategy TTL cache,
  in-memory rate counters, streaming with a single commit at end of
  request, per-`(user, provider)` in-flight tracking.
- **Security** — one-time bootstrap token for the setup wizard, JWT
  secret kept independent from the encryption master key, login rate
  limiting, body/image size caps, SSRF guard on `image_url`, sanitized
  provider error bodies, strict security headers, CORS locked down by
  default, tests for the security-sensitive paths.
- **Observability** — structured JSON logs with request IDs, Prometheus
  `/metrics`, Grafana dashboards under the `observability` compose
  profile.
- **Tests** — 233 pytest tests across unit, integration, E2E,
  streaming, and security categories. A handful run without Docker;
  the rest use testcontainers against a real Postgres.

## 2. Known limitations

These are conscious tradeoffs, not bugs. If any of them bite, file an
issue with a concrete use case and we'll weigh the cost of changing them.

**Postgres is required.** SQLite was considered but the explicit
locking story and multi-host requirements pointed to Postgres. There is
no single-binary / embedded-DB mode.

**Event tables are not partitioned.** `usage_events` grows linearly
with traffic; the periodic purge trims it to 90 days but a very busy
instance will still see the table reach tens of GB over time. Daily
rollups are already in place to keep the analytics dashboard fast.
See backlog item below.

**No Helm chart.** Docker Compose is the supported deployment path.
Running on Kubernetes is possible — mount the data volume, run
`alembic upgrade head` as a pre-deploy hook, point multiple replicas
at the same Postgres — but we don't ship manifests.

**Auto-strategy is heuristic.** Language-aware and fast, but it will
occasionally pick `fastest` when a human would pick `reasoning`.
Swapping it for a small classifier model is on the backlog.

**No cost tracking.** Token counts are captured in `usage_events` but
there's no built-in mapping from `(provider, model) → price`. You can
compute it yourself from the `usage_events` table.

**Observability profile is optional.** Prometheus/Grafana are off by
default (`profiles: ["observability"]`). The app exposes `/metrics`
regardless, so you can point an external Prometheus at it.

**No semantic cache.** Every request hits a provider. A prompt-hash
→ response cache would bypass duplicates but hasn't been built yet.

**Frontend is plain HTML + vanilla JS.** No build step, no components,
no framework. This is deliberate — keeps the panel tiny and
maintainable — but if you want to extend the UI significantly you'll
feel it. Component structure is documented in
[DEVELOPMENT.md § 4](DEVELOPMENT.md#4-frontend-structure).

## 3. Design decisions worth preserving

Things that look arbitrary but are load-bearing:

- **Repository pattern + DTO boundary.** Sessions are bounded to one
  HTTP request, but the orchestrator and tests want to pass provider
  config around without worrying about session state. DTOs detach us
  from that. The cost is a `_row_to_dto` conversion — cheap.
- **The `ErrorKind` enum and its benign/non-benign split.** This is
  what makes health tracking trustworthy: rate-limits fall back without
  tripping the breaker, safety filters fall back as benign, parsing
  errors and server errors trip it. Don't lose this distinction.
- **`freeai_try_reserve` stays in plpgsql.** "Atomic check-and-insert
  in one statement" is the right approach for multi-pod safety. Do not
  move reservation logic into Python — you'll re-introduce the race.
- **No module-level singletons.** Everything flows through `app.state`
  and `Depends(...)` — makes tests trivial. Preserve that.
- **Testcontainers for integration tests.** Slow to start, but
  reproducible and isolated. Do not add a shared dev-database.
- **Master key separate from JWT secret.** A leak of one must not
  compromise the other. They live in different files (`.master_key`
  and `.jwt_secret`) with different rotation paths.

## 4. Backlog (nice to have, not blocking)

These are items we'd pick up next. PRs welcome; please open an issue
first to coordinate on approach.

1. **Table partitioning for `usage_events` and `rate_events`.** Monthly
   partitions with automatic drop after the retention window. Cleanest
   using `pg_partman`.
2. **Helm chart / K8s manifests.** StatefulSet for Postgres (or point
   at a managed Postgres), Deployment for the app, HPA optional,
   PodDisruptionBudget, ServiceMonitor for Prometheus Operator.
3. **Router-LLM for `auto` strategy.** Replace heuristics with a
   Llama-8B classifier call. Only worth it if heuristic accuracy
   becomes a measured problem.
4. **Cost tracking.** `(provider, model) → price_per_1k_tokens` table
   seeded from published pricing; monthly report endpoint.
5. **Semantic cache.** Embedding-based prompt cache in Redis with a
   similarity threshold. Bypass the provider for near-duplicate
   prompts. Careful with privacy — requires opt-in per client.
6. **End-to-end CI with tests.** The repo has no GitHub Actions test
   job — intentionally, since every push ran the full suite locally.
   A fork that wants to move faster should add one.
7. **Audit log.** Every admin action into a persistent table, viewable
   from the UI. Right now admin actions only land in structured logs.

## 5. Where to look when something is off

- **Rate counters seem wrong.** Start with [OPERATIONS.md
  § 5](OPERATIONS.md#5-troubleshooting) — most reports trace to
  cancelled requests not rolling back their reservation, which is now
  handled, but edge cases surface.
- **A provider keeps getting quarantined.** `provider_stats` table has
  the recent failure counts; the `ErrorKind` classification decides
  whether a failure was benign. Check `usage_events` for the kind.
- **Something looks slow.** Check Prometheus's
  `freeai_http_request_duration_seconds` histograms; then zoom in with
  the request-id in the JSON logs.
