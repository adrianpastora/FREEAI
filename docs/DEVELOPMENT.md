# Development guide

> How to run tests, add a provider, add a strategy, work on the frontend,
> and follow the project's conventions when making changes.

## 1. Quick start

```bash
# Clone, get into the repo
cd FreeAI

# One-shot bring-up (Postgres + FreeAI)
docker compose up --build

# Or, for local Python dev with auto-reload (needs Postgres reachable from the host)
cd backend
pip install -r requirements.txt -r requirements-dev.txt
# Postgres is bound to 127.0.0.1:5444 by docker-compose, so either use that
# port locally or run a native Postgres on :5432 and point FREEAI_DATABASE_URL
# at it.
export FREEAI_DATABASE_URL="postgresql+asyncpg://freeai:freeai@localhost:5444/freeai"
export FREEAI_MASTER_KEY=devkey
export FREEAI_ADMIN_TOKEN=adm_devtoken
python run.py    # uvicorn with --reload
```

The frontend is served by the same FastAPI app — edit `frontend/*.{html,css,js}`
and refresh the browser. No build step, no watcher.

## 2. Tests

### 2.1 Layout

243 tests total in [backend/tests/](../backend/tests/). This list
highlights the main buckets rather than every file:

- **Pure** (no DB, no Docker): `test_auto_strategy.py`, `test_crypto.py`,
  `test_known_models.py`, `test_strategy_dsl.py`,
  `test_virtual_models.py`, `test_provider_robustness.py` (empty response
  / content filter / stream parsing), `test_orchestrator_retry_budget.py`
  (retry budget + circuit-breaker kwargs), `test_schema_tool_calls.py`
  (OpenAI-compatible payload shapes).
- **DB-backed** (need Postgres via testcontainers):
  `test_orchestrator.py`, `test_rate_repo.py`, `test_config_repo.py`,
  `test_usage_repo.py`, `test_usage_repo_rollup.py`,
  `test_strategy_repo.py`, `test_client_repo.py`,
  `test_client_rate_repo.py`, `test_main_endpoints.py`,
  `test_embeddings_providers.py`, `test_security_integration.py`,
  `test_setup_api.py`, `test_streaming.py`, `test_migration_0009.py`.

"Pure" means no database, no external calls, no Docker — fast loop for
iterating on providers, schemas, or scoring logic. Running them alone
takes a couple of seconds.

### 2.2 Running tests

```bash
cd backend

# Everything — needs Docker running (testcontainers spins up postgres)
pytest

# Only the pure tests — no Docker needed
pytest tests/test_auto_strategy.py tests/test_crypto.py tests/test_known_models.py

# One file, verbose
pytest tests/test_orchestrator.py -v

# One test
pytest tests/test_rate_repo.py::test_concurrent_reservations_respect_limit -v

# With coverage
pytest --cov=app --cov-report=term-missing
```

### 2.3 How the DB-backed tests work

[conftest.py](../backend/tests/conftest.py):

1. On the first DB-requiring test, `_start_test_postgres()` spins a
   `testcontainers.PostgresContainer` (pulls `postgres:16-alpine` from
   Docker Hub once). The URL is stuck into `FREEAI_DATABASE_URL`.
2. A session-scoped fixture runs `alembic upgrade head` against it once.
3. Per-test fixtures use `TRUNCATE … RESTART IDENTITY CASCADE` between tests
   — much faster than recreating the schema or throwing away the container.
4. If Docker isn't running, the pytest fixture does `pytest.skip(...)` with a
   clear error, so pure tests still run.

**CI with a pre-existing Postgres**: set `FREEAI_TEST_DATABASE_URL` to skip
the container start and use your own instance (must be empty / dedicated).

```bash
FREEAI_TEST_DATABASE_URL="postgresql+asyncpg://runner:runner@postgres:5432/freeai_test" pytest
```

### 2.4 Writing new tests

- DB-backed tests take a `session: AsyncSession` fixture (one per test,
  auto-committed). Use `seeded_session` if you want the default providers
  already inserted.
