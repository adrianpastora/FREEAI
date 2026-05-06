# FreeAI

[![tests](https://github.com/adrianpastora/FREEAI/actions/workflows/tests.yml/badge.svg)](https://github.com/adrianpastora/FREEAI/actions/workflows/tests.yml)

A single OpenAI-compatible endpoint that orchestrates multiple free-tier AI
providers. Routes each request to the best one based on strategy tags, rate
limits and health, and falls back on failure — so your app gets the
reliability of a paid API with the cost of a free one.

```
┌──────────┐    ┌──────────────────────┐    ┌──────────────┐
│ your app │───▶│  FreeAI orchestrator │───▶│ Groq         │
│ (OpenAI  │    │   • routing          │───▶│ Gemini       │
│  format) │    │   • atomic rate      │───▶│ Mistral      │
└──────────┘    │   • fallback chain   │───▶│ OpenRouter   │
                │   • streaming        │───▶│ Cohere       │
                │   • Postgres state   │───▶│ HuggingFace  │
                └──────────────────────┘    └──────────────┘
                          │
                          ▼
                  ┌───────────────┐
                  │   Postgres    │  ◀── multi-pod safe
                  │ (config + RL) │
                  └───────────────┘
```

## Quick start

```bash
docker compose up --build
```

No `.env` file is required for a **local** trial: Compose uses a built-in Postgres
password (see `docker-compose.yml` — override `POSTGRES_PASSWORD` for anything
beyond localhost). Opens on [http://localhost:8000](http://localhost:8000).
Postgres listens on `127.0.0.1:5444` only.

**First visit:** if you did not set `FREEAI_MASTER_KEY`, the logs print a
**bootstrap token** and an **encryption master key**. Open the UI once, paste both,
pick admin username and password, and submit — that is the only required setup.
Optional: `python scripts/ensure_dotenv.py` sets a random Postgres password in `.env`;
`FREEAI_ADMIN_TOKEN` or `FREEAI_LEGACY_INITIAL_SETUP=true` enable the legacy
admin-token wizard. Add provider keys in the panel, create a client from *Clients*,
and you have a working endpoint:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer fai_…" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Explain QUIC in one sentence"}]}'
```

With streaming:

```bash
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer fai_…" \
  -d '{"messages":[{"role":"user","content":"Count to 10"}],"stream":true}'
```

Any OpenAI-compatible client works — point its base URL at
`http://localhost:8000/v1`.

## What it does

- **Multi-provider routing** — adapters for Groq, Google Gemini, Mistral,
  OpenRouter, Cohere and HuggingFace, all behind one OpenAI-compatible endpoint.
- **Strategies as data** — 8 built-in routing strategies (`auto`, `fastest`,
  `cheapest`, `best_quality`, `coding`, `reasoning`, `vision`, `long_context`)
  plus any you create from the UI at runtime.
- **Atomic rate limiting** — reservation happens inside a Postgres plpgsql
  function so N concurrent pods against the same provider never exceed the
  cap. Test `test_concurrent_reservations_respect_limit` fires 50 sessions
  against a limit of 5 and verifies exactly 5 succeed.
- **Typed error handling** — every upstream failure is classified as `auth`,
  `rate_limited`, `client_error`, `server_error`, `network`, `parsing`, or
  `unknown`. Transient errors retry once in place; rate-limited errors fall
  back immediately without marking the provider unhealthy; auth errors
  quarantine for 24h.
- **Streaming with bounded fallback** — SSE out, falling back only before the
  first byte. OpenAI-compatible providers and Gemini support it natively.
- **Multimodal vision** — send images via OpenAI-compatible `image_url`
  content blocks. The orchestrator detects images and routes to
  vision-capable providers (Gemini, OpenRouter). Gemini receives images
  translated to its native `inlineData`/`fileData` format; OpenRouter gets
  the standard OpenAI multimodal format. Non-vision providers are
  automatically excluded.
