"""Run several profiles at once, on one account — the proof that the pool,
the per-key workers and the keyed connection cache do what they claim.

Two phases, run one after the other:

  1. neutral   Each profile, on its own worker thread, writes a tag into its
               own browser and reads it back while the others do the same.
               Proves: parallel (not queued), each thread talks only to its
               own browser (no cross-talk), ports are distinct. Touches no
               login of any kind.
  2. webassign Each profile rebuilds a Cengage session over LTI (the student's
               own Canvas, via the sessionless launch) and opens the same
               assignment. Proves — or disproves — that one account can hold
               several WebAssign sessions at once. That answer matters: real
               classmates each have their own account, so it only bounds how
               "one person, many profiles" can be tested.

    python scale-up/multi.py --profiles 2 --phase neutral
    python scale-up/multi.py --profiles 2 --phase webassign --dep 40575445

Never touches the single-user browser on 9222 except to READ the student's
Canvas cookies for the launch (the same read the bot itself performs).
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from browsers import BrowserPool                     # noqa: E402
from registry import Registry                        # noqa: E402

from ainl.util import browserthread                  # noqa: E402

log = logging.getLogger("ainl.scaleup.multi")


def _tag_roundtrip(pool: BrowserPool, key: str, hold: float) -> dict:
    """On this key's worker: attach to this key's browser, plant a tag, wait
    (so the others are provably running at the same time), read it back."""
    from ainl.util import pw

    def go():
        with pw.connect(pool.cdp_url(key)) as browser:
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.evaluate("(k) => { window.__ainl_tag = k; }", key)
            time.sleep(hold)
            return {"tag": page.evaluate("() => window.__ainl_tag"),
                    "thread": threading.current_thread().name,
                    "port": pool.cdp_url(key).rsplit(":", 1)[1]}

    with browserthread.using(key):
        return browserthread.run(go, budget=30 + hold)


def _open_assignment(pool: BrowserPool, key: str, dep: str, launch_url) -> dict:
    from ainl.external.webassign import WebAssign

    wa = WebAssign(cdp_url=pool.cdp_url(key), launch_url=launch_url,
                   restart=lambda: pool.restart(key))
    with browserthread.using(key):
        t0 = time.monotonic()
        title, qs, shots = wa.questions(dep)
        return {"title": title, "questions": len(qs), "shots": sum(1 for s in shots if s),
                "seconds": round(time.monotonic() - t0, 1),
                "thread": threading.current_thread().name}


def _parallel(keys: list[str], fn) -> tuple[dict, float]:
    """Run fn(key) for every key on its own thread; return results and wall time."""
    results: dict = {}
    t0 = time.monotonic()

    def one(k):
        try:
            results[k] = fn(k)
        except Exception as e:                       # report, don't hide
            results[k] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}

    threads = [threading.Thread(target=one, args=(k,), name=f"driver-{k}") for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, round(time.monotonic() - t0, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", type=int, default=2)
    ap.add_argument("--phase", choices=["neutral", "webassign"], default="neutral")
    ap.add_argument("--dep", default="40575445")
    ap.add_argument("--hold", type=float, default=3.0,
                    help="neutral phase: seconds each profile holds its browser, to prove overlap")
    ap.add_argument("--keep", action="store_true", help="leave the browsers running")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    reg = Registry()
    keys = [f"me_{i + 1}" for i in range(a.profiles)]
    for k in keys:
        if reg.get(k) is None:
            reg.add(k, name=f"self (profile {k})", login_id="self")
            reg.approve(k)
    pool = BrowserPool(reg)

    try:
        if a.phase == "neutral":
            results, wall = _parallel(keys, lambda k: _tag_roundtrip(pool, k, a.hold))
            ok = all(r.get("tag") == k for k, r in results.items())
            ports = {r.get("port") for r in results.values()}
            print(f"\nneutral: {len(keys)} profiles, wall {wall}s "
                  f"(serial would be >= {a.hold * len(keys)}s)")
            for k, r in results.items():
                print(f"  {k}: {r}")
            print("PARALLEL:", wall < a.hold * len(keys),
                  "| NO CROSS-TALK:", ok, "| DISTINCT PORTS:", len(ports) == len(keys))
            return 0 if ok and len(ports) == len(keys) else 1

        # webassign phase: the launch comes from the student's own Canvas.
        from ainl.canvas import channel
        from ainl.external.platforms import classify
        canvas = channel.open_source()
        ref = None
        for course in canvas.courses():
            for row in canvas._http.paginate(f"/api/v1/courses/{course.id}/assignments",
                                             {"per_page": 50}):
                tool = (row.get("external_tool_tag_attributes") or {}).get("url") or ""
                p = classify(tool)
                if p is not None and p.key == "webassign":
                    ref = (str(course.id), str(row["id"]))
                    break
            if ref:
                break
        if ref is None:
            print("no WebAssign assignment found in Canvas", file=sys.stderr)
            return 2
        launch_url = lambda: canvas.sessionless_launch(*ref)          # noqa: E731

        results, wall = _parallel(keys, lambda k: _open_assignment(pool, k, a.dep, launch_url))
        print(f"\nwebassign: {len(keys)} profiles opened dep={a.dep}, wall {wall}s")
        for k, r in results.items():
            print(f"  {k}: {r}")
        # Did the sessions survive each other? Re-check every profile, read-only.
        from ainl.util import pw
        alive = {}
        for k in keys:
            def check(k=k):
                with pw.connect(pool.cdp_url(k)) as b:
                    pages = [p for p in b.contexts[0].pages if not p.is_closed()]
                    return any("webassign.net" in p.url and "login" not in p.url.lower()
                               for p in pages)
            with browserthread.using(k):
                try:
                    alive[k] = browserthread.run(check, budget=20)
                except Exception as e:
                    alive[k] = f"?: {str(e)[:60]}"
        print("sessions still logged in afterwards:", alive)
        canvas.close()
        return 0 if all(isinstance(v, bool) and v for v in alive.values()) else 1
    finally:
        if not a.keep:
            pool.shutdown_all()
        browserthread.reset(all_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
