"""Website session cookies kept across a browser stop — with a fake CDP."""
from __future__ import annotations

from cryptography.fernet import Fernet

from registry import Registry
from sitecookies import SiteCookies, as_params, session_cookies

WA = {"name": "JSESSIONID", "value": "wa-1", "domain": "www.webassign.net", "path": "/",
      "expires": -1, "session": True, "httpOnly": True, "secure": True, "sameSite": "None"}
CANVAS = {"name": "canvas_session", "value": "c-1", "domain": "canvas.wisc.edu", "path": "/",
          "expires": -1, "session": True}
KEEP = {"name": "prefs", "value": "x", "domain": ".cengage.com", "path": "/",
        "expires": 1_900_000_000, "session": False}


class FakeCDP:
    def __init__(self, cookies):
        self.cookies, self.set = cookies, None

    def __call__(self, port, method, params=None, timeout=5):
        if method == "Storage.getCookies":
            return {"cookies": list(self.cookies)}
        if method == "Storage.setCookies":
            self.set = params["cookies"]
            return {}
        raise AssertionError(method)


def _world(tmp_path, cookies, clock=lambda: 1000.0):
    reg = Registry(base=tmp_path)
    reg.add("111", "a", "a")
    reg.approve("111")
    cdp = FakeCDP(cookies)
    return reg, cdp, SiteCookies(reg, Fernet.generate_key(), canvas_host_of=lambda u: "canvas.wisc.edu",
                                 cdp=cdp, clock=clock)


def test_only_non_canvas_session_cookies_are_kept():
    assert session_cookies([WA, CANVAS, KEEP], "canvas.wisc.edu") == [WA]


def test_restore_drops_expires_so_cdp_does_not_read_it_as_expired():
    (p,) = as_params([WA])
    assert "expires" not in p and "session" not in p and p["name"] == "JSESSIONID"


def test_saved_encrypted_then_restored_into_a_new_browser(tmp_path):
    reg, cdp, site = _world(tmp_path, [WA, CANVAS, KEEP])
    assert site.save("111", 9300) == 1
    raw = (reg.user_dir("111") / "site-cookies.enc").read_bytes()
    assert b"wa-1" not in raw and b"JSESSIONID" not in raw          # encrypted at rest
    cdp.cookies = []                                               # a fresh browser
    assert site.restore("111", 9301) == 1
    assert [c["value"] for c in cdp.set] == ["wa-1"]


def test_stale_or_someone_elses_snapshot_is_not_used(tmp_path):
    t = {"now": 1000.0}
    reg, cdp, site = _world(tmp_path, [WA], clock=lambda: t["now"])
    site.save("111", 9300)
    t["now"] += 13 * 3600
    assert site.restore("111", 9300) == 0 and cdp.set is None       # the server expired it anyway
    reg.add("222", "b", "b")
    reg.approve("222")
    (reg.user_dir("222") / "site-cookies.enc").write_bytes((reg.user_dir("111") / "site-cookies.enc").read_bytes())
    t["now"] = 1000.0
    assert site.restore("222", 9300) == 0                          # copied from 111: refused


def test_a_failing_browser_never_breaks_a_stop(tmp_path):
    reg, _, site = _world(tmp_path, [])
    def boom(*a, **k):
        raise OSError("gone")
    site._cdp = boom
    assert site.save("111", 9300) == 0 and site.restore("111", 9300) == 0