- **Embeddings with fallback** — `POST /v1/embeddings` (OpenAI-compatible)
  returns text embeddings through Mistral (`mistral-embed`, 1024 dim) or
  Gemini (`text-embedding-004`, 768 dim). Same rate-limiting, quarantine
  and analytics pipeline as chat — events land in `usage_events` with
  `strategy = "embedding"`. See [docs/API.md § Embeddings](docs/API.md#embeddings).
- **Language-aware auto strategy** — detects EN/ES/FR/DE/PT from stopword
  frequency and picks coding/reasoning/vision/long_context/fastest from
  signals in the prompt (including actual `image_url` blocks), without an
  external LLM call.
- **Inbound auth with bootstrap mode** — the server issues its own client API
  keys. With zero clients created, `/v1/*` is open and the server logs a
  warning. One `POST /api/clients` later, it requires a bearer key.
- **Encryption at rest** — provider keys are stored Fernet-encrypted in
  Postgres; the master key comes from `FREEAI_MASTER_KEY` or an
  autogenerated file.
- **Observability included** — `structlog` with request-id contextvars,
  Prometheus `/metrics`, and an opt-in Grafana dashboard preconfigured under
  the `observability` docker-compose profile.
- **Analytics panel** — the frontend has a live analytics tab reading from
  the `usage_events` table, with KPIs, time series, and breakdowns by
  provider / strategy / outcome.
- **236 pytest tests** — unit, integration, E2E, streaming, security,
  and fallback robustness (empty responses, content filtering, stream
  idle timeout, circuit breaker, configurable retries). A handful of pure
  tests run without Docker; the rest spin up a real Postgres via
  testcontainers locally, or a service container in CI.
- **Robust fallback chain** — empty 200-OK responses, content-filtered
  finish reasons and stream stalls all trigger automatic fallback to
  the next provider. Per-user sliding-window circuit breaker with
  exponential cooldown prevents a degraded upstream from dragging
  every user down. See [docs/ARCHITECTURE.md § 4](docs/ARCHITECTURE.md#4-error-handling).

## Documentation

This README is a pointer. The actual documentation lives in
[docs/](docs/):

| Document | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layering, request flow, error taxonomy, design decisions |
| [docs/API.md](docs/API.md) | Full endpoint reference with examples and auth details |
| [docs/EMBEDDINGS.md](docs/EMBEDDINGS.md) | `/v1/embeddings` — provider table, same-model rule, fallback loop, RAG guidance |
| [docs/DATABASE.md](docs/DATABASE.md) | Schema, migrations, the atomic reservation function, useful queries |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Deploy, observability, backup/restore, reverse-proxy, troubleshooting |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Contributing, tests, adding providers/strategies, frontend conventions |
| [docs/REVIEW.md](docs/REVIEW.md) | Current state, known limitations, design decisions worth preserving, backlog |

If you're **using FreeAI as a client**, start with [API.md](docs/API.md).

If you're **deploying it**, [OPERATIONS.md](docs/OPERATIONS.md) is the one.

If you're **contributing or extending**, [ARCHITECTURE.md](docs/ARCHITECTURE.md)
+ [DEVELOPMENT.md](docs/DEVELOPMENT.md), and skim
[REVIEW.md](docs/REVIEW.md) so you know which design decisions are
load-bearing before you refactor them. Also read
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
Releases are tracked in [CHANGELOG.md](CHANGELOG.md).

## Status

FreeAI is production-ready and actively maintained. Highlights of what's
in the box today:

- **Reliability** — atomic reservation inside a Postgres plpgsql function,
  per-client rate limiting, self-healing quarantine, robust fallback on
  empty/filtered/stalled responses, per-user sliding-window circuit breaker.
- **Performance** — bounded-cardinality Prometheus metrics, batched ranking
  queries (3 per request instead of 2×N), strategy TTL cache, in-memory
  rate counters, streaming with a single commit at end of request,
  per-(user, provider) in-flight tracking.
- **Smart routing** — EMA-based latency scoring, reliability penalty,
  concurrency-aware load spreading, 8 built-in strategies plus user-defined
  ones as data.
- **Multimodal vision** — OpenAI `image_url` blocks accepted as `data:`
  URIs, translated to Gemini `inlineData` natively, auto-routed to
  vision-capable providers.
- **Security by default** — one-time bootstrap token for the setup wizard,
  per-user provider keys encrypted at rest with Fernet, JWT secret kept
  independent from the master key, tight body/image size caps, login
  rate limiting, sanitized error bodies, strict security headers, CORS
  locked down by default.

Remaining future-work backlog (table partitioning, Helm chart, cost
tracking, semantic cache) lives in [docs/REVIEW.md](docs/REVIEW.md).

## Run the tests

```bash
cd backend
pytest             # full suite (Postgres via testcontainers — needs Docker)

# Already running Postgres? Point pytest at it and skip the testcontainers
# spin-up (this is exactly what CI does):
FREEAI_TEST_DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:5432/DB pytest

# A few pure unit tests run without any Postgres at all:
pytest tests/test_crypto.py tests/test_auto_strategy.py \
       tests/test_known_models.py tests/test_schema_tool_calls.py
```

Every push and pull request runs the full suite against a Postgres 16
service container in GitHub Actions — see [`.github/workflows/tests.yml`](.github/workflows/tests.yml).

## License

MIT License. See [LICENSE](LICENSE).
