# Strategy DSL — design doc

> Status: **shipped** (commits 1–4 in `main`, finished 2026-04-12).
> Author: collaborative session, 2026-04-12.
> Replaces: ad-hoc tag matching in `orchestrator._score()`.

## Why (historical)

Before this rework, a strategy was `{name, tags: [str], description}`.
The orchestrator matched strategy tags against provider tags with
literal string equality. A user who created a strategy `creative` with
tags `["creative", "poetic"]` got a strategy that **looked** custom but
did nothing — no provider had those tags, so the scoring loop added
zero points and the ranking degraded to `weight + headroom + latency_bonus`.

The UI didn't tell the user this. The text-input editor accepted any
string. The result was a feature that was silently a no-op for any tag
the user invented.

## Goal

Replace the tag list with a small declarative DSL where every field has
a defined effect on the routing decision. A strategy that compiles is a
strategy that does something. A strategy whose conditions match nothing
is rejected at save time, not silently ignored.

## Non-goals

- Not a Turing-complete language. No loops, no functions, no variables.
- No nested boolean logic (`AND`/`OR`/`NOT`). Conditions are an implicit
  AND inside `require`. If you need an OR, you split into two strategies.
- No prompt inspection. `auto` stays as a hardcoded special case that
  inspects the message and picks one of the data-driven strategies.
- No replacement of `weight`/`rpd_limit`/`enabled` on providers. Those
  stay where they are; the DSL **reads** them.

## Schema

Stored in `strategies.definition` as a single JSONB column. The
`tags` column is dropped.

```jsonc
{
  "require": [           // hard filter — providers that fail are excluded
    { "field": "tags", "op": "contains", "value": "coding" }
  ],
  "prefer": [            // soft scoring — adds points to candidates that match
    { "field": "tags",            "op": "contains", "value": "fast",   "weight": 5 },
    { "field": "last_latency_ms", "op": "<",        "value": 1000,     "weight": 3 },
    { "field": "rpd_remaining",   "op": ">",        "value": 0.5,      "weight": 2 }
  ]
}
```

A strategy with empty `require` and empty `prefer` ranks providers by
the existing `weight + headroom + latency_bonus` baseline — same as
today's "no matching tags" case, but **explicit**.

### Fields available

These are the only field names the DSL recognizes. Anything else is a
parse error at save time.

| Field             | Type      | Source                                               | Notes                                                  |
|-------------------|-----------|------------------------------------------------------|--------------------------------------------------------|
| `tags`            | string[]  | `provider_configs.tags`                              | Use with `op: contains`                                |
| `name`            | string    | `provider_configs.name`                              | Use with `op: ==` / `!=`                               |
| `weight`          | float     | `provider_configs.weight`                            | Hand-tuned admin priority                              |
| `enabled`         | bool      | `provider_configs.enabled`                           | Always true here — disabled providers are pre-filtered |
| `last_latency_ms` | int       | `ProviderSnapshot.last_latency_ms`                   | Single most recent observed call                       |
| `latency_ema_ms`  | float     | `ProviderSnapshot.latency_ema_ms`                    | **New.** Exponential moving average (alpha=0.3). More stable than `last_latency_ms` — prefer this for latency-based rules |
| `requests_today`  | int       | `ProviderSnapshot.requests_today`                    |                                                        |
| `requests_this_minute` | int  | `ProviderSnapshot.requests_this_minute`              |                                                        |
| `rpd_remaining`   | float     | derived: `1 - requests_today / rpd_limit` (0..1)     | Convenience field, normalized                          |
| `rpm_remaining`   | float     | derived: `1 - requests_this_minute / rpm_limit`      | Convenience field, normalized                          |
| `total_failures`  | int       | `ProviderSnapshot.total_failures`                    |                                                        |

### Operators

| Op           | Allowed on             | Meaning                              |
|--------------|------------------------|--------------------------------------|
| `contains`   | string[] only          | array contains the value             |
| `==`, `!=`   | scalar (string/number) | equality                             |
| `<`, `<=`    | numeric                | less than                            |
| `>`, `>=`    | numeric                | greater than                         |
| `in`         | string                 | value is in a provided list          |

