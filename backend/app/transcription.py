"""Audio transcription with multi-provider fallback.

Supported providers (in default priority order):
  1. Groq  — OpenAI-compatible Whisper endpoint, fastest inference.
  2. Gemini — sends audio inline (base64) to generateContent with a
             transcription prompt. Slower but very accurate.

Each provider has a `transcribe()` function with the same signature and
return type, making them interchangeable for the fallback loop.

Architecture
────────────
The endpoint in main.py calls `transcribe_with_fallback()`, which:
  1. Collects enabled providers that support transcription.
  2. Tries each one in priority order, reserving capacity first.
  3. On success, commits the reservation and returns the result.
  4. On transient failure, commits the error, logs it, and moves on.
  5. On auth/client error, stops (nothing else will help).

This follows the same reserve → attempt → commit pattern used by the
chat orchestrator, keeping error classification and quarantine logic
consistent across the codebase.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .providers.base import ErrorKind, classify_status, parse_retry_after


# ─────────────────────────── shared types ───────────────────────────


@dataclass
class TranscriptionResult:
    """Normalized result returned by every transcription provider."""
    text: str
    provider: str
    model: str
    latency_ms: int


@dataclass
class TranscriptionError:
    """Structured error from a failed transcription attempt."""
    provider: str
    model: str
    kind: ErrorKind
    message: str
    latency_ms: int
    retry_after: Optional[float] = None


@dataclass
class AudioInput:
    """Pre-read audio file ready to be sent to any provider."""
    file_bytes: bytes
    filename: str
    content_type: str
    language: Optional[str] = None


# ─────────────────────────── constants ──────────────────────────────

# Provider priority for transcription. First available wins.
TRANSCRIPTION_PROVIDERS = ["groq", "gemini"]

# Groq Whisper config
_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_MODEL = "whisper-large-v3-turbo"
_GROQ_TIMEOUT = 120.0

# Gemini config
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_TIMEOUT = 120.0
_GEMINI_PROMPT = (
    "Generate a verbatim transcript of the speech in this audio. "
    "Output ONLY the transcript text, no timestamps, no speaker labels, "
    "no commentary."
)

# MIME types we accept (superset of what both providers support)
AUDIO_MIMETYPES = {
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "webm": "audio/webm",
    "m4a": "audio/m4a",
    "flac": "audio/flac",
    "mpeg": "audio/mpeg",
    "mpga": "audio/mpeg",
    "aac": "audio/aac",
    "aiff": "audio/aiff",
}

# Gemini accepts these MIME types for inline audio
_GEMINI_AUDIO_MIMES = {"audio/ogg", "audio/mpeg", "audio/wav", "audio/flac",
                       "audio/aac", "audio/aiff", "audio/mp3", "audio/webm"}


# ─────────────────────── provider functions ─────────────────────────


async def _transcribe_groq(
    audio: AudioInput, api_key: str
) -> TranscriptionResult | TranscriptionError:
    """Send audio to Groq's OpenAI-compatible Whisper endpoint.

    Uses multipart/form-data, same wire format as OpenAI's
    /v1/audio/transcriptions.
    """
    files = {"file": (audio.filename, audio.file_bytes, audio.content_type)}
    data: dict[str, str] = {"model": _GROQ_MODEL}
    if audio.language:
        data["language"] = audio.language

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files=files,
                data=data,
                timeout=_GROQ_TIMEOUT,
            )
    except httpx.TimeoutException:
        return TranscriptionError(
            provider="groq", model=_GROQ_MODEL, kind=ErrorKind.NETWORK,
            message="timeout", latency_ms=_elapsed(started),
        )
    except httpx.HTTPError as exc:
        return TranscriptionError(
            provider="groq", model=_GROQ_MODEL, kind=ErrorKind.NETWORK,
            message=str(exc), latency_ms=_elapsed(started),
        )

    latency_ms = _elapsed(started)

    if resp.status_code >= 400:
        kind = classify_status(resp.status_code)
        return TranscriptionError(
            provider="groq", model=_GROQ_MODEL, kind=kind,
            message=resp.text[:500], latency_ms=latency_ms,
            retry_after=parse_retry_after(resp.headers),
        )

    # Groq returns {"text": "..."} on success
    try:
        text = resp.json()["text"]
    except (KeyError, ValueError):
        return TranscriptionError(
            provider="groq", model=_GROQ_MODEL, kind=ErrorKind.PARSING,
            message=f"unexpected response: {resp.text[:300]}", latency_ms=latency_ms,
        )

    return TranscriptionResult(
        text=text, provider="groq", model=_GROQ_MODEL, latency_ms=latency_ms,
    )


async def _transcribe_gemini(
    audio: AudioInput, api_key: str
) -> TranscriptionResult | TranscriptionError:
    """Send audio to Gemini's generateContent endpoint with a transcription prompt.

    Gemini doesn't have a dedicated Whisper-style endpoint. Instead we
    send the audio as inline base64 data alongside a text prompt asking
    for a verbatim transcript. The model returns plain text.

    Authentication: Gemini uses the API key as a query parameter.
    """
    # Gemini expects a MIME type it recognises; fall back to audio/mpeg
    mime = audio.content_type
    if mime not in _GEMINI_AUDIO_MIMES:
        mime = "audio/mpeg"

    audio_b64 = base64.standard_b64encode(audio.file_bytes).decode("ascii")

    prompt = _GEMINI_PROMPT
    if audio.language:
        prompt += f" The audio is in {audio.language}."

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": mime, "data": audio_b64}},
            ],
        }],
        "generationConfig": {"temperature": 0.0},
    }

    url = f"{_GEMINI_URL}/{_GEMINI_MODEL}:generateContent?key={api_key}"

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(url, json=payload, timeout=_GEMINI_TIMEOUT)
    except httpx.TimeoutException:
        return TranscriptionError(
            provider="gemini", model=_GEMINI_MODEL, kind=ErrorKind.NETWORK,
            message="timeout", latency_ms=_elapsed(started),
        )
    except httpx.HTTPError as exc:
        return TranscriptionError(
            provider="gemini", model=_GEMINI_MODEL, kind=ErrorKind.NETWORK,
            message=str(exc), latency_ms=_elapsed(started),
        )

    latency_ms = _elapsed(started)

    if resp.status_code >= 400:
        kind = classify_status(resp.status_code)
        return TranscriptionError(
            provider="gemini", model=_GEMINI_MODEL, kind=kind,
            message=resp.text[:500], latency_ms=latency_ms,
            retry_after=parse_retry_after(resp.headers),
        )

    # Gemini returns {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError):
        return TranscriptionError(
            provider="gemini", model=_GEMINI_MODEL, kind=ErrorKind.PARSING,
            message=f"unexpected response: {resp.text[:300]}", latency_ms=latency_ms,
        )

    return TranscriptionResult(
        text=text.strip(), provider="gemini", model=_GEMINI_MODEL,
        latency_ms=latency_ms,
    )


# ─────────────────────── provider dispatch ──────────────────────────

# Maps provider name → transcription function. Order doesn't matter
# here; priority is controlled by TRANSCRIPTION_PROVIDERS.
_TRANSCRIBE_FN = {
    "groq": _transcribe_groq,
    "gemini": _transcribe_gemini,
}


def supports_transcription(provider_name: str) -> bool:
    """Return True if the provider has a transcription implementation."""
    return provider_name in _TRANSCRIBE_FN


async def transcribe(
    provider_name: str, audio: AudioInput, api_key: str
) -> TranscriptionResult | TranscriptionError:
    """Dispatch to the right provider's transcription function."""
    fn = _TRANSCRIBE_FN.get(provider_name)
    if not fn:
        return TranscriptionError(
            provider=provider_name, model="unknown", kind=ErrorKind.CLIENT_ERROR,
            message=f"{provider_name} does not support transcription", latency_ms=0,
        )
    return await fn(audio, api_key)


# ─────────────────────── helpers ────────────────────────────────────


def _elapsed(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def resolve_content_type(filename: str, fallback: Optional[str] = None) -> str:
    """Determine MIME type from file extension."""
    ext = (filename or "audio.ogg").rsplit(".", 1)[-1].lower()
    return AUDIO_MIMETYPES.get(ext, fallback or "application/octet-stream")
