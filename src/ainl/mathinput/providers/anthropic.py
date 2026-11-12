"""Anthropic Messages API provider (Claude Sonnet 5 = escalation tier, Haiku 4.5 = cheaper variant).

Uses the ``anthropic`` SDK, imported lazily. The SDK reads ``ANTHROPIC_API_KEY``
from the environment itself; we only check that it exists.
"""

from __future__ import annotations

from typing import Callable, ClassVar

from .. import prompts
from . import base

API_KEY_ENV = "ANTHROPIC_API_KEY"


class AnthropicProvider:
    required_env: ClassVar[tuple[str, ...]] = (API_KEY_ENV,)

    def __init__(
        self,
        model: str,
        *,
        name: str,
        cost_per_1k: float,
        client_factory: Callable[[], object] | None = None,
    ) -> None:
        """``client_factory`` is an injection point for tests / the legacy
        ``vision._make_client`` hook; when given it owns the key check."""
        self.model = model
        self.name = name
        self.cost_per_1k = cost_per_1k
        if client_factory is None:
            base.require_env(*self.required_env)
            client_factory = _default_client
        self._client_factory = client_factory

    def transcribe(self, image_bytes: bytes, *, hint: str = "") -> str:
        client = self._client_factory()
        response = client.messages.create(  # type: ignore[attr-defined]
            model=self.model,
            max_tokens=512,
            system=prompts.SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": base.detect_media_type(image_bytes),
                                "data": base.b64(image_bytes),
                            },
                        },
                        {"type": "text", "text": prompts.user_prompt(hint)},
                    ],
                }
            ],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return base.clean_latex_response(text)


def _default_client():
    import anthropic

    return anthropic.Anthropic()


def sonnet(**kw) -> AnthropicProvider:
    # cost_per_1k: report sec. 1 line 30, "Claude Sonnet 5 (baseline) ... $/1k images 3.07"
    return AnthropicProvider("claude-sonnet-5", name="claude-sonnet-5", cost_per_1k=3.07, **kw)


def haiku(**kw) -> AnthropicProvider:
    # cost_per_1k: report sec. 1 line 31, "Claude Haiku 4.5 ... $/1k images 1.54"
    return AnthropicProvider(
        "claude-haiku-4-5-20251001", name="claude-haiku-4-5", cost_per_1k=1.54, **kw
    )
