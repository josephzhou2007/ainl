"""Failures: the scene is kept, the owner is told, script and Jev stay apart."""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

from ainl.util import trouble


@pytest.fixture
def on(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_TROUBLE", "1")
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    told = []
    trouble.set_notifier(lambda text, png: told.append((text, png)), owner="999")
    trouble._recent.clear()
    yield SimpleNamespace(home=tmp_path, told=told)
    trouble.set_notifier(None)
    trouble._recent.clear()


def test_which_bot_messages_are_failures():
    assert trouble.problem_line("⚠️ Gradescope didn't answer") == "⚠️ Gradescope didn't answer"
    assert trouble.problem_line("❌ Something broke on my side") .startswith("❌")
    assert trouble.problem_line("Filled 4 boxes.\n⚠️ Couldn't press submit on: 3").startswith("⚠️ Couldn't")
    assert trouble.problem_line("Filled.\n⚠️ 3, 4: the page didn't draw the answer").startswith("⚠️ 3, 4")
    assert trouble.problem_line("📤 Uploaded.\n\n⚠️ Gradescope asks you to mark which pages "
                                "answer which question — I can't do that part yet.") == ""
    assert trouble.problem_line("Here is 9.3 Homework.\n⚠️ 2 has 3 blanks") == ""
    assert trouble.problem_line("All done ✅") == ""


def test_a_script_failure_is_filed_with_its_error_and_the_owner_told(on):
    try:
        raise TimeoutError("Timeout 8000ms waiting for #submit_3003_3")
    except TimeoutError:
        d = trouble.report("script", chat="8974357225", where="webassign:A1",
                           message="⚠️ WebAssign didn't answer. Nothing was submitted.", background=False)
    ev = json.loads((d / "event.json").read_text())
    assert d.parts[-4:-2] == ("failures", "script")
    assert ev["source"] == "script" and ev["chat"] == "8974357225" and ev["where"] == "webassign:A1"
    assert ev["error"].startswith("TimeoutError") and "#submit_3003_3" in ev["traceback"]
    idx = [json.loads(ln) for ln in (on.home / "failures" / "index.jsonl").read_text().splitlines()]
    assert idx[-1]["source"] == "script" and idx[-1]["dir"] == str(d)
    [(text, png)] = on.told
    assert "脚本" in text and "8974357225" in text and "webassign:A1" in text and "TimeoutError" in text


def test_jev_misses_are_kept_apart_and_carry_their_files(on):
    d = trouble.report("jev", chat="trial", where="trial:08", message="⚠️ Jev trial: wrong element",
                       files={"page.png": b"\x89PNG", "page.html": "<html></html>"},
                       notify=False, background=False)
    assert d.parts[-4:-2] == ("failures", "jev")
    assert (d / "page.png").read_bytes() == b"\x89PNG" and (d / "page.html").exists()
    assert on.told == []                               # the trial reports once, at the end


def test_the_same_failure_twice_is_counted_not_refiled(on):
    a = trouble.report("script", chat="1", where="x", message="⚠️ boom", background=False)
    b = trouble.report("script", chat="1", where="x", message="⚠️ boom", background=False)
    c = trouble.report("script", chat="2", where="x", message="⚠️ boom", background=False)
    assert a is not None and b is None and c is not None
    assert len(on.told) == 2


def test_the_owners_own_chat_is_filed_but_not_sent_again(on):
    d = trouble.report("script", chat="999", where="x", message="⚠️ boom", background=False)
    assert d is not None and on.told == []


def test_off_means_nothing_is_written(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_TROUBLE", "0")
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    assert trouble.report("script", chat="1", where="x", message="⚠️ boom") is None
    assert not (tmp_path / "failures").exists()


def test_a_broken_notifier_never_breaks_the_bot(on):
    trouble.set_notifier(lambda t, p: 1 / 0, owner="999")
    assert trouble.report("script", chat="1", where="x", message="⚠️ boom", background=False) is not None


def test_urls_lose_their_one_time_credentials():
    assert trouble.redact("https://x.test/launch?token=abc#f") == "https://x.test/launch"


def test_conversation_files_its_failure_messages(on, monkeypatch):
    from ainl.bot.conversation import Conversation
    from ainl.bot.state import BotState
    got = []
    monkeypatch.setattr(trouble, "report", lambda source, **kw: got.append((source, kw)))
    sent = []
    tg = SimpleNamespace(send=lambda chat, text, **kw: sent.append(text))
    conv = Conversation(SimpleNamespace(), tg, "999", BotState(), tz="UTC")
    conv._send("Here is your list.")
    assert got == []
    conv._send("⚠️ WebAssign didn't answer in 25 s. Nothing was submitted.")
    [(source, kw)] = got
    assert source == "script" and kw["chat"] == "999" and kw["where"] == "chat"
    assert kw["cdp_url"] == "http://127.0.0.1:9222"
    assert sent[-1].startswith("⚠️ WebAssign")          # the owner's bot adds nothing


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_capture_reads_the_real_browser_over_cdp(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    port = _free_port()
    html = "<title>PS3</title><h1>Problem Set 3</h1><button>Submit PDF</button>"
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
            page = browser.new_page()
            page.route("https://fixtures.test/**",
                       lambda r: r.fulfill(body=html, content_type="text/html"))
            page.goto("https://fixtures.test/ps3?token=one-time-secret")
            tabs = trouble.capture(f"http://127.0.0.1:{port}", tmp_path)
            browser.close()
    except Exception as e:                                # no Chromium here: nothing to test
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    assert tabs == [{"url": "https://fixtures.test/ps3", "title": "PS3"}]
    assert (tmp_path / "tab-0.png").read_bytes()[:4] == b"\x89PNG"
    assert "Submit PDF" in (tmp_path / "tab-0.html").read_text()
