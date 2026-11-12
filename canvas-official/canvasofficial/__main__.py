"""CLI for the official-token client.

    python -m canvasofficial --probe                  what does this token allow?
    python -m canvasofficial --whoami
    python -m canvasofficial --todo [--days 4]
    python -m canvasofficial --courses
    python -m canvasofficial --assignment <course_id> <assignment_id>

Writes live behind --submit-* and always print what they are about to do first.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import CanvasClient, CanvasError, TokenError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m canvasofficial")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--probe", action="store_true", help="capability table for this token")
    g.add_argument("--whoami", action="store_true")
    g.add_argument("--courses", action="store_true")
    g.add_argument("--todo", action="store_true", help="planner items, today .. +N days")
    g.add_argument("--assignment", nargs=2, metavar=("COURSE", "ASSIGNMENT"))
    g.add_argument("--submit-files", nargs="+", metavar=("COURSE ASSIGNMENT", "FILE"),
                   help="COURSE ASSIGNMENT FILE [FILE...] — the student's own files")
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--comment", default="")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    a = p.parse_args(argv)

    try:
        c = CanvasClient.from_env()
    except TokenError as e:
        print(f"error: {e.body}", file=sys.stderr)
        return 2

    try:
        with c:
            return _dispatch(c, a)
    except TokenError as e:
        print(f"token rejected ({e.status}) on {e.path}: {e.body[:200]}", file=sys.stderr)
        return 3
    except CanvasError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _dispatch(c: CanvasClient, a: argparse.Namespace) -> int:
    if a.probe:
        from .probe import format_table, run_probe
        print(format_table(run_probe(c)))
        return 0

    if a.whoami:
        me = c.whoami()
        print(f"{me.get('name')}  id={me.get('id')}  tz={me.get('time_zone')}")
        return 0

    if a.courses:
        for course in c.courses():
            print(f"{course['id']:>8}  {course.get('course_code', ''):<16} {course.get('name')}")
        return 0

    if a.todo:
        now = datetime.now(timezone.utc)
        items = c.planner(now - timedelta(hours=12), now + timedelta(days=a.days))
        for it in items:
            plan = it.get("plannable") or {}
            due = plan.get("due_at") or it.get("plannable_date") or ""
            print(f"{due[:16]:<17} {it.get('context_name', '')[:20]:<21} "
                  f"{plan.get('title', '')[:50]}")
            print(f"{'':<17} {c.absolute(it.get('html_url', ''))}")
        print(f"\n{len(items)} items, today .. +{a.days}d")
        return 0

    if a.assignment:
        course_id, assignment_id = a.assignment
        d = c.assignment(course_id, assignment_id)
        print(f"{d.get('name')}\n  due    {d.get('due_at')}\n  types  {d.get('submission_types')}"
              f"\n  points {d.get('points_possible')}\n  url    {d.get('html_url')}")
        return 0

    if a.submit_files:
        return _submit_files(c, a)
    return 2


def _submit_files(c: CanvasClient, a: argparse.Namespace) -> int:
    from .submit import submit_files

    if len(a.submit_files) < 3:
        print("error: --submit-files COURSE ASSIGNMENT FILE [FILE...]", file=sys.stderr)
        return 2
    course_id, assignment_id, *names = a.submit_files
    paths = [Path(n) for n in names]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"error: no such file: {', '.join(missing)}", file=sys.stderr)
        return 2

    d = c.assignment(course_id, assignment_id)
    print(f"about to submit to: {d.get('name')} (due {d.get('due_at')})")
    for p in paths:
        print(f"  {p.name}  {p.stat().st_size:,} bytes")
    if not a.yes and input("type 'submit' to confirm: ").strip() != "submit":
        print("aborted.")
        return 1

    out = submit_files(c, course_id, assignment_id, paths, comment=a.comment)
    print(f"submitted at {out.get('submitted_at')}  attempt={out.get('attempt')}  "
          f"late={out.get('late')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
