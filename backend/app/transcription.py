"""Audio-transcription dispatch helpers.

The actual provider adapters live in ``app.providers.*`` — each one that
supports speech-to-text overrides ``BaseProvider.transcribe()``. This
module only exports:

  • ``TRANSCRIPTION_PROVIDERS`` — default priority order for the dispatch loop.
  • ``supports_transcription(name)`` — capability lookup, derived from the
    provider class flag so a new adapter just sets ``supports_transcription = True``.
  • ``AUDIO_MIMETYPES`` + ``resolve_content_type`` — file-extension → MIME mapping
    used by the upload endpoint.

There is no provider-specific code here; that lives with each provider so
``transcription`` doesn't need editing every time a new transcription-capable
adapter joins the registry.
"""
from __future__ import annotations

from typing import Optional

from .providers import PROVIDER_REGISTRY


# Priority order for fallback when the caller hasn't pinned a provider.
# First entry is tried first.
TRANSCRIPTION_PROVIDERS: tuple[str, ...] = ("groq", "gemini")


# MIME types accepted on the upload endpoint. Superset of what individual
# providers support — each provider does its own sanity check before sending.
AUDIO_MIMETYPES: dict[str, str] = {
    "ogg":  "audio/ogg",
    "mp3":  "audio/mpeg",
    "wav":  "audio/wav",
    "webm": "audio/webm",
    "m4a":  "audio/m4a",
    "flac": "audio/flac",
    "mpeg": "audio/mpeg",
    "mpga": "audio/mpeg",
    "aac":  "audio/aac",
    "aiff": "audio/aiff",
}


def supports_transcription(provider_name: str) -> bool:
    """Whether a provider has a transcription implementation.

    Derived from ``BaseProvider.supports_transcription`` rather than a
    duplicate list, so adding the capability to a new provider just
    means setting the flag on its class.
    """
    cls = PROVIDER_REGISTRY.get(provider_name)
    if cls is None:
        return False
    return bool(getattr(cls, "supports_transcription", False))


def resolve_content_type(filename: str, fallback: Optional[str] = None) -> str:
    """Determine MIME type from file extension."""
    ext = (filename or "audio.ogg").rsplit(".", 1)[-1].lower()
    return AUDIO_MIMETYPES.get(ext, fallback or "application/octet-stream")
