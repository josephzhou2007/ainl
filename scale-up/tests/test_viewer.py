"""The Canvas login viewer with a fake browser (no Chrome, no Canvas, no Telegram).

Two layers:
  BrowserView   what goes to the browser for each phone gesture, and which
                navigations are let through
  LoginServer   a real WebSocket server on a free port: who gets in, whose
                browser they reach, the "is this you?" step, and that nothing
                typed ends up in the logs
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

import initdata
import viewer
from viewer import BrowserView, LoginPolicy, LoginServer

CANVAS = "https://canvas.wisc.edu"
TOKEN = "123456:TEST-token"
SECRET = "hunter2-do-not-log"


class FakeCdp:
    """Records every command; answers the few whose results the view reads."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str | None]] = []
        self.on_event = lambda *a: None
        self.closed_ = False
        self._n = 0

    async def send(self, method, params=None, session=None, timeout=15):
        self.calls.append((method, params or {}, session))
        if method == "Target.createTarget":
            return {"targetId": "T1"}
        if method == "Target.attachToTarget":
            self._n += 1
            return {"sessionId": f"S{self._n}"}
        if method == "Runtime.evaluate":
            return {"result": {"value": True}}
        if method == "Page.getNavigationHistory":
            return {"currentIndex": 1, "entries": [{"id": 7}, {"id": 8}]}
        return {}

    async def close(self):
        self.closed_ = True

    def methods(self):
        return [m for m, _, _ in self.calls]


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# LoginPolicy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "https://canvas.wisc.edu/", "https://login.wisc.edu/idp/profile/SAML2/Redirect/SSO",
    "https://api-12ab34cd.duosecurity.com/frame/v4/auth/prompt", "about:blank",
    "https://sso.canvaslms.com/api/v1/sso",
])
def test_policy_lets_canvas_uw_login_and_duo_through(url):
    assert LoginPolicy(CANVAS).allows(url)


@pytest.mark.parametrize("url", [
    "http://login.wisc.edu/", "https://evilwisc.edu/", "https://wisc.edu.evil.com/",
    "https://google.com/", "file:///etc/passwd", "chrome://settings", "javascript:alert(1)",
    "https://127.0.0.1:8790/",
])
def test_policy_refuses_other_hosts_when_restricted(url):
    assert not LoginPolicy(CANVAS, restrict_hosts=True).allows(url)


@pytest.mark.parametrize("url", ["http://login.wisc.edu/", "file:///etc/passwd",
                                 "chrome://settings", "javascript:alert(1)"])
def test_non_https_is_refused_even_unrestricted(url):
    assert not LoginPolicy(CANVAS).allows(url)


def test_on_canvas_means_past_the_login_pages():
    p = LoginPolicy(CANVAS)
    assert p.on_canvas("https://canvas.wisc.edu/") and p.on_canvas("https://canvas.wisc.edu/courses")
    assert not p.on_canvas("https://canvas.wisc.edu/login/saml")
    assert not p.on_canvas("https://login.wisc.edu/")


# --------------------------------------------------------------------------- #
# BrowserView
# --------------------------------------------------------------------------- #

def _view(cdp):
    out, frames, urls = [], [], []

    async def emit(o):
        out.append(o)

    async def emit_frame(b):
        frames.append(b)

    v = BrowserView(cdp, LoginPolicy(CANVAS, restrict_hosts=True), emit=emit, emit_frame=emit_frame,
                    on_url=urls.append)
    return v, out, frames, urls


def test_open_uses_its_own_tab_fenced_sized_and_streaming():
    async def go():
        cdp = FakeCdp()
        v, *_ = _view(cdp)
        await v.open(CANVAS + "/", 390, 700, 3)
        return cdp
    cdp = _run(go())
    m = cdp.methods()
    assert m.index("Target.createTarget") < m.index("Target.attachToTarget")
    assert m.index("Fetch.enable") < m.index("Page.navigate")        # fenced before it loads
    metrics = next(p for n, p, _ in cdp.calls if n == "Emulation.setDeviceMetricsOverride")
    assert metrics == {"width": 390, "height": 700, "deviceScaleFactor": 3.0, "mobile": True}
    shot = next(p for n, p, _ in cdp.calls if n == "Page.startScreencast")
    assert shot["maxWidth"] == 1170 and shot["format"] == "jpeg"
    assert ("Page.navigate", {"url": CANVAS + "/"}, "S1") in cdp.calls


