"""The nightly canary: walk every platform up to — never past — the last step.

Vendors change their pages without notice. On 2026-09-26 Gradescope started
asking "Submit Images / Submit PDF" first, the upload form pointed back at
itself until that was opened, and a real upload ended on a 404. A student
found it. This finds it first: each night, on the OWNER's own account, every
flow is opened as far as the step before anything is handed in, and the parts
the bot relies on are checked.

It never submits, uploads, fills or answers anything, never starts a quiz, and
never reads anyone else's account. Checks that need something that isn't there
today (no Gradescope item due) are SKIP, not FAIL.

    .venv/bin/python scripts/canary.py              # report -> ~/.ainl/reports/canary-<time>.md
    .venv/bin/python scripts/canary.py --notify     # + a summary to the owner's chat
Exit code 1 when any check FAILs.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scale-up")]

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
PUBLIC = "https://example.com/native-bot-learning"
OWNER_CDP = "http://127.0.0.1:9222"


class Canary:
    def __init__(self) -> None:
        self.results: list[dict] = []
        self._canvas = None
        self._items = None

    def check(self, name: str):
        """Decorator that RUNS the check at once, in the order written, and
        records ok/warn/fail/skip with a one-line detail."""
        def wrap(fn):
            t0 = time.monotonic()
            try:
                status, detail = fn()
            except Exception as e:                            # a crash is a finding
                status, detail = FAIL, f"{type(e).__name__}: {str(e)[:200]}"
                detail += "\n" + "".join(traceback.format_exc().splitlines(True)[-4:])
            self.results.append({"check": name, "status": status, "detail": detail,
                                 "s": round(time.monotonic() - t0, 1)})
            print(f"[{status.upper():4}] {name}: {detail.splitlines()[0] if detail else ''}", flush=True)
            return fn
        return wrap

    # -- shared -------------------------------------------------------------

    def canvas(self):
        if self._canvas is None:
            from ainl.canvas import channel
            self._canvas = channel.open_source("token")
        return self._canvas

    def upcoming(self) -> list[dict]:
        """Assignments due in the next 14 days, with their external tool URL."""
        if self._items is None:
            c = self.canvas()
            now = datetime.now(timezone.utc)
            items = c.planner(now - timedelta(days=1), until=now + timedelta(days=14))
            wanted: dict[str, list[str]] = {}
            for i in items:
                if i.course_id and getattr(i.kind, "value", "") == "assignment":
                    wanted.setdefault(i.course_id, []).append(i.id)
            meta = c.assignments_meta_for(wanted)
            self._items = [{"course_id": i.course_id, "id": i.id, "title": i.title,
                            "url": i.canonical_url, "tool": meta.get(i.id, {}).get("tool", "")}
                           for i in items if i.id in meta]
        return self._items

    def by_platform(self, key: str) -> list[dict]:
        from ainl.external.platforms import classify
        return [it for it in self.upcoming() if (p := classify(it["tool"])) and p.key == key]


def run() -> list[dict]:
    k = Canary()

    @k.check("services")
    def _():
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        want = ["com.ainl.bot", "com.ainl.public-bot", "com.ainl.browser"]
        rows = {ln.split()[-1]: ln.split()[0] for ln in out.splitlines() if ln.strip()}
        down = [w for w in want if rows.get(w, "-") == "-"]
        tunnel = subprocess.run(["pgrep", "-f", "cloudflared"], capture_output=True).returncode == 0
        if down or not tunnel:
            return FAIL, f"not running: {', '.join(down + ([] if tunnel else ['cloudflared']))}"
        return OK, "bot, public bot, browser, tunnel all running"

    @k.check("public login page")
    def _():
        import httpx
        r = httpx.get(PUBLIC + "/healthz", timeout=15)
        p = httpx.get(PUBLIC + "/login", timeout=15)
        if r.status_code != 200 or p.status_code != 200 or "Canvas login" not in p.text:
            return FAIL, f"healthz {r.status_code}, login {p.status_code}"
        return OK, "healthz and login page answer through the tunnel"

    @k.check("canvas token")
    def _():
        me = k.canvas().whoami()
        return OK, f"token works (user id {me.get('id')}); {len(k.upcoming())} assignment(s) in 14 days"

    @k.check("canvas cookie (owner's browser)")
    def _():
        from ainl.canvas import ApiSource
        from ainl.canvas.session import auth_from_live_browser
        auth = auth_from_live_browser(k.canvas().base_url, OWNER_CDP)
        if auth is None:
            return WARN, "the browser on 9222 holds no Canvas session (the token channel is unaffected)"
        src = ApiSource(k.canvas().base_url, auth)
        try:
            src.whoami()
        finally:
            src.close()
        return OK, "live browser cookies reach the Canvas API"

    @k.check("one-time launch links")
    def _():
        tools = [it for it in k.upcoming() if it["tool"]]
        if not tools:
            return SKIP, "no external-tool assignment due in 14 days"
        got, bad = 0, []
        for it in tools[:6]:
            try:
                k.canvas().sessionless_launch(it["course_id"], it["id"])
                got += 1
            except Exception as e:
                bad.append(f"{urlsplit(it['tool']).hostname}: {type(e).__name__}")
        return (OK if not bad else WARN), f"{got}/{min(len(tools), 6)} ok" + (f"; {', '.join(bad)}" if bad else "")

    @k.check("webassign read")
    def _():
        items = k.by_platform("webassign")
        if not items:
            return SKIP, "no WebAssign assignment due in 14 days"
        from ainl.external.link import dep_for
        from ainl.external.webassign import WebAssign
        it = items[0]
        wa = WebAssign(cdp_url=OWNER_CDP,
                       launch_url=lambda: k.canvas().sessionless_launch(it["course_id"], it["id"]))
        dep = dep_for(k.canvas(), wa, it["course_id"], it["id"], it["title"])
        if not dep:
            return FAIL, f"could not match '{it['title']}' to a WebAssign assignment"
        title, qs, shots = wa.questions(dep)
        kinds = sorted({p.kind for q in qs for p in q.parts})
        odd = [q.n for q in qs if any(p.kind in ("unknown",) for p in q.parts)]
        if not qs:
            return FAIL, f"'{title}': no questions read"
        return (WARN if odd else OK), (f"'{title}': {len(qs)} question(s), parts {', '.join(kinds)}, "
                                       f"{sum(1 for s in shots if s)} picture(s)"
                                       + (f"; unknown parts in {odd}" if odd else ""))

    @k.check("gradescope upload page (dry run)")
    def _():
        items = k.by_platform("gradescope")
        if not items:
            return SKIP, "no Gradescope assignment due in 14 days"
        from ainl.external import gradescope as G
        from ainl.util import pw
        it = items[0]
        with pw.connect(OWNER_CDP) as browser:
            tab = browser.contexts[0].new_page()
            try:
                tab.goto(it["url"], wait_until="domcontentloaded", timeout=45_000)
                tab.wait_for_function(G._FORM_READY, timeout=20_000, polling=300)
                tab.evaluate(G._LAUNCH)
                for _ in range(40):
                    tab.wait_for_timeout(1000)
                    if urlsplit(tab.url).netloc.endswith("gradescope.com"):
                        break
                else:
                    return FAIL, "the launch from Canvas never reached Gradescope"
                path = urlsplit(tab.url).path
                if re.search(r"/submissions/\d+", path):
                    return SKIP, f"'{it['title']}' is already submitted (Gradescope shows the submission)"
                tab.wait_for_selector(G._FILE_INPUT, state="attached", timeout=15_000)
                opener = tab.get_by_role("button", name="Submit PDF")
                if opener.count() and opener.first.is_visible():
                    opener.first.click()
                tab.wait_for_function(G._FORM_POSTS_TO_SUBMISSIONS, arg=G._FILE_INPUT, timeout=10_000)
                up = tab.locator(G._FILE_INPUT).locator("xpath=ancestor::form//*[@type='submit']").first
                if not (up.count() and up.is_visible()):
                    return FAIL, "the Upload PDF button is not there"
                return OK, f"'{it['title']}': Submit PDF opens, form posts to /submissions, Upload PDF visible (stopped there)"
            finally:
                tab.close()

    @k.check("machine")
    def _():
        import shutil
        import psutil
        mem = psutil.virtual_memory()
        disk = shutil.disk_usage(Path.home())
        free_gb = disk.free / 2**30
        status = FAIL if mem.percent > 90 or free_gb < 5 else WARN if mem.percent > 80 or free_gb < 15 else OK
        return status, f"memory {mem.percent:.0f}% used, disk {free_gb:.0f} GB free"

    @k.check("errors in the last 24 h")
    def _():
        from ainl.config import home
        since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        found: dict[str, int] = {}
        for log in ("bot.log.err", "public_bot.log.err"):
            p = home() / log
            if not p.exists():
                continue
            for ln in p.read_text(errors="replace").splitlines():
                if ln[:16] >= since and (" ERROR " in ln or "Traceback" in ln):
                    key = f"{log.split('.')[0]}: " + re.sub(r"^\S+ \S+ ", "", ln)[:110]
                    found[key] = found.get(key, 0) + 1
        if not found:
            return OK, "none"
        top = sorted(found.items(), key=lambda kv: -kv[1])[:8]
        return WARN, f"{sum(found.values())} error line(s)\n" + "\n".join(f"  {n}× {m}" for m, n in top)

    @k.check("failures filed in the last 24 h")
    def _():
        # <AINL_HOME>/failures (ainl.util.trouble): OUR scripts' failures are
        # the ones to fix; Jev's are its trial record, reported apart.
        from ainl.config import home
        idx = home() / "failures" / "index.jsonl"
        if not idx.exists():
            return OK, "none"
        since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        by: dict[str, dict[str, int]] = {"script": {}, "jev": {}}
        for ln in idx.read_text(errors="replace").splitlines():
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if ev.get("time", "") >= since:
                key = f"{ev.get('where') or '?'} — {(ev.get('error') or ev.get('message') or '')[:70]}"
                src = by.setdefault(ev.get("source") or "script", {})
                src[key] = src.get(key, 0) + 1
        script, jev_ = sum(by["script"].values()), sum(by["jev"].values())
        if not script and not jev_:
            return OK, "none"
        lines = [f"script (ours, to fix): {script}; jev (trials, not bugs): {jev_}"]
        for m, n in sorted(by["script"].items(), key=lambda kv: -kv[1])[:8]:
            lines.append(f"  script {n}× {m}")
        for m, n in sorted(by["jev"].items(), key=lambda kv: -kv[1])[:4]:
            lines.append(f"  jev {n}× {m}")
        return (WARN if script else OK), "\n".join(lines)

    if k._canvas is not None:
        k._canvas.close()
    return k.results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--notify", action="store_true", help="send a summary to the owner's chat")
    a = ap.parse_args()
    from ainl.config import home
    results = run()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    d = home() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"canary-{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    icon = {OK: "✅", WARN: "⚠️", FAIL: "❌", SKIP: "➖"}
    md = [f"# Canary {stamp}", ""] + [f"- {icon[r['status']]} **{r['check']}** ({r['s']}s): "
                                      + r["detail"].replace("\n", "\n  ") for r in results]
    path = d / f"canary-{stamp}.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n-> {path}")
    fails = [r for r in results if r["status"] == FAIL]
    if a.notify:
        from ainl.config import TelegramConfig
        from ainl.telegram import TelegramClient
        cfg = TelegramConfig.from_env()
        tg = TelegramClient(cfg.bot_token)
        lines = [f"🌅 <b>Nightly check</b> — " + ("all good" if not fails else f"{len(fails)} problem(s)")]
        lines += [f"{icon[r['status']]} {r['check']}: {r['detail'].splitlines()[0][:120]}" for r in results]
        tg.send(cfg.chat_id, "\n".join(lines))
        tg.close()
    return 1 if fails else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # Skip interpreter teardown: Playwright's Node driver, still attached,
    # otherwise prints an EPIPE trace that reads like a failure.
    import os
    os._exit(code)
