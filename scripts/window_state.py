"""Read or set the debug browser's window state over CDP (no Playwright).

    python scripts/window_state.py              # print it
    python scripts/window_state.py normal       # restore
    python scripts/window_state.py minimized    # minimise

Why this exists: docs/timing-analysis.md — a MINIMISED window stops producing
frames, and Page.captureScreenshot then takes whole seconds or never answers.
"""
from __future__ import annotations

import json
import sys
import urllib.request

from websockets.sync.client import connect

CDP = "http://127.0.0.1:9222"


def call(ws, i: int, method: str, **params) -> dict:
    ws.send(json.dumps({"id": i, "method": method, "params": params}))
    while True:
        msg = json.loads(ws.recv(timeout=5))
        if msg.get("id") == i:
            return msg


def main() -> None:
    ver = json.loads(urllib.request.urlopen(f"{CDP}/json/version", timeout=2).read())
    targets = json.loads(urllib.request.urlopen(f"{CDP}/json/list", timeout=2).read())
    page = next(t for t in targets if t.get("type") == "page")
    ws = connect(ver["webSocketDebuggerUrl"], open_timeout=2, max_size=None)
    try:
        win = call(ws, 1, "Browser.getWindowForTarget", targetId=page["id"])["result"]
        print("was:", win["bounds"])
        if len(sys.argv) > 1:
            out = call(ws, 2, "Browser.setWindowBounds", windowId=win["windowId"],
                       bounds={"windowState": sys.argv[1]})
            if "error" in out:
                print("error:", out["error"])
            win = call(ws, 3, "Browser.getWindowForTarget", targetId=page["id"])["result"]
            print("now:", win["bounds"])
    finally:
        ws.close()


if __name__ == "__main__":
    main()
