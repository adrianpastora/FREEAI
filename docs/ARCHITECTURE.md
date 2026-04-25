# Architecture

> If you change the layering, the orchestrator flow, or the DB schema,
> please update this document in the same PR.

## 1. Goals and non-goals

**Goals**
- Expose one OpenAI-compatible endpoint that routes every request to the best
  free-tier AI provider currently available.
- Be safe to run behind multiple workers / multiple pods, so state must not
  live in process memory.
- Protect quotas: never blow past a provider's rate limit because of a race.
- Fail gracefully: classify errors and fall back when it makes sense; give up
  when it doesn't.
- Keep the frontend and admin surface usable without a build step.

**Non-goals**
- We are not an LLM gateway with billing / team management / per-user budgets.
  A single installation is a single logical tenant (client keys limit *who* can
  call, not *how much* they pay).
- We do not replace the providers' own dashboards — their analytics are more
  accurate than ours.
- We don't optimize prompts or transform them. Content in, content out.

## 2. Layered view

```
 ┌─────────────────────────────────────────────────────────────┐
 │                        HTTP surface                          │
 │   fastapi app  •  middleware (req id, metrics)  •  CORS      │
 │   routes in main.py  —  dep-inject sessions & orchestrator   │
 └─────────────────────────────────────────────────────────────┘
                 │ Depends(get_session)  Depends(require_*)
                 ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                     Application services                    │
 │   Orchestrator         │ AutoStrategy detector               │
 │   • rank candidates    │ • detects language (EN/ES/FR/DE/PT) │
 │   • reserve slot       │ • picks coding/reasoning/vision/... │
 │   • retry transient    │                                      │
 │   • write usage event  │ Security                            │
 │   • emit metrics       │ • require_admin / require_client    │
 │                        │ • per-client rpm (pg advisory lock) │
 └─────────────────────────────────────────────────────────────┘
                 │ repositories receive an AsyncSession
                 ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                      Repositories                           │
 │  ConfigRepository  │ RateRepository   │ UsageRepository      │
 │  StrategyRepository│ ClientRepository │                      │
 │  (all return DTOs, never ORM rows)                           │
 └─────────────────────────────────────────────────────────────┘
                 │ SQLAlchemy 2.0 async + asyncpg
                 ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                         Postgres                            │
 │   7 tables • plpgsql fn `freeai_try_reserve`  • Alembic      │
 └─────────────────────────────────────────────────────────────┘

                 ▲ httpx.AsyncClient (shared, pooled)
                 │
 ┌─────────────────────────────────────────────────────────────┐
 │                   Provider adapters                         │
 │  OpenAICompatibleProvider (groq, mistral, openrouter, hf)   │
 │  GeminiProvider (Google v1beta)                              │
 │  CohereProvider (v2/chat)                                    │
 │  — all return ProviderResponse / yield StreamChunk           │
 │  — raise typed ProviderError with ErrorKind enum             │
 └─────────────────────────────────────────────────────────────┘
```

### Module map

| Directory | Role |
|---|---|
| [backend/app/main.py](../backend/app/main.py) | FastAPI app, lifespan, middleware, routes. Entry point. |
| [backend/app/settings.py](../backend/app/settings.py) | `pydantic-settings` — single source of config truth. |
| [backend/app/orchestrator.py](../backend/app/orchestrator.py) | Request-level logic: rank, reserve, dispatch, retry, fallback, emit. |
| [backend/app/auto_strategy.py](../backend/app/auto_strategy.py) | Heuristic language + intent detector for `strategy: "auto"`. |
| [backend/app/security.py](../backend/app/security.py) | Admin token + per-client auth dependencies. |
| [backend/app/crypto.py](../backend/app/crypto.py) | Fernet encryption for API keys stored at rest. |
| [backend/app/logging_config.py](../backend/app/logging_config.py) | structlog setup + contextvar wiring. |
| [backend/app/metrics.py](../backend/app/metrics.py) | Prometheus counters + histograms. |
| [backend/app/schemas.py](../backend/app/schemas.py) | Pydantic request/response models for the HTTP surface. |
| [backend/app/db/](../backend/app/db/) | Engine, ORM models, `Depends(get_session)`. |
| [backend/app/repositories/](../backend/app/repositories/) | Typed data-access layer. Returns DTOs. |
| [backend/app/providers/](../backend/app/providers/) | One adapter per upstream provider; shared base + OpenAI-compatible mixin. |
| [backend/alembic/versions/](../backend/alembic/versions/) | Schema migrations + the plpgsql reservation function. |
| [frontend/](../frontend/) | Vanilla HTML/CSS/JS control room — no build step. |
| [deploy/](../deploy/) | Prometheus + Grafana provisioning for the `observability` compose profile. |

