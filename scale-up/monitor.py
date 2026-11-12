"""What the Mac mini is doing right now: memory, processes, people — for the owner.

Every classmate who connects costs a headless Chromium, and a Chromium costs
hundreds of MB, mostly in its renderer/GPU/utility children rather than the
process that has the port. On a 24 GB machine that runs out quietly: macOS
swaps, everything slows, and the first sign used to be a classmate saying the
bot went silent. This module turns that into numbers the owner can watch from
their phone (the /monitor page, served by viewer.py) and a message when memory
crosses a line (``MemoryAlert``).

    snapshot()      system + process groups + people + counts, as one JSON-able dict
    text_summary()  the same, compact, for Telegram (/monitor text)
    MemoryAlert     "memory is high" once per crossing, "back to normal" once

Where each number comes from:

    system          psutil (RAM, swap, CPU, load)
    process groups  the process table, grouped by what each process IS (its
                    command line) and then by tree: a Chromium child inherits
                    its browser's group, so a group's MB is the whole browser
    people          registry (who) + BrowserPool/Dispatcher/LoginServer stats()
                    (what is live now) + ~/.ainl/metrics (what they did today)

What never leaves this module: command lines (cloudflared's can carry its
tunnel token), message text, cookies, tokens. People are their Telegram
display name and chat id — the same as /users shows.

The process table, footprints, clock and metrics directory are injected, so
the grouping and the counting are unit-tested with fakes: no real processes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger("ainl.scaleup.monitor")

MB = 2 ** 20
WARN_PCT = 80.0
CRITICAL_PCT = 90.0
ACTIVE_WINDOW_S = 15 * 60
# Before any classmate browser has run there is no average to go by; this is
# roughly one idle headless Chromium with a Canvas tab (2026-09 measurements).
DEFAULT_BROWSER_MB = 450.0

_PORT = re.compile(r"^--remote-debugging-port=(\d+)$")
_TYPE = re.compile(r"^--type=([\w-]+)$")


def level_of(pct: float) -> str:
    return "critical" if pct >= CRITICAL_PCT else "warn" if pct >= WARN_PCT else "ok"


# --------------------------------------------------------------------------- #
# The process table (real default; tests pass a list of dicts)
# --------------------------------------------------------------------------- #

def real_procs() -> list[dict]:
    """Every process: pid, ppid, name, cmdline, rss (bytes), cpu, created, threads.

    psutil caches Process objects across process_iter() calls, so cpu_percent
    is "since the last snapshot" — the first one reads 0, which is fine.
    """
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "memory_info",
                                  "cpu_percent", "create_time", "num_threads"]):
        i = p.info
        mi = i.get("memory_info")
        out.append({"pid": i["pid"], "ppid": i.get("ppid") or 0, "name": i.get("name") or "",
                    "cmdline": i.get("cmdline") or [], "rss": mi.rss if mi else 0,
                    "cpu": i.get("cpu_percent") or 0.0, "created": i.get("create_time") or 0.0,
                    "threads": i.get("num_threads") or 0})
    return out


def real_system() -> dict:
    import psutil
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {"total": vm.total, "available": vm.available, "swap_used": sw.used,
            "swap_total": sw.total, "cpu": psutil.cpu_percent(None),
            "load": list(os.getloadavg()), "cpus": psutil.cpu_count() or 1}


def real_footprints() -> dict[int, float]:
    """pid -> MB as Activity Monitor counts it (macOS only; {} elsewhere).

    RSS counts a shared page once per process, which overstates Chromium by
    about half; the footprint doesn't. Reading it needs `top` (~0.3 s), so the
    Monitor refreshes it less often than the rest."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from ainl.util.metrics import _footprints
        return _footprints()
    except Exception:                          # noqa: BLE001 — RSS is the fallback
        return {}


# --------------------------------------------------------------------------- #
# Grouping: what each process is
# --------------------------------------------------------------------------- #

def _arg(cmd: list[str], prefix: str) -> Optional[str]:
    for a in cmd:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None


