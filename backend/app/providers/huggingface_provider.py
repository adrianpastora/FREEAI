"""HuggingFace Inference Router — OpenAI-compatible chat completions."""
from .openai_compat import OpenAICompatibleProvider


class HuggingFaceProvider(OpenAICompatibleProvider):
    name = "huggingface"
    BASE_URL = "https://router.huggingface.co/v1/chat/completions"
    request_timeout = 120.0
