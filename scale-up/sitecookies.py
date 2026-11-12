"""Keep a person's website sessions across a browser stop.

A vendor's login lives in SESSION cookies — WebAssign's JSESSIONID and
cmp-session-id have no expiry — which Chrome drops when it exits. So stopping
an idle browser logged the person out of WebAssign, and the next use paid a
~10-20 s re-launch from Canvas. Saving those cookies before a stop and putting
them back after the next start makes the stop almost free, which is what lets
an idle browser go early. (The same move Browserbase and Steel make: session
cookies are backed up and restored; browser-use drops the -1/0 "expires" on
restore because CDP reads it as already expired.)

- Over raw CDP (Storage.getCookies / Storage.setCookies on the browser
  target): no Playwright connection, nothing a worker thread holds.
- Only session cookies (persistent ones survive in the profile anyway), and
  never the person's Canvas cookies: those belong to Cookie Management, which
  checks the account identity before it restores anything.
- Encrypted with the same key as the Canvas snapshot, in their own user dir
  (so /forget deletes it), bound to their id; older than MAX_AGE is not used —
  the server has expired it anyway, and the normal Canvas re-launch takes over.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("ainl.scaleup.sitecookies")

FILE = "site-cookies.enc"
MAX_AGE_S = 12 * 3600


def _cdp(port: int, method: str, params: Optional[dict] = None, timeout: float = 5) -> dict:
    from websockets.sync.client import connect
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
        ws_url = json.load(r)["webSocketDebuggerUrl"]
    with connect(ws_url, proxy=None, max_size=32 * 2**20, open_timeout=timeout) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv(timeout=timeout))
            if m.get("id") == 1:
                if "error" in m:
                    raise RuntimeError(str(m["error"].get("message"))[:120])
                return m.get("result") or {}


def session_cookies(cookies: list[dict], canvas_host: str) -> list[dict]:
    """The cookies a browser stop would lose, minus the person's Canvas ones."""
    host = (canvas_host or "").lower()
    out = []
    for c in cookies:
        if not c.get("session") and c.get("expires", -1) not in (-1, 0, None):
            continue                                   # persistent: kept by the profile
        if host and (c.get("domain") or "").lstrip(".").lower() == host:
            continue                                   # Canvas: Cookie Management's job
        out.append(c)
    return out


def as_params(cookies: list[dict]) -> list[dict]:
    """Storage.getCookies records -> Storage.setCookies params. No "expires"
    for a session cookie: -1/0 would be read as already expired."""
    keep = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite",
            "priority", "sameParty", "sourceScheme", "sourcePort", "partitionKey")
    return [{k: c[k] for k in keep if k in c and c[k] is not None} for c in cookies]


class SiteCookies:
    """Save before a stop, put back after a start, for one person at a time."""

    def __init__(self, registry, key: bytes, *, canvas_host_of=lambda uid: "",
                 cdp=_cdp, clock=time.time) -> None:
        from cryptography.fernet import Fernet
        self._reg, self._cipher = registry, Fernet(key)
        self._canvas_host_of, self._cdp, self._clock = canvas_host_of, cdp, clock

    def _path(self, uid: str) -> Path:
        return self._reg.user_dir(uid) / FILE

    def save(self, uid: str, port: int) -> int:
        """Read the running browser's session cookies and store them. Returns
        how many; never raises (a stop must not fail because of this)."""
        try:
            got = self._cdp(port, "Storage.getCookies").get("cookies") or []
            keep = session_cookies(got, self._canvas_host_of(uid))
            blob = json.dumps({"schema": 1, "user_id": uid, "saved": self._clock(), "cookies": keep})
            data = self._cipher.encrypt(blob.encode())
            path = self._path(uid)
            fd, tmp = tempfile.mkstemp(prefix=".site-cookies-", dir=path.parent)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return len(keep)
        except Exception as e:
            log.info("site cookies for %s not saved: %s", uid, type(e).__name__)
            return 0

    def restore(self, uid: str, port: int) -> int:
        """Put the saved session cookies into a freshly started browser.
        Returns how many; 0 when there is nothing fresh to restore."""
        try:
            raw = self._cipher.decrypt(self._path(uid).read_bytes())
            snap = json.loads(raw)
        except FileNotFoundError:
            return 0
        except Exception as e:
            log.info("site cookies for %s unreadable: %s", uid, type(e).__name__)
            return 0
        if snap.get("user_id") != uid or self._clock() - float(snap.get("saved") or 0) > MAX_AGE_S:
            return 0                                   # someone else's, or stale
        cookies = as_params(snap.get("cookies") or [])
        if not cookies:
            return 0
        try:
            self._cdp(port, "Storage.setCookies", {"cookies": cookies})
            return len(cookies)
        except Exception as e:
            log.info("site cookies for %s not restored: %s", uid, type(e).__name__)
            return 0
