"""Google Gemini — uses the v1beta generateContent / streamGenerateContent endpoints.

Supports multimodal (vision) requests: OpenAI-format ``image_url`` content
blocks are translated to Gemini's ``inlineData`` part format.
"""
from __future__ import annotations

import base64
import json
import re
from typing import AsyncIterator, Optional

import httpx

from ..schemas import ChatMessage
from .base import BaseProvider, EmbeddingResult, ErrorKind, ProviderError, ProviderResponse, StreamChunk

# Tolerate a few malformed SSE frames before failing the whole stream.
_MAX_PARSE_ERRORS = 5

# Matches data URIs: data:image/png;base64,iVBOR...
_DATA_URI_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)

# Gemini finishReason values that mean "no usable content was emitted." The
# orchestrator treats these as CONTENT_FILTERED so another provider is tried.
_GEMINI_BLOCKED_FINISH = {
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"
}


class GeminiProvider(BaseProvider):
    name = "gemini"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    DEFAULT_EMBEDDING_MODEL = "text-embedding-004"
    supports_streaming = True
    supports_vision = True
    supports_embeddings = True
    request_timeout = 60.0

    def _content_to_parts(self, content) -> list[dict]:
        """Convert ChatMessage.content (str, multimodal list, or None) to Gemini parts."""
        if content is None:
            # Empty content (e.g. an assistant turn that only emitted tool_calls
            # in the original OpenAI-format history) — Gemini doesn't support
            # tool_calls in this adapter, so surface as an empty text part.
            return [{"text": ""}]
        if isinstance(content, str):
            return [{"text": content}]

        parts: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append({"text": text})
            elif btype == "image_url":
                image_url_obj = block.get("image_url", {})
                url = image_url_obj.get("url", "") if isinstance(image_url_obj, dict) else ""
                if not url:
                    continue
                # data URI → inlineData
                match = _DATA_URI_RE.match(url)
                if match:
                    mime_type, b64_data = match.group(1), match.group(2)
                    parts.append({
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": b64_data,
                        }
                    })
                else:
                    # HTTP(S) URL → fileData (Gemini fetches it)
                    parts.append({
                        "fileData": {
                            "mimeType": "image/jpeg",
                            "fileUri": url,
                        }
                    })
        return parts or [{"text": ""}]

    def _to_gemini_contents(self, messages: list[ChatMessage]) -> tuple[list[dict], Optional[str]]:
        # Gemini uses "user"/"model" roles and a separate systemInstruction.
        system_text: Optional[str] = None
        contents: list[dict] = []
        for m in messages:
            if m.role == "system":
                system_text = (system_text + "\n" if system_text else "") + m.text_content
                continue
            role = "model" if m.role == "assistant" else "user"
            contents.append({"role": role, "parts": self._content_to_parts(m.content)})
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
            candidate = data["candidates"][0]
        except (KeyError, IndexError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING
            ) from e
        finish_reason = candidate.get("finishReason")
        if finish_reason in _GEMINI_BLOCKED_FINISH:
            raise ProviderError(
                self.name,
                f"content filtered (finishReason={finish_reason})",
                kind=ErrorKind.CONTENT_FILTERED,
            )
        try:
            parts = candidate["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING
            ) from e
        if not text or not text.strip():
            raise ProviderError(
                self.name,
                f"empty response (finishReason={finish_reason or 'unspecified'})",
                kind=ErrorKind.EMPTY_RESPONSE,
            )
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
            async with client.stream("POST", url, json=payload, timeout=httpx.Timeout(self.request_timeout, read=None)) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
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
                    if not payload_str:
                        continue
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
                    parse_errors = 0
                    try:
                        parts = chunk["candidates"][0]["content"]["parts"]
                        text = "".join(p.get("text", "") for p in parts)
                    except (KeyError, IndexError, TypeError):
                        text = ""
                    finish = chunk.get("candidates", [{}])[0].get("finishReason")
                    if finish in _GEMINI_BLOCKED_FINISH and not saw_content:
                        raise ProviderError(
                            self.name,
                            f"content filtered mid-stream (finishReason={finish})",
                            kind=ErrorKind.CONTENT_FILTERED,
                        )
                    if text or finish:
                        if text:
                            saw_content = True
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

    async def embed(
        self,
        texts: list[str],
        *,
        model: Optional[str] = None,
        client: httpx.AsyncClient,
    ) -> EmbeddingResult:
        """Gemini's batchEmbedContents takes a list of requests and returns
        an aligned list of embeddings. Each request specifies its own model,
        even though they're all the same here.
        """
        if not self.api_key:
            raise ProviderError(self.name, "missing API key", kind=ErrorKind.AUTH)
        chosen = model or self.DEFAULT_EMBEDDING_MODEL
        # Gemini's embedding model names use the "models/" prefix in the body
        qualified = chosen if chosen.startswith("models/") else f"models/{chosen}"
        payload = {
            "requests": [
                {"model": qualified, "content": {"parts": [{"text": t}]}}
                for t in texts
            ],
        }
        url = f"{self.BASE_URL}/{chosen}:batchEmbedContents?key={self.api_key}"
        try:
            resp = await client.post(url, json=payload, timeout=self.request_timeout)
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
        self._raise_for_status(resp)
        try:
            data = resp.json()
            # Shape: {"embeddings": [{"values": [...]}, {"values": [...]}, ...]}
            vectors = [e["values"] for e in data["embeddings"]]
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING,
            ) from e
        # Gemini doesn't report token counts for embeddings — leave at 0.
        return EmbeddingResult(
            vectors=vectors,
            model=chosen,
            provider=self.name,
            prompt_tokens=0,
            raw=data,
        )
