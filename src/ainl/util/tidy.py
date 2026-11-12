"""Put an idle browser back to one blank tab.

Every page left open keeps a renderer process and its scripts running — and
the bot's browsers run with background throttling OFF, so an idle WebAssign
tab spends as much as a busy one. Measured 2026-09-26: tabs left open after
work (account.cengage.com, a Macmillan page, Chrome's own new-tab page) were
100–350 MB of renderer each.

Closing them loses nothing: logins live in the cookie jar, not in a tab, and
WebAssign opens its list page again on the next use (a few seconds).

Only over the DevTools HTTP endpoints (/json/list, /json/new, /json/close), so
it needs no Playwright connection and cannot disturb a worker's. The caller
decides WHEN — only while nobody is using that browser.
"""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("ainl.tidy")

BLANK = ("about:blank", "")


def _get(url: str, method: str = "GET", timeout: float = 3):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body[:1] in (b"[", b"{") else body


def tidy(cdp_http: str, *, timeout: float = 3) -> int:
    """Close every page but one blank one. Returns how many were closed."""
    base = cdp_http.rstrip("/")
    pages = [t for t in _get(base + "/json/list", timeout=timeout) if t.get("type") == "page"]
    blank = next((p for p in pages if p.get("url") in BLANK), None)
    if blank is None:
        # Open the blank one FIRST: a browser whose last tab closes may exit.
        blank = _get(base + "/json/new?about:blank", method="PUT", timeout=timeout)
    closed = 0
    for p in pages:
        if p.get("id") != blank.get("id"):
            try:
                _get(f"{base}/json/close/{p['id']}", timeout=timeout)
                closed += 1
            except Exception as e:                  # already gone: fine
                log.debug("close %s failed: %s", p.get("id"), e)
    return closed
