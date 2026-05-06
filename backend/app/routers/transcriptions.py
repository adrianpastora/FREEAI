"""OpenAI-compatible audio transcription with multi-provider fallback.

Tries providers in priority order (Groq Whisper → Gemini). Each provider
is checked for: configured API key, enabled, available capacity. On
transient failure the next provider is attempted. The response always
follows OpenAI shape: ``{"text": "..."}`` with extra ``provider``/``model``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..logging_config import get_logger
from ..providers import ErrorKind
from ..repositories import (
    ProviderConfigDTO,
    RateRepository,
    UsageRepository,
)
from ..repositories.usage_repo import UsageEvent
from ..repositories.user_provider_repo import UserProviderRepository
from ..security import require_client
from ..transcription import (
    TRANSCRIPTION_PROVIDERS,
    AudioInput,
    TranscriptionResult,
    resolve_content_type,
    supports_transcription,
    transcribe,
)
from ._common import MAX_BODY_BYTES_AUDIO, require_user_id, status_for_kind

router = APIRouter(tags=["audio"])
log = get_logger("freeai.transcriptions")


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    client=Depends(require_client),
    user_id: int = Depends(require_user_id),
):
    """OpenAI-compatible audio transcription with multi-provider fallback.

    Tries providers in priority order (Groq Whisper → Gemini). Each
    provider is checked for: configured API key, enabled, and available
    capacity. On transient failure the next provider is attempted.

    The response always follows the OpenAI format: ``{"text": "..."}``
    with additional ``provider`` and ``model`` fields.
    """
    rate_repo = RateRepository(session)
    usage_repo = UsageRepository(session)
    user_provider_repo = UserProviderRepository(session)
    client_hash = client.key_hash if client else None

    # ── Prepare audio input (read once, reuse across attempts) ──
    file_bytes = await file.read()
    if len(file_bytes) > MAX_BODY_BYTES_AUDIO:
        raise HTTPException(
            413,
            f"audio payload exceeds {MAX_BODY_BYTES_AUDIO} bytes",
        )
    audio = AudioInput(
        file_bytes=file_bytes,
        filename=file.filename or "audio.ogg",
        content_type=resolve_content_type(file.filename, file.content_type),
        language=language,
    )

    # ── Collect eligible providers from user's configured providers ──
    user_providers = await user_provider_repo.list_for_user(user_id)
    candidates: list[tuple[str, ProviderConfigDTO]] = []
    for name in TRANSCRIPTION_PROVIDERS:
        if not supports_transcription(name):
            continue
        # Find in user's providers
        up = next((p for p in user_providers if p.provider_name == name), None)
        if up and up.api_key and up.enabled:
            dto = up.to_provider_config()
            candidates.append((name, dto))

    if not candidates:
        raise HTTPException(
            400,
            "No transcription provider configured — add an API key for Groq or Gemini",
        )

    # ── Fallback loop: try each provider in priority order ──
    errors: list[dict] = []       # track every attempt for diagnostics
    fallback_position = 0

    for provider_name, dto in candidates:
        fallback_position += 1

        # Reserve capacity
        reservation = await rate_repo.try_reserve(
            user_id, provider_name, dto.rpm_limit, dto.rpd_limit,
        )
        if reservation is None:
            errors.append({"provider": provider_name, "skipped": "at capacity"})
            continue

        reservation_settled = False
        try:
            # Attempt transcription
            result = await transcribe(
                provider_name, audio, dto.api_key,
                client=request.app.state.orchestrator.http_client,
            )

            if isinstance(result, TranscriptionResult):
                # ── Success ──
                await rate_repo.commit(reservation, result.latency_ms, ok=True)
                reservation_settled = True
                await usage_repo.record(UsageEvent(
                    provider=result.provider, model=result.model,
                    strategy="transcription", outcome="success",
                    latency_ms=result.latency_ms, client_hash=client_hash,
                    user_id=user_id, fallback_position=fallback_position,
                ))
                return {
                    "text": result.text,
                    "provider": result.provider,
                    "model": result.model,
                    "latency_ms": result.latency_ms,
                    "fallback_position": fallback_position,
                }

            # ── Failure: commit error and decide whether to continue ──
            err = result
            errors.append({
                "provider": err.provider,
                "kind": err.kind.value,
                "message": err.message[:200],
            })

            quarantine_s = None
            if err.kind == ErrorKind.SERVER_ERROR:
                quarantine_s = 60
            elif err.kind == ErrorKind.NETWORK:
                quarantine_s = 30

            await rate_repo.commit(
                reservation, err.latency_ms, ok=False,
                error=err.message, error_kind=err.kind.value,
                quarantine_seconds=quarantine_s,
            )
            reservation_settled = True
            await usage_repo.record(UsageEvent(
                provider=err.provider, model=err.model,
                strategy="transcription", outcome=err.kind.value,
                latency_ms=err.latency_ms, client_hash=client_hash,
                user_id=user_id, fallback_position=fallback_position,
            ))

            # Auth/client errors won't be fixed by trying another provider
            if err.kind in (ErrorKind.AUTH, ErrorKind.CLIENT_ERROR):
                break

            # Transient / rate-limit → try next provider
        finally:
            if not reservation_settled:
                try:
                    await rate_repo.rollback(reservation)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "transcription reservation rollback failed",
                        provider=provider_name, exc_info=True,
                    )

    # ── All providers exhausted ──
    last = errors[-1] if errors else {}
    status = status_for_kind(ErrorKind(last["kind"]), 502) if "kind" in last else 503
    raise HTTPException(status, {
        "message": "All transcription providers failed",
        "attempts": errors,
    })
