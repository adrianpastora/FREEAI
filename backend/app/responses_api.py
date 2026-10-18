"""OpenAI Responses API (``POST /v1/responses``) on top of the chat orchestrator.

Newer OpenAI SDKs and many clients speak the Responses API instead of Chat
Completions. This module translates in both directions so those clients get
the same routing, fallback and rate-limit handling as ``/v1/chat/completions``:

  request  → ``ChatCompletionRequest`` (input items, instructions,
             max_output_tokens, previous_response_id)
  response ← ``response`` object with one assistant ``message`` output item
  stream   ← the Responses SSE event sequence (response.created …
             response.output_text.delta … response.completed)

Supported: text and image input, ``instructions``, ``previous_response_id``
(kept in an in-process store), streaming. Not supported: tools / function
calling, file inputs, background mode — those return a clear 400.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Optional

from .providers.base import estimate_tokens
from .schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage

# ChatCompletionRequest caps max_tokens; clamp instead of failing with 422.
_MAX_OUTPUT_TOKENS = 32_000


class ResponsesRequestError(ValueError):
    """Client sent something the Responses adapter can't honour (→ HTTP 400)."""


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


# ──────────────── previous_response_id store ────────────────


class ResponseStore:
    """In-process LRU of conversation histories keyed by (user, response id).

    Lets clients chain turns with ``previous_response_id`` instead of
    re-sending the whole conversation. Process-local and bounded: after a
    restart (or on another replica) the id is unknown and the client gets a
    400 asking it to send the full input — same as an expired id on OpenAI.
    """

    def __init__(self, max_items: int = 2_000, ttl_s: float = 6 * 3600):
        self._data: OrderedDict[tuple[int, str], tuple[float, list[ChatMessage]]] = OrderedDict()
        self._max = max_items
        self._ttl = ttl_s

    def put(self, user_id: int, response_id: str, history: list[ChatMessage]) -> None:
        key = (user_id, response_id)
        self._data[key] = (time.monotonic() + self._ttl, history)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def get(self, user_id: int, response_id: str) -> Optional[list[ChatMessage]]:
        entry = self._data.get((user_id, response_id))
        if entry is None:
            return None
        if entry[0] <= time.monotonic():
            self._data.pop((user_id, response_id), None)
            return None
        return list(entry[1])


# ──────────────── request translation ────────────────


def _content_to_chat(content: Any) -> Any:
    """Responses content (str or list of input_* parts) → chat content."""
    if content is None or isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ResponsesRequestError("message content must be a string or a list of parts")
    parts: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            parts.append({"type": "text", "text": part.get("text", "")})
        elif ptype in ("input_image", "image_url"):
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url:
                raise ResponsesRequestError(
                    "input_image needs an image_url (data: URI); file_id is not supported"
                )
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif ptype in ("refusal",):
            parts.append({"type": "text", "text": part.get("refusal", "")})
        else:
            raise ResponsesRequestError(f"unsupported content part type '{ptype}'")
    # Collapse pure-text content to a string: every provider accepts that.
    if all(p["type"] == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


_ROLE_MAP = {"developer": "system", "system": "system", "user": "user", "assistant": "assistant"}


def input_to_messages(input_value: Any) -> list[ChatMessage]:
    """Responses ``input`` (string or list of items) → chat messages."""
    if input_value is None:
        return []
    if isinstance(input_value, str):
        return [ChatMessage(role="user", content=input_value)]
    if not isinstance(input_value, list):
        raise ResponsesRequestError("input must be a string or a list of items")
    messages: list[ChatMessage] = []
    for item in input_value:
        if not isinstance(item, dict):
            raise ResponsesRequestError("input items must be objects")
        itype = item.get("type", "message")
        if itype == "reasoning":
            continue  # opaque reasoning items from other models: safe to drop
        if itype in ("function_call", "function_call_output", "custom_tool_call",
                     "custom_tool_call_output", "computer_call", "computer_call_output"):
            raise ResponsesRequestError(
                "tool / function calling is not supported by FreeAI's /v1/responses"
            )
        if itype != "message":
            raise ResponsesRequestError(f"unsupported input item type '{itype}'")
        role = _ROLE_MAP.get(item.get("role", "user"))
        if role is None:
            raise ResponsesRequestError(f"unsupported role '{item.get('role')}'")
        messages.append(ChatMessage(role=role, content=_content_to_chat(item.get("content"))))
    return messages


def to_chat_request(
    body: dict, history: Optional[list[ChatMessage]] = None,
) -> tuple[ChatCompletionRequest, list[ChatMessage]]:
    """Build the chat request. Returns it plus the conversation (without the
    system instructions) to store for ``previous_response_id`` chaining."""
    if body.get("tools"):
        raise ResponsesRequestError(
            "tools are not supported by FreeAI's /v1/responses — send the request without 'tools'"
        )
    if body.get("background"):
        raise ResponsesRequestError("background mode is not supported")

    conversation = list(history or []) + input_to_messages(body.get("input"))
    if not conversation:
        raise ResponsesRequestError("'input' is required")

    messages = list(conversation)
    instructions = body.get("instructions")
    if instructions:
        messages.insert(0, ChatMessage(role="system", content=str(instructions)))

    max_out = body.get("max_output_tokens")
    if max_out is not None:
        max_out = max(1, min(int(max_out), _MAX_OUTPUT_TOKENS))

    req = ChatCompletionRequest(
        messages=messages,
        model=body.get("model") or None,
        temperature=body["temperature"] if body.get("temperature") is not None else 0.7,
        max_tokens=max_out,
        stream=bool(body.get("stream")),
        **({"strategy": body["strategy"]} if body.get("strategy") else {}),
        **({"preferred_provider": body["preferred_provider"]} if body.get("preferred_provider") else {}),
    )
    return req, conversation


# ──────────────── response objects ────────────────


def _message_item(item_id: str, text: str, status: str = "completed") -> dict:
    return {
        "type": "message",
        "id": item_id,
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}] if status == "completed" or text else [],
    }


