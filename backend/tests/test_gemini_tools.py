"""Gemini function calling: OpenAI tools <-> functionDeclarations,
functionCall parts <-> tool_calls, thoughtSignature round-trip. No DB."""
from __future__ import annotations

import json

import httpx
import pytest

from app.providers import gemini_provider
from app.providers.gemini_provider import GeminiProvider
from app.schemas import ChatMessage

TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "additionalProperties": False},
}}]


async def _call(handler, messages, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await GeminiProvider(api_key="k", default_model="gemini-3.5-flash").complete(
            messages, client=client, **kwargs)


@pytest.mark.asyncio
async def test_tools_become_function_declarations_and_calls_become_tool_calls():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [
                {"functionCall": {"name": "get_weather", "args": {"city": "Madrid"}},
                 "thoughtSignature": "c2lnLTE="},
                {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}},
            ]}}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3},
        })

    resp = await _call(handler, [ChatMessage(role="user", content="weather?")],
                       tools=TOOLS, tool_choice={"type": "function", "function": {"name": "get_weather"}})
    decl = seen["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "get_weather"
    assert decl["parametersJsonSchema"]["additionalProperties"] is False  # full JSON Schema kept
    assert seen["toolConfig"] == {"functionCallingConfig": {
        "mode": "ANY", "allowedFunctionNames": ["get_weather"]}}
    assert resp.content == ""  # not EMPTY_RESPONSE: the model called tools
    assert [json.loads(tc["function"]["arguments"])["city"] for tc in resp.tool_calls] == ["Madrid", "Paris"]
    assert gemini_provider._signatures[resp.tool_calls[0]["id"]] == "c2lnLTE="


@pytest.mark.asyncio
async def test_history_round_trip_restores_signature_and_groups_responses():
    gemini_provider._remember_signature("call_known", "c2lnLTI=")
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
            "content": {"parts": [{"text": "22C and 18C"}]}}]})

    history = [
        ChatMessage(role="system", content="be brief"),
        ChatMessage(role="user", content="weather in Madrid and Paris?"),
        ChatMessage(role="assistant", content=None, tool_calls=[
            {"id": "call_known", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"Madrid"}'}},
            {"id": "call_other", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}},
        ]),
        ChatMessage(role="tool", tool_call_id="call_known", content="22C"),
        ChatMessage(role="tool", tool_call_id="call_other", content="18C"),
    ]
    resp = await _call(handler, history, tools=TOOLS)
    contents = seen["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    model_parts = contents[1]["parts"]
    assert model_parts[0]["functionCall"] == {"name": "get_weather", "args": {"city": "Madrid"}}
    assert model_parts[0]["thoughtSignature"] == "c2lnLTI="       # restored from cache
    assert "thoughtSignature" not in model_parts[1]               # only the first is required
    responses = contents[2]["parts"]                              # both results in one turn
    assert [p["functionResponse"]["name"] for p in responses] == ["get_weather", "get_weather"]
    assert responses[1]["functionResponse"]["response"] == {"content": "18C"}
    assert seen["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert resp.content == "22C and 18C"


@pytest.mark.asyncio
async def test_unknown_signature_uses_skip_sentinel():
    """Calls made by another provider (or before a restart) have no stored
    signature; Gemini 3 would 400 without one."""
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    await _call(handler, [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="checking", tool_calls=[
            {"id": "call_from_groq", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"}}]),
        ChatMessage(role="tool", tool_call_id="call_from_groq", content="x"),
    ], tools=TOOLS)
    parts = seen["contents"][1]["parts"]
    assert parts[0] == {"text": "checking"}
    assert parts[1]["thoughtSignature"] == "skip_thought_signature_validator"


@pytest.mark.parametrize("choice,expected", [
    ("auto", {"mode": "AUTO"}), ("required", {"mode": "ANY"}), ("none", {"mode": "NONE"}),
])
def test_tool_choice_modes(choice, expected):
    out = GeminiProvider._tools_payload(TOOLS, choice)
    assert out["toolConfig"]["functionCallingConfig"] == expected


def test_no_tools_no_payload():
    assert GeminiProvider._tools_payload(None, "auto") == {}
    assert GeminiProvider._tools_payload([{"type": "function", "function": {}}], None) == {}
