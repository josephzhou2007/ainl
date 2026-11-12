"""Can Jev find the button we need? Ten pages, one right answer each.

    python scripts/jev_trial.py                 # all cases once (needs TYPESAFE_API_KEY)
    python scripts/jev_trial.py --repeat 3      # each case three times
    python scripts/jev_trial.py --jev-reader ~/jev-ultrafast/jev_ultrafast/snapshot.js
    python scripts/jev_trial.py --notify        # summary to the owner's chat

The pages are tests/jev_cases/*.html, built from what the code knows about
WebAssign, Gradescope and Canvas; six are "unknown paths" our scripts have no
rule for (a submit with no id, a file in a closed section, a button inside the
LTI frame…). The right element carries data-truth, which is never sent.

Per case: our reader lists every candidate (did it find the right one?); with
--jev-reader, jev-ultrafast's own reader too (would Jev alone have seen it?);
then Jev picks one (right? how sure?). Nothing is clicked: these are local
pages, served under https://fixtures.test/ by Playwright itself.

Every miss is filed under <AINL_HOME>/failures/jev/ — kept apart from our
scripts' own failures, which the 4 AM review fixes; these measure Jev.
Report: <AINL_HOME>/jev/trials/<time>.md (+ .jsonl).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ainl import jev  # noqa: E402
from ainl.config import home  # noqa: E402
from ainl.util import trouble  # noqa: E402

CASES = ROOT / "tests" / "jev_cases"
ORIGIN = "https://fixtures.test/"

_SEEN_BY_JEV_READER = """(nodes) => nodes.some(n => {
  const e = window.__jevFast && window.__jevFast.nodes.get(n);
  return !!(e && e.hasAttribute('data-truth'));
})"""


def load_cases() -> list[dict]:
    return json.loads((CASES / "cases.json").read_text(encoding="utf-8"))


def serve(route) -> None:
    name = route.request.url[len(ORIGIN):].split("?")[0] or "index.html"
    p = CASES / name
    if p.is_file() and p.suffix == ".html":
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=p.read_text(encoding="utf-8"))
    else:
        route.fulfill(status=404, body="")


def run_case(page, case: dict, reader_js: str | None, ask: bool) -> dict:
    page.goto(ORIGIN + case["file"])
    page.wait_for_load_state("load")
    cands = jev.candidates(page)
    truth = [c["index"] for c in cands if c.get("truth")]
    row = {"case": case["id"], "known": case["known"], "goal": case["goal"],
           "candidates": len(cands), "ours_found": bool(truth), "truth": truth,
           "truth_label": next((c["label"] for c in cands if c.get("truth")), "")}
    if reader_js:
        try:
            state = page.evaluate(reader_js)
            nodes = [a["node"] for a in (state or {}).get("actions", []) if isinstance(a.get("node"), int)]
            row["jev_reader_sees"] = bool(page.evaluate(_SEEN_BY_JEV_READER, nodes))
            row["jev_reader_candidates"] = len(nodes)
        except Exception as e:
            row["jev_reader_sees"] = None
            row["jev_reader_error"] = f"{type(e).__name__}: {e}"[:200]
    if not ask:
        return row
    info = {"url": page.url, "title": page.title(), "text": page.evaluate("() => document.body.innerText")}
    try:
        got = jev.choose(case["goal"], cands, info)
    except jev.JevError as e:
        if "HTTP 401" in str(e) or "HTTP 403" in str(e):
            # A key problem, not a miss: nothing Jev did can be measured.
            raise SystemExit(f"Jev refused the key ({e}). TYPESAFE_API_KEY must come from "
                             "https://console.typesafe.ai/keys — nothing was filed.")
        row.update(ok=False, error=str(e))
        return row
    pick = next((c for c in cands if c["index"] == got["index"]), {})
    row.update(chose=got["index"], chose_label=pick.get("label", ""), chose_context=pick.get("context", ""),
               correct=got["index"] in truth, confidence=round(got["confidence"], 3),
               probability=round(got["probability"], 3), sure=got["sure"],
               latency_ms=got["latency_ms"], usage=got["usage"], model=got["model"],
               # what it weighed: the top three
               top=sorted(((k, round(v, 3)) for k, v in got["probabilities"].items()),
                          key=lambda kv: -kv[1])[:3])
    # Right AND sure is the only outcome we could act on.
    row["ok"] = row["correct"] and row["sure"]
    return row


def file_miss(page, row: dict, cands_json: str) -> None:
    why = ("wrong element" if row.get("correct") is False else
           "right but unsure" if row.get("correct") else row.get("error", "no answer"))
    trouble.report("jev", chat="trial", where=f"trial:{row['case']}",
                   message=f"⚠️ Jev trial {row['case']}: {why}", notify=False, background=False,
                   files={"page.png": page.screenshot(full_page=True), "page.html": page.content(),
                          "candidates.json": cands_json},
                   extra={k: v for k, v in row.items() if k != "goal"} | {"goal": row["goal"]})


def report(rows: list[dict], asked: bool) -> tuple[Path, str]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")      # two runs in one minute: two reports
    d = home() / "jev" / "trials"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stamp}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                      encoding="utf-8")
    n = len(rows)
    ours = sum(r["ours_found"] for r in rows)
    lines = [f"# Jev trial {stamp}", "",
             f"- our reader found the right element: **{ours}/{n}**"]
    seen = [r for r in rows if r.get("jev_reader_sees") is not None]
    if seen:
        lines.append(f"- jev-ultrafast's own reader could see it: **{sum(r['jev_reader_sees'] for r in seen)}/{len(seen)}**")
    if asked:
        right = sum(bool(r.get("correct")) for r in rows)
        ok = sum(bool(r.get("ok")) for r in rows)
        lat = sorted(r["latency_ms"] for r in rows if "latency_ms" in r)
        lines += [f"- Jev chose the right element: **{right}/{n}**",
                  f"- right AND sure (≥ {jev.min_confidence():.2f}) — usable: **{ok}/{n}**",
                  f"- wrong but sure (the dangerous kind): **{sum(1 for r in rows if r.get('correct') is False and r.get('sure'))}**"]
        if lat:
            lines.append(f"- latency median {lat[len(lat) // 2]} ms, max {lat[-1]} ms")
        for part, pick in (("known paths", True), ("unknown paths", False)):
            sub = [r for r in rows if r["known"] is pick]
            if sub:
                lines.append(f"- {part}: {sum(bool(r.get('correct')) for r in sub)}/{len(sub)} right")
    else:
        lines.append("- Jev was NOT asked (no TYPESAFE_API_KEY): this run checks the pages and readers only")
    lines += ["", "| case | path | ours | jev reader | Jev chose | right | conf |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        jr = {True: "✅", False: "❌", None: "?"}.get(r.get("jev_reader_sees"), "—")
        lines.append(f"| {r['case']} | {'known' if r['known'] else 'unknown'} | {'✅' if r['ours_found'] else '❌'} "
                     f"| {jr} | {r.get('chose_label', r.get('error', '—'))[:40]} "
                     f"| {'✅' if r.get('correct') else '❌' if 'correct' in r else '—'} | {r.get('confidence', '—')} |")
    path = d / f"{stamp}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, "\n".join(ln for ln in lines if ln.startswith("- "))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repeat", type=int, default=1, help="runs per case (default 1: ten tests)")
    ap.add_argument("--case", action="append", help="only this case id (repeatable)")
    ap.add_argument("--jev-reader", help="jev-ultrafast's snapshot.js, to see what Jev alone would see")
    ap.add_argument("--no-jev", action="store_true", help="readers only; do not call Jev")
    ap.add_argument("--notify", action="store_true", help="send the summary to the owner's chat")
    a = ap.parse_args()

    ask = jev.available() and not a.no_jev
    if not ask and not a.no_jev:
        print("TYPESAFE_API_KEY not set: checking the pages and readers only (no Jev calls).")
    reader_js = Path(a.jev_reader).expanduser().read_text(encoding="utf-8") if a.jev_reader else None
    cases = [c for c in load_cases() if not a.case or c["id"] in a.case]

    from playwright.sync_api import sync_playwright
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(ORIGIN + "**", serve)
        for case in cases:
            for i in range(a.repeat):
                t0 = time.perf_counter()
                row = run_case(page, case, reader_js, ask)
                row["run"] = i + 1
                row["seconds"] = round(time.perf_counter() - t0, 2)
                rows.append(row)
                mark = ("✅" if row.get("ok") else "⚠️" if row.get("correct") else "❌") if ask else \
                       ("✅" if row["ours_found"] else "❌")
                print(f"{mark} {case['id']} #{i + 1}: " + (
                    f"chose “{row.get('chose_label', row.get('error', ''))[:50]}” conf {row.get('confidence')}"
                    if ask else f"ours {'found' if row['ours_found'] else 'MISSED'} “{row['truth_label'][:40]}”"
                    + (f", jev reader {'sees' if row.get('jev_reader_sees') else 'does NOT see'} it"
                       if reader_js else "")))
                if (ask and not row.get("ok")) or not row["ours_found"]:
                    file_miss(page, row, json.dumps(jev.candidates(page), ensure_ascii=False, indent=1))
        browser.close()

    path, summary = report(rows, ask)
    print(f"\n{summary}\n-> {path}")
    if a.notify:
        from ainl.config import TelegramConfig
        from ainl.telegram import TelegramClient
        cfg = TelegramConfig.from_env()
        tg = TelegramClient(cfg.bot_token)
        tg.send(cfg.chat_id, "🧪 <b>Jev 测试</b>\n" + summary.replace("**", "") + f"\n报告：<code>{path.name}</code>")
        tg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
