# Strategy DSL — design doc

> Status: **draft**, awaiting approval.
> Author: collaborative session, 2026-04-12.
> Replaces: ad-hoc tag matching in `orchestrator._score()`.

## Why

Today, a strategy is `{name, tags: [str], description}`. The orchestrator
matches strategy tags against provider tags with literal string equality.
A user who creates a strategy `creative` with tags `["creative", "poetic"]`
gets a strategy that **looks** custom but does nothing — no provider has
those tags, so the scoring loop adds zero points and the ranking degrades
to `weight + headroom + latency_bonus`.

The UI does not tell the user this. The text-input editor accepts any
string. The result is a feature that's silently a no-op for any tag the
user invents.

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
    { "field": "latency_p50_ms",  "op": "<",        "value": 1000,     "weight": 3 },
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
| `latency_p50_ms`  | int       | `ProviderSnapshot.last_latency_ms` (proxy for now)   | Real p50 needs analytics-side aggregation; v2          |
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
    s = baseline_score(provider)              # weight + headroom + latency_bonus, unchanged
    for clause in prefer:
        if clause matches:
            s += clause.weight
    return s
```

`baseline_score` is exactly today's `_score()` minus the tag loop. The
DSL replaces the tag loop, not the rest of the heuristic.

### Validation rules (enforced at save time)

A strategy is rejected with a structured error if:

1. **Unknown field** in `require` or `prefer`. Error names the field and
   lists allowed fields.
2. **Unknown operator**, or operator that doesn't match the field type
   (e.g. `latency_p50_ms contains 800`).
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
  - { field: latency_p50_ms, op: "<", value: 1000, weight: 3 }

# coding
require:
  - { field: tags, op: contains, value: coding }
prefer:
  - { field: tags, op: contains, value: reasoning, weight: 5 }
  - { field: latency_p50_ms, op: "<", value: 2000, weight: 2 }

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
│  │ FIELD [latency_p50_ms ▾] OP [< ▾] VALUE [2000]    │
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

## Open questions for review

1. **`latency_p50_ms`** — today the only source is `ProviderSnapshot.last_latency_ms`, which is the *single most recent* call's latency, not a real p50. For v1 we use it as a proxy and document the limitation. Real p50 requires aggregating `usage_events` and is a v2 task. Acceptable?

2. **Live preview cost** — `POST /api/strategies/preview` runs `_rank()` against the live snapshot. This is one extra ranking per editor keystroke if the UI is naive. Plan: debounce on the frontend (300ms), and the endpoint is admin-only so it's not abusable from the public side. Acceptable?

3. **What happens to user-created strategies during migration** — the rewrite-as-prefer-tags is lossless but produces strategies that score every matching provider with `weight: 5`, which is the same as the old behavior. Users don't need to do anything. But: if they later edit the strategy in the new UI, they'll see the auto-converted `prefer` clauses and may want to clean them up. Document in the migration notes.

4. **`require` empty + `prefer` empty = baseline** — explicitly allowed (today's `auto` lookalike: a strategy that just lets the baseline scoring run). Or do we disallow it as a "useless strategy"? Recommend allow.

5. **Should `name`-based filters (`field: name, op: ==, value: groq`) be allowed?** It's expressive but tempts users to write per-provider strategies that break when providers are renamed. Recommend allow but document the foot-gun.

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

This is real work. It is not a one-commit job. Plan: ship in 4 commits
that each leave the system green:

1. **DSL module + tests, no integration yet.** `strategy_dsl.py` exists,
   has 35 passing tests, nothing else uses it.
2. **Migration 0008 + repo/DTO swap + orchestrator wiring.** Backend now
   uses the DSL. Frontend still works because it sends `definition` once
   the next commit lands; in the meantime, the API accepts both shapes
   for one commit window.
3. **Frontend form builder.** Replaces the textarea editor.
4. **Drop the `tags` accept-also shim and the `GET /api/tags` +
   preview endpoint.** Final cleanup, single shape on the wire.

Each commit is mergeable on its own and the test suite stays green.
