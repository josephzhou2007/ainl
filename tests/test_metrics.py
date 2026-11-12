"""util/metrics: what is written, what is never written, and the report."""
import json
import subprocess
import sys
from pathlib import Path

import httpx

from ainl.util import metrics


def _on(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    monkeypatch.setenv("AINL_METRICS", "1")


def _lines(tmp_path):
    return [json.loads(x) for f in (tmp_path / "metrics").glob("*.jsonl") for x in f.read_text().splitlines()]


def test_http_calls_are_timed_and_the_bot_token_never_written(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    tr = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"ok": True, "result": []}, headers={"X-Rate-Limit-Remaining": "699.5"}))
    c = httpx.Client(transport=tr, event_hooks=metrics.hooks())
    c.post("https://api.telegram.org/bot123:SECRET/sendMessage", json={})
    c.get("https://canvas.wisc.edu/api/v1/courses/100004/quizzes/749802?access_token=zzz")
    recs = _lines(tmp_path)
    raw = (tmp_path / "metrics").glob("*.jsonl").__next__().read_text()
    assert "SECRET" not in raw and "123:" not in raw and "zzz" not in raw
    tg = next(r for r in recs if r["svc"] == "telegram")
    assert tg["op"] == "sendMessage" and tg["status"] == 200 and tg["ms"] is not None
    cv = next(r for r in recs if r["svc"] == "canvas")
    assert cv["op"] == "/api/v1/courses/:id/quizzes/:id" and cv["rate_left"] == "699.5"


def test_nothing_is_written_when_off(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    monkeypatch.setenv("AINL_METRICS", "0")
    metrics.emit("mark", x=1)
    assert not (tmp_path / "metrics").exists() or not list((tmp_path / "metrics").glob("*"))
    assert metrics.hooks() == {}


def test_a_snapshot_has_the_bot_the_browsers_and_the_machine():
    s = metrics.snapshot()
    assert s["bot"]["rss_mb"] > 0 and s["machine"]["mem_total_mb"] > 0
    assert isinstance(s["browsers"], dict)


def test_pct():
    assert metrics.pct([1, 2, 3, 4, 5], 50) == 3 and metrics.pct([], 50) is None


def test_the_report_reads_a_sweep(monkeypatch, tmp_path):
    _on(monkeypatch, tmp_path)
    metrics.emit("sweep", phase="start", code="B1", course="MATH222", item_kind="webassign")
    metrics.emit("sample", bot={"rss_mb": 50, "footprint_mb": 40, "threads": 9, "cpu": 1},
                 browsers={"owner": {"procs": 9, "rss_mb": 3000, "footprint_mb": 2000, "cpu": 5, "renderers": 3}},
                 machine={"mem_used_mb": 14000, "mem_total_mb": 24576, "mem_pct": 57, "load1": 2.1,
                          "net_sent_mb": 1, "net_recv_mb": 1})
    metrics.emit("llm", model="deepseek-flash", s=30, prompt=4000, reasoning=17000, output=100)
    metrics.emit("update", what="/sweep", lag_s=0.4, handle_s=1.2)
    metrics.emit("sweep", phase="end", code="B1", course="MATH222", item_kind="webassign", s=41.0, crashed=False)
    out = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "metrics_report.py")],
                         capture_output=True, text=True, env={**__import__("os").environ})
    assert out.returncode == 0, out.stderr
    assert "B1" in out.stdout and "21100" in out.stdout and "2000" in out.stdout
    assert "Per student" in out.stdout and "one warm browser ≈ 2000 MB" in out.stdout


def test_a_slow_body_is_timed_to_the_end_not_to_the_headers(monkeypatch, tmp_path):
    import time as _t
    _on(monkeypatch, tmp_path)

    class Slow(httpx.ByteStream):
        def __iter__(self):
            _t.sleep(0.3)
            yield b'{"ok": true}'

    tr = httpx.MockTransport(lambda req: httpx.Response(200, stream=Slow(b"")))
    c = httpx.Client(transport=tr, event_hooks=metrics.hooks())
    c.post("https://api.deepseek.com/chat/completions", json={})
    rec = next(r for r in _lines(tmp_path) if r["svc"] == "deepseek")
    assert rec["ms"] >= 290
