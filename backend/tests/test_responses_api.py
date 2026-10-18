"""OpenAI Responses API compatibility (/v1/responses) — translation unit tests
plus end-to-end tests through the real router with a mocked provider."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import responses_api
from app.main import app
from app.providers import PROVIDER_REGISTRY
from app.providers.base import ProviderResponse, StreamChunk
from app.schemas import ChatMessage


# ──────────────── translation (pure) ────────────────


def test_string_input_becomes_user_message():
    req, conv = responses_api.to_chat_request({"input": "hola", "model": "freeai-fast"})
    assert [m.role for m in req.messages] == ["user"]
    assert req.messages[0].content == "hola"
    assert req.model == "freeai-fast"
    assert conv == req.messages


def test_items_instructions_and_roles():
    body = {
        "instructions": "Be brief",
        "input": [
            {"role": "developer", "content": "sys rule"},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"role": "user", "content": "and now?"},
        ],
        "max_output_tokens": 999_999,
        "temperature": 0.2,
    }
    req, conv = responses_api.to_chat_request(body)
    assert [m.role for m in req.messages] == ["system", "system", "user", "assistant", "user"]
    assert req.messages[0].content == "Be brief"
    assert req.messages[2].content == "hi"           # text parts collapsed to a string
    assert req.max_tokens == 32_000                   # clamped, not a 422
    assert req.temperature == 0.2
    assert len(conv) == 4                             # instructions not stored in history


def test_image_part_maps_to_image_url():
    url = "data:image/png;base64,iVBORw0KGgo="
    req, _ = responses_api.to_chat_request({"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "what is this"},
        {"type": "input_image", "image_url": url},
    ]}]})
    content = req.messages[0].content
    assert content[1] == {"type": "image_url", "image_url": {"url": url}}
    assert req.messages[0].has_images


@pytest.mark.parametrize("body,needle", [
    ({"input": [{"type": "computer_call", "call_id": "c"}]}, "computer_call"),
    ({"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}]}, "input_file"),
    ({"input": []}, "input"),
    ({"input": "x", "background": True}, "background"),
])
def test_unsupported_features_raise_clear_errors(body, needle):
    with pytest.raises(responses_api.ResponsesRequestError) as exc:
        responses_api.to_chat_request(body)
    assert needle in str(exc.value)


def test_history_is_prepended():
    history = [ChatMessage(role="user", content="1"), ChatMessage(role="assistant", content="2")]
    req, conv = responses_api.to_chat_request({"input": "3"}, history)
    assert [m.content for m in req.messages] == ["1", "2", "3"]
    assert len(conv) == 3


def test_store_is_per_user_and_expires():
    store = responses_api.ResponseStore(max_items=2, ttl_s=60)
    store.put(1, "resp_a", [ChatMessage(role="user", content="a")])
    assert store.get(1, "resp_a")[0].content == "a"
    assert store.get(2, "resp_a") is None
    store.put(1, "resp_b", [])
    store.put(1, "resp_c", [])
    assert store.get(1, "resp_a") is None  # evicted (LRU, max 2)
    short = responses_api.ResponseStore(ttl_s=-1)
    short.put(1, "r", [])
    assert short.get(1, "r") is None


@pytest.mark.asyncio
async def test_stream_event_sequence():
    async def chunks():
        yield {"model": "freeai-fast", "provider": "groq", "choices": [{"delta": {"content": "Hel"}}]}
        yield {"model": "freeai-fast", "provider": "groq", "choices": [{"delta": {"content": "lo"}}]}
        yield {"provider": "groq", "choices": [{"delta": {}, "finish_reason": "stop"}]}

    stored = []
    events = [e async for e in responses_api.stream_events(
        chunks(), response_id="resp_x", body={}, requested_model="freeai-fast",
        input_messages=[ChatMessage(role="user", content="hi")], on_complete=stored.append,
    )]
    parsed = [json.loads(e.split("data: ", 1)[1]) for e in events]
    types = [p["type"] for p in parsed]
    assert types == [
        "response.created", "response.in_progress",
        "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done",
        "response.output_item.done", "response.completed",
    ]
    assert all(e.startswith(f"event: {t}\n") for e, t in zip(events, types))
    assert [p["sequence_number"] for p in parsed] == list(range(1, len(parsed) + 1))
    done = parsed[-1]["response"]
    assert done["status"] == "completed"
    assert done["output_text"] == "Hello"
    assert done["output"][0]["content"][0]["text"] == "Hello"
    assert stored == ["Hello"]


# ──────────────── end-to-end through the router ────────────────


@pytest_asyncio.fixture
async def client(seeded_session, sessionmaker):
    from app.auth import create_access_token
    from app.orchestrator import Orchestrator
    from app.repositories.strategy_repo import StrategyRepository
    from app.repositories.user_provider_repo import UserProviderRepository
    from app.repositories.user_repo import UserRepository

    user = await UserRepository(seeded_session).find_by_username("testadmin")
    await StrategyRepository(seeded_session).seed_builtins_if_missing()
    await UserProviderRepository(seeded_session).upsert(user.id, "groq", api_key="k", enabled=True)
    await seeded_session.commit()

    app.state.sessionmaker = sessionmaker
    app.state.orchestrator = Orchestrator()
    headers = {"Authorization": f"Bearer {create_access_token(user.id, user.username, user.role)}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c
    await app.state.orchestrator.aclose()


async def _complete(self, messages, *, model=None, **kwargs):
    last = messages[-1].text_content
    return ProviderResponse(
        content=f"echo:{last}|turns:{len(messages)}", model=model or "m", provider="groq",
        prompt_tokens=7, completion_tokens=3,
    )


@pytest.mark.asyncio
async def test_post_responses_returns_response_object(client):
    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _complete):
        r = await client.post("/v1/responses", json={"model": "freeai-fast", "input": "hola"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["object"] == "response"
    assert data["status"] == "completed"
    assert data["id"].startswith("resp_")
    assert data["output"][0]["type"] == "message"
    assert data["output"][0]["content"][0]["type"] == "output_text"
    assert data["output_text"].startswith("echo:hola")
    assert data["usage"] == {
        "input_tokens": 7, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 3, "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 10,
    }
    assert data["x_freeai"]["provider"] == "groq"


@pytest.mark.asyncio
async def test_previous_response_id_chains_turns(client):
    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _complete):
        first = (await client.post("/v1/responses", json={"input": "uno"})).json()
        second = (await client.post("/v1/responses", json={
            "input": "dos", "previous_response_id": first["id"],
        })).json()
    # user "uno" + assistant reply + user "dos" = 3 turns reached the provider
    assert second["output_text"] == "echo:dos|turns:3"


@pytest.mark.asyncio
async def test_unknown_previous_response_id_is_400(client):
    r = await client.post("/v1/responses", json={"input": "x", "previous_response_id": "resp_nope"})
    assert r.status_code == 400
    assert "send the full conversation" in r.text


@pytest.mark.asyncio
async def test_errors_use_openai_shape(client):
    r = await client.post("/v1/responses", json={"input": "x", "previous_response_id": "resp_nope"})
    assert r.status_code == 400
    err = r.json()["error"]  # top-level "error", not FastAPI's "detail"
    assert err["code"] == "previous_response_not_found"
    assert err["param"] == "previous_response_id"


@pytest.mark.asyncio
async def test_streaming_responses(client):
    async def _stream(self, messages, *, model=None, **kwargs):
        yield StreamChunk(delta="Ho", provider="groq", model=model or "m")
        yield StreamChunk(delta="la", provider="groq", model=model or "m", finish_reason="stop")

    with patch.object(PROVIDER_REGISTRY["groq"], "stream", _stream):
        r = await client.post("/v1/responses", json={"input": "hi", "stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[0]["type"] == "response.created"
    deltas = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    assert deltas == "Hola"
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["output_text"] == "Hola"


@pytest.mark.asyncio
async def test_retrieve_model_and_unknown_v1_route(client):
    ok = await client.get("/v1/models/freeai-auto")
    assert ok.status_code == 200 and ok.json()["id"] == "freeai-auto"
    missing = await client.get("/v1/models/gpt-9")
    assert missing.status_code == 404
    unknown = await client.post("/v1/assistants", json={})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "unknown_url"


# ──────────────── official OpenAI SDK against our endpoint ────────────────


@pytest.mark.asyncio
async def test_official_openai_sdk_parses_our_responses(client):
    """The strongest compatibility check: the real SDK must parse both the
    plain and the streamed Responses payloads without errors."""
    openai = pytest.importorskip("openai")
    jwt = client.headers["Authorization"].removeprefix("Bearer ")
    sdk = openai.AsyncOpenAI(api_key=jwt, base_url="http://test/v1", http_client=client)

    async def _stream(self, messages, *, model=None, **kwargs):
        yield StreamChunk(delta="Hola ", provider="groq", model=model or "m")
        yield StreamChunk(delta="mundo", provider="groq", model=model or "m", finish_reason="stop")

    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _complete), \
         patch.object(PROVIDER_REGISTRY["groq"], "stream", _stream):
        resp = await sdk.responses.create(model="freeai-auto", input="hola")
        assert resp.output_text.startswith("echo:hola")
        assert resp.usage.total_tokens == 10

        followup = await sdk.responses.create(
            model="freeai-auto", input="otra", previous_response_id=resp.id,
        )
        assert followup.output_text == "echo:otra|turns:3"

        text = ""
        final = None
        stream = await sdk.responses.create(model="freeai-auto", input="hi", stream=True)
        async for event in stream:
            if event.type == "response.output_text.delta":
                text += event.delta
            elif event.type == "response.completed":
                final = event.response
        assert text == "Hola mundo"
        assert final is not None and final.output_text == "Hola mundo"

        models = await sdk.models.list()
        assert "freeai-auto" in [m.id for m in models.data]


# ──────────────── function calling ────────────────

WEATHER_TOOL = {
    "type": "function", "name": "get_weather", "description": "Weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
}


def test_function_tools_translate_and_builtins_are_dropped():
    req, _ = responses_api.to_chat_request({
        "input": "weather in Madrid?",
        "tools": [WEATHER_TOOL, {"type": "web_search"}],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "parallel_tool_calls": False,
    })
    assert req.tools == [{"type": "function", "function": {
        "name": "get_weather", "description": "Weather for a city",
        "parameters": WEATHER_TOOL["parameters"],
    }}]
    assert req.tool_choice == {"type": "function", "function": {"name": "get_weather"}}
    assert req.parallel_tool_calls is False


def test_only_builtin_tools_means_no_tools():
    req, _ = responses_api.to_chat_request({"input": "x", "tools": [{"type": "web_search"}]})
    assert req.tools is None and not req.wants_tools


def test_function_call_items_become_assistant_and_tool_messages():
    req, conv = responses_api.to_chat_request({"input": [
        {"role": "user", "content": "weather in Madrid and Paris?"},
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"city":"Madrid"}'},
        {"type": "function_call", "call_id": "c2", "name": "get_weather", "arguments": '{"city":"Paris"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "22C"},
        {"type": "function_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "18C"}]},
    ], "tools": [WEATHER_TOOL]})
    roles = [m.role for m in req.messages]
    assert roles == ["user", "assistant", "tool", "tool"]
    assistant = req.messages[1]
    assert assistant.content is None
    assert [tc["id"] for tc in assistant.tool_calls] == ["c1", "c2"]  # merged into one turn
    assert req.messages[2].tool_call_id == "c1" and req.messages[2].content == "22C"
    assert req.messages[3].content == "18C"


def _tool_call_complete(captured):
    async def _complete(self, messages, *, model=None, tools=None, tool_choice=None, **kwargs):
        captured.update(tools=tools, tool_choice=tool_choice, messages=messages)
        if messages[-1].role == "tool":
            return ProviderResponse(content="It is 22C in Madrid.", model=model or "m",
                                    provider="groq", prompt_tokens=5, completion_tokens=5)
        return ProviderResponse(
            content="", model=model or "m", provider="groq", prompt_tokens=5, completion_tokens=2,
            tool_calls=[{"id": "call_abc", "type": "function",
                         "function": {"name": "get_weather", "arguments": '{"city":"Madrid"}'}}],
        )
    return _complete


@pytest.mark.asyncio
async def test_chat_completions_tool_call_round_trip(client):
    captured: dict = {}
    chat_tool = {"type": "function", "function": {
        "name": "get_weather", "parameters": WEATHER_TOOL["parameters"]}}
    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _tool_call_complete(captured)):
        r = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "weather in Madrid?"}],
            "tools": [chat_tool], "tool_choice": "auto",
        })
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert captured["tools"] == [chat_tool]          # forwarded upstream
    assert captured["tool_choice"] == "auto"
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_chat_completions_stream_with_tools_is_replayed(client):
    captured: dict = {}
    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _tool_call_complete(captured)):
        r = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
            "stream": True,
        })
    lines = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    first, final = json.loads(lines[0]), json.loads(lines[1])
    tc = first["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0 and tc["function"]["name"] == "get_weather"
    assert final["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_responses_function_call_loop(client):
    """Full agent loop: model asks for a tool, client answers with
    function_call_output via previous_response_id, model replies."""
    captured: dict = {}
    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _tool_call_complete(captured)):
        first = (await client.post("/v1/responses", json={
            "input": "weather in Madrid?", "tools": [WEATHER_TOOL],
        })).json()
        call = first["output"][0]
        assert call["type"] == "function_call"
        assert call["name"] == "get_weather" and call["call_id"] == "call_abc"
        assert json.loads(call["arguments"]) == {"city": "Madrid"}
        assert first["tools"][0]["name"] == "get_weather"

        second = (await client.post("/v1/responses", json={
            "previous_response_id": first["id"], "tools": [WEATHER_TOOL],
            "input": [{"type": "function_call_output", "call_id": "call_abc", "output": "22C"}],
        })).json()
    assert second["output_text"] == "It is 22C in Madrid."
    sent = captured["messages"]
    assert [m.role for m in sent] == ["user", "assistant", "tool"]
    assert sent[1].tool_calls[0]["id"] == "call_abc"


@pytest.mark.asyncio
async def test_responses_stream_with_tools_emits_function_call_events(client):
    captured: dict = {}
    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _tool_call_complete(captured)):
        r = await client.post("/v1/responses", json={
            "input": "weather?", "tools": [WEATHER_TOOL], "stream": True,
        })
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
    types = [e["type"] for e in events]
    assert types[:2] == ["response.created", "response.in_progress"]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    assert types[-1] == "response.completed"
    assert [e["sequence_number"] for e in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_tools_skip_providers_without_tool_support(client, seeded_session):
    """Gemini's adapter has no tool support, so a tool request must route
    around it instead of silently dropping the tools."""
    from app.repositories.user_provider_repo import UserProviderRepository
    from app.repositories.user_repo import UserRepository

    user = await UserRepository(seeded_session).find_by_username("testadmin")
    await UserProviderRepository(seeded_session).upsert(user.id, "gemini", api_key="g", enabled=True)
    await seeded_session.commit()
    called = []

    async def gemini_complete(self, *a, **k):
        called.append("gemini")
        raise AssertionError("gemini must not receive tool requests")

    captured: dict = {}
    with patch.object(PROVIDER_REGISTRY["gemini"], "complete", gemini_complete), \
         patch.object(PROVIDER_REGISTRY["groq"], "complete", _tool_call_complete(captured)):
        r = await client.post("/v1/responses", json={
            "input": "weather?", "tools": [WEATHER_TOOL], "preferred_provider": "gemini",
        })
    assert r.status_code == 200, r.text
    assert called == []
    assert r.json()["x_freeai"]["provider"] == "groq"


@pytest.mark.asyncio
async def test_official_sdk_function_calling(client):
    openai = pytest.importorskip("openai")
    jwt = client.headers["Authorization"].removeprefix("Bearer ")
    sdk = openai.AsyncOpenAI(api_key=jwt, base_url="http://test/v1", http_client=client)
    captured: dict = {}
    with patch.object(PROVIDER_REGISTRY["groq"], "complete", _tool_call_complete(captured)):
        resp = await sdk.responses.create(model="freeai-auto", input="weather?", tools=[WEATHER_TOOL])
        call = resp.output[0]
        assert call.type == "function_call" and call.name == "get_weather"
        done = await sdk.responses.create(
            model="freeai-auto", previous_response_id=resp.id, tools=[WEATHER_TOOL],
            input=[{"type": "function_call_output", "call_id": call.call_id, "output": "22C"}],
        )
        assert done.output_text == "It is 22C in Madrid."

        chat = await sdk.chat.completions.create(
            model="freeai-auto", messages=[{"role": "user", "content": "weather?"}],
            tools=[{"type": "function", "function": {"name": "get_weather",
                                                     "parameters": WEATHER_TOOL["parameters"]}}],
        )
        assert chat.choices[0].finish_reason == "tool_calls"
        assert chat.choices[0].message.tool_calls[0].function.name == "get_weather"

        stream = await sdk.responses.create(
            model="freeai-auto", input="weather?", tools=[WEATHER_TOOL], stream=True,
        )
        final = None
        async for event in stream:
            if event.type == "response.completed":
                final = event.response
        assert final.output[0].type == "function_call"
