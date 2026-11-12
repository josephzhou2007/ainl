"""Turn normalized Canvas data into the morning brief (L5 content).

Pure functions: no I/O, fully testable. Output is Telegram HTML.

Shape of the brief:
    ☀️ Tue 09-16 · morning brief
    📊 one line per course: due / missing / submitted
    🔴 overdue, not submitted
    🟠 due today
    🟡 due in the next N days
    📣 unread announcements
    (nothing due → say so; never send an empty message)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..canvas.models import Announcement, CourseStats, Item, Kind
from ..telegram.client import esc

LOOKAHEAD_DAYS = 3
MAX_PER_SECTION = 8       # cap each list; the rest becomes "+N more"


@dataclass
class Brief:
    missing: list[Item] = field(default_factory=list)      # past due AND Canvas says you owe it
    stale: dict[str, int] = field(default_factory=dict)    # past due, but Canvas isn't tracking it (external / ungraded), per course
    pace: int = 0                                          # not_graded class-day markers we dropped from the lists
    today: list[Item] = field(default_factory=list)
    soon: list[Item] = field(default_factory=list)
    announcements: list[Announcement] = field(default_factory=list)
    stats: list[CourseStats] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.missing or self.today or self.soon or self.announcements)


import re

_CODE = re.compile(r"^[A-Z]{2,}\s?\d{2,}[A-Z]?")


def _short_course(name: str | None) -> str:
    """'MATH222: Calculus …' -> 'MATH222';  'Fall 2026 - International Student Orientation (ISO)' -> 'Intl Student Orient'."""
    if not name:
        return ""
    m = _CODE.match(name)
    if m:
        return m.group(0).replace(" ", "")
    # no course code: drop a leading term ("Fall 2026 - ") and keep a few words
    rest = re.sub(r"^(Fall|Spring|Summer)\s+\d{4}\s*[-–:]?\s*", "", name)
    rest = re.sub(r"\s*[|(].*$", "", rest)              # drop "| Fall 2026", "(ISO)"
    acc: list[str] = []
    for w in rest.split():
        if len(" ".join(acc + [w])) > 22:
            break
        acc.append(w)
    return " ".join(acc).rstrip(" &-:") or name[:22]


def bucket(items: list[Item], now: datetime, lookahead_days: int = LOOKAHEAD_DAYS) -> Brief:
    b = Brief()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    soon_end = today_end + timedelta(days=lookahead_days)
    seen: set[str] = set()
    for it in items:
        if it.kind == Kind.ANNOUNCEMENT or not it.actionable:
            continue
        key = f"{it.kind.value}:{it.id}"
        if key in seen:
            continue
        seen.add(key)
        due = it.due_at or it.planned_at
        if due is None:
            continue
        if it.is_ungraded:
            b.pace += 1
            continue
        due = due.astimezone(now.tzinfo)
        if due < now:
            if it.missing:
                b.missing.append(it)
            else:
                c = _short_course(it.course_name)
                b.stale[c] = b.stale.get(c, 0) + 1
        elif due <= today_end:
            b.today.append(it)
        elif due <= soon_end:
            b.soon.append(it)
    # missing: most recently due first — that's what you can still realistically rescue
    b.missing.sort(key=lambda i: (i.due_at or i.planned_at), reverse=True)
    for lst in (b.today, b.soon):
        lst.sort(key=lambda i: (i.due_at or i.planned_at))
    return b


def _line(it: Item, tz: ZoneInfo, show_date: bool) -> str:
    due = (it.due_at or it.planned_at).astimezone(tz)
    when = due.strftime("%m-%d %H:%M") if show_date else due.strftime("%H:%M")
    ext = " ↗" if it.is_external else ""
    return f"  • {when}  <a href=\"{esc(it.canonical_url)}\">{esc(it.title[:60])}</a>{ext}  <i>{esc(_short_course(it.course_name))}</i>"


def _section(title: str, items: list[Item], tz: ZoneInfo, show_date: bool) -> list[str]:
    if not items:
        return []
    out = [f"{title} ({len(items)})"]
    out += [_line(i, tz, show_date) for i in items[:MAX_PER_SECTION]]
    if len(items) > MAX_PER_SECTION:
        out.append(f"  … +{len(items) - MAX_PER_SECTION} more")
    out.append("")
    return out


def render(b: Brief, now: datetime) -> str:
    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else ZoneInfo("UTC")
    lines = [f"☀️ <b>{now.strftime('%a %m-%d')}</b> · morning brief", ""]

    active = [s for s in b.stats if s.due or s.missing]
    if active:
        for s in sorted(active, key=lambda s: (-s.missing, -s.due)):
            lines.append(f"📊 <b>{esc(_short_course(s.course_name))}</b>  due {s.due} · missing {s.missing} · done {s.submitted}")
        lines.append("")

    lines += _section("🔴 <b>Missing</b>", b.missing, tz, show_date=True)
    lines += _section("🟠 <b>Due today</b>", b.today, tz, show_date=False)
    lines += _section(f"🟡 <b>Next {LOOKAHEAD_DAYS} days</b>", b.soon, tz, show_date=True)

    foot = []
    if b.stale:
        parts = ", ".join(f"{esc(c)} {n}" for c, n in sorted(b.stale.items(), key=lambda kv: -kv[1]))
        foot.append(f"⚪ <i>Past due, not tracked by Canvas (external): {parts}</i>")
    if b.pace:
        foot.append(f"⚪ <i>{b.pace} ungraded class-day items hidden</i>")
    if foot:
        lines += foot + [""]

    if b.announcements:
        lines.append(f"📣 <b>Unread announcements</b> ({len(b.announcements)})")
        for a in b.announcements[:MAX_PER_SECTION]:
            lines.append(f"  • <b>{esc(_short_course(a.course_name))}</b> — {esc(a.title[:70])}")
        lines.append("")

    if b.empty:
        lines.append("Nothing due in the next few days and no unread announcements. 🎉")

    return "\n".join(lines).rstrip()


def compose(
    items: list[Item],
    announcements: list[Announcement],
    stats: list[CourseStats],
    now: datetime,
) -> str:
    b = bucket(items, now)
    b.announcements = [a for a in announcements if not a.read]
    b.stats = stats
    return render(b, now)