def classify(cmd: list[str], name: str = "", port_owner: Optional[dict] = None) -> Optional[str]:
    """The group a process belongs to by its OWN command line, or None.

    ``port_owner`` maps a pooled browser's port to its chat id, for a browser
    whose profile path doesn't say (it always does today; ports are the
    fallback). A pooled-looking browser nobody owns is an orphan — the kind a
    crash leaves behind, holding memory until someone notices.
    """
    joined = " ".join(cmd)
    base = os.path.basename(cmd[0]) if cmd else name
    if "cloudflared" in (base, name):
        return "cloudflared"
    if "playwright" in joined and "run-driver" in joined:
        return "playwright"
    if "python" in base.lower() or "python" in name.lower():
        if any(a.endswith("scale-up/bot.py") for a in cmd):
            return "public-bot"
        if "ainl.bot" in cmd and "-m" in cmd:
            return "private-bot"
        if any(a.endswith("browser_start.py") for a in cmd):
            return "owner-browser"            # the launcher of the 9222 window
        return None
    profile = _arg(cmd, "--user-data-dir=")
    port = _arg(cmd, "--remote-debugging-port=")
    if profile is None and port is None:
        return None
    if profile and "/users/" in profile and profile.rstrip("/").endswith("chrome-profile"):
        uid = profile.split("/users/")[1].split("/")[0]
        return f"user:{uid}"
    if port and port.isdigit():
        p = int(port)
        if p == 9222:
            return "owner-browser"
        if port_owner and p in port_owner:
            return f"user:{port_owner[p]}"
        if 9300 <= p < 9400:
            return f"orphan:{p}"
    if profile and profile.rstrip("/").endswith("chrome-profile"):
        return "owner-browser"                # ~/.ainl/chrome-profile
    return None


def _part(cmd: list[str], name: str, kind: str, itself: bool = True) -> str:
    """What this one process is inside its group: Chromium's --type, or its role."""
    t = _arg(cmd, "--type=")
    if t:
        return t
    if any(a.endswith("browser_start.py") for a in cmd):
        return "launcher"
    if kind.startswith(("user:", "orphan:")) or kind == "owner-browser":
        return "browser" if _arg(cmd, "--remote-debugging-port=") or _arg(cmd, "--user-data-dir=") \
            else (name or "helper")
    if itself and kind in ("public-bot", "private-bot", "cloudflared"):
        return "main"
    return name or "child"


