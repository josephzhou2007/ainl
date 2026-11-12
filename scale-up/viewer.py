"""Canvas login for classmates: their own browser on this machine, on their phone.

A classmate taps "Canvas login" in the public bot. Telegram opens /login (this
server, behind the Cloudflare Tunnel) inside the app. The page sends Telegram's
signed initData; we check it (initdata.py), which tells us WHO this is, and
only then attach to THAT person's pooled browser (browsers.py) over the Chrome
DevTools Protocol:

    browser  --Page.startScreencast (JPEG)-->  WebSocket  -->  <canvas> on the phone
    phone    --tap / text / key-->  WebSocket  -->  Input.dispatchMouseEvent / insertText

They type their NetID and password into UW's own page and approve Duo on their
phone. We never see the password as anything but keystrokes passing through,
and never write one down: no input is logged, no field is ever read back.

When the tab reaches Canvas we ask Canvas who is logged in (Cookie
Management's inspect), show the name, and only on "Yes, that's me" bind the
account and save the encrypted snapshot (complete_login). Then the page closes
and the bot says so in the chat.

What the viewer may and may not do:

- A viewer only ever reaches the browser of the user in the verified initData.
  Nothing from the URL or the page picks the user.
- Top-level navigation is limited to Canvas, UW's login and Duo (LoginPolicy).
  There is no address bar; anything else is refused before it loads.
- One live viewer per person; a session lasts at most ``max_session_s`` and
  ends after ``idle_s`` without input. Ending it closes the tabs it opened.
- It opens its OWN tab in the browser, so a page the bot is using is never
  hijacked, and never launches a second browser on the same profile.

Dev, on this machine only (no Telegram; the page skips initData):

    .venv/bin/python scale-up/viewer.py --dev-user me_1
    open http://127.0.0.1:8790/login
    (--base-path /native-bot-learning  ->  http://127.0.0.1:8790/native-bot-learning/login)
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import initdata

log = logging.getLogger("ainl.scaleup.viewer")
# websockets logs whole frames at DEBUG — those carry keystrokes. Never let a
# root DEBUG level turn that on.
logging.getLogger("websockets").setLevel(logging.INFO)

PAGE = Path(__file__).resolve().parent / "login.html"
MONITOR_PAGE = Path(__file__).resolve().parent / "monitor.html"
# The monitor page stays open while the owner watches it, but Telegram signs
# initData once, when the Mini App opens. Long enough for a session of
# watching; after that the page says "reopen /monitor".
MONITOR_MAX_AGE_S = 2 * 3600
DEFAULT_PORT = 8790

# UW-Madison's Canvas signs in through Shibboleth on login.wisc.edu, Duo's
# Universal Prompt on *.duosecurity.com, and back through Instructure's
# sso.canvaslms.com (found by a real login on 2026-09-26). Suffixes, matched
# on a dot boundary. Every host the tab visits is logged (host only), so a
# missing hop shows up as "refused" next to the ones that were let through.
DEFAULT_ALLOWED = ("wisc.edu", "duosecurity.com", "duo.com", "instructure.com", "canvaslms.com")

# "Open in my browser" mode: a platform that insists on a new window opens in
# this page instead. A headless browser's new windows stay blank — McGraw Hill
# does window.open('', 'ltipaircourse'), checks that it is about:blank, then
# submits its launch form into it (2026-09-26). So window.open('') hands back a
# stand-in that looks blank, and every form / link target becomes _self.
SAME_TAB_JS = r"""(() => {
  const blank = { location: { href: 'about:blank' }, closed: false, focus() {}, close() {}, blur() {} };
  window.open = function (url) { if (url) { location.href = url; return window; } return blank; };
  const retarget = f => { if (f && f.target && f.target !== '_self') f.target = '_self'; };
  const submit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () { retarget(this); return submit.call(this); };
  document.addEventListener('submit', e => retarget(e.target), true);
  document.addEventListener('click', e => {
    const a = e.target && e.target.closest && e.target.closest('a[target]');
    if (a && a.target !== '_self') a.target = '_self';
  }, true);
})();"""

# windowsVirtualKeyCode for the keys a phone keyboard can send.
_KEYS = {"Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27, "ArrowLeft": 37,
         "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40, "Delete": 46}


# --------------------------------------------------------------------------- #
# Where the login tab may go
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LoginPolicy:
    canvas_origin: str
    allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED
    # False = any https host (every host is logged, to learn the list); True = only the list.
    # Non-https (file:, chrome:, data:, http:) is refused either way.
    restrict_hosts: bool = False

    @property
    def canvas_host(self) -> str:
        return urlsplit(self.canvas_origin).hostname or ""

    def allows(self, url: str) -> bool:
        u = urlsplit(url)
        if u.scheme == "about" and url in ("about:blank", "about:srcdoc"):
            return True
        if u.scheme != "https" or not u.hostname:
            return False
        host = u.hostname.lower()
        if host == self.canvas_host or not self.restrict_hosts:
            return True
        return any(host == s or host.endswith("." + s) for s in self.allowed_suffixes)

    def on_canvas(self, url: str) -> bool:
        """On Canvas proper, past the login pages — time to ask who this is."""
        u = urlsplit(url)
        return (u.scheme == "https" and u.hostname == self.canvas_host
                and not u.path.startswith("/login"))


def _where(url: str) -> str:
    """Host + path for the status line and the logs. Never the query: SAML
    responses and Duo state ride in it."""
    u = urlsplit(url)
    return (u.hostname or url.split(":", 1)[0]) + (u.path if u.hostname else "")


# --------------------------------------------------------------------------- #
# A minimal async CDP client: one browser WebSocket, flattened sessions
# --------------------------------------------------------------------------- #

class CdpError(RuntimeError):
    pass


class Cdp:
    """Send commands, get results; events go to ``on_event(method, params, session)``."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self.on_event: Callable[[str, dict, Optional[str]], None] = lambda *a: None
        self._reader = asyncio.ensure_future(self._read())

    @classmethod
    async def connect(cls, http_url: str) -> "Cdp":
        from websockets.asyncio.client import connect

        def version() -> str:
            with urllib.request.urlopen(http_url.rstrip("/") + "/json/version", timeout=5) as r:
                return json.load(r)["webSocketDebuggerUrl"]

        ws_url = await asyncio.to_thread(version)
        ws = await connect(ws_url, proxy=None, max_size=64 * 2**20, ping_interval=None)
        return cls(ws)

    async def send(self, method: str, params: Optional[dict] = None,
                   session: Optional[str] = None, timeout: float = 15) -> dict:
        i = next(self._ids)
        msg: dict = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        try:
            await self._ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(i, None)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                m = json.loads(raw)
                if "id" in m:
                    fut = self._pending.get(m["id"])
                    if fut and not fut.done():
                        if "error" in m:
                            fut.set_exception(CdpError(str(m["error"].get("message"))[:200]))
                        else:
                            fut.set_result(m.get("result") or {})
                else:
                    try:
                        self.on_event(m.get("method", ""), m.get("params") or {}, m.get("sessionId"))
                    except Exception:
                        log.exception("cdp event handler failed")
        except Exception:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CdpError("browser connection closed"))

    @property
    def closed(self) -> bool:
        return self._reader.done()

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass
        self._reader.cancel()


