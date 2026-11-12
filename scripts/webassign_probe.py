"""Read-only probe of a live WebAssign assignment page.

Opens the same persistent Chromium profile the Canvas reverse-engineering uses,
then waits while a human logs in and navigates. Whenever the tab lands on an
assignment page it dumps the page's structure to ~/.ainl/webassign/ and keeps
watching, so several assignments can be captured in one sitting.

It NEVER clicks, fills, saves or submits. WebAssign is graded work with a
limited number of submissions per part; everything here is observation only.

What it captures, and why each piece is needed:

  problems[]   the question text, per question and part. This is what the bot
               pushes to Telegram so the student can write all five answers on
               one sheet and photograph it once.
  fields[]     the RA_<qid>_<qIdx>_<part>_<respId> hidden inputs — where a
               MathML answer has to land, and in what order. This is the map
               that lets "problem 3" from the OCR reach the right box.
  page_api     whether setMathTypeValue / mathTypeOverlay / the MooTools
               question object are actually present on this page, rather than
               only in our notes (docs/cengage-webassign-api.md sec. 9).

    python scripts/webassign_probe.py                 # opens the window, watches
    python scripts/webassign_probe.py --minutes 30
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ainl.canvas.session import BrowserSession          # noqa: E402
from ainl.config import CanvasConfig, home              # noqa: E402

ANSWER_PAGE = re.compile(r"webassign\.net/web/Student/Assignment-Responses/", re.I)
OUT = home() / "webassign"

# Runs in the page. Pure reads: querySelector + textContent + typeof.
_EXTRACT = r"""
() => {
  const txt = el => (el ? el.innerText.replace(/\s+/g, ' ').trim() : '');

  // Each answer part is a hidden RA_<qid>_<qIdx>_<part>_<respId> input.
  const fields = [...document.querySelectorAll('input[name^="RA_"]')].map(i => {
    const m = i.name.match(/^RA_(\d+)_(\d+)_(\d+)_(\d+)$/) || [];
    return {
      name: i.name, qid: m[1], qIdx: m[2], part: m[3], respId: m[4],
      value_len: (i.value || '').length,
      enabled: !!document.querySelector(`input[name="enabled_${i.name}"]`),
      has_wiris: !!document.querySelector(`#latex-source-${i.name}`),
    };
  });

  // Question blocks are id="question<qid>_<idx>" (MooTools widgets).
  const problems = [...document.querySelectorAll('[id^="question"]')]
    .filter(el => /^question\d+_\d+$/.test(el.id))
    .map(el => ({
      id: el.id,
      text: txt(el.querySelector('.qText, .question-text') || el).slice(0, 2000),
      points: txt(el.querySelector('.points, .qPoints')),
      parts: fields.filter(f => el.id === `question${f.qid}_${f.qIdx}`).map(f => f.name),
      images: [...el.querySelectorAll('img')].map(i => i.src).slice(0, 8),
    }));

  return {
    url: location.href,
    title: document.title,
    deployment: (document.querySelector('input[name="deployment"]') || {}).value || null,
    assignment_name: txt(document.querySelector('.assignmentName, h1')),
    n_fields: fields.length,
    fields, problems,
    page_api: {
      setMathTypeValue: typeof window.setMathTypeValue,
      mathTypeEditor: typeof window.mathTypeEditor,
      mathTypeOverlay: typeof window.mathTypeOverlay,
      syncAnswerValue: typeof (window.mathTypeOverlay || {}).syncAnswerValue,
      mootools_$: typeof window.$,
      // does the MooTools question object exist and expose save/submit?
      question_object: (() => {
        try {
          const el = document.querySelector('[id^="question"]');
          const q = el && window.$ && window.$(el.id).retrieve('question');
          return q ? Object.keys(q).filter(k => typeof q[k] === 'function').slice(0, 20) : null;
        } catch (e) { return 'err: ' + e.message; }
      })(),
    },
  };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30, help="how long to keep watching")
    ap.add_argument("--start", default="https://www.cengage.com/dashboard/home",
                    help="where to open the tab; log in there, then OPEN WEBASSIGN")
    ap.add_argument("--drive", action="store_true",
                    help="after login, walk the assignment list by URL and dump each one")
    ap.add_argument("--attach", action="store_true",
                    help="connect to the browser started by browser_start.py, read, disconnect. "
                         "Never opens or closes a window, so re-running costs no logins.")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--chromium", action="store_true",
                    help="use Playwright's bundled Chromium instead of installed Chrome. "
                         "Chrome hands new windows off to an already-running instance, "
                         "which silently takes them out of our control.")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if a.attach:
        return _attach(a)
    sess = BrowserSession(CanvasConfig.from_env().base_url)
    sess._refuse_if_profile_in_use()

    from playwright.sync_api import sync_playwright

    seen: set[str] = set()
    deadline = time.time() + a.minutes * 60
    print(f"opening a browser window. log in to WebAssign and open a MATH222 assignment.\n"
          f"I will not click, fill or submit anything — only read the page.\n"
          f"dumps -> {OUT}\nwatching for {a.minutes:g} min (Ctrl+C to stop)\n", flush=True)

    with sync_playwright() as p:
        launch = {"headless": False, "args": sess._CHROME_ARGS, "no_viewport": True}
        if a.chromium:
            ctx = p.chromium.launch_persistent_context(str(sess.profile_dir), **launch)
        else:
            try:
                ctx = p.chromium.launch_persistent_context(str(sess.profile_dir),
                                                           channel="chrome", **launch)
            except Exception:
                ctx = p.chromium.launch_persistent_context(str(sess.profile_dir), **launch)

        # A popup (OPEN WEBASSIGN is one) joins this context — say so, so a tab
        # appearing in silence is distinguishable from one we lost.
        ctx.on("page", lambda pg: print(f"  + new tab: {_redact(pg.url)[:90]}", flush=True))

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(a.start, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"(initial navigation failed, navigate yourself: {e})", flush=True)

        # Scan *every* tab, not just the newest: an LTI launch or an OAuth hop
        # can land the assignment in a tab that is not the one we opened, and a
        # stale page object keeps reporting its old URL forever.
        last_seen: dict[str, str] = {}
        tick = 0
        driven = False
        while time.time() < deadline:
            tick += 1
            try:
                pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if not pages:
                    print("all tabs closed — stopping.", flush=True)
                    break
                urls = []
                for pg in pages:
                    try:
                        url = pg.url
                    except Exception:
                        continue
                    urls.append(url)
                    key = str(id(pg))
                    if last_seen.get(key) != url:
                        print(f"  [{datetime.now():%H:%M:%S}] tab{pages.index(pg)}: {_redact(url)[:100]}",
                              flush=True)
                        last_seen[key] = url
                    if ANSWER_PAGE.search(url):
                        _dump(pg, url, seen)
                # Retry the walk until the assignment list is actually open: at
                # startup only the dashboard exists, so a single attempt at t=0
                # always finds nothing.
                if a.drive and not driven and tick % 5 == 0:
                    driven = _drive(ctx, page, seen)
            except Exception as e:                       # navigation mid-read, etc.
                print(f"  (skip: {type(e).__name__}: {str(e)[:80]})", flush=True)
            # Heartbeat lives OUTSIDE the try: if a Playwright call starts
            # blocking, the silence has to be visible rather than look like a
            # quiet loop. The last run wedged with no output at all.
            if tick % 5 == 0:
                mins = int((deadline - time.time()) / 60)
                print(f"  … tick {tick}, {mins} min left, {len(seen)} captured", flush=True)
            time.sleep(3)

        print("done watching. the window stays open; close it yourself.", flush=True)
        try:
            ctx.close()
        except Exception:
            pass
    return 0


