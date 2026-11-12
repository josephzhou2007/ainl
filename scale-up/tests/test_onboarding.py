"""Registration over Telegram with a fake client: no network, no Canvas."""
from __future__ import annotations

import json

import tokens
from onboarding import Onboarding
from registry import Registry

OWNER = "1"


class FakeTG:
    def __init__(self):
        self.sent: list[tuple[str, str, dict | None]] = []

    def send(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((str(chat_id), text, reply_markup))

    def answer_callback(self, cb_id, text=""):
        pass

    def to(self, chat_id):
        return [t for c, t, _ in self.sent if c == str(chat_id)]

    @property
    def last(self):
        return self.sent[-1][1]


def _msg(chat, text, kind="private", first="Ada", username="ada"):
    return {"update_id": 1, "message": {"chat": {"id": int(chat), "type": kind}, "text": text,
                                        "from": {"first_name": first, "username": username}}}


def _cb(from_chat, data):
    return {"update_id": 2, "callback_query": {"id": "x", "data": data,
                                               "message": {"chat": {"id": int(from_chat)}}}}


def _no_network(*a, **kw):
    raise AssertionError("no network in tests")


def _world(tmp_path, **kw):
    reg, tg = Registry(base=tmp_path), FakeTG()
    kw.setdefault("directory", lambda q: [])
    kw.setdefault("detect_school", _no_network)
    kw.setdefault("background", lambda fn: fn())       # searches run inline in tests
    return reg, tg, Onboarding(reg, tg, OWNER, **kw)


def _token(tg, chat):
    """The token from the latest message that carries one."""
    msg = [t for t in tg.to(chat) if "<code>" in t][-1]
    return msg.split("<code>")[1].split("</code>")[0]


def _buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


def test_a_stranger_is_told_to_start_and_nothing_is_kept(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "hello"))
    assert "Send /start" in tg.last and reg.all() == []


def test_start_registers_unapproved_and_asks_the_owner(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "/start"))
    u = reg.get("55")
    assert u and not u.approved and u.name == "Ada" and u.login_id == "ada"
    assert "waiting for approval" in tg.to("55")[-1]
    chat, text, markup = tg.sent[-1]
    assert chat == OWNER and "Ada" in text and "@ada" in text
    datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert datas == ["reg:ok:55", "reg:no:55"]
    ob.handle(_msg("55", "/todo"))
    assert "Not approved yet" in tg.last


def test_approve_issues_a_token_once_and_stores_only_its_hash(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "/start"))
    ob.handle(_cb(OWNER, "reg:ok:55"))
    assert reg.is_approved("55")
    tok = _token(tg, "55")
    assert len(tok) > 30
    on_disk = json.loads((reg.user_dir("55") / "app-token.json").read_text())
    assert tok not in json.dumps(on_disk) and len(on_disk["sha256"]) == 64
    assert tokens.verify(reg, tok) == "55"
    assert tokens.verify(reg, "nope") is None
    assert "Approved Ada" in tg.to(OWNER)[-1]


def test_only_the_owner_can_approve(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "/start"))
    ob.handle(_cb("55", "reg:ok:55"))                   # the applicant pressing it
    ob.handle(_cb("77", "reg:ok:55"))                   # anyone else
    assert not reg.is_approved("55")


def test_reject_removes_the_record_and_says_so(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "/start"))
    ob.handle(_cb(OWNER, "reg:no:55"))
    assert reg.get("55") is None
    assert "didn't approve" in tg.to("55")[-1]


def test_token_reissue_revokes_the_old_one(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "/start"))
    ob.handle(_cb(OWNER, "reg:ok:55"))
    old = _token(tg, "55")
    ob.handle(_msg("55", "/token"))
    new = _token(tg, "55")
    assert new != old
    assert tokens.verify(reg, old) is None and tokens.verify(reg, new) == "55"


def test_forget_deletes_everything(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "/start"))
    ob.handle(_cb(OWNER, "reg:ok:55"))
    d = reg.user_dir("55")
    assert d.exists()
    ob.handle(_msg("55", "/forget"))
    assert reg.get("55") is None and not d.exists()
    assert "gone" in tg.last


def test_unapproved_token_never_verifies(tmp_path):
    reg, tg, ob = _world(tmp_path)
    reg.add("55", "Ada", "ada")
    tok = tokens.issue(reg, "55")
    assert tokens.verify(reg, tok) is None            # not approved


def test_groups_are_ignored(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("-100", "/start", kind="supergroup"))
    assert tg.sent == [] and reg.all() == []


def test_users_listing_for_the_owner(tmp_path):
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("55", "/start"))
    ob.handle(_msg("66", "/start", first="Bo", username="bo"))
    ob.handle(_cb(OWNER, "reg:ok:66"))
    out = ob.admin_command("/users")
    assert "Ada ⏳" in out and "Bo ✅ 🔑" in out
    assert ob.admin_command("/todo") is None


LOGIN = "https://login.example.org/login"


