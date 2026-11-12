"""When does Page.captureScreenshot answer, and when does it hang? Raw CDP.

Found while building the wedge probe (docs/timing-analysis.md): on a healthy,
fully loaded WebAssign tab a bare Page.captureScreenshot sometimes never
answers, while evaluate and getLayoutMetrics answer in 1-2 ms. This script
pins down the trigger without Playwright in the way. Variants:

    idle        capture on a page nobody has touched for 1.5 s
    concurrent  capture, and a trivial evaluate('1+1') sent right behind it
    dirty       change a style (page now needs a new frame), THEN capture
    scroll      scrollBy(0, 1) then capture 120 ms later — what shoot_all does
    raf         no capture at all: does requestAnimationFrame ever fire?

    python scripts/capture_probe.py [trials]

Touches nothing on the assignment: no clicks, no form fields, no submit. The
style it toggles is an invisible outline on <html>, cleared at the end.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from websockets.sync.client import connect

CDP = "http://127.0.0.1:9222"
BUDGET = 6.0
DIRTY = ("(() => { const s = document.documentElement.style;"
         " s.outline = s.outline ? '' : '1px solid transparent'; return 1; })()")
CLEAN = "(() => { document.documentElement.style.outline = ''; return 1; })()"
RAF = "new Promise(r => requestAnimationFrame(() => r(1)))"


def page_ws() -> str:
    targets = json.loads(urllib.request.urlopen(f"{CDP}/json/list", timeout=2).read())
    pages = [t for t in targets if t.get("type") == "page"]
    page = next((t for t in pages if "webassign.net" in t.get("url", "")), pages[0])
    return page["webSocketDebuggerUrl"]


class Session:
    def __init__(self, ws_url: str):
        self.ws = connect(ws_url, open_timeout=2, max_size=None)
        self.n = 0

    def send(self, method: str, **params) -> int:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        return self.n

    def wait(self, msg_id: int, budget: float = BUDGET):
        """Seconds until that id answered, or None."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget:
            try:
                msg = json.loads(self.ws.recv(timeout=max(0.01, budget - (time.monotonic() - t0))))
            except TimeoutError:
                return None
            if msg.get("id") == msg_id:
                return time.monotonic() - t0
        return None

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def shot(s: Session) -> int:
    return s.send("Page.captureScreenshot", format="jpeg", quality=10)


def run(variant: str, ws_url: str):
    s = Session(ws_url)
    try:
        if variant == "idle":
            return s.wait(shot(s))
        if variant == "concurrent":
            i = shot(s)
            s.send("Runtime.evaluate", expression="1+1")
            return s.wait(i)
        if variant == "dirty":
            s.wait(s.send("Runtime.evaluate", expression=DIRTY), 2)
            return s.wait(shot(s))
        if variant == "scroll":
            s.wait(s.send("Runtime.evaluate", expression="window.scrollBy(0, 1); 1"), 2)
            time.sleep(0.12)
            return s.wait(shot(s))
        if variant == "raf":
            return s.wait(s.send("Runtime.evaluate", expression=RAF, awaitPromise=True))
        raise ValueError(variant)
    finally:
        s.close()


def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    variants = sys.argv[2].split(",") if len(sys.argv) > 2 else \
        ["idle", "concurrent", "dirty", "scroll", "raf"]
    ws_url = page_ws()
    out: dict[str, list] = {v: [] for v in variants}
    try:
        for i in range(trials):
            for v in variants:
                time.sleep(1.5)                  # let the page go quiet again
                dt = run(v, ws_url)
                out[v].append(None if dt is None else round(dt, 3))
                print(f"trial {i + 1} {v:<10} "
                      f"{f'NO ANSWER in {BUDGET:.0f}s' if dt is None else f'{dt:.3f}s'}", flush=True)
    finally:
        s = Session(ws_url)
        s.wait(s.send("Runtime.evaluate", expression=CLEAN), 2)
        s.close()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
