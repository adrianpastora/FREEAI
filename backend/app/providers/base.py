"""Base interface for AI providers. All adapters return a normalized ProviderResponse."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional

import httpx

from ..schemas import ChatMessage


class ErrorKind(str, Enum):
    """Classification used by the orchestrator to decide retry/fallback behavior."""
    AUTH = "auth"                 # 401/403 — bad key, disable provider, alert user
    RATE_LIMITED = "rate_limited" # 429 — fall back immediately, NOT a health failure
    CLIENT_ERROR = "client_error" # 4xx other — bad request, propagate to caller
    SERVER_ERROR = "server_error" # 5xx — transient, retry-then-fallback
    NETWORK = "network"           # connection / timeout — transient
    PARSING = "parsing"           # response shape changed — likely a bug
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
        return self.kind in {ErrorKind.SERVER_ERROR, ErrorKind.NETWORK}

    @property
    def is_benign(self) -> bool:
        """Provider is healthy, just don't pick it right now (or ever, for auth)."""
        return self.kind in {ErrorKind.RATE_LIMITED, ErrorKind.CLIENT_ERROR, ErrorKind.AUTH}


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


class BaseProvider:
    """Subclass and implement `complete`. Each adapter handles its own auth/payload shape."""

    name: str = "base"
    supports_streaming: bool = False

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

    def _messages_to_dicts(self, messages: list[ChatMessage]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """Translate an httpx Response into a typed ProviderError."""
        if resp.status_code < 400:
            return
        kind = classify_status(resp.status_code)
        retry_after = parse_retry_after(resp.headers)
        # Try to extract a useful message from the body
        body = resp.text[:500]
        try:
            data = resp.json()
            body = (
                data.get("error", {}).get("message")
                or data.get("message")
                or data.get("detail")
                or body
            )
        except Exception:
            pass
        raise ProviderError(self.name, body, kind=kind, status=resp.status_code, retry_after=retry_after)
