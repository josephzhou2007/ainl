"""Mathpix Convert API ``/v3/text`` (pure httpx).

Purpose-built handwriting/math OCR; returns LaTeX *and* MathML natively
(report sec. 2 line 55), so ``transcribe_mathml`` exposes the MathML too.
Keys: ``MATHPIX_APP_ID`` + ``MATHPIX_APP_KEY`` as request headers.
Note: this provider does not take a prompt, so the verbatim instruction in
``prompts.py`` cannot be applied; Mathpix is an OCR engine, not a VLM, and
does not "correct" answers, which is the property the prompt exists to enforce.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from . import base

APP_ID_ENV = "MATHPIX_APP_ID"
APP_KEY_ENV = "MATHPIX_APP_KEY"
ENDPOINT = "https://api.mathpix.com/v3/text"

_DELIM_RE = re.compile(r"^\s*(?:\\\(|\\\[|\$\$?)\s*|\s*(?:\\\)|\\\]|\$\$?)\s*$")


class MathpixProvider:
    name = "mathpix"
    # cost_per_1k: report sec. 2 line 55, "Mathpix Convert API (image OCR) ... $/1k images 2.00"
    cost_per_1k = 2.00
    required_env: ClassVar[tuple[str, ...]] = (APP_ID_ENV, APP_KEY_ENV)

    def __init__(self) -> None:
        keys = base.require_env(*self.required_env)
        self._headers = {
            "app_id": keys[APP_ID_ENV],
            "app_key": keys[APP_KEY_ENV],
            "Content-Type": "application/json",
        }

    def _call(self, image_bytes: bytes) -> dict[str, Any]:
        body = {
            "src": base.data_url(image_bytes),
            "formats": ["text", "data"],
            "data_options": {"include_latex": True, "include_mathml": True},
        }
        return base.http_post(ENDPOINT, headers=self._headers, json=body)

    @staticmethod
    def _data(payload: dict[str, Any], kind: str) -> str:
        for item in payload.get("data") or []:
            if item.get("type") == kind and item.get("value"):
                return str(item["value"]).strip()
        return ""

    def transcribe(self, image_bytes: bytes, *, hint: str = "") -> str:
        payload = self._call(image_bytes)
        latex = self._data(payload, "latex")
        if not latex:
            # ``text`` is Mathpix Markdown: the formula wrapped in \( ... \) or \[ ... \].
            latex = _DELIM_RE.sub("", (payload.get("text") or "").strip())
        return base.clean_latex_response(latex)

    def transcribe_mathml(self, image_bytes: bytes) -> str:
        """MathML straight from Mathpix (empty string if the response has none)."""
        return self._data(self._call(image_bytes), "mathml")
