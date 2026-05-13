"""Cerebras Inference — OpenAI-compatible chat on Wafer-Scale Engine hardware.

Free tier is generous (1M tokens/day per model on gpt-oss-120b) and does not
expire, which makes Cerebras a good default for users without their own keys.
See docs/providers/cerebras.md for the full integration reference."""
from __future__ import annotations

from .openai_compat import OpenAICompatibleProvider


class CerebrasProvider(OpenAICompatibleProvider):
    name = "cerebras"
    BASE_URL = "https://api.cerebras.ai/v1/chat/completions"
    request_timeout = 60.0
