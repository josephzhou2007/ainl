"""What kinds of question does this course actually ask?

Walks every assignment on My Assignments and classifies each question by the
input elements it contains, because that is what decides how the student can
answer it in chat:

    choice      radio buttons      -> tap a button, nothing to write
    multi       checkboxes         -> tap several
    dropdown    <select>           -> tap a button
    math        RA_ + WIRIS editor -> handwriting -> LaTeX -> MathML (the path we have)
    text        plain text input   -> type it
    essay       <textarea>         -> type it
    file        file input         -> upload

Reads only: no clicking, no filling, no submitting.

    python scripts/wa_survey.py                 # everything
    python scripts/wa_survey.py --deps 1 2 3    # only these
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ainl.config import home  # noqa: E402
from ainl.external.webassign import WA_LIST, WebAssign, _wait_ready  # noqa: E402
from ainl.util import pw  # noqa: E402

# Classify by what the student would have to do, not by WebAssign's own markup.
SURVEY_JS = r"""() => {
  const qs = [...document.querySelectorAll('[id^="question"]')]
    .filter(e => /^question\d+_\d+$/.test(e.id));
  return {
    url: location.href,
    title: document.title,
    questions: qs.map(el => {
      const radios = [...el.querySelectorAll('input[type=radio]')];
      const checks = [...el.querySelectorAll('input[type=checkbox]')];
      const selects = [...el.querySelectorAll('select')];
      const ra = [...el.querySelectorAll('input[name^="RA_"]')];
      const wiris = [...el.querySelectorAll('.mathtype-wrapper, [id^="editable-math-"]')];
      const texts = [...el.querySelectorAll('input[type=text]')];
      const areas = [...el.querySelectorAll('textarea')].filter(t => !/latex-source/.test(t.id));
      const files = [...el.querySelectorAll('input[type=file]')];
      // radio groups: one group = one question part
      const groups = {};
      radios.forEach(r => { (groups[r.name] = groups[r.name] || []).push(r); });
      const kinds = [];
      if (Object.keys(groups).length) kinds.push('choice');
      if (checks.length) kinds.push('multi');
      if (selects.length) kinds.push('dropdown');
      if (wiris.length) kinds.push('math');
      else if (ra.length) kinds.push('ra_no_editor');
      if (texts.length) kinds.push('text');
      if (areas.length) kinds.push('essay');
      if (files.length) kinds.push('file');
      const label = r => {
        const l = r.closest('label') || document.querySelector(`label[for="${r.id}"]`);
        return (l ? l.innerText : (r.parentElement ? r.parentElement.innerText : ''))
                 .replace(/\s+/g, ' ').trim().slice(0, 60);
      };
      return {
        id: el.id,
        kinds: kinds.length ? kinds : ['unknown'],
        n_radio_groups: Object.keys(groups).length,
        radio_options: Object.values(groups).map(g => g.map(label)).slice(0, 2),
        n_checks: checks.length, n_selects: selects.length,
        n_ra: ra.length, n_text: texts.length, n_essay: areas.length,
        stem: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 110),
      };
    }),
  };
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deps", nargs="*", help="only these deployment ids")
    ap.add_argument("--out", default=str(home() / "wa-survey.json"))
    a = ap.parse_args()

    wa = WebAssign()
    items = wa.assignments(include_quizzes=True)
    if a.deps:
        items = [i for i in items if i.dep in a.deps]
    print(f"{len(items)} assignments to survey\n", flush=True)

    results = []
    with pw.connect() as browser:
        ctx = browser.contexts[0]
        pages = [p for p in ctx.pages if not p.is_closed()]
        tab = next((p for p in pages if "webassign.net" in p.url), None) or ctx.new_page()
        for it in items:
            t0 = time.time()
            try:
                tab.goto(it.href or f"{WA_LIST}", wait_until="domcontentloaded", timeout=25_000)
                _wait_ready(tab)
                tab.wait_for_timeout(1200)
                data = tab.evaluate(SURVEY_JS)
            except Exception as e:
                print(f"  {it.title[:38]:<40} FAILED {type(e).__name__}", flush=True)
                continue
            data["dep"], data["assignment"] = it.dep, it.title
            results.append(data)
            kinds: dict[str, int] = {}
            for q in data["questions"]:
                for k in q["kinds"]:
                    kinds[k] = kinds.get(k, 0) + 1
            summary = ", ".join(f"{k}x{v}" for k, v in sorted(kinds.items()))
            print(f"  {it.title[:38]:<40} {len(data['questions']):>2}q  {summary}"
                  f"   ({time.time()-t0:.1f}s)", flush=True)

    Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nfull detail -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
