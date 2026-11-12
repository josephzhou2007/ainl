"""Google Gemini provider via the REST ``generateContent`` endpoint (pure httpx, no SDK).

Default model is Gemini 3.1 Flash-Lite (report sec. 1 line 38; shutdown
2027-05-07, so the id is a constructor argument, not hard-wired).
Key: ``GEMINI_API_KEY``, sent in the ``x-goog-api-key`` header.
"""

from __future__ import annotations

from typing import ClassVar

from .. import prompts
from . import base

API_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider:
    name = "gemini-flash-lite"
    # cost_per_1k: report sec. 1 line 38, "Gemini 3.1 Flash-Lite ... $/1k images 0.35"
    cost_per_1k = 0.35
    required_env: ClassVar[tuple[str, ...]] = (API_KEY_ENV,)

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._key = base.require_env(API_KEY_ENV)[API_KEY_ENV]

    def transcribe(self, image_bytes: bytes, *, hint: str = "") -> str:
        body = {
            "system_instruction": {"parts": [{"text": prompts.SYSTEM_PROMPT}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": base.detect_media_type(image_bytes),
                                "data": base.b64(image_bytes),
                            }
                        },
                        {"text": prompts.user_prompt(hint)},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 512},
        }
        payload = base.http_post(
            ENDPOINT.format(model=self.model),
            headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
            json=body,
        )
        candidates = payload.get("candidates") or []
        parts = (candidates[0].get("content") or {}).get("parts", []) if candidates else []
        text = "".join(p.get("text", "") for p in parts)
        return base.clean_latex_response(text)
