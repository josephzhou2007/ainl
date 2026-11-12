"""The intro video: once per person, uploaded once, then sent by file_id."""
import json

import pytest

from intro import IntroVideo, MARKER


class FakeTg:
    def __init__(self, refuse_file_id=False):
        self.calls = []
        self.refuse_file_id = refuse_file_id

    def send_video(self, chat_id, *, path=None, file_id="", caption="", **meta):
        self.calls.append((chat_id, "path" if path else "file_id"))
        if file_id and self.refuse_file_id:
            raise RuntimeError("wrong file identifier")
        return len(self.calls), file_id or "FID-1"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("AINL_INTRO_VIDEO", raising=False)
    monkeypatch.setattr("intro._probe", lambda p: {})
    (tmp_path / "intro.mp4").write_bytes(b"\0" * 1000)

    def user_dir(uid):
        d = tmp_path / "users" / uid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def make(tg):
        return IntroVideo(tg, user_dir, home=tmp_path, background=lambda fn: fn())
    return tmp_path, make


def test_first_person_uploads_the_next_gets_the_file_id(world):
    home, make = world
    tg = FakeTg()
    iv = make(tg)
    iv.send_once("1")
    iv.send_once("2")
    assert tg.calls == [("1", "path"), ("2", "file_id")]
    assert json.loads((home / "intro_video.json").read_text())["file_id"] == "FID-1"


def test_each_person_gets_it_once(world):
    home, make = world
    tg = FakeTg()
    make(tg).send_once("1")
    make(tg).send_once("1")                  # a second login, even after a restart
    assert tg.calls == [("1", "path")]
    assert (home / "users" / "1" / MARKER).exists()


def test_a_forgotten_person_gets_it_again(world):
    home, make = world
    tg = FakeTg()
    iv = make(tg)
    iv.send_once("1")
    import shutil
    shutil.rmtree(home / "users" / "1")      # /forget deletes their directory
    iv.send_once("1")
    assert tg.calls == [("1", "path"), ("1", "file_id")]


def test_a_refused_file_id_falls_back_to_uploading(world):
    home, make = world
    (home / "intro_video.json").write_text("{}")
    tg = FakeTg(refuse_file_id=True)
    iv = make(tg)
    iv.send_once("1")                        # uploads, caches FID-1
    iv.send_once("2")                        # FID-1 refused -> upload again
    assert tg.calls == [("1", "path"), ("2", "file_id"), ("2", "path")]
    assert (home / "users" / "2" / MARKER).exists()


def test_a_replaced_video_is_uploaded_again(world):
    home, make = world
    tg = FakeTg()
    iv = make(tg)
    iv.send_once("1")
    (home / "intro.mp4").write_bytes(b"\0" * 2000)   # a new cut: different size
    iv.send_once("2")
    assert tg.calls == [("1", "path"), ("2", "path")]


def test_a_failed_send_is_retried_at_the_next_login(world):
    home, make = world

    class Down(FakeTg):
        def send_video(self, *a, **k):
            raise RuntimeError("network")
    iv = make(Down())
    iv.send_once("1")
    assert not (home / "users" / "1" / MARKER).exists()
    iv.tg = FakeTg()
    iv.send_once("1")
    assert iv.tg.calls == [("1", "path")]


def test_no_video_file_means_off(world):
    home, make = world
    (home / "intro.mp4").unlink()
    tg = FakeTg()
    iv = make(tg)
    assert not iv.enabled
    iv.send_once("1")
    assert tg.calls == []
