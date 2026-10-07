# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

Pre-1.0 caveat: breaking changes land on `main` with a clear note here
and a bumped minor. Backward-compat shims are not guaranteed between
pre-1.0 versions — follow the Unreleased section if you track `main`.

## [Unreleased]

<!-- Add entries here as they land. Categories used in this changelog:
     Added, Changed, Fixed, Security, Removed, Deprecated. -->

## [0.8.0] — 2026-10-08

Reliable fallback and a self-maintaining provider catalog. Between May and
October 2026 most free tiers changed under FreeAI. Cerebras went card-only
(2026-07-16), Groq shut down Llama 3.x (2026-08-16), OpenRouter rotated
every `:free` model, the HuggingFace default left the router, and Gemini
2.5 / `text-embedding-004` were restricted or retired. The fallback chain
also stopped at the first of those errors, so most requests failed outright.

### Upgrade notes
- Migration **0021** adds `app_config.catalog_version`. It runs automatically
  with `FREEAI_AUTO_MIGRATE=true` (the default).
- On the first boot after upgrading, `sync_catalog()` updates the built-in
  provider rows to the 2026-10-07 catalog: default models, limits, weights and
  tags. Admin edits to those rows are overwritten once. API keys and the
  enabled flag are not touched. Per-user `default_model` overrides that point
  at a retired model are cleared. Admin-tuned prices are kept.
- New provider **NVIDIA NIM** (`nvidia`). Its card appears in the panel.
  Get a free key at build.nvidia.com; no card required.
- Gemini embeddings now default to `gemini-embedding-001` (3072 dims).
  `text-embedding-004` (768 dims) was shut down by Google on 2026-01-14.
  Re-embed stored vectors if you mix the two.
- Behaviour change: an upstream 4xx (`client_error`) now falls back to the
  next provider. The chain stops only once two providers reject the same
  request. A 429 without `Retry-After` parks the provider for 60 s.

### Fixed
- Fallback stopped at the first provider that returned a non-auth 4xx, so a
  retired model (Groq `model_decommissioned`, OpenRouter 404) or a spent quota
  (HuggingFace 402) killed every request. Those errors were also "benign",
  so the broken provider stayed first in the ranking forever.
- The 24 h quarantine for `auth` errors was computed but never applied,
  because the breaker path ignored it. A revoked key was retried every 30 s
  to 1 h.
- A 429 without `Retry-After` no longer gets the same provider retried on the
  very next request.
- Streaming: an unexpected adapter exception before the first chunk now falls
  back instead of killing the stream. The Gemini stream no longer crashes on
  `candidates: []`. Gemini and Cohere non-JSON bodies surface as `parsing`.
- Embeddings and transcription no longer stop the chain on `auth` errors.
- When every provider is quarantined, the error now names them instead of
  saying "no provider configured".
- Setup wizard: the bootstrap-token banner now reprints on every restart while
  setup is still pending (`d9cc95e`). The paranoid-mode restart path now
  reprints the banner instead of silently swallowing it (`678a86b`).

### Added
- `model_unavailable` and `quota_exhausted` error kinds, classified from the
  status code *and* the error body: 402, `model_not_found`,
  `decommissioned`, "no endpoints found", Gemini `limit: 0`, trial/credits
  wording, and daily-quota 429s.
- Shared quarantine policy (`providers.base.quarantine_seconds_for`):
  - auth: 24 h
  - 402: 6 h
  - daily quota: 1 h
  - 429: `Retry-After` or 60 s
  - every model of a provider gone: 1 h
- Per-provider model fallback. When a model is retired, the orchestrator tries
  the provider's next catalog model before moving to another provider. A
  model the client named explicitly is never swapped. Dead models are
  remembered for 6 h, per user (availability is per account).