def test_gestures_become_the_right_cdp_input():
    async def go():
        cdp = FakeCdp()
        v, out, *_ = _view(cdp)
        await v.open(CANVAS + "/", 390, 700, 2)
        cdp.calls.clear()
        await v.handle({"type": "tap", "x": 10, "y": 20})
        await v.handle({"type": "text", "text": SECRET})
        await v.handle({"type": "key", "key": "Enter"})
        await v.handle({"type": "key", "key": "F12"})               # not on the list: dropped
        await v.handle({"type": "wheel", "x": 1, "y": 2, "dy": 50})
        await v.handle({"type": "back"})
        await asyncio.sleep(0.3)                                    # the focus report
        return cdp, out
    cdp, out = _run(go())
    mouse = [p["type"] for n, p, _ in cdp.calls if n == "Input.dispatchMouseEvent"]
    assert mouse == ["mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"]
    assert ("Input.insertText", {"text": SECRET}, "S1") in cdp.calls
    keys = [p for n, p, _ in cdp.calls if n == "Input.dispatchKeyEvent"]
    assert [k["type"] for k in keys] == ["keyDown", "keyUp"] and keys[0]["text"] == "\r"
    assert ("Page.navigateToHistoryEntry", {"entryId": 7}, "S1") in cdp.calls
    assert {"type": "focus", "editable": True} in out
    # The focus probe asks what KIND of element has focus, never its value.
    probe = next(p for n, p, _ in cdp.calls if n == "Runtime.evaluate")["expression"]
    assert ".value" not in probe


def test_navigation_outside_the_policy_is_failed_and_reported():
    async def go():
        cdp = FakeCdp()
        v, out, *_ = _view(cdp)
        await v.open(CANVAS + "/", 390, 700, 2)
        await v._gate({"requestId": "r1", "request": {"url": "https://login.wisc.edu/x"}}, "S1")
        await v._gate({"requestId": "r2", "request": {"url": "https://example.com/?q=1"}}, "S1")
        return cdp, out
    cdp, out = _run(go())
    assert ("Fetch.continueRequest", {"requestId": "r1"}, "S1") in cdp.calls
    assert ("Fetch.failRequest", {"requestId": "r2", "errorReason": "BlockedByClient"}, "S1") in cdp.calls
    assert {"type": "blocked", "host": "example.com"} in out


def test_each_allowed_host_is_reported_for_learning_and_refused_ones_are_not():
    visits = []

    async def go():
        cdp = FakeCdp()

        async def emit(o):
            pass
        v = BrowserView(cdp, LoginPolicy(CANVAS, restrict_hosts=True), emit=emit,
                        emit_frame=emit, on_visit=visits.append)
        await v.open(CANVAS + "/", 390, 700, 2)
        await v._gate({"requestId": "r1", "request": {"url": "https://login.wisc.edu/x?SAMLRequest=s"}}, "S1")
        await v._gate({"requestId": "r2", "request": {"url": "https://example.com/"}}, "S1")
    _run(go())
    assert visits == ["login.wisc.edu"]                 # host only, never the query


def test_the_server_hands_visits_on_with_the_persons_id_off_the_loop():
    got = []
    srv = LoginServer(bot_token=TOKEN, canvas_origin=CANVAS, allowed=lambda u: True,
                      cdp_url=lambda u: "http://x", port=0,
                      on_visit=lambda uid, host: got.append((uid, host)))

    async def go():
        srv._loop = asyncio.get_running_loop()
        srv._visited("4242", "login.wisc.edu")
        for _ in range(50):
            if got:
                break
            await asyncio.sleep(0.01)
    _run(go())
    assert got == [("4242", "login.wisc.edu")]


