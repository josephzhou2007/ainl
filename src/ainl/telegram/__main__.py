"""
    python -m ainl.telegram --discover      # list chats that have messaged the bot
    python -m ainl.telegram --ping          # send a test message to TELEGRAM_CHAT_ID
    python -m ainl.telegram --transcript [N]                 # last N records (default 40)
    python -m ainl.telegram --transcript-since 2026-09-24T23:00
"""
from __future__ import annotations

import argparse
import sys

from ..config import ConfigError, TelegramConfig
from .client import TelegramClient
from .transcript import Transcript, format_record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ainl.telegram")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--transcript", nargs="?", const=40, type=int, metavar="N",
                    help="print the last N conversation records from <AINL_HOME>/transcript.jsonl")
    ap.add_argument("--transcript-since", metavar="ISO",
                    help="print every record since this local time, e.g. 2026-09-24T23:00")
    a = ap.parse_args(argv)

    # The transcript is read from disk; no token needed, no network.
    if a.transcript is not None or a.transcript_since:
        t = Transcript()
        if not t.path.exists():
            print(f"no transcript at {t.path}", file=sys.stderr)
            return 1
        try:
            recs = t.since(a.transcript_since) if a.transcript_since else t.tail(a.transcript)
        except ValueError as e:
            print(f"bad --transcript-since value: {e}", file=sys.stderr)
            return 2
        for r in recs:
            print(format_record(r))
        return 0

    if not (a.discover or a.ping):
        ap.print_help()
        return 2
    try:
        cfg = TelegramConfig.from_env(need_chat=a.ping)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2

    tg = TelegramClient(cfg.bot_token)
    try:
        if a.discover:
            chats = tg.discover_chats()
            if not chats:
                print("No chats yet. Open your bot in Telegram, press Start, then re-run.")
                return 1
            for c in chats:
                print(f"  TELEGRAM_CHAT_ID={c['chat_id']:<16} {c['type']:<8} {c['name']}  @{c['username'] or '-'}")
        if a.ping:
            tg.send(cfg.chat_id, "✅ <b>ainl</b> is connected.")
            print("sent")
    finally:
        tg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
