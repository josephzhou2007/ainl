"""Attach to the served window and walk it to the WebAssign assignment list.

Clicks exactly one thing: the Cengage dashboard's "OPEN WEBASSIGN" button, which
is navigation, not graded content. Everything past that is done by URL. Never
closes a window or a tab.

    python scripts/wa_open.py
"""
from __future__ import annotations

import re
import sys

from playwright.sync_api import sync_playwright

PORT = 9222


def redact(url: str) -> str:
    return re.sub(r"(UserPass|CSRFToken|key)=[^&]*", r"\1=<redacted>", url)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{PORT}")
        ctx = browser.contexts[0]
        pages = [pg for pg in ctx.pages if not pg.is_closed()]

        wa = next((pg for pg in pages if "webassign.net" in pg.url), None)
        if wa is None:
            dash = next((pg for pg in pages if "cengage.com/dashboard" in pg.url), None)
            if dash is None:
                print("no Cengage dashboard tab open", file=sys.stderr)
                return 2
            if "auth.cengage.com" in dash.url:
                print("not logged in — log in in the window first", file=sys.stderr)
                return 3
            # The button is a plain <a target="_blank"> to a gateway launch URL.
            # Following it in THIS tab avoids spawning a popup — popups in a
            # Playwright-launched browser start paused and hang at about:blank
            # unless the launching client is pumping events.
            href = dash.evaluate("""() => {
                const a = [...document.querySelectorAll('a[data-testid="home-page-launch-course-link"]')]
                  .find(e => /open\\s*webassign/i.test(e.innerText));
                return a ? a.href : null; }""")
            if not href:
                print("OPEN WEBASSIGN link not found on the dashboard", file=sys.stderr)
                return 2
            href = re.sub(r"&_gl=[^&]*", "", href)          # analytics noise
            dash.goto(href, wait_until="domcontentloaded", timeout=90_000)
            dash.wait_for_timeout(6000)                       # gateway -> login.pl -> student.pl
            wa = dash

        print(f"webassign tab: {redact(wa.url)[:120]}")
        rows = wa.evaluate("""() => [...document.querySelectorAll('a[href*="dep="]')].map(a => ({
            dep: (a.href.match(/[?&]dep=(\\d+)/) || [])[1],
            text: a.innerText.replace(/\\s+/g, ' ').trim().slice(0, 100)})).filter(x => x.dep)""")
        seen = {}
        for r in rows:
            seen.setdefault(r["dep"], r["text"])
        print(f"{len(seen)} assignment link(s):")
        for dep, text in seen.items():
            print(f"  dep={dep}  {text}")
        if not seen:
            print("title:", wa.title())
            print(wa.evaluate("() => document.body.innerText.slice(0, 1500)"))
        # deliberately no browser.close(): for a CDP attach it would only drop
        # the connection, but leaving it to process exit is unambiguous.
    return 0


if __name__ == "__main__":
    sys.exit(main())
