from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ainl.brief.compose import bucket, compose, render
from ainl.canvas.models import Announcement, CourseStats, Item, Kind

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 16, 8, 0, tzinfo=TZ)


def item(id, title, due, *, kind=Kind.ASSIGNMENT, submitted=False, course="MATH222: Calc 2", ext=False, missing=None):
    return Item(
        id=id, course_id="1", course_name=course, kind=kind, title=title,
        canonical_url=f"https://c/x/{id}", planned_at=due, due_at=due, points_possible=10,
        submission_types=("external_tool",) if ext else ("online_upload",),
        submitted=submitted,
        missing=(due < NOW and not submitted) if missing is None else missing,
    )


def test_bucket_splits_missing_from_stale_and_sorts():
    items = [
        item("a", "yesterday, canvas says missing", NOW - timedelta(days=1)),
        item("a2", "last week, canvas says missing", NOW - timedelta(days=7)),
        item("s", "past due but external/ungraded", NOW - timedelta(days=2), missing=False),
        item("b", "tonight", NOW.replace(hour=23, minute=59)),
        item("c", "in 2 days", NOW + timedelta(days=2)),
        item("d", "in 10 days", NOW + timedelta(days=10)),
        item("e", "already done", NOW + timedelta(hours=3), submitted=True),
        item("f", "announcement", NOW, kind=Kind.ANNOUNCEMENT),
        item("a", "duplicate of a", NOW - timedelta(days=1)),
    ]
    b = bucket(items, NOW)
    assert [i.id for i in b.missing] == ["a", "a2"]      # most recent first
    assert b.stale == {"MATH222": 1}
    assert [i.id for i in b.today] == ["b"]
    assert [i.id for i in b.soon] == ["c"]


def test_ungraded_pace_items_are_hidden_and_counted():
    from ainl.canvas.models import enrich_submission_types
    raw = [
        Item(id="p", course_id="1", course_name="SPANISH101: x", kind=Kind.ASSIGNMENT, title="Week 3 Day 2",
             canonical_url="https://c/p", planned_at=NOW + timedelta(hours=2), due_at=NOW + timedelta(hours=2), points_possible=None),
        Item(id="g", course_id="1", course_name="SPANISH101: x", kind=Kind.ASSIGNMENT, title="2 - Prep",
             canonical_url="https://c/g", planned_at=NOW + timedelta(hours=2), due_at=NOW + timedelta(hours=2), points_possible=10),
    ]
    assert raw[0].submission_types == ()                       # planner gives nothing
    items = enrich_submission_types(raw, {"p": ("not_graded",), "g": ("external_tool",)})
    b = bucket(items, NOW)
    assert [i.id for i in b.today] == ["g"] and b.pace == 1
    out = render(b, NOW)
    assert "2 - Prep</a> ↗" in out and "1 ungraded class-day items hidden" in out


def test_short_course_names():
    from ainl.brief.compose import _short_course
    assert _short_course("MATH222: Calculus and Analytic Geometry 2 (003 004) FA26") == "MATH222"
    assert _short_course("PHYSICS247: A Modern Introduction") == "PHYSICS247"
    assert _short_course("Fall 2026 - International Student Orientation (ISO)") == "International Student"
    assert _short_course("GetWIse for New Students | Fall 2026") == "GetWIse for New"
    assert _short_course("Fall 2026 Decisions: Alcohol & Other Drug Awareness Course") == "Decisions: Alcohol"


def test_render_escapes_html_and_marks_external():
    b = bucket([item("x", "A <b>bold</b> & co", NOW + timedelta(hours=2), ext=True)], NOW)
    out = render(b, NOW)
    assert "A &lt;b&gt;bold&lt;/b&gt; &amp; co" in out
    assert "↗" in out
    assert 'href="https://c/x/x"' in out


def test_compose_full_brief():
    items = [item("1", "PS1", NOW - timedelta(days=2)), item("2", "Quiz", NOW + timedelta(hours=5), kind=Kind.QUIZ)]
    anns = [
        Announcement("9", "1", "MATH222: Calc 2", "Exam room", "<p>x</p>", NOW, "Prof", read=False),
        Announcement("8", "1", "MATH222: Calc 2", "Old news", "<p>x</p>", NOW, "Prof", read=True),
    ]
    stats = [CourseStats("1", "MATH222: Calc 2", due=48, missing=1, submitted=0)]
    out = compose(items, anns, stats, NOW)
    assert "Wed 09-16" in out
    assert "MATH222</b>  due 48 · missing 1 · done 0" in out
    assert "🔴 <b>Missing</b> (1)" in out and "PS1" in out
    assert "🟠 <b>Due today</b> (1)" in out and "Quiz" in out
    assert "📣 <b>Unread announcements</b> (1)" in out and "Exam room" in out and "Old news" not in out


def test_empty_brief_still_says_something():
    out = compose([], [], [], NOW)
    assert "Nothing due" in out


def test_section_caps_with_more_marker():
    items = [item(str(i), f"hw{i}", NOW + timedelta(hours=1 + i)) for i in range(12)]
    out = render(bucket(items, NOW), NOW)
    assert "+4 more" in out
