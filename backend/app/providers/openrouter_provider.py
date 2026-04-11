"""OpenRouter — gateway exposing many free :free models behind one OpenAI-compatible API."""
from .openai_compat import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    request_timeout = 90.0

    def _extra_headers(self) -> dict[str, str]:
        return {
            "HTTP-Referer": "https://freeai.local",
            "X-Title": "FreeAI Orchestrator",
        }
