"""ApiSource — CanvasSource over REST + GraphQL.

Endpoints and their provenance: docs/canvas-api-map.md.
Auth is injected (auth.py); this class does not care which kind.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable, Optional

import httpx

if TYPE_CHECKING:
    from .session import BrowserSession

from ..config import CanvasConfig
from ._http import CanvasAuthError, CanvasHTTP, CanvasHTTPError
from .auth import Auth, BearerAuth
from .models import Announcement, Course, CourseStats, Item

log = logging.getLogger("ainl.canvas")

# GraphQL, trimmed from what the Canvas dashboard sends (see api-map §4).

_Q_COURSE_WORK = """
query GetUserCourseWork($userId: ID!, $first: Int, $after: String, $courseFilter: String,
                        $startDate: ISO8601DateTime, $includeOverdue: Boolean,
                        $includeNoDueDate: Boolean) {
  legacyNode(_id: $userId, type: User) { ... on User {
    courseWorkSubmissionsConnection(first: $first, after: $after, courseFilter: $courseFilter,
        startDate: $startDate, includeOverdue: $includeOverdue,
        includeNoDueDate: $includeNoDueDate, onlySubmitted: false) {
      nodes {
        _id cachedDueDate submittedAt late missing excused state
        assignment { _id name dueAt pointsPossible htmlUrl submissionTypes state published
                     quiz { _id } discussion { _id } course { _id name } }
      }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
"""

_Q_STATS = """
query GetUserCourseStatistics($userId: ID!, $startDate: ISO8601DateTime!, $endDate: ISO8601DateTime!) {
  legacyNode(_id: $userId, type: User) { ... on User {
    enrollments(currentOnly: true) { course { _id name
      submissionStatistics {
        submissionsDueCount(startDate: $startDate, endDate: $endDate)
        missingSubmissionsCount
        submissionsSubmittedCount
      }
    } }
  } }
}
"""

_Q_ANNOUNCEMENTS = """
query GetUserAnnouncements($userId: ID!, $first: Int!, $after: String, $readState: String) {
  legacyNode(_id: $userId, type: User) { ... on User {
    discussionParticipantsConnection(first: $first, after: $after,
        filter: {isAnnouncement: true, readState: $readState}) {
      nodes { id read discussionTopic { _id title message createdAt contextName contextId
                                        author { _id name } } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
"""


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ApiSource:
    def __init__(self, base_url: str, auth: Auth, *, transport: Optional[httpx.BaseTransport] = None,
                 session: "Optional[BrowserSession]" = None):
        self.base_url = base_url.rstrip("/")
        self._http = CanvasHTTP(self.base_url, auth, transport=transport)
        self._user_id: Optional[str] = None
        self._session = session          # set for cookie auth; lets close() persist rotations
        self._channel = "token" if isinstance(auth, BearerAuth) else "cookie"

    @classmethod
    def from_env(cls, *, interactive: bool = True) -> "ApiSource":
        """Bearer token if CANVAS_TOKEN is set; otherwise a browser session.

        The token is the primary channel (canvas-official/README.md); the
        browser session is the documented fallback for when it is revoked or
        an admin narrows its scopes. The choice also honours the /channel pin.
        """
        from . import channel
        return channel.open_source(interactive=interactive)

    def recover_auth(self, exc: CanvasAuthError, *, interactive: bool = False) -> "ApiSource | None":
        """Token died mid-run -> hand back a cookie-channel replacement, or None.

        Policy (the student's call): token first, fall back to the reverse-
        engineered browser session on failure. The fallback is *not* silent —
        callers are expected to tell the student which channel they ended up
        on, otherwise a revoked token looks exactly like a working one.

        A cookie run that dies has no second channel: it re-reads the browser,
        which holds a new session once someone has signed in there (the login
        button); otherwise NeedsCanvasLogin, and a human has to sign in.

        Pinning the channel from Telegram (/channel token) disables the
        fallback, because a pinned "token" usually means "I want to know
        whether the token still works".
        """
        from . import channel

        if channel.preference() == "token":
            log.info("channel pinned to token; not falling back (%s)", exc)
            return None
        if self._channel != "token":
            if not interactive:
                # A cookie run has no second channel, but the browser may hold
                # a NEW session by now (signed in through the login button):
                # re-read it. None of that -> NeedsCanvasLogin for the caller.
                try:
                    return channel.open_source("cookie")
                except channel.NeedsCanvasLogin:
                    raise
                except Exception as e:             # playwright missing, no window, ...
                    log.warning("cookie re-read unavailable: %s", e)
                    return None
            from .session import BrowserSession
            sess = BrowserSession(self.base_url)
            return ApiSource(self.base_url, sess.login_interactive(), session=sess)

        log.warning("token channel failed (%s); falling back to browser session", exc)
        try:
            return channel.open_source("cookie", interactive=interactive)
        except channel.NeedsCanvasLogin:
            raise                              # the caller tells the student what to do
        except Exception as e:                 # playwright missing, no window, ...
            log.warning("fallback unavailable: %s", e)
            return None

    def sessionless_launch(self, course_id: str, assignment_id: str) -> str:
        """A one-shot URL that performs this assignment's LTI launch.

        Verified as a *student* 2026-09-19: launch_type=assessment is allowed,
        and opening the URL lands on the vendor's copy of that assignment. This
        is how Canvas itself hands a student over to WebAssign, so it needs no
        vendor password and works for anyone with the Canvas session — which
        the dashboard route did not.

        The URL carries a verifier; it is a credential. Never log it.
        """
        out = self._http.get(
            f"/api/v1/courses/{course_id}/external_tools/sessionless_launch",
            {"assignment_id": assignment_id, "launch_type": "assessment"})
        return out["url"]

    def close(self) -> None:
        try:
            if self._session is not None:
                self._session.save_cookies(self._http.cookies)
        finally:
            self._http.close()

    # -- identity -------------------------------------------------------------

    def whoami(self) -> dict:
        p = self._http.get("/api/v1/users/self/profile")
        self._user_id = str(p["id"])
        return {k: p.get(k) for k in ("id", "name", "login_id", "time_zone")}

    @property
    def user_id(self) -> str:
        if self._user_id is None:
            self.whoami()
        return self._user_id  # type: ignore[return-value]

    # -- courses --------------------------------------------------------------

    def courses(self) -> list[Course]:
        rows = self._http.paginate(
            "/api/v1/users/self/favorites/courses",
            {"include[]": ["term"], "sort": "nickname"},
        )
        return [Course.from_json(r) for r in rows]

    def tabs(self, course_id: str) -> list[dict]:
        rows = self._http.get(f"/api/v1/courses/{course_id}/tabs") or []
        return [
            {"id": t.get("id"), "label": t.get("label"), "type": t.get("type"),
             "html_url": t.get("html_url"), "hidden": bool(t.get("hidden"))}
            for t in rows
        ]

    # -- the feed -------------------------------------------------------------

    def planner(
        self,
        since: datetime,
        course_ids: Optional[Iterable[str]] = None,
        incomplete_only: bool = True,
        until: Optional[datetime] = None,
    ) -> list[Item]:
        """The unified feed from ``since`` — and, when ``until`` is given, no further.

        Without ``until`` Canvas returns the rest of the semester: 304 items
        over 7 pages, 7.8 s, of which /todo kept the 26 inside its 4-day
        window. Let the server do the filtering it is already able to do.
        """
        params: dict = {"start_date": _iso(since), "order": "asc"}
        if until is not None:
            params["end_date"] = _iso(until)
        if incomplete_only:
            params["filter"] = "incomplete_items"
        if course_ids:
            params["context_codes[]"] = [f"course_{c}" for c in course_ids]
        rows = self._http.paginate("/api/v1/planner/items", params, per_page=50)
        return [Item.from_planner(r, self.base_url) for r in rows]

    def course_work(
        self,
        course_id: Optional[str] = None,
        include_overdue: bool = True,
        since: Optional[datetime] = None,
    ) -> list[Item]:
        items: list[Item] = []
        after: Optional[str] = None
        while True:
            data = self._http.graphql(_Q_COURSE_WORK, {
                "userId": self.user_id,
                "first": 50,
                "after": after,
                "courseFilter": f"course_{course_id}" if course_id else None,
                "startDate": _iso(since) if since else None,
                "includeOverdue": include_overdue,
                "includeNoDueDate": True,
            })
            conn = (data.get("legacyNode") or {}).get("courseWorkSubmissionsConnection") or {}
            items.extend(Item.from_course_work(n) for n in conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return items
            after = page.get("endCursor")

    def stats(self) -> list[CourseStats]:
        now = datetime.now(timezone.utc)
        data = self._http.graphql(_Q_STATS, {
            "userId": self.user_id,
            "startDate": _iso(now),
            "endDate": _iso(now.replace(year=now.year + 1)),
        })
        # enrollments come back one per *section*; a course with two sections
        # (MATH222 003+004) appears twice with identical numbers. Dedupe by course.
        seen: dict[str, CourseStats] = {}
        for e in (data.get("legacyNode") or {}).get("enrollments") or []:
            c = e.get("course") or {}
            cid = str(c.get("_id"))
            if cid in seen:
                continue
            s = c.get("submissionStatistics") or {}
            seen[cid] = CourseStats(
                course_id=cid, course_name=c.get("name") or "",
                due=int(s.get("submissionsDueCount") or 0),
                missing=int(s.get("missingSubmissionsCount") or 0),
                submitted=int(s.get("submissionsSubmittedCount") or 0),
            )
        return list(seen.values())

    def announcements(self, unread_only: bool = True) -> list[Announcement]:
        out: list[Announcement] = []
        after: Optional[str] = None
        while True:
            data = self._http.graphql(_Q_ANNOUNCEMENTS, {
                "userId": self.user_id, "first": 50, "after": after,
                "readState": "unread" if unread_only else None,
            })
            conn = (data.get("legacyNode") or {}).get("discussionParticipantsConnection") or {}
            out.extend(Announcement.from_graphql(n) for n in conn.get("nodes") or [])
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return out
            after = page.get("endCursor")

    # -- assignments (full list per course) -----------------------------------

    def assignments_index(self, course_ids: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """assignment id -> submission_types, for every assignment in the given courses.

        planner/items omits submission_types and GraphQL course_work only covers
        assignments that already have a submission row, so this REST sweep is the
        one reliable source. One request per course (paginated)."""
        return {k: v["types"] for k, v in self.assignments_meta(course_ids).items()}

    def assignments_meta(self, course_ids: Iterable[str]) -> dict[str, dict]:
        """assignment id -> {"types": submission_types, "tool": external tool url}.

        The same sweep as assignments_index, keeping one more field it was
        already being sent. The tool url is what says WHICH vendor an
        external_tool item hands off to — and so whether it can be done in
        chat — without opening a browser or making a second request.
        """
        return self._sweep({cid: None for cid in dict.fromkeys(course_ids)})

    def assignments_meta_for(self, wanted: dict[str, list[str]]) -> dict[str, dict]:
        """The same, but only for the assignments named: {course_id: [ids]}.

        /todo needs the 31 items in its window, and was fetching all 387 in the
        four courses to get them — SPANISH101 alone has 307, which cost 7.5 s of
        a 10.3 s sweep. Canvas filters server-side with assignment_ids[], so
        asking for exactly what is needed takes 1.5 s. The same mistake as the
        unbounded planner: a filter we had, applied on the wrong side.
        """
        return self._sweep({cid: list(ids) for cid, ids in wanted.items() if ids})

    def _sweep(self, wanted: dict) -> dict[str, dict]:
        def one(cid: str, ids) -> list:
            params: dict = {"exclude_response_fields[]": ["description", "rubric"]}
            if ids:
                params["assignment_ids[]"] = ids
            return list(self._http.paginate(f"/api/v1/courses/{cid}/assignments", params))

        meta: dict[str, dict] = {}
        courses = list(wanted.items())
        if len(courses) > 1:
            # The courses are independent; four requests in a row is four round
            # trips of waiting that can overlap. httpx.Client is thread-safe.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(len(courses), 6)) as pool:
                pages = list(pool.map(lambda kv: one(*kv), courses))
        else:
            pages = [one(*kv) for kv in courses]
        for rows in pages:
            for a in rows:
                meta[str(a["id"])] = {
                    "types": tuple(a.get("submission_types") or ()),
                    "tool": (a.get("external_tool_tag_attributes") or {}).get("url") or "",
                }
        return meta

    # -- one assignment, in full ---------------------------------------------

    def assignment_detail(self, course_id: str, assignment_id: str) -> dict:
        a = self._http.get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
                           {"include[]": ["submission"]}) or {}
        sub = a.get("submission") or {}
        return {
            "id": str(a.get("id")), "course_id": course_id,
            "name": a.get("name") or "", "description_html": a.get("description") or "",
            "due_at": a.get("due_at"), "points_possible": a.get("points_possible"),
            "submission_types": tuple(a.get("submission_types") or ()),
            "allowed_extensions": tuple(a.get("allowed_extensions") or ()),
            "html_url": a.get("html_url") or "", "locked": bool(a.get("locked_for_user")),
            # which vendor an external_tool assignment hands off to
            "external_tool_url": ((a.get("external_tool_tag_attributes") or {}).get("url") or ""),
            "lock_explanation": a.get("lock_explanation") or "",
            "attempt": sub.get("attempt"), "submitted_at": sub.get("submitted_at"),
            "workflow_state": sub.get("workflow_state"),
        }

    def file_info(self, file_id: str) -> dict:
        f = self._http.get(f"/api/v1/files/{file_id}") or {}
        return {"id": str(f.get("id")), "name": f.get("display_name") or f.get("filename") or "",
                "size": int(f.get("size") or 0), "content_type": f.get("content-type") or "",
                "url": f.get("url") or ""}

    def download_file(self, file_id: str, max_bytes: int = 20 * 1024 * 1024) -> tuple[str, bytes]:
        """Canvas file → (name, bytes).

        Two steps — ask Canvas where the file is, then fetch it — and what the
        first step answers depends on the channel (measured 2026-09-21):

          token   a signed link on a storage host. Anyone holding it may fetch,
                  and it must be fetched WITHOUT our credentials: they are not
                  that host's business, and a signed link refuses them anyway.
          cookie  a plain canvas.wisc.edu/files/…/download link, which needs the
                  login. Fetched without it, Canvas answers 200 with a 3.7 KB
                  HTML page — which was sent to the student named "….pdf".

        So: our own host gets our credentials, any other host does not. And a
        200 is not a file — the size Canvas reported is checked against what
        arrived.
        """
        info = self.file_info(file_id)
        if info["size"] > max_bytes:
            raise CanvasHTTPError(413, f"/api/v1/files/{file_id}", f"{info['size']} bytes > cap")
        url = info["url"]
        if url.startswith("http") and not url.startswith(self.base_url + "/"):
            r = httpx.get(url, follow_redirects=True, timeout=60.0)
        else:
            r = self._http._request("GET", url or f"/files/{file_id}/download",
                                    params=None if url else {"download_frd": 1},
                                    follow_redirects=True)
        if r.status_code >= 400:
            raise CanvasHTTPError(r.status_code, url.split("?")[0][:80], r.text)
        got = len(r.content)
        if (info["size"] and got != info["size"]) or r.content[:64].lstrip().lower().startswith(b"<!doctype html"):
            raise CanvasHTTPError(502, url.split("?")[0][:80],
                                  f"expected {info['size']} bytes of {info['content_type'] or 'file'}, "
                                  f"got {got} of {r.headers.get('content-type', '?')} — not the file")
        return info["name"], r.content

    # -- submissions (L7-A: the student's own text / url / file) ---------------

    def _submit(self, course_id: str, assignment_id: str, submission: dict) -> dict:
        r = self._http.post(f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
                            {"submission": submission}) or {}
        return {"submitted_at": r.get("submitted_at"), "attempt": r.get("attempt"),
                "workflow_state": r.get("workflow_state"), "late": bool(r.get("late")),
                "preview_url": r.get("preview_url"),
                "attachments": [x.get("display_name") for x in r.get("attachments") or []]}

    def submit_text(self, course_id: str, assignment_id: str, body_html: str) -> dict:
        return self._submit(course_id, assignment_id, {"submission_type": "online_text_entry", "body": body_html})

    def submit_url(self, course_id: str, assignment_id: str, url: str) -> dict:
        return self._submit(course_id, assignment_id, {"submission_type": "online_url", "url": url})

    def _upload_submission_file(self, course_id: str, assignment_id: str, filename: str, data: bytes,
                                content_type: str) -> str:
        """Official 3-step upload (docs/canvas-submit-protocol.md §官方 REST 等价) → Canvas file id."""
        pending = self._http.post(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self/files",
            {"name": filename, "size": len(data), "content_type": content_type},
        ) or {}
        upload_url, params = pending.get("upload_url"), pending.get("upload_params") or {}
        if not upload_url:
            raise CanvasHTTPError(500, "submissions/self/files", f"no upload_url in {pending!r:.200}")
        # upload_params first, file last — order matters for S3/inst-fs
        r = httpx.post(upload_url, data=params, files={"file": (filename, data, content_type)},
                       follow_redirects=False, timeout=120.0)
        if r.status_code in (301, 302, 303):
            confirmed = self._http.get(r.headers["location"]) or {}
        elif r.status_code < 300:
            confirmed = r.json()
        else:
            raise CanvasHTTPError(r.status_code, upload_url, r.text)
        return str(confirmed.get("id"))

    def submit_files(self, course_id: str, assignment_id: str,
                     files: list[tuple[str, bytes, str]]) -> dict:
        """Upload every (filename, bytes, content_type), then ONE online_upload
        submission carrying all file_ids — so a 5-photo album is one attempt with
        5 attachments, not 5 attempts of 1."""
        if not files:
            raise ValueError("submit_files: no files")
        file_ids = [self._upload_submission_file(course_id, assignment_id, name, data, ctype)
                    for name, data, ctype in files]
        return self._submit(course_id, assignment_id, {"submission_type": "online_upload", "file_ids": file_ids})

    def submit_file(self, course_id: str, assignment_id: str, filename: str, data: bytes,
                    content_type: str = "application/octet-stream") -> dict:
        return self.submit_files(course_id, assignment_id, [(filename, data, content_type)])

    def post_discussion_entry(self, course_id: str, topic_id: str, message_html: str) -> dict:
        r = self._http.post(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries",
                            {"message": message_html}) or {}
        return {"id": str(r.get("id")), "created_at": r.get("created_at")}

    # -- external tools -------------------------------------------------------

    def external_tool_url(self, course_id: str, assignment_id: str) -> Optional[str]:
        a = self._http.get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}")
        tag = (a or {}).get("external_tool_tag_attributes") or {}
        return tag.get("url") or None
