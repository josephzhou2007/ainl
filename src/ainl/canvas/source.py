"""The one interface every upper layer talks to.

Two implementations are planned (ARCHITECTURE.md, L0):
  - ApiSource     REST + GraphQL with a Bearer token. Fast, stable. Default.
  - ScrapeSource  Playwright on real Chrome. Slow, brittle, but survives an
                  admin turning API scopes off. Not built yet.

Upper layers must never import either implementation directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Protocol

from .models import Announcement, Course, CourseStats, Item


class CanvasSource(Protocol):
    def whoami(self) -> dict:
        """Minimal profile: id, name, login_id, time_zone."""

    def courses(self) -> list[Course]:
        """Courses the student has favorited (what the Dashboard shows)."""

    def planner(
        self,
        since: datetime,
        course_ids: Optional[Iterable[str]] = None,
        incomplete_only: bool = True,
    ) -> list[Item]:
        """Unified feed: assignments, quizzes, discussions, announcements."""

    def course_work(
        self,
        course_id: Optional[str] = None,
        include_overdue: bool = True,
        since: Optional[datetime] = None,
    ) -> list[Item]:
        """Submission-centric view of assignments (GraphQL). Includes overdue."""

    def stats(self) -> list[CourseStats]:
        """Per-course Due / Missing / Submitted counts."""

    def announcements(self, unread_only: bool = True) -> list[Announcement]:
        ...

    def tabs(self, course_id: str) -> list[dict]:
        """Course navigation as the student sees it (label, html_url, type, hidden)."""

    def external_tool_url(self, course_id: str, assignment_id: str) -> Optional[str]:
        """LTI launch URL for an external_tool assignment, or None."""