def test_frames_go_to_the_phone_and_are_acked_after():
    async def go():
        cdp = FakeCdp()
        v, out, frames, _ = _view(cdp)
        await v.open(CANVAS + "/", 390, 700, 2)
        await v._frame({"data": "aGVsbG8=", "sessionId": 3,
                        "metadata": {"deviceWidth": 390, "deviceHeight": 700}}, "S1")
        await v._frame({"data": "aGVsbG8=", "sessionId": 4, "metadata": {}}, "S9")  # not visible
        return cdp, out, frames
    cdp, out, frames = _run(go())
    assert frames == [b"hello"]
    assert {"type": "meta", "w": 390, "h": 700} in out
    acks = [p["sessionId"] for n, p, _ in cdp.calls if n == "Page.screencastFrameAck"]
    assert acks == [3, 4]                                # every frame is acked, shown or not


def test_popups_from_our_tab_are_followed_and_the_url_reported_without_query():
    async def go():
        cdp = FakeCdp()
        v, out, _, urls = _view(cdp)
        await v.open(CANVAS + "/", 390, 700, 2)
        v._event("Target.targetCreated", {"targetInfo": {
            "type": "page", "targetId": "T2", "openerId": "T1",
            "url": "https://api-1.duosecurity.com/frame"}}, None)
        v._event("Target.targetCreated", {"targetInfo": {           # someone else's tab
            "type": "page", "targetId": "T3", "openerId": "T0", "url": "https://x.org"}}, None)
        await asyncio.sleep(0.05)
        v._event("Page.frameNavigated", {"frame": {
            "url": "https://login.wisc.edu/idp/SSO?SAMLRequest=abc"}}, "S2")
        await asyncio.sleep(0.05)
        visible = v.visible.target_id
        v._event("Target.targetDestroyed", {"targetId": "T2"}, None)
        await asyncio.sleep(0.05)
        return cdp, out, urls, visible, v.visible.target_id
    cdp, out, urls, visible, after = _run(go())
    assert visible == "T2" and after == "T1"
    attached = [p["targetId"] for n, p, _ in cdp.calls if n == "Target.attachToTarget"]
    assert attached == ["T1", "T2"]
    assert {"type": "url", "where": "login.wisc.edu/idp/SSO"} in out
    assert all("SAMLRequest" not in json.dumps(o) for o in out)
    assert urls == ["https://login.wisc.edu/idp/SSO?SAMLRequest=abc"]


def test_close_closes_only_the_tabs_it_opened():
    async def go():
        cdp = FakeCdp()
        v, *_ = _view(cdp)
        await v.open(CANVAS + "/", 390, 700, 2)
        await v.close()
        return cdp
    cdp = _run(go())
    assert [p["targetId"] for n, p, _ in cdp.calls if n == "Target.closeTarget"] == ["T1"]
    assert cdp.closed_


# --------------------------------------------------------------------------- #
# LoginServer
# --------------------------------------------------------------------------- #

class FakeSessions:
    def __init__(self):
        self.completed = []

    def inspect(self, uid):
        return SimpleNamespace(state=SimpleNamespace(value="unbound"), canvas_id="987",
                               name="Ada Lovelace")

    def complete_login(self, uid, canvas_id):
        self.completed.append((uid, canvas_id))
        return SimpleNamespace(state=SimpleNamespace(value="active"), canvas_id=canvas_id)


@pytest.fixture
def server():
    cdps: list[FakeCdp] = []
    browsers: list[str] = []
    connected: list[tuple[str, str]] = []
    opened: list[str] = []
    sessions = FakeSessions()

    async def fake_connect(url):
        cdp = FakeCdp()
        cdps.append(cdp)
        return cdp

    def cdp_url(uid):
        browsers.append(uid)
        return f"http://127.0.0.1:93{uid[-2:]}"

    srv = LoginServer(bot_token=TOKEN, canvas_origin=CANVAS, allowed=lambda uid: uid != "666",
                      cdp_url=cdp_url, sessions=sessions,
                      on_connected=lambda u, n: connected.append((u, n)),
                      on_open=opened.append,
                      port=0, connect=fake_connect)
    srv.start_in_thread()
    world = SimpleNamespace(srv=srv, cdps=cdps, browsers=browsers, connected=connected,
                            opened=opened,
                            sessions=sessions, url=f"ws://127.0.0.1:{srv.port}/ws")
    yield world
    srv.stop()


