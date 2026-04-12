"""Pydantic schemas — OpenAI-compatible chat completion shapes."""
from __future__ import annotations

from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """OpenAI-compatible message. ``content`` can be a plain string or a list
    of content blocks for multimodal requests::

        content: [
            {"type": "text", "text": "What's in this image?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ]
    """
    role: Literal["system", "user", "assistant", "tool"]
    content: Union[str, list[dict[str, Any]]]
    name: Optional[str] = None

    @property
    def text_content(self) -> str:
        """Extract plain text from content (works for both str and multimodal)."""
        if isinstance(self.content, str):
            return self.content
        return " ".join(
            block.get("text", "")
            for block in self.content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    @property
    def has_images(self) -> bool:
        """True if content contains image_url blocks."""
        if isinstance(self.content, str):
            return False
        return any(
            isinstance(b, dict) and b.get("type") == "image_url"
            for b in self.content
        )


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
    latency_ema_ms: Optional[float] = None
    tags: list[str] = Field(default_factory=list)
    default_model: Optional[str] = None
