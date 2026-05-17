"""Shared base for providers that speak the OpenAI chat-completions wire format.
Groq, Mistral, OpenRouter, and HuggingFace router all use this exact shape, so
the body of complete()/stream() lives here once instead of in four near-identical
files."""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from ..schemas import ChatMessage
from .base import BaseProvider, ErrorKind, ProviderError, ProviderResponse, StreamChunk, estimate_tokens

# Tolerate a few malformed SSE frames before bailing out — providers occasionally
# emit truncated lines under network pressure. Reset the counter on every
# successfully parsed chunk so isolated glitches don't accumulate.
_MAX_PARSE_ERRORS = 5

# Connect phase is bounded tight: a slow DNS / TLS handshake means the upstream
# is unreachable, and we'd rather fall back to another provider than hang on
# httpx's default per-phase budget of `request_timeout`.
_CONNECT_TIMEOUT = 5.0


def _phase_timeout(read: float) -> httpx.Timeout:
    """Build a per-phase Timeout: short connect, long read, sane write/pool."""
    return httpx.Timeout(connect=_CONNECT_TIMEOUT, read=read, write=30.0, pool=_CONNECT_TIMEOUT)


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
                self.BASE_URL, json=payload, headers=headers,
                timeout=_phase_timeout(self.request_timeout),
            )
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
        self._raise_for_status(resp)
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING
            ) from e
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter":
            raise ProviderError(
                self.name,
                "content filtered (finish_reason=content_filter)",
                kind=ErrorKind.CONTENT_FILTERED,
            )
        # content may be legitimately empty when the model chose to emit
        # tool_calls instead — only treat as failure when there are none.
        has_tool_calls = bool(message.get("tool_calls"))
        if (not content or not content.strip()) and not has_tool_calls:
            raise ProviderError(
                self.name,
                f"empty response (finish_reason={finish_reason or 'unspecified'})",
                kind=ErrorKind.EMPTY_RESPONSE,
            )
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        # Some OpenAI-compatible gateways (notably a few OpenRouter :free
        # routes) omit the usage block. Estimate from text length so usage
        # accounting and tpd_limit don't silently drop to zero.
        if not prompt_tokens:
            prompt_tokens = sum(estimate_tokens(m.text_content) for m in messages)
        if not completion_tokens and content:
            completion_tokens = estimate_tokens(content)
        return ProviderResponse(
            content=content or "",
            model=data.get("model", chosen),
            provider=self.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
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
                timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=None, write=30.0, pool=_CONNECT_TIMEOUT),
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    # Build a fake non-streaming response for the shared error parser
                    fake = httpx.Response(
                        status_code=resp.status_code, headers=resp.headers, content=body
                    )
                    self._raise_for_status(fake)
                parse_errors = 0
                saw_content = False
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if payload_str == "[DONE]":
                        return
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        if parse_errors >= _MAX_PARSE_ERRORS:
                            raise ProviderError(
                                self.name,
                                f"stream emitted {_MAX_PARSE_ERRORS} unparseable frames",
                                kind=ErrorKind.PARSING,
                            )
                        continue
                    try:
                        choice = chunk["choices"][0]
                    except (KeyError, IndexError):
                        continue
                    parse_errors = 0
                    delta = choice.get("delta", {}).get("content") or ""
                    finish = choice.get("finish_reason")
                    # Bail out to fallback only if the provider filtered before
                    # we emitted anything useful. After first delta, propagate
                    # the finish_reason so the caller knows what happened.
                    if finish == "content_filter" and not saw_content:
                        raise ProviderError(
                            self.name,
                            "content filtered mid-stream before any delta",
                            kind=ErrorKind.CONTENT_FILTERED,
                        )
                    usage = chunk.get("usage") or {}
                    if delta or finish or usage:
                        if delta:
                            saw_content = True
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
