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
(kept in an in-process store), streaming, and function calling (``tools`` of
type ``function``, ``function_call`` / ``function_call_output`` items).
Built-in OpenAI tools (web_search, file_search, …) can't be emulated and are
dropped. Not supported: file inputs, background mode — those return a 400.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Callable, Optional

from .logging_config import get_logger
from .providers.base import estimate_tokens
from .schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage

log = get_logger("freeai.responses")

# ChatCompletionRequest caps max_tokens; clamp instead of failing with 422.
_MAX_OUTPUT_TOKENS = 32_000


class ResponsesRequestError(ValueError):
    """Client sent something the Responses adapter can't honour (→ HTTP 400)."""


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def new_function_call_id() -> str:
    return f"fc_{uuid.uuid4().hex}"


def _tool_output_text(output: Any) -> str:
    """function_call_output.output may be a string or a list of parts."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(
            p.get("text", "") for p in output if isinstance(p, dict)
        ) or json.dumps(output)
    return json.dumps(output)


def tools_to_chat(tools: Any) -> Optional[list[dict]]:
    """Responses ``tools`` → chat-completions ``tools``.

    Only ``function`` tools can be served by other providers; built-in
    OpenAI tools (web_search, file_search, code_interpreter, mcp, …) are
    dropped so the request still works, just without them.
    """
    if not tools:
        return None
    if not isinstance(tools, list):
        raise ResponsesRequestError("'tools' must be a list")
    out: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        if "function" in tool:  # already chat-shaped
            out.append({"type": "function", "function": tool["function"]})
            continue
        fn: dict = {"name": tool.get("name", "")}
        if tool.get("description"):
            fn["description"] = tool["description"]
        fn["parameters"] = tool.get("parameters") or {"type": "object", "properties": {}}
        if tool.get("strict") is not None:
            fn["strict"] = tool["strict"]
        out.append({"type": "function", "function": fn})
    return out or None


def tool_choice_to_chat(choice: Any) -> Any:
    if choice is None or isinstance(choice, str):
        return choice  # "auto" | "none" | "required"
    if isinstance(choice, dict) and choice.get("type") == "function":
        name = choice.get("name") or (choice.get("function") or {}).get("name")
        return {"type": "function", "function": {"name": name}}
    return "auto"  # built-in tool choices: nothing equivalent, let the model decide


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

    # Individual items (msg_…, fc_…) for ``item_reference`` input items,
    # which point at items from earlier responses / requests by id.
    _ITEMS_MAX = 20_000

    def put_items(self, user_id: int, items: list[dict]) -> None:
        store = self.__dict__.setdefault("_items", OrderedDict())
        expires = time.monotonic() + self._ttl
        for item in items:
            item_id = item.get("id") if isinstance(item, dict) else None
            if not item_id or item.get("type") == "item_reference":
                continue
            store[(user_id, item_id)] = (expires, item)
            store.move_to_end((user_id, item_id))
        while len(store) > self._ITEMS_MAX:
            store.popitem(last=False)

    def get_item(self, user_id: int, item_id: str) -> Optional[dict]:
        store = self.__dict__.get("_items")
        entry = store.get((user_id, item_id)) if store else None
        if entry is None:
            return None
        if entry[0] <= time.monotonic():
            store.pop((user_id, item_id), None)
            return None
        return entry[1]


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


def input_to_messages(
    input_value: Any, resolve: Optional[Callable[[str], Optional[dict]]] = None,
) -> list[ChatMessage]:
    """Responses ``input`` (string or list of items) → chat messages.

    ``resolve(item_id)`` looks up ``item_reference`` items. Unresolvable
    references and item types with no chat equivalent (built-in tool call
    history, reasoning) are dropped with a log line rather than failing the
    whole request.
    """
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
        if item.get("type") == "item_reference":
            ref_id = item.get("id")
            resolved = resolve(ref_id) if (resolve and ref_id) else None
            if resolved is None:
                log.warning("item_reference not found, dropping", item_id=ref_id)
                continue
            item = resolved
        _append_item(messages, item)
    return messages


def _append_item(messages: list[ChatMessage], item: dict) -> None:
    itype = item.get("type", "message")
    if itype == "function_call":
        call = {
            "id": item.get("call_id") or item.get("id") or new_function_call_id(),
            "type": "function",
            "function": {
                "name": item.get("name", ""),
                "arguments": item.get("arguments") or "{}",
            },
        }
        # Consecutive calls belong to one assistant turn.
        last = messages[-1] if messages else None
        if last is not None and last.role == "assistant" and last.tool_calls:
            last.tool_calls.append(call)
        else:
            messages.append(ChatMessage(role="assistant", content=None, tool_calls=[call]))
        return
    if itype == "function_call_output":
        messages.append(ChatMessage(
            role="tool",
            tool_call_id=item.get("call_id"),
            content=_tool_output_text(item.get("output")),
        ))
        return
    if itype != "message":
        # reasoning, web_search_call, file_search_call, computer_call, …:
        # no equivalent other providers understand.
        log.info("dropping unsupported input item", item_type=itype)
        return
    role = _ROLE_MAP.get(item.get("role", "user"))
    if role is None:
        raise ResponsesRequestError(f"unsupported role '{item.get('role')}'")
    messages.append(ChatMessage(role=role, content=_content_to_chat(item.get("content"))))


def _repair_tool_pairs(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Turn orphan tool results into plain user text.

    A ``tool`` message whose call isn't in a preceding assistant turn (e.g.
    its ``function_call`` was an item_reference we could no longer resolve)
    makes every provider reject the request; as text it still gives the
    model the information.
    """
    known: set[str] = set()
    out: list[ChatMessage] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            known.update(tc.get("id", "") for tc in m.tool_calls)
        if m.role == "tool" and (m.tool_call_id or "") not in known:
            out.append(ChatMessage(
                role="user",
                content=f"[tool result for call {m.tool_call_id}]\n{m.text_content}",
            ))
            continue
        out.append(m)
    return out


