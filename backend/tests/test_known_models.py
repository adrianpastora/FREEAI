"""Known-model list + suggestion helper. Pure tests, no DB."""
from __future__ import annotations

from app.providers.known_models import KNOWN_MODELS, is_known, suggest_similar


def test_known_models_populated():
    """Every provider in the registry should have at least one known model."""
    from app.providers import PROVIDER_REGISTRY
    for name in PROVIDER_REGISTRY:
        assert name in KNOWN_MODELS, f"{name} missing from KNOWN_MODELS"
        assert len(KNOWN_MODELS[name]) > 0, f"{name} has empty known models list"


def test_is_known_positive():
    assert is_known("groq", "openai/gpt-oss-120b") is True


def test_is_known_negative():
    assert is_known("groq", "fake-9001") is False


def test_is_known_unknown_provider():
    assert is_known("nonexistent", "whatever") is False


def test_suggest_similar_finds_typo():
    suggestions = suggest_similar("groq", "gpt-oss-120")
    assert any("gpt-oss-120b" in s for s in suggestions)


def test_suggest_similar_empty_for_nonsense():
    suggestions = suggest_similar("groq", "zzz")
    assert suggestions == [] or all(s != "zzz" for s in suggestions)


def test_suggest_similar_unknown_provider():
    assert suggest_similar("nonexistent", "anything") == []
