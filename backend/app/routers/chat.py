"""OpenAI-compatible chat surface — /v1/models, /v1/chat/completions and
/v1/responses (Responses API, translated onto the same orchestrator).

Streaming owns its session for the full lifetime of the generator —
``Depends(get_session)`` would close the session as soon as the endpoint
returns the StreamingResponse, leaving the generator with a dead session
and leaking connections when the client aborts.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ..orchestrator import Orchestrator
from ..providers import ProviderError
from ..repositories import (
    ConfigRepository,
    PricingRepository,
    RateRepository,
    StrategyRepository,
    UsageRepository,
)
from ..repositories.user_provider_repo import UserProviderRepository
from .. import responses_api
from ..schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from ..security import require_client
from ..virtual_models import DEFAULT_VIRTUAL_MODEL, VIRTUAL_MODELS
from ._common import (
    get_orchestrator,
    http_from_provider_error,
    require_user_id,
    status_for_kind,
)

router = APIRouter(tags=["chat"])


@router.get("/v1/models")
async def list_models() -> dict:
    """OpenAI-compatible /v1/models — lists FreeAI virtual models.

    Each virtual model maps to an internal routing strategy. Clients can
    use any of these as the ``model`` parameter in chat completions.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": vm.id,
                "object": "model",
                "created": 0,
                "owned_by": "freeai",
                "description": vm.description,
            }
            for vm in VIRTUAL_MODELS
        ],
    }


async def _run_chat(
    request: Request,
    orch: Orchestrator,
    req: ChatCompletionRequest,
    user_id: int,
    client_hash,
) -> ChatCompletionResponse:
    """One non-streaming orchestrator call in its own session. Raises
    ProviderError for the caller to map to its own error format."""
    async with request.app.state.sessionmaker() as session:
        try:
            result = await orch.chat(
                req, user_id, UserProviderRepository(session),
                ConfigRepository(session), RateRepository(session),
                UsageRepository(session), StrategyRepository(session),
                client_hash=client_hash,
                pricing_repo=PricingRepository(session),
            )
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise


def _orchestrator_stream(request: Request, orch, req, user_id, client_hash):
    """Wrap orch.stream() so the session lives as long as the generator."""
    sessionmaker = request.app.state.sessionmaker

    async def gen():
        async with sessionmaker() as stream_session:
            try:
                async for chunk in orch.stream(
                    req, user_id, UserProviderRepository(stream_session),
                    ConfigRepository(stream_session), RateRepository(stream_session),
                    UsageRepository(stream_session), StrategyRepository(stream_session),
                    client_hash=client_hash,
                    pricing_repo=PricingRepository(stream_session),
                ):
                    yield chunk
                await stream_session.commit()
            except BaseException:
                await stream_session.rollback()
                raise

    return gen()


def _sse(gen) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _replay_chat_stream(result: ChatCompletionResponse):
    """chat.completion.chunk SSE for a finished completion — used when tools
    are involved, so tool calls arrive whole from any provider."""
    msg = result.choices[0].message
    base = {
        "id": result.id, "object": "chat.completion.chunk", "created": result.created,
        "model": result.model, "provider": result.provider,
        "strategy_used": result.strategy_used,
    }
    delta: dict = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(msg.tool_calls)]
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})}\n\n"
    final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": result.choices[0].finish_reason}]}
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    orch: Orchestrator = Depends(get_orchestrator),
    client=Depends(require_client),
    user_id: int = Depends(require_user_id),
):
    client_hash = client.key_hash if client else None

    if req.stream and not req.wants_tools:
        async def event_stream():
            try:
                async for chunk in _orchestrator_stream(request, orch, req, user_id, client_hash):
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except ProviderError as e:
                err = {"error": {"provider": e.provider, "kind": e.kind.value, "message": e.message}}
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"

        return _sse(event_stream())

    try:
        result = await _run_chat(request, orch, req, user_id, client_hash)
    except ProviderError as e:
        raise http_from_provider_error(e)
    if req.stream:
        return _sse(_replay_chat_stream(result))
    return result


@router.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str):
    """OpenAI-compatible model retrieval for the virtual models."""
    for vm in VIRTUAL_MODELS:
        if vm.id == model_id:
            return {
                "id": vm.id, "object": "model", "created": 0,
                "owned_by": "freeai", "description": vm.description,
            }
    return _openai_error(404, f"model '{model_id}' not found", code="model_not_found")


def _openai_error(status: int, message: str, *, code=None, param=None,
                  type_: str = "invalid_request_error") -> JSONResponse:
    """Error body in OpenAI's shape ({"error": {...}}) — what SDKs parse."""
    return JSONResponse(status_code=status, content={"error": {
        "message": message, "type": type_, "param": param, "code": code,
    }})


# Conversation histories for previous_response_id chaining (process-local).
_RESPONSE_STORE = responses_api.ResponseStore()


@router.post("/v1/responses")
async def create_response(
    request: Request,
    orch: Orchestrator = Depends(get_orchestrator),
    client=Depends(require_client),
    user_id: int = Depends(require_user_id),
):
    """OpenAI Responses API, translated onto the chat orchestrator so it gets
    the same routing, fallback and rate-limit handling as chat completions."""
    client_hash = client.key_hash if client else None
    try:
        body = await request.json()
    except ValueError:
        return _openai_error(400, "request body must be JSON")
    if not isinstance(body, dict):
        return _openai_error(400, "request body must be a JSON object")

    history = None
    prev_id = body.get("previous_response_id")
    if prev_id:
        history = _RESPONSE_STORE.get(user_id, prev_id)
        if history is None:
            return _openai_error(
                400,
                f"previous response '{prev_id}' not found (expired or created on another "
                "server) — send the full conversation in 'input'",
                param="previous_response_id", code="previous_response_not_found",
            )
    try:
        req, conversation = responses_api.to_chat_request(body, history)
    except (responses_api.ResponsesRequestError, ValidationError, TypeError, ValueError) as e:
        return _openai_error(400, str(e))

    response_id = responses_api.new_response_id()
    store = body.get("store", True) is not False

    def remember(turn: ChatMessage) -> None:
        if store:
            _RESPONSE_STORE.put(user_id, response_id, conversation + [turn])

    if req.stream and not req.wants_tools:
        requested_model = req.model or DEFAULT_VIRTUAL_MODEL

        async def event_stream():
            try:
                async for event in responses_api.stream_events(
                    _orchestrator_stream(request, orch, req, user_id, client_hash),
                    response_id=response_id, body=body,
                    requested_model=requested_model, input_messages=req.messages,
                    on_complete=lambda text: remember(responses_api.assistant_turn(text, None)),
                ):
                    yield event
            except ProviderError as e:
                yield responses_api.failed_event(
                    response_id, body, requested_model,
                    {"code": e.kind.value, "message": e.message},
                )

        return _sse(event_stream())

    try:
        result = await _run_chat(request, orch, req, user_id, client_hash)
    except ProviderError as e:
        return _openai_error(
            status_for_kind(e.kind), e.message, code=e.kind.value,
            type_="invalid_request_error" if e.kind.value == "client_error" else "api_error",
        )
    obj = responses_api.from_chat_response(result, response_id=response_id, body=body)
    msg = result.choices[0].message
    remember(responses_api.assistant_turn(msg.content, msg.tool_calls))
    if req.stream:
        return _sse(responses_api.replay_events(obj))
    return obj