def to_chat_request(
    body: dict,
    history: Optional[list[ChatMessage]] = None,
    resolve: Optional[Callable[[str], Optional[dict]]] = None,
) -> tuple[ChatCompletionRequest, list[ChatMessage]]:
    """Build the chat request. Returns it plus the conversation (without the
    system instructions) to store for ``previous_response_id`` chaining."""
    if body.get("background"):
        raise ResponsesRequestError("background mode is not supported")

    conversation = _repair_tool_pairs(
        list(history or []) + input_to_messages(body.get("input"), resolve)
    )
    if not conversation:
        raise ResponsesRequestError("'input' is required")

    messages = list(conversation)
    instructions = body.get("instructions")
    if instructions:
        messages.insert(0, ChatMessage(role="system", content=str(instructions)))

    max_out = body.get("max_output_tokens")
    if max_out is not None:
        max_out = max(1, min(int(max_out), _MAX_OUTPUT_TOKENS))

    tools = tools_to_chat(body.get("tools"))
    extra: dict = {}
    if tools:
        extra["tools"] = tools
        extra["tool_choice"] = tool_choice_to_chat(body.get("tool_choice"))
        if body.get("parallel_tool_calls") is not None:
            extra["parallel_tool_calls"] = bool(body["parallel_tool_calls"])

    req = ChatCompletionRequest(
        messages=messages,
        model=body.get("model") or None,
        temperature=body["temperature"] if body.get("temperature") is not None else 0.7,
        max_tokens=max_out,
        stream=bool(body.get("stream")),
        **extra,
        **({"strategy": body["strategy"]} if body.get("strategy") else {}),
        **({"preferred_provider": body["preferred_provider"]} if body.get("preferred_provider") else {}),
    )
    return req, conversation


# ──────────────── response objects ────────────────


def _function_call_item(tool_call: dict, item_id: Optional[str] = None) -> dict:
    fn = tool_call.get("function") or {}
    return {
        "type": "function_call",
        "id": item_id or new_function_call_id(),
        "call_id": tool_call.get("id") or new_function_call_id(),
        "name": fn.get("name", ""),
        "arguments": fn.get("arguments") or "{}",
        "status": "completed",
    }


