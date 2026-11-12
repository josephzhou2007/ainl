"""Attach, open selected WebAssign assignments in the same tab, dump each one.

Only follows the links already on the "My Assignments" page (they carry the
session), in the same tab, and only for the deps named on the command line.
Quizzes are refused unless --allow-quiz: opening a timed quiz can start its
clock, which cannot be undone.

    python scripts/wa_capture.py 40575442 40575441
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from webassign_probe import _dump, _redact  # noqa: E402

LIST_JS = r"""() => [...document.querySelectorAll('a[href*="dep="]')].map(a => ({
  dep: (a.href.match(/[?&]dep=(\d+)/) || [])[1], href: a.href,
  text: a.innerText.replace(/\s+/g, ' ').trim()})).filter(x => x.dep)"""


# Applied only while the screenshot is taken; the live page is not changed.
# Hides the cookie banner (without answering it) and the per-question chrome
# (Resources, SUBMIT ANSWER, the empty answer box) so the image is just the task.
_SHOT_CSS = """
#onetrust-consent-sdk, #onetrust-banner-sdk { display: none !important; }
.resources, [class*="resource"], button, input[type=submit],
.mathtype-wrapper, [class*="answer-box"] { visibility: hidden !important; }
"""


def _question_shots(tab, dep: str) -> None:
    out = Path.home() / ".ainl" / "webassign" / f"dep{dep}"
    out.mkdir(parents=True, exist_ok=True)
    ids = tab.evaluate(r"""() => [...document.querySelectorAll('[id^="question"]')]
        .map(e => e.id).filter(id => /^question\d+_\d+$/.test(id))""")
    for n, qid in enumerate(ids, 1):
        path = out / f"q{n}.png"
        try:
            tab.locator(f"#{qid}").screenshot(path=str(path), style=_SHOT_CSS, timeout=20_000)
            print(f"  q{n}: {path.name}", flush=True)
        except Exception as e:
            print(f"  q{n}: screenshot failed ({type(e).__name__})", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deps", nargs="+")
    ap.add_argument("--allow-quiz", action="store_true")
    ap.add_argument("--shots", action="store_true",
                    help="also save one image per question (what the bot will send)")
    a = ap.parse_args()

    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        pages = [pg for c in b.contexts for pg in c.pages if not pg.is_closed()]
        tab = next((pg for pg in pages if "webassign.net" in pg.url), None)
        if tab is None:
            print("no WebAssign tab — run scripts/wa_open.py first", file=sys.stderr)
            return 2
        list_url = tab.url
        seen: set[str] = set()
        for dep in a.deps:
            if re.search(rf"[?&]dep={dep}\b", tab.url):       # already on it
                print(f"dep={dep}: already open", flush=True)
                tab.wait_for_timeout(2000)
                _dump(tab, tab.url, seen)
                if a.shots:
                    _question_shots(tab, dep)
                continue
            if "student.pl" not in tab.url:
                tab.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
                tab.wait_for_timeout(3000)
            row = next((r for r in tab.evaluate(LIST_JS) if r["dep"] == dep), None)
            if row is None:
                print(f"dep={dep}: not on the assignment list, skipped")
                continue
            if re.search(r"\bquiz\b", row["text"], re.I) and not a.allow_quiz:
                print(f"dep={dep}: '{row['text'][:50]}' is a quiz — refusing (--allow-quiz)")
                continue
            print(f"dep={dep}: opening '{row['text'][:60]}'", flush=True)
            tab.goto(row["href"], wait_until="domcontentloaded", timeout=90_000)
            tab.wait_for_timeout(7000)                  # WIRIS editors mount late
            print(f"  landed: {_redact(tab.url)[:100]}", flush=True)
            _dump(tab, tab.url, seen)
            if a.shots:
                _question_shots(tab, dep)
        tab.goto(list_url, wait_until="domcontentloaded", timeout=60_000)  # leave it tidy
    return 0


if __name__ == "__main__":
    sys.exit(main())
