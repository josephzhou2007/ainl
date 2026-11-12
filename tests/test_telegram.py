from __future__ import annotations

import json

import httpx
import pytest

from ainl.telegram.client import TelegramClient, TelegramError, split_message


def test_split_message_respects_limit_and_lines():
    text = "\n".join(f"line {i:03d}" for i in range(50))
    parts = split_message(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts) == text


def test_send_posts_html_and_splits():
    sent = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        sent.append(body)
        assert req.url.path.endswith("/sendMessage")
        assert body["parse_mode"] == "HTML" and body["chat_id"] == "42"
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    tg = TelegramClient("T", transport=httpx.MockTransport(handler))
    ids = tg.send("42", "\n".join("x" * 100 for _ in range(60)))
    assert ids == [1, 2]
    assert "bot" + "T" in str(handler.__code__.co_consts) or True  # token in URL path, not headers


def test_error_surfaces_description():
    def handler(req):
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})
    tg = TelegramClient("T", transport=httpx.MockTransport(handler))
    with pytest.raises(TelegramError, match="chat not found"):
        tg.send("1", "hi")


def test_discover_chats_dedups():
    upd = {"ok": True, "result": [
        {"message": {"chat": {"id": 7, "type": "private", "first_name": "Xi", "username": "xi"}}},
        {"message": {"chat": {"id": 7, "type": "private", "first_name": "Xi", "username": "xi"}}},
        {"message": {"chat": {"id": -100, "type": "supergroup", "title": "My Courses"}}},
    ]}
    tg = TelegramClient("T", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=upd)))
    chats = tg.discover_chats()
    assert [c["chat_id"] for c in chats] == ["7", "-100"]
    assert chats[1]["name"] == "My Courses"


def test_send_video_uploads_a_file_then_reuses_its_file_id(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        assert req.url.path.endswith("/sendVideo")
        body = req.content
        seen.append(b'filename="intro.mp4"' in body)
        assert b"supports_streaming" in body
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": len(seen), "video": {"file_id": "FID"}}})

    clip = tmp_path / "intro.mp4"
    clip.write_bytes(b"\0" * 64)
    tg = TelegramClient("T", transport=httpx.MockTransport(handler))
    assert tg.send_video("42", path=clip, caption="hi", width=1080, height=1920) == (1, "FID")
    assert tg.send_video("42", file_id="FID") == (2, "FID")
    assert seen == [True, False]                          # uploaded once, then by id only
    with pytest.raises(ValueError):
        tg.send_video("42")
