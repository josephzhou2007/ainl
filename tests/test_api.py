"""ApiSource against a fake Canvas. No network.

Fixture shapes mirror docs/canvas-api-map.md; values are synthetic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from ainl.canvas import ApiSource, Kind
from ainl.canvas.auth import BearerAuth
from ainl.config import CanvasConfig

BASE = "https://canvas.example.edu"
TOKEN = "tok-secret-123"


def _json(body, status=200, headers=None):
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    return httpx.Response(status, text=json.dumps(body), headers=h)


PLANNER_PAGE1 = [
    {
        "context_type": "Course", "course_id": "100002", "context_name": "SPANISH101",
        "plannable_type": "quiz", "plannable_id": "743132",
        "plannable_date": "2026-09-02T13:00:00Z",
        "plannable": {"id": "743132", "title": "Orientation Quiz",
                      "due_at": "2026-09-02T13:00:00Z", "points_possible": 12.0},
        "submissions": {"submitted": False, "missing": True, "late": False, "graded": False},
        "html_url": "/courses/100002/quizzes/743132", "new_activity": False,
    },
    {
        "context_type": "Course", "course_id": "100002", "context_name": "SPANISH101",
        "plannable_type": "announcement", "plannable_id": "2490601",
        "plannable_date": "2026-09-01T10:00:00Z",
        "plannable": {"id": "2490601", "title": "Welcome", "read_state": "unread"},
        "submissions": False,
        "html_url": "/courses/100002/discussion_topics/2490601", "new_activity": True,
    },
]
PLANNER_PAGE2 = [
    {
        "context_type": "Course", "course_id": "100003", "context_name": "MATH222",
        "plannable_type": "assignment", "plannable_id": "3152478",
        "plannable_date": "2026-09-17T23:59:00-05:00",
        "plannable": {"id": "3152478", "title": "7.2 Homework", "due_at": "2026-09-17T23:59:00-05:00",
                      "points_possible": 10.0, "submission_types": ["external_tool"]},
        "submissions": {"submitted": False, "missing": False, "late": False, "graded": False},
        "html_url": "/courses/100003/assignments/3152478", "new_activity": False,
    },
]


def fake_canvas(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    # every call must carry the two headers Canvas needs
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "canvas-string-ids" in request.headers["accept"]

    if p == "/api/v1/users/self/profile":
        return httpx.Response(200, text='while(1);{"id":"701304","name":"Test Student","login_id":"t@x.edu","time_zone":"America/Chicago"}',
                              headers={"content-type": "application/json"})

    if p == "/api/v1/planner/items":
        if request.url.params.get("page") == "2":
            return _json(PLANNER_PAGE2)
        assert request.url.params["filter"] == "incomplete_items"
        return _json(PLANNER_PAGE1, headers={
            "Link": f'<{BASE}/api/v1/planner/items?page=2&per_page=50>; rel="next", '
                    f'<{BASE}/api/v1/planner/items?page=1&per_page=50>; rel="first"'
        })

    if p == "/api/v1/users/self/favorites/courses":
        return _json([{"id": "100003", "name": "MATH222: Calc 2", "course_code": "FA26 MATH 222",
                       "term": {"name": "Fall 2026"}, "default_view": "modules",
                       "time_zone": "America/Chicago", "calendar": {"ics": f"{BASE}/feeds/x.ics"}}])

    if p == "/api/v1/courses/100003/assignments":
        assert "description" in request.url.params.get_list("exclude_response_fields[]")
        return _json([{"id": "3152478", "submission_types": ["external_tool"]},
                      {"id": "3152519", "submission_types": ["online_upload"]},
                      {"id": "3152999", "submission_types": ["not_graded"]}])

    if p == "/api/v1/courses/100003/assignments/3152478":
        return _json({"id": "3152478", "external_tool_tag_attributes": {
            "url": "https://gateway.cengage.com/rest/launchBasicLTI/1/2/3/activityId/ABC"}})

    if p == "/api/graphql":
        body = json.loads(request.content)
        assert body["variables"]["userId"] == "701304"
        if "GetUserCourseStatistics" in body["query"]:
            return _json({"data": {"legacyNode": {"enrollments": [
                {"course": {"_id": "100003", "name": "MATH222", "submissionStatistics":
                    {"submissionsDueCount": 48, "missingSubmissionsCount": 0, "submissionsSubmittedCount": 0}}}]}}})
        if "GetUserCourseWork" in body["query"]:
            assert body["variables"]["includeOverdue"] is True
            return _json({"data": {"legacyNode": {"courseWorkSubmissionsConnection": {
                "nodes": [{"_id": "1", "cachedDueDate": "2026-09-14T15:00:00Z", "submittedAt": None,
                           "late": True, "missing": True, "excused": False, "state": "unsubmitted",
                           "assignment": {"_id": "3228932", "name": "Problem Set 1", "dueAt": "2026-09-14T15:00:00Z",
                                          "pointsPossible": 20, "htmlUrl": f"{BASE}/courses/100001/assignments/3228932",
                                          "submissionTypes": ["online_upload"], "state": "published", "published": True,
                                          "quiz": None, "discussion": None,
                                          "course": {"_id": "100001", "name": "PHYSICS247"}}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
        if "GetUserAnnouncements" in body["query"]:
            return _json({"data": {"legacyNode": {"discussionParticipantsConnection": {
                "nodes": [{"id": "x", "read": False, "discussionTopic": {
                    "_id": "2520105", "title": "MLC Math Path", "message": "<p>Hi</p>",
                    "createdAt": "2026-09-10T12:00:00Z", "contextName": "MATH222", "contextId": "100003",
                    "author": {"_id": "9", "name": "Janine"}}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})

    return httpx.Response(404, text="not found")


@pytest.fixture
def src():
    s = ApiSource(BASE, BearerAuth(TOKEN), transport=httpx.MockTransport(fake_canvas))
    yield s
    s.close()


def test_token_never_in_repr():
    cfg = CanvasConfig(base_url=BASE, token=TOKEN)
    assert TOKEN not in repr(cfg)


def test_whoami_strips_while1(src):
    me = src.whoami()
    assert me["id"] == "701304" and me["name"] == "Test Student"


def test_planner_follows_link_pagination_and_normalizes(src):
    items = src.planner(since=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert [i.kind for i in items] == [Kind.QUIZ, Kind.ANNOUNCEMENT, Kind.ASSIGNMENT]

    quiz, ann, hw = items
    # canonical URL comes straight from planner, not synthesized from /assignments/
    assert quiz.canonical_url == f"{BASE}/courses/100002/quizzes/743132"
    assert quiz.missing is True and quiz.actionable
    assert ann.read is False and ann.actionable and ann.new_activity
    assert hw.is_external and hw.submission_types == ("external_tool",)
    assert hw.due_at.utcoffset().total_seconds() == -5 * 3600


def test_course_work_includes_overdue(src):
    (ps1,) = src.course_work()
    assert ps1.title == "Problem Set 1" and ps1.late and ps1.missing
    assert ps1.kind == Kind.ASSIGNMENT and ps1.course_id == "100001"


def test_stats(src):
    (m,) = src.stats()
    assert (m.due, m.missing, m.submitted) == (48, 0, 0)


def test_announcements(src):
    (a,) = src.announcements()
    assert a.title == "MLC Math Path" and a.author == "Janine" and not a.read


def test_assignments_index_dedupes_courses(src):
    idx = src.assignments_index(["100003", "100003"])
    assert idx == {"3152478": ("external_tool",), "3152519": ("online_upload",), "3152999": ("not_graded",)}


def test_courses_and_external_host(src):
    (c,) = src.courses()
    assert c.default_view == "modules" and c.ics_url.endswith(".ics")
    url = src.external_tool_url("100003", "3152478")
    assert url.startswith("https://gateway.cengage.com/")
    items = src.planner(since=datetime(2026, 9, 1, tzinfo=timezone.utc))
    hw = items[-1].with_external_host(url)
    assert hw.external_host == "gateway.cengage.com"


def _files_canvas(link: str, served: bytes, seen: list):
    """A Canvas that reports a 9-byte PDF at ``link`` and serves ``served`` for it."""
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.host, req.url.path, "authorization" in req.headers))
        if req.url.path == "/api/v1/files/7":
            return _json({"id": 7, "display_name": "ps2.pdf", "size": 9,
                          "content-type": "application/pdf", "url": link})
        return httpx.Response(200, content=served)
    return handler


def test_a_file_on_our_own_host_is_fetched_with_our_credentials():
    """The cookie channel's link is canvas/files/…/download: it needs the login."""
    seen: list = []
    s = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(
        _files_canvas(f"{BASE}/files/7/download?download_frd=1", b"%PDF-1234", seen)))
    assert s.download_file("7") == ("ps2.pdf", b"%PDF-1234")
    assert seen[-1] == ("canvas.example.edu", "/files/7/download", True)


def test_a_signed_link_elsewhere_never_sees_our_credentials(monkeypatch):
    got = {}
    def fake_get(url, **kw):
        got.update(url=url, kw=kw)
        return httpx.Response(200, content=b"%PDF-1234")
    monkeypatch.setattr("ainl.canvas.api.httpx.get", fake_get)
    s = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(
        _files_canvas("https://storage.example.net/f/7?sig=abc", b"", [])))
    assert s.download_file("7")[1] == b"%PDF-1234"
    assert got["url"].startswith("https://storage.example.net/") and "headers" not in got["kw"]


def test_a_login_page_is_not_a_file():
    """200 + HTML is what Canvas sends when the fetch was not logged in. It
    reached a student as a 3.7 KB 'ps2.pdf' (2026-09-21)."""
    from ainl.canvas._http import CanvasHTTPError
    s = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(
        _files_canvas(f"{BASE}/files/7/download", b"\n<!DOCTYPE html><html>sign in</html>", [])))
    with pytest.raises(CanvasHTTPError, match="not the file"):
        s.download_file("7")
