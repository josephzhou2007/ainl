"""Read-only: what does the dashboard's OPEN WEBASSIGN button actually do?"""
from __future__ import annotations

import json
import re

from playwright.sync_api import sync_playwright

JS = r"""
() => {
  const cands = [...document.querySelectorAll('a,button,[role=button]')]
    .filter(e => /open\s*webassign/i.test(e.innerText || ''));
  const describe = e => {
    const form = e.closest('form');
    return {
      tag: e.tagName, text: (e.innerText || '').trim(), href: e.getAttribute('href'),
      target: e.getAttribute('target'), type: e.getAttribute('type'),
      onclick: (e.getAttribute('onclick') || '').slice(0, 300),
      dataset: {...e.dataset},
      form: form ? {action: form.action, method: form.method, target: form.target,
                    fields: [...form.elements].map(x => x.name).filter(Boolean)} : null,
      outer: e.outerHTML.slice(0, 600),
    };
  };
  const forms = [...document.forms].map(f => ({action: f.action, method: f.method,
                                               target: f.target, n: f.elements.length}));
  return {candidates: cands.map(describe), forms};
}
"""


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        pages = [pg for c in b.contexts for pg in c.pages if not pg.is_closed()]
        for i, pg in enumerate(pages):
            print(f"tab{i}: {re.sub(r'(UserPass|CSRFToken)=[^&]*', r'\1=<redacted>', pg.url)[:110]}")
        dash = next(pg for pg in pages if "cengage.com/dashboard" in pg.url)
        out = dash.evaluate(JS)
        print(json.dumps(out, indent=2, ensure_ascii=False)[:6000])


if __name__ == "__main__":
    main()
