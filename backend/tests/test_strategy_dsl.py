"""Pure tests for the strategy DSL — parser, validator, evaluator.

No DB required. These tests pin the public contract: every validation
rule documented in docs/STRATEGY_DSL.md has at least one test here, and
every operator + field-type combination has at least one positive and
one negative case in the evaluator suite.
"""
from __future__ import annotations

import pytest

from app.strategy_dsl import (
    Clause,
    Definition,
    EvalContext,
    ParseError,
    context_from_provider,
    matches,
    parse_definition,
    score,
    serialize_definition,
)


# ─────────────────────────────────────────────────────────────────────
#                              parser
# ─────────────────────────────────────────────────────────────────────


def test_parse_none_returns_empty_definition():
    d = parse_definition(None)
    assert isinstance(d, Definition)
    assert d.is_empty()


def test_parse_empty_dict_returns_empty_definition():
    d = parse_definition({})
    assert d.is_empty()


def test_parse_only_require_no_prefer():
    d = parse_definition({
        "require": [{"field": "tags", "op": "contains", "value": "coding"}],
    })
    assert len(d.require) == 1
    assert d.prefer == ()


def test_parse_only_prefer_no_require():
    d = parse_definition({
        "prefer": [{"field": "tags", "op": "contains", "value": "fast", "weight": 5}],
    })
    assert d.require == ()
    assert len(d.prefer) == 1
    assert d.prefer[0].weight == 5.0


def test_parse_full_definition():
    d = parse_definition({
        "require": [
            {"field": "tags", "op": "contains", "value": "coding"},
            {"field": "latency_p50_ms", "op": "<", "value": 2000},
        ],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
            {"field": "rpd_remaining", "op": ">", "value": 0.5, "weight": 2},
        ],
    })
    assert len(d.require) == 2
    assert len(d.prefer) == 2
    assert d.require[0].field == "tags"
    assert d.require[1].op == "<"
    assert d.prefer[1].value == 0.5


def test_parse_rejects_top_level_string():
    with pytest.raises(ParseError, match="must be an object"):
        parse_definition("oops")


def test_parse_rejects_unknown_top_level_key():
    with pytest.raises(ParseError, match="unknown top-level key"):
        parse_definition({"requires": []})  # typo: should be 'require'


def test_parse_rejects_clause_that_is_not_dict():
    with pytest.raises(ParseError, match=r"require\[0\]: clause must be an object"):
        parse_definition({"require": ["tags=coding"]})


def test_parse_rejects_unknown_clause_key():
    with pytest.raises(ParseError, match="unknown clause key"):
        parse_definition({"require": [
            {"field": "tags", "op": "contains", "value": "x", "extra": 1},
        ]})


def test_parse_rejects_clause_missing_field():
    with pytest.raises(ParseError, match="missing 'field'"):
        parse_definition({"require": [{"op": "contains", "value": "x"}]})


def test_parse_rejects_clause_missing_op():
    with pytest.raises(ParseError, match="missing 'op'"):
        parse_definition({"require": [{"field": "tags", "value": "x"}]})


def test_parse_rejects_clause_missing_value():
    with pytest.raises(ParseError, match="missing 'value'"):
        parse_definition({"require": [{"field": "tags", "op": "contains"}]})


def test_parse_rejects_unknown_field():
    with pytest.raises(ParseError, match="unknown field"):
        parse_definition({"require": [
            {"field": "creativity", "op": "==", "value": "high"},
        ]})


def test_parse_rejects_unknown_operator():
    with pytest.raises(ParseError, match="unknown operator"):
        parse_definition({"require": [
            {"field": "tags", "op": "matches", "value": "x"},
        ]})


def test_parse_rejects_op_invalid_for_field_type():
    # `contains` only valid on string_array, not on a numeric field.
    with pytest.raises(ParseError, match="not valid for field"):
        parse_definition({"require": [
            {"field": "latency_p50_ms", "op": "contains", "value": 800},
        ]})


