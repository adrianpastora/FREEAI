"""Shared base for providers that speak the OpenAI chat-completions wire format.
Groq, Mistral, OpenRouter, and HuggingFace router all use this exact shape, so
the body of complete()/stream() lives here once instead of in four near-identical
files."""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from ..schemas import ChatMessage
from .base import BaseProvider, ErrorKind, ProviderError, ProviderResponse, StreamChunk


class OpenAICompatibleProvider(BaseProvider):
    """Subclasses just set BASE_URL, name, supports_streaming, and (optionally)
    override _extra_headers / _build_payload."""

    BASE_URL: str = ""
    supports_streaming = True
    request_timeout: float = 60.0

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _extra_headers(self) -> dict[str, str]:
        return {}

    def _build_payload(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: Optional[int],
        stream: bool,
    ) -> dict:
        payload: dict = {
            "model": model,
            "messages": self._messages_to_dicts(messages),
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _resolve_model(self, model: Optional[str]) -> str:
        chosen = model or self.default_model
        if not chosen:
            raise ProviderError(self.name, "no model specified", kind=ErrorKind.CLIENT_ERROR)
        return chosen

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        client: httpx.AsyncClient,
    ) -> ProviderResponse:
        if not self.api_key:
            raise ProviderError(self.name, "missing API key", kind=ErrorKind.AUTH)
        chosen = self._resolve_model(model)
        payload = self._build_payload(messages, chosen, temperature, max_tokens, stream=False)
        headers = {**self._auth_headers(), **self._extra_headers()}
        try:
            resp = await client.post(
                self.BASE_URL, json=payload, headers=headers, timeout=self.request_timeout
            )
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
        self._raise_for_status(resp)
        try:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING
            ) from e
        usage = data.get("usage", {})
        return ProviderResponse(
            content=content,
            model=data.get("model", chosen),
            provider=self.name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            raw=data,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        client: httpx.AsyncClient,
    ) -> AsyncIterator[StreamChunk]:
        if not self.api_key:
            raise ProviderError(self.name, "missing API key", kind=ErrorKind.AUTH)
        chosen = self._resolve_model(model)
        payload = self._build_payload(messages, chosen, temperature, max_tokens, stream=True)
        headers = {**self._auth_headers(), **self._extra_headers()}
        try:
            async with client.stream(
                "POST",
                self.BASE_URL,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(self.request_timeout, read=None),
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    # Build a fake non-streaming response for the shared error parser
                    fake = httpx.Response(
                        status_code=resp.status_code, headers=resp.headers, content=body
                    )
                    self._raise_for_status(fake)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if payload_str == "[DONE]":
                        return
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    try:
                        choice = chunk["choices"][0]
                    except (KeyError, IndexError):
                        continue
                    delta = choice.get("delta", {}).get("content") or ""
                    finish = choice.get("finish_reason")
                    usage = chunk.get("usage") or {}
                    if delta or finish or usage:
                        yield StreamChunk(
                            delta=delta,
                            provider=self.name,
                            model=chunk.get("model", chosen),
                            finish_reason=finish,
                            prompt_tokens=usage.get("prompt_tokens", 0),
                            completion_tokens=usage.get("completion_tokens", 0),
                        )
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
