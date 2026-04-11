"""Groq — OpenAI-compatible. Free tier: very fast Llama/Mixtral inference."""
from .openai_compat import OpenAICompatibleProvider


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    request_timeout = 60.0
