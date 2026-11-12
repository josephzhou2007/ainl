"""Run the bot (long polling). Single user: only TELEGRAM_CHAT_ID is served.

    python -m ainl.bot
"""
from __future__ import annotations

import logging
import sys
import time

from ..canvas import ApiSource
from ..canvas._http import CanvasAuthError
from ..config import ConfigError, TelegramConfig, home
from ..telegram import TelegramClient
from .conversation import Conversation
from .state import BotState

log = logging.getLogger("ainl.bot")


_LOCK = None                    # held for the life of the process


def single_instance(path) -> bool:
    """One bot per bot token. Two on the same token both receive every
    message and both answer: everything came twice, and the two drove the
    same WebAssign tab at once, so pictures and numbers crossed (2026-09-27,
    a test run started while the first was still up). The lock goes with
    the process, however it ends."""
    global _LOCK
    import fcntl
    import os
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        other = f.read().strip() or "?"
        f.close()
        print(f"Another bot is already running (pid {other}). Stop it first — "
              f"kill {other}, or: scripts/macos/service.sh stop bot", file=sys.stderr)
        return False
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _LOCK = f
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # bot token lives in the URL
    if not single_instance(home() / "bot.lock"):
        return 3
    try:
        tcfg = TelegramConfig.from_env()
        try:
            canvas = ApiSource.from_env(interactive=True)
        except Exception as e:
            # A pinned channel that cannot open must not stop the bot starting:
            # the student would have no way to unpin it except on the host.
            from ..canvas import channel
            log.warning("pinned channel %r unusable (%s) — falling back to auto",
                        channel.preference(), str(e)[:120])
            channel.set_preference("auto")
            canvas = ApiSource.from_env(interactive=True)
        tz = canvas.whoami().get("time_zone") or "UTC"
    except (ConfigError, CanvasAuthError) as e:
        print(e, file=sys.stderr)
        return 2

    state_path = home() / "bot_state.json"
    state = BotState.load(state_path)
    tg = TelegramClient(tcfg.bot_token)
    conv = Conversation(canvas, tg, tcfg.chat_id, state, tz=tz)
    conv._state_path = state_path
    from ..util import metrics, trouble
    # Failures are filed under <AINL_HOME>/failures; this bot's only chat is
    # the owner's, who sees the message already — so nothing extra is sent.
    trouble.set_notifier(trouble.telegram_notifier(tg, tcfg.chat_id), owner=tcfg.chat_id)
    metrics.start_sampler()
    try:
        # Without this Telegram's "/" menu is empty and every command has to be
        # remembered. Re-registered on each start so it matches this build.
        commands = [
            # /todo is the one way in; everything else starts from a code in it.
            # There is deliberately no /math here — see Conversation._command.
            ("todo", "What's due — reply a code like B1 to do it"),
            ("resume", "WebAssign work you saved for later"),
            ("restart", "Clear everything in flight"),
            ("check", "Try every /todo item, report what works (fills nothing)"),
            ("cancel", "Drop the current item"),
            ("channel", "Canvas via token or browser session"),
            ("timing", "How long recent steps took"),
            ("stats", "How busy the bot is, memory, latency"),
            ("help", "What I can do"),
        ]
        if conv.autosolve:          # TEST ONLY — listed only while AINL_AUTOSOLVE is set
            commands.insert(1, ("auto", "TEST: the model does the open assignment"))
            commands.insert(2, ("sweep", "TEST: walk every /todo item up to Submit, report problems"))
        tg.set_commands(commands)
    except Exception as e:                               # not worth failing to start over
        log.warning("setMyCommands failed: %s", e)
    # Keep the Canvas session warm so it never has to be re-created: a session
    # dies from neglect, not from age.
    try:
        from ..canvas import keepalive
        from ..config import CanvasConfig
        import os
        def session_gone() -> None:
            # Only a bot that is on the cookie channel is stopped by this; on
            # the token the browser session is a spare.
            if getattr(conv.canvas, "_channel", "") == "cookie":
                with conv._lock:                     # the chat thread may be sending too
                    conv.ask_login()

        keepalive.start(CanvasConfig.from_env().base_url,
                        float(os.environ.get("AINL_KEEPALIVE_MIN") or keepalive.DEFAULT_MINUTES),
                        on_gone=session_gone)
    except Exception as e:
        log.warning("keepalive not started: %s", e)
    # Tidy the browser (close its tabs, keep its logins) once it has been idle
    # a while: every page left open is a renderer running its scripts.
    import os
    import threading
    idle = {"since": time.time(), "tidied": False}
    tidy_after = float(os.environ.get("AINL_TIDY_MIN") or 10) * 60

    def _tidy_loop() -> None:
        from ..util import pw
        from ..util.tidy import tidy
        while True:
            time.sleep(60)
            if idle["tidied"] or time.time() - idle["since"] < tidy_after:
                continue
            auto = conv._auto_thread
            if auto is not None and auto.is_alive():
                continue                             # /auto is working in there
            if not conv._lock.acquire(blocking=False):
                continue                             # a message is being handled
            try:
                n = tidy(pw.CDP_URL)
                idle["tidied"] = True
                if n:
                    log.info("tidied the browser: closed %d tab(s)", n)
            except Exception as e:
                log.info("tidy skipped: %s", str(e)[:80])
            finally:
                conv._lock.release()

    threading.Thread(target=_tidy_loop, name="tidy", daemon=True).start()
    log.info("bot up for chat %s (tz %s)", tcfg.chat_id, tz)
    try:
        while True:
            try:
                updates = tg.get_updates(offset=state.offset, timeout=30)
            except Exception as e:                       # network blip: back off, keep going
                log.warning("getUpdates failed: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                state.offset = int(u["update_id"]) + 1
                tg.transcript.record_update(u)       # before handling: even a crash leaves a trace
                # How long it waited for us (the bot handles one thing at a time,
                # so this is the queue), and how long it took.
                msg = u.get("message") or {}
                sent = msg.get("date")                   # a tap has no send time of its own
                text = msg.get("text") or ""
                if u.get("callback_query"):
                    what = "tap"
                elif text.startswith("/"):
                    what = text.split()[0][:20]
                elif msg.get("photo") or msg.get("document"):
                    what = "file"
                else:
                    what = "text"
                t_start = time.time()
                idle["tidied"] = False
                conv.handle(u)
                idle["since"] = time.time()
                if conv.waiting_for_student():           # their move now: tabs can go
                    try:
                        from ..util import pw
                        from ..util.tidy import tidy
                        tidy(pw.CDP_URL)
                        idle["tidied"] = True
                    except Exception as e:
                        log.info("release skipped: %s", str(e)[:80])
                metrics.emit("update", what=what, lag_s=round(t_start - sent, 2) if sent else None,
                             handle_s=round(time.time() - t_start, 2))
                with conv._lock:                          # /auto's worker also changes state
                    state.save(state_path)
    except KeyboardInterrupt:
        pass
    finally:
        state.save(state_path)
        canvas.close()
        tg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