- Use `pytest.mark.asyncio` (we have `asyncio_mode = auto` in `pytest.ini`
  so you often can skip the decorator, but being explicit is fine).
- Faking providers: see `FakeProvider` in
  [test_orchestrator.py](../backend/tests/test_orchestrator.py). Patch
  `Orchestrator._build_provider` to return fakes by name instead of
  constructing real adapters.
- Don't hit real provider APIs in tests. The adapters have no `respx`/httpx
  mocks today — if you add adapter-level tests, use `respx` (it's in
  `requirements.txt`).

## 3. Adding a provider

This is the most common extension. The work splits into four steps, in order:

### 3.1 Write the adapter

If the provider speaks OpenAI-compatible wire format (`/v1/chat/completions`
with `messages`, `temperature`, `stream`, `choices[0].message.content`), you're
lucky — subclass [OpenAICompatibleProvider](../backend/app/providers/openai_compat.py)
and you're done:

```python
# backend/app/providers/together_provider.py
from .openai_compat import OpenAICompatibleProvider

class TogetherProvider(OpenAICompatibleProvider):
    name = "together"
    BASE_URL = "https://api.together.xyz/v1/chat/completions"
    supports_vision = True   # set True if the provider accepts image_url content blocks
    request_timeout = 60.0
```

If it needs extra headers (like OpenRouter wants a Referer), override
`_extra_headers`. If its auth is different, override `_auth_headers`.

**Vision support:** Set `supports_vision = True` on providers that accept
`image_url` content blocks. OpenAI-compatible providers pass multimodal
content as-is. For non-OpenAI providers (like Gemini), implement the
translation from `image_url` to the provider's native image format in
the adapter.

If the wire format is **not** OpenAI-compatible (like Gemini or Cohere), look
at [gemini_provider.py](../backend/app/providers/gemini_provider.py) or
[cohere_provider.py](../backend/app/providers/cohere_provider.py) for the
pattern: inherit `BaseProvider` directly, implement `complete()` and
optionally `stream()`, use `self._raise_for_status(resp)` for error mapping.

### 3.2 Register it

In [backend/app/providers/\_\_init\_\_.py](../backend/app/providers/__init__.py):

```python
from .together_provider import TogetherProvider

PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {
    # existing …
    "together": TogetherProvider,
}
```

### 3.3 Seed a default row

In [backend/app/repositories/config_repo.py](../backend/app/repositories/config_repo.py)
add to `DEFAULT_PROVIDERS`:

```python
"together": ProviderConfigDTO(
    name="together",
    rpm_limit=60,
    rpd_limit=1000,
    weight=0.7,
    tags=["fast", "variety"],
    default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
),
```

The tags must match the strategy tag vocabulary (see § 4 below).

### 3.4 Add known models

In [backend/app/providers/known_models.py](../backend/app/providers/known_models.py):

```python
KNOWN_MODELS["together"] = [
    KnownModel("meta-llama/Llama-3.3-70B-Instruct-Turbo-Free", 131000, ["chat"]),
    KnownModel("deepseek-ai/DeepSeek-V3", 64000, ["chat", "reasoning"]),
]
```

The test [test_known_models_populated](../backend/tests/test_known_models.py)
enforces that every provider in the registry has at least one known model —
the CI will yell at you if you skip this step.

### 3.5 Optional: env var seeding

If you want `TOGETHER_API_KEY` to be picked up on first startup, add it to
[backend/app/settings.py](../backend/app/settings.py) (as a new `Optional[str]`
field) and to `provider_env_keys`.

### 3.6 Test it

```bash
# unit-level
pytest tests/test_known_models.py -v

# manually
export TOGETHER_API_KEY=...
python run.py
# UI → Providers tab → together should appear, drop in a key
# Playground → force provider = together → run a prompt
```

## 4. Adding a strategy

Two flavors: **built-in** (in code, seeded at startup, can't be deleted from
the UI) and **custom** (created entirely at runtime via the API).

### 4.1 Built-in

Edit [backend/app/repositories/strategy_repo.py](../backend/app/repositories/strategy_repo.py):