def _hello(uid, **kw):
    fields = {"auth_date": str(int(time.time())),
              "user": json.dumps({"id": int(uid), "first_name": "Ada"})}
    return json.dumps({"type": "hello", "initData": initdata.sign(fields, kw.get("token", TOKEN)),
                       "w": 390, "h": 700, "dpr": 2})


def _until(ws, kind, limit=20):
    for _ in range(limit):
        m = ws.recv(timeout=5)
        if isinstance(m, str) and json.loads(m).get("type") == kind:
            return json.loads(m)
    raise AssertionError(f"no {kind}")


def test_forged_initdata_gets_nothing(server):
    with connect(server.url) as ws:
        ws.send(_hello("4242", token="999:someone-else"))
        assert json.loads(ws.recv(timeout=5))["type"] == "error"
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
    assert e.value.rcvd.code == 4401
    assert server.browsers == []                        # never even looked up a browser


def test_dev_hello_without_initdata_is_refused_outside_dev(server):
    with connect(server.url) as ws:
        ws.send(json.dumps({"type": "hello", "w": 390, "h": 700}))
        with pytest.raises(ConnectionClosed):
            while True:
                ws.recv(timeout=5)
    assert server.browsers == []


def test_unapproved_people_are_refused(server):
    with connect(server.url) as ws:
        ws.send(_hello("666"))
        _until(ws, "error")
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
    assert e.value.rcvd.code == 4403 and server.browsers == []
    assert server.opened == []                          # no intro video for the unapproved


def test_the_signed_user_reaches_their_own_browser_and_confirms(server, caplog):
    caplog.set_level(logging.DEBUG)
    with connect(server.url) as ws:
        ws.send(_hello("4242"))
        _until(ws, "ready")
        assert server.browsers == ["4242"]               # theirs, from the signature
        assert server.opened == ["4242"]                 # the bot's cue for the intro video
        ws.send(json.dumps({"type": "tap", "x": 5, "y": 5}))
        ws.send(json.dumps({"type": "text", "text": SECRET}))
        time.sleep(0.2)
        cdp = server.cdps[0]
        assert ("Input.insertText", {"text": SECRET}, "S1") in cdp.calls
        # The tab reaches Canvas: the server asks who this is.
        server.srv._loop.call_soon_threadsafe(cdp.on_event, "Page.frameNavigated",
                                              {"frame": {"url": CANVAS + "/"}}, "S1")
        assert _until(ws, "confirm")["name"] == "Ada Lovelace"
        ws.send(json.dumps({"type": "confirm", "yes": True}))
        assert _until(ws, "done")["name"] == "Ada Lovelace"
    assert server.sessions.completed == [("4242", "987")]
    time.sleep(2)                                        # the viewer lingers ~1.5 s to show "done"
    assert server.connected == [("4242", "Ada Lovelace")]
    assert cdp.closed_ and ("Target.closeTarget", {"targetId": "T1"}, None) in cdp.calls
    assert SECRET not in caplog.text


def test_not_me_logs_out_and_saves_nothing(server):
    with connect(server.url) as ws:
        ws.send(_hello("4242"))
        _until(ws, "ready")
        cdp = server.cdps[0]
        server.srv._loop.call_soon_threadsafe(cdp.on_event, "Page.frameNavigated",
                                              {"frame": {"url": CANVAS + "/"}}, "S1")
        _until(ws, "confirm")
        ws.send(json.dumps({"type": "confirm", "yes": False}))
        _until(ws, "status")
        time.sleep(0.2)
    assert server.sessions.completed == []
    assert "Network.clearBrowserCookies" in cdp.methods()


