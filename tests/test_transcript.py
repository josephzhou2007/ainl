"""The developer's after-the-fact record of the conversation. All offline."""
from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from ainl.telegram import __main__ as tg_cli
from ainl.telegram.client import TelegramClient, inline_keyboard
from ainl.telegram.transcript import Transcript, format_record

CHAT = "42"


def _j(body):
    return httpx.Response(200, text=json.dumps(body), headers={"content-type": "application/json"})


def _handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p.endswith("/sendMessage"):
        return _j({"ok": True, "result": {"message_id": 11}})
    if p.endswith("/sendPhoto"):
        return _j({"ok": True, "result": {"message_id": 12}})
    if p.endswith("/sendMediaGroup"):
        return _j({"ok": True, "result": [{"message_id": 13}, {"message_id": 14}]})
    if p.endswith("/editMessageReplyMarkup") or p.endswith("/answerCallbackQuery"):
        return _j({"ok": True, "result": True})
    if p.endswith("/getFile"):
        return _j({"ok": True, "result": {"file_path": "photos/file_7.jpg"}})
    if "/file/bot" in p:
        return httpx.Response(200, content=b"\xff\xd8JPEGBYTES")
    return httpx.Response(404, text=p)


def _records(root):
    return [json.loads(l) for l in (root / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def tg(tmp_path, monkeypatch):
    # The env route, exactly as the bot uses it: AINL_HOME decides where it lands.
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    monkeypatch.setenv("AINL_TRANSCRIPT", "1")
    return TelegramClient("SECRET-TOKEN", transport=httpx.MockTransport(_handler))


def test_incoming_text_and_callback_are_recorded(tg, tmp_path):
    tg.transcript.record_update({"update_id": 500, "message": {"message_id": 1, "chat": {"id": 42},
                                                               "text": "/todo"}})
    tg.transcript.record_update({"update_id": 501, "callback_query": {
        "id": "cb1", "data": "submit", "message": {"message_id": 9, "chat": {"id": 42}}}})
    recs = _records(tmp_path)
    assert [(r["dir"], r["kind"], r["text"], r["update_id"], r["chat_id"]) for r in recs] == [
        ("in", "text", "/todo", 500, CHAT), ("in", "callback", "submit", 501, CHAT)]
    assert recs[1]["message_id"] == 9
    assert all("T" in r["ts"] and r["ts"].startswith("20") for r in recs)     # ISO local time


def test_outgoing_send_records_buttons(tg, tmp_path):
    kb = inline_keyboard([[("✅ Submit", "submit"), ("✏️ Modify", "modify")], [("Cancel", "cancel")]])
    tg.send(CHAT, "Confirm?", reply_markup=kb)
    tg.edit_markup(CHAT, 11, inline_keyboard([[("✅ Submit ✓", "noop")]]))
    recs = _records(tmp_path)
    assert recs[0]["dir"] == "out" and recs[0]["kind"] == "text" and recs[0]["text"] == "Confirm?"
    assert recs[0]["reply_markup"] == [[["✅ Submit", "submit"], ["✏️ Modify", "modify"]], [["Cancel", "cancel"]]]
    assert recs[0]["message_id"] == 11
    assert recs[1]["kind"] == "edit_markup" and recs[1]["reply_markup"] == [[["✅ Submit ✓", "noop"]]]
    assert recs[1]["message_id"] == 11
    line = format_record(recs[0])
    assert "→" in line and "Confirm?" in line and "Submit(submit)" in line
    # the bot token is in the request URL and must never reach the disk
    assert "SECRET-TOKEN" not in (tmp_path / "transcript.jsonl").read_text(encoding="utf-8")


def test_outgoing_photo_and_album_save_bytes(tg, tmp_path):
    tg.send_photo(CHAT, b"PNG1", caption="1. Q1", reply_markup=inline_keyboard([[("A", "pick:a")]]))
    tg.send_media_group(CHAT, [(b"PNG2", "2. Q2"), (b"PNG3", "")])
    recs = _records(tmp_path)
    photo, album = recs
    assert photo["kind"] == "photo" and photo["text"] == "1. Q1" and photo["reply_markup"] == [[["A", "pick:a"]]]
    assert photo["file"].startswith("photos/out-") and photo["file"].endswith(".png")
    assert (tmp_path / photo["file"]).read_bytes() == b"PNG1"
    assert album["kind"] == "album" and album["text"] == "2. Q2" and album["message_ids"] == [13, 14]
    assert len(album["files"]) == 2 and [(tmp_path / f).read_bytes() for f in album["files"]] == [b"PNG2", b"PNG3"]
    assert len({photo["file"], *album["files"]}) == 3                   # distinct names within one second


def test_download_is_saved_and_linked_to_the_update(tg, tmp_path):
    update = {"update_id": 777, "message": {"message_id": 3, "chat": {"id": 42}, "caption": "my answers",
                                            "photo": [{"file_id": "small", "file_size": 10},
                                                      {"file_id": "BIG-FILE-ID", "file_size": 999}]}}
    tg.transcript.record_update(update)
    path, data = tg.download("BIG-FILE-ID")                # what conversation.py does
    assert (path, data) == ("photos/file_7.jpg", b"\xff\xd8JPEGBYTES")
    recs = _records(tmp_path)
    assert recs[0]["kind"] == "photo" and recs[0]["text"] == "my answers" and recs[0]["file_id"] == "BIG-FILE-ID"
    assert recs[1]["kind"] == "photo" and recs[1]["dir"] == "in" and recs[1]["update_id"] == 777
    assert recs[1]["file"].startswith("photos/") and recs[1]["file"].endswith("-777.jpg")
    assert (tmp_path / recs[1]["file"]).read_bytes() == data
    assert sorted(os.listdir(tmp_path / "photos")) == [recs[1]["file"].split("/", 1)[1]]


def test_writer_never_raises_when_unwritable(tmp_path, caplog):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        t = Transcript(root=locked)
        assert t.record("in", CHAT, "text", text="hi") is None
        assert t.save_bytes("x.png", b"1") is None
        t.record_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "hi"}})
        t.record_download("f", "photos/a.jpg", b"1")
        tg = TelegramClient("T", transport=httpx.MockTransport(_handler), transcript=t)
        assert tg.send(CHAT, "still works") == [11]          # the bot keeps talking
        assert tg.send_photo(CHAT, b"P") == 12
        assert not list(t.read())
        warnings = [r for r in caplog.records if r.name == "ainl.transcript" and r.levelname == "WARNING"]
        assert len(warnings) == 1                              # loud once, not once per message
    finally:
        locked.chmod(stat.S_IRWXU)


