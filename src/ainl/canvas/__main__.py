"""Smoke-test CLI.

    python -m ainl.canvas --login          # open Chrome, log in once, save session
    python -m ainl.canvas --whoami
    python -m ainl.canvas --courses
    python -m ainl.canvas --planner [--days 14]
    python -m ainl.canvas --stats
    python -m ainl.canvas --announcements

Uses CANVAS_TOKEN if set; otherwise the saved browser session (see session.py).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from ..config import CanvasConfig, ConfigError
from ._http import CanvasAuthError
from .api import ApiSource


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ainl.canvas")
    ap.add_argument("--login", action="store_true", help="(re)authenticate via a Chrome window")
    ap.add_argument("--debug", action="store_true", help="with --login: screenshot every hop to ~/.ainl/login-debug/")
    ap.add_argument("--reset-profile", action="store_true", help="delete the browser profile and saved session")
    ap.add_argument("--whoami", action="store_true")
    ap.add_argument("--courses", action="store_true")
    ap.add_argument("--planner", action="store_true")
    ap.add_argument("--days", type=int, default=14, help="planner window, days back from now")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--announcements", action="store_true")
    a = ap.parse_args(argv)

    if a.reset_profile:
        from .session import BrowserSession
        s = BrowserSession(CanvasConfig.from_env().base_url)
        s.reset_profile()
        print(f"removed {s.profile_dir} and {s.state_file.name}")
        if not a.login:
            return 0

    if a.login:
        from .session import BrowserSession, LoginTimeout
        try:
            BrowserSession(CanvasConfig.from_env().base_url).login_interactive(debug=a.debug)
        except LoginTimeout as e:
            print(e, file=sys.stderr)
            return 3
        return 0

    try:
        src = ApiSource.from_env()
    except (ConfigError, CanvasAuthError) as e:
        print(e, file=sys.stderr)
        return 2

    try:
        if a.whoami or not any((a.courses, a.planner, a.stats, a.announcements)):
            me = src.whoami()
            print(f"{me['name']}  <{me['login_id']}>  id={me['id']}  tz={me['time_zone']}")

        if a.courses:
            for c in src.courses():
                print(f"  {c.id}  {c.code:<28}  view={c.default_view or '-':<10} {c.name}")

        if a.planner:
            since = datetime.now(timezone.utc) - timedelta(days=a.days)
            for it in src.planner(since):
                flag = "!" if it.missing else ("~" if it.late else " ")
                when = it.planned_at.strftime("%m-%d %H:%M") if it.planned_at else "     --   "
                print(f"  {flag} {when}  {it.kind.value:<12} {it.title[:48]:<48} {it.course_name or ''}")

        if a.stats:
            for s in src.stats():
                print(f"  due={s.due:>3}  missing={s.missing:>3}  submitted={s.submitted:>3}   {s.course_name}")

        if a.announcements:
            for an in src.announcements(unread_only=False)[:20]:
                mark = " " if an.read else "*"
                when = an.posted_at.strftime("%m-%d") if an.posted_at else "--"
                print(f"  {mark} {when}  {an.title[:50]:<50} {an.course_name or ''}")
    except CanvasAuthError as e:
        print(f"Canvas session expired ({e}). Run:  python -m ainl.canvas --login", file=sys.stderr)
        return 3
    finally:
        src.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
