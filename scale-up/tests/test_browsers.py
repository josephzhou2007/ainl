"""BrowserPool with a fake launcher + fake liveness probe: no real Chrome, no
real ports, no subprocesses, no threads."""
from __future__ import annotations

from browsers import RESERVED_PORT, BrowserPool
from registry import Registry


class FakeChrome:
    """Stands in for real launch/alive/stop. Tracks calls and running ports."""

    def __init__(self) -> None:
        self.launches: list[tuple[int, str]] = []
        self.stops: list[tuple[int, str]] = []
        self.running: set[int] = set()

    def launch(self, port, profile):
        self.launches.append((port, str(profile)))
        self.running.add(port)
        return ("handle", port)

    def alive(self, port):
        return port in self.running

    def stop(self, port, profile):
        self.stops.append((port, str(profile)))
        self.running.discard(port)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _pool(tmp_path, fake, clock=None, base_port=9300, poll_interval=1.0):
    reg = Registry(base=tmp_path)
    for cid in ("111", "222"):
        reg.add(cid, cid, cid)
    return BrowserPool(reg, base_port=base_port, launch=fake.launch,
                       alive=fake.alive, stop=fake.stop,
                       clock=(clock or (lambda: 0.0)), poll_interval=poll_interval)


def test_cdp_url_starts_once_and_reuses(tmp_path):
    fake = FakeChrome()
    pool = _pool(tmp_path, fake)

    url1 = pool.cdp_url("111")
    url2 = pool.cdp_url("111")

    assert url1 == url2 == "http://127.0.0.1:9300"
    assert len(fake.launches) == 1            # started exactly once, then reused


def test_launched_with_expected_port_and_profile(tmp_path):
    fake = FakeChrome()
    pool = _pool(tmp_path, fake)
    reg = Registry(base=tmp_path)

    pool.cdp_url("111")
    expected_profile = str(reg.user_dir("111") / "chrome-profile")
    assert fake.launches == [(9300, expected_profile)]


def test_distinct_users_get_distinct_ports_never_9222(tmp_path):
    fake = FakeChrome()
    pool = _pool(tmp_path, fake)

    p1 = pool.cdp_url("111")
    p2 = pool.cdp_url("222")

    assert p1 != p2
    assert p1 == "http://127.0.0.1:9300" and p2 == "http://127.0.0.1:9301"
    ports = {port for port, _ in fake.launches}
    assert RESERVED_PORT not in ports


def test_base_port_skips_reserved_9222(tmp_path):
    fake = FakeChrome()
    pool = _pool(tmp_path, fake, base_port=RESERVED_PORT)
    url = pool.cdp_url("111")
    assert url == "http://127.0.0.1:9223"     # 9222 skipped


def test_reap_stops_only_the_idle_one_and_restart_works(tmp_path):
    fake = FakeChrome()
    clock = Clock()
    pool = _pool(tmp_path, fake, clock=clock)
    reg = Registry(base=tmp_path)

    clock.t = 0.0
    pool.cdp_url("111")           # last_used = 0
    clock.t = 100.0
    pool.cdp_url("222")           # last_used = 100

    clock.t = 120.0
    reaped = pool.reap(idle_seconds=50)

    # Only user 111 was idle >= 50s; 222 (idle 20s) is kept.
    assert reaped == ["111"]
    prof_111 = str(reg.user_dir("111") / "chrome-profile")
    assert fake.stops == [(9300, prof_111)]   # right port + profile, only that one
    assert 9300 not in fake.running and 9301 in fake.running

    # A subsequent cdp_url restarts the reaped user.
    url = pool.cdp_url("111")
    assert url == "http://127.0.0.1:9300"     # port freed by the reap, reused
    assert 9300 in fake.running
    assert len(fake.launches) == 3            # 111, 222, then 111 again


