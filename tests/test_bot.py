"""End-to-end conversation against fake Canvas + fake Telegram. No network."""
from __future__ import annotations

import json
from pathlib import Path
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ainl.bot import BotState, Conversation
from ainl.canvas import ApiSource
from ainl.canvas._http import CanvasAuthError
from ainl.canvas.auth import BearerAuth
from ainl.telegram.client import TelegramClient

BASE = "https://canvas.example.edu"
CHAT = "42"
NOW = datetime.now(timezone.utc)
DUE = (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z")
FAR = (NOW + timedelta(days=10)).isoformat().replace("+00:00", "Z")
PAST = (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")


def _j(body, status=200, headers=None):
    return httpx.Response(status, text=json.dumps(body), headers={"content-type": "application/json", **(headers or {})})


class FakeCanvas:
    def __init__(self):
        self.submitted: list[dict] = []
        self.uploaded: list[tuple[str, int]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/api/v1/users/self/profile":
            return _j({"id": "7", "name": "T", "login_id": "t", "time_zone": "America/Chicago"})
        if p == "/api/v1/planner/items":
            return _j([
                {"course_id": "1", "context_name": "ESL116: Reading", "plannable_type": "assignment", "plannable_id": "100",
                 "plannable_date": DUE, "plannable": {"id": "100", "title": "Week 3.1", "due_at": DUE, "points_possible": 5},
                 "submissions": {"submitted": False}, "html_url": "/courses/1/assignments/100"},
                {"course_id": "1", "context_name": "ESL116: Reading", "plannable_type": "assignment", "plannable_id": "101",
                 "plannable_date": DUE, "plannable": {"id": "101", "title": "Annotations", "due_at": DUE, "points_possible": 5},
                 "submissions": {"submitted": False}, "html_url": "/courses/1/assignments/101"},
                {"course_id": "2", "context_name": "MATH222: Calc", "plannable_type": "assignment", "plannable_id": "200",
                 "plannable_date": DUE, "plannable": {"id": "200", "title": "7.2 Homework", "due_at": DUE, "points_possible": 10},
                 "submissions": {"submitted": False}, "html_url": "/courses/2/assignments/200"},
                {"course_id": "1", "context_name": "ESL116: Reading", "plannable_type": "discussion_topic", "plannable_id": "300",
                 "plannable_date": DUE, "plannable": {"id": "300", "title": "Intro thread"},
                 "submissions": {"submitted": False}, "html_url": "/courses/1/discussion_topics/300"},
                # outside the window: 10 days out, and yesterday (overdue) — must not appear
                {"course_id": "2", "context_name": "MATH222: Calc", "plannable_type": "assignment", "plannable_id": "201",
                 "plannable_date": FAR, "plannable": {"id": "201", "title": "11.9 Homework", "due_at": FAR, "points_possible": 10},
                 "submissions": {"submitted": False}, "html_url": "/courses/2/assignments/201"},
                {"course_id": "2", "context_name": "MATH222: Calc", "plannable_type": "assignment", "plannable_id": "202",
                 "plannable_date": PAST, "plannable": {"id": "202", "title": "7.1 Homework", "due_at": PAST, "points_possible": 10},
                 "submissions": {"submitted": False, "missing": True}, "html_url": "/courses/2/assignments/202"},
            ])
        if p == "/api/v1/courses/1/assignments" and m == "GET":
            return _j([{"id": "100", "submission_types": ["online_text_entry"]},
                       {"id": "101", "submission_types": ["online_upload", "online_url"], "allowed_extensions": ["pdf"]}])
        if p == "/api/v1/courses/2/assignments" and m == "GET":
            return _j([{"id": "200", "submission_types": ["external_tool"]},
                       {"id": "201", "submission_types": ["external_tool"]},
                       {"id": "202", "submission_types": ["external_tool"]}])
        if p == "/api/v1/courses/1/assignments/100":
            return _j({"id": "100", "name": "Week 3.1", "description": "<p>Write <b>200 words</b>.</p><a href='/courses/1/files/9/download'>Guide.pdf</a>",
                       "due_at": DUE, "points_possible": 5, "submission_types": ["online_text_entry"], "html_url": f"{BASE}/courses/1/assignments/100"})
        if p == "/api/v1/courses/1/assignments/101":
            return _j({"id": "101", "name": "Annotations", "description": "<p>Upload your PDF.</p>", "due_at": DUE,
                       "points_possible": 5, "submission_types": ["online_upload", "online_url"], "allowed_extensions": ["pdf"]})
        if p == "/api/v1/files/9":
            return _j({"id": "9", "display_name": "Guide.pdf", "size": 3, "content-type": "application/pdf", "url": "https://files.example/9"})
        if p == "/api/v1/courses/1/assignments/101/submissions/self/files" and m == "POST":
            return _j({"upload_url": "https://upload.example/x", "upload_params": {"key": "k"}})
        if p.endswith("/submissions") and m == "POST":
            body = json.loads(req.content)["submission"]
            self.submitted.append({"path": p, **body})
            # Canvas echoes one attachment per file_id, named as uploaded
            att = [{"display_name": n} for n, _ in self.uploaded[-len(body.get("file_ids") or []):]] \
                if body["submission_type"] == "online_upload" else []
            return _j({"submitted_at": "2026-09-17T04:00:00Z", "attempt": 1, "workflow_state": "submitted",
                       "attachments": att})
        if p == "/api/v1/courses/1/discussion_topics/300/entries" and m == "POST":
            self.submitted.append({"path": p, **json.loads(req.content)})
            return _j({"id": "555", "created_at": "2026-09-17T04:01:00Z"})
        return httpx.Response(404, text=f"no route {m} {p}")


class FakeTelegram:
    def __init__(self):
        self.sent: list[dict] = []
        self.docs: list[str] = []
        self.photos: list[str] = []
        self.albums: list[int] = []
        self.album_captions: list[list[str]] = []
        self.edits: list[dict] = []
        self.media_edits: list[int] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/sendMessage"):
            self.sent.append({**json.loads(req.content), "_id": len(self.sent) + 1})
            return _j({"ok": True, "result": {"message_id": len(self.sent)}})
        if p.endswith("/sendDocument"):
            self.docs.append(req.content.decode(errors="ignore")[:200])
            return _j({"ok": True, "result": {"message_id": 99}})
        if p.endswith("/sendMediaGroup"):
            body = req.content.decode(errors="ignore")
            n = body.count('"type": "photo"') or body.count('"type":"photo"')
            self.albums.append(n)
            self.album_captions.append(re.findall(r'"caption":\s*"([^"]*)"', body))
            return _j({"ok": True, "result": [{"message_id": 90 + i} for i in range(n)]})
        if p.endswith("/sendPhoto"):
            self.photos.append(req.content.decode(errors="ignore")[:400])
            return _j({"ok": True, "result": {"message_id": 98}})
        if p.endswith("/editMessageText"):
            body = json.loads(req.content)
            self.edits.append(body)
            for m in self.sent:                       # the chat now shows the new text
                if m.get("_id") == body["message_id"]:
                    m.update(text=body["text"], reply_markup=body.get("reply_markup"))
            return _j({"ok": True, "result": True})
        if p.endswith("/editMessageReplyMarkup"):
            self.edits.append(json.loads(req.content))
            return _j({"ok": True, "result": True})
        if p.endswith("/editMessageMedia"):
            body = req.content.decode(errors="ignore")
            mid = re.search(r'name="message_id"\r\n\r\n(\d+)', body)
            self.media_edits.append(int(mid.group(1)) if mid else -1)
            return _j({"ok": True, "result": True})
        if p.endswith("/answerCallbackQuery"):
            return _j({"ok": True, "result": True})
        if p.endswith("/getFile"):
            return _j({"ok": True, "result": {"file_path": "documents/notes.pdf"}})
        if "/file/bot" in p:
            return httpx.Response(200, content=b"%PDF-fake")
        return httpx.Response(404, text=p)

    @property
    def last(self) -> str:
        return self.sent[-1]["text"]


@pytest.fixture
def world(monkeypatch):
    fc, ft = FakeCanvas(), FakeTelegram()
    # external uploads/downloads go through module-level httpx.get/post — stub them
    monkeypatch.setattr("ainl.canvas.api.httpx.get", lambda url, **kw: httpx.Response(200, content=b"PDF"))
    # every upload gets its own Canvas file id: 777, 778, 779 …
    monkeypatch.setattr("ainl.canvas.api.httpx.post",
                        lambda url, **kw: (fc.uploaded.append((kw["files"]["file"][0], len(kw["files"]["file"][1])))
                                           or _j({"id": str(776 + len(fc.uploaded))})))
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    conv = Conversation(canvas, tg, CHAT, BotState(), tz="America/Chicago")
    return conv, fc, ft


def msg(text=None, **extra):
    return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": int(CHAT)}, **({"text": text} if text else {}), **extra}}


def cb(data):
    return {"update_id": 2, "callback_query": {"id": "cbid", "data": data, "message": {"chat": {"id": int(CHAT)}}}}


def test_todo_splits_doable_from_external(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    out = ft.last
    assert "A · ESL116" in out and "B · MATH222" in out          # one block per course
    assert "7.2 Homework" in out and "↗" in out
    assert "11.9 Homework" not in out and "7.1 Homework" not in out   # outside today..+4d
    assert "more" not in out and "Open in the platform" not in out   # no second list
    codes = {m.title: m.code for m in conv.cs.menu}
    assert codes == {"Week 3.1": "A1", "Annotations": "A2", "Intro thread": "A3", "7.2 Homework": "B1"}


def test_text_submission_flow(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("a1"))
    # description rendered as text, attachment pushed as a document
    assert "Write 200 words." in ft.last or "Write 200 words." in ft.sent[-2]["text"]
    assert ft.docs and "Guide.pdf" in ft.docs[0]
    conv.handle(msg("My essay.\n\nSecond paragraph."))
    assert "Confirm?" in ft.last and ft.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "submit"
    conv.handle(cb("submit"))
    assert fc.submitted == [{"path": "/api/v1/courses/1/assignments/100/submissions",
                             "submission_type": "online_text_entry", "body": "<p>My essay.</p><p>Second paragraph.</p>"}]
    assert "✅ Submitted" in ft.last and "Week 3.1" in ft.last
    assert conv.cs.pending is None
    # receipt points at what's left; the submitted item is gone from the menu
    assert "⏭ Next:" in ft.last and "A2" in ft.last and "A3" in ft.last
    assert all(m.title != "Week 3.1" for m in conv.cs.menu)


def test_file_submission_flow(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("A2"))
    assert "a file (pdf)" in ft.last
    conv.handle(msg(document={"file_id": "F1", "file_name": "notes.pdf", "mime_type": "application/pdf", "file_size": 9}))
    assert "notes.pdf" in ft.last and "Confirm?" in ft.last
    conv.handle(cb("submit"))
    assert fc.uploaded == [("notes.pdf", 9)]
    assert fc.submitted[-1]["submission_type"] == "online_upload" and fc.submitted[-1]["file_ids"] == ["777"]
    assert "notes.pdf" in ft.last


def test_url_submission_and_cancel(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("A2"))
    conv.handle(msg("https://docs.google.com/document/d/abc"))
    assert "🔗" in ft.last
    conv.handle(cb("cancel"))
    assert "Cancelled" in ft.last and fc.submitted == [] and conv.cs.pending is None


def test_discussion_post(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("A3"))
    assert "Send your post" in ft.last
    conv.handle(msg("Hola, soy Alex."))
    conv.handle(cb("submit"))
    assert fc.submitted[-1] == {"path": "/api/v1/courses/1/discussion_topics/300/entries", "message": "<p>Hola, soy Alex.</p>"}
    assert "Posted to" in ft.last


def test_external_item_gives_link_not_form(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    assert "external platform" in ft.last and "/courses/2/assignments/200" in ft.last
    assert conv.cs.pending is None


def test_ignores_other_chats(world):
    conv, fc, ft = world
    conv.handle({"update_id": 9, "message": {"chat": {"id": 999}, "text": "/todo"}})
    assert ft.sent == []


def photo(i: int, group: str = "G1"):
    """One message of a Telegram album: several sizes, shared media_group_id."""
    return msg(media_group_id=group, photo=[{"file_id": f"P{i}s", "file_size": 100},
                                            {"file_id": f"P{i}", "file_size": 9000}])


def _queue_album(conv, n=5):
    conv.handle(msg("/todo"))
    conv.handle(msg("A2"))
    for i in range(1, n + 1):
        conv.handle(photo(i))


def test_album_of_five_photos_is_one_submission(world):
    conv, fc, ft = world
    _queue_album(conv, 5)
    d = conv.cs.draft
    assert d.mode == "file" and d.filenames == [f"photo-{i}.jpg" for i in range(1, 6)]
    assert len(conv._file_bytes[CHAT]) == 5
    # the running confirmation: count + names + the 3-button keyboard
    assert ft.last.startswith("📎 5 files") and "photo-5.jpg" in ft.last and "send more" in ft.last
    assert [b["callback_data"] for b in ft.sent[-1]["reply_markup"]["inline_keyboard"][0]] == ["submit", "remove_last", "cancel"]
    conv.handle(cb("submit"))
    assert [n for n, _ in fc.uploaded] == [f"photo-{i}.jpg" for i in range(1, 6)]
    assert len(fc.submitted) == 1                                   # ONE POST …/submissions
    assert fc.submitted[0]["submission_type"] == "online_upload"
    assert fc.submitted[0]["file_ids"] == ["777", "778", "779", "780", "781"]
    assert "✅ Submitted" in ft.last and all(f"photo-{i}.jpg" in ft.last for i in range(1, 6))
    assert conv.cs.pending is None and conv.cs.draft is None and CHAT not in conv._file_bytes


def test_remove_last_leaves_four(world):
    conv, fc, ft = world
    _queue_album(conv, 5)
    conv.handle(cb("remove_last"))
    assert conv.cs.draft.filenames == [f"photo-{i}.jpg" for i in range(1, 5)]
    assert len(conv._file_bytes[CHAT]) == 4
    assert ft.last.startswith("📎 4 files") and "removed photo-5.jpg" in ft.last
    conv.handle(cb("submit"))
    assert fc.submitted[-1]["file_ids"] == ["777", "778", "779", "780"]


def test_restart_loses_bytes_asks_to_resend(tmp_path, world):
    conv, fc, ft = world
    _queue_album(conv, 3)
    conv.state.save(tmp_path / "s.json")
    # a fresh process: names came back from disk, bytes did not
    conv2 = Conversation(conv.canvas, conv.tg, CHAT, BotState.load(tmp_path / "s.json"), tz="America/Chicago")
    assert conv2.cs.draft.filenames == ["photo-1.jpg", "photo-2.jpg", "photo-3.jpg"]
    conv2.handle(cb("submit"))
    assert fc.submitted == [] and fc.uploaded == []                 # never submits an empty upload
    assert "re-send" in ft.last and "Nothing was submitted" in ft.last
    assert conv2.cs.draft is None and conv2.cs.pending is not None  # item still open: just send again
    conv2.handle(photo(1))
    conv2.handle(cb("submit"))
    assert fc.submitted[-1]["file_ids"] == ["777"]


def test_duplicate_document_names_get_suffix(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("A2"))
    for i in range(3):
        conv.handle(msg(document={"file_id": f"F{i}", "file_name": "notes.pdf", "mime_type": "application/pdf"}))
    assert conv.cs.draft.filenames == ["notes.pdf", "notes-2.pdf", "notes-3.pdf"]


def test_text_after_files_replaces_and_says_so(world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("A2"))
    conv.handle(photo(1))
    conv.handle(photo(2))
    conv.handle(msg("https://docs.google.com/document/d/abc"))    # A2 takes online_url too
    assert conv.cs.draft.mode == "url" and CHAT not in conv._file_bytes
    assert "🔗" in ft.last and "replaced your 2 queued files" in ft.last
    conv.handle(photo(3))                                            # and back: file wins, url gone
    assert conv.cs.draft.mode == "file" and conv.cs.draft.filenames == ["photo-1.jpg"]
    assert "replaced your link draft" in ft.last


def test_state_roundtrip(tmp_path, world):
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    conv.handle(msg("A1"))
    conv.state.offset = 123
    conv.state.save(tmp_path / "s.json")
    st = BotState.load(tmp_path / "s.json")
    assert st.offset == 123 and st.chat(CHAT).pending.title == "Week 3.1" and st.chat(CHAT).menu[0].code == "A1"


# -- channel switching (token primary, cookie fallback) -----------------------

def test_channel_shows_live_channel_and_pin(world, monkeypatch, tmp_path):
    conv, _, ft = world
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    conv.handle(msg("/channel"))
    assert "token" in ft.last and "auto" in ft.last


def test_channel_pin_rejects_garbage(world, monkeypatch, tmp_path):
    conv, _, ft = world
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    conv.handle(msg("/channel banana"))
    assert "must be one of" in ft.last


def test_auth_failure_falls_back_and_says_so(world, monkeypatch):
    conv, _, ft = world
    replacement = ApiSource(BASE, BearerAuth("t2"), transport=httpx.MockTransport(FakeCanvas()))
    replacement._channel = "cookie"
    monkeypatch.setattr(type(conv.canvas), "recover_auth",
                        lambda self, exc, interactive=False: replacement)

    def boom(*a, **kw):
        raise CanvasAuthError(401, "/api/v1/planner/items", "Invalid access token.")

    monkeypatch.setattr(type(conv.canvas), "planner", boom)
    conv.handle(msg("/todo"))
    assert conv.canvas is replacement
    assert "switched to" in ft.last and "revoked" in ft.last


def test_auth_failure_without_fallback_tells_the_host_command(world, monkeypatch):
    conv, _, ft = world
    monkeypatch.setattr(type(conv.canvas), "recover_auth",
                        lambda self, exc, interactive=False: None)

    def boom(*a, **kw):
        raise CanvasAuthError(401, "/api/v1/planner/items", "Invalid access token.")

    monkeypatch.setattr(type(conv.canvas), "planner", boom)
    conv.handle(msg("/todo"))
    assert "--login" in ft.last


def _expire_planner(conv, monkeypatch):
    def boom(*a, **kw):
        raise CanvasAuthError(401, "/api/v1/planner/items", "Invalid access token.")
    monkeypatch.setattr(type(conv.canvas), "planner", boom)


def test_expired_session_with_a_login_page_sends_the_button(world, monkeypatch):
    """No terminal: the message carries the 🔐 Canvas login button, and is not
    filed as a failure (an expired session is the normal course of things)."""
    conv, _, ft = world
    monkeypatch.setenv("AINL_LOGIN_URL", "https://example.org/native-bot-learning/login")
    monkeypatch.setattr(type(conv.canvas), "recover_auth",
                        lambda self, exc, interactive=False: None)
    _expire_planner(conv, monkeypatch)
    conv.handle(msg("/todo"))
    last = ft.sent[-1]
    assert last["text"].startswith("🔐") and "--login" not in last["text"]
    button = json.loads(last["reply_markup"])["inline_keyboard"][0][0] \
        if isinstance(last["reply_markup"], str) else last["reply_markup"]["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == "https://example.org/native-bot-learning/login"


def test_needs_login_also_gets_the_button_when_there_is_a_login_page(world, monkeypatch):
    from ainl.canvas.channel import NeedsCanvasLogin
    conv, _, ft = world
    monkeypatch.setenv("AINL_LOGIN_URL", "https://example.org/login")
    monkeypatch.setattr(type(conv.canvas), "recover_auth",
                        lambda self, e, interactive=False: (_ for _ in ()).throw(
                            NeedsCanvasLogin("go to the window and sign in")))
    _expire_planner(conv, monkeypatch)
    conv.handle(msg("/todo"))
    assert "Canvas login" in ft.last and "reply_markup" in ft.sent[-1]


def test_a_new_login_in_the_browser_is_picked_up_without_a_restart(world, monkeypatch):
    """Cookie channel, session dead, then the owner signs in through the button:
    the next failure re-reads the browser and carries on — no restart."""
    conv, _, ft = world
    conv.canvas._channel = "cookie"
    fresh = ApiSource(BASE, BearerAuth("t2"), transport=httpx.MockTransport(FakeCanvas()))
    fresh._channel = "cookie"
    from ainl.canvas import channel
    monkeypatch.setattr(channel, "preference", lambda: "cookie")
    monkeypatch.setattr(channel, "open_source", lambda prefer=None, interactive=False: fresh)
    _expire_planner(conv, monkeypatch)
    conv.handle(msg("/todo"))
    assert conv.canvas is fresh
    assert "Picked up your new Canvas login" in ft.last


def test_keepalive_tells_once_per_loss():
    from ainl.canvas.keepalive import observe
    told, n = False, []
    for alive in (True, False, False, True, False):
        told = observe(alive, told, lambda: n.append(1))
    assert n == [1, 1]            # gone (told), still gone (quiet), back, gone again (told)


def test_pinned_token_refuses_to_fall_back(monkeypatch, tmp_path):
    from ainl.canvas import channel
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    channel.set_preference("token")
    src = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(FakeCanvas()))
    assert src.recover_auth(CanvasAuthError(401, "/x", "nope")) is None


# -- /math: WebAssign questions out, one answer sheet in, fill (never submit) --

from ainl.external.webassign import (Assignment, Option, Part,  # noqa: E402
                                     Question, WebAssignUnavailable)


def _mathq(n, qid, names):
    """A question whose parts are all maths boxes — the common case."""
    return Question(n, qid, list(names), [Part("math", nm) for nm in names])
from ainl.mathinput.pipeline import Result  # noqa: E402


class FakeWebAssign:
    """Stands in for the browser. Records what would have been written."""

    def __init__(self, questions=None, fail=None):
        self.filled: dict[str, str] = {}
        self.submitted: list[str] = []
        self.fail = fail
        self.qs = questions or [
            _mathq(1, "question11_0", ["RA_11_0_0_91"]),
            _mathq(2, "question22_1", ["RA_22_1_0_92"]),
            _mathq(3, "question33_2", ["RA_33_2_0_93", "RA_33_2_1_94"]),  # two blanks
        ]

    def assignments(self, include_quizzes=False):
        if self.fail:
            raise WebAssignUnavailable(self.fail)
        return [Assignment("40575442", "7.5 Homework-Strategy", "Thu, Sep 24", "https://x/?dep=1")]

    def questions(self, dep):
        return "7.5 Homework-Strategy", self.qs, [b"\x89PNG-q" + bytes([q.n]) for q in self.qs]

    def fill(self, dep, answers):
        self.filled.update(answers)
        return {"readback": dict(answers), "missing": []}, [b"\x89PNG-filled"]

    def step_shot(self, dep, box):
        return b"\x89PNG-step-" + box.encode()

    def begin_step(self, dep, button_id):
        """Pretend the page opened that part: the caller's `opened` says what
        the question looks like afterwards."""
        self.begun = getattr(self, "begun", []) + [button_id]
        q = self.opened[button_id]
        return q, b"\x89PNG-step"

    def submit(self, dep, boxes, expect=None):
        self.submitted.extend(boxes)
        return ({"submitted": ["question11_0"], "failed": {},
                 "outcome": {"question11_0": {"points": "1.25 / 1.25 Points",
                                              "marks": ["right"]}}},
                [b"\x89PNG-graded"])


def math_world(monkeypatch, latex=r"1. x^2 \quad 2. \frac{1}{2}", wa=None):
    fc, ft = FakeCanvas(), FakeTelegram()
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    wa = wa or FakeWebAssign()
    conv = Conversation(canvas, tg, CHAT, BotState(), tz="America/Chicago", webassign=wa,
                        transcribe=lambda data, hint="": Result(latex=latex, agreed=True))
    return conv, wa, ft


def photo_msg():
    return msg(photo=[{"file_id": "F1", "file_size": 100}])


def test_math_lists_then_sends_one_photo_per_question(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/math"))
    assert "Which one" in ft.last
    conv.handle(cb("m:40575442"))
    assert ft.albums == [3]                      # one album, not three sends
    assert conv.cs.math.stage == "shoot"
    assert conv.cs.math.targets == {1: ["RA_11_0_0_91"], 2: ["RA_22_1_0_92"],
                                    3: ["RA_33_2_0_93", "RA_33_2_1_94"]}
    assert conv.cs.math.multipart == [3]          # q3 has two boxes
    assert "3 has 2 blanks" in ft.last and "commas" in ft.last


def test_answer_sheet_fills_the_right_boxes_and_submits_nothing(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    assert conv.cs.math.stage == "confirm"
    assert conv.cs.math.answers == {1: ["x^2"], 2: [r"\frac{1}{2}"]}
    conv.handle(cb("mfill"))
    assert set(wa.filled) == {"RA_11_0_0_91", "RA_22_1_0_92"}
    assert "<mfrac>" in wa.filled["RA_22_1_0_92"]      # MathML, not LaTeX
    assert "Submit?" in ft.last                      # asked, not done
    assert conv.cs.math.stage == "filled"
    assert not wa.submitted                          # nothing submitted yet


def test_photo_is_an_answer_sheet_only_during_math(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(photo_msg())                       # no /math yet
    assert "Pick an item first" in ft.last         # went to the Canvas upload path
    assert not wa.filled


def test_a_wrong_reading_can_be_corrected_by_number(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    conv.handle(msg(r"2: \sin(x)"))
    assert conv.cs.math.answers[2] == [r"\sin(x)"]
    conv.handle(cb("mfill"))
    assert "<mi>sin</mi>" in wa.filled["RA_22_1_0_92"]


def test_multi_blank_question_fills_each_box_in_order(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, latex=r"3. x^2, \sin(x)")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    assert conv.cs.math.answers == {3: ["x^2", r"\sin(x)"]}
    conv.handle(cb("mfill"))
    assert "<msup>" in wa.filled["RA_33_2_0_93"]     # x^2 -> first blank
    assert "<mi>sin</mi>" in wa.filled["RA_33_2_1_94"]


def test_too_few_answers_for_a_multi_blank_question_is_flagged(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, latex=r"3. x^2")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    assert "2 blanks" in ft.last and "only have 1" in ft.last


def test_answers_for_questions_not_in_this_assignment_are_dropped(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, latex=r"1. x \quad 7. y")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    assert set(conv.cs.math.answers) == {1}
    assert "Ignored" in ft.last and "7" in ft.last


def test_unreadable_sheet_asks_for_another_photo(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.transcribe = lambda data, hint="": Result(needs_human=True, errors={"gemini": "bad"})
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    assert "couldn't read" in ft.last.lower()
    assert not wa.filled


def test_no_browser_says_how_to_start_it(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=FakeWebAssign(fail="run scripts/browser_start.py"))
    conv.handle(msg("/math"))
    assert "browser_start.py" in ft.last


def test_cancel_drops_the_math_session(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(msg("/cancel"))
    assert conv.cs.math is None


# -- submit / modify / cancel --------------------------------------------------

def _to_filled(monkeypatch, **kw):
    conv, wa, ft = math_world(monkeypatch, **kw)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    conv.handle(cb("mfill"))
    return conv, wa, ft


def _submit_button(ft) -> str:
    """The Submit button the bot really sent last — its payload as Telegram
    will send it back, not one typed into the test."""
    datas = [b["callback_data"] for m in ft.sent if m.get("reply_markup")
             for row in m["reply_markup"]["inline_keyboard"] for b in row]
    return [d for d in datas if d.startswith("msubmit")][-1]


def test_submit_only_happens_after_the_button(monkeypatch):
    conv, wa, ft = _to_filled(monkeypatch)
    assert not wa.submitted                       # filling submits nothing
    assert _submit_button(ft) == "msubmit:40575442"   # the button names its round
    conv.handle(cb(_submit_button(ft)))
    assert wa.submitted == ["RA_11_0_0_91", "RA_22_1_0_92"]
    assert "1.25 / 1.25 Points" in ft.last        # the graded result comes back
    assert conv.cs.math is None


def test_cancel_at_the_filled_stage_submits_nothing(monkeypatch):
    conv, wa, ft = _to_filled(monkeypatch)
    conv.handle(cb("cancel"))
    assert not wa.submitted
    assert conv.cs.math is None


def test_modify_then_type_a_correction_costs_no_model_call(monkeypatch):
    calls = []
    conv, wa, ft = _to_filled(monkeypatch)
    conv.transcribe = lambda data, hint="": calls.append(1)   # must not be reached
    conv.handle(cb("mmodify"))
    assert "Which one is wrong" in ft.last
    conv.handle(cb("mfix:2"))
    assert conv.cs.math.stage == "fixing" and conv.cs.math.fixing == 2
    conv.handle(msg(r"\sin(x)"))                  # no "2:" prefix needed now
    assert conv.cs.math.answers[2] == [r"\sin(x)"]
    assert conv.cs.math.stage == "confirm"
    assert not calls                              # zero LLM calls for a typed fix
    conv.handle(cb("mfill"))
    conv.handle(cb(_submit_button(ft)))
    assert "<mi>sin</mi>" in wa.filled["RA_22_1_0_92"]


def test_an_old_submit_pressed_after_a_fix_submits_nothing(monkeypatch):
    """The bug this guards: WebAssign grades what the PAGE holds. Pressing the
    Submit sent before a fix graded the old answer while the chat showed the new."""
    conv, wa, ft = _to_filled(monkeypatch)
    old = _submit_button(ft)
    conv.handle(cb("mmodify"))
    conv.handle(cb("mfix:2"))
    conv.handle(msg(r"\sin(x)"))                  # chat now shows the new answer
    conv.handle(cb(old))
    assert not wa.submitted
    assert "nothing was submitted" in ft.last
    assert "mfill" in json.dumps(ft.sent[-1]["reply_markup"])
    conv.handle(cb("mfill"))                      # page brought up to date…
    conv.handle(cb(_submit_button(ft)))           # …and only then submitted
    assert wa.submitted and "<mi>sin</mi>" in wa.filled["RA_22_1_0_92"]


def test_a_submit_button_from_another_round_submits_nothing(monkeypatch):
    conv, wa, ft = _to_filled(monkeypatch)
    for stale in ("msubmit:99999999", "msubmit"):   # another round; pre-fix button
        conv.handle(cb(stale))
        assert "nothing was submitted" in ft.last
    assert not wa.submitted
    assert conv.cs.math is not None


def test_a_photo_while_fixing_replaces_only_that_answer(monkeypatch):
    conv, wa, ft = _to_filled(monkeypatch)
    conv.handle(cb("mmodify"))
    conv.handle(cb("mfix:1"))
    conv.transcribe = lambda data, hint="": Result(latex="y^3", agreed=True)
    conv.handle(photo_msg())
    assert conv.cs.math.answers[1] == ["y^3"]
    assert conv.cs.math.answers[2] == [r"\frac{1}{2}"]   # untouched


def test_back_from_modify_returns_to_the_submit_choice(monkeypatch):
    conv, wa, ft = _to_filled(monkeypatch)
    conv.handle(cb("mmodify"))
    conv.handle(cb("mback"))
    assert not wa.submitted
    assert conv.cs.math is not None
    assert "msubmit:40575442" in json.dumps(ft.sent[-1]["reply_markup"])


def test_back_after_a_fix_offers_fill_not_submit(monkeypatch):
    conv, wa, ft = _to_filled(monkeypatch)
    conv.handle(cb("mmodify"))
    conv.handle(cb("mfix:2"))
    conv.handle(msg(r"\sin(x)"))
    conv.handle(cb("mmodify"))
    conv.handle(cb("mback"))
    markup = json.dumps(ft.sent[-1]["reply_markup"])
    assert "msubmit" not in markup and "mfill" in markup
    assert not wa.submitted


class _Deadline(BaseException):
    """The deadline hitting mid-press. Not an Exception, so submit's own
    per-question try/except can't swallow it — just as the real one can't."""


def test_a_submit_that_times_out_is_never_pressed_again(monkeypatch):
    """The bug this guards: submit ran under the retrying wrapper, so a timeout
    after the click landed pressed SUBMIT ANSWER a second time. No real browser."""
    from ainl.external import webassign as wam
    from ainl.util import browserthread
    clicks, runs = [], []

    class Button:
        def click(self, timeout=None):
            clicks.append(1)
            raise _Deadline                        # the click landed, then time ran out

    class Tab:
        def locator(self, sel):
            return type("Found", (), {"first": Button(), "click": Button().click})()

        def evaluate(self, js, arg):
            if js is wam._SUBMIT_BUTTON_JS:        # which button: the question's own
                return "submit_" + arg[0].replace("question", "")
            assert js is wam._OUTCOME_JS           # the read-only look afterwards
            return {d: {"points": "1.25 / 1.25 Points", "marks": []} for d in arg}

    def with_tab(fn, attempts=2, budget=25.0, need_tab=True):
        runs.append(attempts)
        try:
            return fn(None, Tab())
        except _Deadline:
            raise browserthread.Timeout("deadline") from None

    client = wam.WebAssign()
    monkeypatch.setattr(client, "_open", lambda tab, dep: None)
    monkeypatch.setattr(client, "_with_tab", with_tab)
    with pytest.raises(wam.WebAssignUnavailable, match="don't know whether") as e:
        client.submit("40575442", ["RA_11_0_0_91", "RA_22_1_0_92"])
    assert clicks == [1]                           # pressed once, never again
    assert runs == [1, 1]                          # one submit, one read-only look
    assert "1.25 / 1.25 Points" in str(e.value)    # told what the page shows


def test_a_channel_that_cannot_open_is_not_pinned(monkeypatch, tmp_path):
    """The bug this guards: pinning first left the preference set to a channel
    that could not be opened, and the bot then refused to start at all."""
    from ainl.canvas import channel
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    channel.set_preference("auto")
    conv, wa, ft = math_world(monkeypatch)
    monkeypatch.setattr(channel, "open_source",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no session")))
    conv.handle(msg("/channel cookie"))
    assert channel.preference() == "auto"          # unchanged
    assert "Still on" in ft.last and "no session" in ft.last


def test_expired_canvas_puts_the_login_page_in_front(world, monkeypatch):
    """When both channels are out, the student should not be told to go and run
    a command on the host — the sign-in page is opened for them."""
    from ainl.canvas.channel import NeedsCanvasLogin
    conv, _, ft = world

    def boom(*a, **kw):
        raise CanvasAuthError(401, "/api/v1/planner/items", "Invalid access token.")

    monkeypatch.setattr(type(conv.canvas), "planner", boom)
    monkeypatch.setattr(type(conv.canvas), "recover_auth",
                        lambda self, e, interactive=False: (_ for _ in ()).throw(
                            NeedsCanvasLogin("I've opened the Canvas sign-in page")))
    conv.handle(msg("/todo"))
    assert "opened the Canvas sign-in page" in ft.last
    assert "--login" not in ft.last


# -- /math lists quizzes too, behind a confirmation ---------------------------

def test_math_lists_homework_and_quizzes_together(monkeypatch):
    from ainl.external.webassign import Assignment

    class WAQuiz(FakeWebAssign):
        def assignments(self, include_quizzes=False):
            out = [Assignment("11", "7.5 Homework-Strategy", "Thu, Sep 24"),
                   # confirmed untimed by the student: opens directly
                   Assignment("22", "Module Quiz 10 - Integration Bee", "Sun, Sep 20"),
                   # rules unknown: must ask first
                   Assignment("33", "Midterm Quiz 2", "Fri, Oct 3")]
            return out if include_quizzes else [a for a in out if not a.is_quiz]

    conv, wa, ft = math_world(monkeypatch, wa=WAQuiz())
    conv.handle(msg("/math"))
    markup = json.dumps(ft.sent[-1]["reply_markup"])
    assert '"m:11"' in markup                      # homework: straight in
    assert '"m:22"' in markup                      # Module Quiz: straight in too
    assert '"mq:33"' in markup                     # unknown quiz: confirm first
    assert "23f1" in markup                        # the ⏱ marker, JSON-escaped


def test_an_unknown_quiz_opens_only_after_saying_yes(monkeypatch):
    from ainl.external.webassign import Assignment

    class WAQuiz(FakeWebAssign):
        def assignments(self, include_quizzes=False):
            return [Assignment("33", "Midterm Quiz 2", "Fri, Oct 3")]

    conv, wa, ft = math_world(monkeypatch, wa=WAQuiz())
    conv.handle(msg("/math"))
    conv.handle(cb("mq:33"))
    assert "starts the clock" in ft.last
    assert conv.cs.math.stage == "pick"            # nothing opened yet
    conv.handle(cb("m:33"))
    assert conv.cs.math.stage == "shoot"


# -- ending a round frees what it was holding ---------------------------------

class CachingWA(FakeWebAssign):
    """Mimics the real cache: question images stay in memory until forgotten."""

    def __init__(self):
        super().__init__()
        self.cache: dict[str, bytes] = {}

    def questions(self, dep):
        self.cache[dep] = b"\x89PNG" * 1000
        return super().questions(dep)

    def forget(self, dep=None):
        n = len(self.cache) if dep is None else (1 if dep in self.cache else 0)
        self.cache.clear() if dep is None else self.cache.pop(dep, None)
        return n


def test_restart_frees_the_question_images(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=CachingWA())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    assert wa.cache                                  # held while the round is live
    conv.handle(msg("/restart"))
    assert not wa.cache                              # and gone when it ends
    assert conv.cs.math is None
    assert "pictures" in ft.last and "stage" not in ft.last


def test_cancel_frees_them_too(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=CachingWA())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("cancel"))
    assert not wa.cache
    assert "nothing kept" in ft.last


def test_slash_cancel_frees_them_too(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=CachingWA())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(msg("/cancel"))
    assert not wa.cache


def test_the_cache_cannot_grow_without_bound():
    from ainl.external.webassign import WebAssign
    wa = WebAssign()
    for i in range(5):
        wa._cache[str(i)] = (float(i), ("t", [], []))
        while len(wa._cache) > wa.CACHE_MAX:
            wa._cache.pop(min(wa._cache, key=lambda k: wa._cache[k][0]), None)
    assert len(wa._cache) == wa.CACHE_MAX
    assert set(wa._cache) == {"3", "4"}              # oldest dropped first


def test_a_code_for_webassign_homework_opens_it_immediately(monkeypatch):
    """A code from /todo already means "open this one" — a second tap to
    confirm protects nothing and just adds a step."""
    conv, wa, ft = math_world(monkeypatch)
    monkeypatch.setattr(type(conv), "_webassign_dep", lambda self, item: "40575442")
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))                        # the MATH222 external item
    assert conv.cs.math is not None and conv.cs.math.stage == "shoot"
    assert ft.albums                              # questions already sent


def test_a_code_for_an_unknown_quiz_still_asks_first(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    monkeypatch.setattr(type(conv), "_webassign_dep", lambda self, item: "99")
    conv.handle(msg("/todo"))
    for m in conv.cs.menu:                        # make the item look like a quiz
        if m.kind == "external":
            m.title = "Midterm Quiz 2"
    conv.handle(msg("B1"))
    assert "starts the clock" in ft.last
    assert conv.cs.math is None or conv.cs.math.stage != "shoot"


# -- questions answered by tapping --------------------------------------------

class MixedWA(FakeWebAssign):
    """The real shape of 7.8 Homework: a choice AND a maths box in one question,
    plus a question that is only a choice."""

    def __init__(self):
        super().__init__()
        self.chosen: dict = {}
        self.qs = [
            _mathq(1, "question11_0", ["RA_11_0_0_91"]),
            Question(2, "question22_1", ["RA_22_1_1_92"],
                     [Part("choice", "RC_22_1_0_92",
                           [Option("0", "convergent"), Option("1", "divergent")]),
                      Part("math", "RA_22_1_1_92")]),
            Question(3, "question33_2", [],
                     [Part("choice", "RC_33_2_0_93",
                           [Option("0", "yes"), Option("1", "no")])]),
        ]

    def choose(self, dep, picks, skip_shots=()):
        self.chosen.update(picks)
        self.skipped = tuple(skip_shots)
        return {"done": list(picks), "missing": [], "readback": dict(picks),
                "saved": ["q"], "shots": [b"PNG-tapped"]}


def test_only_the_written_questions_go_on_the_answer_sheet(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    ms = conv.cs.math
    assert sorted(ms.targets) == [1, 2]          # 3 is tap-only, nothing to write
    assert [t["n"] for t in ms.taps] == [2, 3]
    # the instruction names only the ones needing a pen
    sheet = next(m["text"] for m in ft.sent if "one sheet" in m["text"])
    assert "answers for 1, 2" in sheet


def test_tapping_answers_a_choice_without_writing_or_a_model_call(monkeypatch):
    calls = []
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA())
    conv.transcribe = lambda data, hint="": calls.append(1)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("c:40575442:0:0"))                     # question 2 -> convergent
    assert conv.cs.math.picked == {"RC_22_1_0_92": ["0"]}
    assert "convergent" in ft.last
    assert not calls                             # tapping costs no model call


def test_a_tapped_choice_is_written_and_submitted_with_the_rest(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA(), latex=r"1. x^2 \quad 2. \frac{1}{2}")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("c:40575442:0:1"))                     # question 2 -> divergent
    conv.handle(cb("c:40575442:1:0"))                     # question 3 -> yes
    conv.handle(photo_msg())
    conv.handle(cb("mfill"))
    assert wa.chosen == {"RC_22_1_0_92": ["1"], "RC_33_2_0_93": ["0"]}
    assert "RA_11_0_0_91" in wa.filled
    conv.handle(cb(_submit_button(ft)))
    assert "RC_33_2_0_93" in wa.submitted        # the tap-only question too


def test_an_all_tap_assignment_needs_no_photo(monkeypatch):
    class AllTaps(MixedWA):
        def __init__(self):
            super().__init__()
            self.qs = [Question(1, "question11_0", [],
                                [Part("choice", "RC_11_0_0_91",
                                      [Option("0", "yes"), Option("1", "no")])])]

    conv, wa, ft = math_world(monkeypatch, wa=AllTaps())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    assert "no writing needed" in ft.last
    conv.handle(cb("c:40575442:0:0"))
    assert "That's all of them" in ft.last


def test_the_tap_buttons_are_actually_sent(monkeypatch):
    """The other tap tests inject the callback, so they passed while no button
    was ever sent. This one looks at what reached the chat."""
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    datas = [b["callback_data"] for m in ft.sent if m.get("reply_markup")
             for row in m["reply_markup"]["inline_keyboard"] for b in row]
    assert "c:40575442:0:0" in datas and "c:40575442:0:1" in datas          # question 2's choice
    assert "c:40575442:1:0" in datas                                # question 3's
    labels = [b["text"] for m in ft.sent if m.get("reply_markup")
              for row in m["reply_markup"]["inline_keyboard"] for b in row]
    assert "convergent" in labels


def test_an_all_tap_round_can_be_filled_and_submitted(monkeypatch):
    class AllTaps(MixedWA):
        def __init__(self):
            super().__init__()
            self.qs = [Question(1, "question11_0", [],
                                [Part("choice", "RC_11_0_0_91",
                                      [Option("0", "yes"), Option("1", "no")])])]

    conv, wa, ft = math_world(monkeypatch, wa=AllTaps())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("c:40575442:0:1"))
    conv.handle(cb("mfill"))
    assert wa.chosen == {"RC_11_0_0_91": ["1"]}
    assert "Filled 1 tapped answer(s)" in ft.last          # not "Filled 0"
    conv.handle(cb(_submit_button(ft)))
    assert "RC_11_0_0_91" in wa.submitted


def test_a_question_of_plain_text_boxes_is_written_as_typed(monkeypatch):
    """Module Quiz 13: four typed blanks, no maths editor. multipart() counted
    them, targets did not, and indexing one with the other was a KeyError.
    Now they are blanks like any other — filled with the bare text."""
    class TextOnly(MixedWA):
        def __init__(self):
            super().__init__()
            names = ["RA_11_0_0_1", "RA_11_0_1_1"]
            self.qs = [Question(1, "question11_0", names, [Part("text", n) for n in names]),
                       _mathq(2, "question22_1", ["RA_22_1_0_2"])]

    conv, wa, ft = math_world(monkeypatch, wa=TextOnly(), latex=r"1. yes, 4  2. x^2")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    assert conv.cs.math.stage == "shoot"
    assert conv.cs.math.targets[1] == ["RA_11_0_0_1", "RA_11_0_1_1"]
    assert "typed box" in ft.last and "need typing" not in ft.last
    conv.handle(photo_msg())
    conv.handle(cb("mfill"))
    assert wa.filled["RA_11_0_0_1"] == "yes"            # bare text, no <math>
    assert wa.filled["RA_11_0_1_1"] == "4"
    assert "<math" in wa.filled["RA_22_1_0_2"]           # the maths box still gets MathML


def test_a_numeric_box_beside_a_maths_box_is_the_second_blank(monkeypatch):
    """9.3 Homework q7 (2026-09-25): (a) a maths box, (b) an RN_ box "round to
    one decimal place". The RN_ box was not seen at all, so (b) could never be
    filled. It is the question's second blank, typed as-is."""
    class Tank(MixedWA):
        def __init__(self):
            super().__init__()
            self.qs = [Question(7, "question4598094_6", ["RA_4598094_6_0_4607000"],
                                [Part("math", "RA_4598094_6_0_4607000"),
                                 Part("text", "RN_4598094_6_9_4607000")])]

    conv, wa, ft = math_world(monkeypatch, wa=Tank(), latex=r"7. (a) y=11e^{-t/100} (b) y=8.1 kg")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    assert "7 has 2 blanks" in ft.last
    conv.handle(photo_msg())
    assert conv.cs.math.answers == {7: [r"11e^{-t/100}", "8.1 kg"]}   # read as written…
    conv.handle(cb("mfill"))
    assert "<math" in wa.filled["RA_4598094_6_0_4607000"]
    assert wa.filled["RN_4598094_6_9_4607000"] == "8.1"   # …filled without the unit the page prints
    # and the fix-by-number path keeps the typed one typed
    conv.handle(msg("7: 11e^{-t/100}, 8.2"))
    conv.handle(cb("mfill"))
    assert wa.filled["RN_4598094_6_9_4607000"] == "8.2"


def test_a_master_it_question_is_done_one_step_at_a_time(monkeypatch):
    """9.3 Homework q6 (SCalc9 9.3.047.MI.SA), as seen 2026-09-25: after
    "Click here to begin!" Exercise (a) is a chain of Steps, each ONE box with
    its own SUBMIT; a step that is right is replaced by the next, a wrong one
    stays open with ✗ for another try; when (a) is finished, (b) has its own
    "begin". Nothing can be written ahead, so each step is asked for in chat,
    typed or photographed, and submitted only after the student confirms."""
    from dataclasses import asdict
    from ainl.bot.state import MathSession
    dom, a, b = "question4598017_5", "step_button_id_4598017_0_0_0", "step_button_id_4598017_0_1_0"
    s1, s2, b1 = "RA_4598017_5_0_1", "RA_4598017_5_1_1", "RA_4598017_5_9_1"
    P = lambda box, label, wrong=False: Part("math", box, label=label, wrong=wrong)   # noqa: E731
    fresh   = Question(6, dom, [], [Part("step", a, label="Exercise (a)")])
    step1   = Question(6, dom, [s1], [P(s1, "Step 1"), Part("step", b, label="Exercise (b)")])
    step2   = Question(6, dom, [s2], [P(s2, "Step 2"), Part("step", b, label="Exercise (b)")])
    step2x  = Question(6, dom, [s2], [P(s2, "Step 2", wrong=True), Part("step", b, label="Exercise (b)")])
    a_done  = Question(6, dom, [], [Part("step", b, label="Exercise (b)")])
    b_step1 = Question(6, dom, [b1], [P(b1, "Step 1")])
    finished = Question(6, dom, [], [Part("unknown", "")])

    class MasterIt(MixedWA):
        def __init__(self):
            super().__init__()
            self.q1 = _mathq(1, "question11_0", ["RA_11_0_0_91"])
            self.qs = [self.q1, fresh]
            self.opened = {a: step1, b: b_step1}
            self.after_submit = iter([step1, step2, step2x, a_done, finished])   # page after each SUBMIT
        def begin_step(self, dep, button_id):
            q, shot = super().begin_step(dep, button_id)
            self.qs = [self.q1, q]
            return q, shot
        def submit(self, dep, boxes, expect=None):
            self.submitted.extend(boxes)
            self.qs = [self.q1, next(self.after_submit)]
            return ({"submitted": [dom], "failed": {}, "outcome": {}}, [b"\x89PNG-graded"])

    conv, wa, ft = math_world(monkeypatch, wa=MasterIt(), latex=r"1. x^2  6. 25")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    ms = conv.cs.math
    assert wa.begun == [a] and ms.stepped == [6] and ms.steps == []
    assert 6 not in ms.targets                       # not on the sheet: asked for later
    assert "6 goes step by step" in ft.last and "leave it off the sheet" in ft.last
    assert MathSession.from_dict(asdict(ms)).stepped == [6]

    # the sheet answers 1; "6. 25" on it is neither filled nor called unknown
    conv.handle(photo_msg())
    assert ms.answers == {1: ["x^2"]} and "Ignored" not in ft.last
    conv.handle(cb("mfill"))
    assert set(wa.filled) == {"RA_11_0_0_91"}
    conv.handle(cb("msubmit:40575442"))
    assert wa.submitted == ["RA_11_0_0_91"] and conv.cs.math is ms
    # now Step 1 of 6 is shown (just that step) and asked for
    assert ms.stage == "fixing" and ms.fixing == 6 and ms.targets[6] == [s1]
    assert 'name="photo"' in ft.photos[-1]            # a picture of the step went out
    assert "Step 1" in ft.last and "is open" in ft.last and "photo" in ft.last
    assert not any("step-by-step" in m.get("text", "") for m in ft.sent)   # no Begin buttons

    conv.handle(msg("25"))                           # typed, no number needed while fixing
    assert ms.answers[6] == ["25"] and ms.stage == "confirm"
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert wa.submitted[-1] == s1
    assert ms.stage == "fixing" and ms.targets[6] == [s2] and "Step 2" in ft.last
    conv.handle(msg("12000"))
    assert ms.answers[6] == ["25", "12000"]
    conv.handle(cb("mfill"))
    assert s2 in wa.filled and "<mn>12000</mn>" in wa.filled[s2]
    conv.handle(cb("msubmit:40575442"))
    assert wa.submitted[-1] == s2
    # marked wrong: the same box is asked for again, its value forgotten
    assert "marked wrong" in "".join(m.get("text", "") for m in ft.sent[-3:])
    assert ms.stage == "fixing" and ms.fixing == 6 and s2 not in ms.done[6]
    assert ms.answers[6] == ["25"]
    conv.handle(msg("6: 12000"))                     # the habit of numbering is fine
    assert ms.answers[6] == ["25", "12000"]
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert wa.submitted[-1] == s2 and wa.submitted.count(s1) == 1
    # (a) finished -> (b) begun by the bot, its Step 1 asked for
    assert wa.begun == [a, b] and ms.targets[6] == [b1] and ms.stage == "fixing"
    conv.handle(msg("22.6"))
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert wa.submitted[-1] == b1
    assert conv.cs.math is None and "All correct" in ft.last


def test_a_correction_mid_way_through_a_stepped_question_is_for_the_open_part(monkeypatch):
    from ainl.bot.state import MathSession
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    ms = conv.cs.math
    ms.stepped, ms.targets[2], ms.done[2] = [2], ["RA_22_1_0_92", "RA_22_1_1_92"], ["RA_22_1_0_92"]
    ms.answers[2] = ["first", "second"]
    conv.handle(msg("2: third"))
    assert ms.answers[2] == ["first", "third"]        # (a) kept, (b) replaced
    conv.handle(msg("2: x, y")); assert ms.answers[2] == ["x", "y"]   # a full line replaces all


def test_a_begin_that_fails_falls_back_to_a_button(monkeypatch):
    button = "step_button_id_1_0_0_0"
    class Stuck(MixedWA):
        def __init__(self):
            super().__init__()
            self.qs = [Question(6, "question66_5", [], [Part("step", button, label="Exercise (a)")])]
        def begin_step(self, dep, button_id):
            raise WebAssignUnavailable("no")
    conv, wa, ft = math_world(monkeypatch, wa=Stuck())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    offer = next(m for m in ft.sent if "step-by-step" in m.get("text", ""))
    assert _buttons(offer)[0]["callback_data"] == f"mstep:40575442:{button}"
    assert conv.cs.math.steps == [{"n": 6, "id": button, "label": "Exercise (a)"}]
def test_an_unexpected_error_is_reported_not_fatal(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    def boom(arg): raise KeyError("x")
    conv._math = boom
    conv.handle(msg("/math"))                    # must not raise
    assert "broke on my side" in ft.last and "KeyError" in ft.last


def _buttons(m):
    return [b for row in m["reply_markup"]["inline_keyboard"] for b in row]


def test_a_tap_ticks_its_own_button_instead_of_sending_a_message(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    n = len(ft.sent)
    tap = cb("c:40575442:0:1")
    tap["callback_query"]["message"]["message_id"] = 7
    conv.handle(tap)
    assert len(ft.sent) == n                         # nothing new in the chat
    texts = [b["text"] for b in _buttons(ft.edits[-1])]
    assert "✅ divergent" in texts and "convergent" in texts
    conv.handle(dict(tap, callback_query=dict(tap["callback_query"], data="c:40575442:0:0")))
    assert conv.cs.math.picked["RC_22_1_0_92"] == ["0"]        # changing your mind works


def test_buttons_from_an_older_round_do_not_land_on_this_one(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("c:11111111:0:1"))
    assert conv.cs.math.picked == {}
    assert "round that's over" in ft.last


def test_long_or_unlabelled_options_get_letters_in_page_order(monkeypatch):
    class Wordy(MixedWA):
        def __init__(self):
            super().__init__()
            self.qs = [Question(1, "question11_0", [], [Part("choice", "RC_11_0_0_91", [
                Option("0", "The series converges by the comparison test"),
                Option("1", "The series diverges by the integral test"),
                Option("2", "")])])]

    conv, wa, ft = math_world(monkeypatch, wa=Wordy())
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    m = next(x for x in ft.sent if x.get("reply_markup") and "tap your answer" in x["text"])
    assert [b["text"] for b in _buttons(m)] == ["A", "B", "C"]
    assert "<b>A.</b> The series converges by the comparison test" in m["text"]
    assert "Letters are mine" in m["text"]


def test_a_half_answered_question_is_stopped_before_filling(monkeypatch):
    """Question 2 has a written value AND a choice. With only the value, a
    fill-and-submit spends an attempt on an answer that cannot be right."""
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA(), latex=r"1. x^2 \quad 2. \frac{1}{2}")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    conv.handle(cb("mfill"))
    assert not wa.filled                              # stopped
    assert any("Half-answered: <b>2</b>" in m["text"] for m in ft.sent)
    assert "tap your answer" in ft.last               # and its buttons are right there
    conv.handle(cb("mfill!"))                         # the student may insist
    assert "RA_11_0_0_91" in wa.filled


def test_untapped_buttons_come_back_after_the_sheet_is_read(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA(), latex=r"1. x^2 \quad 2. \frac{1}{2}")
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("c:40575442:0:1"))                 # 2 tapped, 3 not
    n = len(ft.sent)
    conv.handle(photo_msg())
    again = [m["text"] for m in ft.sent[n:] if m.get("reply_markup") and "tap your answer" in m["text"]]
    assert len(again) == 1 and again[0].startswith("<b>3.</b>")
    assert "Not tapped yet: 3" in "".join(m["text"] for m in ft.sent[n:])


# The student's real sheet for 7.8 Homework, as Gemini read it (2026-09-21).
SHEET_78 = (r"2. divergent -\frac{1}{9}\ln|2-9x| 3. Convergent [-\frac{13}{2}e^{-x^2}]_0^\infty"
            r" = 0-(\frac{13}{2}) = \frac{13}{2} 4. divergent 5. convergent \int_2^\infty "
            r"\frac{1}{10}(\frac{1}{v-1} - \frac{1}{v+9})dv = \frac{1}{10}\ln(11) 6. Conver. "
            r"-40e^{-1}(-2) = \frac{80}{e} 0-\frac{80}{e} = -\frac{80}{e} 7. Convergent 8. divergent")


class Real78(MixedWA):
    """7.8 Homework as it is on the page: q2-6 a radio AND a maths box, with
    "If the quantity diverges, enter DIVERGES."; q7-8 a radio alone."""

    def __init__(self):
        super().__init__()
        cd = lambda: [Option("0", "convergent"), Option("1", "divergent")]
        stem = ("Determine whether the integral is convergent or divergent. If it is "
                "convergent, evaluate it. (If the quantity diverges, enter DIVERGES.)")
        self.qs = [Question(n, f"question{n}{n}_{n - 1}", [f"RA_{n}{n}_{n - 1}_1_9"],
                            [Part("choice", f"RC_{n}{n}_{n - 1}_0_9", cd()),
                             Part("math", f"RA_{n}{n}_{n - 1}_1_9")], stem) for n in range(2, 7)]
        self.qs += [Question(n, f"question{n}{n}_{n - 1}", [],
                             [Part("choice", f"RC_{n}{n}_{n - 1}_0_9", cd())], "Use the Comparison Theorem")
                    for n in (7, 8)]
        for i, q in enumerate(self.qs):          # page numbering follows list order
            q.n = i + 2


def test_choices_written_on_the_sheet_need_no_tap(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=Real78(), latex=SHEET_78)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    ms = conv.cs.math
    val = lambda n: ms.picked[f"RC_{n}{n}_{n - 1}_0_9"]
    assert [val(n) for n in range(2, 9)] == [["1"], ["0"], ["1"], ["0"], ["0"], ["0"], ["1"]]
    # the word is not part of the maths; divergent ones get what the page asks for
    assert ms.answers == {2: ["DIVERGES"], 3: [r"\frac{13}{2}"], 4: ["DIVERGES"],
                          5: [r"\frac{1}{10}\ln(11)"], 6: [r"-\frac{80}{e}"]}
    preview = "".join(m["text"] for m in ft.sent[-3:])
    assert "no such question" not in preview            # 7 and 8 are real, just tap-only
    assert "the page asks for that word" in preview
    conv.handle(cb("mfill"))                            # nothing half-done: goes straight in
    assert len(wa.filled) == 5 and len(wa.chosen) == 7


def test_a_tap_beats_the_reading_and_says_so(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=Real78(), latex=SHEET_78)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("c:40575442:1:1"))                   # question 3 -> divergent, by hand
    conv.handle(photo_msg())
    assert conv.cs.math.picked["RC_33_2_0_9"] == ["1"]
    assert any("I kept your tap" in m["text"] for m in ft.sent)


def test_a_clash_is_one_tap_to_resolve_not_a_cancel(monkeypatch):
    """Tapped the wrong one on purpose; the sheet says otherwise. Getting out
    must not mean cancelling and starting again from /todo."""
    conv, wa, ft = math_world(monkeypatch, wa=Real78(), latex=SHEET_78)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(cb("c:40575442:1:1"))                   # q3 -> divergent (wrong, by hand)
    conv.handle(photo_msg())
    ms = conv.cs.math
    assert ms.answers[3] == ["DIVERGES"]                # follows the tap, as it must
    offer = next(m for m in ft.sent if "sheet and tap disagree" in m["text"])
    data = offer["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert data == "c:40575442:1:0"                     # = an ordinary tap on "convergent"
    conv.handle(cb(data))
    assert ms.picked["RC_33_2_0_9"] == ["0"]
    assert ms.answers[3] == [r"\frac{13}{2}"]           # the value came back, no new photo
    assert "I read:" in ft.last and "Fill these in" in str(ft.sent[-1]["reply_markup"])
    assert conv.cs.math is ms and ms.stage == "confirm"


def test_a_tap_does_not_undo_a_hand_correction(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=Real78(), latex=SHEET_78)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    conv.handle(msg("5: 7"))
    conv.handle(cb("c:40575442:3:0"))                   # re-tap q5's own choice
    assert conv.cs.math.answers[5] == ["7"]


def test_text_inside_a_round_does_not_send_you_back_to_todo(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=Real78(), latex=SHEET_78)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    conv.handle(msg("question 3 is wrong"))
    assert "nothing is lost" in ft.last and "Reply a code" not in ft.last
    assert conv.cs.math.stage == "confirm"
    conv.handle(photo_msg())                            # a new photo just re-reads
    assert "I read:" in "".join(m["text"] for m in ft.sent[-4:])


def _filled_round(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=Real78(), latex=SHEET_78)
    conv.handle(msg("/math"))
    conv.handle(cb("m:40575442"))
    conv.handle(photo_msg())
    conv.handle(cb("mfill"))
    return conv, wa, ft


def test_later_sets_the_round_aside_and_resume_brings_it_back(monkeypatch):
    conv, wa, ft = _filled_round(monkeypatch)
    assert "Later" in str(ft.sent[-1]["reply_markup"])
    answers = dict(conv.cs.math.answers)
    conv.handle(cb("mlater"))
    assert conv.cs.math is None and len(conv.cs.parked) == 1
    assert "nothing was submitted" in ft.last and "/resume" in ft.last
    assert not wa.submitted

    conv.handle(msg("/resume"))
    data = ft.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert data == "mres:40575442"
    wa.filled.clear()
    conv.handle(cb(data))
    ms = conv.cs.math
    assert ms.answers == answers and ms.stage == "filled" and not conv.cs.parked
    assert len(wa.filled) == 5                          # the page was put back, then shown
    assert "Submit" in str(ft.sent[-1]["reply_markup"])
    conv.handle(cb(_submit_button(ft)))
    assert wa.submitted


def test_parked_rounds_survive_a_restart_of_the_bot_and_of_the_chat(monkeypatch, tmp_path):
    conv, wa, ft = _filled_round(monkeypatch)
    conv.handle(cb("mlater"))
    conv.handle(msg("/restart"))
    assert len(conv.cs.parked) == 1 and "set aside are untouched" in ft.last
    path = tmp_path / "state.json"
    conv.state.save(path)
    from ainl.bot.state import BotState
    again = BotState.load(path).chat(conv.chat_id)
    assert again.parked[0].dep == "40575442" and again.parked[0].answers[3] == [r"\frac{13}{2}"]


def test_resuming_one_round_parks_the_one_in_hand(monkeypatch):
    conv, wa, ft = _filled_round(monkeypatch)
    conv.handle(cb("mlater"))
    conv.handle(msg("/math"))
    conv.handle(cb("m:99999999"))                       # a different assignment, now open
    conv.cs.math.answers = {2: ["1"]}
    conv.handle(cb("mres:40575442"))
    assert conv.cs.math.dep == "40575442"
    assert [p.dep for p in conv.cs.parked] == ["99999999"]   # swapped, not dropped


def test_resume_with_nothing_set_aside(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/resume"))
    assert "Nothing set aside" in ft.last


# -- /todo is the one way in ---------------------------------------------------

class CanvasWithTools(FakeCanvas):
    """Course 2's assignments point at WebAssign, as the real MATH222 does."""

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/courses/2/assignments" and req.method == "GET":
            tool = {"url": "https://gateway.cengage.com/rest/launchBasicLTI/1/2"}
            return _j([{"id": i, "submission_types": ["external_tool"],
                        "external_tool_tag_attributes": tool} for i in ("200", "201", "202")])
        return super().__call__(req)


def tools_world(monkeypatch, wa=None):
    fc, ft = CanvasWithTools(), FakeTelegram()
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    conv = Conversation(canvas, tg, CHAT, BotState(), tz="America/Chicago",
                        webassign=wa or FakeWebAssign(),
                        transcribe=lambda data, hint="": Result(latex="1. x", agreed=True))
    return conv, fc, ft


def test_todo_marks_webassign_work_as_doable_here_not_as_a_link(monkeypatch):
    conv, _, ft = tools_world(monkeypatch)
    conv.handle(msg("/todo"))
    b1 = next(m for m in conv.cs.menu if m.code == "B1")
    assert b1.kind == "webassign"
    text = ft.last
    assert "🧮" in text                                   # shown as work, not as ↗
    math_block = next(ln for ln in text.split("\n") if "MATH222" in ln)
    assert "1 to do" in math_block and "↗" not in math_block


def test_a_webassign_code_goes_straight_into_the_questions(monkeypatch):
    conv, _, ft = tools_world(monkeypatch)
    monkeypatch.setattr(type(conv), "_webassign_dep", lambda self, item: "40575442")
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    assert conv.cs.math is not None and conv.cs.math.stage == "shoot"
    assert ft.albums                                      # the questions were sent


def test_todo_can_look_further_ahead_so_no_second_list_is_needed(monkeypatch):
    conv, _, ft = tools_world(monkeypatch)
    conv.handle(msg("/todo"))
    assert not any("11.9" in m.title for m in conv.cs.menu)     # 10 days out: hidden
    assert "todo:14" in json.dumps(ft.sent[-1].get("reply_markup"))
    conv.handle(msg("/todo 14"))
    assert any("11.9" in m.title for m in conv.cs.menu)         # now reachable
    assert "+14 days" in ft.last
    conv.handle(cb("todo:14"))                                  # the button does the same
    assert any("11.9" in m.title for m in conv.cs.menu)


def test_math_is_not_advertised_anywhere(monkeypatch):
    from ainl.bot.conversation import HELP
    assert "/math" not in HELP
    assert "Everything starts from a code" in HELP


def test_a_promised_webassign_item_that_will_not_open_says_so(monkeypatch):
    conv, _, ft = tools_world(monkeypatch)
    monkeypatch.setattr(type(conv), "_webassign_dep", lambda self, item: None)
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    assert "couldn't open" in ft.last                     # not a silent fallback to a link


def test_codes_survive_a_submission_and_a_cancel(world):
    """One /todo is good for the whole sitting: finishing or cancelling one
    item must not renumber or void the others."""
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    codes = [m.code for m in conv.cs.menu]
    conv.handle(msg(codes[0]))
    conv.handle(msg("/cancel"))
    assert [m.code for m in conv.cs.menu] == codes
    assert "codes still work" in ft.last


# -- Gradescope: an external item whose files we can carry ------------------------

class CanvasWithGradescope(FakeCanvas):
    """Course 2's item 200 is PHYSICS 247's Problem Set 3: an external_tool that
    points at Gradescope, with its question sheet attached on the CANVAS side."""

    TOOL = {"url": "https://lti-gradescope.int.turnitin.com/lti/launch"}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/v1/courses/2/assignments" and req.method == "GET":
            return _j([{"id": i, "submission_types": ["external_tool"],
                        "external_tool_tag_attributes": self.TOOL} for i in ("200", "201", "202")])
        if p == "/api/v1/courses/2/assignments/200":
            return _j({"id": "200", "name": "Problem Set 3", "due_at": DUE, "points_possible": 10,
                       "description": "<a href='/courses/2/files/9/download'>PS3.pdf</a>",
                       "submission_types": ["external_tool"], "external_tool_tag_attributes": self.TOOL})
        return super().__call__(req)


class FakeGradescope:
    def __init__(self, moved=True):
        self.got, self.moved = [], moved

    def upload_pdf(self, canvas_url, filename, pdf):
        self.got.append((canvas_url, filename, pdf))
        return {"title": "Assign pages | Gradescope", "landed": "www.gradescope.com/x",
                "moved": self.moved, "shot": b"PNG"}


def gs_world(monkeypatch, moved=True):
    fc, ft, gs = CanvasWithGradescope(), FakeTelegram(), FakeGradescope(moved)
    monkeypatch.setattr("ainl.canvas.api.httpx.get", lambda url, **kw: httpx.Response(200, content=b"PDF"))
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("TOK", transport=httpx.MockTransport(ft))
    return Conversation(canvas, tg, CHAT, BotState(), tz="UTC", gradescope=gs), fc, ft, gs


def test_an_external_item_still_gets_its_canvas_description_and_files(monkeypatch):
    conv, fc, ft, gs = gs_world(monkeypatch)
    conv.handle(msg("/todo"))
    assert "📤" in ft.last                                # marked as doable here, not ↗
    conv.handle(msg("B1"))
    assert ft.docs                                         # the PDF attached on Canvas was pushed
    assert "Gradescope" in ft.last and "after you confirm" in ft.last
    assert conv.cs.pending is not None and conv.cs.pending.vendor == "gradescope"


def test_a_pdf_goes_to_gradescope_only_after_confirm(monkeypatch):
    conv, fc, ft, gs = gs_world(monkeypatch)
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    conv.handle(msg(document={"file_id": "F9", "file_name": "ps3.pdf", "mime_type": "application/pdf"}))
    assert "upload to Gradescope" in ft.last and not gs.got        # previewed, nothing sent yet
    conv.handle(cb("submit"))
    (url, name, pdf), = gs.got
    assert url.endswith("/courses/2/assignments/200") and name == "ps3.pdf" and pdf == b"%PDF-fake"
    assert not fc.submitted                                         # and NOT handed in to Canvas
    assert "Uploaded to Gradescope" in ft.last and "which pages" in ft.last


def test_an_upload_that_may_not_have_happened_is_not_called_a_success(monkeypatch):
    conv, fc, ft, gs = gs_world(monkeypatch, moved=False)
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    conv.handle(msg(document={"file_id": "F9", "file_name": "ps3.pdf", "mime_type": "application/pdf"}))
    conv.handle(cb("submit"))
    assert "can't tell whether it went through" in ft.last


def test_photos_are_bound_into_one_pdf_in_the_order_sent():
    from io import BytesIO
    from PIL import Image
    from ainl.external.gradescope import as_one_pdf

    def jpg(colour):
        b = BytesIO()
        Image.new("RGB", (40, 60), colour).save(b, format="JPEG")
        return b.getvalue()

    name, pdf = as_one_pdf([("a.jpg", "image/jpeg", jpg("red")), ("b.jpg", "image/jpeg", jpg("blue"))])
    assert name == "pages-2.pdf" and pdf.startswith(b"%PDF")
    assert pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages") == 2      # two pages, one per photo
    assert as_one_pdf([("x.pdf", "application/pdf", b"%PDF-1")]) == ("x.pdf", b"%PDF-1")   # a PDF is not touched
    with pytest.raises(ValueError, match="not a mix"):
        as_one_pdf([("x.pdf", "application/pdf", b"%PDF-1"), ("a.jpg", "image/jpeg", jpg("red"))])


def test_an_album_updates_one_preview_instead_of_sending_one_per_file(world):
    """Four photos arrive as four messages. The student should see ONE preview,
    ending at "4 files", not four previews stacked up."""
    conv, fc, ft = world
    conv.handle(msg("/todo"))
    code = next(m.code for m in conv.cs.menu if "online_upload" in m.types)
    conv.handle(msg(code))
    before = len(ft.sent)
    for i in range(4):
        conv.handle(msg(photo=[{"file_id": f"P{i}", "file_size": 10}]))
    previews = [m for m in ft.sent[before:] if "Submit all" in str(m.get("reply_markup"))]
    assert len(previews) == 1 and "4 files" in previews[0]["text"]
    conv.handle(cb("cancel"))                          # the next queue gets its own preview
    conv.handle(msg(code))
    conv.handle(msg(photo=[{"file_id": "Q", "file_size": 10}]))
    assert "1 file " in ft.sent[-1]["text"]


def test_a_remark_while_fixing_is_not_written_into_the_box(monkeypatch):
    """"first one is blank" (2026-09-24) was taken as question 7's answer."""
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("mmodify")); conv.handle(cb("mfix:2"))
    assert conv.cs.math.stage == "fixing"
    conv.handle(msg("first one is blank"))
    assert conv.cs.math.answers[2] == [r"\frac{1}{2}"]        # unchanged
    assert conv.cs.math.stage == "fixing" and "reads like a sentence" in ft.last
    conv.handle(msg(r"\sin(x)"))                                # a real answer still works
    assert conv.cs.math.answers[2] == [r"\sin(x)"] and conv.cs.math.stage == "confirm"


def test_a_box_the_page_would_not_draw_is_said_before_submit(monkeypatch):
    class Undrawn(FakeWebAssign):
        def fill(self, dep, answers):
            info, shots = super().fill(dep, answers)
            info["undrawn"] = ["RA_22_1_0_92"]
            return info, shots
    conv, wa, ft = math_world(monkeypatch, wa=Undrawn())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill"))
    assert "2: the page didn't draw" in ft.last and "Submit?" in ft.last


class GradedWA(FakeWebAssign):
    """submit() grades: question 2 wrong the first time, right after a redo."""
    def __init__(self):
        super().__init__()
        self.rounds = 0

    def submit(self, dep, boxes, expect=None):
        self.submitted.extend(boxes)
        self.rounds += 1
        doms = sorted({b.split("_")[1] + "_" + b.split("_")[2] for b in boxes})
        outcome = {}
        for d in doms:
            dom = f"question{d}"
            wrong = dom == "question22_1" and self.rounds == 1
            outcome[dom] = {"points": ("0 / 1" if wrong else "1 / 1") + " Points",
                            "marks": ["wrong" if wrong else "right"]}
        return ({"submitted": [f"question{d}" for d in doms], "failed": {}, "outcome": outcome},
                [b"\x89PNG-graded"])


def test_after_grading_the_wrong_ones_are_named_and_can_be_redone_by_typing(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=GradedWA())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    ms = conv.cs.math
    assert ms is not None and ms.stage == "graded"          # the round stays open
    assert ms.graded == {1: "right", 2: "wrong"}
    assert "✗ <b>2.</b>" in ft.last and "✓ <b>1.</b>" in ft.last
    kb = json.dumps(ft.sent[-1].get("reply_markup"), ensure_ascii=False)
    assert "Redo 2" in kb and "mredo:40575442" in kb and "mfinish:40575442" in kb
    assert "20 submissions" not in ft.last and "/cancel" not in ft.last     # the buttons say it
    assert ms.redo == [2]
    n_filled, n_sub = dict(wa.filled), len(wa.submitted)
    conv.handle(msg(r"2: \sin(x)"))
    assert ms.stage == "confirm" and ms.answers[2] == [r"\sin(x)"]
    assert "Graded so far" in ft.last and "✗ 2" in ft.last
    conv.handle(cb("mfill"))
    assert "<mi>sin</mi>" in wa.filled["RA_22_1_0_92"]
    conv.handle(cb("msubmit:40575442"))
    assert wa.submitted[n_sub:] == ["RA_22_1_0_92"]           # only the redone one
    assert conv.cs.math is None and "All correct" in ft.last


def test_a_photo_of_corrections_after_grading_refills_only_what_changed(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=GradedWA())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    ms = conv.cs.math
    conv.transcribe = lambda data, hint="": Result(latex=r"1. x^2  2. \cos(x)", agreed=True)
    conv.handle(photo_msg())                                    # whole sheet, 2 changed
    assert ms.answers == {1: ["x^2"], 2: [r"\cos(x)"]} and 1 in ms.done and 2 not in ms.done
    n_sub = len(wa.submitted)
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert wa.submitted[n_sub:] == ["RA_22_1_0_92"]
    assert conv.cs.math is None


def test_a_correction_photo_that_changes_nothing_says_so(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=GradedWA())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    conv.handle(photo_msg())                                    # the same sheet again
    assert "differs" in ft.last and conv.cs.math.stage == "graded"


def test_step_questions_come_last_in_the_album(monkeypatch):
    class WithStep(MixedWA):
        def __init__(self):
            super().__init__()
            self.qs = [Question(1, "question11_0", [], [Part("step", "step_button_id_1", label="Exercise (a)")]),
                       _mathq(2, "question22_1", ["RA_22_1_0_92"])]
            self.opened = {"step_button_id_1": Question(1, "question11_0", ["RA_11_0_0_1"], [Part("math", "RA_11_0_0_1", label="Step 1")])}
        def begin_step(self, dep, button_id):
            q, shot = super().begin_step(dep, button_id); self.qs = [q, self.qs[1]]; return q, shot
    conv, wa, ft = math_world(monkeypatch, wa=WithStep())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    assert ft.album_captions[-1] == ["2.", "1."]


class FakeSolver:
    """Scripted replies in the model's per-box format."""
    def __init__(self, sheet, steps=(), revision="", repairs=(), quiz=""):
        self.sheet, self.steps, self.revision, self.quiz = sheet, list(steps), revision, quiz
        self.repairs = list(repairs)
        self.calls: list[tuple] = []

    def quiz_reply(self, title, bot_messages):
        self.calls.append(("quiz", title, bot_messages))
        return self.quiz

    def solve(self, title, items, prompt, wrong=None, fix=None, previous="", report_seen=False):
        kind = "repair" if fix else "revise" if wrong else "sweep" if report_seen else "solve"
        self.calls.append((kind, title, [(i.n, i.boxes, i.choices, bool(i.image)) for i in items],
                           prompt, fix or wrong))
        if fix:
            return self.repairs.pop(0)
        return self.revision if wrong else self.sheet

    def solve_step(self, title, stem, step_text, label, earlier, image=None, kind="maths"):
        self.calls.append(("step", label, step_text, list(earlier), bool(image), kind))
        return self.steps.pop(0)


def _auto_world(monkeypatch, wa, solver):
    fc, ft = FakeCanvas(), FakeTelegram()
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    conv = Conversation(canvas, tg, CHAT, BotState(), tz="America/Chicago", webassign=wa,
                        transcribe=lambda data, hint="": (_ for _ in ()).throw(AssertionError("no photo")),
                        autosolve=solver)
    return conv, ft


def _run_auto(conv, text="/auto"):
    conv.handle(msg(text))
    if conv._auto_thread is not None:
        conv._auto_thread.join(timeout=10)
        assert not conv._auto_thread.is_alive()


def test_auto_plays_the_student_through_answers_fill_and_submit(monkeypatch):
    wa = FakeWebAssign()
    solver = FakeSolver("1.1 x^2\n2.1 \\frac{1}{2}\n3.1 1\n3.2 2")
    conv, ft = _auto_world(monkeypatch, wa, solver)
    _run_auto(conv)
    assert "Open an assignment first" in ft.last
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    assert not solver.calls and not wa.filled                # nothing until /auto
    _run_auto(conv)
    kind, title, items, prompt, wrong = solver.calls[0]
    assert kind == "solve" and [i[0] for i in items] == [1, 2, 3]
    assert items[2][1] == ["maths", "maths"] and all(i[3] for i in items)   # boxes described, pictures sent
    assert "one sheet" in prompt
    assert set(wa.filled) == {"RA_11_0_0_91", "RA_22_1_0_92", "RA_33_2_0_93", "RA_33_2_1_94"}
    assert wa.submitted and conv.cs.math is None               # filled, submitted, done


def test_a_box_that_takes_a_list_keeps_the_whole_list(monkeypatch):
    """9.1 Homework q1 (2026-09-25): "k = (comma-separated list)" is ONE box.
    Split on commas, -7/4 and 7/4 took two boxes and every later answer moved."""
    class ListBox(FakeWebAssign):
        def __init__(self):
            super().__init__()
            names = ["RA_41_0_0_1", "RA_41_0_1_1"]
            self.qs = [Question(1, "question41_0", names, [Part("math", n) for n in names])]
    wa = ListBox()
    conv, ft = _auto_world(monkeypatch, wa, FakeSolver("1.1 -\\frac{7}{4}, \\frac{7}{4}\n1.2 0"))
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); _run_auto(conv)
    assert "<mfrac>" in wa.filled["RA_41_0_0_1"] and "," in wa.filled["RA_41_0_0_1"]
    assert "<mn>0</mn>" in wa.filled["RA_41_0_1_1"]


def test_auto_answers_multiple_choice_too(monkeypatch):
    wa = MixedWA()
    solver = FakeSolver("1.1 x^2\n2.c1 B\n2.1 5\n3.c1 A")
    conv, ft = _auto_world(monkeypatch, wa, solver)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); _run_auto(conv)
    items = solver.calls[0][2]
    assert [i[2] for i in items if i[0] == 2] == [[["convergent", "divergent"]]]
    assert wa.chosen["RC_22_1_0_92"] == ["1"] and wa.chosen["RC_33_2_0_93"] == ["0"]
    assert "RA_22_1_1_92" in wa.filled


def test_auto_redoes_what_was_marked_wrong_then_stops(monkeypatch):
    wa = GradedWA()
    solver = FakeSolver("1.1 x^2\n2.1 \\frac{1}{2}\n3.1 1\n3.2 2", revision="2.1 \\sin(x)")
    conv, ft = _auto_world(monkeypatch, wa, solver)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); _run_auto(conv)
    assert [c[0] for c in solver.calls] == ["solve", "revise"]
    assert solver.calls[1][4] == {2: [r"\frac{1}{2}"]}          # the wrong answer is shown to it
    assert "<mi>sin</mi>" in wa.filled["RA_22_1_0_92"]
    assert wa.submitted.count("RA_22_1_0_92") == 2 and wa.submitted.count("RA_11_0_0_91") == 1
    assert conv.cs.math is None


def test_auto_off_is_answered_at_once_and_the_late_answer_is_dropped(monkeypatch):
    import threading
    gate, entered = threading.Event(), threading.Event()
    class Slow(FakeSolver):
        def solve(self, *a, **k):
            entered.set(); gate.wait(5)
            return super().solve(*a, **k)
    wa = FakeWebAssign()
    conv, ft = _auto_world(monkeypatch, wa, Slow("1.1 x^2"))
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    conv.handle(msg("/auto"))
    assert entered.wait(5)                                        # the model call is in flight
    conv.handle(msg("/auto off"))                                 # not blocked by it
    assert "auto off" in ft.last and "stopping after the step in progress" in ft.last
    gate.set(); conv._auto_thread.join(5)
    assert not wa.filled and not wa.submitted                     # its answer was not used
    assert "stopped" in ft.last


def test_auto_answers_each_step_of_a_master_it_question(monkeypatch):
    dom, a, b = "question4598017_5", "step_button_id_4598017_0_0_0", "step_button_id_4598017_0_1_0"
    s1, s2, b1 = "RA_4598017_5_0_1", "RA_4598017_5_1_1", "RA_4598017_5_9_1"
    P = lambda box, label, **k: Part("math", box, label=label, text=f"{label} text", **k)   # noqa: E731
    fresh   = Question(6, dom, [], [Part("step", a, label="Exercise (a)")])
    step1   = Question(6, dom, [s1], [P(s1, "Step 1"), Part("step", b, label="Exercise (b)")])
    step2   = Question(6, dom, [s1, s2], [P(s1, "Step 1", graded=True), P(s2, "Step 2"),
                                          Part("step", b, label="Exercise (b)")])
    step2x  = Question(6, dom, [s1, s2], [P(s1, "Step 1", graded=True), P(s2, "Step 2", wrong=True),
                                          Part("step", b, label="Exercise (b)")])
    a_done  = Question(6, dom, [s1, s2], [P(s1, "Step 1", graded=True), P(s2, "Step 2", graded=True),
                                          Part("step", b, label="Exercise (b)")])
    b_step1 = Question(6, dom, [s1, s2, b1], [P(s1, "Step 1", graded=True), P(s2, "Step 2", graded=True), P(b1, "Step 1")])
    finished = Question(6, dom, [s1, s2, b1], [P(s1, "Step 1", graded=True), P(s2, "Step 2", graded=True), P(b1, "Step 1", graded=True)])

    class MasterIt(MixedWA):
        def __init__(self):
            super().__init__()
            self.q1 = _mathq(1, "question11_0", ["RA_11_0_0_91"])
            self.qs = [self.q1, fresh]
            self.opened = {a: step1, b: b_step1}
            self.after_submit = iter([step1, step2, step2x, a_done, finished])
        def begin_step(self, dep, button_id):
            q, shot = super().begin_step(dep, button_id); self.qs = [self.q1, q]; return q, shot
        def submit(self, dep, boxes, expect=None):
            self.submitted.extend(boxes); self.qs = [self.q1, next(self.after_submit)]
            return ({"submitted": [dom], "failed": {}, "outcome": {}}, [b"\x89PNG-graded"])

    wa = MasterIt()
    solver = FakeSolver("1.1 x^2", steps=["25", "12000", "12000", "22.6"])
    conv, ft = _auto_world(monkeypatch, wa, solver)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); _run_auto(conv)
    assert conv.cs.math is None                                   # ran to the end
    assert [c[0] for c in solver.calls] == ["solve", "step", "step", "step", "step"]
    assert [i[0] for i in solver.calls[0][2]] == [1]             # the step question is not on the sheet
    assert [c[1] for c in solver.calls[1:]] == ["Step 1", "Step 2", "Step 2", "Step 1"]
    assert solver.calls[2][3] == ["25"]                          # Step 2 sees Step 1's answer
    assert solver.calls[3][2] == "Step 2 text" and solver.calls[3][4]   # its text and its picture
    assert wa.submitted == ["RA_11_0_0_91", s1, s2, s2, b1]      # graded ✓ boxes never resubmitted
    assert wa.begun == [a, b]


def test_a_step_already_answered_on_the_page_is_not_asked_again(monkeypatch):
    """Opening a Master It question whose Steps 5 and 6 are ✓: their boxes are
    still on the page. They are done, and the label/picture is the new step's."""
    dom = "question4598017_5"
    s5, s6, s7 = "RA_4598017_5_4_1", "RA_4598017_5_5_1", "RA_4598017_5_6_1"
    P = lambda box, label, **k: Part("math", box, label=label, text=f"{label} text", **k)   # noqa: E731
    class Mid(MixedWA):
        def __init__(self):
            super().__init__()
            self.qs = [_mathq(1, "question11_0", ["RA_11_0_0_91"]),
                       Question(6, dom, [s5, s6, s7], [P(s5, "Step 5", graded=True), P(s6, "Step 6", graded=True), P(s7, "Step 7")])]
        def submit(self, dep, boxes, expect=None):
            self.submitted.extend(boxes)
            return ({"submitted": [dom], "failed": {}, "outcome": {}}, [b"\x89PNG-graded"])
        def step_shot(self, dep, box):
            self.shot_of = box; return b"\x89PNG-step"
    conv, wa, ft = math_world(monkeypatch, wa=Mid())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    ms = conv.cs.math
    assert ms.done[6] == [s5, s6]
    conv.handle(photo_msg()); conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert ms.stage == "fixing" and ms.fixing == 6
    assert "Step 7" in ft.last and wa.shot_of == s7


def test_the_public_build_ships_no_solver(monkeypatch):
    monkeypatch.setenv("AINL_AUTOSOLVE", "deepseek"); monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    from ainl.autosolve import from_env
    assert from_env() is None                                  # whatever the env says



def test_finished_webassign_work_is_marked_done_on_todo(monkeypatch):
    """Canvas only learns WebAssign work is done when the grade is passed back,
    days later; B2 stayed on /todo after it was submitted (2026-09-25)."""
    conv, _, ft = tools_world(monkeypatch)
    conv.handle(msg("/todo"))
    b1 = next(m for m in conv.cs.menu if m.code == "B1")
    head = lambda n, got: Question(n, f"question{n}_0", [f"RA_{n}_0_0_1"], [Part("math", f"RA_{n}_0_0_1")],   # noqa: E731
                                   stem=f"{n}. [{got} / 1 Points] DETAILS")
    conv._record_progress("999", b1.title, [head(1, "1"), head(2, "–")], b1.id)
    conv.handle(msg("/todo"))
    assert any(m.id == b1.id for m in conv.cs.menu)              # half done: still listed
    conv._record_progress("999", b1.title, [head(1, "1"), head(2, "0")], b1.id)
    assert conv.cs.wa_progress["999"]["complete"] and conv.cs.wa_progress["999"]["points"] == 1
    conv.handle(msg("/todo"))
    b1 = next(m for m in conv.cs.menu if m.id == b1.id)           # still listed…
    assert b1.done == "1/2 (Q1 1/1 · Q2 0/1)"                     # …with each question's score
    assert f"✅ <b>{b1.code}</b>" in ft.last and "<b>Done</b>" in ft.last   # done is done…
    assert "1/2" not in ft.last and "Q1 1/1" not in ft.last                 # …no score on the list
    math_block = next(ln for ln in ft.last.split("\n") if "MATH222" in ln)
    assert "1 done" in math_block


def test_finished_webassign_work_leaves_todo_a_day_after_it_was_done(monkeypatch):
    """Kept a day (a new attempt is one tap away), then off the list with a
    count — Canvas still says "not submitted" until the grade comes back."""
    conv, _, ft = tools_world(monkeypatch)
    conv.handle(msg("/todo"))
    b1 = next(m for m in conv.cs.menu if m.code == "B1")
    head = lambda n: Question(n, f"question{n}_0", [f"RA_{n}_0_0_1"], [Part("math", f"RA_{n}_0_0_1")],   # noqa: E731
                              stem=f"{n}. [1 / 1 Points] DETAILS")
    conv._record_progress("999", b1.title, [head(1)], b1.id)
    first = conv.cs.wa_progress["999"]["done_at"]
    conv._record_progress("999", b1.title, [head(1)], b1.id)      # opened again later…
    assert conv.cs.wa_progress["999"]["done_at"] == first          # …the day does not restart
    old = (datetime.now(conv.tz) - timedelta(days=1, minutes=5)).isoformat(timespec="minutes")
    conv.cs.wa_progress["999"]["done_at"] = old
    conv.handle(msg("/todo"))
    assert not any(m.id == b1.id for m in conv.cs.menu)
    assert "1 finished more than a day ago" in ft.last and "/todo all" in ft.last
    conv.handle(msg("/todo all"))                                  # still reachable
    assert any(m.id == b1.id and m.done for m in conv.cs.menu)


def test_a_step_question_with_parts_left_is_not_complete(monkeypatch):
    conv, _, ft = tools_world(monkeypatch)
    q = Question(6, "question6_5", ["RA_6_5_0_1"],
                 [Part("math", "RA_6_5_0_1", label="Step 5", graded=True), Part("step", "step_button_x", label="Exercise (b)")],
                 stem="6. [0.72 / 1.42 Points] DETAILS")
    conv._record_progress("1", "9.3 Homework", [q])
    assert conv.cs.wa_progress["1"]["answered"] == 0 and not conv.cs.wa_progress["1"]["complete"]


def test_progress_survives_a_restart(tmp_path):
    from ainl.bot.state import BotState
    st = BotState(); st.chat(CHAT).wa_progress["5"] = {"title": "t", "complete": True}
    st.save(tmp_path / "s.json")
    assert BotState.load(tmp_path / "s.json").chat(CHAT).wa_progress["5"]["complete"] is True


def test_a_question_graded_on_a_value_other_than_the_filled_one_is_not_called_wrong(monkeypatch):
    """9.1 Homework q1, 2026-09-25: WebAssign twice graded the older saved
    answers instead of the ones just filled and shown. That is the bot's fault,
    not the answer's — it must not be reported as wrong or redone by /auto."""
    class Stale(GradedWA):
        def submit(self, dep, boxes, expect=None):
            self.expect = dict(expect or {})
            info, shots = super().submit(dep, boxes, expect)
            info["differs"] = ["RA_22_1_0_92"]
            return info, shots
    wa = Stale()
    conv, wa2, ft = math_world(monkeypatch, wa=wa)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert "<mfrac>" in wa.expect["RA_22_1_0_92"]                 # what was filled is passed on
    assert "graded a DIFFERENT value" in ft.last
    assert conv.cs.math is None or conv.cs.math.graded.get(2) == "?"


def test_auto_asks_for_no_taps(monkeypatch):
    """Under /auto the bot presses Fill and Submit itself; the buttons for a
    human (Fill / Submit / Modify / Later / Cancel) would only be clutter."""
    wa = FakeWebAssign()
    conv, ft = _auto_world(monkeypatch, wa, FakeSolver("1.1 x^2\n2.1 \\frac{1}{2}\n3.1 1\n3.2 2"))
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    n = len(ft.sent)
    _run_auto(conv)
    after = ft.sent[n:]
    buttons = [b["callback_data"] for m in after if m.get("reply_markup")
               for row in m["reply_markup"]["inline_keyboard"] for b in row]
    assert not buttons
    assert not any("Submit?" in m.get("text", "") or m.get("text") == "Filling…" for m in after)
    assert wa.submitted


def test_auto_asks_again_for_a_missing_box_before_filling(monkeypatch):
    """7.8 Homework q1 (2026-09-25) went up with a box empty. Now every box is
    checked before anything is filled, and the bad lines are asked for again."""
    wa = FakeWebAssign()
    solver = FakeSolver("1.1 x^2\n2.1 \\frac{1}{2}\n3.1 1", repairs=["3.2 2"])
    conv, ft = _auto_world(monkeypatch, wa, solver)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); _run_auto(conv)
    assert [c[0] for c in solver.calls] == ["solve", "repair"]
    assert solver.calls[1][4] == {"3.2": "empty"} and [i[0] for i in solver.calls[1][2]] == [3]
    assert "<mn>2</mn>" in wa.filled["RA_33_2_1_94"] and wa.submitted


def test_a_prose_answer_is_repaired_and_a_second_failure_does_not_loop(monkeypatch):
    wa = FakeWebAssign()
    solver = FakeSolver("1.1 the answer is x\n2.1 \\frac{1}{2}\n3.1 1\n3.2 2",
                        repairs=["1.1 still words here"])
    conv, ft = _auto_world(monkeypatch, wa, solver)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); _run_auto(conv)
    assert [c[0] for c in solver.calls] == ["solve", "repair"]          # one repair, not more
    assert "contains words" in solver.calls[1][4]["1.1"]
    assert any("still not usable after the repair" in m.get("text", "") for m in ft.sent)



class QuizCanvas:
    """Canvas as ESL116 showed it (2026-09-25): a matching quiz and a
    fill-in-the-blanks quiz, unlimited attempts, questions only via an attempt."""
    def __init__(self, untaken=False, time_limit=None, attempts_allowed=-1):
        self.saved, self.completed, self.started = [], 0, 0
        self.untaken, self.time_limit, self.allowed = untaken, time_limit, attempts_allowed

    def __call__(self, req):
        p, m = req.url.path, req.method
        if p == "/api/v1/users/self/profile":
            return _j({"id": "7", "name": "T", "login_id": "t", "time_zone": "America/Chicago"})
        if p == "/api/v1/planner/items":
            return _j([{"course_id": "1", "context_name": "ESL116: Reading", "plannable_type": "quiz",
                        "plannable_id": "749803", "plannable_date": DUE,
                        "plannable": {"id": "749803", "title": "Identity Module Week 4 Quiz: Fill in the blanks", "due_at": DUE,
                                      "points_possible": 5.5},
                        "submissions": {"submitted": False}, "html_url": "/courses/1/quizzes/749803"}])
        if p == "/api/v1/courses/1/quizzes/749803" and m == "GET":
            return _j({"id": 749803, "title": "Week 4 Quiz", "quiz_type": "assignment", "time_limit": self.time_limit,
                       "allowed_attempts": self.allowed, "question_types": ["short_answer_question", "matching_question"],
                       "points_possible": 5.5, "require_lockdown_browser": False, "access_code": None, "locked_for_user": False})
        if p == "/api/v1/courses/1/quizzes/749803/submissions" and m == "GET":
            return _j({"quiz_submissions": [{"id": "99", "attempt": 1, "workflow_state": "untaken",
                                             "validation_token": "tok"}] if self.untaken else []})
        if p == "/api/v1/courses/1/quizzes/749803/submissions" and m == "POST":
            self.started += 1
            return _j({"quiz_submissions": [{"id": "99", "attempt": 1, "validation_token": "tok"}]})
        if p == "/api/v1/quiz_submissions/99/questions" and m == "GET":
            return _j({"quiz_submission_questions": [
                {"id": 11, "question_type": "short_answer_question", "question_text": "<p>An ___ is a partnership.</p>", "answers": []},
                {"id": 12, "question_type": "short_answer_question", "question_text": "<p>A ___ is a hard choice.</p>", "answers": []},
                {"id": 13, "question_type": "matching_question", "question_text": "",
                 "answers": [{"id": 5200, "text": "alliance"}, {"id": 5335, "text": "dilemma"}],
                 "matches": [{"match_id": "8011", "text": "difficult choice situation"},
                             {"match_id": "9468", "text": "cooperative partnership"}]}]})
        if p == "/api/v1/quiz_submissions/99/questions" and m == "POST":
            self.saved.append(json.loads(req.content)); return _j({"quiz_submission_questions": []})
        if p == "/api/v1/courses/1/quizzes/749803/submissions/99/complete" and m == "POST":
            self.completed += 1; body = json.loads(req.content)
            assert body == {"attempt": 1, "validation_token": "tok"}
            return _j({"quiz_submissions": [{"id": "99", "score": 5.5, "kept_score": 5.5}]})
        return httpx.Response(404, text=f"no route {m} {p}")


def _quiz_world(fc):
    ft = FakeTelegram()
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    return Conversation(canvas, tg, CHAT, BotState(), tz="America/Chicago", webassign=FakeWebAssign()), ft


def test_a_canvas_quiz_is_answered_by_the_student_and_submitted_on_their_tap():
    fc = QuizCanvas()
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo"))
    item = conv.cs.menu[0]
    assert item.kind == "quiz" and "📝" in ft.last and "1 to do" in ft.last     # doable here, not a link
    conv.handle(msg(item.code))
    assert fc.started == 1 and conv.cs.quiz is not None
    texts = " ".join(m["text"] for m in ft.sent[-3:])
    assert "An ___ is a partnership" in texts
    assert "<b>1.</b> alliance" in texts and "<b>A.</b> difficult choice situation" in texts
    conv.handle(msg("1. alliance\n2. dilemma"))
    assert "Still to answer: 3" in ft.last and not fc.saved and not fc.completed
    conv.handle(msg("1B 2A"))
    assert "your answers" in ft.last and "alliance → cooperative partnership" in ft.last
    assert not fc.completed                                        # nothing submitted without the tap
    conv.handle(cb("qS:99"))
    saved = fc.saved[0]
    assert saved["validation_token"] == "tok" and saved["attempt"] == 1
    got = {q["id"]: q["answer"] for q in saved["quiz_questions"]}
    assert got["11"] == "alliance" and got["12"] == "dilemma"
    assert sorted((x["answer_id"], x["match_id"]) for x in got["13"]) == [("5200", "9468"), ("5335", "8011")]
    assert fc.completed == 1 and "5.5 / 5.5" in ft.last and conv.cs.quiz is None


def test_an_attempt_already_started_in_the_browser_is_picked_up_not_duplicated():
    fc = QuizCanvas(untaken=True)
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    assert fc.started == 0 and "picking up the attempt you already started" in " ".join(m["text"] for m in ft.sent[-4:])


def test_a_timed_or_limited_quiz_asks_before_starting():
    fc = QuizCanvas(time_limit=20, attempts_allowed=1)
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    assert fc.started == 0 and "20-minute time limit" in ft.last and "1 attempt(s)" in ft.last
    conv.handle(cb("qs:1:749803:749803"))
    assert fc.started == 1 and conv.cs.quiz is not None


def test_cancel_leaves_the_quiz_and_submits_nothing():
    fc = QuizCanvas()
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    conv.handle(msg("1. alliance"))
    conv.handle(msg("/cancel"))
    assert conv.cs.quiz is None and not fc.completed and not fc.saved


def test_quiz_state_survives_a_restart(tmp_path):
    fc = QuizCanvas()
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code)); conv.handle(msg("1. alliance"))
    conv.state.save(tmp_path / "s.json")
    back = BotState.load(tmp_path / "s.json").chat(CHAT).quiz
    assert back.sub_id == "99" and back.answers == {"11": "alliance"} and back.token == "tok"


