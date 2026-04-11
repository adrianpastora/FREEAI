"""Strategy DSL — declarative routing rules.

A strategy definition has the shape:

    {
      "require": [ {"field": "tags", "op": "contains", "value": "coding"} ],
      "prefer":  [ {"field": "tags", "op": "contains", "value": "fast", "weight": 5} ]
    }

`require` clauses are hard filters — a provider that fails any of them is
excluded from the candidate pool. `prefer` clauses are soft scoring —
each match adds its `weight` to the provider's score on top of the
baseline (provider weight + headroom + latency bonus, computed
elsewhere).

This module is pure: no DB, no I/O, no global state. It only knows how
to parse a definition into a typed shape and how to evaluate it against
an `EvalContext` of provider data. The orchestrator builds the context
and calls `score()`.

See docs/STRATEGY_DSL.md for the full design rationale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ───────────────────────────── field schema ─────────────────────────────

# Every field the DSL recognizes, with its type. Anything not in this
# dict is rejected at parse time. The "type" controls which operators
# are legal — see OPS_BY_TYPE below.
#
# Why a closed set: the parser must reject typos before they reach the
# evaluator. A typo'd field name in user-supplied JSON is a UX bug — we
# tell them up front rather than silently scoring zero.

FIELD_TYPES: dict[str, str] = {
    "tags":                "string_array",
    "name":                "string",
    "weight":              "number",
    "enabled":             "bool",
    "latency_p50_ms":      "number",
    "requests_today":      "number",
    "requests_this_minute": "number",
    "rpd_remaining":       "number",
    "rpm_remaining":       "number",
    "total_failures":      "number",
}

OPS_BY_TYPE: dict[str, set[str]] = {
    "string_array": {"contains"},
    "string":       {"==", "!=", "in"},
    "number":       {"==", "!=", "<", "<=", ">", ">="},
    "bool":         {"==", "!="},
}

ALL_OPS: set[str] = {"contains", "==", "!=", "<", "<=", ">", ">=", "in"}


# ───────────────────────────── data shapes ─────────────────────────────


@dataclass(frozen=True)
class Clause:
    """A single condition: a field/op/value triple, with weight for prefer."""
    field: str
    op: str
    value: Any
    weight: float = 0.0  # ignored on require clauses


@dataclass(frozen=True)
class Definition:
    """A parsed, validated strategy definition."""
    require: tuple[Clause, ...] = ()
    prefer: tuple[Clause, ...] = ()

    def is_empty(self) -> bool:
        return not self.require and not self.prefer


@dataclass
class EvalContext:
    """Provider state used to evaluate clauses against.

    Built by the orchestrator from a `ProviderConfigDTO` + `ProviderSnapshot`.
    Kept as a flat dict so the evaluator stays trivial — no field accessors
    to maintain.
    """
    fields: dict[str, Any] = field(default_factory=dict)


class ParseError(ValueError):
    """Raised by `parse_definition` with a human-readable message.

    The first message line is the issue, optionally followed by a hint.
    The exception is caught at the API boundary and returned as a 422
    with the message in `detail`.
    """


# ───────────────────────────── parser ─────────────────────────────


def parse_definition(raw: Any) -> Definition:
    """Validate a raw definition (typically from JSON) into a `Definition`.

    Accepts None or an empty dict and returns an empty Definition — that
    represents the "baseline only" strategy (no filters, no extra
    scoring).

    Raises ParseError on any structural or semantic problem.
    """
    if raw is None:
        return Definition()
    if not isinstance(raw, dict):
        raise ParseError(f"definition must be an object, got {type(raw).__name__}")

    # Reject unknown top-level keys so a typo like "requires" doesn't
    # silently degrade to an empty filter.
    allowed_top = {"require", "prefer"}
    extras = set(raw.keys()) - allowed_top
    if extras:
        raise ParseError(
            f"unknown top-level key(s): {sorted(extras)}. "
            f"Allowed: {sorted(allowed_top)}"
        )

    require = tuple(_parse_clause(c, "require", i) for i, c in enumerate(raw.get("require") or []))
    prefer = tuple(_parse_clause(c, "prefer", i) for i, c in enumerate(raw.get("prefer") or []))
    return Definition(require=require, prefer=prefer)


def _parse_clause(raw: Any, section: str, idx: int) -> Clause:
    if not isinstance(raw, dict):
        raise ParseError(f"{section}[{idx}]: clause must be an object, got {type(raw).__name__}")

    allowed_keys = {"field", "op", "value", "weight"}
    extras = set(raw.keys()) - allowed_keys
    if extras:
        raise ParseError(
            f"{section}[{idx}]: unknown clause key(s): {sorted(extras)}. "
            f"Allowed: {sorted(allowed_keys)}"
        )

    if "field" not in raw:
        raise ParseError(f"{section}[{idx}]: missing 'field'")
    if "op" not in raw:
        raise ParseError(f"{section}[{idx}]: missing 'op'")
    if "value" not in raw:
        raise ParseError(f"{section}[{idx}]: missing 'value'")

    field_name = raw["field"]
    op = raw["op"]
    value = raw["value"]

    if not isinstance(field_name, str) or field_name not in FIELD_TYPES:
        raise ParseError(
            f"{section}[{idx}]: unknown field {field_name!r}. "
            f"Allowed fields: {sorted(FIELD_TYPES.keys())}"
        )

    field_type = FIELD_TYPES[field_name]

    if not isinstance(op, str) or op not in ALL_OPS:
        raise ParseError(
            f"{section}[{idx}]: unknown operator {op!r}. Allowed: {sorted(ALL_OPS)}"
        )

    legal_ops = OPS_BY_TYPE[field_type]
    if op not in legal_ops:
        raise ParseError(
            f"{section}[{idx}]: operator {op!r} is not valid for field "
            f"{field_name!r} (type {field_type}). Valid ops: {sorted(legal_ops)}"
        )

    _validate_value_type(field_name, field_type, op, value, section, idx)

    weight: float = 0.0
    if section == "prefer":
        if "weight" not in raw:
            raise ParseError(f"prefer[{idx}]: 'weight' is required on prefer clauses")
        try:
            weight = float(raw["weight"])
        except (TypeError, ValueError):
            raise ParseError(
                f"prefer[{idx}]: 'weight' must be numeric, got {raw['weight']!r}"
            )
    elif "weight" in raw:
        # require clauses ignore weight, but a user setting it is almost
        # certainly a misunderstanding — fail loud so they don't think
        # it's doing something.
        raise ParseError(
            f"require[{idx}]: 'weight' is not allowed on require clauses "
            f"(require is a hard filter, not a score)"
        )

    return Clause(field=field_name, op=op, value=value, weight=weight)


def _validate_value_type(
    field_name: str, field_type: str, op: str, value: Any,
    section: str, idx: int,
) -> None:
    """Make sure `value` matches what `op` expects for this field type."""
    if op == "in":
        # `in` always expects a list of valid scalars for the underlying type.
        if not isinstance(value, list) or not value:
            raise ParseError(
                f"{section}[{idx}]: operator 'in' expects a non-empty list, "
                f"got {type(value).__name__}"
            )
        for v in value:
            if field_type == "string" and not isinstance(v, str):
                raise ParseError(
                    f"{section}[{idx}]: 'in' on string field {field_name!r} "
                    f"expects strings; got {v!r}"
                )
            if field_type == "number" and not isinstance(v, (int, float)):
                raise ParseError(
                    f"{section}[{idx}]: 'in' on numeric field {field_name!r} "
                    f"expects numbers; got {v!r}"
                )
        return

    if op == "contains":
        # contains is only valid on string_array fields, and value must be a string.
        if not isinstance(value, str):
            raise ParseError(
                f"{section}[{idx}]: 'contains' expects a string value, "
                f"got {type(value).__name__}"
            )
        return

    # Comparison operators: scalar value matching field type.
    if field_type == "string":
        if not isinstance(value, str):
            raise ParseError(
                f"{section}[{idx}]: field {field_name!r} is a string, "
                f"value must be a string (got {type(value).__name__})"
            )
    elif field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParseError(
                f"{section}[{idx}]: field {field_name!r} is numeric, "
                f"value must be a number (got {type(value).__name__})"
            )
    elif field_type == "bool":
        if not isinstance(value, bool):
            raise ParseError(
                f"{section}[{idx}]: field {field_name!r} is boolean, "
                f"value must be true/false (got {type(value).__name__})"
            )


# ───────────────────────────── evaluator ─────────────────────────────


def matches(clause: Clause, ctx: EvalContext) -> bool:
    """Return True if `ctx` satisfies `clause`. Missing field → False."""
    if clause.field not in ctx.fields:
        return False
    actual = ctx.fields[clause.field]
    if actual is None:
        return False
    op = clause.op
    expected = clause.value

    if op == "contains":
        # actual must be an iterable of strings; expected is a string.
        try:
            return expected in actual
        except TypeError:
            return False
    if op == "in":
        return actual in expected
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == ">=":
        return actual >= expected
    return False


def score(defn: Definition, ctx: EvalContext) -> Optional[float]:
    """Compute the DSL contribution to a provider's score.

    Returns:
        None  if any `require` clause fails (provider is excluded).
        0.0   if all requires pass and no `prefer` clauses match
              (provider is in the pool, scored only by baseline).
        > 0   sum of weights of matching `prefer` clauses.

    The orchestrator adds this on top of `baseline_score(provider)`.
    Returning None lets the caller distinguish "excluded" from
    "in the pool but no DSL bonus".
    """
    for c in defn.require:
        if not matches(c, ctx):
            return None
    total = 0.0
    for c in defn.prefer:
        if matches(c, ctx):
            total += c.weight
    return total


# ───────────────────────────── helpers for callers ─────────────────────────────


def context_from_provider(
    *,
    name: str,
    enabled: bool,
    weight: float,
    tags: list[str],
    last_latency_ms: Optional[int],
    requests_today: int,
    requests_this_minute: int,
    rpd_limit: Optional[int],
    rpm_limit: Optional[int],
    total_failures: int,
) -> EvalContext:
    """Build an EvalContext from the fields the orchestrator already has.

    Convenience constructor so callers don't have to know the field
    names. Derived fields (`rpd_remaining`, `rpm_remaining`) are computed
    here. If a limit is None or zero, the corresponding remaining field
    is 1.0 (treated as "no limit, fully open").
    """
    rpd_remaining = 1.0
    if rpd_limit and rpd_limit > 0:
        rpd_remaining = max(0.0, 1.0 - requests_today / rpd_limit)
    rpm_remaining = 1.0
    if rpm_limit and rpm_limit > 0:
        rpm_remaining = max(0.0, 1.0 - requests_this_minute / rpm_limit)
    return EvalContext(fields={
        "name": name,
        "enabled": enabled,
        "weight": weight,
        "tags": tags,
        "latency_p50_ms": last_latency_ms,
        "requests_today": requests_today,
        "requests_this_minute": requests_this_minute,
        "rpd_remaining": rpd_remaining,
        "rpm_remaining": rpm_remaining,
        "total_failures": total_failures,
    })


def serialize_definition(defn: Definition) -> dict:
    """Round-trip a Definition back to the JSON-shaped dict.

    Used by the API and by `BUILTIN_STRATEGIES` so the wire format and
    the in-memory format are never out of sync.
    """
    def clause_to_dict(c: Clause, in_prefer: bool) -> dict:
        d: dict[str, Any] = {"field": c.field, "op": c.op, "value": c.value}
        if in_prefer:
            d["weight"] = c.weight
        return d
    return {
        "require": [clause_to_dict(c, False) for c in defn.require],
        "prefer":  [clause_to_dict(c, True) for c in defn.prefer],
    }
