"""Heuristic auto-strategy detector with lightweight language detection.

Previous version: one English regex for "why/explain/...", which meant every
Spanish prompt fell straight to `fastest`. This version:

  1. Detects the dominant language using stopword frequencies (EN/ES/FR/DE/PT)
     — no external deps, fast enough to run on every request.
  2. Picks patterns for that language when scoring the prompt.
  3. Combines signals: length, code density, reasoning markers, and vision cues.

It is still a heuristic, not a classifier — but it's good enough to stop sending
"explica paso a paso" to the fastest provider when the user actually wants a
reasoning model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import ChatMessage, Strategy

# Stopword sets — small, single-word only (multi-word entries never matched
# because the tokenizer splits on word boundaries). Cross-language collisions
# exist (e.g. "de", "a") but are tolerable — the language-specific
# interrogatives and _REASONING_MARKERS break ties.
_STOPWORDS = {
    "en": {
        "the", "and", "of", "to", "in", "is", "you", "that", "it",
        "he", "was", "for", "on", "are", "with", "his", "they",
        "why", "how", "what", "when", "where", "which",
    },
    "es": {
        "el", "la", "los", "las", "un",
        "ser", "se", "no", "haber", "por", "con", "su", "para", "como",
        "porque", "qué", "cómo", "cuándo", "dónde", "cuál",
    },
    "fr": {
        "le", "les", "des", "une", "et", "à", "est",
        "pour", "dans", "qui", "sur", "avec", "pas",
        "pourquoi", "comment", "quand", "où", "quel",
    },
    "de": {
        "der", "die", "das", "und", "ist", "zu", "den", "nicht", "von",
        "sie", "ein", "mit", "auf", "für", "sich",
        "warum", "wie", "wann", "wo", "welche",
    },
    "pt": {
        "o", "é", "do", "da", "em", "um",
        "com", "não", "os", "uma",
        "porque", "quando", "onde", "qual",
    },
}

# Reasoning markers per language — "explain", "step by step", etc.
_REASONING_MARKERS = {
    "en": re.compile(
        r"\b(why|prove|explain|reason|calculate|derive|analyz(?:e|sis)|step[-\s]by[-\s]step)\b",
        re.IGNORECASE,
    ),
    "es": re.compile(
        r"\b(por\s?qué|porque|demuestra|explica|razona|calcula|analiza|paso\s?a\s?paso)\b",
        re.IGNORECASE,
    ),
    "fr": re.compile(
        r"\b(pourquoi|prouve|explique|raisonne|calcule|analyse|étape\s?par\s?étape)\b",
        re.IGNORECASE,
    ),
    "de": re.compile(
        r"\b(warum|beweis|erkläre|begründe|berechne|analys(?:e|iere)|schritt\s?für\s?schritt)\b",
        re.IGNORECASE,
    ),
    "pt": re.compile(
        r"\b(por\s?que|porque|prove|explique|raciocine|calcule|analis(?:e|a)|passo\s?a\s?passo)\b",
        re.IGNORECASE,
    ),
}

# Code density — same regex for all languages, catches fences, stack traces,
# common language keywords, and SQL.
_CODE_RE = re.compile(
    r"```|\bdef\s|\bclass\s|\bfunction\s|\bimport\s|\bconst\s|\blet\s|\bvar\s"
    r"|\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b"
    r"|Traceback|stack\s?trace"
    r"|\{.*?\bfunction\b|=>\s*\{|\bpublic\s+class\s",
    re.IGNORECASE,
)

# Vision cues — "look at this image", "describe the picture"
_VISION_RE = re.compile(
    r"\b(image|picture|photo|screenshot|imagen|foto|captura|visual|diagram|chart)\b",
    re.IGNORECASE,
)


@dataclass
class AutoSignal:
    """What the detector found — exposed for observability/testing."""
    language: str
    strategy: Strategy
    total_chars: int
    has_code: bool
    has_reasoning: bool
    has_vision: bool


def detect_language(text: str) -> str:
    """Returns a 2-letter code from {en, es, fr, de, pt}. Defaults to 'en'."""
    lowered = text.lower()
    tokens = re.findall(r"\b\w+\b", lowered, flags=re.UNICODE)
    if not tokens:
        return "en"
    scores = {lang: 0 for lang in _STOPWORDS}
    token_set = set(tokens)
    for lang, words in _STOPWORDS.items():
        # count intersection — cheap O(n)
        scores[lang] = sum(1 for w in words if w in token_set)
    # pick the winner; tie → english (most likely default for API prompts)
    best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0:
        return "en"
    return best_lang


def detect_auto_strategy(messages: list[ChatMessage]) -> AutoSignal:
    # Use text_content property to handle both str and multimodal content
    last_user_msg = next((m for m in reversed(messages) if m.role == "user"), None)
    last_user = last_user_msg.text_content if last_user_msg else ""
    total_chars = sum(len(m.text_content) for m in messages)

    # Detect actual image_url blocks in any message (not just keywords)
    has_image_blocks = any(m.has_images for m in messages)

    language = detect_language(last_user)
    has_code = bool(_CODE_RE.search(last_user))
    has_vision = has_image_blocks or bool(_VISION_RE.search(last_user))
    has_reasoning = bool(_REASONING_MARKERS.get(language, _REASONING_MARKERS["en"]).search(last_user))

    if has_vision:
        strategy: Strategy = "vision"
    elif total_chars > 12_000:
        strategy = "long_context"
    elif has_code:
        strategy = "coding"
    elif has_reasoning:
        strategy = "reasoning"
    else:
        strategy = "fastest"

    return AutoSignal(
        language=language,
        strategy=strategy,
        total_chars=total_chars,
        has_code=has_code,
        has_reasoning=has_reasoning,
        has_vision=has_vision,
    )
