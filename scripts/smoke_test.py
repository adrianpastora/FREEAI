#!/usr/bin/env python3
"""End-to-end smoke test against a running FreeAI, with real providers.

    python scripts/smoke_test.py --url https://freeai.example.dev --key fai_...
    FREEAI_URL=... FREEAI_KEY=... python scripts/smoke_test.py --providers groq,gemini

Uses a client API key (create one in the panel under *Clients*). It runs the
OpenAI surface (chat, streaming, Responses, previous_response_id, tool
calling, model aliasing), then asks each provider directly via
``preferred_provider`` for both plain chat and a tool call. If a provider
doesn't answer, FreeAI falls back to another, and the report shows a WARN
naming the provider that actually served the request. That usually means
the requested provider's key, quota or catalog models need attention.

Each check is one small real request (a few dozen tokens). Exit code 1 if a
core check fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.providers.catalog import CATALOG  # noqa: E402

TOOL_CHAT = {"type": "function", "function": {
    "name": "get_weather",
    "description": "Get the current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]},
}}
TOOL_RESP = {"type": "function", "name": "get_weather",
             "description": "Get the current weather for a city",
             "parameters": TOOL_CHAT["function"]["parameters"]}
WEATHER_PROMPT = "What's the weather in Madrid right now? Use the tool."

results: list[tuple[str, str, str]] = []


def report(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"  {status:<4} {name}" + (f" - {detail}" if detail else ""), flush=True)


def check(client: httpx.Client, name: str, fn, core: bool = True) -> None:
    started = time.perf_counter()
    try:
        detail = fn(client) or ""
        ms = int((time.perf_counter() - started) * 1000)
        if isinstance(detail, tuple):  # ("WARN", msg)
            report(detail[0], name, f"{detail[1]} ({ms} ms)")
        else:
            report("PASS", name, f"{detail} ({ms} ms)".strip())
    except Exception as e:  # noqa: BLE001
        report("FAIL" if core else "WARN", name, str(e)[:300])


def _post(client: httpx.Client, path: str, payload: dict) -> dict:
    r = client.post(path, json=payload)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


# ──────────────── core API surface ────────────────


def t_health(c):
    r = c.get("/api/health")
    r.raise_for_status()
    return r.json().get("status")


def t_models(c):
    data = c.get("/v1/models").json()["data"]
    assert any(m["id"] == "freeai-auto" for m in data), "freeai-auto missing"
    return f"{len(data)} models"


def t_chat(c):
    d = _post(c, "/v1/chat/completions", {
        "model": "freeai-auto", "max_tokens": 40,
        "messages": [{"role": "user", "content": "Reply with the word OK."}],
    })
    return f"{d['provider']} / {d.get('real_model') or d['model']}, chain={d['fallback_chain']}"


def t_chat_stream(c):
    text, provider = "", None
    with c.stream("POST", "/v1/chat/completions", json={
        "model": "freeai-fast", "stream": True, "max_tokens": 40,
        "messages": [{"role": "user", "content": "Count from 1 to 5."}],
    }) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.read()[:300]!r}")
        for line in r.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            provider = chunk.get("provider") or provider
            text += (chunk["choices"][0].get("delta") or {}).get("content") or ""
    assert text.strip(), "empty stream"
    return f"{provider}, {len(text)} chars"


def t_alias(c):
    d = _post(c, "/v1/chat/completions", {
        "model": "gpt-4o", "max_tokens": 20,
        "messages": [{"role": "user", "content": "Say hi."}],
    })
    return f"gpt-4o -> {d['model']} via {d['provider']}"


def t_responses_chain(c):
    first = _post(c, "/v1/responses", {
        "model": "freeai-auto", "max_output_tokens": 60,
        "input": "Remember the number 42. Reply only with OK.",
    })
    second = _post(c, "/v1/responses", {
        "model": "freeai-auto", "max_output_tokens": 60,
        "previous_response_id": first["id"],
        "input": "Which number did I ask you to remember? Reply with just the number.",
    })
    out = second.get("output_text", "")
    if "42" not in out:
        return ("WARN", f"chain worked but the model answered {out[:60]!r}")
    return f"chained via {second['x_freeai']['provider']}"


def t_responses_tools(c):
    first = _post(c, "/v1/responses", {
        "model": "freeai-auto", "input": WEATHER_PROMPT, "tools": [TOOL_RESP],
        "tool_choice": "required",
    })
    calls = [o for o in first["output"] if o["type"] == "function_call"]
    assert calls, f"no function_call in output: {first['output']}"
    final = _post(c, "/v1/responses", {
        "model": "freeai-auto", "tools": [TOOL_RESP], "previous_response_id": first["id"],
        "input": [{"type": "function_call_output", "call_id": calls[0]["call_id"],
                   "output": "22 C, sunny"}],
    })
    assert final.get("output_text"), "no final answer after tool output"
    return f"call via {first['x_freeai']['provider']}, answer via {final['x_freeai']['provider']}"


# ──────────────── per provider ────────────────


def provider_checks(provider: str):
    def plain(c):
        d = _post(c, "/v1/chat/completions", {
            "model": "freeai-auto", "preferred_provider": provider, "max_tokens": 40,
            "messages": [{"role": "user", "content": "Reply with the word OK."}],
        })
        if d["provider"] != provider:
            return ("WARN", f"served by {d['provider']} (fallback) - chain {d['fallback_chain']}")
        return d.get("real_model") or d["model"]

    def tools(c):
        d = _post(c, "/v1/chat/completions", {
            "model": "freeai-auto", "preferred_provider": provider,
            "messages": [{"role": "user", "content": WEATHER_PROMPT}],
            "tools": [TOOL_CHAT], "tool_choice": "required",
        })
        msg = d["choices"][0]["message"]
        if d["provider"] != provider:
            return ("WARN", f"served by {d['provider']} (fallback)")
        if not msg.get("tool_calls"):
            return ("WARN", f"no tool call, answered: {(msg.get('content') or '')[:60]!r}")
        args = json.loads(msg["tool_calls"][0]["function"]["arguments"] or "{}")
        return f"{msg['tool_calls'][0]['function']['name']}({args})"

    return plain, tools


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("FREEAI_URL", "http://localhost:8000"))
    ap.add_argument("--key", default=os.environ.get("FREEAI_KEY"))
    ap.add_argument("--providers", default="all",
                    help="comma-separated provider names, 'all', or 'none'")
    args = ap.parse_args()
    if not args.key:
        print("missing --key (or FREEAI_KEY): create a client key in the panel -> Clients")
        return 2

    base = args.url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.key}"}
    with httpx.Client(base_url=base, headers=headers, timeout=120) as c:
        print(f"FreeAI smoke test - {base}")
        print("\ncore")
        check(c, "health", t_health)
        check(c, "GET /v1/models", t_models)
        check(c, "chat completion", t_chat)
        check(c, "chat streaming", t_chat_stream)
        check(c, "proprietary model alias (gpt-4o)", t_alias)
        check(c, "responses + previous_response_id", t_responses_chain)
        check(c, "responses tool-call loop", t_responses_tools)

        if args.providers != "none":
            names = sorted(CATALOG) if args.providers == "all" else args.providers.split(",")
            print("\nper provider (WARN = that provider didn't serve it)")
            for name in names:
                plain, tools = provider_checks(name)
                check(c, f"{name}: chat", plain, core=False)
                check(c, f"{name}: tools", tools, core=False)

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    print(f"\n{len(results) - len(fails) - len(warns)} pass, {len(warns)} warn, {len(fails)} fail")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
