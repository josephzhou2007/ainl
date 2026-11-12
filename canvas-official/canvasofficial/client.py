"""Canvas over an official access token. Documented REST only.

Deliberately separate from ``ainl.canvas``: nothing in this package uses a
browser session, an internal GraphQL query, a scraped CSRF token, or any other
finding from the reverse-engineering work. Every call here appears in
Instructure's published API docs, so this package can be read, audited or
handed to the university on its own.

That constraint costs something — it is recorded honestly in README.md §Gaps.

    from canvasofficial.client import CanvasClient
    c = CanvasClient.from_env()
    c.whoami()
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

DEFAULT_BASE_URL = "https://canvas.wisc.edu"


class CanvasError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        super().__init__(f"{status} on {path}: {body[:300]}")
        self.status = status
        self.path = path
        self.body = body


class TokenError(CanvasError):
    """401/403: the token is invalid, revoked, or lacks the scope for this call."""


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class CanvasClient:
    base_url: str = DEFAULT_BASE_URL
    token: str = field(default="", repr=False)
    timeout: float = 30.0
    transport: Optional[httpx.BaseTransport] = None   # tests inject MockTransport
    _client: httpx.Client = field(init=False, repr=False)

    # Canvas refills ~50 units/s into a 700-unit bucket; pause rather than get 403'd.
    RATE_FLOOR = 100.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {self.token}",
                # string IDs: Canvas IDs exceed 2^53 and JS clients corrupt them.
                "Accept": "application/json+canvas-string-ids, application/json",
                "User-Agent": "canvasofficial/0.1",
            },
        )

    @classmethod
    def from_env(cls) -> "CanvasClient":
        token = os.environ.get("CANVAS_TOKEN", "").strip()
        if not token:
            raise TokenError(0, "-", "CANVAS_TOKEN is not set. Canvas -> Account -> "
                                     "Settings -> New Access Token, then set the variable.")
        return cls(base_url=os.environ.get("CANVAS_BASE_URL", DEFAULT_BASE_URL).strip()
                   or DEFAULT_BASE_URL, token=token)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CanvasClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport ------------------------------------------------------------

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        r = self._client.request(method, path, **kw)
        remaining = r.headers.get("X-Rate-Limit-Remaining")
        if remaining:
            try:
                if float(remaining) < self.RATE_FLOOR:
                    time.sleep(1.0)
            except ValueError:
                pass
        if r.status_code == 403 and "Rate Limit Exceeded" in r.text:
            time.sleep(5.0)
            r = self._client.request(method, path, **kw)
        # A token that is wrong looks like 401; a token that is right but narrow
        # looks like 403 with an "insufficient scopes" body. Both mean re-auth.
        if r.status_code in (401, 403):
            raise TokenError(r.status_code, path, r.text)
        if r.status_code >= 400:
            raise CanvasError(r.status_code, path, r.text)
        return r

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return json.loads(self.request("GET", path, params=params).text or "null")

    def post(self, path: str, data: Optional[dict] = None, json_body: Optional[dict] = None) -> Any:
        kw = {"json": json_body} if json_body is not None else {"data": data or {}}
        return json.loads(self.request("POST", path, **kw).text or "null")

    def paginate(self, path: str, params: Optional[dict] = None, per_page: int = 100) -> Iterator[Any]:
        r = self.request("GET", path, params={**(params or {}), "per_page": per_page})
        while True:
            yield from (json.loads(r.text or "[]") or [])
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                return
            r = self.request("GET", nxt)

    # -- read -----------------------------------------------------------------

    def whoami(self) -> dict:
        return self.get("/api/v1/users/self/profile")

    def courses(self, favorites_only: bool = True) -> list[dict]:
        path = "/api/v1/users/self/favorites/courses" if favorites_only else "/api/v1/courses"
        return list(self.paginate(path, {"enrollment_state": "active"}))

    def planner(self, start: datetime, end: Optional[datetime] = None) -> list[dict]:
        """The dashboard's unified feed: assignments, quizzes, discussions, notes.

        This is the single most valuable read in the whole API — each item already
        carries the canonical ``html_url``, which is what makes a quiz link land on
        the quiz rather than bouncing through the assignment redirect.
        """
        params = {"start_date": _iso(start)}
        if end:
            params["end_date"] = _iso(end)
        return list(self.paginate("/api/v1/planner/items", params))

    def todo(self) -> list[dict]:
        return list(self.paginate("/api/v1/users/self/todo"))

    def missing_submissions(self) -> list[dict]:
        return list(self.paginate("/api/v1/users/self/missing_submissions",
                                  {"include[]": "course", "filter[]": "submittable"}))

    def assignments(self, course_id: str) -> list[dict]:
        return list(self.paginate(f"/api/v1/courses/{course_id}/assignments",
                                  {"include[]": "submission", "order_by": "due_at"}))

    def assignment(self, course_id: str, assignment_id: str) -> dict:
        return self.get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}",
                        {"include[]": "submission"})

    def submission(self, course_id: str, assignment_id: str) -> dict:
        return self.get(
            f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            {"include[]": "submission_comments"})

    def tabs(self, course_id: str) -> list[dict]:
        return list(self.paginate(f"/api/v1/courses/{course_id}/tabs"))

    def announcements(self, course_ids: list[str], active_only: bool = True) -> list[dict]:
        if not course_ids:
            return []
        return list(self.paginate("/api/v1/announcements", {
            "context_codes[]": [f"course_{c}" for c in course_ids],
            "active_only": str(active_only).lower(),
        }))

    def files(self, course_id: str) -> list[dict]:
        return list(self.paginate(f"/api/v1/courses/{course_id}/files"))

    def absolute(self, url: str) -> str:
        """planner/items returns html_url as a path ("/courses/1/assignments/2").

        Every deep link we hand the student has to survive being opened outside
        the app, so paths get the host put back on.
        """
        return f"{self.base_url}{url}" if url.startswith("/") else url

    def download(self, url: str) -> bytes:
        """Canvas file URLs are absolute and already carry a verifier."""
        r = httpx.get(url, follow_redirects=True, timeout=self.timeout,
                      headers={"Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        return r.content
