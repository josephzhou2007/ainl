"""Per-key workers: people run in parallel, on their own threads, and the
single-user path (no key) is untouched. No browser involved."""
from __future__ import annotations

import threading
import time

from ainl.util import browserthread as bt


def _thread_name():
    return threading.current_thread().name


def test_no_key_is_the_default_worker_as_before():
    a = bt.run(_thread_name, budget=5)
    b = bt.run(_thread_name, budget=5)
    assert a == b and a.startswith("browser-") and "-me" not in a


def test_each_key_gets_its_own_thread_and_using_routes_there():
    with bt.using("alice"):
        alice = bt.run(_thread_name, budget=5)
        again = bt.run(_thread_name, budget=5)
    with bt.using("bob"):
        bob = bt.run(_thread_name, budget=5)
    default = bt.run(_thread_name, budget=5)
    assert alice == again                       # same person, same worker
    assert len({alice, bob, default}) == 3      # three distinct threads
    assert "alice" in alice and "bob" in bob


def test_two_keys_run_at_the_same_time_not_queued():
    """The old lock was held for the whole job, so everyone queued behind one
    slow page. Two 0.6 s jobs on two keys must take ~0.6 s, not ~1.2 s."""
    def slow():
        time.sleep(0.6)
        return True

    out = {}

    def drive(k):
        with bt.using(k):
            out[k] = bt.run(slow, budget=5)

    t0 = time.monotonic()
    ts = [threading.Thread(target=drive, args=(k,)) for k in ("p1", "p2")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out == {"p1": True, "p2": True}
    assert time.monotonic() - t0 < 1.0


def test_the_key_survives_the_hop_onto_the_worker():
    """Inside the job the key is still set (the context is copied across),
    and a job that calls run() on its own key runs inline instead of
    queueing behind itself — which would have been a deadlock."""
    def outer():
        return (bt.current_key(), bt.run(_thread_name, budget=5))

    with bt.using("carol"):
        key, inner_thread = bt.run(outer, budget=5)
        outer_thread = bt.run(_thread_name, budget=5)
    assert key == "carol" and inner_thread == outer_thread


def test_a_timeout_abandons_only_that_key():
    with bt.using("dave"):
        try:
            bt.run(lambda: time.sleep(2), budget=0.2)
        except bt.Timeout:
            pass
        else:
            raise AssertionError("expected Timeout")
        fresh = bt.run(_thread_name, budget=5)   # a new worker for dave
    other = bt.run(_thread_name, budget=5)       # the default one is untouched
    assert "dave" in fresh and "dave" not in other


def test_reset_drops_one_key_or_all():
    with bt.using("erin"):
        first = bt.run(_thread_name, budget=5)
        bt.reset()
        second = bt.run(_thread_name, budget=5)
    assert first != second
    bt.reset(all_keys=True)
    with bt.using("erin"):
        third = bt.run(_thread_name, budget=5)
    assert third != second
