"""Mistral La Plateforme — OpenAI-compatible chat completions."""
from .openai_compat import OpenAICompatibleProvider


class MistralProvider(OpenAICompatibleProvider):
    name = "mistral"
    BASE_URL = "https://api.mistral.ai/v1/chat/completions"
    request_timeout = 60.0
