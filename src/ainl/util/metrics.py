"""Running numbers, kept for two uses: how busy the bot is right now, and how
much one student costs — the data a multi-user scale-up is planned from.

One JSON object per line in <AINL_HOME>/metrics/<YYYYMMDD>.jsonl, "kind" says
what it is:

  update   a Telegram update handled: lag_s (sent → handling started; the bot
           is single-threaded, so this is the queue), handle_s, what
  http     one HTTP call: svc (telegram/canvas/deepseek/other), op, status, ms,
           and Canvas's rate-limit headers when present
  step     a browser/vendor step from util.timing: name, s, ok, over budget
  llm      a model call: model, s, prompt/reasoning/output tokens
  sample   every SAMPLE_EVERY s: bot process, browsers (per profile), machine
  sweep    a sweep item started/ended: code, course, kind, s
  mark     anything else worth a timestamp
  error    something failed that the user saw as "something broke" (where)

Two optional labels ride on every record, for the public bot's monitor
(scale-up/monitor.py): ``tag(bot="public")`` for the whole process, and
``bind(uid=...)`` for one thread — a classmate's worker thread, so the Canvas
calls it makes count as theirs. The private bot sets neither.

Never raises: a metric that cannot be written is dropped. AINL_METRICS=0 turns
it off (the tests do). Secrets never go in: the Telegram path carries the bot
token and is reduced to the method name; query strings are dropped.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("ainl.metrics")

SAMPLE_EVERY = float(os.environ.get("AINL_METRICS_EVERY", "10"))
_lock = threading.Lock()
_warned = False


def enabled() -> bool:
    return os.environ.get("AINL_METRICS", "1") != "0"


def _dir() -> Path:
    from ..config import home
    d = home() / "metrics"
    d.mkdir(exist_ok=True)
    return d


_tags: dict = {}
_bound = threading.local()


def tag(**fields) -> None:
    """Labels for every record this process writes (e.g. bot="public")."""
    _tags.update(fields)


def bind(**fields) -> None:
    """Labels for every record written from THIS thread (e.g. uid=chat id)."""
    _bound.fields = fields


def emit(kind: str, **fields) -> None:
    global _warned
    if not enabled():
        return
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "kind": kind,
           **_tags, **getattr(_bound, "fields", {}), **fields}
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _lock, open(_dir() / (datetime.now().strftime("%Y%m%d") + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:                      # noqa: BLE001 — metrics never break the bot
        if not _warned:
            _warned = True
            log.warning("metrics not written: %s", e)


# -- HTTP: one hook for every httpx client ------------------------------------

_ID = re.compile(r"/\d+(?=/|$)")


def _svc_op(url) -> tuple[str, str]:
    host = url.host or ""
    path = url.path or ""
    if "api.telegram.org" in host:
        # /bot<TOKEN>/sendMessage or /file/bot<TOKEN>/… — keep only the method
        tail = path.rsplit("/", 1)[-1]
        return "telegram", ("file" if "/file/" in path else tail)
    if "deepseek" in host:
        return "deepseek", path
    if "instructure" in host or "canvas" in host:
        return "canvas", _ID.sub("/:id", path)
    return host.split(".")[-2] if "." in host else host, _ID.sub("/:id", path)


def _on_request(request) -> None:
    request.extensions["ainl_t0"] = time.monotonic()


def _on_response(response) -> None:
    try:
        # The hook runs when the HEADERS arrive. A reasoning model sends them at
        # once and the body only when it has finished thinking: DeepSeek looked
        # like 0.9 s while the sweep sat waiting for it (2026-09-25). Read the
        # body here (the clients read it anyway) so the time is the whole call.
        if response.request.method != "GET" or "api.telegram.org" not in str(response.request.url.host):
            response.read()
        req = response.request
        t0 = req.extensions.get("ainl_t0")
        svc, op = _svc_op(req.url)
        rec = {"svc": svc, "op": op, "method": req.method, "status": response.status_code,
               "ms": round((time.monotonic() - t0) * 1000) if t0 else None}
        rl = response.headers.get("X-Rate-Limit-Remaining")
        if rl is not None:
            rec["rate_left"] = rl
            rec["cost"] = response.headers.get("X-Request-Cost")
        emit("http", **rec)
    except Exception:                           # noqa: BLE001
        pass


def hooks() -> dict:
    """event_hooks= for httpx.Client."""
    return {"request": [_on_request], "response": [_on_response]} if enabled() else {}


# -- resource samples -----------------------------------------------------------

_PROFILE = re.compile(r"--user-data-dir=(\S+)")
_UNIT = {"B": 1 / 2**20, "K": 1 / 1024, "M": 1, "G": 1024}


def _footprints() -> dict[int, float]:
    """pid -> memory footprint in MB, as Activity Monitor counts it (macOS).

    Summing Chrome's RSS counts shared pages once per process — 3.0 GB where
    macOS says 2.0 (2026-09-25). Reading the real footprint of another
    process needs privileges psutil does not have; `top` has them. ~0.3 s."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return {}
    try:
        out = subprocess.run(["/usr/bin/top", "-l", "1", "-stats", "pid,mem"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:                            # noqa: BLE001
        return {}
    fp = {}
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+([\d.]+)([BKMG])\+?\s*$", line)
        if m:
            fp[int(m.group(1))] = float(m.group(2)) * _UNIT[m.group(3)]
    return fp


def snapshot() -> dict:
    """Bot process, browsers grouped by profile, and the machine — now."""
    import psutil
    me = psutil.Process()
    with me.oneshot():
        bot = {"rss_mb": round(me.memory_info().rss / 2**20, 1), "cpu": me.cpu_percent(None),
               "threads": me.num_threads()}
    bot["footprint_mb"] = None
    browsers: dict[str, dict] = {}
    fp = _footprints()
    for p in psutil.process_iter(["pid", "cmdline", "memory_info", "cpu_percent"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            m = _PROFILE.search(cmd)
            if not m or "chrome-profile" not in m.group(1):
                continue
            prof = m.group(1)
            # ~/.ainl/chrome-profile is the owner's; ~/.ainl/users/<id>/chrome-profile a pooled user's
            key = "owner" if "/users/" not in prof else "user:" + prof.split("/users/")[1].split("/")[0]
            b = browsers.setdefault(key, {"procs": 0, "rss_mb": 0.0, "footprint_mb": 0.0, "cpu": 0.0,
                                          "renderers": 0})
            b["procs"] += 1
            b["footprint_mb"] += fp.get(p.info["pid"], 0.0)
            b["renderers"] += "--type=renderer" in cmd
            b["rss_mb"] += (p.info["memory_info"].rss if p.info["memory_info"] else 0) / 2**20
            b["cpu"] += p.info["cpu_percent"] or 0.0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for b in browsers.values():
        b["rss_mb"] = round(b["rss_mb"], 1)
        b["footprint_mb"] = round(b["footprint_mb"], 1)
        b["cpu"] = round(b["cpu"], 1)
    bot["footprint_mb"] = round(fp.get(os.getpid(), 0.0), 1) or None
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    return {"bot": bot, "browsers": browsers,
            "machine": {"mem_used_mb": round((vm.total - vm.available) / 2**20),
                        "mem_total_mb": round(vm.total / 2**20), "mem_pct": vm.percent,
                        "load1": round(os.getloadavg()[0], 2),
                        "net_sent_mb": round(net.bytes_sent / 2**20, 1),
                        "net_recv_mb": round(net.bytes_recv / 2**20, 1)}}


_sampler: Optional[threading.Thread] = None


def start_sampler(every: float = SAMPLE_EVERY) -> None:
    global _sampler
    if _sampler is not None or not enabled():
        return

    def loop():
        import psutil
        psutil.Process().cpu_percent(None)       # prime the CPU counters
        while True:
            try:
                emit("sample", **snapshot())
            except Exception as e:               # noqa: BLE001
                log.debug("sample failed: %s", e)
            time.sleep(every)

    _sampler = threading.Thread(target=loop, name="metrics-sampler", daemon=True)
    _sampler.start()


# -- reading back -----------------------------------------------------------------

def read(since: Optional[datetime] = None, until: Optional[datetime] = None) -> list[dict]:
    out = []
    d = _dir()
    for f in sorted(d.glob("*.jsonl")):
        if since and f.stem < since.strftime("%Y%m%d"):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = datetime.fromisoformat(r["ts"])
            if since and t < since:
                continue
            if until and t > until:
                continue
            out.append(r)
    return out


def pct(values: list[float], p: float) -> Optional[float]:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = min(len(v) - 1, max(0, round(p / 100 * (len(v) - 1))))
    return v[k]
