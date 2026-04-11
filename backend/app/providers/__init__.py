from .base import BaseProvider, ErrorKind, ProviderError, ProviderResponse, StreamChunk
from .groq_provider import GroqProvider
from .gemini_provider import GeminiProvider
from .mistral_provider import MistralProvider
from .openrouter_provider import OpenRouterProvider
from .cohere_provider import CohereProvider
from .huggingface_provider import HuggingFaceProvider

PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {
    "groq": GroqProvider,
    "gemini": GeminiProvider,
    "mistral": MistralProvider,
    "openrouter": OpenRouterProvider,
    "cohere": CohereProvider,
    "huggingface": HuggingFaceProvider,
}

__all__ = [
    "BaseProvider",
    "ErrorKind",
    "ProviderError",
    "ProviderResponse",
    "StreamChunk",
    "PROVIDER_REGISTRY",
]