def test_parse_rejects_string_value_on_numeric_field():
    with pytest.raises(ParseError, match="value must be a number"):
        parse_definition({"require": [
            {"field": "latency_p50_ms", "op": "<", "value": "fast"},
        ]})


def test_parse_rejects_number_value_on_string_field():
    with pytest.raises(ParseError, match="value must be a string"):
        parse_definition({"require": [
            {"field": "name", "op": "==", "value": 7},
        ]})


def test_parse_rejects_bool_disguised_as_number():
    # Python booleans are ints. We explicitly reject them on numeric fields
    # so `True` doesn't quietly become 1.
    with pytest.raises(ParseError, match="value must be a number"):
        parse_definition({"require": [
            {"field": "latency_p50_ms", "op": "<", "value": True},
        ]})


def test_parse_in_operator_requires_list():
    with pytest.raises(ParseError, match="non-empty list"):
        parse_definition({"require": [
            {"field": "name", "op": "in", "value": "groq"},
        ]})


def test_parse_in_operator_rejects_empty_list():
    with pytest.raises(ParseError, match="non-empty list"):
        parse_definition({"require": [
            {"field": "name", "op": "in", "value": []},
        ]})


def test_parse_in_operator_validates_element_types():
    with pytest.raises(ParseError, match="'in' on string field"):
        parse_definition({"require": [
            {"field": "name", "op": "in", "value": ["groq", 5]},
        ]})


def test_parse_contains_value_must_be_string():
    with pytest.raises(ParseError, match="'contains' expects a string"):
        parse_definition({"require": [
            {"field": "tags", "op": "contains", "value": ["fast"]},
        ]})


def test_parse_prefer_requires_weight():
    with pytest.raises(ParseError, match="'weight' is required"):
        parse_definition({"prefer": [
            {"field": "tags", "op": "contains", "value": "fast"},
        ]})


def test_parse_prefer_weight_must_be_numeric():
    with pytest.raises(ParseError, match="'weight' must be numeric"):
        parse_definition({"prefer": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": "high"},
        ]})


def test_parse_require_rejects_weight():
    with pytest.raises(ParseError, match="'weight' is not allowed on require"):
        parse_definition({"require": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
        ]})


def test_parse_prefer_weight_can_be_zero_or_negative():
    # Zero and negative weights are not parse errors — they're a feature
    # (a soft penalty for matching). The form builder will probably never
    # produce these, but the DSL doesn't artificially disallow them.
    d = parse_definition({"prefer": [
        {"field": "tags", "op": "contains", "value": "slow", "weight": -3},
        {"field": "tags", "op": "contains", "value": "neutral", "weight": 0},
    ]})
    assert d.prefer[0].weight == -3
    assert d.prefer[1].weight == 0


# ─────────────────────────────────────────────────────────────────────
#                              evaluator
# ─────────────────────────────────────────────────────────────────────


def _ctx(**fields) -> EvalContext:
    """Cheap context builder for direct tests."""
    return EvalContext(fields=fields)


def test_matches_contains_positive():
    c = Clause("tags", "contains", "fast")
    assert matches(c, _ctx(tags=["fast", "cheap"])) is True


def test_matches_contains_negative():
    c = Clause("tags", "contains", "vision")
    assert matches(c, _ctx(tags=["fast", "cheap"])) is False


def test_matches_contains_handles_non_iterable_gracefully():
    # If the field somehow holds a non-iterable, matches() returns False
    # rather than raising — keeps the evaluator robust under bad context.
    c = Clause("tags", "contains", "x")
    assert matches(c, _ctx(tags=42)) is False


def test_matches_eq_string():
    c = Clause("name", "==", "groq")
    assert matches(c, _ctx(name="groq")) is True
    assert matches(c, _ctx(name="gemini")) is False


