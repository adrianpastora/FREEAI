"""Schema must accept full OpenAI-compatible conversation histories so
clients that round-trip tool_calls (or simply pass null assistant content)
don't get 422'd on their second turn.
"""
from __future__ import annotations

import pytest

from app.providers.base import BaseProvider
from app.schemas import ChatCompletionRequest, ChatMessage


def test_assistant_content_none_is_accepted():
    req = ChatCompletionRequest(messages=[
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": None},
        {"role": "user", "content": "sigue"},
    ])
    assert req.messages[1].content is None
    assert req.messages[1].text_content == ""
    assert req.messages[1].has_images is False


def test_tool_calls_and_tool_role_round_trip():
    req = ChatCompletionRequest(messages=[
        {"role": "user", "content": "search X"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "s", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": "ok"},
    ])
    assert req.messages[1].tool_calls is not None
    assert req.messages[1].tool_calls[0]["id"] == "call_1"
    assert req.messages[2].tool_call_id == "call_1"


def test_messages_to_dicts_preserves_tool_fields():
    msgs = [
        ChatMessage(role="user", content="search X"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "s", "arguments": "{}"}}],
        ),
        ChatMessage(role="tool", tool_call_id="c1", content="result"),
    ]
    out = BaseProvider(api_key="x")._messages_to_dicts(msgs)
    # assistant dict must have null content AND tool_calls
    assert out[1]["content"] is None
    assert out[1]["tool_calls"][0]["id"] == "c1"
    # tool dict must carry tool_call_id
    assert out[2]["tool_call_id"] == "c1"
    assert out[2]["content"] == "result"


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        {"tool_choice": "auto"},
        {"response_format": {"type": "json_object"}},
        {"seed": 42},
        {"top_p": 0.9},
        {"n": 1},
        {"stop": ["END"]},
        {"presence_penalty": 0.1},
        {"frequency_penalty": 0.1},
        {"logit_bias": {}},
        {"user": "u1"},
    ],
)
def test_openai_sdk_extras_are_accepted(extra):
    """The full OpenAI SDK shape must not 422 — we silently accept extras
    we don't consume yet so clients can pass their usual payload."""
    ChatCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        **extra,
    )


def test_gemini_handles_none_content():
    """Regression: Gemini's _content_to_parts used to crash on None
    (for assistant-with-only-tool_calls turns). Must return an empty text
    part instead."""
    from app.providers.gemini_provider import GeminiProvider
    parts = GeminiProvider(api_key="k")._content_to_parts(None)
    assert parts == [{"text": ""}]