def response_object(
    *,
    response_id: str,
    body: dict,
    created_at: int,
    model: str,
    status: str,
    output: list[dict],
    input_tokens: int = 0,
    output_tokens: int = 0,
    error: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> dict:
    obj = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": error,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": body.get("previous_response_id"),
        "reasoning": {"effort": None, "summary": None},
        "store": body.get("store", True),
        "temperature": body.get("temperature", 0.7),
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": body.get("top_p", 1.0),
        "truncation": "disabled",
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": input_tokens + output_tokens,
        },
        "user": body.get("user"),
        "metadata": body.get("metadata") or {},
    }
    if status == "completed":
        # Convenience mirror of the SDK's ``response.output_text`` property.
        obj["output_text"] = "".join(
            part.get("text", "")
            for item in output if item.get("type") == "message"
            for part in item.get("content", []) if part.get("type") == "output_text"
        )
    if extra:
        obj["x_freeai"] = extra
    return obj


def from_chat_response(
    chat: ChatCompletionResponse, *, response_id: str, body: dict,
) -> dict:
    text = chat.choices[0].message.content if chat.choices else ""
    return response_object(
        response_id=response_id,
        body=body,
        created_at=chat.created,
        model=chat.model,
        status="completed",
        output=[_message_item(new_message_id(), text)],
        input_tokens=chat.usage.prompt_tokens,
        output_tokens=chat.usage.completion_tokens,
        extra={
            "provider": chat.provider,
            "real_model": chat.real_model,
            "strategy_used": chat.strategy_used,
            "fallback_chain": chat.fallback_chain,
            "latency_ms": chat.latency_ms,
        },
    )


# ──────────────── streaming ────────────────


def sse_event(event_type: str, payload: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


async def stream_events(
    chunks: AsyncIterator[dict],
    *,
    response_id: str,
    body: dict,
    requested_model: str,
    input_messages: list[ChatMessage],
    on_complete=None,
) -> AsyncIterator[str]:
    """Turn orchestrator SSE chunk dicts into Responses API events.

    ``on_complete(text)`` is called once the full text is known (used to
    store the turn for ``previous_response_id``). Provider errors raised by
    ``chunks`` propagate to the caller, which emits ``response.failed``.
    """
    created_at = int(time.time())
    item_id = new_message_id()
    seq = 0
    model = requested_model
    provider = None
    text_parts: list[str] = []

    def ev(event_type: str, **payload) -> str:
        nonlocal seq
        seq += 1
        return sse_event(event_type, {"type": event_type, "sequence_number": seq, **payload})

    def snapshot(status: str, output: list[dict], **kw) -> dict:
        return response_object(
            response_id=response_id, body=body, created_at=created_at,
            model=model, status=status, output=output, **kw,
        )

    yield ev("response.created", response=snapshot("in_progress", []))
    yield ev("response.in_progress", response=snapshot("in_progress", []))

    started = False
    async for chunk in chunks:
        if chunk.get("model"):
            model = chunk["model"]
        provider = chunk.get("provider") or provider
        choices = chunk.get("choices") or [{}]
        delta = (choices[0].get("delta") or {}).get("content") or ""
        if not started:
            started = True
            yield ev("response.output_item.added", output_index=0,
                     item=_message_item(item_id, "", status="in_progress"))
            yield ev("response.content_part.added", item_id=item_id, output_index=0,
                     content_index=0, part={"type": "output_text", "text": "", "annotations": []})
        if delta:
            text_parts.append(delta)
            yield ev("response.output_text.delta", item_id=item_id, output_index=0,
                     content_index=0, delta=delta)

    text = "".join(text_parts)
    if not started:
        yield ev("response.output_item.added", output_index=0,
                 item=_message_item(item_id, "", status="in_progress"))
        yield ev("response.content_part.added", item_id=item_id, output_index=0,
                 content_index=0, part={"type": "output_text", "text": "", "annotations": []})
    part = {"type": "output_text", "text": text, "annotations": []}
    yield ev("response.output_text.done", item_id=item_id, output_index=0, content_index=0, text=text)
    yield ev("response.content_part.done", item_id=item_id, output_index=0, content_index=0, part=part)
    item = _message_item(item_id, text)
    yield ev("response.output_item.done", output_index=0, item=item)

    if on_complete is not None:
        on_complete(text)
    input_tokens = sum(estimate_tokens(m.text_content) for m in input_messages)
    yield ev("response.completed", response=snapshot(
        "completed", [item],
        input_tokens=input_tokens, output_tokens=estimate_tokens(text),
        extra={"provider": provider},
    ))


def failed_event(response_id: str, body: dict, model: str, error: dict, seq: int = 0) -> str:
    return sse_event("response.failed", {
        "type": "response.failed",
        "sequence_number": seq,
        "response": response_object(
            response_id=response_id, body=body, created_at=int(time.time()),
            model=model, status="failed", output=[], error=error,
        ),
    })
