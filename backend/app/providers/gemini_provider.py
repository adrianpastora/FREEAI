"""Google Gemini — uses the v1beta generateContent / streamGenerateContent endpoints."""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from ..schemas import ChatMessage
from .base import BaseProvider, ErrorKind, ProviderError, ProviderResponse, StreamChunk


class GeminiProvider(BaseProvider):
    name = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1/models"
    supports_streaming = True
    request_timeout = 60.0

    def _to_gemini_contents(self, messages: list[ChatMessage]) -> tuple[list[dict], Optional[str]]:
        # Gemini uses "user"/"model" roles and a separate systemInstruction.
        system_text: Optional[str] = None
        contents: list[dict] = []
        for m in messages:
            if m.role == "system":
                system_text = (system_text + "\n" if system_text else "") + m.content
                continue
            role = "model" if m.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m.content}]})
        return contents, system_text

    def _build_payload(
        self,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: Optional[int],
    ) -> dict:
        contents, system_text = self._to_gemini_contents(messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature},
        }
        if max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = max_tokens
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        return payload

    def _resolve_model(self, model: Optional[str]) -> str:
        return model or self.default_model or "gemini-2.5-flash"

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
        payload = self._build_payload(messages, temperature, max_tokens)
        url = f"{self.BASE_URL}/{chosen}:generateContent?key={self.api_key}"
        try:
            resp = await client.post(url, json=payload, timeout=self.request_timeout)
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
        self._raise_for_status(resp)
        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING
            ) from e
        usage = data.get("usageMetadata", {})
        return ProviderResponse(
            content=text,
            model=chosen,
            provider=self.name,
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
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
        payload = self._build_payload(messages, temperature, max_tokens)
        url = f"{self.BASE_URL}/{chosen}:streamGenerateContent?alt=sse&key={self.api_key}"
        try:
            async with client.stream("POST", url, json=payload, timeout=self.request_timeout) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    fake = httpx.Response(
                        status_code=resp.status_code, headers=resp.headers, content=body
                    )
                    self._raise_for_status(fake)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if not payload_str:
                        continue
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    try:
                        parts = chunk["candidates"][0]["content"]["parts"]
                        text = "".join(p.get("text", "") for p in parts)
                    except (KeyError, IndexError):
                        text = ""
                    finish = chunk.get("candidates", [{}])[0].get("finishReason")
                    if text or finish:
                        yield StreamChunk(
                            delta=text,
                            provider=self.name,
                            model=chosen,
                            finish_reason=finish.lower() if finish else None,
                        )
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
