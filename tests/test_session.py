from __future__ import annotations

import json

import httpx

from ainl.canvas.session import BrowserSession


def _state(tmp_path, cookies):
    f = tmp_path / "canvas.example.edu.storage_state.json"
    f.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return f


def test_save_cookies_persists_rotated_session(tmp_path):
    f = _state(tmp_path, [
        {"name": "canvas_session", "value": "OLD", "domain": "canvas.example.edu", "path": "/"},
        {"name": "_csrf_token", "value": "C", "domain": "canvas.example.edu", "path": "/"},
    ])
    s = BrowserSession("https://canvas.example.edu", profile_dir=tmp_path / "p", state_file=f)

    # simulate Canvas rotating the session cookie on a response
    jar = httpx.Cookies()
    jar.set("canvas_session", "NEW", domain="canvas.example.edu", path="/")
    jar.set("_csrf_token", "C", domain="canvas.example.edu", path="/")
    jar.set("unrelated", "x", domain="other.example.edu", path="/")

    assert s.save_cookies(jar) == 1
    saved = {c["name"]: c["value"] for c in json.loads(f.read_text())["cookies"]}
    assert saved == {"canvas_session": "NEW", "_csrf_token": "C"}      # foreign domain ignored

    # and the reloaded auth uses the rotated value
    assert s.load().canvas_session == "NEW"


def test_save_cookies_adds_new_cookie_for_host(tmp_path):
    f = _state(tmp_path, [{"name": "canvas_session", "value": "S", "domain": "canvas.example.edu", "path": "/"}])
    s = BrowserSession("https://canvas.example.edu", profile_dir=tmp_path / "p", state_file=f)
    jar = httpx.Cookies()
    jar.set("canvas_session", "S", domain="canvas.example.edu")
    jar.set("log_session_id", "L1", domain="canvas.example.edu")
    assert s.save_cookies(jar) == 1
    names = {c["name"] for c in json.loads(f.read_text())["cookies"]}
    assert names == {"canvas_session", "log_session_id"}


def test_save_cookies_noop_without_state(tmp_path):
    s = BrowserSession("https://canvas.example.edu", profile_dir=tmp_path / "p",
                       state_file=tmp_path / "missing.json")
    assert s.save_cookies(httpx.Cookies()) == 0
