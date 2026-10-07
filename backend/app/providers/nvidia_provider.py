"""NVIDIA NIM (build.nvidia.com) — OpenAI-compatible chat completions.

Free with an NVIDIA Developer Program account, no card: ~40 req/min per
model on a rotating catalogue of open-weight models (Nemotron, DeepSeek,
GLM, gpt-oss). Model IDs keep the vendor prefix, e.g.
``nvidia/nemotron-3-super-120b-a12b``.
"""
from .openai_compat import OpenAICompatibleProvider


class NvidiaProvider(OpenAICompatibleProvider):
    name = "nvidia"
    BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
    request_timeout = 120.0
