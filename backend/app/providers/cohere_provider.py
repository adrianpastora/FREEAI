"""Cohere — uses /v2/chat. No streaming yet (could be added with text-generation events)."""
from __future__ import annotations

from typing import Optional

import httpx

from ..schemas import ChatMessage
from .base import BaseProvider, ErrorKind, ProviderError, ProviderResponse, estimate_tokens
from .openai_compat import _phase_timeout


class CohereProvider(BaseProvider):
    name = "cohere"
    BASE_URL = "https://api.cohere.com/v2/chat"
    request_timeout = 60.0

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
        chosen = model or self.default_model or "command-r-08-2024"
        payload: dict = {
            "model": chosen,
            "messages": self._messages_to_dicts(messages),
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            resp = await client.post(
                self.BASE_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=_phase_timeout(self.request_timeout),
            )
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
        self._raise_for_status(resp)
        data = resp.json()
        try:
            parts = data["message"]["content"]
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING
            ) from e
        usage = data.get("usage", {}).get("billed_units", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)
        # Cohere usually returns billed_units, but some endpoints / model
        # families occasionally omit it. Fall back to a length-based estimate
        # so usage_events and tpd_limit don't silently drop to zero.
        if not prompt_tokens:
            prompt_tokens = sum(
                estimate_tokens(m.text_content) for m in messages
            )
        if not completion_tokens:
            completion_tokens = estimate_tokens(text)
        return ProviderResponse(
            content=text,
            model=chosen,
            provider=self.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            raw=data,
        )