def test_dead_browser_is_restarted_on_same_port(tmp_path):
    fake = FakeChrome()
    pool = _pool(tmp_path, fake)
    pool.cdp_url("111")
    fake.running.discard(9300)                 # simulate an external crash/kill
    url = pool.cdp_url("111")
    assert url == "http://127.0.0.1:9300"
    assert 9300 in fake.running
    assert len(fake.launches) == 2


def test_shutdown_all_stops_everything(tmp_path):
    fake = FakeChrome()
    pool = _pool(tmp_path, fake)
    pool.cdp_url("111")
    pool.cdp_url("222")
    pool.shutdown_all()
    assert fake.running == set()
    assert {port for port, _ in fake.stops} == {9300, 9301}


def test_restart_relaunches_only_that_user_on_the_same_port(tmp_path):
    """What WebAssign's watchdog calls for a pooled user — instead of the
    single-user default, which restarts the browser on 9222 (someone else's)."""
    fake = FakeChrome()
    pool = _pool(tmp_path, fake, poll_interval=0)
    a, b = pool.cdp_url("111"), pool.cdp_url("222")
    port_a = int(a.rsplit(":", 1)[1])
    assert pool.restart("111") is True
    assert fake.stops == [(port_a, str(pool._reg.user_dir("111") / "chrome-profile"))]
    assert pool.cdp_url("111") == a and pool.cdp_url("222") == b     # same ports, 222 untouched
    assert [p for p, _ in fake.launches].count(port_a) == 2
    assert pool.restart("nobody") is False


# -- hooks for Canvas login ---------------------------------------------------

def test_on_start_runs_after_a_fresh_start_only_and_may_reenter(tmp_path):
    fake = FakeChrome()
    reg = Registry(base=tmp_path)
    reg.add("111", "111", "111")
    seen = []

    def on_start(cid):
        # Cookie Management's restore goes through cdp_url() again: must not deadlock.
        seen.append((cid, pool.cdp_url(cid)))

    pool = BrowserPool(reg, launch=fake.launch, alive=fake.alive, stop=fake.stop,
                       clock=lambda: 0.0, poll_interval=0, on_start=on_start)
    pool.cdp_url("111")
    pool.cdp_url("111")
    assert seen == [("111", "http://127.0.0.1:9300")]
    fake.running.discard(9300)                 # it died: the relaunch is fresh again
    pool.cdp_url("111")
    assert len(seen) == 2


def test_a_failing_on_start_does_not_fail_the_caller(tmp_path):
    fake = FakeChrome()
    reg = Registry(base=tmp_path)
    reg.add("111", "111", "111")

    def boom(cid):
        raise RuntimeError("restore failed")

    pool = BrowserPool(reg, launch=fake.launch, alive=fake.alive, stop=fake.stop,
                       clock=lambda: 0.0, poll_interval=0, on_start=boom)
    assert pool.cdp_url("111") == "http://127.0.0.1:9300"


def test_stop_stops_one_person_only(tmp_path):
    fake = FakeChrome()
    pool = _pool(tmp_path, fake, poll_interval=0)
    pool.cdp_url("111")
    pool.cdp_url("222")
    assert pool.stop("111") is True
    assert fake.running == {9301} and not pool.running("111") and pool.running("222")
    assert pool.stop("111") is False


def test_a_port_someone_else_is_listening_on_is_skipped(tmp_path):
    # After a crash another person's orphaned browser may still hold 9300.
    fake = FakeChrome()
    fake.running.add(9300)
    pool = _pool(tmp_path, fake)
    assert pool.cdp_url("111") == "http://127.0.0.1:9301"


# -- keeping browsers lean ----------------------------------------------------

def _lean_pool(tmp_path, clock, tidied):
    fake = FakeChrome()
    reg = Registry(base=tmp_path)
    for cid in ("111", "222"):
        reg.add(cid, cid, cid)
    pool = BrowserPool(reg, launch=fake.launch, alive=fake.alive, stop=fake.stop,
                       clock=clock, poll_interval=0,
                       tidy=lambda url: tidied.append(url) or 2)
    return pool, fake


