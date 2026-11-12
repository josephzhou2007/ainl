"""Read-only capability probe: what does *this* token actually allow?

A Canvas access token is not all-or-nothing. If the institution issued it from a
scoped developer key, individual endpoints answer 403 "insufficient scopes"
while the token itself is perfectly valid. UW also disables some endpoints
outright (``page_views`` answered 403 under a browser session too). The only
honest way to know is to call each one and record what came back.

Every check here is a GET or a harmless GraphQL read. Nothing is written, so
this is safe to run against a live account at any time.

    python -m canvasofficial --probe

The table it prints is the input to one decision: for each capability, can we
retire the reverse-engineered path, or must we keep it as fallback?
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .client import CanvasClient, CanvasError, TokenError

OK, SCOPE, AUTH, MISSING, ERROR = "OK", "SCOPE", "AUTH", "MISSING", "ERROR"


@dataclass
class Check:
    name: str
    verdict: str
    detail: str = ""
    n: Optional[int] = None


def _run(name: str, fn: Callable[[], object]) -> Check:
    try:
        out = fn()
    except TokenError as e:
        # 403 with a token that works elsewhere == scope, not a bad token.
        verdict = SCOPE if e.status == 403 else AUTH
        return Check(name, verdict, _reason(e.body))
    except CanvasError as e:
        return Check(name, MISSING if e.status == 404 else ERROR, f"{e.status} {_reason(e.body)}")
    except Exception as e:  # network, JSON, SDK
        return Check(name, ERROR, f"{type(e).__name__}: {e}"[:120])
    n = len(out) if isinstance(out, (list, dict)) else None
    return Check(name, OK, "", n)


def _reason(body: str) -> str:
    try:
        d = json.loads(body)
        msgs = d.get("errors") or d.get("message") or d
        return json.dumps(msgs)[:120]
    except Exception:
        return body[:120].replace("\n", " ")


_GQL_COURSE_WORK = """
query Probe($userId: ID!) {
  legacyNode(_id: $userId, type: User) { ... on User {
    courseWorkSubmissionsConnection(first: 1, includeOverdue: true) { nodes { _id } }
  } }
}
"""


def run_probe(c: CanvasClient) -> list[Check]:
    checks: list[Check] = []
    checks.append(_run("users/self/profile", c.whoami))
    try:
        uid = str(c.whoami()["id"])
    except Exception:
        return checks  # nothing else can work

    checks.append(_run("favorites/courses", lambda: c.courses(True)))
    try:
        course_ids = [str(x["id"]) for x in c.courses(True)]
    except Exception:
        course_ids = []
    cid = course_ids[0] if course_ids else None

    now = datetime.now(timezone.utc)
    checks.append(_run("planner/items", lambda: c.planner(now - timedelta(days=1),
                                                          now + timedelta(days=14))))
    checks.append(_run("users/self/todo", c.todo))
    checks.append(_run("missing_submissions", c.missing_submissions))
    checks.append(_run("announcements", lambda: c.announcements(course_ids[:5])))

    if cid:
        checks.append(_run(f"courses/{cid}/assignments", lambda: c.assignments(cid)))
        checks.append(_run(f"courses/{cid}/tabs", lambda: c.tabs(cid)))
        checks.append(_run(f"courses/{cid}/files", lambda: c.files(cid)))
        checks.append(_run(f"courses/{cid}/modules",
                           lambda: list(c.paginate(f"/api/v1/courses/{cid}/modules",
                                                   {"include[]": "items"}))))
        try:
            aid = str(next(a["id"] for a in c.assignments(cid)))
            checks.append(_run("submissions/self", lambda: c.submission(cid, aid)))
        except (StopIteration, Exception):
            pass

    # Internal, but the whole M1 brief depends on it: does GraphQL accept a Bearer token?
    checks.append(_run("graphql courseWorkSubmissions", lambda: _gql(c, uid)))
    # Known-disabled at UW under cookies; re-check under a token.
    checks.append(_run("users/self/page_views",
                       lambda: c.get(f"/api/v1/users/{uid}/page_views", {"per_page": 1})))
    return checks


def _gql(c: CanvasClient, user_id: str) -> dict:
    out = c.post("/api/graphql", json_body={"query": _GQL_COURSE_WORK,
                                            "variables": {"userId": user_id}})
    if out.get("errors"):
        raise CanvasError(200, "/api/graphql", json.dumps(out["errors"]))
    return out.get("data") or {}


_MARK = {OK: "PASS", SCOPE: "SCOPE", AUTH: "AUTH", MISSING: "404", ERROR: "FAIL"}


def format_table(checks: list[Check]) -> str:
    w = max((len(k.name) for k in checks), default=10)
    lines = [f"{'endpoint'.ljust(w)}  result  n     note", f"{'-' * w}  ------  ----  ----"]
    for k in checks:
        n = "" if k.n is None else str(k.n)
        lines.append(f"{k.name.ljust(w)}  {_MARK[k.verdict].ljust(6)}  {n.ljust(4)}  {k.detail}")
    passed = sum(1 for k in checks if k.verdict == OK)
    lines.append("")
    lines.append(f"{passed}/{len(checks)} usable with this token")
    return "\n".join(lines)
