"""A watchdog around browser work: give it a deadline, or give up on it.

Measuring a step is not the same as bounding it. util.timing records that a
call took 110 s; it cannot stop it, and the bot polls on one thread, so those
110 s are the whole product frozen.

Python cannot kill a thread, and a wedged Playwright call will not return. So
browser work runs on a dedicated worker thread that owns the Playwright driver,
and the caller waits only for the budget. On overrun the caller gives up, the
worker is abandoned (left to die with the process), and the next call starts a
fresh thread with a fresh driver — the one thing that reliably clears a wedged
connection.

    run(lambda: wa_stuff(), budget=20)   # raises Timeout after 20s

One worker per KEY. The single-user bot never names one, so it gets the
default worker and behaves exactly as before. Serving several people at once
means one worker — one thread, one driver — per person, so they neither queue
behind each other nor share a connection: pw's connection cache is per
thread, so a thread that only ever serves one browser can only ever hand
back that browser. The key is carried by a context variable rather than a
parameter, so code in the middle (WebAssign, the session helpers) needs no
change: whatever runs inside ``with using(key):`` lands on that key's worker.
"""
from __future__ import annotations

import contextvars
import logging
import os
import queue
import sys
import threading
import traceback
from typing import Callable, Optional, TypeVar

from .timing import span

log = logging.getLogger("ainl.browserthread")

T = TypeVar("T")


class Timeout(RuntimeError):
    """The operation outlived its budget and was abandoned."""


class _Worker:
    """One thread, one Playwright driver, jobs one at a time."""

    _n = 0

    def __init__(self, key: Optional[str] = None) -> None:
        _Worker._n += 1
        self.key = key
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.dead = False
        self.glet = None               # the worker's own greenlet, for stuck_stack()
        name = f"browser-{_Worker._n}" if key is None else f"browser-{key}-{_Worker._n}"
        self.thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        from . import pw

        try:
            import greenlet
            self.glet = greenlet.getcurrent()
        except Exception:
            self.glet = None
        while True:
            fn, out = self.jobs.get()
            if fn is None:
                pw.stop()
                return
            try:
                out.put(("ok", fn()))
            except BaseException as e:            # noqa: BLE001 — relayed to caller
                out.put(("err", e))


_lock = threading.Lock()                 # guards _workers only — never held during a job
_workers: dict[Optional[str], _Worker] = {}
_last_abandoned: Optional[_Worker] = None

# Which worker the current code path belongs to. None = the default, single-
# user worker. Set with using(); read by run() when no key is passed.
_current: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "browserthread.key", default=None)


class using:
    """``with using("alice"):`` — every run() inside lands on alice's worker.

    A context manager rather than a thread-local so that it also survives
    the hop onto the worker thread (run() copies the context), which is what
    lets WebAssign's nested run() calls stay on the same person's worker.
    """

    def __init__(self, key: Optional[str]) -> None:
        self.key = key
        self._token = None

    def __enter__(self):
        self._token = _current.set(self.key)
        return self

    def __exit__(self, *exc):
        _current.reset(self._token)
        return False


def current_key() -> Optional[str]:
    return _current.get()


def _get(key: Optional[str]) -> _Worker:
    with _lock:
        w = _workers.get(key)
        if w is None or w.dead or not w.thread.is_alive():
            w = _workers[key] = _Worker(key)
            log.info("browser worker %s started", w.thread.name)
        return w


def run(fn: Callable[[], T], budget: float = 20.0, key: Optional[str] = None) -> T:
    """Run fn on a browser thread; raise Timeout if it outlives the budget.

    ``key`` picks the worker; left out, it is whatever ``using()`` set, or the
    default worker. The lock is NOT held while waiting: one person's slow
    page must not queue everyone else's work behind it.
    """
    global _last_abandoned
    key = _current.get() if key is None else key
    with span("browserthread.run", "ours", budget=budget):
        worker = _get(key)
        if threading.current_thread() is worker.thread:
            # Already on this key's worker (a job calling run() on itself).
            # Queueing would wait on the job that is waiting on it — forever
            # before the deadline, Timeout after. Just do it here.
            return fn()
        out: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
        # The caller's trace rides along: spans opened on the worker land in
        # the operation the caller is timing. Copied INSIDE the span above, so
        # they nest under it and its own time is pure hand-off.
        ctx = contextvars.copy_context()
        worker.jobs.put((lambda: ctx.run(fn), out))
        try:
            status, value = out.get(timeout=budget)
        except queue.Empty:
            # The thread is still stuck in there. Abandon it: a new one next
            # time gets a new driver, which is what actually unwedges things.
            worker.dead = True
            _last_abandoned = worker
            log.warning("browser op exceeded %.0fs — abandoning worker %s",
                        budget, worker.thread.name)
            raise Timeout(f"gave up after {budget:.0f}s") from None
    if status == "err":
        raise value
    return value


