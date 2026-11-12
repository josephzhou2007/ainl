"""Finding a school in the bot: type a name -> buttons -> pick; or paste an address.

Fake Telegram, fake directory, fake detection: no network. Searches run inline
(``background=lambda fn: fn()``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import detect
import schools
from dispatch import Dispatcher
from onboarding import Onboarding
from registry import Registry

OWNER = "1"
LOGIN = "https://login.example.org/login"


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((str(chat_id), text, reply_markup))

    def answer_callback(self, cb_id, text=""):
        pass

    def to(self, chat_id):
        return [t for c, t, _ in self.sent if c == str(chat_id)]


def _msg(chat, text):
    return {"message": {"chat": {"id": int(chat), "type": "private"}, "text": text,
                        "from": {"first_name": "Ada"}}}


def _cb(chat, data):
    return {"callback_query": {"id": "x", "data": data, "message": {"chat": {"id": int(chat)}}}}


def _buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


class Net:
    """The fake outside world: Instructure's directory and the Canvas check."""

    def __init__(self, directory=None, canvases=None):
        self.directory_rows = directory or {}
        self.canvases = canvases or {}
        self.searched, self.detected = [], []

    def directory(self, term):
        self.searched.append(term)
        return self.directory_rows.get(term.lower(), [])

    def detect(self, address, hint=None):
        self.detected.append((address, hint))
        origin = detect.parse_origin(address)
        if origin not in self.canvases:
            raise detect.Rejected("That doesn't look like a Canvas site.")
        return self.canvases[origin]


UCLA = [{"name": "UCLA Extension", "domain": "my.uclaextension.edu", "authentication_provider": ""},
        {"name": "UCLA", "domain": "bruinlearn.ucla.edu", "authentication_provider": "saml"}]
BRUIN = detect.Detected("https://bruinlearn.ucla.edu", "/", ("ucla.edu",))


def _world(tmp_path, net=None, connected=lambda cid: False):
    reg, tg, net = Registry(base=tmp_path), FakeTG(), net or Net()
    ob = Onboarding(reg, tg, OWNER, login_url=LOGIN, connected=connected,
                    directory=net.directory, detect_school=net.detect, background=lambda fn: fn())
    reg.add("55", "Ada", "ada")
    reg.approve("55")
    return reg, tg, ob, net


def test_type_a_name_get_buttons_pick_one(tmp_path, school_db):
    net = Net({"ucla": UCLA}, {"https://bruinlearn.ucla.edu": BRUIN})
    reg, tg, ob, net = _world(tmp_path, net)
    ob.handle(_msg("55", "/school"))
    assert ob.pending_school_search("55") == "name"
    ob.handle(_msg("55", "UCLA"))
    chat, text, markup = tg.sent[-1]
    assert chat == "55" and "Tap your school" in text
    buttons = _buttons(markup)
    assert [b["text"] for b in buttons] == ["UCLA Extension · my.uclaextension.edu",
                                            "UCLA · bruinlearn.ucla.edu",
                                            "Not listed? Send your Canvas address"]
    for b in buttons:                                   # never a URL, always tiny
        assert len(b["callback_data"].encode()) <= 64 and "/" not in b["callback_data"]
    ob.handle(_cb("55", buttons[1]["callback_data"]))
    assert net.detected == [("bruinlearn.ucla.edu", "saml")]   # the directory's provider hint
    s = schools.get(reg.get("55").school)
    assert (s.name, s.canvas_origin, s.status, s.source) == (
        "UCLA", "https://bruinlearn.ucla.edu", "verified", "instructure")
    assert school_db.row(s.key)["added_by"] == "55"
    assert ob.pending_school_search("55") is None
    assert "Connect your Canvas" in tg.sent[-1][1] and "UCLA" in tg.sent[-1][1]
    # Next person: the school is known now, no second check.
    reg.add("66", "Bo", "bo")
    reg.approve("66")
    ob.handle(_msg("66", "/school"))
    ob.handle(_msg("66", "ucla"))
    datas = [b["callback_data"] for b in _buttons(tg.sent[-1][2])]
    assert f"school:{s.key}" == datas[0] and len(datas) == 3
    ob.handle(_cb("66", datas[0]))
    assert reg.get("66").school == s.key and len(net.detected) == 1


def test_local_and_directory_results_are_merged_without_duplicates(tmp_path):
    rows = [{"name": "University of Wisconsin - Madison", "domain": "canvas.wisc.edu"},
            {"name": "UW - Madison", "domain": "canvas.wisc.edu"},
            {"name": "Wisconsin Lutheran College", "domain": "wlc.instructure.com"}]
    reg, tg, ob, net = _world(tmp_path, Net({"wisconsin": rows}))
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "wisconsin"))
    buttons = _buttons(tg.sent[-1][2])
    assert [b["text"].split(" · ")[1] for b in buttons[:-1]] == ["canvas.wisc.edu", "wlc.instructure.com"]
    assert buttons[0]["callback_data"] == "school:uw"   # the seed, not a new row


