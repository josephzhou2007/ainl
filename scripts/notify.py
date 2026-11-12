"""Send one message to the owner's chat — for the automatic sweep review.

    python scripts/notify.py "text"        (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID from the env)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ainl.config import TelegramConfig  # noqa: E402
from ainl.telegram import TelegramClient  # noqa: E402

cfg = TelegramConfig.from_env()
tg = TelegramClient(cfg.bot_token)
tg.send(cfg.chat_id, " ".join(sys.argv[1:])[:3900])
tg.close()