def group_processes(procs: Iterable[dict], footprints: Optional[dict] = None,
                    port_owner: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """{group key: {procs, rss_mb, fp_mb, cpu, uptime_s, parts{part: {n, mb}}}} + "other".

    Each process is first classified by its own command line. One that says
    nothing about itself (a renderer launched without our flags, a `top` the bot
    ran) takes the group of its nearest classified ancestor; a Playwright driver
    is its own group, named after the bot that started it. Everything else is
    "other", kept only as a total plus the biggest few by name.
    """
    procs = list(procs)
    footprints = footprints or {}
    now = time.time() if now is None else now
    by_pid = {p["pid"]: p for p in procs}
    own = {p["pid"]: classify(p.get("cmdline") or [], p.get("name", ""), port_owner) for p in procs}

    def ancestor_group(pid: int) -> Optional[str]:
        seen = set()
        p = by_pid.get(pid)
        while p is not None and p["pid"] not in seen:
            seen.add(p["pid"])
            ppid = p.get("ppid") or 0
            if ppid in (0, 1) or ppid == p["pid"]:
                return None
            g = own.get(ppid)
            if g is not None and g != "playwright":
                return g
            p = by_pid.get(ppid)
        return None

    groups: dict[str, dict] = {}
    other = {"procs": 0, "rss_mb": 0.0, "fp_mb": 0.0, "top": {}}
    for p in procs:
        pid = p["pid"]
        g = own[pid]
        if g == "playwright":
            g = "playwright:" + (ancestor_group(pid) or "other")
        elif g is None:
            g = ancestor_group(pid)
        rss = (p.get("rss") or 0) / MB
        fp = footprints.get(pid)
        if g is None:
            other["procs"] += 1
            other["rss_mb"] += rss
            other["fp_mb"] += fp if fp is not None else rss
            nm = p.get("name") or "?"
            other["top"][nm] = other["top"].get(nm, 0.0) + (fp if fp is not None else rss)
            continue
        e = groups.setdefault(g, {"procs": 0, "rss_mb": 0.0, "fp_mb": 0.0, "cpu": 0.0,
                                  "started": None, "threads": 0, "parts": {}})
        e["procs"] += 1
        e["rss_mb"] += rss
        e["fp_mb"] += fp if fp is not None else rss
        e["cpu"] += p.get("cpu") or 0.0
        e["threads"] += p.get("threads") or 0
        created = p.get("created") or None
        if created and (e["started"] is None or created < e["started"]):
            e["started"] = created
        part = _part(p.get("cmdline") or [], p.get("name", ""), g, own[pid] is not None)
        pe = e["parts"].setdefault(part, {"n": 0, "mb": 0.0})
        pe["n"] += 1
        pe["mb"] += fp if fp is not None else rss

    for e in groups.values():
        e["rss_mb"] = round(e["rss_mb"], 1)
        e["fp_mb"] = round(e["fp_mb"], 1)
        e["cpu"] = round(e["cpu"], 1)
        e["uptime_s"] = round(now - e.pop("started")) if e.get("started") else None
        for pe in e["parts"].values():
            pe["mb"] = round(pe["mb"], 1)
    top = sorted(other["top"].items(), key=lambda kv: -kv[1])[:6]
    other = {"procs": other["procs"], "rss_mb": round(other["rss_mb"], 1),
             "fp_mb": round(other["fp_mb"], 1),
             "top": [{"name": n[:40], "mb": round(v, 1)} for n, v in top]}
    return {"groups": groups, "other": other}


def _label(key: str, names: dict) -> str:
    fixed = {"public-bot": "Public bot (scale-up/bot.py)", "private-bot": "Private bot (ainl.bot)",
             "owner-browser": "Owner's browser :9222", "cloudflared": "Cloudflare tunnel"}
    if key in fixed:
        return fixed[key]
    if key.startswith("user:"):
        uid = key[5:]
        # Not in the registry: someone who /forget-ed, or a dev user, whose
        # browser outlived them.
        return f"Browser · {names[uid]}" if uid in names else f"Browser · {uid} (not registered)"
    if key.startswith("orphan:"):
        return f"Orphan browser :{key[7:]} (nobody's)"
    if key.startswith("playwright:"):
        parent = key[11:]
        return "Playwright driver · " + (_label(parent, names) if parent != "other" else "?")
    return key


# --------------------------------------------------------------------------- #
# Counting from ~/.ainl/metrics, incrementally
# --------------------------------------------------------------------------- #

_SUBMIT_OP = re.compile(r"/assignments/:id/submissions$|/discussion_topics/:id/entries$")


@dataclass
class _Ev:
    t: float
    kind: str           # update | canvas | submission | error
    bot: str            # public | private
    uid: str


class MetricsTail:
    """Today's events from the metrics file, read only as far as it has grown.

    The file gets a sample every 10 s and a line per HTTP call; re-parsing the
    whole day on every 3-second poll would cost more than everything else here.
    So this keeps a byte offset and only the few fields counting needs.
    """

    def __init__(self, directory: Optional[Path] = None,
                 today: Callable[[], str] = lambda: datetime.now().strftime("%Y%m%d")) -> None:
        self._dir = directory
        self._today = today
        self._day = ""
        self._offset = 0
        self._partial = b""
        self.events: list[_Ev] = []
        self._lock = threading.Lock()

    def _path(self) -> Optional[Path]:
        d = self._dir
        if d is None:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
                from ainl.config import home
                d = home() / "metrics"
            except Exception:                  # noqa: BLE001
                return None
        return Path(d) / f"{self._today()}.jsonl"

    def refresh(self) -> list[_Ev]:
        with self._lock:
            day = self._today()
            if day != self._day:
                self._day, self._offset, self._partial, self.events = day, 0, b"", []
            path = self._path()
            if path is None or not path.exists():
                return self.events
            try:
                with open(path, "rb") as f:
                    f.seek(self._offset)
                    chunk = f.read()
            except OSError:
                return self.events
            self._offset += len(chunk)
            data = self._partial + chunk
            lines = data.split(b"\n")
            self._partial = lines.pop()        # an unfinished last line, kept for next time
            for line in lines:
                ev = self._parse(line)
                if ev is not None:
                    self.events.append(ev)
            return self.events

    @staticmethod
    def _parse(line: bytes) -> Optional[_Ev]:
        if not line.strip() or b'"sample"' in line[:80]:
            return None                        # most of the file; nothing to count
        try:
            r = json.loads(line)
            kind = r.get("kind")
            if kind not in ("update", "http", "error"):
                return None
            t = datetime.fromisoformat(r["ts"]).timestamp()
        except (ValueError, KeyError, TypeError):
            return None
        bot = r.get("bot") or "private"
        uid = str(r.get("uid") or "")
        if kind == "http":
            if r.get("svc") != "canvas":
                if isinstance(r.get("status"), int) and r["status"] >= 500 and r.get("svc") != "telegram":
                    return _Ev(t, "error", bot, uid)
                return None
            status = r.get("status") if isinstance(r.get("status"), int) else 0
            if status >= 500:
                return _Ev(t, "error", bot, uid)
            if (r.get("method") == "POST" and status < 400 and _SUBMIT_OP.search(r.get("op") or "")):
                return _Ev(t, "submission", bot, uid)
            return _Ev(t, "canvas", bot, uid)
        return _Ev(t, kind, bot, uid)


def count(events: Iterable[_Ev], since: float) -> dict:
    """{updates, canvas, submissions, errors} for events at or after ``since``.
    A submission is also a Canvas request."""
    c = {"updates": 0, "updates_public": 0, "canvas": 0, "submissions": 0, "errors": 0}
    for e in events:
        if e.t < since:
            continue
        if e.kind == "update":
            c["updates"] += 1
            c["updates_public"] += e.bot == "public"
        elif e.kind == "canvas":
            c["canvas"] += 1
        elif e.kind == "submission":
            c["canvas"] += 1
            c["submissions"] += 1
        elif e.kind == "error":
            c["errors"] += 1
    return c


# --------------------------------------------------------------------------- #
# The monitor: everything together, cached
# --------------------------------------------------------------------------- #

def _call(f, *a, default=None):
    try:
        return f(*a) if f is not None else default
    except Exception:                          # noqa: BLE001 — a monitor never breaks the bot
        log.debug("monitor source failed", exc_info=True)
        return default


class Monitor:
    """One snapshot every ``ttl`` seconds at most, however many pages poll.

    ``pool``, ``dispatcher`` and ``viewer`` are the live objects (each has a
    read-only ``stats()``); any may be None. ``viewer`` is usually set after
    construction, because the viewer is built with this monitor.
    """

    def __init__(self, reg=None, pool=None, dispatcher=None, viewer=None, *,
                 connected: Optional[Callable[[str], bool]] = None,
                 school_of: Optional[Callable[[str], object]] = None,
                 procs: Callable[[], list] = real_procs,
                 system: Callable[[], dict] = real_system,
                 footprints: Callable[[], dict] = real_footprints,
                 metrics: Optional[MetricsTail] = None,
                 clock: Callable[[], float] = time.time,
                 ttl: float = 2.5, footprint_ttl: float = 10.0) -> None:
        self.reg, self.pool, self.dispatcher, self.viewer = reg, pool, dispatcher, viewer
        self.connected, self.school_of = connected, school_of
        self._procs, self._system, self._footprints = procs, system, footprints
        self.metrics = metrics if metrics is not None else MetricsTail()
        self._clock = clock
        self._ttl, self._fp_ttl = ttl, footprint_ttl
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None
        self._cache_t = -1e18
        self._fp: dict = {}
        self._fp_t = -1e18

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._cache is None or now - self._cache_t >= self._ttl:
                self._cache = self._collect(now)
                self._cache_t = now
            return self._cache

    def json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False)

    # -- collecting ----------------------------------------------------------

    def _collect(self, now: float) -> dict:
        pool_stats = _call(getattr(self.pool, "stats", None), default={}) or {}
        disp_stats = _call(getattr(self.dispatcher, "stats", None), default={}) or {}
        view_stats = _call(getattr(self.viewer, "stats", None), default={}) or {}
        users = _call(getattr(self.reg, "all", None), default=[]) or []
        names = {u.chat_id: u.name for u in users}
        port_owner = {s["port"]: cid for cid, s in pool_stats.items() if "port" in s}

        if now - self._fp_t >= self._fp_ttl:
            self._fp = _call(self._footprints, default={}) or {}
            self._fp_t = now
        grouped = group_processes(_call(self._procs, default=[]) or [], self._fp, port_owner, now)
        groups = grouped["groups"]
        sysinfo = self._system_block(_call(self._system, default={}) or {}, groups)

        events = _call(self.metrics.refresh, default=[]) or []
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0,
                                                       microsecond=0).timestamp()
        per_uid: dict[str, dict] = {}
        for e in events:
            if not e.uid or e.t < midnight:
                continue
            d = per_uid.setdefault(e.uid, {"messages": 0, "canvas": 0, "submissions": 0,
                                           "errors": 0, "last": 0.0})
            if e.kind == "update":
                d["messages"] += 1
                d["last"] = max(d["last"], e.t)
            elif e.kind in ("canvas", "submission"):
                d["canvas"] += 1
                d["submissions"] += e.kind == "submission"
            elif e.kind == "error":
                d["errors"] += 1

        people = []
        for u in users:
            cid = u.chat_id
            b = groups.get(f"user:{cid}")
            ps, ds, vs = pool_stats.get(cid), disp_stats.get(cid), view_stats.get(cid)
            today = per_uid.get(cid, {})
            last = today.get("last") or None
            active = bool(vs) or bool(ds and ds.get("queue")) or \
                (last is not None and now - last <= ACTIVE_WINDOW_S)
            school = _call(self.school_of, cid)
            people.append({
                "id": cid, "name": u.name, "school": getattr(school, "name", None),
                "approved": bool(u.approved),
                "connected": bool(_call(self.connected, cid, default=False)),
                # held=False: a browser of theirs is running but the pool doesn't
                # know it (left over from before a bot restart) — the reaper
                # will never stop it.
                "browser": ({"port": (ps or {}).get("port"), "idle_s": (ps or {}).get("idle_s"),
                             "mb": b["fp_mb"] if b else None, "procs": b["procs"] if b else 0,
                             "held": ps is not None}
                            if ps or b else None),
                "viewer_open": bool(vs), "assistant": bool(ds and ds.get("assistant")),
                "queue": int(ds.get("queue", 0)) if ds else 0,
                "last_active": last, "active": active,
                "today": {k: today.get(k, 0) for k in ("messages", "canvas", "submissions", "errors")},
            })
        people.sort(key=lambda p: (not p["active"], not p["connected"], -(p["last_active"] or 0),
                                   p["name"].lower()))

        group_list = [{"key": k, "label": _label(k, names), "mb": g["fp_mb"], "rss_mb": g["rss_mb"],
                       "procs": g["procs"], "cpu": g["cpu"], "threads": g["threads"],
                       "uptime_s": g["uptime_s"],
                       "parts": sorted(({"part": p, **v} for p, v in g["parts"].items()),
                                       key=lambda x: -x["mb"])}
                      for k, g in groups.items()]
        group_list.sort(key=lambda g: -g["mb"])

        totals = {
            "registered": len(users), "approved": sum(u.approved for u in users),
            "connected": sum(p["connected"] for p in people),
            "active_15m": sum(p["active"] for p in people),
            "browsers": sum(1 for k in groups if k.startswith("user:")),
            "orphans": sum(1 for k in groups if k.startswith("orphan:")),
            "viewers": len(view_stats), "assistants": sum(p["assistant"] for p in people),
            "queued": sum(p["queue"] for p in people),
            "hour": count(events, now - 3600), "today": count(events, midnight),
        }
        return {"at": now, "system": sysinfo, "groups": group_list, "other": grouped["other"],
                "people": people, "totals": totals}

    @staticmethod
    def _system_block(s: dict, groups: dict) -> dict:
        total = s.get("total") or 0
        avail = s.get("available") or 0
        used = max(0, total - avail)
        pct = round(100.0 * used / total, 1) if total else 0.0
        browsers = [g["fp_mb"] for k, g in groups.items() if k.startswith(("user:", "orphan:"))]
        per_browser = (sum(browsers) / len(browsers)) if browsers else \
            (groups["owner-browser"]["fp_mb"] if "owner-browser" in groups else DEFAULT_BROWSER_MB)
        per_browser = max(per_browser, 50.0)
        # Room = what can be added before memory reaches the warning line.
        headroom = WARN_PCT / 100 * total / MB - used / MB
        return {"total_mb": round(total / MB), "used_mb": round(used / MB),
                "available_mb": round(avail / MB), "pct": pct, "level": level_of(pct),
                "swap_used_mb": round((s.get("swap_used") or 0) / MB),
                "swap_total_mb": round((s.get("swap_total") or 0) / MB),
                "cpu": s.get("cpu"), "load": [round(x, 2) for x in (s.get("load") or [])],
                "cpus": s.get("cpus"),
                "per_browser_mb": round(per_browser),
                "room_browsers": max(0, int(headroom // per_browser)),
                "warn_pct": WARN_PCT, "critical_pct": CRITICAL_PCT}


# --------------------------------------------------------------------------- #
# Text, for Telegram
# --------------------------------------------------------------------------- #

def _esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ago(t: Optional[float], now: float) -> str:
    if not t:
        return "–"
    s = max(0, now - t)
    return f"{int(s)}s" if s < 60 else f"{int(s // 60)}m" if s < 3600 else f"{s / 3600:.1f}h"


def _gb(mb) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def top_consumers(snap: dict, n: int = 4) -> list[str]:
    return [f"{g['label']}: {_gb(g['mb'])}" for g in snap["groups"][:n]]


def text_summary(snap: dict, max_people: int = 12) -> str:
    """/monitor text — HTML for Telegram, short enough to read on a phone."""
    s, t = snap["system"], snap["totals"]
    icon = {"ok": "🟢", "warn": "🟠", "critical": "🔴"}[s["level"]]
    lines = [f"{icon} <b>Memory {s['pct']:.0f}%</b> — {_gb(s['used_mb'])} of {_gb(s['total_mb'])}, "
             f"swap {_gb(s['swap_used_mb'])}",
             f"CPU {s['cpu'] or 0:.0f}% · load {' '.join(f'{x:g}' for x in s['load'][:3])}",
             f"Room for ~{s['room_browsers']} more browsers (≈{s['per_browser_mb']} MB each)",
             "",
             f"<b>People</b> {t['registered']} registered · {t['approved']} approved · "
             f"{t['connected']} connected · {t['active_15m']} active · {t['browsers']} browsers"
             + (f" · ⚠️ {t['orphans']} orphan" if t["orphans"] else "")]
    now = snap["at"]
    for p in snap["people"][:max_people]:
        bits = []
        if p["browser"]:
            bits.append(f"🌐{_gb(p['browser']['mb'] or 0)}")
        if p["viewer_open"]:
            bits.append("🔐login")
        if p["queue"]:
            bits.append(f"⏳{p['queue']}")
        d = p["today"]
        if d["messages"] or d["submissions"]:
            bits.append(f"{d['messages']} msg, {d['submissions']} sub")
        mark = "🟢" if p["active"] else ("✅" if p["connected"] else ("☑️" if p["approved"] else "⏳"))
        lines.append(f"{mark} {_esc(p['name'])} · {_ago(p['last_active'], now)} {' '.join(bits)}".rstrip())
    lines += ["", "<b>Processes</b>"]
    for g in snap["groups"][:10]:
        lines.append(f"· {_esc(g['label'])}: {_gb(g['mb'])} ({g['procs']} proc)")
    o = snap["other"]
    lines.append(f"· everything else: {_gb(o['fp_mb'])} ({o['procs']} proc)")
    h, d = t["hour"], t["today"]
    lines += ["", f"<b>Last hour</b> {h['updates']} updates · {h['canvas']} Canvas · "
                  f"{h['submissions']} submitted · {h['errors']} errors",
              f"<b>Today</b> {d['updates']} updates · {d['canvas']} Canvas · "
              f"{d['submissions']} submitted · {d['errors']} errors"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #

class MemoryAlert:
    """Tell the owner once when memory crosses a line, once when it's back.

    Rising: a message on entering "warn", another on entering "critical".
    Falling: the level only drops once memory is ``hysteresis`` points below
    the line, so hovering at 80% doesn't flap; and "back to normal" is sent
    only if something was said about it. The same level is not announced
    again within ``cooldown`` seconds of the last time it was.
    """

    ORDER = {"ok": 0, "warn": 1, "critical": 2}

    def __init__(self, *, hysteresis: float = 5.0, cooldown: float = 30 * 60,
                 clock: Callable[[], float] = time.time) -> None:
        self.level = "ok"
        self.hysteresis, self.cooldown, self._clock = hysteresis, cooldown, clock
        self._said: dict[str, float] = {}
        self._alerted = False

    def _settle(self, pct: float) -> str:
        """The level after hysteresis: up at once, down only with a margin."""
        raw = level_of(pct)
        if self.ORDER[raw] >= self.ORDER[self.level]:
            return raw
        lower = level_of(pct + self.hysteresis)       # still near the line: stay one up
        return lower if self.ORDER[lower] < self.ORDER[self.level] else self.level

    def check(self, snap: dict) -> Optional[str]:
        s = snap["system"]
        new = self._settle(s["pct"])
        old, self.level = self.level, new
        now = self._clock()
        if self.ORDER[new] > self.ORDER[old]:
            if now - self._said.get(new, -1e18) < self.cooldown:
                return None
            self._said[new] = now
            self._alerted = True
            icon = "🔴" if new == "critical" else "🟠"
            top = "\n".join("· " + _esc(x) for x in top_consumers(snap))
            return (f"{icon} <b>Memory {s['pct']:.0f}%</b> ({_gb(s['used_mb'])} of {_gb(s['total_mb'])}, "
                    f"swap {_gb(s['swap_used_mb'])}) — room for ~{s['room_browsers']} more browsers.\n"
                    f"Top:\n{top}\n\n/monitor for the live page.")
        if new == "ok" and old != "ok" and self._alerted:
            self._alerted = False
            return f"🟢 Memory back to {s['pct']:.0f}% ({_gb(s['used_mb'])} of {_gb(s['total_mb'])})."
        return None


def start_alerts(monitor: Monitor, send: Callable[[str], None], *, every: float = 60,
                 alert: Optional[MemoryAlert] = None) -> threading.Event:
    """Check every ``every`` seconds on a daemon thread; set the returned event to stop."""
    alert = alert or MemoryAlert()
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            try:
                msg = alert.check(monitor.snapshot())
                if msg:
                    send(msg)
            except Exception:                  # noqa: BLE001 — try again next minute
                log.exception("memory check failed")

    threading.Thread(target=loop, name="memory-alerts", daemon=True).start()
    return stop


if __name__ == "__main__":
    # A one-off look at this machine: python scale-up/monitor.py [--json]
    # (outside the bot: people come from the registry only, nothing live)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from registry import Registry
    snap = Monitor(Registry()).snapshot()
    if "--json" in sys.argv:
        print(json.dumps(snap, indent=1))
    else:
        import html
        print(html.unescape(re.sub(r"</?b>", "", text_summary(snap))))
