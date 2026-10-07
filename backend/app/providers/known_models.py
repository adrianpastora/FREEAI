"""Well-known models per provider, derived from ``catalog.CATALOG``.

Used for:
  • Fast client-side feedback when editing default_model (no network round-trip)
  • A dropdown in the frontend

It is NOT a hard validation — if the user enters a model that isn't in the list
we still accept it and let the provider say yes/no at request time. That way
FreeAI never blocks a brand-new model just because we haven't updated this list.

Edit the catalog (app/providers/catalog.py), not this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .catalog import CATALOG


@dataclass
class KnownModel:
    id: str
    context_window: Optional[int] = None
    capabilities: list[str] = field(default_factory=list)  # e.g. ["chat","vision","tools"]
    note: str = ""


KNOWN_MODELS: dict[str, list[KnownModel]] = {
    name: [
        KnownModel(m.id, m.context_window, list(m.capabilities), m.note)
        for m in entry.models
    ]
    for name, entry in CATALOG.items()
}


def is_known(provider: str, model_id: str) -> bool:
    models = KNOWN_MODELS.get(provider, [])
    return any(m.id == model_id for m in models)


def suggest_similar(provider: str, model_id: str, max_results: int = 3) -> list[str]:
    """Naive fuzzy-match on substring — gives the user a quick hint when they
    typo a model name."""
    models = KNOWN_MODELS.get(provider, [])
    if not models:
        return []
    lowered = model_id.lower()
    scored = []
    for m in models:
        mid = m.id.lower()
        # simple score: longest common substring length
        score = _lcs_len(lowered, mid)
        scored.append((score, m.id))
    scored.sort(reverse=True)
    return [mid for score, mid in scored[:max_results] if score > 3]


def _lcs_len(a: str, b: str) -> int:
    # Longest common substring length, good enough for model name typos.
    if not a or not b:
        return 0
    best = 0
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                table[i][j] = table[i - 1][j - 1] + 1
                if table[i][j] > best:
                    best = table[i][j]
    return best
