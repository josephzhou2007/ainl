"""How long do people pause between messages? — and where to cut "still working".

The browser a person uses can be released while they think (writing answers on
paper, taking the photo) and brought back when they reply. How long to keep it
first is a question about people, so it is answered from the chat log:

    gap = time between one person's consecutive messages

Plotted on a log time axis the gaps form peaks — seconds (tapping through a
list), minutes to tens of minutes (working on a sheet), hours (gone). The
valley after the "working" peak is where to cut: waiting longer than that
mostly waits for someone who left.

    .venv/bin/python scripts/gap_report.py                 # -> ~/.ainl/reports/gaps.html
    .venv/bin/python scripts/gap_report.py --since 2026-09-26 --out /tmp/gaps.html

Reads only timestamps and who sent them — never message text.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Log-spaced bin edges, in minutes.
EDGES = [0, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45, 60, 90, 120, 240, 480, 1440, 10**9]
ENOUGH = 200            # gaps before a valley is worth trusting


def gaps_from(path: Path, since: datetime | None = None) -> list[float]:
    per: dict[str, list[datetime]] = defaultdict(list)
    for line in path.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("dir") != "in":
            continue
        try:
            t = datetime.fromisoformat(r["ts"])
        except (KeyError, ValueError):
            continue
        if since and t < since:
            continue
        per[str(r.get("chat_id"))].append(t)
    out = []
    for ts in per.values():
        ts.sort()
        out += [(b - a).total_seconds() / 60 for a, b in zip(ts, ts[1:])]
    return out


def histogram(gaps: list[float]) -> list[int]:
    counts = [0] * (len(EDGES) - 1)
    for g in gaps:
        for i in range(len(EDGES) - 1):
            if EDGES[i] <= g < EDGES[i + 1]:
                counts[i] += 1
                break
    return counts


def valley(counts: list[int]) -> int | None:
    """Index of the bin where the valley after the first peak bottoms out:
    smoothed [1,2,1], first local minimum after the highest bin that is
    followed by a rise. None when there is no second peak."""
    n = len(counts)
    sm = [(counts[max(i - 1, 0)] + 2 * counts[i] + counts[min(i + 1, n - 1)]) / 4 for i in range(n)]
    top = max(range(n), key=lambda i: sm[i])
    for i in range(top + 1, n - 1):
        if sm[i] <= sm[i - 1] and sm[i] < sm[i + 1] and any(sm[j] > sm[i] for j in range(i + 1, n)):
            return i
    return None


def _label(m: float) -> str:
    if m >= 10**8:
        return "∞"
    if m < 1:
        return f"{round(m * 60)}s"
    if m < 60:
        return f"{m:g}m"
    return f"{m / 60:g}h"


def render(counts: list[int], cut: int | None, n: int, n_people: str, since: str) -> str:
    W, H, L, R, T, B = 760, 300, 44, 16, 24, 54
    bw = (W - L - R) / len(counts)
    top = max(counts) or 1
    ymax = max(5, math.ceil(top / 5) * 5)
    bars, ticks = [], []
    for i, c in enumerate(counts):
        x = L + i * bw + 1
        h = (H - T - B) * c / ymax
        y = H - B - h
        rng = f"{_label(EDGES[i])}–{_label(EDGES[i + 1])}"
        pct = f"{100 * c / n:.0f}%" if n else "0%"
        # 4px rounded data end, anchored to the baseline: a path with a flat bottom.
        r = min(4, h / 2, (bw - 2) / 2) if h > 0 else 0
        w = bw - 2
        d = (f"M{x:.1f},{H - B} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
             f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{H - B} Z") if h > 0 else ""
        bars.append(f'<g class="bar" data-tip="{html.escape(rng)}: {c} gap(s), {pct}">'
                    f'<rect class="hit" x="{x - 1:.1f}" y="{T}" width="{bw:.1f}" height="{H - T - B}"/>'
                    + (f'<path class="mark" d="{d}"/>' if d else "") + "</g>")
        ticks.append(f'<text class="tick" x="{L + i * bw:.1f}" y="{H - B + 16}" text-anchor="middle">{_label(EDGES[i])}</text>')
    grid = "".join(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{H - B - (H - T - B) * v / ymax:.1f}" '
                   f'y2="{H - B - (H - T - B) * v / ymax:.1f}"/><text class="tick" x="{L - 6}" '
                   f'y="{H - B - (H - T - B) * v / ymax + 4:.1f}" text-anchor="end">{v}</text>'
                   for v in range(0, ymax + 1, max(1, ymax // 5)))
    cutline = ""
    verdict = "No valley yet: the gaps do not show a second peak."
    if cut is not None:
        cx = L + (cut + 1) * bw
        cutline = (f'<line class="cut" x1="{cx:.1f}" x2="{cx:.1f}" y1="{T - 6}" y2="{H - B}"/>'
                   f'<text class="cutlabel" x="{cx + 6:.1f}" y="{T + 6}">T ≈ {_label(EDGES[cut + 1])}</text>')
        verdict = (f"Suggested T ≈ <b>{_label(EDGES[cut + 1])}</b>: the valley after the first peak "
                   f"({_label(EDGES[cut])}–{_label(EDGES[cut + 1])}).")
    if n < ENOUGH:
        verdict += f" Only {n} gaps so far — at least {ENOUGH} are needed before trusting it."
    rows = "".join(f"<tr><td>{_label(EDGES[i])}–{_label(EDGES[i + 1])}</td><td>{c}</td></tr>"
                   for i, c in enumerate(counts))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pause lengths</title>
<style>
:root {{ --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --bar:#2a78d6; --cut:#eb6834; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --surface:#1a1a19; --page:#0d0d0d;
  --ink:#fff; --ink2:#c3c2b7; --grid:#2c2c2a; --axis:#383835; --bar:#3987e5; --cut:#d95926; }} }}
:root[data-theme="dark"] {{ --surface:#1a1a19; --page:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --grid:#2c2c2a;
  --axis:#383835; --bar:#3987e5; --cut:#d95926; }}
body {{ margin:0; background:var(--page); color:var(--ink); font:14px/1.45 -apple-system,system-ui,sans-serif; }}
main {{ max-width:800px; margin:0 auto; padding:20px 16px; }}
.card {{ background:var(--surface); border:1px solid var(--grid); border-radius:10px; padding:16px; }}
h1 {{ font-size:18px; margin:0 0 4px; }} p {{ color:var(--ink2); margin:4px 0 12px; }}
svg {{ width:100%; height:auto; display:block; overflow:visible; }}
.grid {{ stroke:var(--grid); stroke-width:1; }} .tick {{ fill:var(--muted); font-size:11px; }}
.mark {{ fill:var(--bar); }} .hit {{ fill:transparent; }} .bar:hover .mark {{ opacity:.8; }}
.cut {{ stroke:var(--cut); stroke-width:2; stroke-dasharray:4 3; }} .cutlabel {{ fill:var(--ink); font-size:12px; font-weight:600; }}
.axis {{ stroke:var(--axis); }} #tip {{ position:fixed; pointer-events:none; background:var(--ink); color:var(--surface);
  padding:4px 8px; border-radius:6px; font-size:12px; display:none; }}
details {{ margin-top:12px; color:var(--ink2); }} table {{ border-collapse:collapse; }} td {{ padding:2px 12px 2px 0; }}
</style></head><body><main><div class="card">
<h1>How long people pause between messages</h1>
<p>{n} gaps from {n_people} people{(' since ' + html.escape(since)) if since else ''}. Time axis is logarithmic.</p>
<svg viewBox="0 0 {W} {H}" role="img" aria-label="Histogram of pause lengths">
{grid}<line class="axis" x1="{L}" x2="{W - R}" y1="{H - B}" y2="{H - B}"/>
{''.join(bars)}{''.join(ticks)}{cutline}
<text class="tick" x="{(W + L) / 2:.0f}" y="{H - 12}" text-anchor="middle">pause before the next message</text>
</svg>
<p>{verdict}</p>
<details><summary>Table</summary><table><tr><td><b>pause</b></td><td><b>gaps</b></td></tr>{rows}</table></details>
</div></main><div id="tip"></div>
<script>
const tip = document.getElementById('tip');
document.querySelectorAll('.bar').forEach(g => {{
  g.addEventListener('mousemove', e => {{ tip.textContent = g.dataset.tip; tip.style.display = 'block';
    tip.style.left = (e.clientX + 12) + 'px'; tip.style.top = (e.clientY - 28) + 'px'; }});
  g.addEventListener('mouseleave', () => tip.style.display = 'none');
}});
</script></body></html>"""


def main() -> int:
    from ainl.config import home
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", help="ISO date/time, e.g. 2026-09-26")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since).astimezone() if a.since else None
    path = home() / "transcript.jsonl"
    gaps = gaps_from(path, since)
    people = set()
    for line in path.open(encoding="utf-8"):
        try:
            r = json.loads(line)
            if r.get("dir") == "in":
                people.add(str(r.get("chat_id")))
        except ValueError:
            pass
    counts = histogram(gaps)
    cut = valley(counts)
    out = a.out or (home() / "reports" / "gaps.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(counts, cut, len(gaps), str(len(people)), a.since or ""), encoding="utf-8")
    for i, c in enumerate(counts):
        print(f"{_label(EDGES[i]):>5}–{_label(EDGES[i + 1]):<5} {c:4d} " + "█" * c
              + ("   ← valley" if i == cut else ""))
    print(f"\n{len(gaps)} gaps; suggested T = {_label(EDGES[cut + 1]) if cut is not None else 'none yet'}"
          + ("" if len(gaps) >= ENOUGH else f" (needs ≥{ENOUGH} gaps to trust)") + f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
