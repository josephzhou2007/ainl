"""What the bot cost, from ~/.ainl/metrics — for tuning now and for sizing a
multi-user version later.

    python scripts/metrics_report.py                 # the last sweep
    python scripts/metrics_report.py --since 2026-09-25T21:00 [--until …]
    python scripts/metrics_report.py --hours 24

Sections: sweep items (time, peak browser memory, model tokens), how long
messages waited (the queue), latency per service, browser steps, model use,
memory, and a rough per-student estimate for scale-up.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ainl.util.metrics import pct, read  # noqa: E402


def _t(r):
    return datetime.fromisoformat(r["ts"])


def _fmt(x, unit=""):
    return "–" if x is None else (f"{x:.1f}{unit}" if isinstance(x, float) else f"{x}{unit}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since"); ap.add_argument("--until"); ap.add_argument("--hours", type=float)
    a = ap.parse_args()
    tz = datetime.now().astimezone().tzinfo
    def parse(s):
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=tz)
    since = parse(a.since) if a.since else None
    until = parse(a.until) if a.until else None
    if a.hours:
        since = datetime.now().astimezone() - timedelta(hours=a.hours)
    recs = read(since, until)
    if not since:                                   # default: from the last /sweep command on
        cmds = [r for r in recs if r["kind"] == "update" and r.get("what") == "/sweep"]
        if not cmds:
            print("no sweep recorded yet; use --hours or --since"); return 1
        since = _t(cmds[-1]) - timedelta(seconds=10)
        recs = [r for r in recs if _t(r) >= since]
    if not recs:
        print("no metrics in that window"); return 1
    print(f"# ainl metrics {_t(recs[0]):%Y-%m-%d %H:%M} → {_t(recs[-1]):%H:%M}  ({len(recs)} records)\n")
    samples = [r for r in recs if r["kind"] == "sample"]

    def peak_browser(t0, t1):
        # an item shorter than the sampling interval: the nearest sample around it
        near = [s for s in samples if t0 - timedelta(seconds=15) <= _t(s) <= t1 + timedelta(seconds=15)]
        inside = [s for s in near if t0 <= _t(s) <= t1] or near
        vals = [sum((b.get("footprint_mb") or b["rss_mb"]) for b in s["browsers"].values()) for s in inside]
        return max(vals) if vals else None

    # -- sweep items -----------------------------------------------------------
    open_ = {}
    items = []
    for r in recs:
        if r["kind"] != "sweep":
            continue
        if r["phase"] == "start":
            open_[r["code"]] = r
        elif r["code"] in open_:
            s = open_.pop(r["code"])
            t0, t1 = _t(s), _t(r)
            llm = [x for x in recs if x["kind"] == "llm" and t0 <= _t(x) <= t1]
            http = [x for x in recs if x["kind"] == "http" and t0 <= _t(x) <= t1]
            items.append({"code": r["code"], "course": r["course"], "kind": r["item_kind"], "s": r["s"],
                          "peak_mb": peak_browser(t0, t1), "tokens": sum(x["prompt"] + x["reasoning"] + x["output"] for x in llm),
                          "llm_calls": len(llm), "http": len(http)})
    if items:
        print("## Sweep items\n")
        print(f"{'code':6} {'course':11} {'kind':10} {'secs':>6} {'browser MB':>11} {'tokens':>8} {'http':>5}")
        for i in items:
            print(f"{i['code']:6} {i['course'][:11]:11} {i['kind'][:10]:10} {i['s']:6.1f} "
                  f"{_fmt(i['peak_mb']):>11} {i['tokens']:8} {i['http']:5}")
        by_kind = defaultdict(list)
        for i in items:
            by_kind[i["kind"]].append(i)
        print("\nper kind: " + "; ".join(f"{k}: {len(v)} items, median {median(x['s'] for x in v):.1f}s, "
                                         f"median {median(x['tokens'] for x in v):.0f} tokens" for k, v in by_kind.items()))
        print()

    # -- the queue -------------------------------------------------------------
    ups = [r for r in recs if r["kind"] == "update"]
    if ups:
        lags = [r["lag_s"] for r in ups if r.get("lag_s") is not None]
        print("## Messages (the bot does one thing at a time — wait = queue)\n")
        print(f"{len(ups)} handled · wait p50 {_fmt(pct(lags, 50), 's')} p95 {_fmt(pct(lags, 95), 's')} "
              f"max {_fmt(max(lags) if lags else None, 's')} · handling p50 "
              f"{_fmt(pct([r['handle_s'] for r in ups], 50), 's')} p95 {_fmt(pct([r['handle_s'] for r in ups], 95), 's')}")
        busy = sum(r["handle_s"] for r in ups)
        span_s = max(1.0, (_t(recs[-1]) - _t(recs[0])).total_seconds())
        print(f"busy {busy:.0f}s of {span_s:.0f}s ({100 * busy / span_s:.0f}%) on the chat thread\n")

    # -- services --------------------------------------------------------------
    http = [r for r in recs if r["kind"] == "http" and r.get("op") != "getUpdates"]
    if http:
        print("## Latency per service (the long poll excluded)\n")
        for svc in sorted({r["svc"] for r in http}):
            c = [r for r in http if r["svc"] == svc]
            bad = sum(1 for r in c if (r.get("status") or 0) >= 400)
            rl = [float(r["rate_left"]) for r in c if r.get("rate_left")]
            print(f"{svc:9} {len(c):5} calls · p50 {_fmt(pct([r['ms'] for r in c], 50), 'ms')} "
                  f"p95 {_fmt(pct([r['ms'] for r in c], 95), 'ms')} max {_fmt(max(r['ms'] or 0 for r in c), 'ms')}"
                  + (f" · {bad} errors" if bad else "") + (f" · Canvas rate limit lowest {min(rl):.0f}" if rl else ""))
        print()

    steps = [r for r in recs if r["kind"] == "step"]
    if steps:
        print("## Browser / vendor steps\n")
        by = defaultdict(list)
        for r in steps:
            by[r["name"]].append(r)
        for name, v in sorted(by.items(), key=lambda kv: -sum(x["s"] for x in kv[1])):
            print(f"{name:22} {len(v):4}× · p50 {_fmt(pct([x['s'] for x in v], 50), 's')} "
                  f"p95 {_fmt(pct([x['s'] for x in v], 95), 's')} · over budget {sum(x['over'] for x in v)}")
        print()

    llm = [r for r in recs if r["kind"] == "llm"]
    if llm:
        print("## Model\n")
        print(f"{len(llm)} calls ({', '.join(sorted({r['model'] for r in llm}))}) · {sum(r['prompt'] for r in llm)} in · "
              f"{sum(r['reasoning'] for r in llm)} reasoning · {sum(r['output'] for r in llm)} out tokens · "
              f"p50 {_fmt(pct([r['s'] for r in llm], 50), 's')} p95 {_fmt(pct([r['s'] for r in llm], 95), 's')}\n")

    if samples:
        br = [sum((b.get("footprint_mb") or b["rss_mb"]) for b in s["browsers"].values()) for s in samples]
        br = [x for x in br if x]
        bot = [s["bot"].get("footprint_mb") or s["bot"]["rss_mb"] for s in samples]
        mch = samples[-1]["machine"]
        print("## Memory\n")
        if br:
            print(f"browser(s): min {min(br):.0f} · median {median(br):.0f} · max {max(br):.0f} MB")
        print(f"bot: median {median(bot):.0f} MB · machine {mch['mem_used_mb'] / 1024:.1f}/{mch['mem_total_mb'] / 1024:.0f} GB, "
              f"load {mch['load1']}\n")
        # -- scale-up: a rough, stated estimate ------------------------------------
        if br:
            per = median(br)
            spare = mch["mem_total_mb"] - mch["mem_used_mb"] + max(br) - 4096     # keep 4 GB for macOS
            print("## Per student, for scale-up (rough)\n")
            print(f"- one warm browser ≈ {per:.0f} MB (median here; peak {max(br):.0f})")
            print(f"- this machine could keep ≈ {max(0, int(spare // per))} browsers warm at once "
                  f"(free + this browser − 4 GB for macOS); idle ones are reaped by the pool")
            if items:
                wa = [i for i in items if i["kind"] == "webassign" and i["tokens"]]
                if wa:
                    print(f"- a WebAssign assignment ≈ {median(i['tokens'] for i in wa):.0f} model tokens and "
                          f"{median(i['s'] for i in wa):.0f}s of bot time (median of {len(wa)})")
            if ups:
                print(f"- one chat thread was busy {100 * busy / span_s:.0f}% of this window: several students "
                      "on one thread would queue behind each other — the scale-up needs one worker per student")
    return 0


if __name__ == "__main__":
    sys.exit(main())
