"""Runtime configuration.

Credentials come from the environment (or ~/.ainl/ for browser session
state). Nothing here ever logs or repr()s a secret — see ONBOARDING.md.

    CANVAS_BASE_URL       default https://canvas.wisc.edu
    CANVAS_TOKEN          optional; if absent we use a browser session
    AINL_HOME             default ~/.ainl  (browser profile + session state)
    TELEGRAM_BOT_TOKEN    from @BotFather
    TELEGRAM_CHAT_ID      where the brief goes (python -m ainl.telegram --discover)
    BRIEF_HOUR / BRIEF_MINUTE   default 08:00, in the Canvas user's time zone
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def home() -> Path:
    p = Path(_env("AINL_HOME") or Path.home() / ".ainl")
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass(frozen=True)
class CanvasConfig:
    base_url: str
    token: str = field(default="", repr=False)   # empty => browser session

    @classmethod
    def from_env(cls) -> "CanvasConfig":
        return cls(
            base_url=_env("CANVAS_BASE_URL", "https://canvas.wisc.edu").rstrip("/"),
            token=_env("CANVAS_TOKEN"),
        )


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: str

    @classmethod
    def from_env(cls, need_chat: bool = True) -> "TelegramConfig":
        tok = _env("TELEGRAM_BOT_TOKEN")
        if not tok:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set (create a bot with @BotFather).")
        chat = _env("TELEGRAM_CHAT_ID")
        if need_chat and not chat:
            raise ConfigError(
                "TELEGRAM_CHAT_ID is not set. Send /start to your bot, then run:\n"
                "    python -m ainl.telegram --discover"
            )
        return cls(bot_token=tok, chat_id=chat)


@dataclass(frozen=True)
class BriefConfig:
    hour: int = 8
    minute: int = 0

    @classmethod
    def from_env(cls) -> "BriefConfig":
        return cls(hour=int(_env("BRIEF_HOUR", "8")), minute=int(_env("BRIEF_MINUTE", "0")))
