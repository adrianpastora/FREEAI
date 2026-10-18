"""OpenAI-compatible chat surface — /v1/models, /v1/chat/completions and
/v1/responses (Responses API, translated onto the same orchestrator).

Streaming owns its session for the full lifetime of the generator —
``Depends(get_session)`` would close the session as soon as the endpoint
returns the StreamingResponse, leaving the generator with a dead session
and leaking connections when the client aborts.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
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
from ..schemas import ChatCompletionRequest, ChatMessage
from ..security import require_client
from ..virtual_models import DEFAULT_VIRTUAL_MODEL, VIRTUAL_MODELS
from ._common import get_orchestrator, http_from_provider_error, require_user_id

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


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    orch: Orchestrator = Depends(get_orchestrator),
    client=Depends(require_client),
    user_id: int = Depends(require_user_id),
):
    client_hash = client.key_hash if client else None

    if req.stream:
        sessionmaker = request.app.state.sessionmaker

        async def event_stream():
            async with sessionmaker() as stream_session:
                try:
                    config_repo = ConfigRepository(stream_session)
                    rate_repo = RateRepository(stream_session)
                    usage_repo = UsageRepository(stream_session)
                    strategy_repo = StrategyRepository(stream_session)
                    user_provider_repo = UserProviderRepository(stream_session)
                    pricing_repo = PricingRepository(stream_session)
                    try:
                        async for chunk in orch.stream(
                            req, user_id, user_provider_repo,
                            config_repo, rate_repo, usage_repo, strategy_repo,
                            client_hash=client_hash,
                            pricing_repo=pricing_repo,
                        ):
                            yield f"data: {json.dumps(chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                    except ProviderError as e:
                        err = {"error": {"provider": e.provider, "kind": e.kind.value, "message": e.message}}
                        yield f"data: {json.dumps(err)}\n\n"
                        yield "data: [DONE]\n\n"
                    await stream_session.commit()
                except BaseException:
                    await stream_session.rollback()
                    raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async with request.app.state.sessionmaker() as session:
        config_repo = ConfigRepository(session)
        rate_repo = RateRepository(session)
        usage_repo = UsageRepository(session)
        strategy_repo = StrategyRepository(session)
        user_provider_repo = UserProviderRepository(session)
        pricing_repo = PricingRepository(session)
        try:
            result = await orch.chat(
                req, user_id, user_provider_repo,
                config_repo, rate_repo, usage_repo, strategy_repo,
                client_hash=client_hash,
                pricing_repo=pricing_repo,
            )
            await session.commit()
            return result
        except ProviderError as e:
            await session.rollback()
            raise http_from_provider_error(e)
        except BaseException:
            await session.rollback()
            raise


@router.get("/v1/models/{model_id}")
async def retrieve_model(model_id: str) -> dict:
    """OpenAI-compatible model retrieval for the virtual models."""
    for vm in VIRTUAL_MODELS:
        if vm.id == model_id:
            return {
                "id": vm.id, "object": "model", "created": 0,
                "owned_by": "freeai", "description": vm.description,
            }
    raise HTTPException(404, detail={
        "error": {"message": f"model '{model_id}' not found", "type": "invalid_request_error"},
    })


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
        raise HTTPException(400, detail="request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, detail="request body must be a JSON object")

    history = None
    prev_id = body.get("previous_response_id")
    if prev_id:
        history = _RESPONSE_STORE.get(user_id, prev_id)
        if history is None:
            raise HTTPException(400, detail={"error": {
                "message": f"previous response '{prev_id}' not found (expired or created on "
                           "another server) — send the full conversation in 'input'",
                "type": "invalid_request_error", "param": "previous_response_id",
            }})
    try:
        req, conversation = responses_api.to_chat_request(body, history)
    except (responses_api.ResponsesRequestError, ValidationError, TypeError, ValueError) as e:
        raise HTTPException(400, detail={"error": {
            "message": str(e), "type": "invalid_request_error",
        }})

    response_id = responses_api.new_response_id()
    store = body.get("store", True) is not False

    def remember(text: str) -> None:
        if store:
            _RESPONSE_STORE.put(
                user_id, response_id,
                conversation + [ChatMessage(role="assistant", content=text)],
            )

    if req.stream:
        sessionmaker = request.app.state.sessionmaker
        requested_model = req.model or DEFAULT_VIRTUAL_MODEL

        async def event_stream():
            async with sessionmaker() as stream_session:
                try:
                    chunks = orch.stream(
                        req, user_id, UserProviderRepository(stream_session),
                        ConfigRepository(stream_session), RateRepository(stream_session),
                        UsageRepository(stream_session), StrategyRepository(stream_session),
                        client_hash=client_hash,
                        pricing_repo=PricingRepository(stream_session),
                    )
                    try:
                        async for event in responses_api.stream_events(
                            chunks, response_id=response_id, body=body,
                            requested_model=requested_model,
                            input_messages=req.messages, on_complete=remember,
                        ):
                            yield event
                    except ProviderError as e:
                        yield responses_api.failed_event(
                            response_id, body, requested_model,
                            {"code": e.kind.value, "message": e.message},
                        )
                    await stream_session.commit()
                except BaseException:
                    await stream_session.rollback()
                    raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
        except ProviderError as e:
            await session.rollback()
            raise http_from_provider_error(e)
        except BaseException:
            await session.rollback()
            raise
    obj = responses_api.from_chat_response(result, response_id=response_id, body=body)
    remember(obj.get("output_text", ""))
    return obj
