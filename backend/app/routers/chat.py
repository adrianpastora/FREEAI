"""OpenAI-compatible chat surface — /v1/models and /v1/chat/completions.

Streaming owns its session for the full lifetime of the generator —
``Depends(get_session)`` would close the session as soon as the endpoint
returns the StreamingResponse, leaving the generator with a dead session
and leaking connections when the client aborts.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..orchestrator import Orchestrator
from ..providers import ProviderError
from ..repositories import (
    ConfigRepository,
    RateRepository,
    StrategyRepository,
    UsageRepository,
)
from ..repositories.user_provider_repo import UserProviderRepository
from ..schemas import ChatCompletionRequest
from ..security import require_client
from ..virtual_models import VIRTUAL_MODELS
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
                    try:
                        async for chunk in orch.stream(
                            req, user_id, user_provider_repo,
                            config_repo, rate_repo, usage_repo, strategy_repo,
                            client_hash=client_hash,
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
        try:
            result = await orch.chat(
                req, user_id, user_provider_repo,
                config_repo, rate_repo, usage_repo, strategy_repo,
                client_hash=client_hash,
            )
            await session.commit()
            return result
        except ProviderError as e:
            await session.rollback()
            raise http_from_provider_error(e)
        except BaseException:
            await session.rollback()
            raise
