"""Registry: everything under a tmp_path base, no global env, no real ~/.ainl."""
from __future__ import annotations

import pytest

from registry import Registry, User, user_dir


def test_add_is_unapproved_then_approve(tmp_path):
    reg = Registry(base=tmp_path)
    reg.add("111", "Alice", "alice@wisc.edu")

    u = reg.get("111")
    assert isinstance(u, User)
    assert u.name == "Alice" and u.login_id == "alice@wisc.edu"
    assert u.approved is False
    assert reg.is_approved("111") is False
    assert u.created_at                       # stamped

    assert reg.approve("111") is True
    assert reg.is_approved("111") is True


def test_all_lists_every_user(tmp_path):
    reg = Registry(base=tmp_path)
    reg.add("111", "Alice", "a")
    reg.add("222", "Bob", "b")
    ids = {u.chat_id for u in reg.all()}
    assert ids == {"111", "222"}


def test_get_missing_returns_none(tmp_path):
    reg = Registry(base=tmp_path)
    assert reg.get("999") is None
    assert reg.is_approved("999") is False
    assert reg.approve("999") is False        # nothing to approve


def test_remove_deletes_the_user_dir(tmp_path):
    reg = Registry(base=tmp_path)
    reg.add("111", "Alice", "a")
    d = reg.user_dir("111")
    (d / "marker.txt").write_text("hi", encoding="utf-8")
    assert d.exists()

    assert reg.remove("111") is True
    assert not d.exists()                      # whole tree gone
    assert reg.get("111") is None
    assert reg.remove("111") is False          # idempotent


def test_persists_across_instances(tmp_path):
    Registry(base=tmp_path).add("111", "Alice", "a")
    reg2 = Registry(base=tmp_path)             # fresh load from disk
    assert reg2.get("111") is not None


def test_corrupt_json_tolerated(tmp_path):
    (tmp_path / "users").mkdir()
    (tmp_path / "users" / "registry.json").write_text("{ not json", encoding="utf-8")
    reg = Registry(base=tmp_path)              # must not raise
    assert reg.all() == []
    reg.add("111", "Alice", "a")               # and is still usable
    assert reg.get("111") is not None


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "..", ".", "", "  "])
def test_sanitization_rejects_path_tricks(tmp_path, bad):
    reg = Registry(base=tmp_path)
    with pytest.raises(ValueError):
        user_dir(bad, base=tmp_path)
    with pytest.raises(ValueError):
        reg.add(bad, "x", "y")


def test_user_dir_stays_under_base(tmp_path):
    d = user_dir("-100123", base=tmp_path)     # negative group id is fine
    assert d.exists()
    assert (tmp_path / "users") in d.parents