def test_a_second_open_replaces_the_first(server):
    with connect(server.url) as first:
        first.send(_hello("4242"))
        _until(first, "ready")
        with connect(server.url) as second:
            second.send(_hello("4242"))
            _until(second, "ready")
            assert _until(first, "bye")["text"].startswith("opened again")
    assert server.cdps[0].closed_


def test_kick_ends_a_viewer_from_another_thread(server):
    with connect(server.url) as ws:
        ws.send(_hello("4242"))
        _until(ws, "ready")
        server.srv.kick("4242")
        _until(ws, "bye")
    assert server.cdps[0].closed_


def test_wrong_origin_is_refused_when_exposed():
    srv = LoginServer(bot_token=TOKEN, canvas_origin=CANVAS, allowed=lambda u: True,
                      cdp_url=lambda u: "", public_origin="https://login.example.org", port=0)
    srv.start_in_thread()
    try:
        with pytest.raises(InvalidStatus):
            with connect(f"ws://127.0.0.1:{srv.port}/ws", origin="https://evil.example"):
                pass
        with connect(f"ws://127.0.0.1:{srv.port}/ws", origin="https://login.example.org"):
            pass
    finally:
        srv.stop()


def test_the_page_is_served_with_a_tight_policy():
    import urllib.request
    srv = LoginServer(bot_token=TOKEN, canvas_origin=CANVAS, allowed=lambda u: True,
                      cdp_url=lambda u: "", port=0)
    srv.start_in_thread()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/login", timeout=5) as r:
            body = r.read().decode()
            csp = r.headers["Content-Security-Policy"]
            assert r.headers.get_all("Content-Type") == ["text/html; charset=utf-8"]
        assert "Canvas login" in body and "telegram-web-app.js" in body
        assert "default-src 'none'" in csp and "form-action 'none'" in csp
    finally:
        srv.stop()


def test_websockets_debug_logging_stays_off():
    # It would log frame payloads — keystrokes — at DEBUG.
    assert logging.getLogger("websockets").level >= logging.INFO
    assert viewer.PAGE.exists()


