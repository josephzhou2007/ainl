"""monitor.py with a fake process table, fake live objects and a tmp metrics
dir; the owner-only /monitor.json gate on a real LoginServer (port 0) with
signed and forged initData. No real processes, no network beyond localhost."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest

import initdata
import monitor
from monitor import MemoryAlert, MetricsTail, Monitor, classify, group_processes, text_summary
from viewer import LoginServer

MB = 2 ** 20
TOKEN = "123456:TEST-token"
OWNER = "4242"
H = "/Users/x/.ainl"


def P(pid, ppid, cmd, rss_mb=10, name=None, cpu=1.0, created=1000.0):
    return {"pid": pid, "ppid": ppid, "cmdline": cmd, "name": name or (cmd[0].rsplit("/", 1)[-1] if cmd else "?"),
            "rss": rss_mb * MB, "cpu": cpu, "created": created, "threads": 3}


CHROME = "/Applications/Chromium.app/Contents/MacOS/Chromium"
HELPER = "/Applications/Chromium.app/Contents/Frameworks/Chromium Helper (Renderer)"


def fake_table():
    return [
        P(1, 0, ["/sbin/launchd"], name="launchd"),
        P(10, 1, [f"{H}/../.venv/bin/python", "scale-up/bot.py"], 60),
        P(11, 10, ["/x/playwright/driver/node", "/x/playwright/driver/package/cli.js", "run-driver"], 90, name="node"),
        P(12, 10, ["/usr/bin/top", "-l", "1"], 5),                          # a child of the bot
        P(15, 1, ["/x/.venv/bin/python3.12", "-m", "ainl.bot"], 50),
        # a classmate's browser: root by profile, children with and without flags
        P(20, 1, [CHROME, "--remote-debugging-port=9300", f"--user-data-dir={H}/users/123/chrome-profile"], 100, created=500),
        P(21, 20, [HELPER, "--type=renderer"], 200),
        P(22, 20, [CHROME, "--type=gpu-process", f"--user-data-dir={H}/users/123/chrome-profile"], 80),
        P(23, 21, ["/some/child"], 7),                                     # a grandchild
        # a browser known only by its port (the pool says whose)
        P(30, 1, [CHROME, "--remote-debugging-port=9301", "--user-data-dir=/tmp/elsewhere"], 90),
        P(31, 30, [HELPER, "--type=renderer"], 150),
        # a pooled-looking browser nobody holds
        P(40, 1, [CHROME, "--remote-debugging-port=9307"], 70),
        # the owner's window on 9222 and its launcher
        P(50, 1, [CHROME, "--remote-debugging-port=9222", f"--user-data-dir={H}/chrome-profile"], 120),
        P(51, 50, [HELPER, "--type=renderer", f"--user-data-dir={H}/chrome-profile"], 300),
        P(52, 1, ["/x/python", "scripts/browser_start.py"], 15),
        P(60, 1, ["/opt/homebrew/bin/cloudflared", "tunnel", "run", "--token", "SECRET-TUNNEL-TOKEN"], 25),
        P(70, 1, ["/System/Finder.app/Finder"], 400, name="Finder"),
        P(71, 1, ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"], 900, name="Google Chrome"),
    ]


def test_classify_by_own_command_line():
    assert classify(["/x/python", "scale-up/bot.py"]) == "public-bot"
    assert classify(["/x/python3", "-m", "ainl.bot"]) == "private-bot"
    assert classify([CHROME, f"--user-data-dir={H}/users/77/chrome-profile"]) == "user:77"
    assert classify([CHROME, "--remote-debugging-port=9301"], port_owner={9301: "55"}) == "user:55"
    assert classify([CHROME, "--remote-debugging-port=9222"]) == "owner-browser"
    assert classify([CHROME, "--remote-debugging-port=9310"]) == "orphan:9310"
    assert classify(["/opt/cloudflared", "tunnel"]) == "cloudflared"
    # the owner's everyday Chrome, without our flags, is not ours
    assert classify(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]) is None
    assert classify(["/x/python", "other.py"]) is None


def test_process_trees_are_summed_per_browser_and_attributed_by_profile_or_port():
    out = group_processes(fake_table(), port_owner={9301: "456", 9300: "123"}, now=2000.0)
    g = out["groups"]
    u = g["user:123"]
    assert u["procs"] == 4 and u["rss_mb"] == 100 + 200 + 80 + 7        # browser + renderer + gpu + grandchild
    assert u["parts"]["renderer"] == {"n": 1, "mb": 200.0}
    assert u["parts"]["gpu-process"]["mb"] == 80.0
    assert u["uptime_s"] == 1500                                         # from its oldest process
    assert g["user:456"]["procs"] == 2 and g["user:456"]["rss_mb"] == 240
    assert g["orphan:9307"]["procs"] == 1
    assert g["owner-browser"]["procs"] == 3 and g["owner-browser"]["rss_mb"] == 435
    assert "launcher" in g["owner-browser"]["parts"]
    assert g["public-bot"]["procs"] == 2 and set(g["public-bot"]["parts"]) == {"main", "top"}
    assert g["playwright:public-bot"]["rss_mb"] == 90                    # its own group, named by parent
    assert g["private-bot"]["procs"] == 1
    assert g["cloudflared"]["procs"] == 1
    other = out["other"]
    assert other["procs"] == 3                                           # launchd, Finder, everyday Chrome
    assert [t["name"] for t in other["top"]][:2] == ["Google Chrome", "Finder"]


def test_footprints_replace_rss_when_known():
    out = group_processes(fake_table(), footprints={20: 50.0, 21: 60.0, 22: 10.0, 23: 1.0}, now=2000.0)
    u = out["groups"]["user:123"]
    assert u["fp_mb"] == 121.0 and u["rss_mb"] == 387.0


# --------------------------------------------------------------------------- #
# The whole snapshot
# --------------------------------------------------------------------------- #

class FakeReg:
    def __init__(self, users):
        self._u = users

    def all(self):
        return list(self._u)


def _user(cid, name, approved=True, school="uw"):
    return SimpleNamespace(chat_id=cid, name=name, approved=approved, school=school,
                           login_id="handle_" + cid, created_at="")


def _ts(t):
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


def _metrics(tmp_path, now):
    d = tmp_path / "metrics"
    d.mkdir()
    day = datetime.fromtimestamp(now).strftime("%Y%m%d")
    recs = [
        {"ts": _ts(now - 60), "kind": "update", "bot": "public", "uid": "123", "what": "/todo"},
        {"ts": _ts(now - 50), "kind": "http", "bot": "public", "uid": "123", "svc": "canvas",
         "op": "/api/v1/courses/:id/assignments/:id/submissions", "method": "POST", "status": 201},
        {"ts": _ts(now - 40), "kind": "http", "bot": "public", "uid": "123", "svc": "canvas",
         "op": "/api/v1/users/self", "method": "GET", "status": 200},
        {"ts": _ts(now - 30), "kind": "error", "bot": "public", "uid": "123", "where": "assistant"},
        {"ts": _ts(now - 7200), "kind": "update", "bot": "public", "uid": "456", "what": "text"},
        {"ts": _ts(now - 20), "kind": "update", "what": "/stats"},              # the private bot
        {"ts": _ts(now - 10), "kind": "http", "svc": "telegram", "op": "sendMessage", "method": "POST", "status": 200},
        {"ts": _ts(now - 5), "kind": "sample", "machine": {}},
    ]
    (d / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    return d, day


def _monitor(tmp_path, now=None):
    now = now or datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    d, day = _metrics(tmp_path, now)
    reg = FakeReg([_user("123", "Ada"), _user("456", "Bob"), _user("789", "Cy", approved=False, school="")])
    pool = SimpleNamespace(stats=lambda: {"123": {"port": 9300, "idle_s": 12}, "456": {"port": 9301, "idle_s": 900}})
    disp = SimpleNamespace(stats=lambda: {"123": {"assistant": True, "queue": 2}})
    view = SimpleNamespace(stats=lambda: {"456": {"open_s": 30, "idle_s": 3, "done": False}})
    system = lambda: {"total": 24 * 1024 * MB, "available": 8 * 1024 * MB, "swap_used": 512 * MB,
                      "swap_total": 1024 * MB, "cpu": 12.5, "load": [1.0, 2.0, 3.0], "cpus": 10}
    m = Monitor(reg, pool, disp, view, connected=lambda cid: cid in ("123", "456"),
                school_of=lambda cid: SimpleNamespace(name="UW–Madison") if cid != "789" else None,
                procs=fake_table, system=system, footprints=lambda: {},
                metrics=MetricsTail(d, today=lambda: day), clock=lambda: now)
    return m, now


def test_snapshot_joins_people_with_live_state_and_todays_counts(tmp_path):
    m, now = _monitor(tmp_path)
    s = m.snapshot()
    people = {p["id"]: p for p in s["people"]}
    ada, bob, cy = people["123"], people["456"], people["789"]
    assert ada["active"] and ada["assistant"] and ada["queue"] == 2 and not ada["viewer_open"]
    assert ada["browser"]["port"] == 9300 and ada["browser"]["mb"] == 387.0
    assert ada["today"] == {"messages": 1, "canvas": 2, "submissions": 1, "errors": 1}
    assert ada["last_active"] == pytest.approx(now - 60, abs=1)
    assert ada["school"] == "UW–Madison" and ada["connected"]
    assert bob["viewer_open"] and bob["active"]                      # logging in right now
    assert bob["browser"]["mb"] == 240.0
    assert not cy["approved"] and not cy["connected"] and cy["browser"] is None and not cy["active"]
    t = s["totals"]
    assert (t["registered"], t["approved"], t["connected"], t["active_15m"], t["browsers"],
            t["orphans"], t["viewers"], t["queued"]) == (3, 2, 2, 2, 2, 1, 1, 2)
    assert t["hour"] == {"updates": 2, "updates_public": 1, "canvas": 2, "submissions": 1, "errors": 1}
    assert t["today"]["updates"] == 3
    sysb = s["system"]
    assert sysb["used_mb"] == 16 * 1024 and sysb["pct"] == pytest.approx(66.7, abs=0.1)
    assert sysb["level"] == "ok" and sysb["swap_used_mb"] == 512
    # headroom to 80%: 0.8*24576 - 16384 = 3276.8 MB, over the average classmate browser
    per = (387 + 240 + 70) / 3
    assert sysb["room_browsers"] == int(3276.8 // per)
    assert s["groups"][0]["mb"] >= s["groups"][-1]["mb"]             # biggest first
    assert any(g["label"] == "Browser · Ada" for g in s["groups"])
    assert "Ada" in text_summary(s) and "Memory 67%" in text_summary(s)


def test_nothing_secret_like_reaches_the_json(tmp_path):
    m, _ = _monitor(tmp_path)
    body = m.json()
    assert "SECRET-TUNNEL-TOKEN" not in body                   # cloudflared's command line
    assert "--user-data-dir" not in body and "chrome-profile" not in body
    assert "handle_" not in body                               # not even the Telegram @handle

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)

    bad = {"cmdline", "cookie", "cookies", "token", "password", "text", "initData", "session", "auth"}
    assert not bad & {k.lower() for k in keys(json.loads(body))}


def test_snapshot_is_cached_between_polls(tmp_path):
    calls = []
    m, now = _monitor(tmp_path)
    real = m._procs
    m._procs = lambda: calls.append(1) or real()
    t = [now]
    m._clock = lambda: t[0]
    m.snapshot(); m.snapshot()
    t[0] += 1
    m.snapshot()
    assert len(calls) == 1
    t[0] += 5
    m.snapshot()
    assert len(calls) == 2


def test_metrics_tail_reads_only_what_was_added(tmp_path):
    f = tmp_path / "20260926.jsonl"
    now = time.time()
    f.write_text(json.dumps({"ts": _ts(now), "kind": "update", "uid": "1"}) + "\n"
                 + json.dumps({"ts": _ts(now), "kind": "update", "uid": "2"})[:20])     # half a line
    tail = MetricsTail(tmp_path, today=lambda: "20260926")
    assert [e.uid for e in tail.refresh()] == ["1"]
    with open(f, "a") as fh:
        fh.write(json.dumps({"ts": _ts(now), "kind": "update", "uid": "2"})[20:] + "\n")
    assert [e.uid for e in tail.refresh()] == ["1", "2"]
    assert [e.uid for e in tail.refresh()] == ["1", "2"]                                 # nothing new


def test_browsers_the_pool_does_not_hold_still_show(tmp_path):
    # After a bot restart the pool is empty but old browsers still run.
    m, _ = _monitor(tmp_path)
    m.pool = SimpleNamespace(stats=lambda: {"999": {"port": 9301, "idle_s": 1}})
    m.reg = FakeReg([_user("123", "Ada")])
    s = m.snapshot()
    ada = s["people"][0]
    assert ada["browser"] == {"port": None, "idle_s": None, "mb": 387.0, "procs": 4, "held": False}
    labels = {g["label"] for g in s["groups"]}
    assert "Browser · 999 (not registered)" in labels    # the pool's, but nobody we know


def test_a_broken_source_does_not_break_the_snapshot(tmp_path):
    m, _ = _monitor(tmp_path)
    m.pool = SimpleNamespace(stats=lambda: 1 / 0)
    m._procs = lambda: 1 / 0
    s = m.snapshot()
    assert s["groups"] == [] and len(s["people"]) == 3


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #

def _snap(pct):
    total = 24 * 1024
    return {"system": {"pct": pct, "used_mb": total * pct / 100, "total_mb": total, "swap_used_mb": 0,
                       "room_browsers": 1},
            "groups": [{"label": "Browser · Ada", "mb": 900.0}, {"label": "Owner's browser :9222", "mb": 700.0}]}


def test_alerts_once_per_crossing_and_once_on_recovery():
    t = [0.0]
    a = MemoryAlert(clock=lambda: t[0])
    said = []
    for pct in (50, 79, 81, 85, 82, 78, 81, 92, 95, 91, 88, 86, 84, 70, 60, 60):
        t[0] += 60
        msg = a.check(_snap(pct))
        if msg:
            said.append((pct, msg))
    assert [p for p, _ in said] == [81, 92, 70]
    assert "Browser · Ada" in said[0][1] and said[1][1].startswith("🔴")
    assert "back to" in said[2][1]


def test_flapping_around_the_line_does_not_repeat_within_the_cooldown():
    t = [0.0]
    a = MemoryAlert(clock=lambda: t[0], cooldown=1800)
    said = []
    for pct in (81, 60, 81, 60, 81):                      # every 5 minutes
        t[0] += 300
        if a.check(_snap(pct)):
            said.append(pct)
    assert said == [81, 60]                               # once up, once back; then quiet
    t[0] += 3600
    assert a.check(_snap(60)) is None
    assert a.check(_snap(85)) is not None                 # a new crossing, much later


# --------------------------------------------------------------------------- #
# stats() on the live objects
# --------------------------------------------------------------------------- #

def test_pool_stats_is_port_and_idle_time(tmp_path):
    from browsers import BrowserPool
    from registry import Registry
    reg = Registry(base=tmp_path)
    reg.add("111", "a", "a")
    running = set()
    t = [100.0]
    pool = BrowserPool(reg, launch=lambda p, prof: running.add(p), alive=lambda p: p in running,
                       stop=lambda p, prof: running.discard(p), clock=lambda: t[0])
    pool.cdp_url("111")
    t[0] += 42
    assert pool.stats() == {"111": {"port": 9300, "idle_s": 42}}


def test_dispatcher_stats_is_assistant_and_queue():
    import queue
    from dispatch import Dispatcher
    d = Dispatcher(None, None, None, canvas_origin="https://x", connected=lambda c: True)
    d._convs["1"] = object()
    q = queue.Queue()
    q.put({}); q.put({})
    d._queues["2"] = q
    assert d.stats() == {"1": {"assistant": True, "queue": 0}, "2": {"assistant": False, "queue": 2}}


# --------------------------------------------------------------------------- #
# The owner-only page and endpoint
# --------------------------------------------------------------------------- #

@pytest.fixture
def mserver():
    polls = []

    def snap():
        polls.append(1)
        return json.dumps({"ok": True})

    srv = LoginServer(bot_token=TOKEN, canvas_origin="https://canvas.wisc.edu", allowed=lambda u: True,
                      cdp_url=lambda u: "", port=0, base_path="/nbl", monitor=snap, owner_id=OWNER)
    srv.start_in_thread()
    yield SimpleNamespace(srv=srv, base=f"http://127.0.0.1:{srv.port}/nbl", polls=polls)
    srv.stop()


def _init(uid, token=TOKEN, age=0):
    return initdata.sign({"auth_date": str(int(time.time() - age)),
                          "user": json.dumps({"id": int(uid), "first_name": "X"})}, token)


def _get(url, init=None):
    req = urllib.request.Request(url, headers={"X-Telegram-Init-Data": init} if init else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def test_owner_gets_the_data(mserver):
    code, body, headers = _get(mserver.base + "/monitor.json", _init(OWNER))
    assert code == 200 and json.loads(body) == {"ok": True}
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("init", [
    _init("5555"),                                   # someone else, validly signed
    _init(OWNER, token="999:another-bot"),           # forged: not our bot's signature
    _init(OWNER, age=3 * 3600),                      # the owner's, but hours old
    "user=%7B%22id%22%3A4242%7D&auth_date=1&hash=00",
    None,                                            # missing
])
def test_everyone_else_gets_403_and_no_data(mserver, init):
    code, body, _ = _get(mserver.base + "/monitor.json", init)
    assert code == 403 and "ok" not in body
    assert mserver.polls == []                       # never even collected


def test_initdata_in_the_url_does_not_count(mserver):
    from urllib.parse import quote
    code, _, _ = _get(mserver.base + "/monitor.json?initData=" + quote(_init(OWNER)))
    assert code == 403


def test_the_page_itself_is_served_with_its_own_policy(mserver):
    code, body, headers = _get(mserver.base + "/monitor")
    assert code == 200 and "monitor.json" in body and "X-Telegram-Init-Data" in body
    csp = headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp and "default-src 'none'" in csp


def test_without_a_monitor_there_is_no_route():
    srv = LoginServer(bot_token=TOKEN, canvas_origin="https://canvas.wisc.edu", allowed=lambda u: True,
                      cdp_url=lambda u: "", port=0)
    srv.start_in_thread()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        assert _get(base + "/monitor")[0] == 404
        assert _get(base + "/monitor.json", _init(OWNER))[0] == 404
        assert srv.stats() == {}
    finally:
        srv.stop()


def test_monitor_url_sits_next_to_login():
    import bot
    assert bot._monitor_url("https://example.com/native-bot-learning/login") == \
        "https://example.com/native-bot-learning/monitor"
    assert bot._what({"message": {"text": "/todo 14 secret words"}}) == "/todo"
    assert bot._what({"message": {"text": "my answer is 42"}}) == "text"
