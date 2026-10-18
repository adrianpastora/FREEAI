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
    ({"input": "x", "tools": [{"type": "function", "name": "f"}]}, "tools"),
    ({"input": [{"type": "function_call_output", "call_id": "c", "output": "1"}]}, "tool"),
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
async def test_tools_rejected_with_400(client):
    r = await client.post("/v1/responses", json={"input": "x", "tools": [{"type": "function"}]})
    assert r.status_code == 400
    assert "tools" in r.text


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
