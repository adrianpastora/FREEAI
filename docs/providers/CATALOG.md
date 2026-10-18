# Provider catalog: keeping free tiers up to date

Free LLM APIs churn. Models get retired every few months, and free plans
change terms (Cerebras started requiring a card in July 2026, OpenRouter
rotates its `:free` models). FreeAI keeps everything that depends on that
churn in one file and is built to keep working while the file is out of date.

## Where things live

| What | Where |
|---|---|
| Default model, fallback models, retired models, limits, tags, free-tier note, seed prices | `backend/app/providers/catalog.py` |
| Wire format of each provider | `backend/app/providers/*_provider.py` |
| Setup-wizard steps (signup links, screenshots text) | `frontend/app.js` → `PROVIDER_GUIDES` |

`KNOWN_MODELS` (model dropdown) and `DEFAULT_PROVIDERS` (DB seed) are
derived from the catalog. Don't edit them directly. The panel reads the
free-tier note from the catalog too (`GET /api/me/providers/catalog`).

## How FreeAI copes when the catalog is stale

1. **Model fallback inside a provider.** When a model comes back as retired
   or unknown (`model_unavailable`: 404, `model_not_found`, `decommissioned`,
   Gemini `limit: 0`, …), the orchestrator tries the provider's next catalog
   model before moving to another provider. A model the client named
   explicitly is never swapped.
2. **Dead-model memory.** A model that answered `model_unavailable` is
   skipped for 6 h, so later requests don't pay a failed call for it.
3. **Provider fallback on provider-specific errors.** Bad key (`auth`, 24 h
   quarantine), spent credits (`quota_exhausted`: 402 → 6 h, daily 429 → 1 h),
   plain 429 (Retry-After or 60 s) and every model gone (`model_unavailable`,
   1 h) all move on to the next provider. A plain 4xx (`client_error`) also
   falls back; the chain only stops once two providers reject the request.
   Saving a new key or model in the panel clears the quarantine.
4. **Visibility.** Every retired-model hit logs
   `model unavailable — update app/providers/catalog.py` and increments the
   Prometheus counter `freeai_provider_model_unavailable_total{provider,model}`.
   A non-zero rate means the catalog needs an update.

## How rate limits and quotas are handled

FreeAI keeps every request going by moving to the next model or provider
before a limit turns into a failed request:

| Layer | Where | What happens |
|---|---|---|
| Local RPM / RPD | `freeai_try_reserve` (Postgres, atomic) | Provider at its `rpm_limit` / `rpd_limit` is skipped without a call. |
| Daily tokens | `tpd_limit` vs `tokens_today` | Provider over its daily token budget is skipped without a call. |
| Response headers | `x-ratelimit-remaining-*` on successful calls | `remaining = 0` cools the model (or the whole key) until the reported reset, so the *next* request goes elsewhere instead of earning a 429. |
| Upstream 429 / quota | error classification | Wait time comes from `Retry-After`, the `x-ratelimit-reset-*` headers or Gemini's `retryDelay`. |

Every catalog provider declares a `quota_scope`:

- `"model"` (Groq, Gemini, NVIDIA). Each model has its own bucket.
  A 429 or daily quota on one model cools **that model** and the provider's
  next model is tried. The provider is only parked once all its models are
  cooling.
- `"key"` (OpenRouter, Mistral, Cohere). One bucket per account.
  A 429 parks the whole provider and the next provider is tried. A 402 is
  always treated as per key.

Cool-downs are kept in memory per `(user, provider, model)`. Watch
`freeai_provider_model_cooldowns_total{provider,reason}`, where `reason` is
one of `unavailable`, `rate_limited`, `quota` or `headers`. When you add a
provider, set `quota_scope` from its rate-limit docs; if you are unsure,
leave the default `"key"`, which is the conservative choice.

## Update procedure

1. Check the catalog against the live model lists:

   ```bash
   # OpenRouter and NVIDIA lists are public; the rest need a key
   GROQ_API_KEY=... GEMINI_API_KEY=... MISTRAL_API_KEY=... \
   COHERE_API_KEY=... \
   python scripts/check_catalog.py
   ```

   It exits 1 if a catalog model no longer exists. For OpenRouter it also
   lists `:free` models the catalog doesn't include yet.
2. Edit `backend/app/providers/catalog.py`:
   - move dead models to `retired_models` and pick replacements;
   - re-check limits and the `free_tier` text against the provider's own
     rate-limit page, and update `verified`;
   - bump `CATALOG_VERSION` to today's date.
3. Run `python -m pytest backend/tests/test_provider_catalog.py`.
4. Deploy or restart. On boot, `ConfigRepository.sync_catalog()` sees the
   new version and:
   - overwrites catalog rows (default model, limits, weight, tags);
   - inserts rows for new providers;
   - clears per-user `default_model` overrides that point at a retired model;
   - inserts seed prices for models that have none (admin-edited prices are
     kept).

   No migration is needed. Admin edits to catalog rows are kept until the
   next version bump.

Re-run step 1 at least monthly. The script warns when the catalog is more
than 60 days old.

## Removing a provider

1. Delete its `CatalogProvider` entry and add it to `RETIRED_PROVIDERS`
   with the reason. Then remove the adapter, the registry entry, the
   `check_catalog.py` fetcher and the `PROVIDER_GUIDES` / `_pwProviderOrder`
   entries in the frontend.
2. Bump `CATALOG_VERSION`. On the next boot, `sync_catalog()` deletes the
   provider's row, its health stats and every user's stored key for it
   (`ON DELETE CASCADE`). Historical `usage_events` and `model_prices` rows
   are kept.

## Adding a provider

1. If it speaks the OpenAI wire format, subclass `OpenAICompatibleProvider`
   with `name` and `BASE_URL` (see `nvidia_provider.py`). Otherwise
   implement `complete()` / `stream()` and raise errors through
   `_raise_for_status`, so they are classified the same way as every other
   provider.
2. Register it in `providers/__init__.py`, add a `CatalogProvider` entry
   (with the right `quota_scope`),
   and add a fetcher in `scripts/check_catalog.py`.
3. Add a `PROVIDER_GUIDES` entry and a slot in `_pwProviderOrder` in
   `frontend/app.js`.
4. If its keys have a recognisable prefix, add it to `_SECRET_PATTERNS` in
   `providers/base.py`.
5. Bump `CATALOG_VERSION`.