WA_HOME = "https://www.webassign.net/web/Student/Assignment-Responses/last"
_DEP_RE = re.compile(r"[?&]dep=(\d+)")

# Read the assignment list without clicking: every row links to ?dep=<id>.
_LIST = r"""
() => [...document.querySelectorAll('a[href*="dep="]')].map(a => ({
  dep: (a.href.match(/[?&]dep=(\d+)/) || [])[1],
  text: a.innerText.replace(/\s+/g, ' ').trim().slice(0, 120),
  href: a.href,
})).filter(x => x.dep)
"""


def _attach(a) -> int:
    """One-shot read against an already-open window. Opens nothing, closes nothing."""
    from playwright.sync_api import sync_playwright

    seen: set[str] = set()
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{a.port}")
        except Exception as e:
            print(f"no browser on port {a.port} — start scripts/browser_start.py first. "
                  f"({type(e).__name__}: {str(e)[:100]})", file=sys.stderr)
            return 2
        pages = [pg for c in browser.contexts for pg in c.pages if not pg.is_closed()]
        print(f"attached: {len(pages)} tab(s)", flush=True)
        for i, pg in enumerate(pages):
            try:
                url = pg.url
            except Exception:
                continue
            mark = " <- answer page" if ANSWER_PAGE.search(url) else ""
            print(f"  tab{i}: {_redact(url)[:110]}{mark}", flush=True)
        for pg in pages:
            try:
                if ANSWER_PAGE.search(pg.url):
                    _dump(pg, pg.url, seen)
            except Exception as e:
                print(f"  (skip: {type(e).__name__}: {str(e)[:80]})", flush=True)
        if a.drive:
            _drive_attached(browser, seen)
        browser.close()          # closes the CDP connection, not the window
    return 0


