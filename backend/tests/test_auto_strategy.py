"""Language detection + auto strategy — pure tests, no DB."""
from __future__ import annotations

from app.auto_strategy import detect_auto_strategy, detect_language
from app.schemas import ChatMessage


def _msg(text: str):
    return [ChatMessage(role="user", content=text)]


# ──────────────── language detection ────────────────


def test_detect_english():
    assert detect_language("why is the sky blue") == "en"


def test_detect_spanish():
    assert detect_language("por qué el cielo es azul") == "es"


def test_detect_french():
    assert detect_language("pourquoi le ciel est bleu") == "fr"


def test_detect_german():
    assert detect_language("warum ist der himmel blau") == "de"


def test_detect_portuguese():
    assert detect_language("por que o céu é azul") == "pt"


def test_detect_empty_defaults_to_english():
    assert detect_language("") == "en"


# ──────────────── strategy picking ────────────────


def test_auto_picks_coding_from_code_block():
    signal = detect_auto_strategy(_msg("```python\ndef foo():\n  pass\n```"))
    assert signal.strategy == "coding"
    assert signal.has_code is True


def test_auto_picks_reasoning_spanish():
    signal = detect_auto_strategy(_msg("Explica paso a paso por qué ocurre esto"))
    assert signal.language == "es"
    assert signal.strategy == "reasoning"
    assert signal.has_reasoning is True


def test_auto_picks_reasoning_english():
    signal = detect_auto_strategy(_msg("Explain step by step why this happens"))
    assert signal.language == "en"
    assert signal.strategy == "reasoning"


def test_auto_picks_reasoning_french():
    signal = detect_auto_strategy(_msg("Explique étape par étape pourquoi"))
    assert signal.language == "fr"
    assert signal.strategy == "reasoning"


def test_auto_picks_long_context_from_length():
    long_text = "x" * 13_000
    signal = detect_auto_strategy(_msg(long_text))
    assert signal.strategy == "long_context"


def test_auto_picks_vision_when_image_mentioned():
    signal = detect_auto_strategy(_msg("Describe this image for me please"))
    assert signal.strategy == "vision"
    assert signal.has_vision is True


def test_auto_picks_vision_spanish():
    signal = detect_auto_strategy(_msg("Describe esta imagen en detalle"))
    assert signal.strategy == "vision"


def test_auto_falls_back_to_fastest():
    signal = detect_auto_strategy(_msg("hi"))
    assert signal.strategy == "fastest"


def test_vision_wins_over_code():
    """If both signals are present, vision takes precedence because visionproviders
    are strictly more capable (they're the superset)."""
    signal = detect_auto_strategy(_msg("Look at this image of def foo() and tell me if ```code``` is wrong"))
    assert signal.strategy == "vision"


def test_strategy_is_plain_str_not_literal():
    """REVIEW § 6.4: schemas.Strategy used to be a Literal of the builtin
    names, which rejected any custom strategy at the pydantic layer. It's
    now a plain str alias. This test pins that in place."""
    from app.schemas import Strategy, ChatCompletionRequest
    # If Strategy were a Literal, constructing a request with an unknown
    # value would raise a pydantic ValidationError. It must succeed now.
    req = ChatCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        strategy="completely_custom_name_that_is_not_a_builtin",
    )
    assert req.strategy == "completely_custom_name_that_is_not_a_builtin"
    # And the alias itself is just `str`
    assert Strategy is str