def output_items(text: Optional[str], tool_calls: Optional[list[dict]]) -> list[dict]:
    """Assistant text (if any) as a message item, then one item per call."""
    items: list[dict] = []
    if text or not tool_calls:
        items.append(_message_item(new_message_id(), text or ""))
    for tc in tool_calls or []:
        items.append(_function_call_item(tc))
    return items


def assistant_turn(text: Optional[str], tool_calls: Optional[list[dict]]) -> ChatMessage:
    """The assistant message to keep in history for previous_response_id."""
    if tool_calls:
        return ChatMessage(role="assistant", content=text or None, tool_calls=tool_calls)
    return ChatMessage(role="assistant", content=text or "")


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
    tools_out = [t for t in (body.get("tools") or []) if isinstance(t, dict) and t.get("type") == "function"]
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
        "tool_choice": body.get("tool_choice") or "auto",
        "tools": tools_out,
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
    msg = chat.choices[0].message if chat.choices else None
    text = (msg.content if msg else "") or ""
    tool_calls = msg.tool_calls if msg else None
    return response_object(
        response_id=response_id,
        body=body,
        created_at=chat.created,
        model=chat.model,
        status="completed",
        output=output_items(text, tool_calls),
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

    ``on_complete(text, item)`` is called once the full text is known (used
    to store the turn for ``previous_response_id`` / ``item_reference``). Provider errors raised by
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
        on_complete(text, item)
    input_tokens = sum(estimate_tokens(m.text_content) for m in input_messages)
    yield ev("response.completed", response=snapshot(
        "completed", [item],
        input_tokens=input_tokens, output_tokens=estimate_tokens(text),
        extra={"provider": provider},
    ))


async def replay_events(response: dict) -> AsyncIterator[str]:
    """Emit the streaming event sequence for an already-complete response.

    Used when the request has tools: the upstream call runs non-streaming
    (so tool calls arrive whole, from any provider) and the client still
    gets a well-formed Responses stream.
    """
    seq = 0

    def ev(event_type: str, **payload) -> str:
        nonlocal seq
        seq += 1
        return sse_event(event_type, {"type": event_type, "sequence_number": seq, **payload})

    pending = {**response, "status": "in_progress", "output": []}
    pending.pop("output_text", None)
    yield ev("response.created", response=pending)
    yield ev("response.in_progress", response=pending)
    for idx, item in enumerate(response.get("output", [])):
        if item["type"] == "message":
            text = "".join(p.get("text", "") for p in item.get("content", []))
            yield ev("response.output_item.added", output_index=idx,
                     item={**item, "status": "in_progress", "content": []})
            part = {"type": "output_text", "text": "", "annotations": []}
            yield ev("response.content_part.added", item_id=item["id"], output_index=idx,
                     content_index=0, part=part)
            if text:
                yield ev("response.output_text.delta", item_id=item["id"], output_index=idx,
                         content_index=0, delta=text)
            yield ev("response.output_text.done", item_id=item["id"], output_index=idx,
                     content_index=0, text=text)
            yield ev("response.content_part.done", item_id=item["id"], output_index=idx,
                     content_index=0, part={**part, "text": text})
        elif item["type"] == "function_call":
            yield ev("response.output_item.added", output_index=idx,
                     item={**item, "status": "in_progress", "arguments": ""})
            yield ev("response.function_call_arguments.delta", item_id=item["id"],
                     output_index=idx, delta=item["arguments"])
            yield ev("response.function_call_arguments.done", item_id=item["id"],
                     output_index=idx, arguments=item["arguments"])
        yield ev("response.output_item.done", output_index=idx, item=item)
    yield ev("response.completed", response=response)


def failed_event(response_id: str, body: dict, model: str, error: dict, seq: int = 0) -> str:
    return sse_event("response.failed", {
        "type": "response.failed",
        "sequence_number": seq,
        "response": response_object(
            response_id=response_id, body=body, created_at=int(time.time()),
            model=model, status="failed", output=[], error=error,
        ),
    })
