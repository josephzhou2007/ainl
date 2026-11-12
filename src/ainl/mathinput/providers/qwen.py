"""Alibaba Model Studio (international / Singapore) Qwen3-VL-Flash via the
OpenAI-compatible chat endpoint (pure httpx).

Key: ``DASHSCOPE_API_KEY`` as a Bearer token.  Report sec. 1 line 43 notes the
image-token rule was not verified on the official page, so the $/1k could be
off by 2-3x (sec. 9 item 3) - still the cheapest hosted option.
"""

from __future__ import annotations

from typing import ClassVar

from .. import prompts
from . import base

API_KEY_ENV = "DASHSCOPE_API_KEY"
DEFAULT_MODEL = "qwen3-vl-flash"
ENDPOINT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"


class QwenProvider:
    name = "qwen-vl-flash"
    # cost_per_1k: report sec. 1 line 43, "Qwen3-VL-Flash (Alibaba intl.) ... $/1k images 0.08"
    cost_per_1k = 0.08
    required_env: ClassVar[tuple[str, ...]] = (API_KEY_ENV,)

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._key = base.require_env(API_KEY_ENV)[API_KEY_ENV]

    def transcribe(self, image_bytes: bytes, *, hint: str = "") -> str:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": prompts.SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": base.data_url(image_bytes)}},
                        {"type": "text", "text": prompts.user_prompt(hint)},
                    ],
                },
            ],
        }
        payload = base.http_post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            json=body,
        )
        choices = payload.get("choices") or []
        content = (choices[0].get("message") or {}).get("content", "") if choices else ""
        if isinstance(content, list):  # some OpenAI-compatible servers return parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return base.clean_latex_response(content)
