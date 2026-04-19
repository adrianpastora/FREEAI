"""Base interface for AI providers. All adapters return a normalized ProviderResponse."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional

import httpx

from ..schemas import ChatMessage


# Scrub secrets that providers sometimes echo back in error bodies. We clip
# to 500 chars upstream; this is the last filter before the message reaches
# a client response or a log.
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)(?:api[_-]?key|authorization|x-api-key)\s*[:=]\s*['\"]?[A-Za-z0-9._\-+/=]{8,}['\"]?"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"(?i)[?&](?:key|api_key|access_token)=[^&\s]+"),
)


def _sanitize_error_message(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


class ErrorKind(str, Enum):
    """Classification used by the orchestrator to decide retry/fallback behavior."""
    AUTH = "auth"                 # 401/403 — bad key, disable provider, alert user
    RATE_LIMITED = "rate_limited" # 429 — fall back immediately, NOT a health failure
    CLIENT_ERROR = "client_error" # 4xx other — bad request, propagate to caller
    SERVER_ERROR = "server_error" # 5xx — transient, retry-then-fallback
    NETWORK = "network"           # connection / timeout — transient
    PARSING = "parsing"           # response shape changed — likely a bug
    EMPTY_RESPONSE = "empty_response"   # 200 OK but content empty/whitespace — fallback
    CONTENT_FILTERED = "content_filtered"  # provider blocked output (safety/filter) — fallback
    UNKNOWN = "unknown"


class ProviderError(Exception):
    def __init__(
        self,
        provider: str,
        message: str,
        kind: ErrorKind = ErrorKind.UNKNOWN,
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(f"[{provider}] {kind.value}: {message}")
        self.provider = provider
        self.kind = kind
        self.status = status
        self.message = message
        self.retry_after = retry_after  # seconds (from Retry-After header if present)

    @property
    def is_transient(self) -> bool:
        # EMPTY_RESPONSE and CONTENT_FILTERED also trigger fallback but — see
        # is_benign — the orchestrator must NOT count them as health failures.
        return self.kind in {
            ErrorKind.SERVER_ERROR,
            ErrorKind.NETWORK,
            ErrorKind.EMPTY_RESPONSE,
            ErrorKind.CONTENT_FILTERED,
        }

    @property
    def is_benign(self) -> bool:
        """Provider is healthy, just don't pick it right now (or ever, for auth)."""
        return self.kind in {
            ErrorKind.RATE_LIMITED,
            ErrorKind.CLIENT_ERROR,
            ErrorKind.AUTH,
            ErrorKind.CONTENT_FILTERED,
        }


def classify_status(status: int) -> ErrorKind:
    if status in (401, 403):
        return ErrorKind.AUTH
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if 400 <= status < 500:
        return ErrorKind.CLIENT_ERROR
    if 500 <= status < 600:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.UNKNOWN


def parse_retry_after(headers: httpx.Headers) -> Optional[float]:
    """Best-effort parse of Retry-After (seconds form only — date form is rare for APIs)."""
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


@dataclass
class ProviderResponse:
    content: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class StreamChunk:
    """One token/delta from a streaming response."""
    delta: str
    provider: str
    model: str
    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class EmbeddingResult:
    """Normalized result of one successful embeddings call.

    ``vectors`` is aligned with the input list — vectors[i] corresponds to
    the i-th input string. ``prompt_tokens`` is the total input-token count
    reported by the provider (completion_tokens is always 0 for embeddings).
    """
    vectors: list[list[float]]
    model: str
    provider: str
    prompt_tokens: int = 0
    raw: dict = field(default_factory=dict)


class BaseProvider:
    """Subclass and implement `complete`. Each adapter handles its own auth/payload shape."""

    name: str = "base"
    supports_streaming: bool = False
    supports_vision: bool = False
    supports_embeddings: bool = False

    def __init__(self, api_key: Optional[str], default_model: Optional[str] = None):
        self.api_key = api_key
        self.default_model = default_model

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        client: httpx.AsyncClient,
    ) -> ProviderResponse:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        client: httpx.AsyncClient,
    ) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError(f"{self.name} does not support streaming")
        yield  # pragma: no cover — make this an async generator for type checkers

    async def embed(
        self,
        texts: list[str],
        *,
        model: Optional[str] = None,
        client: httpx.AsyncClient,
    ) -> EmbeddingResult:
        """Compute embedding vectors for the given input strings.

        Override in subclasses that expose an embeddings endpoint. The input
        is always a list (single-string callers should wrap in a 1-element list);
        output vectors are returned in the same order.
        """
        raise NotImplementedError(f"{self.name} does not support embeddings")

    def _messages_to_dicts(self, messages: list[ChatMessage]) -> list[dict]:
        """Convert ChatMessages to OpenAI-compatible dicts.

        Multimodal content (list of blocks) is passed through as-is — the
        OpenAI API and compatible providers (Groq, OpenRouter, Mistral)
        accept the ``[{"type": "text"}, {"type": "image_url"}]`` format
        natively. Tool-calling fields are forwarded when present so multi-turn
        histories with tool_calls round-trip correctly.
        """
        out: list[dict] = []
        for m in messages:
            d: dict = {"role": m.role}
            # OpenAI spec: content is required but MAY be null for assistant
            # turns that only emit tool_calls. Preserve the null so upstreams
            # see a valid conversation shape.
            d["content"] = m.content
            if m.name:
                d["name"] = m.name
            if m.tool_calls:
                d["tool_calls"] = m.tool_calls
            if m.tool_call_id:
                d["tool_call_id"] = m.tool_call_id
            out.append(d)
        return out

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """Translate an httpx Response into a typed ProviderError."""
        if resp.status_code < 400:
            return
        kind = classify_status(resp.status_code)
        retry_after = parse_retry_after(resp.headers)
        # Try to extract a useful message from the body. Some providers
        # return HTML or malformed JSON on errors; fall back to the raw
        # text in that case.
        body = resp.text[:500]
        try:
            data = resp.json()
            body = (
                data.get("error", {}).get("message")
                or data.get("message")
                or data.get("detail")
                or body
            )
        except (ValueError, AttributeError):
            pass
        raise ProviderError(
            self.name,
            _sanitize_error_message(body),
            kind=kind,
            status=resp.status_code,
            retry_after=retry_after,
        )