# --------------------------------------------------------------------------- #
# One person's login tab, mirrored to one phone
# --------------------------------------------------------------------------- #

@dataclass
class _Tab:
    target_id: str
    session: str
    url: str = ""


class BrowserView:
    """Opens a login tab in someone's browser and streams it; replays their input.

    ``emit`` gets JSON-able dicts for the page and ``emit_frame`` JPEG bytes.
    ``on_url`` is told every top-level URL the visible tab lands on, and
    ``on_visit`` every host a document request was let through to.
    """

    def __init__(self, cdp: Cdp, policy: LoginPolicy, *,
                 emit: Callable[[dict], Awaitable[None]],
                 emit_frame: Callable[[bytes], Awaitable[None]],
                 on_url: Callable[[str], None] = lambda url: None,
                 same_tab: bool = False,
                 on_visit: Callable[[str], None] = lambda host: None) -> None:
        self.cdp, self.policy = cdp, policy
        self._emit, self._emit_frame, self._on_url = emit, emit_frame, on_url
        self._on_visit = on_visit
        self._tabs: list[_Tab] = []            # opened by us or by our tabs; last = visible
        self._same_tab = same_tab              # open mode: new windows open in this page
        self._size = (390, 700, 2.0)
        self._meta: tuple = ()
        self._closing = False
        cdp.on_event = self._event

    @property
    def visible(self) -> Optional[_Tab]:
        return self._tabs[-1] if self._tabs else None

    # -- lifecycle -----------------------------------------------------------

    async def open(self, start_url: str, width: int, height: int, dpr: float) -> None:
        """Open our own tab on ``start_url``. With ``same_tab`` (open mode),
        SAME_TAB_JS runs in it before any page script."""
        self._size = (max(240, min(int(width), 1200)), max(320, min(int(height), 2000)),
                      max(1.0, min(float(dpr), 3.0)))
        await self.cdp.send("Target.setDiscoverTargets", {"discover": True})
        t = await self.cdp.send("Target.createTarget", {"url": "about:blank"})
        await self._adopt(t["targetId"], "about:blank")
        await self.cdp.send("Page.navigate", {"url": start_url}, self.visible.session)

    async def _adopt(self, target_id: str, url: str) -> None:
        """Attach to a tab, fence it in, size it for the phone, start streaming it."""
        if any(t.target_id == target_id for t in self._tabs):
            return
        r = await self.cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        s = r["sessionId"]
        await self.cdp.send("Fetch.enable", {"patterns": [
            {"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]}, s)
        await self.cdp.send("Page.enable", {}, s)
        if self._same_tab:
            await self.cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": SAME_TAB_JS}, s)
        w, h, dpr = self._size
        await self.cdp.send("Emulation.setDeviceMetricsOverride",
                            {"width": w, "height": h, "deviceScaleFactor": dpr, "mobile": True}, s)
        if self.visible:                        # only one tab streams at a time
            await self._quiet(self.cdp.send("Page.stopScreencast", {}, self.visible.session))
        self._tabs.append(_Tab(target_id, s, url))
        await self._stream()
        if url and url != "about:blank" and not self.policy.allows(url):
            # A popup that was born somewhere it shouldn't be (Fetch was not
            # on yet for its first request).
            await self._emit({"type": "blocked", "host": _where(url)})
            await self._quiet(self.cdp.send("Target.closeTarget", {"targetId": target_id}))

    async def _stream(self) -> None:
        tab = self.visible
        if tab is None:
            return
        w, h, dpr = self._size
        await self.cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 60, "everyNthFrame": 1,
            "maxWidth": int(w * dpr), "maxHeight": int(h * dpr)}, tab.session)
        if tab.url:
            await self._emit({"type": "url", "where": _where(tab.url)})

    async def close(self) -> None:
        """Close every tab we opened, then let go of the browser."""
        self._closing = True
        for tab in reversed(self._tabs):
            await self._quiet(self.cdp.send("Target.closeTarget", {"targetId": tab.target_id},
                                            timeout=3))
        self._tabs.clear()
        await self.cdp.close()

    @staticmethod
    async def _quiet(aw) -> None:
        try:
            await aw
        except Exception:
            pass

    # -- browser -> phone ----------------------------------------------------

    def _event(self, method: str, p: dict, session: Optional[str]) -> None:
        if self._closing:
            return
        if method == "Page.screencastFrame":
            asyncio.ensure_future(self._frame(p, session))
        elif method == "Fetch.requestPaused":
            asyncio.ensure_future(self._gate(p, session))
        elif method == "Page.frameNavigated":
            frame = p.get("frame") or {}
            if frame.get("parentId"):
                return                          # iframes are the page's business
            for tab in self._tabs:
                if tab.session == session:
                    tab.url = frame.get("url", "")
                    if tab is self.visible:
                        asyncio.ensure_future(self._quiet(self._emit({"type": "url", "where": _where(tab.url)})))
                    self._on_url(tab.url)
        elif method == "Target.targetCreated":
            info = p.get("targetInfo") or {}
            opener = info.get("openerId")
            if info.get("type") == "page" and any(t.target_id == opener for t in self._tabs):
                # Duo or the IdP opened a window: follow it.
                asyncio.ensure_future(self._quiet(self._adopt(info["targetId"], info.get("url", ""))))
        elif method == "Target.targetDestroyed":
            asyncio.ensure_future(self._gone(p.get("targetId")))

    async def _frame(self, p: dict, session: Optional[str]) -> None:
        tab = self.visible
        try:
            if tab is not None and session == tab.session:
                md = p.get("metadata") or {}
                meta = (md.get("deviceWidth"), md.get("deviceHeight"))
                if meta != self._meta:
                    self._meta = meta
                    await self._emit({"type": "meta", "w": meta[0], "h": meta[1]})
                # The phone may have gone (closed the page) with frames in
                # flight: a send on a closed socket is nothing to report.
                await self._quiet(self._emit_frame(base64.b64decode(p["data"])))
        finally:
            # Ack only after the phone took it: the browser sends the next
            # frame when we ack, so a slow phone slows the stream, not memory.
            await self._quiet(self.cdp.send("Page.screencastFrameAck",
                                            {"sessionId": p.get("sessionId")}, session))

    async def _gate(self, p: dict, session: Optional[str]) -> None:
        url = (p.get("request") or {}).get("url", "")
        if self.policy.allows(url):
            log.info("login tab visited %s", urlsplit(url).hostname)
            await self._quiet(self.cdp.send("Fetch.continueRequest", {"requestId": p["requestId"]}, session))
            if urlsplit(url).hostname:
                try:
                    self._on_visit(urlsplit(url).hostname)
                except Exception:
                    log.exception("on_visit failed")
            return
        log.info("login tab: refused navigation to %s", urlsplit(url).hostname)
        await self._quiet(self.cdp.send("Fetch.failRequest", {"requestId": p["requestId"],
                                                              "errorReason": "BlockedByClient"}, session))
        await self._emit({"type": "blocked", "host": urlsplit(url).hostname or "?"})

    async def _gone(self, target_id: Optional[str]) -> None:
        was = self.visible
        self._tabs = [t for t in self._tabs if t.target_id != target_id]
        if self._tabs and was is not None and was.target_id == target_id:
            await self._quiet(self._stream())   # the popup closed: back to the tab under it

    # -- phone -> browser ----------------------------------------------------

    async def handle(self, m: dict) -> None:
        """One input message from the phone. Content is never logged."""
        tab = self.visible
        if tab is None:
            return
        s, t = tab.session, m.get("type")
        if t == "tap":
            x, y = float(m["x"]), float(m["y"])
            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, s)
            for kind in ("mousePressed", "mouseReleased"):
                await self.cdp.send("Input.dispatchMouseEvent", {
                    "type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}, s)
            asyncio.ensure_future(self._report_focus(s))
        elif t == "wheel":
            await self.cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": float(m["x"]), "y": float(m["y"]),
                "deltaX": float(m.get("dx", 0)), "deltaY": float(m.get("dy", 0))}, s)
        elif t == "text":
            text = str(m.get("text", ""))[:512]
            if text:
                await self.cdp.send("Input.insertText", {"text": text}, s)
        elif t == "key":
            key = m.get("key")
            code = _KEYS.get(key)
            if code is None:
                return
            down = {"type": "rawKeyDown", "key": key, "code": key,
                    "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
            if key == "Enter":                  # a keypress with text is what submits a form
                down.update(type="keyDown", text="\r", unmodifiedText="\r")
            await self.cdp.send("Input.dispatchKeyEvent", down, s)
            await self.cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": key, "code": key,
                "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}, s)
        elif t == "back":
            h = await self.cdp.send("Page.getNavigationHistory", {}, s)
            i = h.get("currentIndex", 0)
            if i > 0:
                await self.cdp.send("Page.navigateToHistoryEntry",
                                    {"entryId": h["entries"][i - 1]["id"]}, s)

    async def _report_focus(self, session: str) -> None:
        """Tell the phone whether the tap landed in a text field, so it can
        hide its keyboard when it didn't. Reads the element's KIND only —
        never its value. Inside a cross-origin frame (Duo) we cannot tell,
        so the keyboard stays."""
        await asyncio.sleep(0.2)
        try:
            r = await self.cdp.send("Runtime.evaluate", {"returnByValue": True, "expression": (
                "(() => { const a = document.activeElement;"
                " return !!a && (a.isContentEditable || a.tagName === 'IFRAME'"
                " || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)); })()")}, session, timeout=3)
            editable = bool((r.get("result") or {}).get("value"))
        except Exception:
            return
        await self._quiet(self._emit({"type": "focus", "editable": editable}))

    async def stop_loading(self) -> None:
        """Stop the page where it is. The login tab reaching Canvas is the end
        of its job: the Canvas UI behind the "is this you?" card is 20 MB and 92
        scripts (a HAR of canvas.wisc.edu's dashboard, 2026-09-26) that the
        identity check — one API call — never needs."""
        tab = self.visible
        if tab is not None:
            await self._quiet(self.cdp.send("Page.stopLoading", {}, tab.session))

    async def park(self) -> None:
        """Leave the tab on a blank page (its renderer's scripts end there)."""
        tab = self.visible
        if tab is not None:
            await self._quiet(self.cdp.send("Page.navigate", {"url": "about:blank"}, tab.session))

    async def resume(self, url: str) -> None:
        tab = self.visible
        if tab is not None:
            await self._quiet(self.cdp.send("Page.navigate", {"url": url}, tab.session))

    async def restart(self, start_url: str) -> None:
        """Back to the start (after "that's not me": log out and try again)."""
        tab = self.visible
        if tab is not None:
            await self.cdp.send("Network.clearBrowserCookies", {}, tab.session)
            await self.cdp.send("Page.navigate", {"url": start_url}, tab.session)


# --------------------------------------------------------------------------- #
# The server: HTTP for the page, one WebSocket per viewer
# --------------------------------------------------------------------------- #

@dataclass
class _Live:
    uid: str
    ws: object
    view: Optional[BrowserView] = None
    started: float = field(default_factory=time.monotonic)
    last_input: float = field(default_factory=time.monotonic)
    checking: bool = False
    touched: float = 0.0                      # last time we kept their browser from the reaper
    pending: Optional[object] = None          # the Status awaiting "that's me"
    done: bool = False
    policy: Optional[LoginPolicy] = None       # their school's Canvas and login domains
    start_url: str = ""
    mode: str = "login"                        # "login", or "open": working on a page the bot handed them
    owner: bool = False                        # the owner, from the private bot: their own browser


@dataclass
class OwnerLogin:
    """What the login page needs to serve the owner (see LoginServer ``owner``).

      bot_token      the PRIVATE bot's token: its buttons' initData is signed with it
      cdp_url        the owner's own browser (9222)
      canvas_origin  the owner's Canvas
      check()        -> the logged-in Canvas user's name, or None (blocking)
      on_login(name) tell the owner in the private bot's chat (blocking)
    """
    bot_token: str
    cdp_url: str
    canvas_origin: str
    check: Callable[[], Optional[str]]
    on_login: Callable[[str], None]
    allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED


class LoginServer:
    """Serves /login and /ws on ``host:port``; runs its own event loop thread.

    ``base_path`` puts both under a prefix, e.g. "/native-bot-learning" when
    the tunnel sends https://example.com/native-bot-learning/* here (cloudflared
    forwards the path unchanged).

    Everything that touches a person's data is injected:
      bot_token      checks initData (the PUBLIC bot's token)
      allowed(uid)   is this Telegram user approved?
      cdp_url(uid)   their pooled browser (starts it if needed; blocking)
      sessions       Cookie Management's SessionManager (inspect/complete_login),
                     or None to only show the login and not save it
      on_connected(uid, name)   tell them in the chat
      school_of(uid) their schools.School (Canvas origin, SSO start, login
                     domains), or None = not chosen yet. Without it, everyone
                     uses ``canvas_origin``.
      monitor        () -> JSON text for the owner's /monitor page (monitor.py),
                     served only to ``owner_id``'s verified initData; None = off
      on_visit(uid, host)  each host their login tab was let through to (the
                     bot records it per school: each school's real SSO chain)
      on_open(uid)   their login page opened (approved, school known); must not
                     block — the bot starts the intro video here (intro.py)
      owner          OwnerLogin, or None: the same page for the owner, opened from
                     the PRIVATE bot's button when its Canvas session expired.
                     Their initData is signed with that bot's token; it streams
                     the owner's own browser, and no registry or saved session
                     is involved — the private bot reads the new cookies from
                     that browser.
    """

    def __init__(self, *, bot_token: str, canvas_origin: str,
                 allowed: Callable[[str], bool], cdp_url: Callable[[str], str],
                 sessions=None, on_connected: Callable[[str, str], None] = lambda u, n: None,
                 public_origin: Optional[str] = None, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT, max_session_s: float = 600, idle_s: float = 180,
                 allowed_suffixes: tuple[str, ...] = DEFAULT_ALLOWED,
                 restrict_hosts: bool = False,
                 dev_user: Optional[str] = None,
                 base_path: str = "",
                 school_of: Optional[Callable[[str], object]] = None,
                 monitor: Optional[Callable[[], str]] = None,
                 owner_id: Optional[str] = None,
                 on_visit: Optional[Callable[[str, str], None]] = None,
                 on_open: Optional[Callable[[str], None]] = None,
                 owner: Optional["OwnerLogin"] = None,
                 connect: Callable[[str], Awaitable[Cdp]] = Cdp.connect) -> None:
        self.bot_token, self.canvas_origin = bot_token, canvas_origin.rstrip("/")
        self.allowed, self.cdp_url, self.sessions = allowed, cdp_url, sessions
        self.on_connected = on_connected
        self.public_origin = public_origin.rstrip("/") if public_origin else None
        self.host, self.port = host, port
        self.max_session_s, self.idle_s = max_session_s, idle_s
        self.policy = LoginPolicy(self.canvas_origin, allowed_suffixes, restrict_hosts)
        self.school_of = school_of
        self.on_visit = on_visit
        self.on_open = on_open
        self.dev_user = dev_user
        self.base_path = "/" + base_path.strip("/") if base_path.strip("/") else ""
        self._connect = connect
        self.monitor, self.owner_id = monitor, (str(owner_id) if owner_id else None)
        self.owner = owner if (owner is not None and self.owner_id) else None
        self._live: dict[str, _Live] = {}
        # "Open in my browser" tickets: id -> (uid, url, expires). The button
        # carries only the id; the url never leaves the server, and a ticket
        # only works for the person it was made for (their signed initData).
        self._opens: dict[str, tuple[str, str, float]] = {}
        self._opens_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._stopped: Optional[asyncio.Event] = None

    # -- running -------------------------------------------------------------

    async def serve(self, ready: Optional[threading.Event] = None) -> None:
        from websockets.asyncio.server import serve

        self._loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        async with serve(self._ws, self.host, self.port, process_request=self._http,
                         max_size=64 * 1024, ping_interval=20) as server:
            self._server = server
            self.port = server.sockets[0].getsockname()[1]      # when port=0 (tests)
            log.info("Canvas login viewer on http://%s:%s%s/login", self.host, self.port, self.base_path)
            if ready:
                ready.set()
            reaper = asyncio.ensure_future(self._reap())
            try:
                await self._stopped.wait()
            finally:
                reaper.cancel()
                for live in list(self._live.values()):
                    await self._end(live, "server stopping")

    def start_in_thread(self, timeout: float = 10) -> None:
        ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self.serve(ready)),
                                        name="login-viewer", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            raise RuntimeError(f"login viewer did not start on {self.host}:{self.port}")

    def stop(self) -> None:
        if self._loop and self._stopped:
            self._loop.call_soon_threadsafe(self._stopped.set)
        if self._thread:
            self._thread.join(timeout=10)

    OPEN_TTL_S = 600
    OPEN_MAX_S, OPEN_IDLE_S = 3600, 600      # working on homework: longer than a login

    def issue_open(self, uid: str, url: str, ttl: float = OPEN_TTL_S) -> str:
        """A ticket that opens ``url`` in this person's own browser, streamed
        like the login. For platforms Canvas will not give a one-time link to
        from a browser login (LTI 1.3: McGraw Hill, 2026-09-26). Any thread."""
        import secrets
        tid = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._opens_lock:
            self._opens = {k: v for k, v in self._opens.items() if v[2] > now}
            self._opens[tid] = (str(uid), url, now + ttl)
        return tid

    def _open_target(self, uid: str, tid: str) -> Optional[str]:
        with self._opens_lock:
            got = self._opens.get(tid or "")
        if got is None or got[0] != uid or got[2] <= time.monotonic():
            return None
        return got[1]

    def stats(self) -> dict[str, dict]:
        """uid -> {open_s, idle_s, done} for each login viewer open right now.
        Read-only, from any thread (monitor.py)."""
        now = time.monotonic()
        return {uid: {"open_s": round(now - lv.started), "idle_s": round(now - lv.last_input),
                      "done": lv.done}
                for uid, lv in dict(self._live).items()}

    def kick(self, uid: str) -> None:
        """End this person's viewer, from any thread (``/forget`` calls it)."""
        if self._loop is None:
            return
        live = self._live.get(uid)
        if live is not None:
            fut = asyncio.run_coroutine_threadsafe(self._end(live, "ended"), self._loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass

    # -- HTTP ----------------------------------------------------------------

    def _http(self, connection, request):
        path = request.path.split("?", 1)[0]
        if self.base_path:
            if path != self.base_path and not path.startswith(self.base_path + "/"):
                return connection.respond(404, "not found\n")
            path = path[len(self.base_path):] or "/"
        if path == "/ws":
            if not self._origin_ok(request.headers.get("Origin")):
                return connection.respond(403, "forbidden\n")
            return None
        if path == "/healthz":
            return connection.respond(200, "ok\n")
        if path == "/monitor" and self.monitor is not None:
            r = connection.respond(200, MONITOR_PAGE.read_text(encoding="utf-8"))
            del r.headers["Content-Type"]          # respond() set text/plain; one type only
            r.headers["Content-Type"] = "text/html; charset=utf-8"
            r.headers["Cache-Control"] = "no-store"
            r.headers["Referrer-Policy"] = "no-referrer"
            r.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'unsafe-inline' https://telegram.org; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org")
            return r
        if path == "/monitor.json" and self.monitor is not None:
            return self._monitor_json(connection, request)
        if path in ("/", "/login"):
            r = connection.respond(200, PAGE.read_text(encoding="utf-8"))
            del r.headers["Content-Type"]          # respond() set text/plain; one type only
            r.headers["Content-Type"] = "text/html; charset=utf-8"
            r.headers["Cache-Control"] = "no-store"
            r.headers["Referrer-Policy"] = "no-referrer"
            r.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self' 'unsafe-inline' https://telegram.org; "
                "style-src 'unsafe-inline'; img-src blob: data:; connect-src 'self' wss: ws:; "
                "base-uri 'none'; form-action 'none'")
            return r
        return connection.respond(404, "not found\n")

    def _monitor_allowed(self, request) -> bool:
        """Only the owner, by Telegram's signature. The initData rides in a
        header (never the URL, which ends up in logs); a custom header also
        means another site's page can't make the phone's browser send it."""
        if not self.owner_id:
            return False
        got = request.headers.get("X-Telegram-Init-Data") or ""
        uid = initdata.user_of(got, self.bot_token, max_age_s=MONITOR_MAX_AGE_S)
        return uid is not None and uid == self.owner_id

    async def _monitor_json(self, connection, request):
        if not self._monitor_allowed(request):
            return connection.respond(403, "forbidden\n")
        try:
            # Collecting reads the process table (and, every few polls, `top`):
            # off the event loop, so live login streams don't stutter.
            body = await asyncio.to_thread(self.monitor)
        except Exception as e:
            log.warning("monitor snapshot failed: %s", type(e).__name__)
            return connection.respond(500, "monitor failed\n")
        r = connection.respond(200, body)
        del r.headers["Content-Type"]          # respond() set text/plain; one type only
        r.headers["Content-Type"] = "application/json; charset=utf-8"
        r.headers["Cache-Control"] = "no-store"
        return r

    def _origin_ok(self, origin: Optional[str]) -> bool:
        if self.dev_user is not None:
            return True
        if self.public_origin is None:
            return True                          # not exposed; initData still required
        return origin == self.public_origin

    # -- WebSocket -----------------------------------------------------------

    async def _ws(self, ws) -> None:
        uid = None
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            uid = self._who(hello)
            if uid is None and self._owner_of(hello):
                return await self._owner_session(ws, hello)
            if uid is None:
                await ws.send(json.dumps({"type": "error", "text": "Please open this from the bot's Canvas login button."}))
                return await ws.close(4401, "unauthorized")
            if not await asyncio.to_thread(self.allowed, uid):
                await ws.send(json.dumps({"type": "error", "text": "You're not approved yet."}))
                return await ws.close(4403, "not approved")
            policy, start_url = self.policy, self.canvas_origin + "/"
            if self.school_of is not None:
                school = await asyncio.to_thread(self.school_of, uid)
                if school is None:
                    await ws.send(json.dumps({"type": "error", "text":
                        "Pick your school in the bot first (/school), then tap Canvas login."}))
                    return await ws.close(4409, "no school")
                policy = LoginPolicy(school.canvas_origin, school.allowed_suffixes,
                                     self.policy.restrict_hosts)
                start_url = school.start_url
            mode = "login"
            if hello.get("open"):
                target = self._open_target(uid, str(hello["open"]))
                if target is None:
                    await ws.send(json.dumps({"type": "error", "text":
                        "That link has expired — send the assignment's code in the bot again."}))
                    return await ws.close(4410, "open ticket expired")
                mode, start_url = "open", target
            old = self._live.get(uid)
            if old is not None:
                await self._end(old, "opened again somewhere else")
            live = self._live[uid] = _Live(uid, ws, policy=policy, start_url=start_url, mode=mode)
            log.info("login viewer opened for user %s", uid)
            if self.on_open is not None:
                try:
                    self.on_open(uid)
                except Exception:
                    log.exception("on_open failed for user %s", uid)
            await ws.send(json.dumps({"type": "status", "text": "Starting your browser…"}))
            cdp = await self._connect(await asyncio.to_thread(self.cdp_url, uid))

            async def emit(obj: dict) -> None:
                await ws.send(json.dumps(obj))

            live.view = BrowserView(cdp, live.policy, emit=emit, emit_frame=ws.send,
                                    same_tab=live.mode == "open",
                                    on_url=lambda url: self._maybe_check(live, url),
                                    on_visit=lambda host: self._visited(uid, host))
            await live.view.open(live.start_url, hello.get("w", 390),
                                 hello.get("h", 700), hello.get("dpr", 2))
            await ws.send(json.dumps({"type": "ready"}))
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                m = json.loads(raw)
                live.last_input = time.monotonic()
                if m.get("type") == "confirm":
                    await self._confirm(live, bool(m.get("yes")))
                else:
                    await live.view.handle(m)
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            # Type only: a message could echo what was typed.
            log.warning("login viewer for user %s failed: %s", uid, type(e).__name__)
            try:
                await ws.send(json.dumps({"type": "error", "text": "Something went wrong on my side. Close this and tap Canvas login again."}))
            except Exception:
                pass
        finally:
            live = self._live.get(uid) if uid else None
            if live is not None and live.ws is ws:
                await self._end(live, "")

    def _visited(self, uid: str, host: str) -> None:
        """Hand a visited host to on_visit off the event loop (it writes a DB)."""
        if self.on_visit is None or self._loop is None:
            return

        def run() -> None:
            try:
                self.on_visit(uid, host)
            except Exception:
                log.exception("on_visit failed")
        self._loop.run_in_executor(None, run)

    def _who(self, hello: dict) -> Optional[str]:
        if hello.get("type") != "hello":
            return None
        if self.dev_user is not None and not hello.get("initData"):
            return self.dev_user
        return initdata.user_of(str(hello.get("initData") or ""), self.bot_token)

    async def _end(self, live: _Live, reason: str) -> None:
        if self._live.get(live.uid) is live:
            del self._live[live.uid]
        if live.view is not None:
            view, live.view = live.view, None
            await view.close()
        try:
            if reason:
                await live.ws.send(json.dumps({"type": "bye", "text": reason}))
            await live.ws.close()
        except Exception:
            pass
        log.info("login viewer closed for user %s%s", live.uid, f" ({reason})" if reason else "")

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for live in list(self._live.values()):
                opened = live.mode == "open"
                if now - live.started > (self.OPEN_MAX_S if opened else self.max_session_s):
                    await self._end(live, "time's up — send the code again if you still need it"
                                    if opened else "time's up — tap Canvas login again if you still need it")
                elif (now - live.last_input > (self.OPEN_IDLE_S if opened else self.idle_s)
                      and not live.checking):
                    await self._end(live, "closed after a few idle minutes")
                elif not live.done and not live.owner and now - live.touched > 60:
                    # Keep their browser from being reaped as idle mid-login.
                    live.touched = now
                    await asyncio.to_thread(self.cdp_url, live.uid)

    # -- the owner, from the private bot ---------------------------------------

    def _owner_of(self, hello: dict) -> bool:
        """Is this hello the owner's, signed by the PRIVATE bot's token?"""
        if self.owner is None or hello.get("type") != "hello" or hello.get("open"):
            return False
        uid = initdata.user_of(str(hello.get("initData") or ""), self.owner.bot_token)
        return uid is not None and uid == self.owner_id

    async def _owner_session(self, ws, hello: dict) -> None:
        """The login page on the owner's own browser. Same fence (Canvas, the
        school's SSO, Duo), no registry, nothing saved here."""
        uid, o = str(self.owner_id), self.owner
        old = self._live.get(uid)
        if old is not None:
            await self._end(old, "opened again somewhere else")
        policy = LoginPolicy(o.canvas_origin, o.allowed_suffixes, self.policy.restrict_hosts)
        live = self._live[uid] = _Live(uid, ws, policy=policy,
                                       start_url=o.canvas_origin + "/", owner=True)
        log.info("login viewer opened for the owner (private bot)")
        try:
            await ws.send(json.dumps({"type": "status", "text": "Opening your browser…"}))
            cdp = await self._connect(o.cdp_url)

            async def emit(obj: dict) -> None:
                await ws.send(json.dumps(obj))

            live.view = BrowserView(cdp, live.policy, emit=emit, emit_frame=ws.send,
                                    on_url=lambda url: self._maybe_check(live, url))
            await live.view.open(live.start_url, hello.get("w", 390),
                                 hello.get("h", 700), hello.get("dpr", 2))
            await ws.send(json.dumps({"type": "ready"}))
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                m = json.loads(raw)
                live.last_input = time.monotonic()
                if m.get("type") != "confirm":
                    await live.view.handle(m)
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            log.warning("owner login viewer failed: %s", type(e).__name__)
            try:
                await ws.send(json.dumps({"type": "error", "text": "Something went wrong on my side. Close this and tap Canvas login again."}))
            except Exception:
                pass
        finally:
            if self._live.get(uid) is live:
                await self._end(live, "")

    async def _owner_check(self, live: _Live) -> None:
        """On Canvas: logged in for real? Ask Canvas who this is, with the
        browser's own cookies. Yes -> done, and the private bot says so."""
        try:
            name = await asyncio.to_thread(self.owner.check)
            if not name:
                if live.view is not None:                # not logged in after all: go on
                    await live.view.resume(live.policy.canvas_origin + "/")
                return
            live.done = True
            await live.ws.send(json.dumps({"type": "done", "name": name}))
            try:
                await asyncio.to_thread(self.owner.on_login, name)
            except Exception:
                log.exception("owner on_login failed")
            await asyncio.sleep(1.5)
            await self._end(live, "")
        except Exception as e:
            log.warning("owner login check failed: %s", type(e).__name__)
        finally:
            live.checking = False

    # -- "is this you?" --------------------------------------------------------

    def _maybe_check(self, live: _Live, url: str) -> None:
        if live.mode != "login":
            return                                   # working on a page, not logging in
        if live.done or live.checking or live.pending is not None or not live.policy.on_canvas(url):
            return
        live.checking = True
        if live.view is not None:
            asyncio.ensure_future(live.view.stop_loading())
        asyncio.ensure_future(self._owner_check(live) if live.owner else self._check(live))

    async def _check(self, live: _Live) -> None:
        try:
            if self.sessions is None:
                await live.ws.send(json.dumps({"type": "status", "text": "Logged in (dev: not saved)."}))
                return
            st = await asyncio.to_thread(self.sessions.inspect, live.uid)
            state = getattr(st.state, "value", str(st.state))
            if state in ("active", "unbound") and st.canvas_id:
                live.pending = st
                if live.view is not None:
                    await live.view.park()               # the card covers it; nothing to load
                await live.ws.send(json.dumps({"type": "confirm", "name": st.name or "(no name)"}))
            elif state == "identity_mismatch":
                await live.ws.send(json.dumps({"type": "error", "text":
                    "That's a different Canvas account from the one you connected before. "
                    "Log out of it, or /forget in the bot to start over."}))
            else:
                log.info("login check for user %s: %s", live.uid, state)
                if live.view is not None:                # not logged in after all: let them go on
                    await live.view.resume(live.policy.canvas_origin + "/")
        except Exception as e:
            log.warning("login check for user %s failed: %s", live.uid, type(e).__name__)
        finally:
            live.checking = False

    async def _confirm(self, live: _Live, yes: bool) -> None:
        st, live.pending = live.pending, None
        if st is None or live.view is None:
            return
        if not yes:
            await live.ws.send(json.dumps({"type": "status", "text": "Logged out. Sign in with your own NetID."}))
            await live.view.restart(live.start_url)
            return
        res = await asyncio.to_thread(self.sessions.complete_login, live.uid, st.canvas_id)
        if getattr(res.state, "value", str(res.state)) != "active":
            await live.ws.send(json.dumps({"type": "error", "text": "Couldn't save the login. Try again in a minute."}))
            log.warning("complete_login for user %s: %s", live.uid, res.state)
            return
        live.done = True
        await live.ws.send(json.dumps({"type": "done", "name": st.name}))
        try:
            await asyncio.to_thread(self.on_connected, live.uid, st.name)
        except Exception:
            log.exception("on_connected failed for user %s", live.uid)
        await asyncio.sleep(1.5)
        await self._end(live, "")


# --------------------------------------------------------------------------- #
# Dev: this machine only, one registered test user, no Telegram
# --------------------------------------------------------------------------- #

def main() -> int:
    import argparse
    import os
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from browsers import BrowserPool
    from registry import Registry

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dev-user", required=True,
                    help="a registry id to show (created + approved if missing), e.g. me_1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--base-path", default="", help="e.g. /native-bot-learning")
    ap.add_argument("--school", help="a school key (see the schools table), e.g. ucsb "
                                     "(default: --canvas)")
    ap.add_argument("--open", help="dev: also issue an 'open in my browser' ticket for this URL "
                                   "and print the page address that uses it")
    ap.add_argument("--with-sessions", action="store_true",
                    help="run the whole flow: 'is this you?', then save the encrypted "
                         "session (key: AINL_SESSION_KEY, or a throwaway one)")
    ap.add_argument("--canvas", default=os.environ.get("CANVAS_BASE_URL") or "https://canvas.wisc.edu")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    reg = Registry()
    if reg.get(a.dev_user) is None:
        reg.add(a.dev_user, name=f"dev ({a.dev_user})", login_id="dev")
        reg.approve(a.dev_user)
    pool = BrowserPool(reg)
    pool.adopt()                                  # a browser left by an earlier dev run
    import schools
    school = schools.get(a.school) if a.school else None
    if a.school and school is None:
        ap.error(f"unknown school {a.school!r}; known: {', '.join(s.key for s in schools.all())}")
    if school:
        a.canvas = school.canvas_origin
    sessions = None
    if a.with_sessions:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Cookie Management"))
        from cookie_management.integration import build_services
        import tokens
        key = os.environ.get("AINL_SESSION_KEY", "").strip()
        if not key:
            from cryptography.fernet import Fernet
            key = Fernet.generate_key().decode()
            log.warning("no AINL_SESSION_KEY: using a throwaway key; this snapshot is unreadable later")
        sessions = build_services(reg, pool, lambda t: tokens.verify(reg, t), origin=a.canvas,
                                  encryption_key=key.encode("ascii")).sessions
    server = LoginServer(bot_token="", canvas_origin=a.canvas, allowed=reg.is_approved,
                         cdp_url=pool.cdp_url, sessions=sessions, port=a.port, dev_user=a.dev_user,
                         base_path=a.base_path,
                         school_of=(lambda uid: school) if school else None,
                         on_connected=lambda u, n: log.info("dev: user %s connected", u))
    if a.open:
        tid = server.issue_open(a.dev_user, a.open, ttl=3600)
        print(f"open: http://127.0.0.1:{a.port}{server.base_path}/login?open={tid}", flush=True)
    # A background run ignores Ctrl-C and gets SIGTERM instead: treat both as
    # "stop", so the browsers it started are stopped too (a SIGTERM used to
    # leave an orphan Chromium on its 93xx port, 2026-09-26).
    import signal

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass
    finally:
        pool.shutdown_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