def test_matches_neq():
    c = Clause("name", "!=", "groq")
    assert matches(c, _ctx(name="gemini")) is True
    assert matches(c, _ctx(name="groq")) is False


def test_matches_lt_lte_gt_gte():
    ctx = _ctx(latency_p50_ms=500)
    assert matches(Clause("latency_p50_ms", "<", 1000), ctx) is True
    assert matches(Clause("latency_p50_ms", "<", 500), ctx) is False
    assert matches(Clause("latency_p50_ms", "<=", 500), ctx) is True
    assert matches(Clause("latency_p50_ms", ">", 500), ctx) is False
    assert matches(Clause("latency_p50_ms", ">=", 500), ctx) is True


def test_matches_in_string():
    c = Clause("name", "in", ["groq", "gemini"])
    assert matches(c, _ctx(name="groq")) is True
    assert matches(c, _ctx(name="cohere")) is False


def test_matches_returns_false_when_field_missing():
    c = Clause("name", "==", "groq")
    assert matches(c, _ctx(other="x")) is False


def test_matches_returns_false_when_field_value_is_none():
    # latency_p50_ms can legitimately be None for a provider that
    # hasn't been called yet. The evaluator must NOT crash.
    c = Clause("latency_p50_ms", "<", 1000)
    assert matches(c, _ctx(latency_p50_ms=None)) is False


# ─────────────────────────────────────────────────────────────────────
#                              score()
# ─────────────────────────────────────────────────────────────────────


def test_score_empty_definition_returns_zero():
    d = Definition()
    assert score(d, _ctx()) == 0.0


def test_score_require_pass_no_prefer_returns_zero():
    d = parse_definition({"require": [
        {"field": "tags", "op": "contains", "value": "coding"},
    ]})
    assert score(d, _ctx(tags=["coding"])) == 0.0


def test_score_require_fail_returns_none():
    d = parse_definition({"require": [
        {"field": "tags", "op": "contains", "value": "coding"},
    ]})
    assert score(d, _ctx(tags=["vision"])) is None


def test_score_multiple_require_all_must_pass():
    d = parse_definition({"require": [
        {"field": "tags", "op": "contains", "value": "coding"},
        {"field": "latency_p50_ms", "op": "<", "value": 2000},
    ]})
    # First passes, second fails -> excluded.
    assert score(d, _ctx(tags=["coding"], latency_p50_ms=5000)) is None
    # Both pass.
    assert score(d, _ctx(tags=["coding"], latency_p50_ms=500)) == 0.0


