#!/usr/bin/env python3
"""Check the provider catalog against each provider's live model list.

    python scripts/check_catalog.py            # public endpoints + any keys in env
    python scripts/check_catalog.py --strict   # exit 1 if a provider can't be checked

Reads backend/app/providers/catalog.py and, for every provider, fetches the
upstream ``/models`` list. OpenRouter, HuggingFace and NVIDIA publish theirs
without a key; the rest need one in the environment:

    GROQ_API_KEY  GEMINI_API_KEY  MISTRAL_API_KEY  COHERE_API_KEY  CEREBRAS_API_KEY

Exit code 1 when a catalog model is missing upstream (retired / renamed) —
that's the signal to edit catalog.py and bump CATALOG_VERSION. Also prints
free models upstream that the catalog doesn't list yet, as candidates.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.providers.catalog import CATALOG, CATALOG_VERSION  # noqa: E402

STALE_AFTER_DAYS = 60
TIMEOUT = httpx.Timeout(20.0)


def _openai_ids(data: dict) -> set[str]:
    return {m["id"] for m in data.get("data", [])}


def _bearer(env: str) -> Optional[dict]:
    key = os.environ.get(env)
    return {"Authorization": f"Bearer {key}"} if key else None


def _fetch_openai_style(url: str, headers: Optional[dict] = None) -> set[str]:
    r = httpx.get(url, headers=headers or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return _openai_ids(r.json())


def _gemini() -> Optional[set[str]]:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    ids: set[str] = set()
    page = ""
    while True:
        r = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": key, "pageSize": 1000, **({"pageToken": page} if page else {})},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        ids |= {m["name"].removeprefix("models/") for m in data.get("models", [])}
        page = data.get("nextPageToken", "")
        if not page:
            return ids


def _cohere() -> Optional[set[str]]:
    headers = _bearer("COHERE_API_KEY")
    if not headers:
        return None
    r = httpx.get(
        "https://api.cohere.com/v1/models",
        params={"endpoint": "chat", "page_size": 1000},
        headers=headers, timeout=TIMEOUT,
    )
    r.raise_for_status()
    return {m["name"] for m in r.json().get("models", [])}


def _keyed(url: str, env: str) -> Callable[[], Optional[set[str]]]:
    def fetch() -> Optional[set[str]]:
        headers = _bearer(env)
        return _fetch_openai_style(url, headers) if headers else None
    return fetch


FETCHERS: dict[str, Callable[[], Optional[set[str]]]] = {
    "openrouter": lambda: _fetch_openai_style("https://openrouter.ai/api/v1/models"),
    "huggingface": lambda: _fetch_openai_style("https://router.huggingface.co/v1/models"),
    "nvidia": lambda: _fetch_openai_style("https://integrate.api.nvidia.com/v1/models"),
    "groq": _keyed("https://api.groq.com/openai/v1/models", "GROQ_API_KEY"),
    "cerebras": _keyed("https://api.cerebras.ai/v1/models", "CEREBRAS_API_KEY"),
    "mistral": _keyed("https://api.mistral.ai/v1/models", "MISTRAL_API_KEY"),
    "gemini": _gemini,
    "cohere": _cohere,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true",
                    help="fail when a provider can't be checked (no key / error)")
    args = ap.parse_args()

    age = (dt.date.today() - dt.date.fromisoformat(CATALOG_VERSION)).days
    print(f"catalog version {CATALOG_VERSION} ({age} days old)")
    if age > STALE_AFTER_DAYS:
        print(f"  ! older than {STALE_AFTER_DAYS} days — re-check free tiers and limits too")

    missing: list[str] = []
    unchecked: list[str] = []
    for name, entry in CATALOG.items():
        fetch = FETCHERS.get(name)
        if fetch is None:
            print(f"\n[{name}] no fetcher defined — add one to FETCHERS")
            unchecked.append(name)
            continue
        try:
            upstream = fetch()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            print(f"\n[{name}] could not fetch model list: {e}")
            unchecked.append(name)
            continue
        if upstream is None:
            print(f"\n[{name}] skipped (no API key in env)")
            unchecked.append(name)
            continue

        print(f"\n[{name}] {len(upstream)} models upstream")
        for m in entry.models:
            ok = m.id in upstream
            flag = "ok     " if ok else "MISSING"
            role = "default" if m.id == entry.default_model else (
                "fallback" if m.auto_fallback else "listed")
            print(f"  {flag} {m.id}  ({role})")
            if not ok:
                missing.append(f"{name}:{m.id}")
        if name == "openrouter":
            listed = {m.id for m in entry.models}
            new_free = sorted(i for i in upstream if i.endswith(":free") and i not in listed)
            if new_free:
                print("  candidates (free, not in catalog): " + ", ".join(new_free))

    print()
    if missing:
        print("MISSING upstream — edit backend/app/providers/catalog.py, move these to")
        print("retired_models, pick replacements and bump CATALOG_VERSION:")
        for m in missing:
            print(f"  - {m}")
    else:
        print("all checked catalog models exist upstream")
    if unchecked:
        print(f"not checked: {', '.join(unchecked)}")
    if missing or (args.strict and unchecked):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
