"""Groq — OpenAI-compatible chat + Whisper-style audio transcription.

Free tier exposes very fast Llama / Mixtral inference for chat and a
Whisper-large endpoint for audio. Both run on the same API key.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from .base import (
    AudioInput,
    BaseProvider,
    ErrorKind,
    ProviderError,
    TranscriptionResult,
    classify_status,
    parse_retry_after,
)
from .openai_compat import OpenAICompatibleProvider


_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_DEFAULT_TRANSCRIPTION_MODEL = "whisper-large-v3-turbo"
_TRANSCRIPTION_TIMEOUT = 120.0


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    request_timeout = 60.0
    supports_transcription = True

    async def transcribe(
        self,
        audio: AudioInput,
        *,
        model: Optional[str] = None,
        client: httpx.AsyncClient,
    ) -> TranscriptionResult:
        if not self.api_key:
            raise ProviderError(self.name, "missing API key", kind=ErrorKind.AUTH)
        chosen = model or _DEFAULT_TRANSCRIPTION_MODEL
        files = {"file": (audio.filename, audio.file_bytes, audio.content_type)}
        data: dict[str, str] = {"model": chosen}
        if audio.language:
            data["language"] = audio.language

        started = time.perf_counter()
        try:
            resp = await client.post(
                _TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
                timeout=_TRANSCRIPTION_TIMEOUT,
            )
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e

        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code >= 400:
            raise ProviderError(
                self.name,
                resp.text[:500],
                kind=classify_status(resp.status_code),
                status=resp.status_code,
                retry_after=parse_retry_after(resp.headers),
            )

        try:
            data_json = resp.json()
            text = data_json["text"]
        except (KeyError, ValueError) as e:
            raise ProviderError(
                self.name,
                f"unexpected response: {resp.text[:300]}",
                kind=ErrorKind.PARSING,
            ) from e

        return TranscriptionResult(
            text=text,
            model=chosen,
            provider=self.name,
            latency_ms=latency_ms,
            raw=data_json,
        )