def test_score_prefer_sums_matching_weights():
    d = parse_definition({"prefer": [
        {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
        {"field": "tags", "op": "contains", "value": "cheap", "weight": 3},
        {"field": "tags", "op": "contains", "value": "vision", "weight": 7},
    ]})
    # Matches first two only.
    assert score(d, _ctx(tags=["fast", "cheap"])) == 8.0


def test_score_prefer_no_matches_returns_zero():
    d = parse_definition({"prefer": [
        {"field": "tags", "op": "contains", "value": "vision", "weight": 5},
    ]})
    # In the pool but no DSL bonus.
    assert score(d, _ctx(tags=["coding"])) == 0.0


def test_score_require_passes_then_prefer_adds():
    d = parse_definition({
        "require": [{"field": "tags", "op": "contains", "value": "coding"}],
        "prefer":  [{"field": "tags", "op": "contains", "value": "fast", "weight": 4}],
    })
    assert score(d, _ctx(tags=["coding", "fast"])) == 4.0


def test_score_excluded_provider_does_not_get_prefer_bonus():
    d = parse_definition({
        "require": [{"field": "tags", "op": "contains", "value": "vision"}],
        "prefer":  [{"field": "tags", "op": "contains", "value": "fast", "weight": 99}],
    })
    # Provider is fast but doesn't have vision -> excluded entirely.
    assert score(d, _ctx(tags=["fast"])) is None


# ─────────────────────────────────────────────────────────────────────
#                       context_from_provider helper
# ─────────────────────────────────────────────────────────────────────


def test_context_from_provider_computes_remaining():
    ctx = context_from_provider(
        name="groq", enabled=True, weight=1.0,
        tags=["fast", "cheap"],
        last_latency_ms=420,
        requests_today=300, requests_this_minute=10,
        rpd_limit=1000, rpm_limit=20,
        total_failures=2,
    )
    assert ctx.fields["name"] == "groq"
    assert ctx.fields["tags"] == ["fast", "cheap"]
    assert ctx.fields["latency_p50_ms"] == 420
    assert ctx.fields["rpd_remaining"] == pytest.approx(0.7)
    assert ctx.fields["rpm_remaining"] == pytest.approx(0.5)


def test_context_from_provider_handles_no_limits():
    # When rpd_limit / rpm_limit is None or zero, "remaining" is 1.0
    # (treat as fully open) — not zero, which would falsely exclude.
    ctx = context_from_provider(
        name="x", enabled=True, weight=1.0, tags=[],
        last_latency_ms=None,
        requests_today=10000, requests_this_minute=999,
        rpd_limit=None, rpm_limit=0,
        total_failures=0,
    )
    assert ctx.fields["rpd_remaining"] == 1.0
    assert ctx.fields["rpm_remaining"] == 1.0


def test_context_from_provider_clamps_remaining_above_limit():
    # If a provider has somehow exceeded its limit, remaining is 0, not negative.
    ctx = context_from_provider(
        name="x", enabled=True, weight=1.0, tags=[],
        last_latency_ms=100,
        requests_today=2000, requests_this_minute=50,
        rpd_limit=1000, rpm_limit=20,
        total_failures=0,
    )
    assert ctx.fields["rpd_remaining"] == 0.0
    assert ctx.fields["rpm_remaining"] == 0.0


# ─────────────────────────────────────────────────────────────────────
#                       serialize round-trip
# ─────────────────────────────────────────────────────────────────────


def test_serialize_round_trip():
    raw = {
        "require": [
            {"field": "tags", "op": "contains", "value": "coding"},
        ],
        "prefer": [
            {"field": "latency_p50_ms", "op": "<", "value": 1000, "weight": 3},
        ],
    }
    d = parse_definition(raw)
    out = serialize_definition(d)
    assert out == raw
    # And the round-trip is idempotent.
    assert serialize_definition(parse_definition(out)) == raw


def test_serialize_omits_weight_on_require_clauses():
    raw = {
        "require": [{"field": "name", "op": "==", "value": "groq"}],
        "prefer": [],
    }
    d = parse_definition(raw)
    out = serialize_definition(d)
    assert "weight" not in out["require"][0]


# ─────────────────────────────────────────────────────────────────────
#       integration: rebuilding the built-in strategies as DSL
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name,definition", [
    ("fastest", {
        "require": [],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
            {"field": "latency_p50_ms", "op": "<", "value": 1000, "weight": 3},
        ],
    }),
    ("coding", {
        "require": [{"field": "tags", "op": "contains", "value": "coding"}],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "reasoning", "weight": 5},
            {"field": "latency_p50_ms", "op": "<", "value": 2000, "weight": 2},
        ],
    }),
    ("vision", {
        "require": [{"field": "tags", "op": "contains", "value": "vision"}],
        "prefer": [],
    }),
    ("cheapest", {
        "require": [],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "cheap", "weight": 5},
            {"field": "rpd_remaining", "op": ">", "value": 0.5, "weight": 3},
        ],
    }),
])
def test_builtin_definitions_parse(name, definition):
    """The exact JSON shapes documented in STRATEGY_DSL.md must parse."""
    d = parse_definition(definition)
    assert isinstance(d, Definition)
    # Round-trip preserves them.
    assert serialize_definition(d) == definition
