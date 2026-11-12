"""Shared pieces for vision providers: the Protocol, the not-configured error,
image sniffing, response cleaning and the single HTTP entry point that tests stub.

Keys are read only from environment variables, only inside a provider's
constructor (never at import time), and never stored anywhere but the
provider instance's private attribute.
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any, ClassVar, Protocol, runtime_checkable

HTTP_TIMEOUT = 60.0


class ProviderNotConfigured(RuntimeError):
    """Raised by a provider constructor when its API key env var is missing."""


@runtime_checkable
class VisionProvider(Protocol):
    name: str
    cost_per_1k: float  # USD per 1k images, from docs/research/handwriting-ocr-options-2026-09.md
    required_env: ClassVar[tuple[str, ...]]

    def transcribe(self, image_bytes: bytes, *, hint: str = "") -> str:
        """Return one line of LaTeX transcribed verbatim from the image."""
        ...


def require_env(*names: str) -> dict[str, str]:
    """Return ``{name: value}`` for every env var, or raise ``ProviderNotConfigured``."""
    values = {n: os.environ.get(n, "") for n in names}
    missing = [n for n, v in values.items() if not v]
    if missing:
        raise ProviderNotConfigured(f"{', '.join(missing)} not set")
    return values


def is_configured(names: tuple[str, ...]) -> bool:
    return all(os.environ.get(n) for n in names)


def detect_media_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def b64(image_bytes: bytes) -> str:
    return base64.standard_b64encode(image_bytes).decode("ascii")


def data_url(image_bytes: bytes) -> str:
    return f"data:{detect_media_type(image_bytes)};base64,{b64(image_bytes)}"


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def clean_latex_response(text: str) -> str:
    """Take the first non-empty line and strip ``$…$`` / ``\\(…\\)`` / code fences."""
    text = text.strip()
    text = _FENCE_RE.sub("", text).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    line = lines[0]
    if line.startswith("```"):  # fence line followed by content
        line = lines[1].strip() if len(lines) > 1 else ""
    line = _FENCE_RE.sub("", line).strip()
    for open_, close in (("$$", "$$"), ("$", "$"), ("\\[", "\\]"), ("\\(", "\\)")):
        if line.startswith(open_) and line.endswith(close) and len(line) >= len(open_) + len(close):
            line = line[len(open_) : len(line) - len(close)].strip()
            break
    return line


def http_post(url: str, *, headers: dict[str, str], json: dict[str, Any]) -> dict[str, Any]:
    """POST JSON and return the decoded JSON body. The only network call for the
    httpx-based providers; tests monkeypatch ``base.http_post``."""
    import httpx

    response = httpx.post(url, headers=headers, json=json, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()
