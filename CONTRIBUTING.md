# Contributing to FreeAI

Thanks for taking a look. FreeAI is small and opinionated — contributions
are welcome, but please read this before opening a PR so we can avoid
churn.

## Before you start

- **File an issue first** for anything bigger than a typo or an obvious
  bug fix. It takes 5 minutes to check whether I'd merge the direction
  before you spend a weekend on it.
- **Read [docs/REVIEW.md](docs/REVIEW.md)** — it lists the design
  decisions that are load-bearing and shouldn't be casually refactored.
- **Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** if you're
  touching the orchestrator, the reservation function, or the repository
  layer.
- **Security issues**: do *not* open a public issue. See
  [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/adrianpastora/FREEAI.git
cd FREEAI

# Option A — full stack via Docker (easiest)
docker compose up --build
# optional: cp backend/.env.example .env && python scripts/ensure_dotenv.py

# Option B — Python locally, Postgres via compose
python scripts/ensure_dotenv.py
docker compose up postgres -d
cd backend
pip install -r requirements.txt -r requirements-dev.txt
export FREEAI_DATABASE_URL="postgresql+asyncpg://freeai:$(grep POSTGRES_PASSWORD ../.env | cut -d= -f2)@localhost:5444/freeai"
export FREEAI_MASTER_KEY=devkey
python run.py
```

More detail in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Running the tests

```bash
cd backend
pytest             # full suite — Postgres via testcontainers (needs Docker)
pytest tests/test_crypto.py tests/test_auto_strategy.py \
       tests/test_known_models.py tests/test_schema_tool_calls.py
                   # ~42 pure tests, no Docker needed
```

PRs that touch runtime code should add or update tests. If you're
changing behaviour that's covered only by integration tests, please run
the full suite locally before opening the PR.

## Style and conventions

- **Python**: PEP 8 with a soft 100-char line limit. No formatter is
  enforced — match the surrounding code.
- **JavaScript / HTML / CSS**: vanilla, no build step. Keep it that way.
- **Commit messages**: imperative mood, one-line summary, then a blank
  line and detail. Prefixes (`fix:`, `feat:`, `security:`, `docs:`,
  `ui:`) are used but not mandatory.
- **Docstrings**: public-ish entry points (FastAPI handlers, repository
  methods, providers) have them. Helpers don't need them if the name
  is obvious.
- **Comments**: explain *why*, not *what*. If the name of the function
  explains the what, skip it.

## Adding a provider

See [docs/DEVELOPMENT.md § 3](docs/DEVELOPMENT.md#3-adding-a-provider).
High-level: subclass `BaseProvider`, register in `PROVIDER_REGISTRY`,
add a card to the provider catalog, write integration tests under
`backend/tests/test_providers_*`.

## Adding a strategy

Strategies are data, not code. Create one from the UI. If you want the
new strategy to ship with the repo, add it to `seed_builtins_if_missing`
in [backend/app/repositories/strategy_repo.py](backend/app/repositories/strategy_repo.py).

## PR checklist

- [ ] `pytest` passes locally
- [ ] New behaviour covered by at least one test
- [ ] Docs updated if you changed a public endpoint or the config schema
- [ ] No `console.log` / `print()` left behind
- [ ] No Spanish strings in user-facing text (the project is
      English-only; the setup wizard was translated in a previous pass)
- [ ] No secrets in commits (including in test fixtures)

## What I'm unlikely to merge

- A framework migration (React, Vue, Svelte) for the frontend.
- Switching the primary database away from Postgres.
- Replacing `structlog` / the repository pattern / testcontainers with
  an alternative stack "because it's nicer".
- PRs that add configuration knobs without a concrete use case.

These aren't dogmatic positions — I've explained the reasoning in
[REVIEW.md § 3](docs/REVIEW.md#3-design-decisions-worth-preserving).
If you have a strong argument for one of them, open an issue and we'll
talk. Just don't expect the PR to merge without that conversation.
