"""Pydantic schemas — OpenAI-compatible chat completion shapes."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: Optional[str] = None


# `Strategy` used to be a `Literal[...]` enumerating the builtin names. In
# Sprint 3 strategies became data (a Postgres table) so users can create
# custom names at runtime, but this alias was not updated — any custom name
# was rejected by pydantic at the /v1/chat/completions boundary. See
# docs/REVIEW.md § 6.4. Now it's a plain string alias for readability; the
# orchestrator is the validator: it looks up the strategy row and errors if
# the name doesn't resolve.
Strategy = str


class ChatCompletionRequest(BaseModel):
    messages: list[ChatMessage]
    model: Optional[str] = None  # pass-through if set; else provider default_model
    strategy: Strategy = "auto"
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    stream: bool = False
    # advanced
    preferred_provider: Optional[str] = None
    fallback: bool = True


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str                       # virtual model id (e.g. "freeai-fast") or real model
    provider: str
    strategy_used: str
    choices: list[Choice]
    usage: Usage
    latency_ms: int
    fallback_chain: list[str] = Field(default_factory=list)
    real_model: Optional[str] = None  # actual provider model used (set when virtual)


class ProviderStatus(BaseModel):
    name: str
    enabled: bool
    has_key: bool
    healthy: bool
    requests_today: int
    requests_this_minute: int
    rpm_limit: Optional[int]
    rpd_limit: Optional[int]
    tpd_limit: Optional[int] = None
    tokens_today: int = 0
    weight: float = 1.0
    last_error: Optional[str] = None
    last_latency_ms: Optional[int] = None
    tags: list[str] = Field(default_factory=list)
    default_model: Optional[str] = None
