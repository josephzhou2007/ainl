"""Submission over the official API.

The boundary that governs this file, restated because it is the whole point of
the project: *the agent moves artifacts the student made; it never produces or
submits work the student did not do.* So:

  - Canvas-native submission of the student's own file or text: allowed.
  - Any vendor platform (WebAssign, McGraw Hill, ...): not here, not ever.

The file upload is a three-step dance and every step has a gotcha:

  1. POST .../submissions/self/files with name+size  -> {upload_url, upload_params}
  2. POST upload_url as multipart, with upload_params FIRST and ``file`` LAST.
     The order is not cosmetic — the S3-style backend ignores fields after the
     file part. No Authorization header here; the URL is pre-signed.
  3. That POST answers 201 with the file JSON, or 3xx to a confirm URL which
     must be GET'd *with* the Bearer token to finalise.

Only then does the file id exist for ``submission[file_ids][]``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import httpx

from .client import CanvasClient


def upload_file(c: CanvasClient, course_id: str, assignment_id: str, path: Path) -> str:
    """Run the three-step upload; return the Canvas file id."""
    data = path.read_bytes()
    step1 = c.post(
        f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions/self/files",
        data={"name": path.name, "size": len(data), "content_type": _guess_type(path)},
    )
    upload_url, params = step1["upload_url"], step1.get("upload_params", {})

    # params first, file last — see module docstring.
    files = [(k, (None, str(v))) for k, v in params.items()]
    files.append(("file", (path.name, data, _guess_type(path))))
    r = httpx.post(upload_url, files=files, timeout=300.0, follow_redirects=False)

    if r.status_code in (301, 302, 303):
        loc = r.headers["location"]
        confirmed = httpx.get(loc, timeout=60.0, follow_redirects=True,
                              headers={"Authorization": f"Bearer {c.token}"})
        confirmed.raise_for_status()
        return str(confirmed.json()["id"])
    r.raise_for_status()
    return str(r.json()["id"])


def submit_files(c: CanvasClient, course_id: str, assignment_id: str,
                 paths: list[Path], comment: str = "") -> dict:
    ids = [upload_file(c, course_id, assignment_id, p) for p in paths]
    return _submit(c, course_id, assignment_id, {
        "submission[submission_type]": "online_upload",
        **{"submission[file_ids][]": ids},
    }, comment)


def submit_text(c: CanvasClient, course_id: str, assignment_id: str,
                html: str, comment: str = "") -> dict:
    return _submit(c, course_id, assignment_id, {
        "submission[submission_type]": "online_text_entry",
        "submission[body]": html,
    }, comment)


def submit_url(c: CanvasClient, course_id: str, assignment_id: str,
               url: str, comment: str = "") -> dict:
    return _submit(c, course_id, assignment_id, {
        "submission[submission_type]": "online_url",
        "submission[url]": url,
    }, comment)


def _submit(c: CanvasClient, course_id: str, assignment_id: str,
            body: dict, comment: str) -> dict:
    if comment:
        body["comment[text_comment]"] = comment
    return c.post(f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
                  data=body)


_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".txt": "text/plain", ".md": "text/markdown",
          ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          ".zip": "application/zip", ".py": "text/x-python"}


def _guess_type(path: Path) -> str:
    return _TYPES.get(path.suffix.lower(), "application/octet-stream")