That's it. No regex, no glob, no arithmetic. Anything beyond this is a
parse error.

### Scoring formula

```
score(provider, strategy):
    if any require fails:
        return -infinity   # excluded
    s = baseline_score(provider)
    for clause in prefer:
        if clause matches:
            s += clause.weight
    s -= 0.5 * in_flight_requests    # concurrency penalty
    return s

baseline_score(provider):
    s  = weight                       # admin priority (0.0–2.0)
    s += rpd_remaining * 1.5          # request headroom (0..1.5)
    s += tpd_remaining * 2.0          # token headroom (0..2.0, highest weight)
    # latency bonus uses EMA when available, falls back to single sample
    latency = latency_ema_ms or last_latency_ms
    if latency < 500ms:    s += 2.0
    elif latency < 1000ms: s += 1.2
    elif latency < 2000ms: s += 0.4
    else:                  s -= 1.0
    # reliability penalty
    s -= 0.1 * min(total_failures, 20)   # cap at -2.0
    return s
```

The baseline gives roughly equal weight to capacity, latency, and reliability.
In-flight concurrency is penalized to spread load across providers under
burst traffic. Token headroom is the highest-weighted factor because
exhausting tokens causes hard quarantine.

### Validation rules (enforced at save time)

A strategy is rejected with a structured error if:

1. **Unknown field** in `require` or `prefer`. Error names the field and
   lists allowed fields.
2. **Unknown operator**, or operator that doesn't match the field type
   (e.g. `last_latency_ms contains 800`).
3. **`weight` missing or non-numeric** in any `prefer` clause.
4. **`require` matches zero providers** at save time. Hard error: a
   strategy you can't possibly route is a bug, not a config. The error
   tells the user *which* require clause kills the candidate set.
5. **`prefer` references a tag that no provider has** — soft warning, not
   an error. The strategy is saved, but the response includes a
   `warnings: [...]` field so the UI can show "this clause never fires
   today, but will if you add a provider with that tag later".

### Example: rewriting today's built-ins

```yaml
# fastest
require: []
prefer:
  - { field: tags, op: contains, value: fast, weight: 5 }
  - { field: last_latency_ms, op: "<", value: 1000, weight: 3 }

# coding
require:
  - { field: tags, op: contains, value: coding }
prefer:
  - { field: tags, op: contains, value: reasoning, weight: 5 }
  - { field: last_latency_ms, op: "<", value: 2000, weight: 2 }

# vision
require:
  - { field: tags, op: contains, value: vision }
prefer: []

# best_quality
require: []
prefer:
  - { field: tags, op: contains, value: quality, weight: 5 }
  - { field: tags, op: contains, value: reasoning, weight: 4 }

# cheapest
require: []
prefer:
  - { field: tags, op: contains, value: cheap, weight: 5 }
  - { field: rpd_remaining, op: ">", value: 0.5, weight: 3 }

# long_context
require:
  - { field: tags, op: contains, value: long_context }
prefer: []

# reasoning
require: []
prefer:
  - { field: tags, op: contains, value: reasoning, weight: 5 }
  - { field: tags, op: contains, value: quality, weight: 3 }

# auto — special case, NOT in the DSL.
# auto.definition is null. The orchestrator detects auto by name and runs
# detect_auto_strategy(messages) to pick one of the above by signal.
```

## Migration plan

### Schema change

Alembic migration `0008_strategy_dsl.py`:

1. Add column `strategies.definition jsonb null`.
2. For each existing row with `is_builtin=true`, set `definition` to the
   precomputed JSON above. (`auto` gets `null` and is excluded from the
   evaluator.)
3. For each user-created row, write `definition = {require: [], prefer: [{field: "tags", op: "contains", value: t, weight: 5} for t in tags]}`.
   Lossless reinterpretation of the old data.
4. Drop column `strategies.tags` in the **same migration** — no parallel
   universes.
5. Downgrade is best-effort: re-add `tags` column from `prefer` clauses
   that match the old shape, leave `definition` behind. Documented as
   one-way for new strategies that use non-tag fields.

