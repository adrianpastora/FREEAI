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

[Unreleased]: https://github.com/adrianpastora/FREEAI/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.6.0
[0.5.0]: https://github.com/adrianpastora/FREEAI/releases/tag/v0.5.0
