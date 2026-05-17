# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security-sensitive
reports.** Open a private advisory via
[GitHub → Security → Report a vulnerability](../../security/advisories/new).
It's tracked, encrypted in transit, and lets us coordinate a fix and a
CVE without leaking the issue first.

Please include:

- A clear description of the issue and its impact.
- Reproduction steps or a proof-of-concept request, if you have one.
- The commit hash or tag you observed it on.
- Your name / handle if you'd like to be credited.

I'll acknowledge the report within a few days, work with you on a fix
and a timeline, and credit you in the release notes unless you prefer
to stay anonymous.

## Supported versions

FreeAI is pre-1.0 and moves on `main`. Fixes land on `main`; there are
no LTS branches. If you operate an older revision, please upgrade
before asking for a backport.

## Threat model and non-goals

FreeAI is designed to be self-hosted behind a reverse proxy by a single
team. What the project actively defends against:

- Drive-by takeover of a fresh instance. The first-admin endpoint always
  requires a one-time bootstrap token in the `X-Bootstrap-Token` header.
  In the default mode the frontend reads that token from
  `GET /api/setup/bootstrap-token` (loopback peers only) so a normal
  localhost install needs no manual token paste. For instances exposed
  to the public internet on first boot, set
  `FREEAI_REQUIRE_BOOTSTRAP_HEADER=true` — the loopback endpoint is then
  refused and both the master encryption key and the bootstrap token must
  be copied from the server logs into the UI.
- Cross-tenant leakage between users (provider keys, client keys, and
  analytics are scoped per user at the repository layer).
- SSRF through `image_url` content blocks (only `data:` URIs are
  accepted).
- Brute-force login (rate-limited per `(IP, username)`).
- Denial of service by large request bodies (10 MB cap on JSON, 25 MB
  on audio uploads, with tight per-message size caps).
- Secret exposure through provider error bodies (sanitized before
  they reach the client).
- Cache-hash and JWT-forgery pivots from a single leaked secret (the
  encryption master key and the JWT secret are independent).
- Common misconfigurations on the default compose deploy — Postgres
  is bound to `127.0.0.1` only, CSP and other security headers are set by default.
  The bundled `docker-compose.yml` uses a **public default** Postgres password
  when `POSTGRES_PASSWORD` is unset — fine for localhost only; set a strong
  secret in `.env` before any non-trusted network can reach the host.

Non-goals — things we do *not* defend against and that you must handle
at a layer above FreeAI if they matter to you:

- DDoS at the network layer. Put it behind a CDN / WAF.
- Malicious administrators. Admins see everything by design.
- Compromise of the host machine or the Docker daemon.
- Providers leaking your prompts. We send the prompt verbatim.

## Handling secrets

Never share these publicly; if you paste them into a log, rotate them:

- `FREEAI_MASTER_KEY` — encrypts provider API keys at rest.
- `FREEAI_JWT_SECRET` (or `data/.jwt_secret`) — signs access tokens.
- `FREEAI_ADMIN_TOKEN` — legacy admin auth, if you use it.
- `POSTGRES_PASSWORD` — database password.
- The one-time bootstrap token printed at first startup.
- Provider API keys stored in the UI.

Rotating the master key currently invalidates every stored provider
key (they can't be decrypted any more). Plan a re-entry of keys if you
rotate.

## Responsible disclosure timeline

I'll aim for:

- **Acknowledgement** within 3 days.
- **Triage + severity assessment** within 7 days.
- **Fix + release** within 30 days for anything rated high or above,
  sooner for critical issues.

Low-severity findings may be batched into the next release.
