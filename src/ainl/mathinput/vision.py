"""Handwritten math photo -> one line of LaTeX (legacy single-provider entry point).

The implementation now lives in ``ainl.mathinput.providers``; this module keeps
the original ``handwriting_to_latex`` API (Anthropic ``claude-sonnet-5``) for
existing callers.  For the cost-aware multi-provider path use
``ainl.mathinput.pipeline.transcribe_with_consensus``.

The API key is read by the SDK from ``ANTHROPIC_API_KEY``; it is never
passed around, printed or logged here.
"""

from __future__ import annotations

import os

from .prompts import SYSTEM_PROMPT  # noqa: F401  (re-exported for compatibility)
from .providers import anthropic as _anthropic
from .providers.base import ProviderNotConfigured, clean_latex_response, detect_media_type

MODEL = "claude-sonnet-5"
API_KEY_ENV = _anthropic.API_KEY_ENV

# Kept as the historical name; it *is* the providers' error type.
MissingApiKeyError = ProviderNotConfigured

_detect_media_type = detect_media_type

__all__ = [
    "MODEL",
    "API_KEY_ENV",
    "SYSTEM_PROMPT",
    "MissingApiKeyError",
    "clean_latex_response",
    "handwriting_to_latex",
]


def _make_client():
    """Build the Anthropic client. Split out so tests can monkeypatch it."""
    if not os.environ.get(API_KEY_ENV):
        raise MissingApiKeyError(f"{API_KEY_ENV} is not set")
    return _anthropic._default_client()


def handwriting_to_latex(image_bytes: bytes, *, hint: str = "") -> str:
    """Return a single line of LaTeX transcribed from ``image_bytes``.

    ``hint`` describes the question type (e.g. "indefinite integral, include +C").
    """
    provider = _anthropic.AnthropicProvider(
        MODEL, name="claude-sonnet-5", cost_per_1k=3.07, client_factory=_make_client
    )
    return provider.transcribe(image_bytes, hint=hint)