- `backend/app/providers/catalog.py` is now the single source of truth for
  default and fallback models, retired models, limits, tags, seed prices and
  free-tier notes. `KNOWN_MODELS` and `DEFAULT_PROVIDERS` are derived from it.
  It is synced to the DB on boot whenever `CATALOG_VERSION` changes, so
  future refreshes need no migration.
- `scripts/check_catalog.py` checks the catalog against each provider's live
  `/models` list. It exits 1 on missing models and suggests new OpenRouter
  `:free` models.
- Prometheus counter `freeai_provider_model_unavailable_total{provider,model}`.
  A non-zero rate means the catalog is stale.
- `GET /api/me/providers/catalog` now returns `free_tier`,
  `catalog_verified` and `fallback_models`. The panel shows the backend's
  free-tier text instead of hard-coded copy.
- NVIDIA NIM provider (OpenAI-compatible, `integrate.api.nvidia.com`).
  `nvapi-` keys are redacted from error messages.
- Saving a new key or model for a provider clears its quarantine.
- `docs/providers/CATALOG.md`: how the catalog works and the update
  procedure.

### Changed
- Catalog refresh, verified 2026-10-07:

  | Provider | New default (fallbacks) | Notes |
  |---|---|---|
  | Groq | `openai/gpt-oss-120b` (`gpt-oss-20b`, `qwen/qwen3.8-27b`) | Llama 3.x / Gemma / Mixtral retired; ~1,000 RPD, 200K TPD |
  | Gemini | `gemini-3.5-flash` (3.5 Flash-Lite, 3 Flash preview, 3.1 Flash-Lite, 2.5 Flash) | 2.5 only for projects that used it before |
  | OpenRouter | `nvidia/nemotron-3-super-120b-a12b:free` (Gemma 4 31B, Nemotron 3 Ultra, Gemma 4 26B) | old `:free` models gone; 50 RPD without credits |
  | HuggingFace | `openai/gpt-oss-120b` (Qwen3.6-27B, Llama 3.3 70B, Llama 3.1 8B) | $0.10/month of credit on free accounts |
  | NVIDIA | `nvidia/nemotron-3-super-120b-a12b` (DeepSeek v4.1 Flash, GLM 5.3 Flash, gpt-oss-20b) | new; ~40 RPM, no card |
  | Mistral | `mistral-small-latest` (medium, codestral, nemo) | `mistral-large` no longer an automatic fallback |
  | Cohere | `command-r-08-2024` (`command-r7b-12-2024`) | unchanged; 1,000 calls/month |
  | Cerebras | `gpt-oss-120b` | trial-only: $5 for 30 days with a payment method |

- Gemini transcription defaults to `gemini-3.5-flash`.
- The setup wizard lists providers that need no card first; Cerebras is last.
- README "Quick start" restructured as numbered Docker Compose steps, with a
  separate "Useful commands" block. The paranoid / doctor / `ensure_dotenv`
  knobs are tucked under a `<details>` so the first read isn't drowned in
  advanced options.
- Documentation pass across `docs/` to match the current code:
  - 13-table schema (was 10 in `DATABASE.md`, 7 in `ARCHITECTURE.md`).
  - Migration 0020, the `model_prices` table and the `cost_usd` /
    `sum_cost_usd` columns documented.
  - `OPERATIONS.md` env-var list completed with the JWT, paranoid-mode and
    setup-path variables that were missing.
  - `API.md` extended with the per-user, pricing, setup, auth, users, tags
    and historical-analytics endpoint families (roughly half of the admin
    surface was missing).
  - `DEVELOPMENT.md` tag table updated for Cerebras, audio and embeddings.
  - `providers/cerebras.md` status changed from "doc only" to "integrated
    since 0.7.0".
  - Test counts updated from 243 to 272.
  - README claim about tests running in CI corrected to manual-only.
  - `ARCHITECTURE.md`, `API.md` and `DATABASE.md` updated for the new error
    kinds, quarantine policy and migration 0021.

## [0.7.3] — 2026-05-17

