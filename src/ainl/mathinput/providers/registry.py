"""Name -> provider factory. ``get`` constructs (and so checks the key);
``available`` only looks at the environment."""

from __future__ import annotations

from typing import Callable

from . import anthropic, base, gemini, mathpix, qwen
from .base import ProviderNotConfigured, VisionProvider

# name -> (factory, required env vars). Order = display order in the eval table.
_REGISTRY: dict[str, tuple[Callable[[], VisionProvider], tuple[str, ...]]] = {
    "gemini-flash-lite": (gemini.GeminiProvider, gemini.GeminiProvider.required_env),
    "qwen-vl-flash": (qwen.QwenProvider, qwen.QwenProvider.required_env),
    "claude-sonnet-5": (anthropic.sonnet, anthropic.AnthropicProvider.required_env),
    "claude-haiku-4-5": (anthropic.haiku, anthropic.AnthropicProvider.required_env),
    "mathpix": (mathpix.MathpixProvider, mathpix.MathpixProvider.required_env),
}


def names() -> list[str]:
    return list(_REGISTRY)


def get(name: str) -> VisionProvider:
    """Construct the named provider. Raises ``KeyError`` for an unknown name and
    ``ProviderNotConfigured`` when its key env var is missing."""
    try:
        factory, _ = _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown provider {name!r}; known: {', '.join(_REGISTRY)}") from None
    return factory()


def available() -> list[str]:
    """Names whose API key env vars are all set (no construction, no network)."""
    return [n for n, (_, env) in _REGISTRY.items() if base.is_configured(env)]


__all__ = ["get", "available", "names", "ProviderNotConfigured", "VisionProvider"]
