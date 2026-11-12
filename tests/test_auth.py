from __future__ import annotations

import json

import httpx
import pytest

from ainl.canvas._http import CanvasAuthError, CanvasHTTP
from ainl.canvas.auth import BearerAuth, CookieAuth

BASE = "https://canvas.example.edu"


def test_bearer_headers_and_no_cookies():
    a = BearerAuth("tok")
    assert a.headers() == {"Authorization": "Bearer tok"}
    assert a.cookies() == {}
    assert "tok" not in repr(a)


def test_cookie_auth_decodes_csrf_for_header_but_not_cookie():
    a = CookieAuth(canvas_session="sess", csrf_token="abc%2Fdef%3D%3D")
    assert a.headers()["X-CSRF-Token"] == "abc/def=="
    assert a.headers()["X-Requested-With"] == "XMLHttpRequest"
    assert a.cookies() == {"canvas_session": "sess", "_csrf_token": "abc%2Fdef%3D%3D"}
    assert "sess" not in repr(a)


def test_cookie_auth_from_playwright_state():
    cookies = [
        {"name": "canvas_session", "value": "S", "domain": "canvas.example.edu"},
        {"name": "_csrf_token", "value": "C", "domain": ".canvas.example.edu"},
        {"name": "canvas_session", "value": "OTHER", "domain": "other.example.edu"},
    ]
    a = CookieAuth.from_cookie_list(cookies, "canvas.example.edu")
    assert a.canvas_session == "S" and a.csrf_token == "C"
    assert CookieAuth.from_cookie_list(cookies[:1], "canvas.example.edu") is None


def test_cookie_auth_sends_cookies_and_csrf_on_post():
    seen = {}

    def handler(req: httpx.Request):
        seen["cookie"] = req.headers.get("cookie", "")
        seen["csrf"] = req.headers.get("x-csrf-token")
        return httpx.Response(200, json={"data": {}})

    http = CanvasHTTP(BASE, CookieAuth("S", "C%3D"), transport=httpx.MockTransport(handler))
    http.graphql("query { x }")
    assert "canvas_session=S" in seen["cookie"] and "_csrf_token=C%3D" in seen["cookie"]
    assert seen["csrf"] == "C="


@pytest.mark.parametrize("resp", [
    httpx.Response(401, json={"status": "unauthenticated"}),
    httpx.Response(302, headers={"location": f"{BASE}/login/saml"}),
])
def test_expired_session_raises_auth_error(resp):
    http = CanvasHTTP(BASE, CookieAuth("S", "C"), transport=httpx.MockTransport(lambda r: resp))
    with pytest.raises(CanvasAuthError):
        http.get("/api/v1/users/self")


def test_rate_limit_403_retries_once(monkeypatch):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("ainl.canvas._http.time.sleep", lambda s: None)
    http = CanvasHTTP(BASE, BearerAuth("t"), transport=httpx.MockTransport(handler))
    assert http.get("/api/v1/x") == {"ok": True}
    assert calls["n"] == 2