def test_approval_asks_for_the_school_then_offers_its_login(tmp_path):
    reg, tg, ob = _world(tmp_path, login_url=LOGIN)
    ob.handle(_msg("55", "/start"))
    ob.handle(_cb(OWNER, "reg:ok:55"))
    chat, text, markup = [s for s in tg.sent if s[0] == "55"][-1]
    assert "Which school" in text and "Type your school's name" in text
    assert [b["callback_data"] for b in _buttons(markup)] == ["school:?"]
    ob.handle(_msg("55", "santa barbara"))              # the next text is the search
    datas = [b["callback_data"] for b in _buttons(tg.sent[-1][2])]
    assert datas == ["school:ucsb", "school:?"]
    ob.handle(_cb("55", "school:ucsb"))                 # their own button, in their own chat
    assert reg.get("55").school == "ucsb"
    chat, text, markup = tg.sent[-1]
    assert chat == "55" and "UC Santa Barbara" in text and "UCSB NetID" in text
    assert "password goes only to your school" in text
    (button,) = _buttons(markup)
    assert button["text"] == "🔐 Canvas login"
    assert button["web_app"] == {"url": LOGIN}
    assert "web app" not in text.lower() and "webapp" not in text.lower()


def test_login_command_and_any_text_lead_to_the_button(tmp_path):
    reg, tg, ob = _world(tmp_path, login_url=LOGIN)
    reg.add("55", "Ada", "ada")
    reg.approve("55")
    reg.set_school("55", "uw")
    for text in ("/login", "hello", "/todo"):
        ob.handle(_msg("55", text))
        assert _buttons(tg.sent[-1][2])[0]["web_app"]["url"] == LOGIN


def test_without_a_login_url_it_says_the_step_is_not_open(tmp_path):
    reg, tg, ob = _world(tmp_path)
    reg.add("55", "Ada", "ada")
    reg.approve("55")
    ob.handle(_msg("55", "/login"))
    assert "isn't open yet" in tg.last and tg.sent[-1][2] is None


def test_connected_people_are_told_so_and_listed_with_a_lock(tmp_path):
    reg, tg, ob = _world(tmp_path, login_url=LOGIN, connected=lambda cid: cid == "55")
    reg.add("55", "Ada", "ada")
    reg.approve("55")
    ob.handle(_msg("55", "hi"))
    assert "Canvas is connected" in tg.last
    assert "🔐" in ob.admin_command("/users")


def test_forget_stops_their_browser_before_deleting(tmp_path):
    order = []
    reg = Registry(base=tmp_path)
    tg = FakeTG()
    ob = Onboarding(reg, tg, OWNER, on_forget=lambda cid: order.append(
        ("forget", cid, reg.user_dir(cid).exists())))
    reg.add("55", "Ada", "ada")
    ob.handle(_msg("55", "/forget"))
    assert order == [("forget", "55", True)]        # called while the dir still existed
    assert reg.get("55") is None


def test_nobody_picks_a_school_for_someone_else_or_before_approval(tmp_path):
    reg, tg, ob = _world(tmp_path, login_url=LOGIN)
    reg.add("55", "Ada", "ada")                        # registered, not approved
    ob.handle(_cb("55", "school:uw"))
    assert reg.get("55").school == ""
    reg.approve("55")
    ob.handle(_cb("66", "school:uw"))                  # a button pressed in another chat
    assert reg.get("55").school == ""
    ob.handle(_cb("55", "school:nowhere"))             # not a school we know
    assert reg.get("55").school == ""


def test_switching_school_drops_the_old_session_first(tmp_path):
    dropped = []
    reg, tg, ob = _world(tmp_path, login_url=LOGIN, connected=lambda c: False,
                         on_school_change=dropped.append)
    reg.add("55", "Ada", "ada")
    reg.approve("55")
    ob.handle(_cb("55", "school:uw"))
    assert dropped == []                               # first pick: nothing to drop
    ob.handle(_msg("55", "/school"))
    assert "Which school" in tg.sent[-1][1]
    ob.handle(_msg("55", "UCSB"))
    assert "school:ucsb" in [b["callback_data"] for b in _buttons(tg.sent[-1][2])]
    ob.handle(_cb("55", "school:ucsb"))
    assert dropped == ["55"] and reg.get("55").school == "ucsb"
    assert any("Switched to UC Santa Barbara" in t for _, t, _ in tg.sent)


def test_school_survives_a_reload_and_old_records_have_none(tmp_path):
    reg = Registry(base=tmp_path)
    reg.add("55", "Ada", "ada")
    reg.set_school("55", "ucsb")
    assert Registry(base=tmp_path).get("55").school == "ucsb"
    import json
    raw = json.loads(reg.path.read_text())
    del raw["users"]["55"]["school"]                   # a registry from before schools
    reg.path.write_text(json.dumps(raw))
    assert Registry(base=tmp_path).get("55").school == ""


def test_with_auto_approve_a_start_is_approved_at_once(tmp_path, monkeypatch):
    """2026-09-27: ten classmates at once — the owner's Approve tap is switched
    off (not removed). They get their token straight away; the owner is told,
    with no buttons. Someone left waiting from before is approved on their
    next message."""
    reg, tg, ob = _world(tmp_path)
    ob.handle(_msg("66", "/start"))                     # asked while approval was by hand
    assert not reg.is_approved("66")
    monkeypatch.setenv("AINL_AUTO_APPROVE", "1")
    ob.handle(_msg("55", "/start"))
    assert reg.is_approved("55") and _token(tg, "55")
    owner_msgs = tg.to(OWNER)
    assert "approved automatically" in owner_msgs[-1]
    ob.handle(_msg("66", "hello"))
    assert reg.is_approved("66") and _token(tg, "66")
