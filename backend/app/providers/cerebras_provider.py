"""Cerebras Inference — OpenAI-compatible chat on Wafer-Scale Engine hardware.

Since 2026-07-16 there is no permanent free tier: new accounts get a $5 trial
credit (30 days) after adding a payment method. Kept for users with a paid or
trial key. See docs/providers/cerebras.md for the full integration reference."""
from __future__ import annotations

from .openai_compat import OpenAICompatibleProvider


class CerebrasProvider(OpenAICompatibleProvider):
    name = "cerebras"
    BASE_URL = "https://api.cerebras.ai/v1/chat/completions"
    request_timeout = 60.0
