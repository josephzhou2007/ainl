"""The school list, and one session manager per school picked per person."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import schools
from registry import Registry


def test_catalog_is_well_formed():
    for s in schools.SCHOOLS.values():
        assert s.canvas_origin.startswith("https://") and not s.canvas_origin.endswith("/")
        assert s.start_path.startswith("/") and s.start_url.startswith(s.canvas_origin)
        assert "duosecurity.com" in s.allowed_suffixes
    assert schools.get("ucsb").start_url == "https://ucsb.instructure.com/login/saml"
    assert schools.by_origin("https://canvas.wisc.edu/").key == "uw"
    assert schools.get(schools.LEGACY) is not None and schools.get("") is None


def test_each_person_gets_their_schools_session_manager(tmp_path):
    reg = Registry(base=tmp_path)
    for cid, key in (("111", "uw"), ("222", "ucsb"), ("333", "")):
        reg.add(cid, cid, cid)
        reg.approve(cid)
        reg.set_school(cid, key)
    ss = schools.SchoolSessions(reg, pool=SimpleNamespace(), key=Fernet.generate_key(),
                                school_of=lambda uid: schools.get(reg.get(uid).school))
    assert ss.origin("111") == "https://canvas.wisc.edu"
    assert ss.origin("222") == "https://ucsb.instructure.com"
    with pytest.raises(LookupError):
        ss.inspect("333")                                # no school: nothing to check