### Code change

| Layer        | What changes                                                            |
|--------------|-------------------------------------------------------------------------|
| `db/models`  | `StrategyRow.tags` → `StrategyRow.definition` (jsonb)                   |
| `repositories/strategy_repo` | DTO swaps `tags: list[str]` for `definition: dict`. `BUILTIN_STRATEGIES` rewritten in the new shape. |
| `app/strategy_dsl.py` (new)  | Parser + validator + evaluator. ~150 LOC. Pure functions, no DB. Fully unit-tested without Postgres. |
| `app/orchestrator._score`    | Replace tag loop with `dsl.score(provider, snapshot, definition)`. `_resolve_strategy` returns `definition` instead of `tags`. |
| `app/main.py`                | `POST/PATCH /api/strategies` accepts `definition` instead of `tags`. Validation errors return 422 with the structured error from the parser. |
| `auto_strategy.py`           | Unchanged. Still picks a strategy *name* by inspecting the prompt.    |
| `frontend/`                  | Strategy editor becomes a form builder. `app.js` strategy card shows the rules instead of tag chips. |

### Test plan

New tests, all pure (no DB needed):

- `test_strategy_dsl_parser.py` — every validation rule above, one test each. ~20 tests.
- `test_strategy_dsl_evaluator.py` — score formula, require excludes, prefer adds, edge cases (all-zero, missing field on snapshot). ~15 tests.

Existing tests that need updating:

- `test_strategy_repo.py` — replace `tags=["fast"]` with `definition={...}` in fixtures.
- `test_orchestrator.py::test_custom_strategy_is_accepted_and_used` — same fixture swap.
- `test_main_endpoints.py` — strategy CRUD assertions.

Rough impact: 6 existing test files touched, 2 new files added, total
delta ~250 LOC of test code.

## Frontend — form builder

The strategy editor modal becomes a structured form, not a textarea.

```
┌─ NEW STRATEGY ─────────────────────────────────────┐
│  NAME       [my_coding              ]              │
│  DESCRIPTION[Tuned for code in EU peak hours]     │
│                                                    │
│  REQUIRE  (provider must satisfy ALL)             │
│  ┌────────────────────────────────────────────┐   │
│  │ FIELD [tags ▾]  OP [contains ▾]  VALUE [coding ▾] │
│  │ FIELD [last_latency_ms ▾] OP [< ▾] VALUE [2000]   │
│  │                                       [+ add] │   │
│  └────────────────────────────────────────────┘   │
│                                                    │
│  PREFER  (each match adds points)                 │
│  ┌────────────────────────────────────────────┐   │
│  │ FIELD [tags ▾] OP [contains ▾] VALUE [fast ▾] WEIGHT [5] │
│  │                                       [+ add] │   │
│  └────────────────────────────────────────────┘   │
│                                                    │
│  ┌─ live preview: 3 providers match ──────────┐   │
│  │ groq      score 8.5  ▓▓▓▓▓▓▓▓▓             │   │
│  │ mistral   score 7.0  ▓▓▓▓▓▓▓               │   │
│  │ cohere    score 4.5  ▓▓▓▓                  │   │
│  └────────────────────────────────────────────┘   │
│                                                    │
│            [CANCEL]      [SAVE STRATEGY]           │
└────────────────────────────────────────────────────┘
```

Key design points:

- **Value dropdowns are populated by `GET /api/tags`** for `field=tags`,
  `GET /api/providers` for `field=name`, plain number input for numeric
  fields.
- **Live preview** is the killer feature. Calls a new endpoint
  `POST /api/strategies/preview` with the in-progress definition; the
  backend returns the candidate list as it would rank today, without
  saving. The user sees their strategy's effect *before* clicking save.
- **Save is blocked** if validation fails, with the field-level error
  inline next to the broken clause.

## API changes

