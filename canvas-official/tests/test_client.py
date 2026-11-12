"""Network-free tests: httpx.MockTransport stands in for Canvas."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from canvasofficial.client import CanvasClient, CanvasError, TokenError  # noqa: E402
from canvasofficial.probe import AUTH, OK, SCOPE, Check, format_table, _run  # noqa: E402


def client(handler) -> CanvasClient:
    return CanvasClient(base_url="https://canvas.test", token="tok",
                        transport=httpx.MockTransport(handler))


def json_response(payload, status_code: int = 200, **kw) -> httpx.Response:
    return httpx.Response(status_code, text=json.dumps(payload),
                          headers={"content-type": "application/json", **kw.pop("headers", {})},
                          **kw)


# -- transport ---------------------------------------------------------------

def test_bearer_and_string_ids_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return json_response({"id": "701304"})

    client(handler).whoami()
    assert seen["authorization"] == "Bearer tok"
    assert "canvas-string-ids" in seen["accept"]


def test_403_is_scope_not_auth():
    def handler(_):
        return httpx.Response(403, text=json.dumps({"errors": [{"message": "insufficient scopes"}]}))

    with pytest.raises(TokenError) as e:
        client(handler).whoami()
    assert e.value.status == 403


def test_404_is_canvas_error_not_token_error():
    def handler(_):
        return httpx.Response(404, text="{}")

    with pytest.raises(CanvasError) as e:
        client(handler).get("/api/v1/nope")
    assert not isinstance(e.value, TokenError)


def test_paginate_follows_link_header():
    pages = {
        "/api/v1/courses": json_response(
            [{"id": "1"}],
            headers={"link": '<https://canvas.test/api/v1/courses?page=2>; rel="next"'}),
        "/api/v1/courses?page=2": json_response([{"id": "2"}]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (f"?page={request.url.params['page']}"
                                  if "page" in request.url.params else "")
        return pages[key]

    out = list(client(handler).paginate("/api/v1/courses"))
    assert [c["id"] for c in out] == ["1", "2"]


def test_planner_sends_iso_utc_window():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return json_response([])

    now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
    client(handler).planner(now, now + timedelta(days=4))
    assert seen["start_date"] == "2026-09-19T12:00:00Z"
    assert seen["end_date"] == "2026-09-23T12:00:00Z"


def test_announcements_needs_context_codes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("context_codes[]") == ["course_1", "course_2"]
        return json_response([])

    c = client(handler)
    assert c.announcements([]) == []          # no courses -> no call at all
    assert c.announcements(["1", "2"]) == []


# -- probe classification ----------------------------------------------------

def test_probe_classifies_403_as_scope_and_401_as_auth():
    def scope():
        raise TokenError(403, "/x", '{"errors":[{"message":"insufficient scopes"}]}')

    def bad():
        raise TokenError(401, "/x", "Invalid access token.")

    assert _run("a", scope).verdict == SCOPE
    assert _run("b", bad).verdict == AUTH
    assert _run("c", lambda: [1, 2, 3]).n == 3


def test_format_table_counts_usable():
    table = format_table([Check("a", OK), Check("b", SCOPE, "nope")])
    assert "1/2 usable" in table


# -- submission --------------------------------------------------------------

def test_upload_sends_params_before_file(tmp_path):
    from canvasofficial import submit as S

    f = tmp_path / "answer.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    order: list[str] = []

    def canvas_handler(request: httpx.Request) -> httpx.Response:
        return json_response({"upload_url": "https://upload.test/put",
                              "upload_params": {"key": "abc", "policy": "xyz"}})

    def upload_handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("latin-1")
        for name in ("key", "policy", "file"):
            order.append(name) if f'name="{name}"' in body else None
        order.sort(key=lambda n: body.index(f'name="{n}"'))
        return json_response({"id": "999"}, status_code=201)

    c = CanvasClient(base_url="https://canvas.test", token="tok",
                     transport=httpx.MockTransport(canvas_handler))
    real_post = S.httpx.post
    S.httpx.post = lambda url, **kw: httpx.Client(  # noqa: E731
        transport=httpx.MockTransport(upload_handler)).post(url, **kw)
    try:
        assert S.upload_file(c, "1", "2", f) == "999"
    finally:
        S.httpx.post = real_post
    assert order[-1] == "file", "the file part must come last or the backend drops fields"
