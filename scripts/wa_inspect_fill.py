"""Read-only: how exactly do the page's fill functions want to be called?

Prints the source of setMathTypeValue / syncAnswerValue and the shape of
window.mathTypeEditor on the open assignment tab. Calls none of them.
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

JS = r"""
() => {
  const src = f => (typeof f === 'function' ? f.toString().slice(0, 1500) : String(typeof f));
  const ov = window.mathTypeOverlay || {};
  const ed = window.mathTypeEditor || {};
  const first = document.querySelector('input[name^="RA_"]');
  const wrap = first && first.closest('[id^="question"]')?.querySelector('.mathtype-wrapper');
  return {
    setMathTypeValue: src(window.setMathTypeValue),
    syncAnswerValue: src(ov.syncAnswerValue),
    overlay_methods: Object.keys(ov).concat(Object.keys(Object.getPrototypeOf(ov) || {}))
                       .filter(k => typeof ov[k] === 'function').slice(0, 40),
    editor_keys: Object.keys(ed).slice(0, 40),
    editor_key_types: Object.fromEntries(Object.keys(ed).slice(0, 5).map(k => [k, typeof ed[k]])),
    first_field: first && first.name,
    wrapper: wrap ? {id: wrap.id, cls: wrap.className, data: {...wrap.dataset},
                     html: wrap.outerHTML.slice(0, 700)} : null,
    checkForAndSave: src(window.checkForAndSaveNewMathTypeAnswer).slice(0, 200),
    getCurrentMathML: src(window.getCurrentMathML),
    getAnswerField: src(window.getAnswerField),
    queuePreview: src(ov.queuePreview),
    renderPreview: src(ov.renderPreview),
    initializeBox: src(ov.initializeBox),
    open: src(ov.open),
  };
}
"""


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        pages = [pg for c in b.contexts for pg in c.pages if not pg.is_closed()]
        tab = next((pg for pg in pages if "Assignment-Responses" in pg.url), None)
        if tab is None:
            print("no assignment page open", file=sys.stderr)
            return 2
        print(json.dumps(tab.evaluate(JS), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