```python
BUILTIN_STRATEGIES: list[StrategyDTO] = [
    # existing …
    StrategyDTO(
        "rag",
        ["rag", "quality"],
        "Prefer RAG-tuned models with good reasoning",
        is_builtin=True,
    ),
]
```

`seed_builtins_if_missing()` is idempotent — existing DBs will get the new
row on next startup.

### 4.2 Custom (no code change needed)

```bash
curl -X POST http://localhost:8000/api/strategies \
  -H "X-Admin-Token: adm_…" \
  -H "Content-Type: application/json" \
  -d '{"name": "mine", "tags": ["coding", "fast"], "description": "my thing"}'
```

Or from the **Strategy** tab in the UI.

### 4.3 The tag vocabulary

Strategies reference tags that providers advertise. They're plain strings —
no enum — so there's nothing stopping you from making up new ones, but any
strategy referencing a tag that no provider has will rank every provider by
weight alone.

Current tag usage (from `DEFAULT_PROVIDERS`):

| Tag | Providers with it |
|---|---|
| `fast` | groq, mistral, cohere, huggingface, gemini (implicit via `long_context`) |
| `cheap` | groq, mistral, huggingface |
| `quality` | gemini, openrouter, cohere |
| `coding` | groq, mistral |
| `reasoning` | groq, gemini, openrouter |
| `vision` | gemini |
| `long_context` | gemini |
| `rag` | cohere |
| `variety` | openrouter, huggingface |

If you add a new tag, either:

- add it to an existing provider's `tags` array (via `PATCH /api/providers/{name}`
  or by editing `DEFAULT_PROVIDERS`) so the strategy has something to score,
- or expect the strategy to be a no-op until a future provider claims it.

## 5. Database changes

### 5.1 New migration

```bash
cd backend
alembic revision -m "add_something"
# edit alembic/versions/NNNN_add_something.py
alembic upgrade head
```

Autogeneration is supported but not trusted — **always review the generated
file**. Postgres-specific features (array defaults, `EXTRACT(EPOCH FROM
NOW())` server defaults, plpgsql functions) often need manual SQL. Look at
[0001_initial_schema.py](../backend/alembic/versions/0001_initial_schema.py)
for the pattern.

### 5.2 Updating an ORM model

If you add / remove / rename a column, you need **both**:

1. Update the model in [backend/app/db/models.py](../backend/app/db/models.py).
2. Write a migration. Don't just change the model — the old DBs will be out
   of sync with the code.

### 5.3 Adding a new repository method

The repository pattern here has one unusual rule: **return DTOs, not ORM
rows**. The reason: the session lifetime is tied to one HTTP request. If a
repository returned ORM rows, the caller would be holding objects whose
session might close out from under them. DTOs decouple us.

```python
@dataclass
class FoobarDTO:
    id: int
    name: str

class FoobarRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, id: int) -> Optional[FoobarDTO]:
        row = await self.session.get(FoobarRow, id)
        return self._to_dto(row) if row else None

    @staticmethod
    def _to_dto(row: FoobarRow) -> FoobarDTO:
        return FoobarDTO(id=row.id, name=row.name)
```

Always add the new repo to
[backend/app/repositories/\_\_init\_\_.py](../backend/app/repositories/__init__.py).

## 6. The frontend

Stack: **vanilla HTML + CSS + JS, no build step, no framework**. Three files:

- [frontend/index.html](../frontend/index.html) — structure
- [frontend/styles.css](../frontend/styles.css) — brutalist-control-room theme
- [frontend/app.js](../frontend/app.js) — tab switcher, admin auth, API calls,
  analytics charts (SVG, no Chart.js)

### 6.1 Conventions

- **Aesthetic is not optional.** The design choices (JetBrains Mono + Fraunces,
  bone/charcoal/amber palette, hard shadows, ASCII corners) are the product's
  personality. If you add new UI, match the existing components — look at
  `.provider-card`, `.strategy-card`, `.chart-card` for the playbook.
