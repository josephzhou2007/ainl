#!/usr/bin/env python3
"""
Analyze flow-recorder exports from one or more students.

Usage:
    python analyze_flow.py data/*.json
    python analyze_flow.py data/ --out report.md

Each input file is one student's export. The student label is taken from the
filename stem (rename to e.g. `s01.json`, `s02.json` to keep people anonymous).
"""
import json, sys, statistics, argparse
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime

SESSION_GAP_MS = 30 * 60 * 1000          # 30 min of silence starts a new session
ACTION_TYPES = {"file_attach", "form_submit"}
PASSTHROUGH_SEC = 8                       # dwell under this = you were just passing through


def load(paths):
    students = {}
    files = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        else:
            files.append(p)
    for f in files:
        try:
            ev = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ! skipped {f.name}: {e}", file=sys.stderr)
            continue
        if isinstance(ev, list) and ev:
            students[f.stem] = sorted(ev, key=lambda e: e.get("t", 0))
    return students


def sessionize(events):
    sessions, cur, last = [], [], None
    for e in events:
        if last is not None and e.get("t", 0) - last > SESSION_GAP_MS:
            sessions.append(cur); cur = []
        cur.append(e); last = e.get("t", 0)
    if cur:
        sessions.append(cur)
    return sessions


def episodes(events):
    """A submission episode = everything leading up to an action event."""
    out, buf = [], []
    for e in events:
        buf.append(e)
        if e.get("type") in ACTION_TYPES:
            out.append(buf)
            buf = []
            continue
        if len(buf) > 400:
            buf = buf[-400:]
    return out


def friction_score(episode):
    """Rate how painful one submission episode was, 0 (smooth) .. 100 (awful).

    `episode` is the list of events leading up to a single file_attach /
    form_submit, oldest first. Useful fields on each event:

        e["type"]      nav | click | dwell | file_attach | form_submit
        e["host"]      "canvas.wisc.edu", "www.gradescope.com", ...
        e["path"]      "/courses/100003/assignments"
        e["label"]     text of the link/button clicked (clicks only)
        e["leavesTo"]  target host, set only when the click left the domain
        e["seconds"]   dwell time (dwell events only)
        e["t"]         epoch ms

    Return (score, reason) where reason is a short string naming the single
    biggest problem, e.g. "4 domains" or "backtracked 6 times".
    """
    # TODO(human): implement the scoring.
    return 0, "not scored"


def analyze_one(name, events):
    r = {"student": name, "events": len(events)}
    ts = [e["t"] for e in events if "t" in e]
    if ts:
        r["span_days"] = round((max(ts) - min(ts)) / 86400000, 1)
        r["first"] = datetime.fromtimestamp(min(ts) / 1000).strftime("%Y-%m-%d")
        r["last"] = datetime.fromtimestamp(max(ts) / 1000).strftime("%Y-%m-%d")

    r["hosts"] = Counter(e.get("host", "?") for e in events)
    r["types"] = Counter(e.get("type", "?") for e in events)
    r["jumps"] = Counter(
        f'{e.get("host")} -> {e["leavesTo"]}' for e in events if e.get("leavesTo")
    )

    navs = r["types"].get("nav", 0)
    acts = sum(r["types"].get(t, 0) for t in ACTION_TYPES)
    # How many pages did you load per thing you actually accomplished?
    r["hunting_index"] = round(navs / acts, 1) if acts else None
    r["actions"] = acts

    dwells = [e["seconds"] for e in events if e.get("type") == "dwell" and "seconds" in e]
    if dwells:
        r["dwell_median"] = statistics.median(dwells)
        r["passthrough_pct"] = round(
            100 * sum(1 for d in dwells if d < PASSTHROUGH_SEC) / len(dwells)
        )

    r["sessions"] = len(sessionize(events))

    eps = episodes(events)
    if eps:
        steps, doms, mins = [], [], []
        for ep in eps:
            window = ep[-60:]
            steps.append(sum(1 for e in window if e.get("type") in ("nav", "click")))
            doms.append(len({e.get("host") for e in window}))
            if len(window) > 1:
                mins.append((window[-1]["t"] - window[0]["t"]) / 60000)
        r["episodes"] = len(eps)
        r["steps_median"] = round(statistics.median(steps), 1)
        r["domains_median"] = round(statistics.median(doms), 1)
        if mins:
            r["minutes_median"] = round(statistics.median(mins), 1)

        scored = []
        for ep in eps:
            sc, why = friction_score(ep[-60:])
            scored.append((sc, why, ep[-1].get("host", "?"), ep[-1].get("path", "?")))
        scored.sort(key=lambda x: -x[0])
        r["worst"] = scored[:5]
        r["friction_mean"] = round(statistics.mean(s[0] for s in scored), 1) if scored else None

    r["top_clicks"] = Counter(
        e["label"] for e in events if e.get("type") == "click" and e.get("label")
    ).most_common(15)
    return r