def test_env_switch_disables_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    monkeypatch.setenv("AINL_TRANSCRIPT", "0")
    tg = TelegramClient("T", transport=httpx.MockTransport(_handler))
    assert tg.transcript.enabled is False
    tg.transcript.record_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "hi"}})
    tg.send(CHAT, "x", reply_markup=inline_keyboard([[("a", "b")]]))
    tg.send_photo(CHAT, b"P")
    tg.download("F")
    assert not (tmp_path / "transcript.jsonl").exists() and not (tmp_path / "photos").exists()


def test_cli_tail_and_since(tg, tmp_path, capsys, monkeypatch):
    tg.transcript.record_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "first"}})
    tg.send(CHAT, "second")
    tg.transcript.record_update({"update_id": 2, "message": {"chat": {"id": 42}, "text": "x" * 400}})
    assert tg_cli.main(["--transcript", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and "→ text" in out[0] and "second" in out[0]
    assert "← text" in out[1] and "…" in out[1] and len(out[1]) < 400
    assert tg_cli.main(["--transcript-since", "2000-01-01T00:00"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 3
    assert tg_cli.main(["--transcript-since", "2999-01-01T00:00"]) == 0
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("AINL_HOME", str(tmp_path / "empty"))
    assert tg_cli.main(["--transcript"]) == 1                   # no transcript yet: says so, no traceback
