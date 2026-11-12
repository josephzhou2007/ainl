"""The school database: seeds, adding, searching, learned hosts, per-person search state."""
from __future__ import annotations

import sqlite3
import threading

import schooldb
import schools
from registry import Registry
from schooldb import SchoolDB


def test_seeds_keep_their_keys_and_are_verified(tmp_path):
    db = SchoolDB(base=tmp_path)
    assert db.path == tmp_path / "ainl.db"
    uw, ucsb = db.get("uw"), db.get("ucsb")
    assert (uw.canvas_origin, uw.start_path, uw.domains) == ("https://canvas.wisc.edu", "/", ("wisc.edu",))
    assert (ucsb.canvas_origin, ucsb.start_url) == ("https://ucsb.instructure.com",
                                                     "https://ucsb.instructure.com/login/saml")
    assert uw.status == ucsb.status == "verified" and uw.source == "seed"
    assert "duosecurity.com" in ucsb.allowed_suffixes and "ucsb.edu" in ucsb.allowed_suffixes
    assert db.by_origin("https://canvas.wisc.edu/").key == "uw"
    assert db.get("") is None and db.get("nope") is None


def test_reopening_never_duplicates_or_overwrites(tmp_path):
    db = SchoolDB(base=tmp_path)
    db.set_status("ucsb", "unverified")                  # the owner's change
    db.add(name="UCLA", canvas_origin="https://bruinlearn.ucla.edu", source="instructure",
           status="verified")
    again = SchoolDB(base=tmp_path)
    assert [s.key for s in again.all()] == ["uw", "ucsb", "bruinlearn-ucla"]
    assert again.get("ucsb").status == "unverified"


def test_existing_registry_keys_still_resolve(tmp_path):
    """Records written before the database (school "uw" / "ucsb") keep working."""
    reg = Registry(base=tmp_path)
    for cid, key in (("1", "uw"), ("2", "ucsb")):
        reg.add(cid, cid, cid)
        reg.set_school(cid, key)
    schools.use(SchoolDB(base=tmp_path))
    assert schools.get(reg.get("1").school).canvas_origin == "https://canvas.wisc.edu"
    assert schools.get(reg.get("2").school).canvas_origin == "https://ucsb.instructure.com"
    assert schools.get(schools.LEGACY).key == "uw"


def test_an_empty_database_file_from_before_gets_the_seed(tmp_path):
    sqlite3.connect(tmp_path / "ainl.db").close()        # exists, no tables
    assert SchoolDB(base=tmp_path).get("uw") is not None


def test_add_is_one_row_per_canvas_and_keys_stay_short_and_unique(tmp_path):
    db = SchoolDB(base=tmp_path)
    a = db.add(name="UCLA", canvas_origin="https://bruinlearn.ucla.edu/", status="verified",
               source="instructure", domains=("ucla.edu",), added_by="55")
    assert a.key == "bruinlearn-ucla" and a.domains == ("ucla.edu",)
    assert db.add(name="Other name", canvas_origin="https://BRUINLEARN.ucla.edu").key == a.key
    b = db.add(name="Fake UCSB", canvas_origin="https://ucsb.instructure.org")   # slug "ucsb" taken
    assert b.key == "ucsb-instructure" or b.key.startswith("ucsb")
    assert b.key != "ucsb" and b.status == "unverified" and b.source == "user_url"
    long = db.add(name="x", canvas_origin="https://" + "a" * 60 + ".example.edu")
    assert len(long.key) <= 32
    assert len(f"school:{long.key}".encode()) <= 64
    row = db.row(a.key)
    assert row["added_by"] == "55" and row["verified_at"] and row["created_at"]


def test_slugs():
    assert schooldb.slug_for("https://ucsb.instructure.com") == "ucsb"
    assert schooldb.slug_for("https://canvas.stanford.edu") == "canvas-stanford"


def test_local_search_matches_names_aliases_and_hosts(tmp_path):
    db = SchoolDB(base=tmp_path)
    assert [s.key for s in db.search("wisconsin")] == ["uw"]
    assert [s.key for s in db.search("UW–Madison")] == ["uw"]
    assert [s.key for s in db.search("santa barbara")] == ["ucsb"]
    assert [s.key for s in db.search("instructure")] == ["ucsb"]
    assert db.search("stanford") == [] and db.search("  ") == []
    db.add(name="Pasted UW thing", canvas_origin="https://canvas2.wisc.edu")   # unverified
    assert [s.key for s in db.search("wisc")] == ["uw", "canvas2-wisc"]        # verified first


def test_learned_hosts_are_counted_per_school(tmp_path):
    db = SchoolDB(base=tmp_path)
    for h in ("login.wisc.edu", "api-123.duosecurity.com", "login.wisc.edu", "LOGIN.WISC.EDU."):
        db.learn_host("uw", h)
    db.learn_host("uw", "not a host")
    db.learn_host("", "x.edu")
    assert db.hosts("uw") == [("login.wisc.edu", 3), ("api-123.duosecurity.com", 1)]
    assert db.hosts("ucsb") == []


def test_pending_search_expires_and_is_per_person(tmp_path, monkeypatch):
    db = SchoolDB(base=tmp_path)
    db.set_pending("55", "name")
    assert db.pending("55") == "name" and db.pending("66") is None
    db.set_pending("55", "url")
    assert db.pending("55") == "url"
    monkeypatch.setattr(schooldb, "SEARCH_TTL_S", -1)
    assert db.pending("55") is None
    monkeypatch.setattr(schooldb, "SEARCH_TTL_S", 900)
    db.clear_pending("55")
    assert db.pending("55") is None


def test_candidates_belong_to_the_chat_they_were_shown_in(tmp_path):
    db = SchoolDB(base=tmp_path)
    cid = db.put_candidate("55", "UCLA", "bruinlearn.ucla.edu", "saml")
    assert db.candidate(cid, "55")["domain"] == "bruinlearn.ucla.edu"
    assert db.candidate(cid, "66") is None
    db.set_pending("55")
    db.forget("55")
    assert db.candidate(cid, "55") is None and db.pending("55") is None


def test_many_threads_at_once(tmp_path):
    db = SchoolDB(base=tmp_path)
    errors = []

    def work(i):
        try:
            for j in range(20):
                db.learn_host("uw", f"h{i}.wisc.edu")
                db.set_pending(str(i), "name")
                db.add(name=f"S{i}", canvas_origin=f"https://s{i}.example.edu")
                db.search("wisc")
        except Exception as e:                           # pragma: no cover - the failure
            errors.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert sorted(v for _, v in db.hosts("uw")) == [20] * 8
    assert len(db.all()) == 2 + 8