## 3. Data flow: one completion

The happy path for `POST /v1/chat/completions` (non-streaming):

```
  client POST                                             HTTP 200
        │                                                     ▲
        ▼                                                     │
  ┌──────────┐   req + session + orch + client                │
  │  main.py │ ─────────────────────────────────────────────┐ │
  │  route   │                                              │ │
  └──────────┘                                              │ │
        │ Orchestrator.chat(req, cfg_repo, rate_repo,       │ │
        │                   usage_repo, strategy_repo, ...) │ │
        ▼                                                    │ │
  ┌──────────────┐                                           │ │
  │ Orchestrator │                                           │ │
  │              │                                           │ │
  │  1. resolve  │ — strategy=auto ? run detect_auto_strategy│ │
  │     strategy │ — else load strategy row (tags)           │ │
  │              │                                           │ │
  │  2. rank     │ — list_providers (enabled + api_key)      │ │
  │              │ — snapshot_all (health+counts+EMA+tokens) │ │
  │              │ — score = DSL + weight + headroom +       │ │
  │              │   latency EMA + reliability - in_flight   │ │
  │              │ — if images: filter to vision providers   │ │
  │              │ — sort desc, preferred gets +100          │ │
  │              │                                           │ │
  │  3. loop     │ — try_reserve (rpm,rpd) via plpgsql       │ │
  │              │   SELECT freeai_try_reserve(name, ...)    │ │
  │              │ — if None → next candidate                │ │
  │              │                                           │ │
  │  4. dispatch │ — provider.complete() with retry-1 on     │ │
  │              │   transient errors                        │ │
  │              │                                           │ │
  │  5. commit   │ — rate_repo.commit (health + counters)    │ │
  │              │ — usage_repo.record (telemetry row)       │ │
  │              │ — prom counters + histograms              │ │
  │              │                                           │ │
  │  6. done     │ — assemble ChatCompletionResponse         │ │
  └──────────────┘                                           │ │
        │                                                    │ │
        ▼                                                    │ │
  get_session() commits the unit-of-work ─────────────────── ┘ │
  (one tx covering rate commit + usage row)                    │
                                                               │
  response returned ─────────────────────────────────────────── ┘
```

### The atomic reservation

The non-obvious part is step 3. The problem it solves is that naive
"check capacity → call provider → record call" is racy: two concurrent requests
can both pass the check before either of them increments the counter.

