"""How long each step takes, where the time went, and a ceiling on it.

The bot polls Telegram on one thread, so a slow call is not slow — it is the
whole product stopping. One screenshot that waited 30 s for an element to hold
still made the bot look dead for 30 s.

So every step that leaves the process is wrapped:

    with step("wa.questions", budget=10):
        ...

which logs its duration, keeps a rolling record for `/timing`, and warns when
it goes over budget. Budgets are deliberately short — better to abort and retry
a 5 s operation than to wait 30 s for one that was never going to finish.

A total says THAT an operation was slow, never WHY. So a step is also a trace:
inside it, the code marks its seams with categorised spans,

    with span("goto_assignment", cat="network"):
        tab.goto(...)

and afterwards ``last_trace("wa.questions")`` gives the ordered spans — start
offset, duration, category, nesting — and ``by_category()`` says how much of
the wall time was the remote server, the page settling, our own code, Telegram,
the model, or session upkeep.

Categories:
    network   waiting for a remote web server (navigation, HTTP API calls)
    render    the browser laying out / painting / mounting scripts, screenshots
    ours      our own code, the Playwright driver, fixed sleeps we chose
    telegram  Telegram Bot API round trips
    llm       model calls
    session   checking for / rebuilding the vendor login

Threading: the bot's caller thread opens the step, then waits while a browser
worker thread does the work. The current trace travels in a ContextVar, and
``browserthread.run`` copies the caller's context onto the worker, so spans
opened over there land in the caller's trace. A span is recorded when it
OPENS and completed when it closes, so when the watchdog abandons a wedged
worker, ``Trace.open_spans()`` still says which action never came back.
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("ainl.timing")

# Default ceiling for anything that touches the network or the browser.
DEFAULT_BUDGET = 10.0
HISTORY = 200
TRACE_HISTORY = 60

CATEGORIES = ("network", "render", "ours", "telegram", "llm", "session")


@dataclass
class Sample:
    name: str
    seconds: float
    ok: bool
    over_budget: bool


_history: deque[Sample] = deque(maxlen=HISTORY)


class StepTimeout(RuntimeError):
    """A step ran past its budget and was given up on."""


# --------------------------------------------------------------------------- #
# spans and traces
# --------------------------------------------------------------------------- #

@dataclass
class Span:
    name: str
    cat: str
    start: float                       # seconds after the trace began
    seconds: Optional[float] = None    # None while open — or abandoned, never closed
    depth: int = 0
    parent: Optional[int] = None       # index into Trace.spans
    thread: str = ""
    ok: bool = True
    meta: dict = field(default_factory=dict)
    # Re-attribute part of this span's own time to other categories, e.g. a
    # goto measured by the browser's Navigation Timing as {"network": 0.8,
    # "render": 0.3}. Whatever is left stays "ours" (driver overhead).
    split: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "cat": self.cat, "start": round(self.start, 4),
             "seconds": None if self.seconds is None else round(self.seconds, 4),
             "depth": self.depth, "parent": self.parent, "thread": self.thread,
             "ok": self.ok}
        if self.meta:
            d["meta"] = self.meta
        if self.split:
            d["split"] = {k: round(v, 4) for k, v in self.split.items()}
        return d


class Trace:
    """Everything that happened inside one operation, in the order it began."""

    def __init__(self, name: str):
        self.name = name
        self.t0 = time.monotonic()
        self.wall: Optional[float] = None
        self.ok = True
        self.spans: list[Span] = []
        self.notes: list[dict] = []
        self._lock = threading.Lock()

    # -- recording ------------------------------------------------------------

    def _open(self, name: str, cat: str, parent: Optional[int], meta: dict) -> int:
        with self._lock:
            depth = 0 if parent is None else self.spans[parent].depth + 1
            self.spans.append(Span(name, cat, time.monotonic() - self.t0, None, depth,
                                   parent, threading.current_thread().name, True,
                                   dict(meta)))
            return len(self.spans) - 1

    def _close(self, idx: int, ok: bool) -> None:
        with self._lock:
            s = self.spans[idx]
            s.seconds = (time.monotonic() - self.t0) - s.start
            s.ok = ok

    def note(self, key: str, value: Any) -> None:
        with self._lock:
            self.notes.append({"at": round(time.monotonic() - self.t0, 4),
                               "key": key, "value": value})

    # -- reading --------------------------------------------------------------

    def open_spans(self) -> list[Span]:
        """Spans that began and never ended — on a wedge, the innermost of
        these is the action that hung."""
        with self._lock:
            return [s for s in self.spans if s.seconds is None]

    def elapsed(self) -> float:
        return self.wall if self.wall is not None else time.monotonic() - self.t0

    def by_category(self) -> dict[str, float]:
        """Wall time split by category, each second counted exactly once.

        A span's OWN time is its duration minus its direct children's; nesting
        therefore never double counts. Time inside the trace that no span
        covers is ours by definition (thread hand-off, Python between seams).
        A span still open is charged up to now.
        """
        with self._lock:
            spans = list(self.spans)
        total = self.elapsed()
        dur = [s.seconds if s.seconds is not None else max(0.0, total - s.start)
               for s in spans]
        child = [0.0] * len(spans)
        top = 0.0
        for i, s in enumerate(spans):
            if s.parent is None:
                top += dur[i]
            else:
                child[s.parent] += dur[i]
        out = {c: 0.0 for c in CATEGORIES}
        for i, s in enumerate(spans):
            own = max(0.0, dur[i] - child[i])
            if s.split:
                left = own
                for cat, sec in s.split.items():
                    take = max(0.0, min(left, float(sec)))
                    out[cat] = out.get(cat, 0.0) + take
                    left -= take
                out["ours"] += left
            else:
                out[s.cat] = out.get(s.cat, 0.0) + own
        out["ours"] += max(0.0, total - top)
        return out

    def to_dict(self) -> dict:
        with self._lock:
            spans = [s.to_dict() for s in self.spans]
            notes = list(self.notes)
        return {"name": self.name, "wall": round(self.elapsed(), 4), "ok": self.ok,
                "by_category": {k: round(v, 4) for k, v in self.by_category().items()},
                "spans": spans, "notes": notes}

    def waterfall(self) -> str:
        """Plain text, one line per span — for a log or a terminal."""
        with self._lock:
            spans = list(self.spans)
        rows = [f"{self.name}  {self.elapsed():.2f}s"]
        for s in spans:
            d = "  open" if s.seconds is None else f"{s.seconds:6.3f}"
            rows.append(f"  {s.start:7.3f}  {d}  {s.cat:<8} {'  ' * s.depth}{s.name}")
        return "\n".join(rows)


_trace_var: contextvars.ContextVar[Optional[Trace]] = contextvars.ContextVar(
    "ainl_trace", default=None)
_parent_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "ainl_span_parent", default=None)
_traces: deque[Trace] = deque(maxlen=TRACE_HISTORY)
_traces_lock = threading.Lock()

# Extra measurements that cost a browser round trip each (Navigation Timing,
# resource timing, the wedge probe). Off in the bot unless asked for, because
# instrumenting must not change how the instrumented thing behaves.
_detail = os.environ.get("AINL_TRACE_DETAIL", "") not in ("", "0")


def detail() -> bool:
    return _detail


def set_detail(on: bool) -> None:
    global _detail
    _detail = bool(on)


def current() -> Optional[Trace]:
    return _trace_var.get()


class _SpanHandle:
    """What ``with span(...) as s`` yields: a way to hang facts on the span."""

    __slots__ = ("_trace", "_idx")

    def __init__(self, trace: Optional[Trace], idx: int):
        self._trace, self._idx = trace, idx

    def set(self, **meta) -> None:
        if self._trace is not None:
            with self._trace._lock:
                self._trace.spans[self._idx].meta.update(meta)

    def split(self, **cats: float) -> None:
        if self._trace is not None:
            with self._trace._lock:
                self._trace.spans[self._idx].split = {k: float(v) for k, v in cats.items()}


@contextmanager
def span(name: str, cat: str = "ours", **meta):
    """Mark one seam. Outside any trace this costs a ContextVar lookup."""
    trace = _trace_var.get()
    if trace is None:
        yield _SpanHandle(None, -1)
        return
    idx = trace._open(name, cat, _parent_var.get(), meta)
    token = _parent_var.set(idx)
    ok = True
    try:
        yield _SpanHandle(trace, idx)
    except BaseException:
        ok = False
        raise
    finally:
        _parent_var.reset(token)
        trace._close(idx, ok)


def note(key: str, value: Any) -> None:
    trace = _trace_var.get()
    if trace is not None:
        trace.note(key, value)


@contextmanager
def trace(name: str, cat: str = "ours"):
    """Start a trace (or, inside one, a container span). Yields the Trace.

    ``cat`` only matters when nested: it is the category the container's own
    time — whatever its children do not account for — is charged to.
    """
    outer = _trace_var.get()
    if outer is not None:
        with span(name, cat):
            yield outer
        return
    tr = Trace(name)
    t_tok, p_tok = _trace_var.set(tr), _parent_var.set(None)
    try:
        yield tr
    except BaseException:
        tr.ok = False
        raise
    finally:
        tr.wall = time.monotonic() - tr.t0
        _parent_var.reset(p_tok)
        _trace_var.reset(t_tok)
        with _traces_lock:
            _traces.append(tr)


def traces(name: Optional[str] = None) -> list[Trace]:
    with _traces_lock:
        return [t for t in _traces if name is None or t.name == name]


def last_trace(name: Optional[str] = None) -> Optional[Trace]:
    found = traces(name)
    return found[-1] if found else None


# --------------------------------------------------------------------------- #
# steps — the API the bot already uses
# --------------------------------------------------------------------------- #

@contextmanager
def step(name: str, budget: float = DEFAULT_BUDGET, cat: str = "ours"):
    t0 = time.monotonic()
    ok = True
    try:
        with trace(name, cat):
            yield
    except Exception:
        ok = False
        raise
    finally:
        dt = time.monotonic() - t0
        over = dt > budget
        _history.append(Sample(name, dt, ok, over))
        from . import metrics
        metrics.emit("step", name=name, s=round(dt, 2), ok=ok, over=over)
        if over:
            log.warning("%s took %.1fs (budget %.0fs)%s", name, dt, budget,
                        "" if ok else " and failed")
        else:
            log.info("%s %.1fs", name, dt)


def samples() -> list[Sample]:
    return list(_history)


def summary(limit: int = 12) -> str:
    """Worst offenders first — what to shorten next, by measurement."""
    by_name: dict[str, list[float]] = {}
    fails: dict[str, int] = {}
    for s in _history:
        by_name.setdefault(s.name, []).append(s.seconds)
        if not s.ok:
            fails[s.name] = fails.get(s.name, 0) + 1
    if not by_name:
        return "No steps recorded yet."
    rows = sorted(by_name.items(), key=lambda kv: max(kv[1]), reverse=True)[:limit]
    out = ["<b>step</b>  n · median · worst"]
    for name, xs in rows:
        xs = sorted(xs)
        med = xs[len(xs) // 2]
        bad = f"  ({fails[name]} failed)" if name in fails else ""
        out.append(f"<code>{name}</code>  {len(xs)} · {med:.1f}s · {xs[-1]:.1f}s{bad}")
    return "\n".join(out)


def category_summary(name: Optional[str] = None) -> str:
    """Where the time went, per operation — median seconds per category."""
    groups: dict[str, list[Trace]] = {}
    for t in traces(name):
        groups.setdefault(t.name, []).append(t)
    if not groups:
        return "No traces recorded yet."
    out = ["<b>where the time went</b> (median s)"]
    for nm, ts in groups.items():
        cats = [t.by_category() for t in ts]
        walls = sorted(t.elapsed() for t in ts)
        parts = []
        for c in CATEGORIES:
            xs = sorted(d.get(c, 0.0) for d in cats)
            med = xs[len(xs) // 2]
            if med >= 0.05:
                parts.append(f"{c} {med:.1f}")
        out.append(f"<code>{nm}</code> n={len(ts)} · {walls[len(walls) // 2]:.1f}s · "
                   + (", ".join(parts) or "—"))
    return "\n".join(out)