| Endpoint                    | Before                                           | After                                                                        |
|----------------------------|--------------------------------------------------|------------------------------------------------------------------------------|
| `GET /api/strategies`      | `[{name, tags, description, is_builtin}]`        | `[{name, definition, description, is_builtin}]`                              |
| `POST /api/strategies`     | body: `{name, tags, description}`                | body: `{name, definition, description}` — 422 with structured error on invalid |
| `PATCH /api/strategies/:n` | body: `{tags?, description?}`                    | body: `{definition?, description?}`                                          |
| `DELETE /api/strategies/:n` | unchanged                                        | unchanged                                                                    |
| `POST /api/strategies/preview` | new                                          | body: `{definition}`, returns ranked provider list with scores               |
| `GET /api/tags`            | new                                              | returns `[{tag, providers: [name]}]` — vocabulary discovery for the editor   |

Breaking change: `tags` is gone from the strategy API. There is no
backwards-compat shim. Migration 0008 rewrites everything in the DB
in one go; clients that still send `tags` get a 422.

## Design decisions (resolved)

1. **`last_latency_ms`** — initially named `latency_p50_ms` in the design doc and the first three commits, but the implementation only reads `ProviderSnapshot.last_latency_ms` (the most recent observed call), not a windowed percentile. The misleading name was renamed in migration 0009 (2026-04-12). A real p50 over `usage_events` would need analytics-side aggregation and is intentionally out of scope until cost tracking lands — at that point the same aggregation infra produces both. New strategies should use `last_latency_ms`; old strategies stored as JSONB are rewritten in place by 0009.

2. **Live preview cost** — debounced to 300ms on the frontend, and the endpoint is admin-only. Not exposed to the public side.

3. **User-created strategies during migration** — migration 0008 rewrites every existing tag list as a `prefer.contains` per tag with weight 5, lossless. Editors that open them after the migration see the auto-converted clauses.

4. **`require` empty + `prefer` empty = baseline** — allowed. The strategy ranks providers by baseline only. Useful for users who want a named alias for the default behavior.

5. **`name`-based filters** — allowed. Documented foot-gun in this doc: per-provider strategies break when providers are renamed.

## What this does NOT solve

- **No conditional dispatch.** A strategy that says "use Gemini for vision and Groq for code" needs `auto` or two separate strategies, not a single one. The DSL is intentionally per-strategy filtering, not routing logic.
- **No prompt-aware filters.** You can't write `require: prompt_length > 4000`. The DSL only sees provider state, not the request. Prompt inspection stays inside `auto_strategy.py`.
- **No vocabulary management for tags.** Tags are still strings on `provider_configs.tags`. The DSL reads them but doesn't validate them at provider edit time. (Easy follow-up: enforce a vocabulary in the providers panel, but separate from this work.)

## Effort estimate

| Phase                                            | LOC delta (rough)         |
|--------------------------------------------------|---------------------------|
| `strategy_dsl.py` parser+validator+evaluator     | +250                      |
| Alembic 0008 + repo/DTO updates                  | +120                      |
| Orchestrator integration                         | -30 / +20                 |
| Built-in strategy rewrites                       | +60                       |
| `GET /api/tags`, `POST /api/strategies/preview`  | +60                       |
| Updated test fixtures + new tests                | +250                      |
| Frontend form builder + live preview             | +400                      |
| Total                                            | ~1100 LOC delta           |

Shipped as 4 mergeable commits, the suite stayed green at every step:

1. ✅ **DSL module + tests + design doc.** `4228c71` —
   `strategy_dsl.py` with 52 unit tests; not yet wired into anything.
2. ✅ **Migration 0008 + repo/DTO swap + orchestrator wiring.**
   `99fc2aa` — backend uses the DSL; the API accepts both `definition`
   and the legacy `tags` shape during this commit so the frontend can
   land in commit 3 without a breaking merge.
3. ✅ **Frontend form builder + `/api/tags` + `/api/strategies/preview`.**
   `fe9d003` — strategy editor becomes a structured form with live
   preview. Strategy cards render rules with REQ/PREF prefixes.
4. ✅ **Drop the legacy `tags` shim.** Final cleanup, single shape on
   the wire. `StrategyUpsertIn`/`StrategyOut` now expose `definition`
   only; `_resolve_definition` and `_derive_legacy_tags` are gone.

Final test count: 159 passing (up from 99 before the rework).
