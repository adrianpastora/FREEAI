"""OpenAI-compatible audio transcription with multi-provider fallback.

Each transcription-capable provider implements ``BaseProvider.transcribe``;
this router runs the same reserve → attempt → commit loop used elsewhere,
falling through to the next provider on transient errors and stopping on
auth/client errors.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..logging_config import get_logger
from ..providers import (
    PROVIDER_REGISTRY,
    AudioInput,
    BaseProvider,
    ErrorKind,
    ProviderError,
    TranscriptionResult,
)
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
    resolve_content_type,
    supports_transcription,
)
from ._common import MAX_BODY_BYTES_AUDIO, require_user_id, status_for_kind

router = APIRouter(tags=["audio"])
log = get_logger("freeai.transcriptions")


def _quarantine_for(kind: ErrorKind) -> Optional[int]:
    if kind == ErrorKind.SERVER_ERROR:
        return 60
    if kind == ErrorKind.NETWORK:
        return 30
    return None


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

    Tries providers in ``TRANSCRIPTION_PROVIDERS`` order, skipping any the
    user hasn't enabled or that have run out of capacity. On success
    returns ``{"text": ..., "provider": ..., "model": ...}``; on auth or
    client errors fails fast; on transient errors falls through to the
    next provider.
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
        up = next((p for p in user_providers if p.provider_name == name), None)
        if up and up.api_key and up.enabled:
            candidates.append((name, up.to_provider_config()))

    if not candidates:
        raise HTTPException(
            400,
            "No transcription provider configured — add an API key for Groq or Gemini",
        )

    http_client = request.app.state.orchestrator.http_client
    errors: list[dict] = []  # one entry per attempted provider, for diagnostics
    fallback_position = 0

    for provider_name, dto in candidates:
        fallback_position += 1

        reservation = await rate_repo.try_reserve(
            user_id, provider_name, dto.rpm_limit, dto.rpd_limit,
        )
        if reservation is None:
            errors.append({"provider": provider_name, "skipped": "at capacity"})
            continue

        provider_cls = PROVIDER_REGISTRY[provider_name]
        provider: BaseProvider = provider_cls(
            api_key=dto.api_key, default_model=dto.default_model,
        )

        reservation_settled = False
        try:
            try:
                result: TranscriptionResult = await provider.transcribe(
                    audio, client=http_client,
                )
            except ProviderError as err:
                errors.append({
                    "provider": err.provider,
                    "kind": err.kind.value,
                    "message": err.message[:200],
                })
                await rate_repo.commit(
                    reservation, 0, ok=False,
                    error=err.message, error_kind=err.kind.value,
                    quarantine_seconds=_quarantine_for(err.kind),
                )
                reservation_settled = True
                await usage_repo.record(UsageEvent(
                    provider=err.provider, model=None,
                    strategy="transcription", outcome=err.kind.value,
                    latency_ms=0, client_hash=client_hash,
                    user_id=user_id, fallback_position=fallback_position,
                ))
                # Auth/client errors won't be fixed by trying another provider.
                if err.kind in (ErrorKind.AUTH, ErrorKind.CLIENT_ERROR):
                    break
                continue
        finally:
            if not reservation_settled:
                try:
                    await rate_repo.rollback(reservation)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "transcription reservation rollback failed",
                        provider=provider_name, exc_info=True,
                    )

        # ── Success ──
        await rate_repo.commit(reservation, result.latency_ms, ok=True)
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

    # ── All providers exhausted ──
    last = errors[-1] if errors else {}
    status = status_for_kind(ErrorKind(last["kind"]), 502) if "kind" in last else 503
    raise HTTPException(status, {
        "message": "All transcription providers failed",
        "attempts": errors,
    })
