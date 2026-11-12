"""Where does the time go? Run each user-visible operation and keep the spans.

    python scripts/timing_trace.py --sections assignments,questions
    python scripts/timing_trace.py --sections all            # everything but the two below
    python scripts/timing_trace.py --sections telegram       # ONE text + ONE album, then never again
    python scripts/timing_trace.py --sections gemini         # at most 3 model calls, ever

Raw results accumulate in ~/.ainl/timing-trace.json (``--fresh`` starts over);
``scripts/timing_report.py`` turns them into tables.

What it will not do, by construction:
  - submit anything. WebAssign.submit is never imported into a call here, and
    fill/choose are followed by a reload plus a check that the form is back to
    what it was.
  - print or save a secret. Secrets are read from the Windows *User*
    environment inside this process; URLs are cut to host + path before they
    are recorded, and the JSON is scrubbed once more before it is written.
  - start a bot, kill a process, or close the browser.

A Node EPIPE stack trace at exit is an abandoned Playwright driver shutting
down. It is noise: the results are printed BEFORE it, and saved to disk.
"""
from __future__ import annotations

import argparse
import contextvars
import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SECRET_NAMES = ("CANVAS_TOKEN", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
COURSE = "100003"
LTI_ASSIGNMENT = "3152480"            # Canvas id of "7.4 Homework" (from ~/.ainl/lti-map.json)
DEPS = {"40575441": "7.4 Homework", "40575445": "7.8 Homework", "40575437": "Module Quiz 9"}
FILL_DEP, FILL_Q = "40575442", 1      # rule: fill only here
CHOOSE_DEP, CHOOSE_Q = "40575445", 2  # rule: choose only here
CDP = "http://127.0.0.1:9222"
CAPTION = "⏱ timing test — ignore"
ONE = "<math xmlns=\"http://www.w3.org/1998/Math/MathML\"><mn>1</mn></math>"


def load_user_env() -> None:
    """Secrets live in the Windows User environment, not in this shell."""
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        for name in SECRET_NAMES + ("CANVAS_BASE_URL", "AINL_HOME"):
            if os.environ.get(name):
                continue
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if value:
                os.environ[name] = str(value)


load_user_env()

from ainl.config import home                                  # noqa: E402
from ainl.external import webassign as wa_mod                 # noqa: E402
from ainl.external.webassign import WebAssign, WebAssignUnavailable  # noqa: E402
from ainl.util import timing                                  # noqa: E402

OUT = home() / "timing-trace.json"
_SCRUB = re.compile(r"(UserPass|CSRFToken|verifier|key|token|access_token)=[^&\"' ]*", re.I)


# --------------------------------------------------------------------------- #
# results file
# --------------------------------------------------------------------------- #

class Results:
    def __init__(self, fresh: bool):
        self.data = {"meta": {}, "runs": []}
        if OUT.exists() and not fresh:
            try:
                self.data = json.loads(OUT.read_text(encoding="utf-8"))
            except ValueError:
                pass
        self.data.setdefault("meta", {})
        self.data.setdefault("runs", [])

    def add(self, run: dict) -> None:
        run["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.data["runs"].append(run)
        self.save()
        cats = (run.get("trace") or {}).get("by_category") or {}
        shown = " ".join(f"{k}={v:.2f}" for k, v in cats.items() if v >= 0.01)
        print(f"  {run['op']:<24} {run.get('variant', ''):<18} "
              f"{run['wall']:6.2f}s {'ok ' if run['ok'] else 'FAIL'} {shown}"
              f"{'  ' + run['error'] if run.get('error') else ''}", flush=True)

    def count(self, op: str) -> int:
        return sum(1 for r in self.data["runs"] if r["op"] == op)

    def save(self) -> None:
        text = json.dumps(self.data, ensure_ascii=False, indent=1)
        text = _SCRUB.sub(lambda m: m.group(1) + "=<redacted>", text)
        for name in SECRET_NAMES:                  # belt and braces
            value = os.environ.get(name, "")
            if len(value) >= 6 and value in text:
                text = text.replace(value, "<redacted>")
        OUT.write_text(text, encoding="utf-8")


def measured(results: Results, op: str, variant: str, fn, trace_name: str | None = None,
             extra=None) -> tuple[object, dict]:
    """Run fn, keep its wall time and whichever trace it produced."""
    seen = {id(t) for t in timing.traces()}
    t0 = time.monotonic()
    ok, err, value = True, "", None
    try:
        value = fn()
    except Exception as e:                                   # noqa: BLE001
        ok, err = False, f"{type(e).__name__}: {str(e)[:160]}"
    wall = time.monotonic() - t0
    new = [t for t in timing.traces() if id(t) not in seen]
    picked = [t for t in new if trace_name is None or t.name == trace_name]
    run = {"op": op, "variant": variant, "wall": round(wall, 4), "ok": ok, "error": err,
           "trace": picked[-1].to_dict() if picked else None,
           "window": window_state()}
    if extra and ok:
        try:
            run.update(extra(value))
        except Exception:                                    # noqa: BLE001
            traceback.print_exc()
    results.add(run)
    return value, run


# --------------------------------------------------------------------------- #
# the browser window (raw CDP — independent of Playwright)
# --------------------------------------------------------------------------- #

def _browser_call(method: str, **params) -> dict:
    from websockets.sync.client import connect
    ver = json.loads(urllib.request.urlopen(f"{CDP}/json/version", timeout=3).read())
    ws = connect(ver["webSocketDebuggerUrl"], open_timeout=3, max_size=None)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv(timeout=5))
            if msg.get("id") == 1:
                return msg.get("result", {})
    finally:
        ws.close()


def _window():
    targets = json.loads(urllib.request.urlopen(f"{CDP}/json/list", timeout=3).read())
    page = next(t for t in targets if t.get("type") == "page")
    return _browser_call("Browser.getWindowForTarget", targetId=page["id"])


def window_state() -> str:
    try:
        return _window()["bounds"]["windowState"]
    except Exception:                                        # noqa: BLE001
        return "unknown"


def set_window(state: str) -> None:
    win = _window()
    _browser_call("Browser.setWindowBounds", windowId=win["windowId"],
                  bounds={"windowState": state})
    time.sleep(1.5)


# --------------------------------------------------------------------------- #
# Canvas
# --------------------------------------------------------------------------- #

def canvas_source():
    """ApiSource whose every HTTP request becomes a span, pages counted."""
    from ainl.canvas.api import ApiSource
    src = ApiSource.from_env(interactive=False)
    http = src._http
    inner = http._request

    def traced(method, path, **kw):
        label = path.split("?")[0]
        label = label[label.find("/api/"):] if "/api/" in label else label
        with timing.span("canvas.http", "network", method=method, path=label) as s:
            r = inner(method, path, **kw)
            try:
                s.set(status=r.status_code, bytes=len(r.content),
                      server_seconds=round(r.elapsed.total_seconds(), 4))
            except Exception:                                # noqa: BLE001
                pass
            return r

    http._request = traced
    return src


def section_canvas(results: Results, runs: int) -> None:
    print("== canvas")
    src = canvas_source()
    print("  channel:", getattr(src, "_channel", "?"))
    results.data["meta"]["canvas_channel"] = getattr(src, "_channel", "?")
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def traced(name, fn):
        def go():
            with timing.trace(name):
                return fn()
        return go

    def shape(items):
        tr = timing.last_trace()
        pages = [s for s in tr.spans if s.name == "canvas.http"] if tr else []
        return {"items": len(items), "http_requests": len(pages)}

    for _ in range(runs):
        measured(results, "canvas.planner", "full semester (no until)",
                 traced("canvas.planner", lambda: src.planner(start)),
                 "canvas.planner", shape)
    for _ in range(runs):
        measured(results, "canvas.planner", "4-day window (until)",
                 traced("canvas.planner", lambda: src.planner(start, until=start + timedelta(days=5))),
                 "canvas.planner", shape)
    # Canvas answers an IDENTICAL query from a cache (~0.11 s a page instead of
    # ~0.95 s), so repeats above flatter it. Shift the start by a minute per
    # run: same data, different cache key — what a first /todo of the day pays.
    for i in range(runs):
        s0 = start - timedelta(minutes=i + 1 + now.minute)
        measured(results, "canvas.planner", "full semester, cache-busted",
                 traced("canvas.planner", lambda s0=s0: src.planner(s0)),
                 "canvas.planner", shape)
    for i in range(runs):
        s0 = start - timedelta(minutes=i + 1 + now.minute)
        measured(results, "canvas.planner", "4-day window, cache-busted",
                 traced("canvas.planner",
                        lambda s0=s0: src.planner(s0, until=start + timedelta(days=5, minutes=i + 1 + now.minute))),
                 "canvas.planner", shape)
    for _ in range(runs):
        measured(results, "canvas.assignments_index", "MATH222",
                 traced("canvas.assignments_index", lambda: src.assignments_index([COURSE])),
                 "canvas.assignments_index", lambda d: {"items": len(d)})
    src.close()


# --------------------------------------------------------------------------- #
# WebAssign
# --------------------------------------------------------------------------- #

def make_wa(cache_seconds: float = 0):
    src = canvas_source()
    return WebAssign(cache_seconds=cache_seconds,
                     launch_url=lambda: src.sessionless_launch(COURSE, LTI_ASSIGNMENT)), src


def q_shape(out) -> dict:
    title, qs, shots = out
    return {"title": title, "questions": len(qs), "shots": sum(1 for s in shots if s),
            "png_bytes": sum(len(s) for s in shots if s)}


def section_assignments(results: Results, runs: int) -> None:
    print("== assignments (tab already on the list)")
    wa, src = make_wa()
    wa.assignments()                                         # put the tab on the list, untimed
    for _ in range(runs):
        measured(results, "wa.assignments", "already on list", wa.assignments,
                 "wa.assignments", lambda a: {"items": len(a)})
    src.close()


def section_questions(results: Results, runs: int, deps: list[str]) -> list[bytes]:
    """The real flow: /math lists (tab lands on the list), the student taps one."""
    print("== questions, cold, from the list — what a tap on an assignment costs")
    wa, src = make_wa(cache_seconds=0)
    shots: list[bytes] = []
    for i in range(runs):
        for dep in deps:
            # The list call is the /math command itself; here the tab is coming
            # from an assignment page, so it has a navigation to do.
            measured(results, "wa.assignments", "from an assignment page", wa.assignments,
                     "wa.assignments", lambda a: {"items": len(a)})
            out, _ = measured(results, "wa.questions", f"cold {dep}",
                              lambda d=dep: wa.questions(d), "wa.questions", q_shape)
            if out and not shots:
                shots = [s for s in out[2] if s]
    save_shots(shots)

    print("== questions, cold, from ANOTHER assignment page (two navigations)")
    for dep in deps[:2]:
        measured(results, "wa.questions", f"cold-from-other {dep}",
                 lambda d=dep: wa.questions(d), "wa.questions", q_shape)

    print("== questions, page already open (no navigation: extraction + pictures only)")
    for _ in range(3):
        measured(results, "wa.questions", f"same-page {deps[1]}",
                 lambda: wa.questions(deps[1]), "wa.questions", q_shape)

    print("== questions, warm (served from the in-process cache)")
    warm, src2 = make_wa(cache_seconds=900)
    warm.questions(deps[1])                                  # prime, untimed
    for _ in range(3):
        measured(results, "wa.questions", f"warm {deps[1]}",
                 lambda: warm.questions(deps[1]), "wa.questions", q_shape)
    src.close(), src2.close()
    return shots


def save_shots(shots: list[bytes]) -> None:
    d = home() / "timing-shots"
    d.mkdir(exist_ok=True)
    for i, s in enumerate(shots[:8], 1):
        (d / f"q{i}.png").write_bytes(s)


def section_launch(results: Results, runs: int) -> None:
    print("== LTI launch: Canvas HTTP, then the browser follows it")
    wa, src = make_wa()
    for _ in range(runs):
        def http():
            with timing.trace("canvas.sessionless_launch"):
                return src.sessionless_launch(COURSE, LTI_ASSIGNMENT)
        url, _ = measured(results, "canvas.sessionless_launch", "http only", http,
                          "canvas.sessionless_launch", lambda u: {"got_url": bool(u)})
        if not url:
            continue
        measured(results, "wa.open_launch", "browser", lambda u=url: wa.open_launch(u),
                 "wa.launch", lambda dep: {"dep": dep})
    src.close()


def _reload_and_check(wa: WebAssign, names: list[str], before: dict, js: str) -> dict:
    """Undo a fill/choose: reload, then prove the form is what it was."""
    def go(ctx, tab):
        tab.reload(wait_until="domcontentloaded", timeout=20_000)
        wa_mod._wait_ready(tab)
        after = tab.evaluate(js, names)
        if after != before:
            # Chrome can restore form state across a reload. A fresh navigation
            # never does.
            tab.goto(wa_mod.WA_LIST, wait_until="domcontentloaded", timeout=20_000)
            return {"restored": False, "left_via": "navigated to list"}
        return {"restored": True}
    return wa._with_tab(go, budget=25)


def section_fill(results: Results, runs: int) -> None:
    print(f"== fill: dep {FILL_DEP} question {FILL_Q} only; reloaded after every run")
    wa, src = make_wa(cache_seconds=0)
    out, _ = measured(results, "wa.questions", f"cold {FILL_DEP}",
                      lambda: wa.questions(FILL_DEP), "wa.questions", q_shape)
    if not out:
        print("  could not open it; skipping fill")
        return
    q = out[1][FILL_Q - 1]
    boxes = [p.name for p in q.parts if p.is_math][:1]
    if not boxes:
        print("  question 1 has no maths box; skipping fill")
        return
    before = wa._with_tab(lambda ctx, tab: tab.evaluate(wa_mod._READBACK_JS, boxes), budget=12)
    for _ in range(runs):
        measured(results, "wa.fill", "1 box, page already open",
                 lambda: wa.fill(FILL_DEP, {boxes[0]: ONE}), "wa.fill",
                 lambda r: {"missing": len(r[0]["missing"]), "shots": len(r[1])})
        state = _reload_and_check(wa, boxes, before, wa_mod._READBACK_JS)
        print("   form after reload:", state)
        results.data["meta"].setdefault("fill_cleanup", []).append(state)
    src.close()


def section_choose(results: Results, runs: int) -> None:
    print(f"== choose: dep {CHOOSE_DEP} question {CHOOSE_Q} only; reloaded after every run")
    wa, src = make_wa(cache_seconds=0)
    out, _ = measured(results, "wa.questions", f"cold {CHOOSE_DEP}",
                      lambda: wa.questions(CHOOSE_DEP), "wa.questions", q_shape)
    if not out:
        print("  could not open it; skipping choose")
        return
    q = out[1][CHOOSE_Q - 1]
    part = next((p for p in q.parts if p.kind == "choice" and p.options), None)
    if part is None:
        print("  question 2 has no radio part; skipping choose")
        return
    names = [part.name]
    before = wa._with_tab(lambda ctx, tab: tab.evaluate(wa_mod._CHOICE_READBACK_JS, names),
                          budget=12)
    for _ in range(runs):
        measured(results, "wa.choose", "1 radio, page already open",
                 lambda: wa.choose(CHOOSE_DEP, {part.name: part.options[0].value}), "wa.choose",
                 lambda r: {"missing": len(r["missing"])})
        state = _reload_and_check(wa, names, before, wa_mod._CHOICE_READBACK_JS)
        print("   form after reload:", state)
        results.data["meta"].setdefault("choose_cleanup", []).append(state)
    src.close()


def section_minimized(results: Results, runs: int) -> None:
    """The window as it was found: minimised.

    Deliberately NOT through WebAssign.questions(): its 15 s deadline would
    answer a slow capture by restarting the browser, which logs WebAssign out
    and proves nothing about where the time went. The same steps are run here
    on the same worker thread with a 45 s deadline and no restart, and if one
    hangs anyway the wedge evidence is captured while the page is still hung.
    """
    from ainl.external.webassign import Option, Part, Question, shoot_all
    from ainl.util import browserthread
    from ainl.util.timing import span
    print("== questions with the browser window MINIMISED (45 s deadline, no restart)")
    wa, src = make_wa(cache_seconds=0)
    dep = "40575441"

    def inner(ctx, tab):
        wa._open(tab, dep)
        with span("page.title", "ours"):
            title = tab.title().split(" - ")[0]
        with span("extract_questions", "ours"):
            raw = tab.evaluate(wa_mod._QUESTIONS_JS)
        qs = [Question(n, q["dom_id"], q["fields"], [], q.get("stem", "")) for n, q in enumerate(raw, 1)]
        with span("shoot_all", "ours", questions=len(qs)):
            cropped = shoot_all(tab, [q.dom_id for q in qs], wa_mod._HIDE_QUESTION)
        return title, qs, [cropped.get(q.dom_id) for q in qs]

    try:
        for _ in range(runs):
            set_window("normal")
            wa.assignments()
            set_window("minimized")
            time.sleep(3)

            def go():
                with timing.trace("wa.questions") as tr:
                    try:
                        return browserthread.run(
                            lambda: wa._attempt(inner, reload_first=False), budget=45)
                    except browserthread.Timeout:
                        rec = browserthread.capture_wedge(CDP, tr, op="minimised window, 45 s")
                        print("   WEDGE open spans:", [s["name"] for s in rec.get("open_spans", [])])
                        print("   WEDGE stack tail:", rec.get("stuck_stack", [])[-4:])
                        print("   WEDGE probe:", rec.get("probe"))
                        raise

            measured(results, "wa.questions", f"MINIMISED cold {dep}", go, "wa.questions", q_shape)
    finally:
        try:
            set_window("normal")
        except Exception:                                    # noqa: BLE001
            pass
        src.close()


# --------------------------------------------------------------------------- #
# Telegram and Gemini — rationed
# --------------------------------------------------------------------------- #

def section_telegram(results: Results) -> None:
    if results.count("tg.send") or results.count("tg.send_media_group"):
        print("== telegram: already measured once — the ration is one. Skipping.")
        return
    from ainl.telegram.client import TelegramClient
    shots = sorted((home() / "timing-shots").glob("q*.png"))[:8]
    if len(shots) < 8:
        print(f"== telegram: only {len(shots)} saved screenshots; run the questions section first")
        return
    print("== telegram: ONE text, ONE album of 8")
    tg = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"])
    chat = os.environ["TELEGRAM_CHAT_ID"]

    def text():
        with timing.trace("tg.send"):
            with timing.span("sendMessage", "telegram"):
                return tg.send(chat, CAPTION)

    def album():
        images = [(p.read_bytes(), CAPTION if i == 0 else "") for i, p in enumerate(shots)]
        with timing.trace("tg.send_media_group"):
            with timing.span("sendMediaGroup", "telegram", photos=len(images),
                             upload_bytes=sum(len(b) for b, _ in images)):
                return tg.send_media_group(chat, images)

    measured(results, "tg.send", "first request (TLS handshake included)", text, "tg.send")
    measured(results, "tg.send_media_group", "8 PNG, connection reused", album,
             "tg.send_media_group",
             lambda ids: {"messages": len(ids),
                          "upload_bytes": sum(p.stat().st_size for p in shots)})
    tg.close()


def section_gemini(results: Results, images: int) -> None:
    from ainl.mathinput import pipeline
    from ainl.mathinput.pipeline import registry
    used = sum(r.get("model_calls", 0) for r in results.data["runs"] if r["op"] == "llm.transcribe")
    print(f"== gemini: {used} model calls used so far, ceiling 3")
    samples = sorted((ROOT / "photosamples").glob("*.png"))
    calls = {"n": 0}

    def get(name):
        provider = registry.get(name)
        inner = provider.transcribe
        # The pipeline calls providers from a thread pool, and a ContextVar
        # does not follow a pool thread — so carry the trace across by hand.
        ctx = contextvars.copy_context()

        def transcribe(image_bytes, hint=""):
            calls["n"] += 1

            def timed():
                with timing.span(f"model:{name}", "llm", image_bytes=len(image_bytes)):
                    return inner(image_bytes, hint=hint)
            return ctx.run(timed)
        provider.transcribe = transcribe
        return provider

    for path in samples[:images]:
        if used + calls["n"] + 2 > 3 and calls["n"]:
            # One more image could take two calls (cheap + escalation).
            print("  stopping: another image could pass the ceiling of 3")
            break
        data = path.read_bytes()
        start = calls["n"]

        def go():
            with timing.trace("llm.transcribe"):
                return pipeline.transcribe_with_consensus(data, hint="", get=get)

        measured(results, "llm.transcribe", path.name, go, "llm.transcribe",
                 lambda r: {"model_calls": calls["n"] - start, "escalated": r.escalated,
                            "needs_human": r.needs_human, "image_bytes": len(data),
                            "errors": {k: v[:80] for k, v in r.errors.items()}})


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", default="all")
    ap.add_argument("--fresh", action="store_true", help="discard earlier results")
    ap.add_argument("--runs", type=int, default=0, help="override the per-section run count")
    ap.add_argument("--window", choices=["normal", "minimized", "leave"], default="normal",
                    help="window state for the browser sections (default: normal)")
    a = ap.parse_args()
    want = set(a.sections.split(","))
    every = {"canvas", "assignments", "questions", "launch", "fill", "choose"}
    if "all" in want:
        want |= every

    timing.set_detail(True)                                  # Navigation Timing + wedge capture
    results = Results(a.fresh)
    meta = results.data["meta"]
    meta["python"] = sys.version.split()[0]
    meta.setdefault("window_found", window_state())
    print("browser window is:", window_state())
    if a.window != "leave" and want & (every - {"canvas"}):
        set_window(a.window)
        print("browser window set to:", window_state())

    n = a.runs
    try:
        if "canvas" in want:
            section_canvas(results, n or 3)
        if "assignments" in want:
            section_assignments(results, n or 5)
        if "questions" in want:
            section_questions(results, n or 4, list(DEPS))
        if "launch" in want:
            section_launch(results, n or 3)
        if "fill" in want:
            section_fill(results, n or 3)
        if "choose" in want:
            section_choose(results, n or 3)
        if "minimized" in want:
            section_minimized(results, n or 2)
        if "telegram" in want:
            section_telegram(results)
        if "gemini" in want:
            section_gemini(results, n or 2)
    finally:
        results.save()
        print(f"saved {len(results.data['runs'])} runs to {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)          # skip interpreter teardown: that is where the EPIPE noise comes from
