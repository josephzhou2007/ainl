"""Probe the saved Canvas cookie session every N minutes and log how it ages.

Two clients run side by side:
  frozen    — uses the cookies exactly as saved at start, never accepts Set-Cookie.
              Answers: how long does a *stale* copy of the session stay valid?
  rotating  — a normal cookie jar that absorbs every Set-Cookie Canvas sends.
              Answers: how long does the session live if we keep up with rotation?

Each tick appends one JSON line to ~/.ainl/session-monitor.jsonl with the status
of both probes, which cookies were re-issued, and their new expiry attributes.
Values are never logged — only names, lengths and a short hash.

    python scripts/session_monitor.py            # every 5 min, forever (Ctrl+C to stop)
    python scripts/session_monitor.py --minutes 2 --once
"""
from __future__ import annotations

import argparse, hashlib, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import httpx

HOME = Path(os.environ.get("AINL_HOME") or Path.home() / ".ainl")
STATE = HOME / "canvas.wisc.edu.storage_state.json"
LOG = HOME / "session-monitor.jsonl"
BASE = "https://canvas.wisc.edu"
PROBE = "/api/v1/users/self"


def h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()[:10]


def load_cookies() -> dict[str, str]:
    st = json.loads(STATE.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in st["cookies"] if c.get("domain", "").endswith("canvas.wisc.edu")}


def probe(client: httpx.Client) -> dict:
    t0 = time.time()
    try:
        r = client.get(PROBE, headers={"Accept": "application/json+canvas-string-ids, application/json"})
    except httpx.HTTPError as e:
        return {"ok": False, "error": type(e).__name__, "ms": int((time.time() - t0) * 1000)}
    setc = r.headers.get_list("set-cookie")
    reissued = []
    for sc in setc:
        name, _, rest = sc.partition("=")
        attrs = {kv.split("=")[0].strip().lower(): (kv.split("=", 1)[1].strip() if "=" in kv else True)
                 for kv in rest.split(";")[1:]}
        reissued.append({"name": name.strip(), "expires": attrs.get("expires"), "max_age": attrs.get("max-age"),
                         "value_hash": h(rest.split(";")[0])})
    body_ok = r.status_code == 200 and '"id"' in r.text[:200]
    return {"ok": body_ok, "status": r.status_code, "ms": int((time.time() - t0) * 1000),
            "location": r.headers.get("location", "")[:80] if r.status_code in (301, 302) else None,
            "reissued": reissued, "rate_remaining": r.headers.get("x-rate-limit-remaining"),
            "canvas_meta": r.headers.get("x-canvas-meta", "")[:60]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    start_cookies = load_cookies()
    started = datetime.now(timezone.utc)
    frozen = httpx.Client(base_url=BASE, cookies=start_cookies, follow_redirects=False, timeout=30)
    rotating = httpx.Client(base_url=BASE, cookies=dict(start_cookies), follow_redirects=False, timeout=30)
    print(f"monitor started {started:%Y-%m-%d %H:%M:%S}Z, every {a.minutes} min, log -> {LOG}", flush=True)
    frozen_died_at = None
    tick = 0
    while True:
        tick += 1
        # frozen: re-inject start cookies every time so any Set-Cookie it received is discarded
        frozen.cookies.clear()
        for k, v in start_cookies.items():
            frozen.cookies.set(k, v, domain="canvas.wisc.edu")
        fr = probe(frozen)
        ro = probe(rotating)
        now = datetime.now(timezone.utc)
        age_min = round((now - started).total_seconds() / 60, 1)
        if not fr["ok"] and frozen_died_at is None:
            frozen_died_at = age_min
        rec = {"t": now.isoformat(timespec="seconds"), "tick": tick, "age_min": age_min,
               "frozen": fr, "rotating": ro,
               "rotating_session_hashes": [h(c.value) for c in rotating.cookies.jar if c.name == "canvas_session"],
               "frozen_first_failure_min": frozen_died_at}
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[{now:%H:%M:%S}] age={age_min:>6}m  frozen={'OK ' if fr['ok'] else 'DEAD'}({fr.get('status')})  "
              f"rotating={'OK ' if ro['ok'] else 'DEAD'}({ro.get('status')})  reissued={[c['name'] for c in ro.get('reissued', [])]}",
              flush=True)
        if a.once or not ro["ok"]:
            if not ro["ok"]:
                print("rotating client died — session is truly over. stopping.", flush=True)
            return 0
        time.sleep(a.minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
