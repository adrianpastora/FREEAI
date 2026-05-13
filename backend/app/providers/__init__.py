from .base import (
    AudioInput,
    BaseProvider,
    EmbeddingResult,
    ErrorKind,
    ProviderError,
    ProviderResponse,
    StreamChunk,
    TranscriptionResult,
)
from .cerebras_provider import CerebrasProvider
from .groq_provider import GroqProvider
from .gemini_provider import GeminiProvider
from .mistral_provider import MistralProvider
from .openrouter_provider import OpenRouterProvider
from .cohere_provider import CohereProvider
from .huggingface_provider import HuggingFaceProvider

PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {
    "cerebras": CerebrasProvider,
    "groq": GroqProvider,
    "gemini": GeminiProvider,
    "mistral": MistralProvider,
    "openrouter": OpenRouterProvider,
    "cohere": CohereProvider,
    "huggingface": HuggingFaceProvider,
}

__all__ = [
    "AudioInput",
    "BaseProvider",
    "EmbeddingResult",
    "ErrorKind",
    "ProviderError",
    "ProviderResponse",
    "StreamChunk",
    "TranscriptionResult",
    "PROVIDER_REGISTRY",
]
