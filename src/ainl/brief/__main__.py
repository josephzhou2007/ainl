"""The morning brief (L5 schedule + L6 delivery).

    python -m ainl.brief --preview      # print what would be sent, send nothing
    python -m ainl.brief --now          # send once, right now
    python -m ainl.brief --serve        # run forever; send daily at BRIEF_HOUR:BRIEF_MINUTE

Time zone comes from the Canvas profile (America/Chicago for UW).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..canvas import ApiSource
from ..canvas._http import CanvasAuthError
from ..canvas.models import enrich_submission_types
from ..config import BriefConfig, ConfigError, TelegramConfig
from ..telegram import TelegramClient
from .compose import LOOKAHEAD_DAYS, compose

log = logging.getLogger("ainl.brief")


def build(src: ApiSource) -> tuple[str, str]:
    """Returns (brief_html, time_zone)."""
    me = src.whoami()
    tz = ZoneInfo(me.get("time_zone") or "UTC")
    now = datetime.now(tz)
    since = datetime.now(timezone.utc) - timedelta(days=30)   # catch overdue, not just upcoming
    items = src.planner(since)
    # planner omits submission_types; one REST sweep per course fills them in
    # (drives the ↗ external marker and the not_graded filter)
    course_ids = [i.course_id for i in items if i.course_id]
    items = enrich_submission_types(items, src.assignments_index(course_ids))
    stats = src.stats()
    anns = src.announcements(unread_only=True)
    return compose(items, anns, stats, now), str(tz)


_EXPIRED = "⚠️ Canvas session expired — run <code>python -m ainl.canvas --login</code> on the host."


def _notify_expired(preview: bool, err: Exception) -> int:
    """Whatever else happens, an unattended run must tell the phone, not just stderr."""
    log.warning("canvas session expired: %s", err)
    if preview:
        print(_EXPIRED)
        return 3
    cfg = TelegramConfig.from_env()
    tg = TelegramClient(cfg.bot_token)
    try:
        tg.send(cfg.chat_id, _EXPIRED)
    finally:
        tg.close()
    return 3


def send_once(preview: bool) -> int:
    try:
        src = ApiSource.from_env(interactive=preview)   # a scheduled run must not pop a browser
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    except CanvasAuthError as e:
        return _notify_expired(preview, e)
    try:
        text, tz = build(src)
    except CanvasAuthError as e:
        return _notify_expired(preview, e)
    finally:
        src.close()

    if preview:
        print(f"[tz={tz}]\n")
        print(text)
        return 0

    cfg = TelegramConfig.from_env()
    tg = TelegramClient(cfg.bot_token)
    try:
        ids = tg.send(cfg.chat_id, text)
        log.info("brief sent, %d message(s)", len(ids))
    finally:
        tg.close()
    return 0


def serve() -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    # validate config up front so a typo fails now, not at 08:00
    TelegramConfig.from_env()
    bc = BriefConfig.from_env()
    try:
        src = ApiSource.from_env(interactive=True)
        tz = src.whoami().get("time_zone") or "UTC"
        src.close()
    except (ConfigError, CanvasAuthError) as e:
        print(e, file=sys.stderr)
        return 2

    sched = BlockingScheduler(timezone=tz)
    sched.add_job(lambda: send_once(preview=False), CronTrigger(hour=bc.hour, minute=bc.minute, timezone=tz),
                  id="morning-brief", misfire_grace_time=3600, coalesce=True)
    log.info("scheduled daily brief at %02d:%02d %s", bc.hour, bc.minute, tz)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx logs full request URLs at INFO — for Telegram that URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="ainl.brief")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preview", action="store_true")
    g.add_argument("--now", action="store_true")
    g.add_argument("--serve", action="store_true")
    a = ap.parse_args(argv)
    if a.serve:
        return serve()
    return send_once(preview=a.preview)


if __name__ == "__main__":
    raise SystemExit(main())
