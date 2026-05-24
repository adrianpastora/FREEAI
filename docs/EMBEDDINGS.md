# Embeddings

> OpenAI-compatible text embeddings behind the same fallback, rate-limiting,
> and analytics pipeline that powers `/v1/chat/completions`. See
> [docs/API.md § Embeddings](API.md#embeddings) for the full HTTP reference —
> this page covers the conceptual model, provider trade-offs, and operational
> guidance.

## Why it exists

Chat completions are only half of the typical LLM app. RAG pipelines,
semantic search, clustering, deduplication and recommendation systems all
depend on **embedding vectors** — dense numerical representations of text
that can be compared with cosine similarity.

`POST /v1/embeddings` covers embeddings with the same architectural
decisions used by chat:

- one OpenAI-compatible endpoint,
- multi-provider fallback with per-user quota enforcement,
- telemetry into `usage_events` (visible on the analytics dashboard under
  `strategy = "embedding"`).

## Supported providers

| Provider | Default model        | Dim  | OpenAI wire format | Free-tier limit (2026-05)                | Token usage reported |
|----------|----------------------|------|--------------------|------------------------------------------|----------------------|
| Mistral  | `mistral-embed`      | 1024 | ✅                  | ~1 RPS, ~1B tokens/month                 | ✅                    |
| Gemini   | `text-embedding-004` | 768  | ❌ (custom adapter) | 1500 RPM, 30k RPD                        | ❌ (always 0)         |

Default priority on fallback: **mistral → gemini**. Override per-request
with `preferred_provider`.

### ⚠ Only these model names are valid

The `model` field must match a model the chosen upstream provider actually
serves. FreeAI does **not** translate OpenAI-style model names
(`text-embedding-3-small`, `text-embedding-3-large`, `text-embedding-ada-002`)
into native ones — it passes them through as-is, and the upstream provider
replies with `400 invalid model`.

Safe choices:

```json
{"input": "hello"}                                         // uses mistral-embed (default)
{"input": "hello", "model": "mistral-embed"}               // explicit mistral
{"input": "hello", "model": "text-embedding-004", "preferred_provider": "gemini"}
```

This will **not** work even if you have an OpenAI key configured elsewhere:

```json
{"input": "hello", "model": "text-embedding-3-small"}      // → 400 Bad Request
```

If your client hardcodes OpenAI model names, either:
1. Omit the `model` field (recommended for new code), or
2. Map the OpenAI names to native ones in your client before calling FreeAI.

Why only these two today? They are the providers already integrated in the
catalog that expose embeddings on their free tier. Groq's free tier is
chat + audio only; Cohere has embeddings but its free trial quota (~33
embeds/day) is too restrictive to be useful as a fallback. OpenRouter
embeddings are free-credit-limited and not a stable default. Adding any
of these later is a matter of implementing `BaseProvider.embed()` on the
adapter — see **Adding a new embedding provider** below.

## The "same model" rule

Vectors from different models are **not comparable**, even if the
dimensionalities happen to match. Cosine similarity between a
`mistral-embed` vector and a `text-embedding-004` vector is noise.

Practical consequences:

1. **Tag every stored vector with its `model` + `provider`.** The response
   echoes both fields specifically so you can do this.
2. **Queries must embed with the same model used for ingest.** If you let
   the router fall back to a different provider mid-session, search
   quality collapses silently — the numbers look normal, the results are
   wrong.
3. **Switching providers means re-embedding your corpus.** Budget for
   this if you plan to migrate.

For production RAG, the safest pattern is to pin one provider+model via
`preferred_provider` and `model`, treat `fallback: false`, and surface
failures as explicit errors:

```python
resp = client.embeddings.create(
    model="mistral-embed",
    input=documents,
    extra_body={"preferred_provider": "mistral", "fallback": False},
)
```

The fallback chain is useful for loose use cases (ad-hoc semantic search,
exploratory clustering) where quality matters less than availability.

## Request / response

See [docs/API.md § Embeddings](API.md#embeddings) for the exact schema
and curl / OpenAI SDK examples. Quick shape:

```json
// request
{
  "input": ["first doc", "second doc"],
  "model": "mistral-embed",
  "preferred_provider": "mistral",
  "fallback": true
}

// response (OpenAI-compatible + FreeAI extensions)
{
  "object": "list",
  "data": [
    {"object": "embedding", "index": 0, "embedding": [...]},
    {"object": "embedding", "index": 1, "embedding": [...]}
  ],
  "model": "mistral-embed",
  "provider": "mistral",                   // FreeAI extension
  "usage": {"prompt_tokens": 14, "total_tokens": 14},
  "fallback_position": 1                   // FreeAI extension
}
```

The OpenAI Python SDK ignores unknown fields, so `.embeddings.create()`
works unchanged. The `provider` and `fallback_position` fields are there
for you to log / tag / alert on.

## How the fallback loop works

Simpler than chat because there's no streaming, no DSL scoring, and no
preferred-provider ranking based on live latency — embeddings are
one-shot, and the handful of providers means a fixed priority order is
good enough.

```
input list
    │
    ▼
 candidates = [mistral, gemini] ∩ user's configured providers with embedding support
    │           ^^ reshuffled if preferred_provider is set
    ▼
 for each candidate:
     reserve RPM/RPD quota  ── at capacity? skip ───────┐
         │                                              │
         ▼                                              │
     call provider.embed()                              │
         │                                              │
      ┌──┴──────────────┐                               │
      ▼                 ▼                               │
    success           failure                           │
      │                 │                               │
      │           record error_kind                     │
      │                 │                               │
      │        auth/client? ── yes ── break loop ───────┤
      │                 │                               │
      │                 └─ transient/429 ─ try next ────┤
      ▼                                                 │
  commit success,                                       │
  return response                                       │
                                                        │
                                                  all exhausted:
                                                  HTTP 5xx with attempts[]
```

Rate-limit reservation goes through `freeai_try_reserve` — the exact
same plpgsql function that protects chat traffic, so N concurrent
workers cannot cumulatively blow past a provider's free-tier RPM.

## Telemetry

Every call — success or failure — writes one row to `usage_events` with:

- `provider_name` — which adapter handled (or tried to handle) the call
- `model` — what the provider echoed back (for `success`); empty string otherwise
- `strategy` — the literal string `"embedding"`
- `outcome` — `"success"` or an error kind (`rate_limited`, `server_error`, …)
- `latency_ms` — full request duration
- `prompt_tokens` — reported by the provider (always 0 for Gemini embeddings)
- `completion_tokens` — always 0 for embeddings
- `fallback_position` — which candidate in the priority chain handled it

The analytics dashboard picks these up automatically:

- The **BY STRATEGY** chart shows `embedding` alongside `auto`, `fastest`, etc.
- The **ERRORS BY KIND** chart groups embedding failures by error type.
- The **BY MODEL** chart separates `mistral-embed` from `text-embedding-004`.
- The **HISTORICAL** section rolls up embeddings daily just like chat.

No new dashboard widget is needed — strategy-as-data makes this free.

## Adding a new embedding provider

Three steps:

1. **Implement `embed()`** on the provider adapter
   ([backend/app/providers/base.py](../backend/app/providers/base.py) defines
   the signature):

   ```python
   async def embed(
       self,
       texts: list[str],
       *,
       model: Optional[str] = None,
       client: httpx.AsyncClient,
   ) -> EmbeddingResult:
       ...
   ```

   Return an `EmbeddingResult` whose `vectors[i]` corresponds to `texts[i]`.
   If the upstream API doesn't report prompt tokens, leave
   `prompt_tokens=0` — don't fake it.

2. **Set `supports_embeddings = True`** on the provider class (same file
   pattern as `supports_streaming` / `supports_vision`).

3. **Register in the priority list** at
   [backend/app/embeddings.py](../backend/app/embeddings.py): append the
   provider name to `EMBEDDING_PROVIDERS`. Position defines fallback
   priority. Optionally add the `"embeddings"` tag to the provider's
   default entry in
   [backend/app/repositories/config_repo.py](../backend/app/repositories/config_repo.py)
   so the DSL can filter on it.

No schema migration required. No frontend change required. Tests for
the new adapter go in `backend/tests/test_embeddings_providers.py` —
that file documents the mocking pattern.

## Limits and known gotchas

- **Mistral free tier is 1 RPS.** Batch inputs whenever possible:
  `input` accepts a list of strings and the provider embeds all of them
  in one HTTP call, counted as **one** rate-limit token regardless of
  batch size.
- **Gemini doesn't report token usage for embeddings.** Analytics will
  show `prompt_tokens = 0` for all Gemini embedding traffic. This is an
  upstream limitation, not a bug — we don't want to estimate tokens and
  pretend they're authoritative.
- **No streaming.** Embeddings are always one-shot; the request body
  doesn't accept a `stream` field.
- **Input size limits vary per provider.** Mistral's max is ~32k tokens
  per request body; Gemini's `text-embedding-004` truncates inputs
  longer than 2048 tokens. For long documents, chunk before embedding.
- **Preferred provider is a hard filter, not a hint.** If
  `preferred_provider` isn't configured for the user, you get 400 — the
  request is *not* silently routed elsewhere. This mirrors how
  `preferred_provider` works in `/v1/chat/completions`.
