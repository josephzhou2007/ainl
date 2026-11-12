"""What went wrong since the last look — for the scheduled self-review.

Reads ~/.ainl/transcript.jsonl (every chat message, both ways) and
~/.ainl/bot.log.err (the bot's own log) from the last checkpoint on, groups
the chat into rounds (one assignment opened → worked → left), and prints a
report of everything that looks wrong:

  - ✗ after a submit (with the model's answers and the question pictures)
  - the bot's own failures: "Something broke", "solver failed", "couldn't
    read any answer lines", "nothing changed", "auto crashed", "can't find",
    "didn't draw", "read back empty", "Couldn't tick", "Couldn't press"
  - WARNING/ERROR lines and tracebacks in the log

Prints nothing and exits 0 when there is nothing new. Exits 3 when there is
something to look at. The checkpoint moves forward only with --commit, so a
review that crashes half way sees the same material next time.

    python scripts/auto_review.py            # report since the checkpoint
    python scripts/auto_review.py --since 2026-09-25T10:00
    python scripts/auto_review.py --commit   # report, then move the checkpoint
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ainl.config import home  # noqa: E402

BAD_OUT = re.compile(
    r"Something broke|solver failed|couldn't read any answer|nothing changed|auto crashed|"
    r"can't find the open step|didn't draw|read back empty|Couldn't tick|Couldn't press|"
    r"Couldn't convert|marked wrong|✗|I couldn't read that|couldn't match any answer|"
    r"step limit|⚠️", re.I)
# instructions that carry a ⚠️ but are not problems
BENIGN = re.compile(r"^(?:Write |⚠️ \d+ has \d+ blanks)|has \d+ blanks — write those answers", re.I)
LOG_BAD = re.compile(r" (WARNING|ERROR|CRITICAL) |Traceback|Error:|Exception", re.I)
LOG_NOISE = re.compile(r"getUpdates failed|took .*s \(budget", re.I)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO time; default: the checkpoint")
    ap.add_argument("--commit", action="store_true", help="move the checkpoint to now")
    a = ap.parse_args()
    h = home()
    ck = h / "review_checkpoint"
    since = a.since or (ck.read_text().strip() if ck.exists() else "1970-01-01T00:00:00+00:00")
    t0 = _ts(since)
    if t0.tzinfo is None:
        t0 = t0.astimezone()

    recs = []
    tp = h / "transcript.jsonl"
    if tp.exists():
        for line in tp.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if _ts(r["ts"]) > t0:
                    recs.append(r)
            except Exception:
                continue

    log_lines = []
    lp = h / "bot.log.err"
    if lp.exists():
        naive0 = t0.astimezone().replace(tzinfo=None)
        tb = False
        for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
            if m:
                tb = False
                try:
                    if datetime.fromisoformat(m.group(1)) <= naive0:
                        continue
                except ValueError:
                    continue
                if LOG_BAD.search(line) and not LOG_NOISE.search(line):
                    log_lines.append(line[:400])
                    tb = "Traceback" in line or " ERROR " in line
            elif tb or line.startswith("Traceback"):
                tb = True
                log_lines.append("    " + line[:300])

    # rounds: split where an assignment is opened ("<b>B3 · Title</b> — N questions")
    rounds, cur = [], None
    for r in recs:
        txt = r.get("text") or ""
        m = re.search(r"<b>(?:[A-J]\d{1,2} · )?([^<]+)</b> — (\d+) questions", txt)
        if r["dir"] == "out" and m:
            cur = {"title": m.group(1), "start": r["ts"], "events": [], "bad": [], "pictures": []}
            rounds.append(cur)
        if cur is None:
            cur = {"title": "(no assignment open)", "start": r["ts"], "events": [], "bad": [], "pictures": []}
            rounds.append(cur)
        short = re.sub(r"<[^>]+>", "", txt).replace("\n", " ")[:600]
        arrow = "→" if r["dir"] == "out" else "←"
        cur["events"].append(f"{r['ts'][11:19]} {arrow} {r.get('kind')}: {short}")
        for f in [r.get("file")] + list(r.get("files") or []):
            if f and r["dir"] == "out":
                cur["pictures"].append(str(h / f))
        if r["dir"] == "out" and BAD_OUT.search(txt) and not BENIGN.search(txt):
            cur["bad"].append(f"{r['ts'][11:19]} {short}")

    bad_rounds = [x for x in rounds if x["bad"]]
    if not bad_rounds and not log_lines:
        if a.commit:
            ck.write_text(datetime.now().astimezone().isoformat(timespec="seconds"))
        return 0

    print(f"# ainl review since {since}\n")
    for x in bad_rounds:
        print(f"## {x['title']} (from {x['start'][:19]})\n")
        print("Problems:")
        for b in x["bad"]:
            print(f"  - {b}")
        print("\nWhole round:")
        for e in x["events"][-80:]:
            print(f"  {e}")
        if x["pictures"]:
            print("\nPictures the bot sent (question pictures first, then filled/graded):")
            for p in x["pictures"][:40]:
                print(f"  {p}")
        print()
    if log_lines:
        print("## Log warnings/errors\n")
        for ln in log_lines[-120:]:
            print("  " + ln)
    if a.commit:
        ck.write_text(datetime.now().astimezone().isoformat(timespec="seconds"))
    return 3


if __name__ == "__main__":
    sys.exit(main())