def test_at_most_five_results(tmp_path):
    rows = [{"name": f"School {i}", "domain": f"s{i}.instructure.com"} for i in range(9)]
    reg, tg, ob, net = _world(tmp_path, Net({"school": rows}))
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "school"))
    assert len(_buttons(tg.sent[-1][2])) == 5 + 1


def test_nothing_found_offers_the_address_path(tmp_path):
    reg, tg, ob, net = _world(tmp_path)
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "Hogwarts"))
    chat, text, markup = tg.sent[-1]
    assert "found no school" in text and [b["callback_data"] for b in _buttons(markup)] == ["school:?"]
    assert ob.pending_school_search("55") == "name"        # they can type another name


def test_directory_failure_still_shows_local_results(tmp_path):
    reg, tg, ob, net = _world(tmp_path)

    def broken(term):
        raise RuntimeError("down")
    ob.directory = broken
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "santa barbara"))
    assert [b["callback_data"] for b in _buttons(tg.sent[-1][2])] == ["school:ucsb", "school:?"]


def test_a_stale_or_foreign_candidate_button_does_nothing(tmp_path, school_db):
    reg, tg, ob, net = _world(tmp_path, Net({"ucla": UCLA}, {"https://bruinlearn.ucla.edu": BRUIN}))
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "ucla"))
    data = _buttons(tg.sent[-1][2])[1]["callback_data"]
    reg.add("66", "Bo", "bo")
    reg.approve("66")
    ob.handle(_cb("66", data))                             # someone else's button
    ob.handle(_cb("55", "school:+999999"))                 # a row that doesn't exist
    ob.handle(_cb("55", "school:+abc"))
    assert net.detected == [] and reg.get("66").school == "" and reg.get("55").school == ""
    assert "out of date" in tg.to("55")[-1]


def test_a_directory_school_that_fails_the_check_is_not_added(tmp_path, school_db):
    reg, tg, ob, net = _world(tmp_path, Net({"ucla": UCLA}))      # no Canvas answers
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "ucla"))
    ob.handle(_cb("55", _buttons(tg.sent[-1][2])[0]["callback_data"]))
    assert "couldn't use" in tg.to("55")[-1] and reg.get("55").school == ""
    assert len(school_db.all()) == 2


def test_not_listed_then_paste_an_address(tmp_path, school_db):
    canvas = detect.Detected("https://canvas.smallcollege.edu", "/login/saml", ("smallcollege.edu",))
    reg, tg, ob, net = _world(tmp_path, Net(canvases={"https://canvas.smallcollege.edu": canvas}))
    ob.handle(_msg("55", "/school"))
    ob.handle(_cb("55", "school:?"))
    assert "Send the address" in tg.to("55")[-1] and ob.pending_school_search("55") == "url"
    ob.handle(_msg("55", "https://canvas.smallcollege.edu/courses/12?x=y"))
    s = schools.get(reg.get("55").school)
    assert (s.canvas_origin, s.start_path, s.status, s.source) == (
        "https://canvas.smallcollege.edu", "/login/saml", "unverified", "user_url")
    note = tg.to(OWNER)[-1]
    assert "Ada" in note and "canvas.smallcollege.edu" in note and "unverified" in note
    assert f"/verify {s.key}" in note
    assert net.searched == ["canvas.smallcollege.edu"]      # checked the directory for its name


def test_a_pasted_address_found_in_the_directory_is_verified(tmp_path):
    rows = {"bruinlearn.ucla.edu": [UCLA[1]]}
    reg, tg, ob, net = _world(tmp_path, Net(rows, {"https://ucla.instructure.com": BRUIN}))
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "ucla.instructure.com"))          # an address typed as a "name"
    s = schools.get(reg.get("55").school)
    assert (s.name, s.canvas_origin, s.status) == ("UCLA", "https://bruinlearn.ucla.edu", "verified")
    assert not any("unverified" in t for t in tg.to(OWNER))


@pytest.mark.parametrize("address", ["http://canvas.wisc.edu", "https://127.0.0.1", "localhost:8080",
                                     "https://www.wisc.edu"])
def test_bad_or_non_canvas_addresses_store_nothing(tmp_path, school_db, address):
    reg, tg, ob, net = _world(tmp_path)
    ob.handle(_msg("55", "/school"))
    ob.handle(_cb("55", "school:?"))
    ob.handle(_msg("55", address))
    assert "couldn't use" in tg.to("55")[-1]
    assert reg.get("55").school == "" and len(school_db.all()) == 2
    assert ob.pending_school_search("55") == "url"         # they can send another one
    assert tg.to(OWNER) == []