def test_idle_browsers_are_tidied_once_and_busy_ones_never(tmp_path):
    clock, tidied = Clock(), []
    pool, _ = _lean_pool(tmp_path, clock, tidied)
    pool.cdp_url("111")
    pool.cdp_url("222")
    clock.t = 300
    assert pool.tidy_idle(600) == []                       # not idle long enough
    clock.t = 700
    assert pool.tidy_idle(600, busy=lambda c: c == "222") == ["111"]
    assert tidied == ["http://127.0.0.1:9300"]
    assert pool.tidy_idle(600, busy=lambda c: c == "222") == []   # once per idle spell
    pool.touch("111")                                      # used again…
    clock.t = 1400
    assert "111" in pool.tidy_idle(600)                    # …so tidied again later


def test_touch_keeps_a_working_browser_from_the_reaper(tmp_path):
    """WebAssign asks for the address once, then works: that alone must not
    look like 30 idle minutes."""
    clock, tidied = Clock(), []
    pool, fake = _lean_pool(tmp_path, clock, tidied)
    pool.cdp_url("111")
    clock.t = 1500
    pool.touch("111")                                      # a message handled at 25 min
    clock.t = 2000
    assert pool.reap(1800) == []                           # 8 min since its last use
    assert pool.reap(0, busy=lambda c: True) == []         # busy: never stopped
    clock.t = 3400
    assert pool.reap(1800) == ["111"]


def test_a_restart_adopts_its_own_running_browsers_and_nothing_else(tmp_path):
    fake = FakeChrome()
    reg = Registry(base=tmp_path)
    for cid in ("111", "222"):
        reg.add(cid, cid, cid)
    fake.running |= {9301, 9302, 9222, 9400}
    mine = str(reg.user_dir("111") / "chrome-profile")
    pool = BrowserPool(reg, launch=fake.launch, alive=fake.alive, stop=fake.stop,
                       clock=lambda: 0.0, poll_interval=0)
    taken = pool.adopt(scan=lambda: [
        (9301, mine),                                           # 111's own: adopted
        (9222, str(reg.user_dir("222") / "chrome-profile")),    # the owner's port: never
        (9400, "/somewhere/else/chrome-profile"),               # not ours
        (9303, str(reg.user_dir("222") / "chrome-profile")),    # port not answering
    ])
    assert taken == ["111"]
    assert pool.cdp_url("111") == "http://127.0.0.1:9301" and fake.launches == []   # no relaunch
    assert pool.running("111") and not pool.running("222")


def test_checkpoint_before_every_stop_except_forget(tmp_path):
    fake, saved = FakeChrome(), []
    reg = Registry(base=tmp_path)
    for cid in ("111", "222", "333"):
        reg.add(cid, cid, cid)
    clock = Clock()
    pool = BrowserPool(reg, launch=fake.launch, alive=fake.alive, stop=fake.stop, clock=clock,
                       poll_interval=0, tidy=lambda url: 1,
                       checkpoint=lambda cid, port: saved.append((cid, port)))
    for cid in ("111", "222", "333"):
        pool.cdp_url(cid)
    pool.tidy_now("111")
    pool.restart("111")
    pool.stop("222")                                           # /forget: nothing kept
    clock.t = 5000
    pool.reap(1800)
    # tidy_now, restart, then the reaper stops 333 and 111 (idle since its restart)
    assert saved == [("111", 9300), ("111", 9300), ("333", 9302), ("111", 9300)]
    assert ("222", 9301) not in saved


def test_the_reaper_can_follow_a_changing_t(tmp_path):
    clock, tidied = Clock(), []
    pool, _ = _lean_pool(tmp_path, clock, tidied)
    pool.cdp_url("111")
    clock.t = 1000
    t = {"s": 1800}
    assert pool.reap(t["s"]) == []
    t["s"] = 600
    assert pool.reap(t["s"]) == ["111"]
