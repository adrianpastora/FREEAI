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
    def _validate_content(cls, value):
        """Validate multimodal content: data-URI only images, sane sizes.

        - Letting providers fetch arbitrary client-supplied URLs is SSRF
          (cloud metadata, internal services, ...). Clients must inline.
        - Per-block and per-message caps so a single request cannot exhaust
          memory or bandwidth when relayed upstream.
        """
        MAX_BLOCKS_PER_MESSAGE = 32
        MAX_IMAGES_PER_MESSAGE = 8
        MAX_TEXT_BLOCK_CHARS = 200_000
        MAX_IMAGE_URL_CHARS = 6_000_000  # ~4.5 MB once base64-decoded

        if isinstance(value, str):
            if len(value) > MAX_TEXT_BLOCK_CHARS:
                raise ValueError(
                    f"content string exceeds {MAX_TEXT_BLOCK_CHARS} chars"
                )
            return value

        if not isinstance(value, list):
            return value

        if len(value) > MAX_BLOCKS_PER_MESSAGE:
            raise ValueError(
                f"too many content blocks (max {MAX_BLOCKS_PER_MESSAGE})"
            )

        image_count = 0
        for block in value:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if len(text) > MAX_TEXT_BLOCK_CHARS:
                    raise ValueError(
                        f"text block exceeds {MAX_TEXT_BLOCK_CHARS} chars"
                    )
            elif btype == "image_url":
                image_count += 1
                if image_count > MAX_IMAGES_PER_MESSAGE:
                    raise ValueError(
                        f"too many image blocks (max {MAX_IMAGES_PER_MESSAGE})"
                    )
                url_obj = block.get("image_url") or {}
                url = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
                if url and not url.startswith("data:"):
                    raise ValueError(
                        "image_url must be a data: URI; remote URLs are not accepted"
                    )
                if len(url) > MAX_IMAGE_URL_CHARS:
                    raise ValueError(
                        f"image_url payload exceeds {MAX_IMAGE_URL_CHARS} chars"
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


# Strategies live in the `strategies` table so users can define custom ones
# at runtime; the orchestrator is the validator and will reject unknown
# names when it tries to resolve the definition.
Strategy = str


class ChatCompletionRequest(BaseModel):
    # Be lenient with OpenAI SDK extras we don't consume yet (tools,
    # tool_choice, response_format, seed, top_p, n, stop, presence_penalty,
    # frequency_penalty, logit_bias, user, ...). Accepting them prevents
    # 422 Unprocessable Entity at the edge when a client rounds-trips a
    # conversation built with the full OpenAI SDK.
    model_config = {"extra": "allow"}

    messages: list[ChatMessage] = Field(..., min_length=1, max_length=200)
    model: Optional[str] = None  # pass-through if set; else provider default_model
    strategy: Strategy = "auto"
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32_000)
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
