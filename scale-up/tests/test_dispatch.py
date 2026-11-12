"""The dispatcher with fakes: no Telegram, no Canvas, no browser."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import dispatch
from dispatch import Dispatcher, Heartbeat
from registry import Registry


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, **kw):
        self.sent.append((str(chat_id), text))


class FakeConv:
    def __init__(self, cid, log, hold=0.0):
        self.cid, self.log, self.hold = cid, log, hold
        self.canvas = SimpleNamespace(close=lambda: None)
        self._state_path = None
        self._lock = threading.RLock()

    def handle(self, u):
        from ainl.util import browserthread
        self.log.append((self.cid, browserthread.current_key(), threading.current_thread().name,
                         (u.get("message") or {}).get("text")))
        time.sleep(self.hold)


def _msg(cid, text):
    return {"message": {"chat": {"id": int(cid), "type": "private"}, "text": text}}


def _world(tmp_path, hold=0.0, connected=("111", "222")):
    reg = Registry(base=tmp_path)
    for cid in ("111", "222", "333"):
        reg.add(cid, cid, cid)
        reg.approve(cid)
    reg.add("444", "pending", "p")                        # not approved
    log, built = [], []

    def make(cid):
        built.append(cid)
        return FakeConv(cid, log, hold)

    d = Dispatcher(reg, pool=None, tg=FakeTG(), canvas_origin="https://canvas.example.edu",
                   connected=lambda cid: cid in connected, make_conversation=make)
    return d, log, built


def _wait(pred, t=3.0):
    end = time.time() + t
    while time.time() < end and not pred():
        time.sleep(0.02)
    assert pred()


def test_routing_only_connected_approved_people_and_not_onboarding_commands(tmp_path):
    d, *_ = _world(tmp_path)
    assert d.wants(_msg("111", "/todo")) and d.wants(_msg("111", "B1"))
    assert d.wants(_msg("111", "/start"))
    for cmd in ("/login", "/token", "/forget", "/LOGIN@canvashelper_bot"):
        assert not d.wants(_msg("111", cmd))
    assert not d.wants(_msg("333", "/todo"))              # approved, not connected
    assert not d.wants(_msg("444", "/todo"))              # not approved
    assert not d.wants(_msg("999", "/todo"))              # a stranger
    group = {"message": {"chat": {"id": -5, "type": "group"}, "text": "/todo"}}
    assert not d.wants(group)
    tap = {"callback_query": {"data": "pick:B1", "message": {"chat": {"id": 111, "type": "private"}}}}
    assert d.wants(tap)
    tap["callback_query"]["data"] = "reg:ok:111"          # approvals belong to onboarding
    assert not d.wants(tap)


def test_each_person_runs_on_their_own_worker_in_parallel(tmp_path):
    d, log, built = _world(tmp_path, hold=0.4)
    t0 = time.time()
    d.submit("111", _msg("111", "/todo"))
    d.submit("222", _msg("222", "/todo"))
    _wait(lambda: len(log) == 2)
    assert time.time() - t0 < 0.75                        # not one after the other
    by = {cid: key for cid, key, _, _ in log}
    assert by == {"111": "111", "222": "222"}             # browser work lands on their own key
    assert {name for _, _, name, _ in log} == {"person-111", "person-222"}


def test_one_assistant_per_person_kept_between_messages(tmp_path):
    d, log, built = _world(tmp_path)
    for text in ("/todo", "B1", "/cancel"):
        d.submit("111", _msg("111", text))
    _wait(lambda: len(log) == 3)
    assert built == ["111"] and [t for *_, t in log] == ["/todo", "B1", "/cancel"]


def test_owner_tools_are_refused_before_reaching_the_assistant(tmp_path):
    d, log, built = _world(tmp_path)
    for cmd in ("/auto", "/sweep", "/stats", "/channel token", "/timing"):
        d.submit("111", _msg("111", cmd))
    _wait(lambda: len(d.tg.sent) == 5)
    assert log == [] and all("only for the bot's owner" in t for _, t in d.tg.sent)


def test_logged_out_person_is_asked_to_log_in_again(tmp_path):
    reg = Registry(base=tmp_path)
    reg.add("111", "a", "a")
    reg.approve("111")
    d = Dispatcher(reg, pool=None, tg=FakeTG(), canvas_origin="https://c.example.edu",
                   connected=lambda c: True, make_conversation=lambda c: None)
    d.submit("111", _msg("111", "/todo"))
    _wait(lambda: d.tg.sent)
    assert "Canvas login" in d.tg.sent[0][1]


def test_drop_rebuilds_on_the_next_message(tmp_path):
    d, log, built = _world(tmp_path)
    d.submit("111", _msg("111", "/todo"))
    _wait(lambda: len(log) == 1)
    d.drop("111")
    d.submit("111", _msg("111", "/todo"))
    _wait(lambda: len(log) == 2)
    assert built == ["111", "111"]


# -- the real Conversation subclass: nothing of the owner's leaks in -----------

def _classmate(recover_called, reauth_result=None):
    cls = dispatch._conversation_class()
    canvas = SimpleNamespace(
        recover_auth=lambda *a, **k: recover_called.append(1),
        close=lambda: None, _channel="cookie")
    pool = SimpleNamespace(cdp_url=lambda uid: f"http://127.0.0.1:93{uid[-2:]}",
                           restart=lambda uid: recover_called.append(("restart", uid)))
    from ainl.bot.state import BotState
    tg = FakeTG()
    conv = cls(canvas, tg, "111", BotState(), autosolve=False, pool=pool, uid="111",
               reauth=lambda: reauth_result)
    return conv, tg


def test_classmate_never_falls_back_to_another_channel():
    recover = []
    conv, tg = _classmate(recover)
    assert conv._fall_back(Exception("401")) is True
    assert recover == []                                  # recover_auth = the owner's channel
    assert "Canvas login" in tg.sent[-1][1]


def test_classmate_webassign_and_gradescope_use_their_own_browser():
    recover = []
    conv, _ = _classmate(recover)
    assert conv._wa().cdp_url == "http://127.0.0.1:9311"
    conv._wa()._restart()
    assert recover == [("restart", "111")]               # only theirs, never 9222
    assert conv._gs().cdp_url == "http://127.0.0.1:9311"


def test_classmate_has_no_model_answers_and_no_channel_switch():
    conv, tg = _classmate([])
    assert not conv.autosolve
    conv._channel("token")
    assert "only for the bot's owner" in tg.sent[-1][1]


# -- heartbeat ---------------------------------------------------------------

def test_heartbeat_checks_only_running_browsers_and_tells_once(tmp_path):
    reg = Registry(base=tmp_path)
    for cid in ("111", "222"):
        reg.add(cid, cid, cid)
        reg.approve(cid)
    states = {"111": "reauth_required"}
    calls, dropped = [], []
    sessions = SimpleNamespace(heartbeat=lambda cid: (calls.append(cid), SimpleNamespace(
        state=SimpleNamespace(value=states.get(cid, "active"))))[1])
    pool = SimpleNamespace(running=lambda cid: cid == "111")
    tg = FakeTG()
    hb = Heartbeat(reg, pool, sessions, tg, connected=lambda c: True, on_logged_out=dropped.append)
    hb.tick()
    hb.tick()
    assert calls == ["111", "111"]                        # 222's browser was reaped: left alone
    assert dropped == ["111"] and len(tg.sent) == 1       # told once, not every tick
    states["111"] = "active"
    hb.tick()
    states["111"] = "reauth_required"
    hb.tick()
    assert len(tg.sent) == 2                              # a new logout after recovering: told again


def test_the_logged_out_message_carries_the_login_button(tmp_path, monkeypatch):
    """Logging back in is one tap: the heartbeat's "Canvas logged you out"
    comes with the 🔐 Canvas login button, not just the words."""
    import dispatch
    kb = {"inline_keyboard": [[{"text": "🔐 Canvas login", "web_app": {"url": "https://x/login"}}]]}
    monkeypatch.setattr(dispatch, "login_keyboard", lambda: kb)
    reg = Registry(base=tmp_path)
    reg.add("111", "111", "111")
    reg.approve("111")
    sessions = SimpleNamespace(heartbeat=lambda cid: SimpleNamespace(
        state=SimpleNamespace(value="reauth_required")))
    got = []

    class TG:
        def send(self, chat_id, text, **kw):
            got.append((text, kw.get("reply_markup")))

    Heartbeat(reg, SimpleNamespace(running=lambda c: True), sessions, TG(),
              connected=lambda c: True).tick()
    assert got == [(dispatch.RELOGIN, kb)]


def test_classmate_help_lists_only_their_commands():
    conv, tg = _classmate([])
    conv._command("/help")
    text = tg.sent[-1][1]
    assert "/todo" in text and "/login" in text and "/forget" in text
    assert "/channel" not in text and "/timing" not in text and "/auto" not in text


def test_school_picking_stays_with_onboarding(tmp_path):
    d, *_ = _world(tmp_path)
    assert not d.wants(_msg("111", "/school"))
    tap = {"callback_query": {"data": "school:ucsb", "message": {"chat": {"id": 111, "type": "private"}}}}
    assert not d.wants(tap)


def test_no_school_means_no_canvas(tmp_path):
    reg = Registry(base=tmp_path)
    d = Dispatcher(reg, pool=SimpleNamespace(cdp_url=lambda c: 1 / 0), tg=FakeTG(),
                   canvas_origin=lambda uid: None, connected=lambda c: True)
    assert d._canvas("111") is None                      # never even starts a browser


def test_busy_while_working_and_the_browser_is_touched_around_it(tmp_path):
    touched = []
    d, log, built = _world(tmp_path, hold=0.4)
    d.pool = SimpleNamespace(touch=touched.append)
    d.submit("111", _msg("111", "/todo"))
    _wait(lambda: d.busy("111"))
    assert not d.busy("222")
    _wait(lambda: not d.busy("111"))
    assert touched == ["111", "111"]                       # at the start and at the end


def test_tabs_are_released_when_the_next_move_is_theirs(tmp_path):
    released = []
    d, log, built = _world(tmp_path)
    d.pool = SimpleNamespace(touch=lambda c: None, tidy_now=released.append)
    waiting = {"now": False}
    d._make = lambda cid: SimpleNamespace(handle=lambda u: None, canvas=SimpleNamespace(close=lambda: None),
                                          _state_path=None, _lock=threading.RLock(),
                                          waiting_for_student=lambda: waiting["now"])
    d.submit("111", _msg("111", "/todo"))
    _wait(lambda: not d.busy("111"))
    assert released == []                                  # a list: they will pick next, fast
    waiting["now"] = True
    d.submit("111", _msg("111", "A1"))                      # questions sent: they write now
    _wait(lambda: released == ["111"])


def test_classmate_external_items_can_open_in_their_own_browser():
    recover = []
    conv, tg = _classmate(recover)
    item = SimpleNamespace(url="https://canvas.wisc.edu/courses/1/assignments/2", code="D2")
    assert conv._open_elsewhere(item) is None                   # no opener configured
    conv._opener = lambda uid, url: {"uid": uid, "url": url}
    assert conv._open_elsewhere(item) == {"uid": "111", "url": item.url}


def test_open_in_browser_starts_at_canvas_borderless_launch_of_the_tool():
    conv, _ = _classmate([])
    conv.canvas.base_url = "https://canvas.wisc.edu"
    item = SimpleNamespace(url="https://canvas.wisc.edu/courses/5/assignments/9", course_id="5", id="9",
                           tool="https://sso.router.integration.prod.mheducation.com/v1/lms/ltiv1p3")
    page = conv._launch_page(item)
    assert page.startswith("https://canvas.wisc.edu/courses/5/external_tools/retrieve?")
    assert "assignment_id=9" in page and "display=borderless" in page and "mheducation.com" in page
    item.tool = ""
    assert conv._launch_page(item) == item.url                    # no tool known: the assignment page


# -- when something fails: say so, give a way out, tell the owner ----------------

def test_a_classmates_failure_says_the_owner_knows_and_offers_their_browser(monkeypatch):
    from ainl.util import trouble
    filed = []
    monkeypatch.setattr(trouble, "report", lambda source, **kw: filed.append((source, kw)))
    monkeypatch.setenv("AINL_TROUBLE", "1")
    conv, tg = _classmate([])
    sent = []
    conv.tg = SimpleNamespace(send=lambda chat, text, reply_markup=None: sent.append((text, reply_markup)))
    conv._pool.running_cdp_url = lambda uid: f"http://127.0.0.1:93{uid[-2:]}"
    conv._opener = lambda uid, url: {"inline_keyboard": [[{"text": "↗ Open in my browser"}]]}
    from ainl.bot.state import MenuItem
    conv.cs.pending = MenuItem(idx=1, course_id="1", id="2", kind="assignment", title="Problem Set 3",
                               course="PHYSICS247", types=["external_tool"], due="",
                               url="https://canvas.wisc.edu/courses/1/assignments/2", code="B1",
                               vendor="gradescope")

    conv._send("⚠️ Gradescope didn't answer in two minutes.\nNothing was uploaded.")
    text, kb = sent[-1]
    assert "told the bot's owner" in text and "own browser" in text
    assert kb == {"inline_keyboard": [[{"text": "↗ Open in my browser"}]]}
    [(source, kw)] = filed
    assert source == "script" and kw["chat"] == "111" and kw["where"] == "gradescope:B1"
    assert kw["cdp_url"] == "http://127.0.0.1:9311"          # their browser, never the owner's 9222

    conv._send("Here is what's due.")                        # not a failure: untouched, not filed
    assert sent[-1] == ("Here is what's due.", None) and len(filed) == 1


def test_a_failure_that_already_has_buttons_keeps_them():
    conv, _ = _classmate([])
    kb = {"inline_keyboard": [[{"text": "🔁 Try again"}]]}
    text, got = conv._way_out("⚠️ It failed.", kb)
    assert got is kb and "told the bot's owner" in text


def test_logged_out_is_not_dressed_up():
    conv, _ = _classmate([])
    assert conv._way_out(dispatch.RELOGIN, None) == (dispatch.RELOGIN, None)


def test_a_crash_is_filed_with_their_running_browser(tmp_path, monkeypatch):
    from ainl.util import trouble
    filed = []
    monkeypatch.setattr(trouble, "report", lambda source, **kw: filed.append((source, kw)))
    reg = Registry(base=tmp_path)
    reg.add("111", "Ann", "ann")
    reg.approve("111")

    class Boom:
        canvas = SimpleNamespace(close=lambda: None)
        _state_path = None

        def handle(self, u):
            raise KeyError("pending")

    tg = FakeTG()
    pool = SimpleNamespace(running_cdp_url=lambda uid: "http://127.0.0.1:9311")
    d = Dispatcher(reg, pool, tg, canvas_origin="https://canvas.wisc.edu",
                   connected=lambda cid: True, make_conversation=lambda cid: Boom())
    d.submit("111", _msg("111", "/todo"))
    _wait(lambda: filed)
    [(source, kw)] = filed
    assert source == "script" and kw["chat"] == "111" and kw["cdp_url"] == "http://127.0.0.1:9311"
    assert "told the bot's owner" in tg.sent[-1][1] and "Nothing was submitted" in tg.sent[-1][1]