def render(results):
    L = ["# Flow Recorder — 分析报告", ""]
    L.append(f"样本：**{len(results)}** 名学生，"
             f"共 **{sum(r['events'] for r in results):,}** 条事件")
    L.append("")

    L.append("## 总览")
    L.append("")
    L.append("| 学生 | 事件 | 天数 | 会话 | 提交动作 | 找路指数 | 每次提交步数 | 跨域名数 | 耗时(分) |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        L.append("| {} | {:,} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["student"], r["events"], r.get("span_days", "-"), r.get("sessions", "-"),
            r.get("actions", 0), r.get("hunting_index", "-"),
            r.get("steps_median", "-"), r.get("domains_median", "-"),
            r.get("minutes_median", "-")))
    L.append("")
    L.append("> **找路指数** = 加载的页面数 ÷ 真正完成的动作数。"
             "等于 1 表示每开一个页面就干成一件事；等于 20 表示为了交一次作业翻了 20 个页面。")
    L.append("")

    agg = [r["hunting_index"] for r in results if r.get("hunting_index")]
    if agg:
        L.append(f"### 关键数字")
        L.append("")
        L.append(f"- 找路指数中位数：**{statistics.median(agg)}**")
        st = [r["steps_median"] for r in results if r.get("steps_median")]
        dm = [r["domains_median"] for r in results if r.get("domains_median")]
        if st:
            L.append(f"- 完成一次提交平均要点 **{round(statistics.median(st))}** 下")
        if dm:
            L.append(f"- 完成一次提交平均跨 **{round(statistics.median(dm),1)}** 个域名")
        pt = [r["passthrough_pct"] for r in results if r.get("passthrough_pct")]
        if pt:
            L.append(f"- **{round(statistics.median(pt))}%** 的页面停留不足 {PASSTHROUGH_SEC} 秒"
                     f"（= 纯粹过路，没在上面读任何东西）")
        L.append("")

    L.append("## 域名分布（合并全部学生）")
    L.append("")
    allh = Counter()
    for r in results:
        allh.update(r["hosts"])
    L.append("| 域名 | 事件数 | 占比 |")
    L.append("|---|---|---|")
    tot = sum(allh.values()) or 1
    for h, n in allh.most_common(20):
        L.append(f"| `{h}` | {n:,} | {100*n//tot}% |")
    L.append("")

    L.append("## 跨域名跳转 Top 20")
    L.append("")
    L.append("每一条都是一次「Canvas 没法解决、必须换个网站」的时刻。")
    L.append("")
    allj = Counter()
    for r in results:
        allj.update(r["jumps"])
    L.append("| 跳转 | 次数 |")
    L.append("|---|---|")
    for j, n in allj.most_common(20):
        L.append(f"| {j} | {n} |")
    L.append("")

    if any(r.get("worst") for r in results):
        L.append("## 最痛的提交流程（friction score 排序）")
        L.append("")
        L.append("| 学生 | 分数 | 主要问题 | 终点 |")
        L.append("|---|---|---|---|")
        rows = []
        for r in results:
            for sc, why, host, path in r.get("worst", []):
                rows.append((sc, r["student"], why, f"`{host}{path}`"))
        rows.sort(key=lambda x: -x[0])
        for sc, stu, why, dest in rows[:15]:
            L.append(f"| {stu} | {sc} | {why} | {dest} |")
        L.append("")

    L.append("## 学生在找什么（点击标签 Top 20）")
    L.append("")
    allc = Counter()
    for r in results:
        allc.update(dict(r["top_clicks"]))
    for lab, n in allc.most_common(20):
        L.append(f"- `{lab}` × {n}")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="JSON exports or a directory of them")
    ap.add_argument("--out", default=None, help="write markdown report here")
    a = ap.parse_args()

    students = load(a.paths)
    if not students:
        sys.exit("no usable input files")
    print(f"loaded {len(students)} student(s): {', '.join(students)}", file=sys.stderr)

    results = [analyze_one(n, ev) for n, ev in sorted(students.items())]
    md = render(results)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"wrote {a.out}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
