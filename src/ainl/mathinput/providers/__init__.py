"""Pluggable handwriting -> LaTeX vision providers.

    from ainl.mathinput.providers import registry
    registry.available()          # names whose API key env var is set
    registry.get("qwen-vl-flash").transcribe(image_bytes, hint="...")

Importing this package never touches the network or an SDK; keys are checked
only when a provider is constructed.
"""

from .base import ProviderNotConfigured, VisionProvider, clean_latex_response, detect_media_type
from .registry import available, get, names

__all__ = [
    "VisionProvider",
    "ProviderNotConfigured",
    "clean_latex_response",
    "detect_media_type",
    "get",
    "available",
    "names",
]