def reset(key: Optional[str] = None, *, all_keys: bool = False) -> None:
    """Drop a worker (this code path's by default); the next call starts a
    fresh driver. ``all_keys`` drops every worker, for a clean shutdown."""
    with _lock:
        keys = list(_workers) if all_keys else [_current.get() if key is None else key]
        for k in keys:
            w = _workers.pop(k, None)
            if w is not None:
                w.dead = True


def stuck_stack(limit: int = 12) -> list[str]:
    """Where the most recently abandoned worker is standing, innermost last.

    A wedged Playwright call never returns, so nothing it would have logged
    ever appears. Its Python stack is still there to be read, though, and it
    names the exact call that hung — no guessing from which span was open.
    """
    w = _last_abandoned
    if w is None or not w.thread.is_alive():
        return []
    # Sync Playwright parks the calling code on a greenlet and runs its event
    # loop on another, so the THREAD's current frame is only the loop idling.
    # The call that hung is on the worker's own greenlet, suspended.
    frame = None
    try:
        frame = w.glet.gr_frame if w.glet is not None else None
    except Exception:
        frame = None
    if frame is None:
        frame = sys._current_frames().get(w.thread.ident)
    if frame is None:
        return []
    rows = traceback.extract_stack(frame)[-limit:]
    return [f"{r.filename.replace(chr(92), '/').split('/')[-1]}:{r.lineno} {r.name}: "
            f"{(r.line or '').strip()[:100]}" for r in rows]


# --------------------------------------------------------------------------- #
# wedge evidence
# --------------------------------------------------------------------------- #
#
# About one cold open in four never comes back. The watchdog's answer is to
# restart the browser, which also destroys every clue about WHAT hung. This
# gathers the clues first, over channels that do not pass through the wedged
# Playwright driver at all: the debugging port's HTTP endpoint and a raw
# WebSocket of our own to the page. If those answer while the driver does not,
# the driver is the patient; if they do not answer either, the renderer is.

def wedge_capture_enabled() -> bool:
    from . import timing
    flag = os.environ.get("AINL_WEDGE_CAPTURE", "")
    return flag not in ("", "0") or (flag == "" and timing.detail())