def test_mounted_under_a_path_prefix():
    import urllib.error
    import urllib.request
    srv = LoginServer(bot_token=TOKEN, canvas_origin=CANVAS, allowed=lambda u: True,
                      cdp_url=lambda u: "", port=0, base_path="/native-bot-learning/")
    srv.start_in_thread()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        with urllib.request.urlopen(base + "/native-bot-learning/login", timeout=5) as r:
            assert "Canvas login" in r.read().decode()
        with urllib.request.urlopen(base + "/native-bot-learning/healthz", timeout=5) as r:
            assert r.read() == b"ok\n"
        for wrong in ("/login", "/native-bot-learningX/login", "/other/login"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(base + wrong, timeout=5)
        with connect(f"ws://127.0.0.1:{srv.port}/native-bot-learning/ws") as ws:
            ws.send(json.dumps({"type": "hello"}))
            assert json.loads(ws.recv(timeout=5))["type"] == "error"   # reached the handler
    finally:
        srv.stop()


def test_each_person_logs_in_to_their_own_schools_canvas():
    import schools
    cdps, checked = [], []

    async def fake_connect(url):
        cdps.append(FakeCdp())
        return cdps[-1]

    class Sessions(FakeSessions):
        def inspect(self, uid):
            checked.append(uid)
            return super().inspect(uid)

    school = {"4242": schools.get("ucsb"), "5151": None}
    srv = LoginServer(bot_token=TOKEN, canvas_origin=CANVAS, allowed=lambda u: True,
                      cdp_url=lambda u: "http://x", sessions=Sessions(), port=0,
                      connect=fake_connect, school_of=lambda u: school.get(u))
    srv.start_in_thread()
    url = f"ws://127.0.0.1:{srv.port}/ws"
    try:
        with connect(url) as ws:
            ws.send(_hello("4242"))
            _until(ws, "ready")
            nav = [p["url"] for n, p, _ in cdps[0].calls if n == "Page.navigate"]
            assert nav == ["https://ucsb.instructure.com/login/saml"]   # straight into UCSB SSO
            # UW's Canvas is not "their Canvas": no identity check there...
            srv._loop.call_soon_threadsafe(cdps[0].on_event, "Page.frameNavigated",
                                           {"frame": {"url": "https://canvas.wisc.edu/"}}, "S1")
            time.sleep(0.3)
            assert checked == []
            # ...UCSB's is.
            srv._loop.call_soon_threadsafe(cdps[0].on_event, "Page.frameNavigated",
                                           {"frame": {"url": "https://ucsb.instructure.com/"}}, "S1")
            _until(ws, "confirm")
            assert checked == ["4242"]
        with connect(url) as ws:                        # approved, no school picked yet
            ws.send(_hello("5151"))
            assert "school" in _until(ws, "error")["text"]
            with pytest.raises(ConnectionClosed) as e:
                ws.recv(timeout=5)
        assert e.value.rcvd.code == 4409 and len(cdps) == 1   # never reached a browser
    finally:
        srv.stop()


def test_reaching_canvas_stops_the_heavy_ui_and_parks_the_tab(server):
    """The dashboard behind the "is this you?" card is 20 MB / 92 scripts;
    the identity check needs one API call."""
    with connect(server.url) as ws:
        ws.send(_hello("4242"))
        _until(ws, "ready")
        cdp = server.cdps[0]
        server.srv._loop.call_soon_threadsafe(cdp.on_event, "Page.frameNavigated",
                                              {"frame": {"url": CANVAS + "/"}}, "S1")
        _until(ws, "confirm")
        m = cdp.methods()
        assert "Page.stopLoading" in m
        parked = [p for n, p, _ in cdp.calls if n == "Page.navigate" and p.get("url") == "about:blank"]
        assert parked and m.index("Page.stopLoading") < m.index("Page.navigate", m.index("Page.stopLoading"))


def _hello_open(uid, ticket):
    h = json.loads(_hello(uid))
    h["open"] = ticket
    return json.dumps(h)


def test_open_in_my_browser_goes_to_the_page_and_never_asks_who_you_are(server):
    target = "https://canvas.wisc.edu/courses/1/assignments/2"
    tid = server.srv.issue_open("4242", target)
    with connect(server.url) as ws:
        ws.send(_hello_open("4242", tid))
        _until(ws, "ready")
        cdp = server.cdps[0]
        assert ("Page.navigate", {"url": target}, "S1") in cdp.calls
        server.srv._loop.call_soon_threadsafe(cdp.on_event, "Page.frameNavigated",
                                              {"frame": {"url": target}}, "S1")
        time.sleep(0.3)
        assert "Page.stopLoading" not in cdp.methods()        # working, not logging in
        assert server.srv._live["4242"].mode == "open"


def test_a_ticket_only_works_for_its_owner_and_not_after_it_expires(server):
    tid = server.srv.issue_open("4242", "https://canvas.wisc.edu/x")
    with connect(server.url) as ws:
        ws.send(_hello_open("5151", tid))                       # someone else's ticket
        assert "expired" in _until(ws, "error")["text"]
    expired = server.srv.issue_open("4242", "https://canvas.wisc.edu/x", ttl=-1)
    with connect(server.url) as ws:
        ws.send(_hello_open("4242", expired))
        _until(ws, "error")
        with pytest.raises(ConnectionClosed) as e:
            ws.recv(timeout=5)
    assert e.value.rcvd.code == 4410
    assert all(c.calls == [] or "Page.navigate" not in c.methods() for c in server.cdps)


def test_open_mode_makes_new_windows_open_in_the_page_itself():
    async def go(same_tab):
        cdp = FakeCdp()
        v = BrowserView(cdp, LoginPolicy(CANVAS), emit=lambda o: asyncio.sleep(0),
                        emit_frame=lambda b: asyncio.sleep(0), same_tab=same_tab)
        await v.open("https://x.edu/launch", 390, 700, 2)
        return cdp
    on = _run(go(True))
    scripts = [p["source"] for n, p, _ in on.calls if n == "Page.addScriptToEvaluateOnNewDocument"]
    assert len(scripts) == 1 and "window.open" in scripts[0] and "'_self'" in scripts[0]
    m = on.methods()
    assert m.index("Page.addScriptToEvaluateOnNewDocument") < m.index("Page.navigate")   # before any page script
    off = _run(go(False))
    assert "Page.addScriptToEvaluateOnNewDocument" not in off.methods()          # never during a login


def test_a_frame_for_a_phone_that_just_left_is_dropped_quietly():
    """2026-09-26: the phone closed the page (1001 going away) with frames in
    flight; each became an 'exception was never retrieved' in the log."""
    async def go():
        cdp = FakeCdp()

        async def gone(b):
            raise ConnectionClosed(None, None)

        v = BrowserView(cdp, LoginPolicy(CANVAS), emit=lambda o: asyncio.sleep(0), emit_frame=gone)
        await v.open(CANVAS + "/", 390, 700, 2)
        await v._frame({"data": "aGk=", "sessionId": 7, "metadata": {}}, "S1")   # must not raise
        return cdp
    cdp = _run(go())
    assert ("Page.screencastFrameAck", {"sessionId": 7}, "S1") in cdp.calls


# -- the owner, from the private bot (its Canvas session expired) --------------

PRIVATE = "555:private-bot"
OWNER = "7777"


@pytest.fixture
def owner_server():
    from viewer import OwnerLogin
    cdps, urls, logins, pooled = [], [], [], []
    state = {"name": None}

    async def fake_connect(url):
        urls.append(url)
        cdp = FakeCdp()
        cdps.append(cdp)
        return cdp

    owner = OwnerLogin(bot_token=PRIVATE, cdp_url="http://127.0.0.1:9222", canvas_origin=CANVAS,
                       check=lambda: state["name"], on_login=logins.append)
    srv = LoginServer(bot_token=TOKEN, canvas_origin=CANVAS, allowed=lambda uid: True,
                      cdp_url=lambda uid: pooled.append(uid) or "http://127.0.0.1:9300",
                      sessions=FakeSessions(), port=0, connect=fake_connect,
                      owner_id=OWNER, owner=owner)
    srv.start_in_thread()
    yield SimpleNamespace(srv=srv, cdps=cdps, urls=urls, logins=logins, pooled=pooled,
                          state=state, url=f"ws://127.0.0.1:{srv.port}/ws")
    srv.stop()


def test_the_owner_from_the_private_bot_logs_in_on_their_own_browser(owner_server):
    w = owner_server
    with connect(w.url) as ws:
        ws.send(_hello(OWNER, token=PRIVATE))
        _until(ws, "ready")
        assert w.urls == ["http://127.0.0.1:9222"]       # the owner's browser, not a pooled one
        assert w.pooled == []
        cdp = w.cdps[0]
        # Canvas, but not logged in yet: keep going, nothing announced.
        w.srv._loop.call_soon_threadsafe(cdp.on_event, "Page.frameNavigated",
                                         {"frame": {"url": CANVAS + "/"}}, "S1")
        time.sleep(0.3)
        assert w.logins == []
        # Logged in: Canvas says who, the page says done, the private bot is told.
        w.state["name"] = "Alex"
        w.srv._loop.call_soon_threadsafe(cdp.on_event, "Page.frameNavigated",
                                         {"frame": {"url": CANVAS + "/courses"}}, "S1")
        assert _until(ws, "done")["name"] == "Alex"
    time.sleep(0.3)
    assert w.logins == ["Alex"]


def test_the_owner_signed_by_the_public_bot_is_not_the_private_path(owner_server):
    """From the public bot the owner is just an unregistered person: the owner
    path (their own browser) opens only for the private bot's signature."""
    w = owner_server
    with connect(w.url) as ws:
        ws.send(_hello(OWNER))                           # public token
        _until(ws, "ready")
    assert "http://127.0.0.1:9222" not in w.urls


def test_someone_else_signed_by_the_private_bot_gets_nothing(owner_server):
    w = owner_server
    with connect(w.url) as ws:
        ws.send(_hello("4242", token=PRIVATE))
        assert json.loads(ws.recv(timeout=5))["type"] == "error"
    assert w.urls == [] and w.pooled == []
