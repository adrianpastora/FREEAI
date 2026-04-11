"""Cohere — uses /v2/chat. No streaming yet (could be added with text-generation events)."""
from __future__ import annotations

from typing import Optional

import httpx

from ..schemas import ChatMessage
from .base import BaseProvider, ErrorKind, ProviderError, ProviderResponse


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
                timeout=self.request_timeout,
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
        return ProviderResponse(
            content=text,
            model=chosen,
            provider=self.name,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            raw=data,
        )