def test_a_known_address_needs_no_check(tmp_path):
    reg, tg, ob, net = _world(tmp_path)
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "canvas.wisc.edu"))
    assert reg.get("55").school == "uw" and net.detected == []


def test_names_that_look_a_bit_like_addresses_are_still_searched(tmp_path):
    reg, tg, ob, net = _world(tmp_path)
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "St. Olaf"))
    ob.handle(_msg("55", "st.olaf"))
    assert net.searched == ["St. Olaf", "st.olaf"] and net.detected == []


def test_another_command_drops_the_question(tmp_path):
    reg, tg, ob, net = _world(tmp_path)
    reg.set_school("55", "uw")                             # (with no school, /login asks again)
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "/login"))
    assert ob.pending_school_search("55") is None
    ob.handle(_msg("55", "UCLA"))                          # now just a message
    assert net.searched == []


def test_unapproved_people_never_search(tmp_path, school_db):
    reg, tg, ob, net = _world(tmp_path)
    reg.add("77", "Cy", "cy")
    school_db.set_pending("77", "name")                    # even if a row were there
    assert ob.pending_school_search("77") is None
    ob.handle(_msg("77", "UCLA"))
    ob.handle(_cb("77", "school:?"))
    assert net.searched == [] and "Not approved" in tg.to("77")[-1]


def test_forget_clears_the_search_state(tmp_path, school_db):
    reg, tg, ob, net = _world(tmp_path)
    ob.handle(_msg("55", "/school"))
    ob.handle(_msg("55", "/forget"))
    assert school_db.pending("55") is None


def test_a_connected_persons_answer_to_school_skips_their_assistant(tmp_path):
    """Their messages normally go to their homework assistant; right after
    /school the next text is the school search."""
    reg, tg, ob, net = _world(tmp_path, connected=lambda cid: True)
    d = Dispatcher(reg, pool=None, tg=tg, canvas_origin="https://canvas.wisc.edu",
                   connected=lambda cid: True, make_conversation=lambda cid: None,
                   owns_text=ob.pending_school_search)
    assert d.wants(_msg("55", "hello"))
    assert not d.wants(_msg("55", "/school"))
    ob.handle(_msg("55", "/school"))
    assert "logs you out" in tg.to("55")[-1]
    assert not d.wants(_msg("55", "santa barbara"))       # -> onboarding
    assert d.wants(_msg("55", "/todo"))                    # commands still go to the assistant
    assert not d.wants(_cb("55", "school:ucsb"))
    ob.handle(_msg("55", "santa barbara"))
    ob.handle(_cb("55", "school:ucsb"))
    assert reg.get("55").school == "ucsb" and d.wants(_msg("55", "hello"))


def test_owner_schools_listing_and_verify(tmp_path, school_db):
    reg, tg, ob, net = _world(tmp_path)
    reg.set_school("55", "ucsb")
    s = school_db.add(name="canvas.x.edu", canvas_origin="https://canvas.x.edu")
    school_db.learn_host("ucsb", "passport.identity.ucsb.edu")
    out = ob.admin_command("/schools")
    assert "UC Santa Barbara" in out and "1 people" in out and "passport.identity.ucsb.edu" in out
    assert "⚠️ unverified" in out and s.key in out
    assert "verified" in ob.admin_command("/verify", s.key)
    assert school_db.get(s.key).status == "verified"
    assert "Usage" in ob.admin_command("/verify", "nope")
    assert "UC Santa Barbara" in ob.admin_command("/users")


def test_legacy_school_callbacks_still_work(tmp_path):
    reg, tg, ob, net = _world(tmp_path)
    ob.handle(_cb("55", "school:ucsb"))                     # an old keyboard from before
    assert reg.get("55").school == "ucsb"


def test_sessions_are_made_lazily_per_origin(tmp_path, school_db):
    made = []

    class FakeManager:
        def __init__(self, reg, browser, store, origin):
            made.append(origin)
            self.origin = origin
    reg = Registry(base=tmp_path)
    who = {"a": "uw", "b": "ucsb", "c": "uw"}
    ss = schools.SchoolSessions.__new__(schools.SchoolSessions)
    import threading
    ss._registry, ss._make, ss._browser, ss._store = reg, FakeManager, None, None
    ss._by_origin, ss._lock = {}, threading.Lock()
    ss._school_of = lambda uid: schools.get(who.get(uid))
    assert made == []
    new = school_db.add(name="New", canvas_origin="https://canvas.new.edu")
    who["d"] = new.key                                      # a school added while running
    assert [ss.origin(u) for u in "abcd"] == ["https://canvas.wisc.edu", "https://ucsb.instructure.com",
                                              "https://canvas.wisc.edu", "https://canvas.new.edu"]
    assert made == ["https://canvas.wisc.edu", "https://ucsb.instructure.com", "https://canvas.new.edu"]
