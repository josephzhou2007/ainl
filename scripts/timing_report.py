"""Turn ~/.ainl/timing-trace.json into the tables in docs/timing-analysis.md.

    python scripts/timing_report.py            # markdown on stdout
    python scripts/timing_report.py --op wa.questions

Medians are true medians (mean of the middle two when n is even). "worst" is
the slowest successful run; failures are counted separately, never averaged in.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ainl.config import home  # noqa: E402

CATS = ("network", "render", "ours", "telegram", "llm", "session")
NAV_KEYS = ("ttfb", "download", "response_end", "dom_interactive", "dcl_end", "load_end",
            "net_busy_before_dcl", "net_busy_after_dcl", "resources", "resources_from_cache",
            "doc_transfer", "doc_decoded", "dns", "connect", "tls")


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else 0.0


def group_of(run: dict) -> str:
    """Runs that did the same work, judged by what the trace shows they did."""
    op, variant = run["op"], run.get("variant", "")
    names = {s["name"] for s in (run.get("trace") or {}).get("spans", [])}
    if op == "wa.assignments":
        return "wa.assignments · 需要导航回列表" if "goto_list" in names else "wa.assignments · 已在列表页"
    if op == "wa.questions":
        if "MINIMISED" in variant:
            return "wa.questions · 窗口最小化"
        if variant.startswith("warm"):
            return "wa.questions · 进程内缓存命中"
        if variant.startswith("same-page"):
            return "wa.questions · 页面已打开(无导航)"
        if variant.startswith("cold-from-other") or "goto_list" in names:
            return "wa.questions · 冷开(从另一份作业,两次导航)"
        q = run.get("questions")
        return f"wa.questions · 冷开(从列表) · {q} 题" if q else "wa.questions · 冷开(从列表)"
    if op == "canvas.planner":
        # Canvas serves an identical repeat from a short-lived cache (~0.11 s a
        # page instead of ~0.95 s). Judge by what the requests actually cost.
        http = [s["seconds"] for s in (run.get("trace") or {}).get("spans", [])
                if s["name"] == "canvas.http" and s["seconds"] is not None]
        scope = "整学期(无 until)" if "full" in variant else "4 天窗口(until)"
        cold = http and statistics.median(http) > 0.4
        return f"canvas.planner · {scope} · {'冷' if cold else 'Canvas 服务端缓存命中'}"
    return f"{op} · {variant}" if op.startswith(("tg.", "llm.")) else op


def waterfall(runs: list[dict]) -> list[tuple]:
    """Per span name: median first start, median total seconds per run, calls per run."""
    per_run = []
    order: dict[str, float] = {}
    cat_of: dict[str, str] = {}
    depth_of: dict[str, int] = {}
    for r in runs:
        tot: dict[str, float] = defaultdict(float)
        cnt: dict[str, int] = defaultdict(int)
        first: dict[str, float] = {}
        for s in r["trace"]["spans"]:
            if s["seconds"] is None:
                continue
            tot[s["name"]] += s["seconds"]
            cnt[s["name"]] += 1
            first.setdefault(s["name"], s["start"])
            cat_of.setdefault(s["name"], s["cat"])
            depth_of.setdefault(s["name"], s["depth"])
        per_run.append((tot, cnt, first))
    names = {n for tot, _, _ in per_run for n in tot}
    rows = []
    for n in names:
        present = [(t[n], c[n], f[n]) for t, c, f in per_run if n in t]
        rows.append((med([p[2] for p in present]), n, cat_of[n], depth_of[n],
                     med([p[0] for p in present]), max(p[0] for p in present),
                     med([p[1] for p in present]), len(present)))
    return sorted(rows)


def nav_rows(runs: list[dict]) -> dict[str, dict]:
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in runs:
        for s in r["trace"]["spans"]:
            nav = (s.get("meta") or {}).get("nav") or (s.get("meta") or {}).get("final_nav")
            if nav:
                for k in NAV_KEYS:
                    out[s["name"]][k].append(nav.get(k))
    return out


def slowest_resources(runs: list[dict], top: int = 6) -> list[tuple]:
    seen: dict[str, list] = defaultdict(list)
    for r in runs:
        for s in r["trace"]["spans"]:
            nav = (s.get("meta") or {}).get("nav") or (s.get("meta") or {}).get("final_nav")
            for e in (nav or {}).get("slowest", []):
                seen[e["url"]].append((e["duration"], e["transfer"], e["kind"]))
    rows = [(med([d for d, _, _ in v]), max(d for d, _, _ in v), len(v),
             med([t for _, t, _ in v]), v[0][2], url) for url, v in seen.items()]
    return sorted(rows, reverse=True)[:top]


def report(runs: list[dict], only: str | None) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        if only and r["op"] != only:
            continue
        groups[group_of(r)].append(r)

    print("| 操作 | n | 失败 | 中位 s | 最差 s | " + " | ".join(CATS) + " |")
    print("|---|---|---|---|---|" + "---|" * len(CATS))
    for g, rs in groups.items():
        ok = [r for r in rs if r["ok"]]
        if not ok:                      # nothing succeeded: show what the failures cost
            g += " (全部失败, 数字取自失败的运行)"
        walls = [r["wall"] for r in (ok or rs)]
        traced = [r for r in (ok or rs) if r.get("trace")]
        cells = []
        for c in CATS:
            m = med([r["trace"]["by_category"].get(c, 0.0) for r in traced]) if traced else 0.0
            w = med(walls) or 1.0
            cells.append(f"{m:.2f} ({100 * m / w:.0f}%)" if m >= 0.005 else "—")
        print(f"| {g} | {len(rs)} | {len(rs) - len(ok)} | {med(walls):.2f} | "
              f"{max(walls) if walls else 0:.2f} | " + " | ".join(cells) + " |")

    for g, rs in groups.items():
        traced = [r for r in rs if r.get("trace") and r["trace"]["spans"]]
        if not traced:
            continue
        okish = [r for r in traced if r["ok"]] or traced
        print(f"\n### {g}  (n={len(okish)})\n")
        print("| 开始 s | span | 类别 | 每次调用数 | 中位合计 s | 最差 s | 出现于 |")
        print("|---|---|---|---|---|---|---|")
        for start, name, cat, depth, m, worst, calls, n in waterfall(okish):
            print(f"| {start:.2f} | {'· ' * depth}{name} | {cat} | {calls:.0f} | {m:.3f} | "
                  f"{worst:.3f} | {n}/{len(okish)} |")
        navs = nav_rows(okish)
        for span_name, cols in navs.items():
            print(f"\nNavigation Timing · `{span_name}` (中位, ms, n={len(cols['ttfb'])}): "
                  + ", ".join(f"{k}={med(v):.0f}" for k, v in cols.items()))
        slow = slowest_resources(okish)
        if slow:
            print("\n最慢子资源 (中位 ms / 最差 ms / 出现次数 / transferSize / 类型 / host+path):\n")
            for m, worst, n, transfer, kind, url in slow:
                print(f"- {m:.0f} / {worst:.0f} / {n} / {transfer:.0f} / {kind} / `{url[:110]}`")
        hops = [n["value"] for r in okish for n in r["trace"].get("notes", [])
                if n["key"].startswith("doc_hops:launch")]
        if hops:
            print("\n主文档跳转链 (每次运行一行: host+path@开始ms ttfb/结束ms):\n")
            for chain in hops:
                print("- " + " → ".join(f"`{h['url'][:60]}`@{h['at']:.0f} {h['ttfb']:.0f}/{h['response_end']:.0f}"
                                         for h in sorted(chain, key=lambda h: h["at"])))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--op")
    ap.add_argument("--file", default=str(home() / "timing-trace.json"))
    a = ap.parse_args()
    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"<!-- {len(data['runs'])} runs; meta: {json.dumps(data['meta'], ensure_ascii=False)[:300]} -->\n")
    report(data["runs"], a.op)


if __name__ == "__main__":
    main()
