"""Static list of well-known models per provider.

Used for:
  • Fast client-side feedback when editing default_model (no network round-trip)
  • A dropdown in the frontend

It is NOT a hard validation — if the user enters a model that isn't in the list
we still accept it and let the provider say yes/no at request time. That way
FreeAI never blocks a brand-new model just because we haven't updated this list.

Each entry records capability hints that *could* later drive provider tag
auto-population (e.g. "this model supports vision"). For now tags are still
set at the provider level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KnownModel:
    id: str
    context_window: Optional[int] = None
    capabilities: list[str] = field(default_factory=list)  # e.g. ["chat","vision","tools"]
    note: str = ""


KNOWN_MODELS: dict[str, list[KnownModel]] = {
    "groq": [
        KnownModel("llama-3.3-70b-versatile", 128_000, ["chat", "tools"]),
        KnownModel("llama-3.1-70b-versatile", 128_000, ["chat", "tools"], note="legacy"),
        KnownModel("llama-3.1-8b-instant", 128_000, ["chat"], note="fastest"),
        KnownModel("mixtral-8x7b-32768", 32_768, ["chat"]),
        KnownModel("gemma2-9b-it", 8_192, ["chat"]),
    ],
    "gemini": [
        KnownModel("gemini-2.5-flash", 1_048_576, ["chat", "vision", "tools", "long_context", "reasoning"]),
        KnownModel("gemini-2.5-flash-lite", 1_048_576, ["chat", "vision", "long_context"]),
        KnownModel("gemini-2.5-pro", 1_048_576, ["chat", "vision", "tools", "long_context", "reasoning"], note="most capable"),
        KnownModel("gemini-3-flash-preview", 1_048_576, ["chat", "vision", "tools", "long_context", "reasoning"], note="preview"),
    ],
    "mistral": [
        KnownModel("mistral-small-latest", 32_000, ["chat", "tools"]),
        KnownModel("mistral-large-latest", 128_000, ["chat", "tools", "reasoning"]),
        KnownModel("open-mistral-nemo", 128_000, ["chat"]),
        KnownModel("codestral-latest", 32_000, ["chat", "coding"]),
    ],
    "openrouter": [
        KnownModel("meta-llama/llama-3.3-70b-instruct:free", 131_000, ["chat"]),
        KnownModel("meta-llama/llama-3.2-3b-instruct:free", 131_000, ["chat"]),
        KnownModel("google/gemini-2.0-flash-exp:free", 1_048_576, ["chat", "vision"]),
        KnownModel("mistralai/mistral-small-3.1-24b-instruct:free", 32_000, ["chat"]),
        KnownModel("qwen/qwen-2.5-72b-instruct:free", 32_000, ["chat", "reasoning"]),
    ],
    "cohere": [
        KnownModel("command-r-08-2024", 128_000, ["chat", "tools", "rag"]),
        KnownModel("command-r-plus-08-2024", 128_000, ["chat", "tools", "rag", "reasoning"]),
        KnownModel("command-r7b-12-2024", 128_000, ["chat"], note="smallest"),
    ],
    "huggingface": [
        KnownModel("meta-llama/Llama-3.2-3B-Instruct", 131_000, ["chat"]),
        KnownModel("meta-llama/Llama-3.3-70B-Instruct", 131_000, ["chat"]),
        KnownModel("Qwen/Qwen2.5-72B-Instruct", 32_000, ["chat", "reasoning"]),
        KnownModel("mistralai/Mixtral-8x7B-Instruct-v0.1", 32_000, ["chat"]),
    ],
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