def _drive_attached(browser, seen: set[str]) -> None:
    pages = [pg for c in browser.contexts for pg in c.pages if not pg.is_closed()]
    if not pages:
        return
    ctx = pages[0].context
    _drive(ctx, pages[-1], seen)


def _redact(url: str) -> str:
    """student.pl carries UserPass=<session token> in the query string.

    It is a live credential, so it must not reach the console, the dumps or the
    docs — the whole point of doing this read-only is not to leave secrets lying
    around afterwards.
    """
    return re.sub(r"(UserPass|CSRFToken|key)=[^&]*", r"\1=<redacted>", url)


def _drive(ctx, page, seen: set[str]) -> bool:
    """Visit each assignment by its deep link.

    Navigation only — no element is ever clicked. WebAssign's submit and save
    controls live in the DOM next to the answer boxes, and a stray click on a
    graded assignment cannot be undone; going by URL removes that whole class
    of accident. It also survives the page being re-rendered by React.
    """
    print("\n-- drive: looking for assignment links in every open tab", flush=True)
    deps: dict[str, str] = {}
    for pg in [p for p in ctx.pages if not p.is_closed()]:
        try:
            url = pg.url
        except Exception:
            continue
        if "auth.cengage.com" in url or "/login" in url.lower():
            print(f"   (a tab is still on the login page: {_redact(url)[:80]})", flush=True)
            continue
        try:
            rows = pg.evaluate(_LIST)
        except Exception:
            continue
        for r in rows:
            deps.setdefault(r["dep"], r["text"])
        here = _DEP_RE.search(url)
        if here:
            deps.setdefault(here.group(1), "(already open)")
        if rows:
            print(f"   {len(rows)} link(s) on {_redact(url)[:70]}", flush=True)
            page = pg                       # drive from the tab that has the list

    if not deps:
        print("   no ?dep= links in any open tab.\n"
              "   open the assignment list (My Assignments) and I'll pick it up.", flush=True)
        return False

    print(f"   {len(deps)} assignment(s) found:", flush=True)
    for dep, text in deps.items():
        print(f"     dep={dep}  {text}", flush=True)

    for dep, text in deps.items():
        try:
            page.goto(f"{WA_HOME}?dep={dep}", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(5000)          # WIRIS editors mount late
            _dump(page, page.url, seen)
        except Exception as e:
            print(f"   dep={dep} failed: {str(e)[:100]}", flush=True)
    print("-- drive: done\n", flush=True)
    return True


def _dump(page, url: str, seen: set[str]) -> None:
    try:
        data = page.evaluate(_EXTRACT)
    except Exception as e:
        print(f"  (extract failed: {str(e)[:80]})", flush=True)
        return
    if not data.get("n_fields"):
        return                                   # page still loading, or not an answer page
    key = f"{data.get('deployment')}:{data['n_fields']}"
    if key in seen:
        return
    seen.add(key)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dep = data.get("deployment") or "unknown"
    base = OUT / f"dep{dep}-{stamp}"
    data["captured_at"] = stamp
    base.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    print(f"  ✓ captured dep={dep}: {len(data['problems'])} questions, "
          f"{data['n_fields']} answer fields -> {base.name}.json", flush=True)
    api = data["page_api"]
    print(f"    page API: setMathTypeValue={api['setMathTypeValue']} "
          f"mathTypeOverlay={api['mathTypeOverlay']} syncAnswerValue={api['syncAnswerValue']}",
          flush=True)


if __name__ == "__main__":
    sys.exit(main())
