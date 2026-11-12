"""The span tracer: every second counted once, across the worker-thread hop."""
from __future__ import annotations

import threading
import time

import pytest

from ainl.util import browserthread, timing
from ainl.util.timing import span, step, trace


def test_step_still_records_a_sample_and_summary():
    with step("unit.step", budget=5):
        pass
    assert any(s.name == "unit.step" and s.ok for s in timing.samples())
    assert "unit.step" in timing.summary(limit=500)


def test_step_failure_is_recorded_and_reraised():
    with pytest.raises(ValueError):
        with step("unit.fail", budget=5):
            raise ValueError("x")
    assert any(s.name == "unit.fail" and not s.ok for s in timing.samples())
    assert timing.last_trace("unit.fail").ok is False


def test_span_outside_a_trace_is_a_no_op():
    with span("orphan", "network") as s:
        s.set(a=1)
        s.split(network=1.0)
    assert timing.current() is None


def test_nested_spans_count_each_second_once():
    with trace("unit.nested") as tr:
        with span("outer", "ours"):
            time.sleep(0.03)
            with span("inner", "network"):
                time.sleep(0.05)
    cats = tr.by_category()
    assert [s.name for s in tr.spans] == ["outer", "inner"]
    assert tr.spans[1].parent == 0 and tr.spans[1].depth == 1
    assert cats["network"] == pytest.approx(tr.spans[1].seconds)
    # outer's OWN time only: its child is not charged twice
    assert cats["ours"] < tr.spans[0].seconds
    assert sum(cats.values()) == pytest.approx(tr.wall, abs=0.005)


def test_split_moves_part_of_a_span_and_leaves_the_rest_ours():
    with trace("unit.split") as tr:
        with span("goto", "network") as g:
            time.sleep(0.06)
        g.split(network=0.02, render=0.01)
    cats = tr.by_category()
    assert cats["network"] == pytest.approx(0.02)
    assert cats["render"] == pytest.approx(0.01)
    assert cats["ours"] >= 0.02                      # the driver's share of the goto
    assert sum(cats.values()) == pytest.approx(tr.wall, abs=0.005)


def test_split_can_never_invent_time():
    with trace("unit.clamp") as tr:
        with span("goto", "network") as g:
            time.sleep(0.01)
        g.split(network=5.0, render=5.0)
    assert sum(tr.by_category().values()) == pytest.approx(tr.wall, abs=0.005)


def test_nested_step_becomes_a_container_span_with_its_category():
    with step("unit.outer", budget=5):
        with step("unit.session", budget=5, cat="session"):
            with span("find_tab", "session"):
                pass
    tr = timing.last_trace("unit.outer")
    assert [(s.name, s.cat, s.depth) for s in tr.spans] == [
        ("unit.session", "session", 0), ("find_tab", "session", 1)]
    assert timing.last_trace("unit.session") is None       # not a trace of its own


def test_spans_opened_on_the_browser_worker_land_in_the_callers_trace():
    seen = {}

    def work():
        seen["thread"] = threading.current_thread().name
        with span("on_worker", "render"):
            time.sleep(0.01)
        return 7

    with step("unit.thread", budget=5):
        assert browserthread.run(work, budget=5) == 7
    tr = timing.last_trace("unit.thread")
    names = [s.name for s in tr.spans]
    assert names == ["browserthread.run", "on_worker"]
    worker_span = tr.spans[1]
    assert worker_span.thread == seen["thread"] != threading.current_thread().name
    assert worker_span.parent == 0                          # nested under the hand-off span


def test_an_abandoned_worker_leaves_its_open_span_behind():
    release = threading.Event()

    def wedged():
        with span("the_call_that_hung", "render"):
            release.wait(5)

    with pytest.raises(browserthread.Timeout):
        with step("unit.wedge", budget=5):
            browserthread.run(wedged, budget=0.1)
    tr = timing.last_trace("unit.wedge")
    try:
        assert [s.name for s in tr.open_spans()] == ["the_call_that_hung"]
        assert tr.to_dict()["spans"][-1]["seconds"] is None
        assert any("wedged" in row for row in browserthread.stuck_stack(40))
    finally:
        release.set()
        browserthread.reset()


def test_notes_and_serialisation_round_trip():
    import json
    with trace("unit.notes") as tr:
        with span("s", "telegram", photos=8) as h:
            h.set(upload_bytes=123)
        timing.note("doc_hops:x", [{"url": "host/path", "ttfb": 1.0}])
    d = json.loads(json.dumps(tr.to_dict()))
    assert d["spans"][0]["meta"] == {"photos": 8, "upload_bytes": 123}
    assert d["notes"][0]["key"] == "doc_hops:x"
    assert set(d["by_category"]) >= set(timing.CATEGORIES)
    assert "unit.notes" in tr.waterfall()


def test_category_summary_mentions_the_operation():
    with trace("unit.catsum"):
        with span("x", "llm"):
            time.sleep(0.06)
    assert "unit.catsum" in timing.category_summary("unit.catsum")
    assert "llm" in timing.category_summary("unit.catsum")
