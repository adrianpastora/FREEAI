"""Pydantic schemas — OpenAI-compatible chat completion shapes."""
from __future__ import annotations

from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    """OpenAI-compatible message. ``content`` can be a plain string, a list
    of content blocks for multimodal requests, or ``None`` (for assistant
    turns that only emitted tool_calls, or for tool turns mid-conversation)::

        content: [
            {"type": "text", "text": "What's in this image?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ]

    Tool-calling fields (``tool_calls``, ``tool_call_id``) are accepted so a
    full OpenAI-compatible conversation history round-trips without 422s.
    They are currently passed through to providers that natively speak the
    OpenAI wire format; Gemini silently drops them (no tool-calling support
    in that adapter yet).
    """
    model_config = {"extra": "allow"}

    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[Union[str, list[dict[str, Any]]]] = None
    name: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None

    @field_validator("content")
    @classmethod
    def _reject_remote_image_urls(cls, value):
        """Only accept data: URIs for image_url blocks.

        Letting providers fetch arbitrary client-supplied URLs is SSRF — they
        could be pointed at cloud metadata endpoints, internal services, or
        private networks. Clients must inline images as data URIs.
        """
        if not isinstance(value, list):
            return value
        for block in value:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "image_url":
                continue
            url_obj = block.get("image_url") or {}
            url = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
            if url and not url.startswith("data:"):
                raise ValueError(
                    "image_url must be a data: URI; remote URLs are not accepted"
                )
        return value

    @property
    def text_content(self) -> str:
        """Extract plain text from content (works for both str and multimodal)."""
        if self.content is None:
            return ""
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
        if not isinstance(self.content, list):
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
    # Be lenient with OpenAI SDK extras we don't consume yet (tools,
    # tool_choice, response_format, seed, top_p, n, stop, presence_penalty,
    # frequency_penalty, logit_bias, user, ...). Accepting them prevents
    # 422 Unprocessable Entity at the edge when a client rounds-trips a
    # conversation built with the full OpenAI SDK.
    model_config = {"extra": "allow"}

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