def _host_path(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except ValueError:
        return url.split("?")[0]


def _probe_page(ws_url: str, budget: float = 4.0) -> dict:
    """Ask the page five things at once and see which ones answer in time."""
    import json
    import time

    from websockets.sync.client import connect

    asks = {
        # answered by the browser process, without asking the renderer anything
        1: ("browser_side:Page.getNavigationHistory", "Page.getNavigationHistory", {}),
        2: ("evaluate:1+1", "Runtime.evaluate", {"expression": "1+1", "returnByValue": True}),
        3: ("evaluate:readyState", "Runtime.evaluate",
            {"expression": "document.readyState + '|fonts=' + document.fonts.status + '|questions=' + "
                           "document.querySelectorAll('[id^=question]').length + '|visibility=' + "
                           "document.visibilityState", "returnByValue": True}),
        4: ("Page.getLayoutMetrics", "Page.getLayoutMetrics", {}),
        5: ("Page.captureScreenshot", "Page.captureScreenshot", {"format": "jpeg", "quality": 10}),
    }
    out: dict = {label: {"ok": False, "ms": None, "value": "no answer"} for label, _, _ in asks.values()}
    t0 = time.monotonic()
    try:
        ws = connect(ws_url, open_timeout=2, max_size=None)
    except Exception as e:
        return {"websocket": f"could not open: {type(e).__name__}: {str(e)[:80]}"}
    out["websocket_open_ms"] = round((time.monotonic() - t0) * 1000)
    try:
        t1 = time.monotonic()
        for i, (_, method, params) in asks.items():
            ws.send(json.dumps({"id": i, "method": method, "params": params}))
        pending = set(asks)
        while pending and time.monotonic() - t1 < budget:
            try:
                msg = json.loads(ws.recv(timeout=max(0.05, budget - (time.monotonic() - t1))))
            except TimeoutError:
                break
            i = msg.get("id")
            if i not in pending:
                continue
            pending.discard(i)
            label = asks[i][0]
            ms = round((time.monotonic() - t1) * 1000)
            if "error" in msg:
                out[label] = {"ok": False, "ms": ms, "value": str(msg["error"].get("message"))[:120]}
                continue
            res = msg.get("result", {})
            if i == 1:
                entries = res.get("entries", [])
                cur = res.get("currentIndex", -1)
                value = {"entries": len(entries),
                         "current": _host_path(entries[cur].get("url", "")) if 0 <= cur < len(entries) else None}
            elif i in (2, 3):
                value = res.get("result", {}).get("value")
            elif i == 4:
                cs = res.get("cssContentSize") or res.get("contentSize") or {}
                vv = res.get("cssVisualViewport") or res.get("visualViewport") or {}
                value = {"content": [cs.get("width"), cs.get("height")],
                         "viewport": [vv.get("clientWidth"), vv.get("clientHeight")],
                         "scrollY": vv.get("pageY")}
            else:
                value = f"{len(res.get('data', ''))} b64 chars"
            out[label] = {"ok": True, "ms": ms, "value": value}
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return out


def capture_wedge(cdp_url: str, trace=None, op: str = "") -> dict:
    """Everything knowable about a hang, written down before the restart.

    Bounded at about six seconds, never raises, and appends one JSON line to
    ~/.ainl/wedge-captures.jsonl. URLs are cut to scheme://host/path: the
    query string on these pages is a credential.
    """
    import json
    import time
    import urllib.request
    from datetime import datetime, timezone

    rec: dict = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "op": op}
    try:
        if trace is not None:
            rec["trace"] = trace.name
            rec["elapsed"] = round(trace.elapsed(), 2)
            rec["open_spans"] = [{"name": s.name, "cat": s.cat, "open_for": round(trace.elapsed() - s.start, 2),
                                  "thread": s.thread, "meta": s.meta} for s in trace.open_spans()]
            done = [s for s in trace.spans if s.seconds is not None]
            rec["last_closed_spans"] = [{"name": s.name, "seconds": round(s.seconds, 3)} for s in done[-6:]]
        rec["stuck_stack"] = stuck_stack(40)

        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/list", timeout=2) as r:
                targets = json.loads(r.read().decode("utf-8"))
            rec["http_json_list_ms"] = round((time.monotonic() - t0) * 1000)
        except Exception as e:
            rec["http_json_list"] = f"no answer: {type(e).__name__}: {str(e)[:80]}"
            targets = []
        pages = [t for t in targets if t.get("type") == "page"]
        rec["tabs"] = [{"url": _host_path(t.get("url", "")), "title": (t.get("title") or "")[:60]}
                       for t in pages]
        page = next((t for t in pages if "webassign.net" in t.get("url", "")), None) \
            or (pages[0] if pages else None)
        if page is not None and page.get("webSocketDebuggerUrl"):
            rec["probed"] = _host_path(page.get("url", ""))
            rec["probe"] = _probe_page(page["webSocketDebuggerUrl"])
        else:
            rec["probe"] = "no page target to probe"
    except Exception as e:                        # evidence is optional; recovery is not
        rec["capture_error"] = f"{type(e).__name__}: {str(e)[:120]}"
    try:
        from ..config import home
        with open(home() / "wedge-captures.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
    except Exception:
        pass
    if trace is not None:
        try:
            trace.note("wedge", rec)
        except Exception:
            pass
    log.warning("wedge captured: open=%s probe=%s",
                [s["name"] for s in rec.get("open_spans", [])][-1:],
                {k: v.get("ok") for k, v in rec["probe"].items() if isinstance(v, dict)}
                if isinstance(rec.get("probe"), dict) else rec.get("probe"))
    return rec