A Python `RLock` would close the race for one process but not for many
workers. FreeAI uses a Postgres plpgsql function, `freeai_try_reserve`
(see [DATABASE.md § 3](DATABASE.md#3-the-atomic-reservation-function)),
which in a single statement:

1. Select (and lock) the `provider_stats` row with `FOR UPDATE`
2. Check quarantine / health
3. Count rows in `rate_events` within the rpm/rpd windows
4. If any limit is hit, `RETURN NULL`
5. Otherwise `INSERT` a new `rate_events` row and return its id

Because the whole thing runs inside one Postgres transaction with row locks,
two concurrent pods calling it serialize correctly. The test
[test_concurrent_reservations_respect_limit](../backend/tests/test_rate_repo.py)
fires 50 concurrent sessions against a cap of 5 and asserts that exactly 5
succeed.

### Streaming path

`/v1/chat/completions` with `stream: true` returns a `StreamingResponse` that
iterates `orchestrator.stream(...)`. The key difference from the non-streaming
path is that fallback only works **before the first chunk is sent** — once the
client has started receiving bytes, we can't silently switch providers. If an
error hits mid-stream, we re-raise and terminate the SSE stream.

The streaming adapter in
[openai_compat.py](../backend/app/providers/openai_compat.py) parses SSE
line-by-line looking for `data: {...}` frames and yields `StreamChunk`s. Gemini
has its own streaming path because its API doesn't speak OpenAI-compatible SSE.

## 4. Error handling

### The `ErrorKind` taxonomy

Every provider adapter must translate any failure into a
`ProviderError(kind=ErrorKind.X)`. The orchestrator then decides what to do:

| Kind | Example | Retry in place? | Fall back? | Counts as health failure? |
|---|---|---|---|---|
| `AUTH` | 401, 403, missing key | no | yes | no, but quarantines 24h so the admin notices |
| `RATE_LIMITED` | 429 | no | yes | no, respects `Retry-After` |
| `CLIENT_ERROR` | 400, 422 | no | **no** — same request would fail elsewhere | no |
| `SERVER_ERROR` | 5xx | yes (configurable, default 1 retry) | yes | yes, ticks circuit breaker |
| `NETWORK` | timeout, conn reset, stream idle | yes (configurable) | yes | yes, ticks circuit breaker |
| `PARSING` | unexpected response shape, 5+ malformed stream frames | no | yes | yes, ticks circuit breaker |
| `EMPTY_RESPONSE` | 200 OK with `content=""` and no `tool_calls` | yes | yes | yes, ticks circuit breaker |
| `CONTENT_FILTERED` | `finish_reason="content_filter"`, Gemini `SAFETY`/etc. | no | yes (unless first chunk already sent mid-stream) | **no** — provider is alive, just refused this prompt |
| `UNKNOWN` | unexpected exception | no | yes | yes, ticks circuit breaker |

The rules live in `rate_repo.commit()`:
`rate_limited`, `client_error`, `auth` and `content_filtered` are **benign** —
we never use them as evidence that the provider is unhealthy. Benign errors
don't count toward the circuit-breaker streak. AUTH is the one benign kind
that still triggers quarantine (24h), because a bad key won't fix itself.

### Circuit breaker and retry budget

`rate_repo.commit()` implements a sliding-window circuit breaker per
`(user_id, provider)` pair. Parameters are read from `app_config` at call
time so operators can tune them without restarting:

| Knob | Default | Meaning |
|---|---|---|
| `circuit_breaker_threshold` | 3 | Consecutive non-benign failures that trip the breaker. |
| `circuit_breaker_window_s` | 300 | Sliding window. Failures older than this are forgotten — an isolated blip every 10 minutes never trips. |
| `circuit_breaker_base_cooldown_s` | 30 | First cooldown after a trip. |
| `circuit_breaker_max_cooldown_s` | 3600 | Upper bound. Effective cooldown is `min(base * 2^cooldown_level, max)`. |
| `provider_max_retries` | 1 | Default retries per provider for transient errors before falling back. |
| `user_providers.max_retries` | NULL | Per-user-provider override of the retry budget. NULL = use global. |

`cooldown_level` lives in `provider_stats` and escalates on every trip
without a success in between; a single success resets it. That gives you
exponential backoff for a genuinely broken provider while healing
instantly when it recovers.

### Streaming robustness

The orchestrator wraps the upstream chunk iterator in
`asyncio.wait_for(anext(it), timeout=stream_idle_timeout_s)` (default 45s).
If the upstream goes silent for longer than that:

- **Before any chunk was flushed to the client** → raised as
  `ErrorKind.NETWORK`, triggers fallback to the next provider.
- **After at least one chunk** → propagated to the client as an SSE
  error; no fallback is possible once bytes are in flight.

Malformed SSE frames are tolerated up to 5 consecutive before giving up
with `ErrorKind.PARSING`. One valid frame resets the counter.

### Resource cleanup

Every endpoint that reserves a rate slot (`chat`, `stream`,
`/v1/embeddings`, `/v1/audio/transcriptions`) wraps its critical section
in `try/finally` that:

1. Decrements the in-flight concurrency counter (`_in_flight`).
2. Calls `rate_repo.rollback(reservation)` if the outcome was never
   committed — prevents cancelled client retries from piling up ghost
   entries in `rate_events` that would falsely exhaust a provider's RPM.
3. Closes the httpx stream iterator so the connection returns to the pool.

This matters because HTTP clients that time out short and retry hard
(some OpenAI SDK defaults) can otherwise exhaust reservations faster
than real traffic.

### HTTP error mapping

The orchestrator's `ProviderError.kind` maps to an HTTP status in
[main.py](../backend/app/main.py) via `_KIND_TO_STATUS`:

| Kind | HTTP |
|---|---|
| `CLIENT_ERROR` | 400 |
| `RATE_LIMITED` | 503 |
| `NETWORK` | 504 |
| anything else | 502 |

The body is always `{detail: {provider, kind, message}}` so clients can react
to the `kind` instead of parsing strings.

## 5. Request observability

Every HTTP request gets a `request_id` (from the `X-Request-Id` header if
present, otherwise a fresh 16-char hex). The middleware binds it to the
`structlog` contextvars so **every log line inside that request** has it
without anyone having to thread it through. The same id is echoed back in
`X-Request-Id`, so a client that logs an error can grep the server logs with
one command.

Prometheus histograms live next to the middleware:

- `freeai_http_requests_total{method,path,status}`
- `freeai_http_request_duration_seconds{method,path}`
- `freeai_provider_calls_total{provider,outcome}` — `outcome` ∈ {success, server_error, rate_limited, auth, network, client_error, parsing, empty_response, content_filtered, unknown}
- `freeai_provider_call_duration_seconds{provider}`
- `freeai_orchestrator_fallbacks_total{from_provider,to_provider}`
- `freeai_provider_circuit_breaker_trips_total{provider}` — incremented each time the breaker trips for any user of that provider.

See [OPERATIONS.md § 3](OPERATIONS.md#3-observability) for the Grafana setup.

## 6. Why these specific choices

Design decisions worth keeping in mind when evolving the project:

**Postgres as the single backing store.** JSON files + in-memory counters
would work for one process but fall apart the moment you run
`uvicorn --workers 4`. Postgres is what makes multi-pod deployments
possible. SQLite was considered; explicit locking semantics plus the
multi-host requirement pointed to Postgres.

**plpgsql for the reservation function instead of advisory locks + Python
logic.** Advisory locks would work, but the function keeps the hot path entirely
in SQL — one round-trip per reservation. Advisory-lock-in-Python-then-count
would need two round-trips, both of which have to succeed or both fail, which
is a smaller but real consistency hazard.

**DTOs instead of returning ORM rows from repositories.** The repository
pattern is sometimes criticized as redundant with SQLAlchemy. We use it for one
concrete reason: the session lifetime is bounded to one HTTP request, but
orchestrator code and tests want to pass provider config around without caring
about whether the session is still open. DTOs detach us from that. The
tradeoff is a `_row_to_dto` conversion — cheap.

**No singletons for orchestrator/repositories.** Everything flows through
`app.state` and `Depends(...)` — no module-level globals. This makes
tests trivial: swap the session, not the module.

**Vanilla JS frontend, no build step.** The frontend needs no toolchain, no
package.json, no bundler. That keeps "clone → run" fast, and the brutalist
control-room aesthetic is easier to keep consistent without a component
library's opinions fighting back. Cost: we're writing DOM manipulation by
hand. Worth it at this size (~900 LOC).

**`auto_strategy` as heuristics, not an LLM call.** An LLM-based intent
classifier would be more accurate but adds another upstream call for every
request. Heuristics get us 80% of the benefit for 0% of the latency. The
`AutoSignal` is logged so you can audit decisions after the fact.

## 7. What is NOT in the architecture

Things that don't exist yet, mostly on purpose, so nobody goes looking:

- **No background scheduler.** `usage_events.purge_older_than()` and
  `rate_events` purge exist as methods but nothing calls them yet. Tables grow
  unbounded until an operator runs the cleanup manually. See
  [REVIEW.md § 2](REVIEW.md#2-known-limitations).
- **In-memory rate counters.** `RateCounterStore` keeps per-provider call
  timestamps in a deque to avoid `COUNT(*)` over `rate_events` on every
  ranking. The plpgsql reservation function remains the correctness gate.
  These counters are per-pod and don't sync across instances.
- **Latency EMA.** `baseline_score()` uses an exponential moving average
  (alpha=0.3, stored in `provider_stats.latency_ema_ms`) instead of the
  single-sample `last_latency_ms`. Computed atomically in SQL to avoid
  read-then-write races.
- **Incremental token counter.** `tokens_today` and `tokens_day_start` in
  `provider_stats` replace the `SUM()` over `usage_events` that used to
  run on every request. Also computed atomically in SQL.
- **Concurrency-aware scoring.** The orchestrator tracks in-flight requests
  per provider and penalizes busy providers in the scoring formula.
- **Vision routing.** Requests with `image_url` content blocks are
  automatically filtered to vision-capable providers (Gemini, OpenRouter).
  Non-vision providers are excluded from the candidate pool.
- **No multi-tenancy.** One installation is one logical tenant. Client keys
  limit access but don't namespace data.
- **No cost tracking.** We count tokens but don't map them to dollars.
- **No model-level routing.** The orchestrator picks a provider, never a
  specific model within a provider. Strategy tags live on the provider row.
- **No retries per provider beyond 1.** `_MAX_RETRIES = 1`. Transient failures
  get one second chance; then fallback.
- **No partitioning for the events tables.** They're regular heap tables with
  indexes. Partition-by-time becomes interesting around ~10M rows.