### Fixed
- Boot crash on the pricing-admin router. `DELETE /api/pricing/{provider}/{model}`
  was declared with `status_code=204` but kept `HTTPException(404, ...)`
  for the not-found case — FastAPI 0.115 rejects any body declaration
  with 204 at import time, taking the whole app down with
  `AssertionError: Status code 204 must not have a response body`.
  Endpoint now returns 200 with `{"deleted": true}` on success and still
  raises 404 on missing rows.

## [0.7.2] — 2026-05-17

Documentation polish for the first public release.

### Changed
- Added explicit non-affiliation disclaimer in the "Acceptable use"
  section of the README to make the trademark posture clear (nominative
  fair use is already covered, this just keeps the legal conversation
  short if a provider's brand team ever reads it).

## [0.7.1] — 2026-05-17

### Fixed
- Cerebras provider was losing every routing decision against Groq
  despite being ~4× faster. Bumped its default weight to 1.1 and added
  `coding`/`quality` tags so it wins `fastest`, competes in `reasoning`,
  and stops being hard-excluded from `coding`/`best_quality` routes.

## [0.7.0] — 2026-05-17

First release after the multi-user migration. Adds a new provider
(Cerebras), a real self-host story with HTTPS, a design pass over the
Providers panel, robustness improvements on the request pipeline, and an
install doctor to make troubleshooting the setup flow obvious.

### Added
- **Cerebras Inference provider.** `gpt-oss-120b` on Cerebras' Wafer-Scale
  Engine hardware (~3000 t/s, 1M tokens/day free tier, no card, no expiry).
  Wired into the multi-step setup wizard with its own guide and added to
  the secret-scrub regex so `csk-…` keys are redacted from error bodies
  like the other provider key formats. Full reference doc at
  `docs/providers/cerebras.md`.
- **`docker-compose.prod.yml` overlay with Caddy.** One command brings up
  FreeAI behind automatic Let's Encrypt HTTPS for a given `FREEAI_DOMAIN`.
  SSE streaming flushes correctly (`flush_interval -1`), security headers
  set, body size capped to match the backend.
- **`scripts/doctor.py`** — read-only diagnostic that reports which startup
  mode this install is in (default, paranoid, legacy admin-token, legacy
  wizard, pending master key) and why. ASCII-only output so it runs on
  Windows cp1252 consoles. Surfaced from the README's "Optional knobs".
- **Per-request retry tuning.** Honors provider `Retry-After` headers
  (capped at 5s — longer waits are quarantine's job) and applies
  multiplicative jitter to the exponential backoff so concurrent failures
  don't retry in lock-step.
- **Inline-image size cap.** 20 MB hard limit on `data:` URIs forwarded to
  vision-capable providers, checked against base64 length before decoding.
- **`estimate_tokens()`** helper for adapters that don't get a usage block
  back from the upstream — uses `chars/4` as a conservative under-count.

### Changed
- **`tests.yml` is manual-only now.** The workflow no longer runs on push
  or PR; trigger it from the Actions tab when you want a clean-room
  verification. Local pytest with testcontainers is the source of truth.
- **`deploy.yml` renamed to `deploy-personal.yml`** with a header banner
  explaining it is maintainer-specific (SSH+Docker to a single VPS) and
  that forks should delete it. Behaviour unchanged: still fires only on
  pushed `v*` tags or manual dispatch.
- **Providers panel design refinements** (frontend/styles.css): provider
  names render as `// groq` (mono italic lowercase) instead of `Groq`
  (serif italic) to read as endpoints rather than wordmarks. Switch
  toggles replaced with a two-cell brutalist control. Status dots
  replaced with typographic glyphs (■ ✕ ○ ▸). Capability tags get a 45°
  hatch background. Meters grouped into recessed mini-blocks. DNA
  (amber/bone/charcoal, Fraunces headings, ASCII corner brackets,
  brutalist shadows) preserved — this is a refinement, not a reset.
- **Setup modal copy** clarified. The paranoid-mode paragraph now names
  `FREEAI_REQUIRE_BOOTSTRAP_HEADER` explicitly so operators know which
  flag put them there, instead of the unhelpful "Paranoid mode is on".
  The bootstrap-token placeholder reads "paste the token printed in the
  server logs" instead of the cryptic "FreeAI bootstrap token banner".
- **httpx pool sized for multi-user concurrent traffic** — 200 connections,
  50 keepalive, 30s pool timeout. The old 100/20 was tight under the
  multi-user model.

### Removed
- The orphan `cloudflare/workers-autoconfig` branch and the bot-generated
  `wrangler.jsonc` that came with it. The Cloudflare Workers integration
  was never wired and would have broken SSE streaming on free-tier plans
  anyway. Self-host with Docker + Caddy is the supported deploy path.

### Security
- Cerebras `csk-` keys added to `_SECRET_PATTERNS` in
  `backend/app/providers/base.py` so leaked Cerebras tokens in error
  bodies and logs get redacted alongside `gsk_`, `hf_`, `AIza`, `xai-`.

## [0.6.0] — 2026-05-06

Code-quality + onboarding pass on top of 0.5.0. Same product surface, much
nicer to read, much nicer to install. The big visible win is the new
zero-friction setup wizard; the rest is the kind of polish that's invisible
when you use the software but obvious when you read its source.

### Added
- **Zero-friction first-run setup.** Default mode now creates the master
  encryption key, auto-confirms it, and stores the bootstrap token where
  the frontend can fetch it from loopback peers — so a fresh
  `docker compose up` only asks the operator for username + password. No
  more copying values from container logs into web-form fields.
- New `FREEAI_REQUIRE_BOOTSTRAP_HEADER` setting restores the previous
  manual-paste behaviour for instances exposed directly to the internet
  on first boot. Documented in `SECURITY.md` and `docs/OPERATIONS.md`.
- New public endpoint `GET /api/setup/bootstrap-token` returns the
  on-disk token to loopback peers (`127.0.0.1` / `::1` / `localhost`)
  in default mode. Refused in paranoid mode and refused for non-loopback
  peers always. The token is still required in the `X-Bootstrap-Token`
  header on `POST /api/setup/first-admin` — the protocol is unchanged,
  only the way the frontend obtains the value moved.
- `paranoid_mode` field added to `GET /api/setup/status` so the frontend
  conditionally reveals the bootstrap-token / master-key fields.
- New `tests` GitHub Actions workflow runs the full suite (243 tests)
  against a Postgres 16 service container on every push and pull
  request. CI badge added to README.
- `BaseProvider.transcribe()` is now part of the public provider
  contract alongside `complete` / `stream` / `embed`. Adding speech-to-text
  to a new provider is a method override + a `supports_transcription = True`
  flag, mirroring how embeddings already worked.
- `UserProviderDTO.to_provider_config()` projects per-user merged DTOs
  to the catalog shape the orchestrator's ranker expects — replaces the
  private `Orchestrator._user_provider_to_config` static method that
  was being accessed from outside its class.
- `Orchestrator.http_client` property exposes the shared httpx pool to
  embeddings + transcription dispatchers without reaching into the
  private `_client` attribute.
- `routers/_common.require_user_id` FastAPI dependency replaces three
  copies of the same `getattr(request.state, "user_id", None)` +
  `raise HTTPException(400, ...)` block in chat / embeddings /
  transcriptions.

### Changed
- **`backend/app/main.py` split into per-resource routers** — 2,397 lines
  → 192 lines. Endpoint groups now live in `app/routers/{auth,chat,
  embeddings,transcriptions,setup,users,me_providers,providers_admin,
  config,strategies,analytics,clients,health}.py` with a shared
  `_common.py` for cross-cutting helpers.
- Lifespan, `_run_migrations`, and `_periodic_purge` extracted from
  `main.py` to a new `app/lifecycle.py` module so the entrypoint reads
  as a single page of wiring.
- `transcription.py` slimmed from 313 lines (two free-floating async
  functions, hardcoded URLs, a parallel `TranscriptionError` type) to
  61 lines (priority order, capability lookup, MIME helpers). Every
  provider-specific knob now lives with its provider class.
- `routers/__init__.py` no longer eagerly re-exports the 14 router
  modules; `main.py` imports the submodules it needs by name. Cuts
  cold-start cost when anything touches `app.routers`.

### Fixed
- `GeminiProvider._content_to_parts` raised `TypeError` when rejecting
  a non-data-URI `image_url` — the `ProviderError` constructor was
  called with positional args in the wrong order (`http_status=400`
  isn't even a valid parameter). Now correctly raises
  `ProviderError(self.name, ..., kind=ErrorKind.CLIENT_ERROR, status=400)`.

### Removed
- Free-floating `_transcribe_groq` / `_transcribe_gemini` functions
  with their own URL / model / timeout constants and a parallel
  `TranscriptionError` type. Subsumed into `BaseProvider.transcribe`
  + `ProviderError`.
- The legacy `setupModal` is no longer the default path for fresh
  installs. The simplified create-admin flow handles both default and
  paranoid modes; the old modal is reachable only when
  `FREEAI_LEGACY_INITIAL_SETUP=true` is set explicitly.

## [0.5.0] — 2026-04-19

First release prepared for a public, open-source audience. The codebase
has been used in production internally for weeks; this release is the
hardened, documented version that's safe to drop into someone else's
infrastructure.

### Security
- One-time bootstrap token required for the setup wizard — blocks
  drive-by takeover of fresh instances exposed to the internet before
  the operator completes setup. Token is printed to stdout on first run
  and consumed on first successful setup / registration.
- JWT signing secret kept independent from the encryption master key.
  A leak of one no longer compromises the other. `.jwt_secret` is
  auto-generated at first startup when unset.
- Postgres bound to `127.0.0.1:5444` on the host by default; the
  `POSTGRES_PASSWORD` env var is now required by `docker-compose.yml`
  (no default fallback).
- Body size middleware — 413 when `Content-Length` exceeds 10 MB
  (25 MB on `/v1/audio/*`).
- Tight chat schema caps: `messages` 1-200, `temperature` 0-2,
  `max_tokens` 1-32k, ≤32 content blocks per message, ≤8 image blocks,
  text ≤200k chars, `image_url` payload ≤6M chars.
- `image_url` content blocks must be `data:` URIs — remote URLs are
  rejected at the schema layer (SSRF guard for every vision-capable
  provider).
- Login rate limiting — 10 attempts per `(IP, username)` in a 5-minute
  window, counter resets on a successful login. Same rate limiter
  covers `/api/auth/migrate-token`.
- Postgres advisory lock around `/api/setup/initial` and the first
  `/api/auth/register` — concurrent callers can't both claim admin.
- Strict security headers: CSP (script-src 'self'), HSTS,
  X-Frame-Options DENY, X-Content-Type-Options nosniff,
  Referrer-Policy no-referrer, Permissions-Policy. CORS
  `allow_headers` narrowed from `*` to the specific headers the app
  uses.
- Provider error bodies sanitized before reaching the client — bearer
  tokens, common API-key prefixes, Authorization-like headers and
  query-string secrets are redacted.
- `/api/health` is now minimal (`{"status":"ok"}`) so an
  unauthenticated scanner can't fingerprint the deployment.
- Docker hardening — `no-new-privileges:true` on every service,
  `cap_drop: [ALL]` on the app container, `mem_limit` / `cpus` on
  every service, multi-stage Dockerfile so `build-essential` stays
  out of the runtime image.
- Orchestrator `_in_flight` keyed by `(user_id, provider)` and
  bounded to 10k entries — one tenant's traffic can't skew another's
  scoring, and many-user floods can't grow the map unbounded.
- `migrate-token` closes the "placeholder + real user" edge case,
  wraps in the setup advisory lock, and rate-limits attempts.
- Dependency bumps: `pydantic 2.9.2 → 2.10.4` (CVE-2024-45590),
  `PyJWT 2.9.0 → 2.10.1` (CVE-2024-33891).
- Defense-in-depth in `_try_jwt` — unknown `role` claims are clamped
  to `"user"` and malformed payloads are rejected.
- `mask_key` now hides the provider prefix (only the last 4 chars are
  shown) so a glimpse of the UI doesn't confirm key formats during
  bruteforce.

### Changed
- Frontend fully translated to English. The setup wizard, login /
  register / migrate modals, the multi-step provider wizard and all
  per-provider guides, inline errors, dialogs, and summary screens no
  longer mix Spanish and English.
- `docs/REVIEW.md` rewritten from a development journal into a concise
  "current state / known limitations / load-bearing design decisions /
  backlog" document aimed at operators and contributors.
- Sprint-numbering framing removed from all public docs
  (`ARCHITECTURE.md`, `EMBEDDINGS.md`, `OPERATIONS.md`,
  `STRATEGY_DSL.md`, `API.md`, `DEVELOPMENT.md`, module docstrings).
- `docs/API.md` gained a dedicated "Bootstrap token" section
  documenting the stdout banner and `X-Bootstrap-Token` header.
- `backend/requirements-dev.txt` split out so the production Docker
  image no longer ships `pytest` and `testcontainers`.

### Added
- `SECURITY.md` — threat model, reporting flow, supported versions,
  secret-handling guidance.
- `CONTRIBUTING.md` — dev setup, conventions, PR checklist, and a
  short list of changes that won't merge without an issue first.
- `.github/ISSUE_TEMPLATE/` — bug report and feature request forms,
  plus a `config.yml` that points security reports to advisories and
  usage questions to Discussions.
- `.github/PULL_REQUEST_TEMPLATE.md` — standard PR template with a
  lightweight checklist.
- Open Graph / Twitter meta tags in `frontend/index.html` so link
  previews look decent.

### Fixed
- Drop auto-recovery of orphaned provider keys in `/api/me/providers`.
  An admin would silently inherit another admin's encrypted keys on
  user deletion; the FK `ON DELETE CASCADE` makes the path
  unreachable anyway.
- `docker-compose.yml` no longer fails the `build` step when the
  `observability` profile is inactive but `GRAFANA_PASSWORD` is unset
  (was a `${VAR:?}` blocker for the main service).
- Alembic stdout dropped from INFO to DEBUG — no more verbose schema
  chatter in production logs; failures still surface with the last
  2k of stdout/stderr.
- `/v1/chat/completions` streaming path and `/v1/embeddings` now
  always release their `_in_flight` slot and roll back their
  `rate_events` reservation on client cancellation.
- Schema accepts `content: null`, `tool_calls`, `tool_call_id`, and
  OpenAI SDK extras (`tools`, `response_format`, `seed`, `top_p`,
  etc.) so full OpenAI histories round-trip without 422s.

### Removed
- Deploy workflow no longer hardcodes the operator's SSH host, port,
  user, project dir, repo URL, or public CORS origin — all are
  supplied via GitHub Actions secrets. The default CORS origin list
  in settings is now just localhost.
- Drop the legacy "Sprint N shipped" changelog section from README —
  replaced with a themed Status section.

[Unreleased]: https://github.com/adrianpastora/FREEAI/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.8.0
[0.7.3]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.7.3
[0.7.2]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.7.2
[0.7.1]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.7.1
[0.7.0]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.7.0
[0.6.0]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.6.0
[0.5.0]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.5.0