- **No dependencies.** Fonts come from Google Fonts (the two links at the top
  of `index.html`). Anything else is a dealbreaker.
- **SVG over Canvas** for charts — easier to inspect, style with CSS, and
  match the rest of the aesthetic.
- **State lives in closures**, not in a state container. At this size a
  component library's value is less than its cognitive cost.
- `adminApi(path, opts)` wraps admin requests with `X-Admin-Token` and 401
  handling. Use it for anything touching `/api/*`. For `/v1/*` (playground)
  call `fetch()` directly.

### 6.2 Adding a tab

Add an entry to the `.ribbon` in `index.html`, a `<section class="panel"
data-panel="...">` body, wire it up in the tab switcher block near the top
of `app.js`. If the tab loads data on demand, call its refresh function from
the `data-tab` handler — see how Clients, Analytics and Strategy work.

### 6.3 Testing the frontend

There are no automated frontend tests. The flow for verifying changes:

1. `docker compose up` (or `python run.py`)
2. Open `http://localhost:8000`
3. Enter the admin token (copy from logs)
4. Click through every tab, especially the one you changed
5. Hit the Playground with `stream: true` to exercise SSE if you touched the
   streaming path
6. Refresh with devtools open — look for console errors and failed requests

## 7. Coding conventions

- **Type annotations on everything.** The code is written expecting `mypy`
  or Pyright if you run them, though CI doesn't enforce it yet.
- **Docstrings explain *why*, not *what*.** The "what" is the code. The
  "why" (tradeoffs, gotchas, invariants) is what future you will forget.
  The best docstrings in this repo are in `rate_tracker.py` (deprecated),
  `orchestrator.py` and `rate_repo.py` — read those for the style.
- **Errors must be typed.** Never `raise Exception(...)` or bare `raise`
  outside tests. If it's a provider-level issue, `ProviderError(kind=...)`.
  If it's an HTTP-level issue, `HTTPException(status_code=...)`.
- **Logs must be structured.** `log.info("event_name", key=value)` not
  f-strings. The `event` name is what you'll grep for.
- **No print statements.** They don't land in the JSON log output.
- **No emojis in code or docs** unless the user explicitly asks.
- **Prefer editing to creating.** Don't add new files for things that fit in
  an existing one.

## 8. Commit conventions

No hard-enforced format. Keep the first line ≤72 chars and start with a verb
(`add`, `fix`, `refactor`, `remove`). If the change is non-obvious, write a
body explaining *why*. Commit messages show up forever in `git blame`, and
code review + memory is cheap — take the 30 seconds.

Examples from the project history that went well:

```
fix rate_repo.snapshot ignoring expired quarantine

The `and stats.healthy` at the end of `healthy = healthy and stats.healthy`
was shadowing the just-computed quarantine-expired case, so a provider that
had been quarantined and should now be back up looked dead forever.
```

```
add streaming support to gemini provider

Gemini doesn't speak OpenAI-style SSE — it has its own streamGenerateContent
endpoint with ?alt=sse. Implemented a separate stream() rather than trying
to shoehorn it into OpenAICompatibleProvider.
```

## 9. Project file map at a glance

```
FreeAI/
├── docs/
│   ├── ARCHITECTURE.md      (layering, flows, decisions)
│   ├── API.md               (endpoint reference)
│   ├── DATABASE.md          (schema, migrations, plpgsql function)
│   ├── OPERATIONS.md        (deploy, observe, back up)
│   ├── DEVELOPMENT.md       (this file)
│   └── REVIEW.md            (critical analysis + improvement backlog)
├── backend/
│   ├── Dockerfile
│   ├── alembic.ini
│   ├── pytest.ini
│   ├── requirements.txt
│   ├── run.py               (uvicorn launcher)
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/        (migrations)
│   ├── app/                 (the FastAPI app, 18 modules)
│   └── tests/               (243 pytest tests)
├── frontend/                (3 files, no build)
├── deploy/                  (prometheus + grafana provisioning)
├── docker-compose.yml
└── README.md                (landing page, links into docs/)
```