def test_a_canvas_quiz_works_over_the_cookie_channel_too():
    """No token, only the browser's Canvas session: the answers and the
    completion are POSTs, which Canvas refuses without the CSRF header."""
    from ainl.canvas.auth import CookieAuth

    class Checked(QuizCanvas):
        def __call__(self, req):
            if req.method == "POST":
                assert req.headers.get("x-csrf-token") == "abc=="            # URL-decoded cookie value
                assert "canvas_session=s" in req.headers.get("cookie", "")
                assert "authorization" not in req.headers
            return super().__call__(req)

    fc = Checked()
    ft = FakeTelegram()
    canvas = ApiSource(BASE, CookieAuth(canvas_session="s", csrf_token="abc%3D%3D"),
                       transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    conv = Conversation(canvas, tg, CHAT, BotState(), tz="America/Chicago", webassign=FakeWebAssign())
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    conv.handle(msg("1. alliance\n2. dilemma")); conv.handle(msg("1B 2A"))
    conv.handle(cb("qS:99"))
    assert fc.started == 1 and fc.saved and fc.completed == 1 and "5.5 / 5.5" in ft.last



def test_a_quiz_choice_can_be_typed_as_a_letter_or_its_text():
    class WithChoice(QuizCanvas):
        def __call__(self, req):
            if req.url.path == "/api/v1/quiz_submissions/99/questions" and req.method == "GET":
                return _j({"quiz_submission_questions": [
                    {"id": 21, "question_type": "true_false_question", "question_text": "Sky is blue.",
                     "answers": [{"id": 1, "text": "True"}, {"id": 2, "text": "False"}]},
                    {"id": 22, "question_type": "multiple_answers_question", "question_text": "Pick primes.",
                     "answers": [{"id": 3, "text": "2"}, {"id": 4, "text": "4"}, {"id": 5, "text": "5"}]}]})
            return super().__call__(req)
    conv, ft = _quiz_world(WithChoice())
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    conv.handle(msg("1. true\n2. A, C"))
    assert conv.cs.quiz.answers == {"21": "1", "22": ["3", "5"]} and "your answers" in ft.last


def _quiz_auto_world(fc, solver):
    ft = FakeTelegram()
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    return Conversation(canvas, tg, CHAT, BotState(), tz="America/Chicago", webassign=FakeWebAssign(),
                        autosolve=solver), ft


def test_auto_on_a_quiz_is_a_dry_run_that_stops_at_the_preview():
    fc = QuizCanvas()
    solver = FakeSolver("", quiz="1. alliance\n2. dilemma\n1B 2A")
    conv, ft = _quiz_auto_world(fc, solver)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    _run_auto(conv)
    assert solver.calls[0][0] == "quiz" and any("1. alliance" in m for m in solver.calls[0][2])
    assert any("[buttons" in m or "Match each" in m for m in solver.calls[0][2])
    # blanks and pairs in ONE message, as a student (or the model) writes it
    assert conv.cs.quiz.answers.get("11") == "alliance" and conv.cs.quiz.answers.get("12") == "dilemma"
    assert sorted((x["answer_id"], x["match_id"]) for x in conv.cs.quiz.answers["13"]) == [("5200", "9468"), ("5335", "8011")]
    assert "every question was understood" in ft.last
    assert not fc.completed and not fc.saved                     # never submitted
    assert "Nothing was submitted" in ft.last


def test_sweep_walks_the_list_and_reports_a_box_the_bot_did_not_see(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    wa = FakeWebAssign()
    solver = FakeSolver("1.1 x^2\n1.seen 1 0\n2.1 \\frac{1}{2}\n2.seen 2 0\n3.1 1\n3.2 2\n3.seen 2 0")
    conv, ft = _auto_world(monkeypatch, wa, solver)
    monkeypatch.setattr(type(conv), "_webassign_dep", lambda self, item: "40575442")
    from ainl.bot.state import MenuItem
    conv.cs.menu = [MenuItem(idx=1, course_id="2", id="200", kind="webassign", title="7.5 Homework",
                             course="MATH222", types=["external_tool"], url="u", due="Thu", code="B1")]
    conv.handle(msg("/sweep"))
    conv._auto_thread.join(10)
    assert [c[0] for c in solver.calls] == ["sweep"]
    assert wa.filled and not wa.submitted                        # filled up to Submit, never past it
    assert any("DeepSeek's answers" in m.get("text", "") for m in ft.sent)
    assert any("stopped before Submit" in m.get("text", "") for m in ft.sent)
    report = next((tmp_path / "sweeps").glob("*.md")).read_text()
    assert "q2: mismatch — bot found 1 box(es)" in report and "model sees 2 box(es)" in report
    assert "finding" in ft.last and "mismatch" in ft.last



def test_a_quiz_shows_its_instructions_first_and_the_dry_run_gets_them():
    """ESL116 Fill in the blanks keeps its WORD BANK in the quiz instructions:
    the student saw only the sentences, and the model guessed words that were
    not in the bank (2026-09-25)."""
    class WithBank(QuizCanvas):
        def __call__(self, req):
            r = super().__call__(req)
            if req.url.path == "/api/v1/courses/1/quizzes/749803" and req.method == "GET":
                body = json.loads(r.content); body["description"] = "<p>Use the words:</p><table><tr><td>Alliance (n)</td><td>Dilemma (n)</td></tr></table>"
                return _j(body)
            return r
    solver = FakeSolver("", quiz="1. alliance\n2. dilemma\n1B 2A")
    conv, ft = _quiz_auto_world(WithBank(), solver)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    first = next(m["text"] for m in ft.sent if "Instructions" in m.get("text", ""))
    assert "Alliance" in first and "Dilemma" in first
    _run_auto(conv)
    assert any("Alliance" in m for m in solver.calls[0][2])


def test_a_todo_code_typed_during_a_quiz_opens_that_item():
    fc = QuizCanvas()
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo")); code = conv.cs.menu[0].code
    conv.handle(msg(code))
    assert conv.cs.quiz is not None
    conv.handle(msg(code))                                    # was "I couldn't match that to a question"
    assert "couldn't match" not in ft.last and conv.cs.quiz is not None


def test_sweep_skips_done_items_and_shows_no_buttons(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    wa = FakeWebAssign()
    solver = FakeSolver("1.1 x^2\n1.seen 1 0\n2.1 1\n2.seen 1 0\n3.1 1\n3.2 2\n3.seen 2 0")
    conv, ft = _auto_world(monkeypatch, wa, solver)
    from ainl.bot.state import MenuItem
    conv.cs.menu = [MenuItem(idx=1, course_id="2", id="200", kind="webassign", title="7.5 Homework",
                             course="MATH222", types=["external_tool"], url="u", due="Thu", code="B1", done="10/10")]
    n = len(ft.sent)
    conv.handle(msg("/sweep")); conv._auto_thread.join(10)
    assert not solver.calls                                   # done: not opened, not asked
    report = next((tmp_path / "sweeps").glob("*.md")).read_text()
    assert "done (10/10) — skipped" in report
    assert not any(m.get("reply_markup") for m in ft.sent[n + 1:])



# -- the 2026-09-25 /sweep: what it showed on a student's real list -----------------

class CanvasWithMcGraw(FakeCanvas):
    """SPANISH101 as it really is: course 2's items are McGraw Hill Connect,
    an LTI 1.3 tool that has nothing to do with WebAssign."""

    TOOL = {"url": "https://learning.mheducation.com/lti/launch"}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/v1/courses/2/assignments" and req.method == "GET":
            return _j([{"id": i, "submission_types": ["external_tool"],
                        "external_tool_tag_attributes": self.TOOL} for i in ("200", "201", "202")])
        if p == "/api/v1/courses/2/assignments/200":
            return _j({"id": "200", "name": "3 - Prep (Vocab)", "due_at": DUE, "points_possible": 10,
                       "description": "", "submission_types": ["external_tool"],
                       "external_tool_tag_attributes": self.TOOL})
        return super().__call__(req)


def _mcgraw_world(monkeypatch):
    asked = []
    monkeypatch.setattr(Conversation, "_webassign_dep", lambda self, item: asked.append(item.code))
    fc, ft = CanvasWithMcGraw(), FakeTelegram()
    canvas = ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(fc))
    tg = TelegramClient("T", transport=httpx.MockTransport(ft))
    return Conversation(canvas, tg, CHAT, BotState(), tz="UTC"), ft, asked


def test_a_mcgraw_item_never_wakes_webassign(monkeypatch):
    """Opening one Spanish item rebuilt the whole WebAssign session (16 s) and
    then title-matched against MATH222 — for a tool Canvas had already named
    as McGraw Hill."""
    conv, ft, asked = _mcgraw_world(monkeypatch)
    conv.handle(msg("/todo"))
    b1 = next(m for m in conv.cs.menu if m.code == "B1")
    assert b1.kind == "external" and "mheducation.com" in b1.tool
    conv.handle(msg("B1"))
    assert asked == []                                    # WebAssign was not asked
    assert "McGraw Hill" in ft.sent[-2]["text"] and "can't hand this one in" in ft.last


def test_a_macmillan_item_never_wakes_webassign(monkeypatch):
    """ECON101 on 2026-09-26: a Macmillan podcast item, unknown to the bot, was
    tried as WebAssign — 16.5 s launch + 13.9 s session rebuild — and the
    classmate never came back."""
    monkeypatch.setattr(CanvasWithMcGraw, "TOOL", {"url": "https://lmslink.bfwpub.com/index.php?custom_action=openItem"})
    conv, ft, asked = _mcgraw_world(monkeypatch)
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    assert asked == []
    assert "Macmillan" in ft.sent[-2]["text"]


def test_a_never_seen_tool_host_never_wakes_webassign(monkeypatch):
    """WebAssign items always carry Cengage's host, so any other host Canvas
    names is some other platform, even one not in the table."""
    monkeypatch.setattr(CanvasWithMcGraw, "TOOL", {"url": "https://lti.some-new-vendor.example/launch"})
    conv, ft, asked = _mcgraw_world(monkeypatch)
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    assert asked == []
    assert "an external site" in ft.sent[-2]["text"]


def test_an_external_item_of_unknown_tool_still_tries_webassign(monkeypatch):
    """The other side of the same rule: when Canvas names no tool, the item may
    still be WebAssign, and the LTI launch is how we find out."""
    conv, ft, asked = _mcgraw_world(monkeypatch)
    conv.handle(msg("/todo"))
    for m in conv.cs.menu:
        m.tool = ""
    conv.handle(msg("B1"))
    assert asked == ["B1"]


def test_menu_items_saved_before_the_tool_field_still_load(tmp_path):
    p = tmp_path / "state.json"
    old = {"idx": 1, "course_id": "2", "id": "200", "kind": "external", "title": "t", "course": "C",
           "types": [], "url": "u", "due": "d", "code": "B1", "vendor": "", "done": ""}
    p.write_text(json.dumps({"offset": 3, "chats": {"1": {"menu": [old]}}}))
    assert BotState.load(p).chats["1"].menu[0].tool == ""


def test_links_with_nothing_between_them_do_not_run_together():
    from ainl.util.html import html_to_text
    text, links = html_to_text(
        "<p><a href='/courses/2/files/1'>PS3.pdf</a><a href='/courses/2/files/2'>https://x.edu/courses/2/files/2</a></p>",
        "https://x.edu")
    assert "PS3.pdf (https://x.edu/courses/2/files/1)\nhttps://x.edu/courses/2/files/2" in text
    assert [u for _, u in links] == ["https://x.edu/courses/2/files/1", "https://x.edu/courses/2/files/2"]
    # links separated by text are left exactly as they were
    assert html_to_text("<a href='/a'>A</a> and <a href='/b'>B</a>", "https://x.edu")[0] == \
        "A (https://x.edu/a) and B (https://x.edu/b)"


def test_a_file_linked_by_its_own_address_is_captioned_with_its_name(monkeypatch):
    class Bare(FakeCanvas):
        def __call__(self, req):
            if req.url.path == "/api/v1/courses/1/assignments/100":
                r = json.loads(super().__call__(req).content)
                r["description"] = "<a href='/courses/1/files/9'>https://canvas.example.edu/courses/1/files/9</a>"
                return _j(r)
            return super().__call__(req)
    monkeypatch.setattr("ainl.canvas.api.httpx.get", lambda url, **kw: httpx.Response(200, content=b"PDF"))
    ft = FakeTelegram()
    conv = Conversation(ApiSource(BASE, BearerAuth("t"), transport=httpx.MockTransport(Bare())),
                        TelegramClient("T", transport=httpx.MockTransport(ft)), CHAT, BotState(), tz="UTC")
    conv.handle(msg("/todo")); conv.handle(msg("A1"))
    assert ft.docs and "Guide.pdf" in ft.docs[0] and "https://" not in ft.docs[0]


def test_a_connecting_word_is_not_shown_as_a_file_type():
    """ESL116 listed "docx, pdf, png, jpeg, gif, svg, webp, and, jpg"."""
    from ainl.bot.conversation import _extensions
    assert _extensions(["docx", "pdf", "webp", "and", "jpg"]) == ["docx", "pdf", "webp", "jpg"]
    assert _extensions(("PDF", "Or")) == ["PDF"] and _extensions(()) == []


def test_a_long_title_in_todo_shows_that_it_was_cut():
    from ainl.bot.conversation import _clip
    long = 'Read and Annotate: "Sense of Belonging Among International Students in Residence Halls"'
    assert _clip(long, 44).endswith("…") and len(_clip(long, 44)) <= 44
    assert _clip("7.2 Homework", 44) == "7.2 Homework"


def test_a_sweep_of_finished_items_reports_no_findings(monkeypatch, tmp_path):
    """Two finished quizzes were skipped on purpose and came back as "3
    finding(s)" — noise that hid the one real gap in the same report."""
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    conv, ft = _auto_world(monkeypatch, FakeWebAssign(), FakeSolver(""))
    from ainl.bot.state import MenuItem
    conv.cs.menu = [MenuItem(idx=1, course_id="2", id="200", kind="webassign", title="Module Quiz 12",
                             course="MATH222", types=["external_tool"], url="u", due="Thu", code="B1", done="1/2")]
    conv.handle(msg("/sweep")); conv._auto_thread.join(10)
    done = next(m["text"] for m in ft.sent if "sweep done" in m.get("text", ""))
    assert "nothing wrong found" in done and "finding" not in done



def test_sweep_skips_the_courses_the_student_named(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    monkeypatch.delenv("AINL_SWEEP_SKIP", raising=False)          # default: SPANISH101
    wa = FakeWebAssign()
    conv, ft = _auto_world(monkeypatch, wa, FakeSolver(""))
    from ainl.bot.state import MenuItem
    conv.cs.menu = [MenuItem(idx=1, course_id="9", id="1", kind="external", title="3 - Lec (la familia)",
                             course="SPANISH101", types=["external_tool"], url="u", due="Mon", code="D1")]
    opened = []
    monkeypatch.setattr(type(conv), "_open", lambda self, code: opened.append(code))
    conv.handle(msg("/sweep")); conv._auto_thread.join(10)
    assert not opened
    report = next((tmp_path / "sweeps").glob("*.md")).read_text()
    assert "skipped courses: SPANISH101 — 1 item(s) not swept" in report
    assert "0 items to check" in next(m["text"] for m in ft.sent if "<b>sweep</b>:" in m.get("text", ""))


def test_dead_cengage_tabs_are_closed_but_canvas_is_left_alone():
    from ainl.external.webassign import WebAssign
    class Pg:
        def __init__(self, url): self.url, self.closed = url, False
        def is_closed(self): return self.closed
        def close(self): self.closed = True
    keep = Pg("https://www.webassign.net/v4cgi/student.pl?action=home/index")
    dead1, dead2 = Pg("https://account.cengage.com/logout/success"), Pg("about:blank")
    canvas = Pg("https://canvas.wisc.edu/login")
    assert WebAssign._close_dead_tabs([keep, dead1, dead2, canvas], keep) == 2
    assert dead1.closed and dead2.closed and not keep.closed and not canvas.closed



def test_sweep_numbers_only_the_items_left_to_do(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    wa = FakeWebAssign()
    conv, ft = _auto_world(monkeypatch, wa, FakeSolver("1.1 x^2\n2.1 1\n3.1 1\n3.2 2"))
    monkeypatch.setattr(type(conv), "_webassign_dep", lambda self, item: "40575442")
    from ainl.bot.state import MenuItem
    mk = lambda code, done="": MenuItem(idx=1, course_id="2", id=code, kind="webassign", title=code,   # noqa: E731
                                        course="MATH222", types=["external_tool"], url="u", due="Thu",
                                        code=code, done=done)
    conv.cs.menu = [mk("B1", "1/2"), mk("B2", "2/2"), mk("B3")]
    conv.handle(msg("/sweep")); conv._auto_thread.join(10)
    heads = [m["text"] for m in ft.sent if "━━" in m.get("text", "")]
    assert len(heads) == 1 and "[1/1] B3" in heads[0]            # done items are not walked or counted
    assert not wa.submitted


def test_an_external_platform_gets_a_one_tap_launch_button(monkeypatch):
    """The Canvas address failed on phones (not logged in to Canvas there);
    the sessionless launch opens the vendor directly (2026-09-26)."""
    conv, ft, asked = _mcgraw_world(monkeypatch)
    secret = "https://canvas.example.edu/courses/1/external_tools/sessionless_launch?verifier=SECRET"
    monkeypatch.setattr(type(conv.canvas), "sessionless_launch", lambda self, c, a: secret)
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    (button,) = [b for row in ft.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert button == {"text": "↗ Open McGraw Hill", "url": secret}
    assert "no Canvas login needed" in ft.last and "B1" in ft.last


def test_link_buttons_are_logged_without_their_query():
    from ainl.telegram.transcript import compact_markup
    km = compact_markup({"inline_keyboard": [[{"text": "↗ Open", "url": "https://x.edu/launch?verifier=SECRET"},
                                              {"text": "A", "callback_data": "c:1"}]]})
    assert km == [[["↗ Open", "https://x.edu/launch?…"], ["A", "c:1"]]]


def test_a_triple_tap_on_look_ahead_sends_one_list(monkeypatch):
    conv, ft, _ = _mcgraw_world(monkeypatch)
    conv.handle(msg("/todo"))
    n = len(ft.sent)
    for _ in range(3):
        conv.handle(cb("todo:14"))
    assert len(ft.sent) == n + 1


def test_check_never_opens_a_quiz_and_mutes_the_item_itself(monkeypatch):
    from types import SimpleNamespace
    conv, ft, _ = _mcgraw_world(monkeypatch)
    quiz = SimpleNamespace(kind="quiz", code="A1", title="Quiz", course="X", types=[], tool="", done="")
    assert conv._check_one(quiz) == ["Canvas quiz — not opened (opening can start an attempt)"]
    from ainl.bot.conversation import _Muted
    m = _Muted(conv.tg)
    m.send(CHAT, "<b>hello</b>")
    assert m.said == ["hello"] and m.send_photo(CHAT, b"x") == 0


def test_a_failed_gradescope_upload_keeps_the_file_and_one_tap_retries(monkeypatch):
    """2026-09-26: the upload hit Gradescope's 404 and the file was dropped —
    the student had to send it again. A definite failure keeps it."""
    conv, fc, ft, gs = gs_world(monkeypatch)
    results = iter([{"status": "failed", "title": "404", "shot": None},
                    {"status": "uploaded", "moved": True, "title": "Assign pages | Gradescope", "shot": None}])
    gs.upload_pdf = lambda url, name, pdf: (gs.got.append((url, name, pdf)), next(results))[1]
    conv.handle(msg("/todo"))
    conv.handle(msg("B1"))
    conv.handle(msg(document={"file_id": "F9", "file_name": "ps3.pdf", "mime_type": "application/pdf"}))
    conv.handle(cb("submit"))
    assert "did <b>not</b> go through" in ft.last and "still here" in ft.last
    buttons = [b["callback_data"] for row in ft.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["submit", "cancel"]
    conv.handle(cb("submit"))                                      # 🔁 Try again
    assert len(gs.got) == 2 and gs.got[0][2] == gs.got[1][2]       # the same file, not re-sent
    assert "Uploaded to Gradescope" in ft.last


def test_a_one_question_at_a_time_quiz_is_not_started_here():
    """Study habits quiz, 2026-09-27: Canvas refuses to list such a quiz's
    questions — after the attempt has begun. So it is not begun here."""
    class OneAtATime(QuizCanvas):
        def __call__(self, req):
            if req.url.path == "/api/v1/courses/1/quizzes/749803" and req.method == "GET":
                body = json.loads(super().__call__(req).content)
                return _j(body | {"one_question_at_a_time": True})
            return super().__call__(req)
    fc = OneAtATime(attempts_allowed=1)
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    assert fc.started == 0 and conv.cs.quiz is None
    assert "one question at a time" in ft.last and "Nothing was started" in ft.last
    assert "Canvas access expired" not in " ".join(m.get("text", "") for m in ft.sent)


def test_an_attempt_whose_questions_canvas_refuses_says_it_has_started():
    """If Canvas refuses the questions anyway (the setting unseen, or changed):
    not "access expired" — the attempt HAS started, finish it in Canvas."""
    class Refuses(QuizCanvas):
        def __call__(self, req):
            if req.url.path == "/api/v1/quiz_submissions/99/questions" and req.method == "GET":
                return httpx.Response(401, json={"status": "unauthorized",
                                                 "message": "Cannot receive one question at a time questions in the API"})
            return super().__call__(req)
    fc = Refuses()
    conv, ft = _quiz_world(fc)
    conv.handle(msg("/todo")); conv.handle(msg(conv.cs.menu[0].code))
    assert fc.started == 1
    assert "Your attempt has started" in ft.last and "/courses/1/quizzes/749803/take" in ft.last
    assert "Canvas access expired" not in ft.last


def test_graded_lines_carry_every_question_number_and_judge_by_the_score():
    """9.2 Homework, 2026-09-27: choice-only questions came back as
    "? 1 / 2 Points" — no number (they have no typed box to learn it from)
    and "?" (their check marks are not the typed boxes' images)."""
    from types import SimpleNamespace
    ms = SimpleNamespace(targets={4: ["RA_40575450_3_1_a"]},
                         taps=[{"n": 1, "name": "RC_40575447_0_1_a"}, {"n": 2, "name": "RC_40575448_1_1_a"}])
    by = Conversation._numbers_by_dom(ms)
    assert by == {"question40575450_3": 4, "question40575447_0": 1, "question40575448_1": 2}
    v = Conversation._verdict
    assert v("2 / 2 Points", []) == "right" and v("0 / 2 Points", ["?"]) == "wrong"
    assert v("1 / 2 Points", []) == "partial" and v("– / 2 Points", []) == "pending"
    assert v(None, ["right", "right"]) == "right" and v("", []) == "?"


def test_redo_reopens_only_the_ones_not_right_and_finish_ends_the_round(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=GradedWA())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    n_albums = len(ft.albums) + len(ft.photos)
    conv.handle(cb("mredo:40575442"))
    ms = conv.cs.math
    assert ms.stage == "shoot" and set(ms.targets) == {2}             # only 2, shown again
    assert "redo 2" in " ".join(m.get("text", "") for m in ft.sent[-4:])
    assert len(ft.albums) + len(ft.photos) > n_albums                 # its picture came again
    conv2, wa2, ft2 = math_world(monkeypatch, wa=GradedWA())
    conv2.handle(msg("/math")); conv2.handle(cb("m:40575442")); conv2.handle(photo_msg())
    conv2.handle(cb("mfill")); conv2.handle(cb("msubmit:40575442"))
    conv2.handle(cb("mfinish:40575442"))
    assert conv2.cs.math is None and "Finished" in ft2.last


def test_a_finished_round_turns_its_todo_line_green_in_place(monkeypatch):
    conv, wa, ft = math_world(monkeypatch)
    conv.handle(msg("/todo"))
    todo_id = conv.cs.todo_msg.get("id")
    assert todo_id and conv.cs.todo_msg.get("days")
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert conv.cs.math is None and "All correct" in ft.last
    assert ft.edits and ft.edits[-1]["message_id"] == todo_id            # the same list, redrawn


def test_auto_redoes_a_wrong_choice_question_too(monkeypatch):
    """9.2 Homework q3, 2026-09-27: after a submit the round's choice parts were
    gone, so the redo sent the question picture alone, to the text model, and
    its letters had nowhere to go — "nothing differs", twice."""
    class ChoiceRedoWA(MixedWA):
        def __init__(self):
            super().__init__()
            self.submitted, self.rounds = [], 0

        def submit(self, dep, boxes, expect=None):
            self.rounds += 1
            self.submitted.extend(boxes)
            doms = sorted({f"question{b.split('_')[1]}_{b.split('_')[2]}" for b in boxes})
            out = {}
            for d in doms:
                wrong = d == "question33_2" and self.chosen.get("RC_33_2_0_93") == ["0"]
                out[d] = {"points": ("0 / 1" if wrong else "1 / 1") + " Points", "marks": []}
            return {"submitted": doms, "failed": {}, "outcome": out}, [b"PNG"]
    wa = ChoiceRedoWA()
    solver = FakeSolver("1.1 x^2\n2.c1 B\n2.1 5\n3.c1 A", revision="3.c1 B")
    conv, ft = _auto_world(monkeypatch, wa, solver)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); _run_auto(conv)
    assert [c[0] for c in solver.calls] == ["solve", "revise"]
    redo_items = solver.calls[1][2]
    assert [(i[0], i[2]) for i in redo_items] == [(3, [["yes", "no"]])]    # its choices, again
    assert wa.chosen["RC_33_2_0_93"] == ["1"] and wa.rounds == 2
    assert not any("Nothing on that differs" in m.get("text", "") for m in ft.sent)
    assert conv.cs.math is None


def test_typed_choices_are_read_in_every_shape():
    from ainl.bot.conversation import typed_choice
    assert typed_choice("3d") == (3, None, "D") and typed_choice("3: D") == (3, None, "D")
    assert typed_choice("3(a) D") == typed_choice("3a D") == typed_choice("3. C1 D") == (3, 0, "D")
    assert typed_choice("3 c2 B, D") == (3, 1, "BD")
    assert typed_choice(r"3: \frac{13}{2}") is None and typed_choice("3: 22.6") is None
    assert typed_choice("3: diverges") is None


def test_after_a_submit_a_typed_choice_reopens_that_question_and_goes_in(monkeypatch):
    """9.2 Homework q3, 2026-09-27: "3: D" said "There's no question 3" and
    "3. C1 D" was not understood — a submitted choice could not be changed."""
    class ChoiceWA(MixedWA):
        def __init__(self):
            super().__init__()
            self.submitted, self.rounds = [], 0

        def submit(self, dep, boxes, expect=None):
            self.rounds += 1
            self.submitted.extend(boxes)
            doms = sorted({f"question{b.split('_')[1]}_{b.split('_')[2]}" for b in boxes})
            out = {d: {"points": ("0 / 1" if d == "question33_2" and self.chosen.get("RC_33_2_0_93") == ["0"]
                                  else "1 / 1") + " Points", "marks": []} for d in doms}
            return {"submitted": doms, "failed": {}, "outcome": out}, [b"PNG"]
    wa = ChoiceWA()
    conv, wa, ft = math_world(monkeypatch, latex=r"1. x^2 \quad 2. 5", wa=wa)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    conv.handle(cb("c:40575442:0:0")); conv.handle(cb("c:40575442:1:0"))   # 2: convergent, 3: yes
    conv.handle(photo_msg()); conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    assert conv.cs.math.stage == "graded" and conv.cs.math.redo == [3]
    conv.handle(msg("3: B"))                                  # "no", typed, after the submit
    ms = conv.cs.math
    assert [t["n"] for t in ms.taps] == [3] and ms.picked["RC_33_2_0_93"] == ["1"]
    assert "There's no question 3" not in " ".join(m.get("text", "") for m in ft.sent)
    assert "mfill!" in json.dumps(ft.sent[-1].get("reply_markup"))
    conv.handle(cb("mfill!")); conv.handle(cb("msubmit:40575442"))
    assert wa.chosen["RC_33_2_0_93"] == ["1"] and wa.rounds == 2
    assert conv.cs.math is None and "All correct" in ft.last


def test_fill_and_submit_can_be_typed(monkeypatch):
    """2026-09-27: "Fill", "Then?" and "Submit" typed got the help text back."""
    conv, wa, ft = math_world(monkeypatch, wa=MixedWA())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    conv.handle(msg("3 b"))                                   # a typed choice, 1 and 2 not written
    assert "mfill!" in json.dumps(ft.sent[-1].get("reply_markup"))
    conv.handle(msg("Fill"))
    assert conv.cs.math.stage == "filled" and wa.chosen["RC_33_2_0_93"] == ["1"]
    conv.handle(msg("submit"))
    assert wa.submitted == ["RC_33_2_0_93"]                  # only what was answered went in
    assert "nothing is lost" not in " ".join(m.get("text", "") for m in ft.sent[-3:])


def test_an_assignment_worked_on_before_opens_with_only_what_is_not_right(monkeypatch):
    """The owner, 2026-09-27: a done question need not be shown again — open
    it like Redo, with just the ones not at full marks."""
    import dataclasses
    def graded(q, head):
        return dataclasses.replace(q, stem=f"{q.n}. [{head} Points] DETAILS")
    base = FakeWebAssign().qs
    wa = FakeWebAssign([graded(base[0], "1 / 1"), graded(base[1], "0 / 1"), graded(base[2], "– / 2")])
    conv, wa, ft = math_world(monkeypatch, wa=wa)
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    ms = conv.cs.math
    assert sorted(ms.targets) == [2, 3]                         # 1 is right: not shown again
    assert "redo 2, 3" in " ".join(m.get("text", "") for m in ft.sent[-4:])
    assert ft.albums[-1] == 2

    wa2 = FakeWebAssign([graded(q, "1 / 1") for q in base])
    conv2, wa2, ft2 = math_world(monkeypatch, wa=wa2)
    conv2.handle(msg("/math")); conv2.handle(cb("m:40575442"))
    assert conv2.cs.math is None and "already right" in ft2.last


def test_a_round_sends_one_set_of_pictures_and_says_the_grade_in_words(monkeypatch):
    """The owner, 2026-09-27: blank pictures, then filled ones, then graded
    ones — three sets per assignment. Now: the filled picture replaces the
    blank one where it is, and the grade comes as text only."""
    class ShotWA(FakeWebAssign):                               # like the real one: which question each shot is
        def fill(self, dep, answers):
            self.filled.update(answers)
            doms = sorted({f"question{b.split('_')[1]}_{b.split('_')[2]}" for b in answers})
            return {"readback": dict(answers), "missing": [], "shot_doms": doms}, [b"\x89PNG-f"] * len(doms)
    conv, wa, ft = math_world(monkeypatch, wa=ShotWA())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442"))
    ms = conv.cs.math
    assert ft.albums == [3] and ms.shot_msgs == {1: 90, 2: 91, 3: 92}
    conv.handle(photo_msg()); conv.handle(cb("mfill"))
    assert sorted(ft.media_edits) == [90, 91] and ft.photos == []   # 1 and 2 answered: replaced in place
    conv.handle(cb("msubmit:40575442"))
    assert ft.photos == [] and ft.albums == [3]               # no graded pictures
    assert "Submitted" in " ".join(m.get("text", "") for m in ft.sent[-3:])


def test_a_second_go_submits_in_one_tap(monkeypatch):
    conv, wa, ft = math_world(monkeypatch, wa=GradedWA())
    conv.handle(msg("/math")); conv.handle(cb("m:40575442")); conv.handle(photo_msg())
    conv.handle(cb("mfill")); conv.handle(cb("msubmit:40575442"))
    conv.handle(cb("mredo:40575442"))                          # 2 was wrong: a second go
    ms = conv.cs.math
    assert ms.again and ms.stage == "shoot"
    conv.handle(msg(r"2: \sin(x)"))
    kb = json.dumps(ft.sent[-1].get("reply_markup"))
    assert "Submit" in kb and "mfill!+" in kb and "Fill these in" not in kb
    n_sub = len(wa.submitted)
    conv.handle(cb("mfill!+"))                                 # one tap: filled AND submitted
    assert wa.submitted[n_sub:] == ["RA_22_1_0_92"] and conv.cs.math is None
