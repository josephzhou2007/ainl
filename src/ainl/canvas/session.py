"""Browser-backed Canvas session for institutions that disable API tokens.

Flow:
  1. First run: a real Chrome window opens on canvas.wisc.edu. The human logs
     in (NetID + Duo). We never see the password; we only wait for the
     canvas_session cookie to appear and for /api/v1/users/self to answer 200.
  2. We save Playwright's storage_state to ~/.ainl/<host>.storage_state.json and
     close the browser. From then on every API call is plain httpx with those
     cookies — no browser in the loop.
  3. When Canvas says 401 / redirects to /login, ensure() reopens the window
     for a fresh login. Tick "remember this device" in Duo to make that rare.

The storage_state file is a session credential. Treat it like a password.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from ..config import home
from ._http import CanvasAuthError, CanvasHTTP
from .auth import CookieAuth


def auth_from_live_browser(base_url: str, cdp_url: str = "http://127.0.0.1:9222",
                           timeout_ms: float = 8000) -> Optional[CookieAuth]:
    """Canvas cookies from the browser window that is open right now.

    The saved storage_state is a snapshot, and a snapshot goes stale — the one
    that produced "session missing or expired" was 46 hours old. The window the
    student actually uses holds live cookies, rotated by Canvas on every
    request, so reading from it means the cookie channel is as fresh as their
    last click instead of as fresh as the last save.

    Returns None when the window is not running or Canvas is not logged in
    there; the caller then falls back to the snapshot or to a real login.
    """
    from ..util import pw

    host = urlparse(base_url).netloc
    try:
        with pw.connect(cdp_url, timeout_ms) as browser:
            for ctx in browser.contexts:
                auth = CookieAuth.from_cookie_list(ctx.cookies(), host)
                if auth:
                    return auth
    except Exception:
        return None
    return None


def open_canvas_login(base_url: str, cdp_url: str = "http://127.0.0.1:9222") -> bool:
    """Put the Canvas login page in front of the student, in the window they
    already have open.

    A session dies every day or so, and the only cure is a human doing SAML +
    Duo. The agent cannot do that part, but it can remove every step around it:
    instead of "run this command on the host", the page is simply there,
    focused, waiting. Returns False if there is no window to put it in.
    """
    from ..util import pw

    host = urlparse(base_url).netloc
    try:
        with pw.connect(cdp_url) as browser:
            ctx = browser.contexts[0]
            pages = [pg for pg in ctx.pages if not pg.is_closed()]
            tab = next((pg for pg in pages if host in pg.url or "login.wisc.edu" in pg.url),
                       None) or ctx.new_page()
            tab.goto(base_url, wait_until="domcontentloaded", timeout=25_000)
            tab.bring_to_front()
            return True
    except Exception:
        return False


class LoginTimeout(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, base_url: str, *, profile_dir: Optional[Path] = None, state_file: Optional[Path] = None):
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).netloc
        self.profile_dir = profile_dir or (home() / "chrome-profile")
        self.state_file = state_file or (home() / f"{self.host}.storage_state.json")

    # -- state on disk --------------------------------------------------------

    def load(self) -> Optional[CookieAuth]:
        if not self.state_file.exists():
            return None
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return CookieAuth.from_cookie_list(state.get("cookies", []), self.host)

    def save_cookies(self, jar: httpx.Cookies) -> int:
        """Merge rotated cookie values from an httpx jar back into storage_state.

        Canvas re-issues canvas_session (and friends) via Set-Cookie every so often;
        a client that keeps using the login-time value gets 401'd once the old one
        ages out. Persisting the rotation after each run keeps the session alive
        for as long as Canvas itself would. Returns the number of cookies updated."""
        if not self.state_file.exists():
            return 0
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        cookies = state.setdefault("cookies", [])
        by_key = {(c.get("name"), c.get("domain", "").lstrip(".")): c for c in cookies}
        changed = 0
        for c in jar.jar:                       # http.cookiejar.Cookie objects
            dom = (c.domain or self.host).lstrip(".")
            if not self.host.endswith(dom):
                continue
            entry = by_key.get((c.name, dom))
            if entry is None:
                entry = {"name": c.name, "domain": c.domain or self.host, "path": c.path or "/",
                         "httpOnly": False, "secure": bool(c.secure), "sameSite": "Lax"}
                cookies.append(entry)
                by_key[(c.name, dom)] = entry
            if entry.get("value") != c.value:
                entry["value"] = c.value
                if c.expires:
                    entry["expires"] = c.expires
                changed += 1
        if changed:
            self.state_file.write_text(json.dumps(state), encoding="utf-8")
        return changed

    def probe(self, auth: CookieAuth, transport: Optional[httpx.BaseTransport] = None) -> bool:
        """Does this session still work? One cheap authenticated GET."""
        http = CanvasHTTP(self.base_url, auth, transport=transport)
        try:
            http.get("/api/v1/users/self")
            return True
        except CanvasAuthError:
            return False
        finally:
            http.close()

    # -- interactive login ----------------------------------------------------

    # The login window must talk to the IdP directly. A system proxy (Anything
    # Analyzer MITM, Reqable, a VPN client) left switched on turns into proxy-auth
    # dialogs or certificate errors that look like "the login form keeps resetting".
    # NB: never add a second --disable-features here; Chrome keeps only the last
    # occurrence and Playwright already passes its own list.
    _CHROME_ARGS = ["--no-proxy-server"]

    def _refuse_if_profile_in_use(self) -> None:
        """Chrome silently hands off to an existing instance on the same profile
        and exits 0 — which surfaces as a baffling TargetClosedError. Check first."""
        import subprocess
        import sys

        needle = str(self.profile_dir)
        try:
            if sys.platform == "win32":
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | "
                     "Select-Object -ExpandProperty CommandLine"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
            else:
                # Full command lines, portable across macOS and Linux. (macOS
                # pgrep prints only PIDs — its -a means "ancestors" — so the
                # earlier ``pgrep -af`` never matched anything there.)
                out = subprocess.run(["ps", "-ax", "-o", "command="],
                                     capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return
        if needle.lower() in out.lower():
            raise LoginTimeout(
                f"A Chrome is already running with the profile {needle}.\n"
                "Close that window (or run:  python -m ainl.canvas --reset-profile) and retry."
            )

    def reset_profile(self) -> None:
        import shutil
        shutil.rmtree(self.profile_dir, ignore_errors=True)
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass

    def login_interactive(self, timeout_s: int = 300, debug: bool = False) -> CookieAuth:
        """Open a headed Chrome, wait for the human to finish SSO, persist cookies."""
        from playwright.sync_api import sync_playwright  # imported lazily: heavy

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        dbg_dir = home() / "login-debug"
        if debug:
            dbg_dir.mkdir(exist_ok=True)
            for old in dbg_dir.glob("*.png"):
                old.unlink()

        self._refuse_if_profile_in_use()

        with sync_playwright() as p:
            launch = dict(headless=False, args=self._CHROME_ARGS,
                          viewport={"width": 1200, "height": 850})
            try:
                ctx = p.chromium.launch_persistent_context(str(self.profile_dir), channel="chrome", **launch)
            except Exception as e:
                print(f"  (real Chrome unavailable: {type(e).__name__}; using bundled Chromium)", flush=True)
                try:
                    ctx = p.chromium.launch_persistent_context(str(self.profile_dir), **launch)
                except Exception as e2:
                    raise LoginTimeout(
                        "Chrome exited immediately. Almost always this means another Chrome is "
                        f"already using the profile at {self.profile_dir}.\n"
                        "Close it (or run:  python -m ainl.canvas --reset-profile) and retry.\n"
                        f"Underlying error: {type(e2).__name__}: {str(e2).splitlines()[0][:120]}"
                    ) from None
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            hops: list[str] = []

            # Event handlers must never block (no waits, no screenshots): doing so
            # stalls the navigation they are reporting on. Record only; act in the loop.
            def on_nav(frame):
                if frame == page.main_frame and frame.url != "about:blank":
                    hops.append(frame.url)

            page.on("framenavigated", on_nav)
            print(f"\nOpening {self.host} in a Chrome window. Log in there (NetID + Duo) — "
                  f"type into the BROWSER window, not this terminal.\n"
                  f"Waiting up to {timeout_s // 60} min…", flush=True)
            page.goto(self.base_url + "/", wait_until="commit")

            deadline = time.time() + timeout_s
            auth: Optional[CookieAuth] = None
            printed = 0
            while time.time() < deadline:
                # report new hops (and screenshot them) from here, outside any callback
                while printed < len(hops):
                    url = hops[printed]
                    printed += 1
                    u = urlparse(url)
                    print(f"  [{printed:02d}] {u.netloc}{u.path[:60]}", flush=True)
                    if debug:
                        try:
                            page.screenshot(path=str(dbg_dir / f"{printed:02d}-{u.netloc}.png"))
                        except Exception:
                            pass
                auth = CookieAuth.from_cookie_list(ctx.cookies(), self.host)
                # only probe the API while actually on Canvas; leave the IdP page alone
                if auth and urlparse(page.url).netloc == self.host and self._logged_in(page):
                    break
                auth = None
                page.wait_for_timeout(1500)
            if not auth:
                ctx.close()
                idp_hits = sum(1 for h in hops if "login.wisc.edu" in h)
                hint = ""
                if idp_hits >= 3:
                    hint = (f"\nThe IdP page loaded {idp_hits} times — a bounce loop. "
                            "Usually a proxy/VPN in the way, or cookies blocked in this profile. "
                            "Re-run with --login --debug and look at ~/.ainl/login-debug/*.png")
                raise LoginTimeout("No Canvas session appeared before the timeout." + hint)

            ctx.storage_state(path=str(self.state_file))
            ctx.close()
        try:
            self.state_file.chmod(0o600)
        except OSError:
            pass
        print(f"Session saved to {self.state_file}", flush=True)
        return auth

    @staticmethod
    def _logged_in(page) -> bool:
        try:
            status = page.evaluate(
                "async () => (await fetch('/api/v1/users/self', {credentials:'include'})).status"
            )
            return status == 200
        except Exception:
            return False

    # -- the one call everyone uses -------------------------------------------

    def ensure(self, *, interactive: bool = True) -> CookieAuth:
        auth = self.load()
        if auth and self.probe(auth):
            return auth
        if not interactive:
            raise CanvasAuthError(401, "/api/v1/users/self", "session missing or expired")
        return self.login_interactive()
