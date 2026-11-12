"""Normalized Canvas objects (the L1 schema).

Everything upstream — REST, GraphQL, someday a scraper — is flattened into
these. Upper layers never see raw Canvas JSON.

Field provenance is documented in docs/canvas-api-map.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse


def _dt(s: Optional[str]) -> Optional[datetime]:
    """Parse Canvas ISO-8601 timestamps ('...Z' or with offset) into aware datetimes."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Kind(str, Enum):
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    DISCUSSION = "discussion"
    ANNOUNCEMENT = "announcement"
    PAGE = "page"
    EVENT = "event"
    NOTE = "note"
    OTHER = "other"


# planner/items plannable_type -> Kind
_PLANNABLE_KIND = {
    "assignment": Kind.ASSIGNMENT,
    "quiz": Kind.QUIZ,
    "discussion_topic": Kind.DISCUSSION,
    "announcement": Kind.ANNOUNCEMENT,
    "wiki_page": Kind.PAGE,
    "calendar_event": Kind.EVENT,
    "planner_note": Kind.NOTE,
}


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    code: str
    term: Optional[str]
    default_view: Optional[str]   # modules | wiki | assignments | syllabus | feed
    time_zone: Optional[str]
    ics_url: Optional[str]

    @classmethod
    def from_json(cls, j: dict[str, Any]) -> "Course":
        return cls(
            id=str(j["id"]),
            name=j.get("name", ""),
            code=j.get("course_code", ""),
            term=(j.get("term") or {}).get("name"),
            default_view=j.get("default_view"),
            time_zone=j.get("time_zone"),
            ics_url=(j.get("calendar") or {}).get("ics"),
        )


@dataclass(frozen=True)
class Item:
    """One thing the student might need to act on. Sourced from planner/items."""

    id: str                          # plannable_id
    course_id: Optional[str]
    course_name: Optional[str]
    kind: Kind
    title: str
    canonical_url: str               # absolute; already the real object URL (quiz -> /quizzes/…)
    planned_at: Optional[datetime]   # plannable_date: due date, or post date for announcements
    due_at: Optional[datetime]
    points_possible: Optional[float]
    submission_types: tuple[str, ...] = ()
    submitted: bool = False
    graded: bool = False
    missing: bool = False
    late: bool = False
    excused: bool = False
    read: Optional[bool] = None      # announcements/discussions only
    new_activity: bool = False
    external_host: Optional[str] = None   # filled later from the assignment's LTI URL

    @property
    def is_external(self) -> bool:
        return "external_tool" in self.submission_types

    @property
    def is_ungraded(self) -> bool:
        """A dated item nobody submits: 'Week 3 Day 2', 'Studying vocab—tips'.
        These are class-pace markers, not obligations."""
        return self.submission_types in (("not_graded",), ("none",))

    @property
    def actionable(self) -> bool:
        """Something the student still has to do (vs. merely read)."""
        if self.kind == Kind.ANNOUNCEMENT:
            return self.read is False
        return not (self.submitted or self.excused)

    @classmethod
    def from_planner(cls, j: dict[str, Any], base_url: str) -> "Item":
        p = j.get("plannable") or {}
        subs = j.get("submissions")
        subs = subs if isinstance(subs, dict) else {}
        html = j.get("html_url") or ""
        url = html if html.startswith("http") else f"{base_url}{html}"
        return cls(
            id=str(j.get("plannable_id") or p.get("id")),
            course_id=str(j["course_id"]) if j.get("course_id") else None,
            course_name=j.get("context_name"),
            kind=_PLANNABLE_KIND.get(j.get("plannable_type", ""), Kind.OTHER),
            title=p.get("title") or p.get("name") or "",
            canonical_url=url,
            planned_at=_dt(j.get("plannable_date")),
            due_at=_dt(p.get("due_at")),
            points_possible=p.get("points_possible"),
            submission_types=tuple(p.get("submission_types") or ()),
            submitted=bool(subs.get("submitted")),
            graded=bool(subs.get("graded")),
            missing=bool(subs.get("missing")),
            late=bool(subs.get("late")),
            excused=bool(subs.get("excused")),
            read=(p.get("read_state") == "read") if "read_state" in p else None,
            new_activity=bool(j.get("new_activity")),
        )

    @classmethod
    def from_course_work(cls, node: dict[str, Any]) -> "Item":
        """From GraphQL GetUserCourseWork nodes (submission + nested assignment)."""
        a = node.get("assignment") or {}
        course = a.get("course") or {}
        if a.get("quiz"):
            kind = Kind.QUIZ
        elif a.get("discussion"):
            kind = Kind.DISCUSSION
        else:
            kind = Kind.ASSIGNMENT
        state = node.get("state") or ""
        return cls(
            id=str(a.get("_id")),
            course_id=str(course["_id"]) if course.get("_id") else None,
            course_name=course.get("name"),
            kind=kind,
            title=a.get("name") or "",
            canonical_url=a.get("htmlUrl") or "",
            planned_at=_dt(node.get("cachedDueDate")),
            due_at=_dt(a.get("dueAt") or node.get("cachedDueDate")),
            points_possible=a.get("pointsPossible"),
            submission_types=tuple(a.get("submissionTypes") or ()),
            submitted=state in ("submitted", "graded", "pending_review"),
            graded=state == "graded",
            missing=bool(node.get("missing")),
            late=bool(node.get("late")),
            excused=bool(node.get("excused")),
        )

    def with_external_host(self, launch_url: Optional[str]) -> "Item":
        host = urlparse(launch_url).netloc if launch_url else None
        return Item(**{**self.__dict__, "external_host": host or None})

    def with_submission_types(self, types: tuple[str, ...]) -> "Item":
        return Item(**{**self.__dict__, "submission_types": tuple(types)})


def enrich_submission_types(items: list["Item"], index: dict[str, tuple[str, ...]]) -> list["Item"]:
    """planner/items omits submission_types; fill from an assignment-id index
    (see ApiSource.assignments_index). Plain assignments only — quiz/discussion
    planner ids are the quiz/discussion ids, not assignment ids, so they won't
    match, and they don't need to: their kind already says what they are."""
    out = []
    for it in items:
        if not it.submission_types and it.kind == Kind.ASSIGNMENT and it.id in index:
            it = it.with_submission_types(index[it.id])
        out.append(it)
    return out


@dataclass(frozen=True)
class Announcement:
    id: str
    course_id: Optional[str]
    course_name: Optional[str]
    title: str
    message_html: str
    posted_at: Optional[datetime]
    author: Optional[str]
    read: bool

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> "Announcement":
        t = node.get("discussionTopic") or {}
        return cls(
            id=str(t.get("_id")),
            course_id=str(t["contextId"]) if t.get("contextId") else None,
            course_name=t.get("contextName"),
            title=t.get("title") or "",
            message_html=t.get("message") or "",
            posted_at=_dt(t.get("createdAt")),
            author=(t.get("author") or {}).get("name"),
            read=bool(node.get("read")),
        )


@dataclass(frozen=True)
class CourseStats:
    course_id: str
    course_name: str
    due: int
    missing: int
    submitted: int
