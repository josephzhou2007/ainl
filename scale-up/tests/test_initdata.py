"""Telegram initData: only a signature made with OUR bot token names a user."""
from __future__ import annotations

import json

import pytest

import initdata
from initdata import InitDataError, sign, verify

TOKEN = "123456:TEST-token"
NOW = 1_800_000_000


def _fields(uid=4242, auth_date=NOW, **extra):
    return {"auth_date": str(auth_date), "query_id": "AAE",
            "user": json.dumps({"id": uid, "first_name": "Ada"}), **extra}


def test_a_valid_signature_gives_the_user_id():
    assert verify(sign(_fields(), TOKEN), TOKEN, clock=lambda: NOW + 5) == "4242"


def test_signature_field_is_accepted_either_way():
    # Newer clients add ``signature``; sign() puts it inside the check string.
    data = sign(_fields(signature="abc"), TOKEN)
    assert verify(data, TOKEN, clock=lambda: NOW) == "4242"
    # ...and outside it, the other reading of the docs.
    data = sign(_fields(), TOKEN) + "&signature=abc"
    assert verify(data, TOKEN, clock=lambda: NOW) == "4242"


def test_another_bots_token_is_rejected():
    with pytest.raises(InitDataError):
        verify(sign(_fields(), "999:other"), TOKEN, clock=lambda: NOW)


def test_changing_the_user_breaks_the_signature():
    data = sign(_fields(uid=4242), TOKEN)
    forged = data.replace("4242", "4243")
    with pytest.raises(InitDataError):
        verify(forged, TOKEN, clock=lambda: NOW)


def test_stale_initdata_is_rejected():
    data = sign(_fields(), TOKEN)
    with pytest.raises(InitDataError):
        verify(data, TOKEN, clock=lambda: NOW + initdata.MAX_AGE_S + 1)


def test_future_dated_initdata_is_rejected():
    with pytest.raises(InitDataError):
        verify(sign(_fields(auth_date=NOW + 3600), TOKEN), TOKEN, clock=lambda: NOW)


@pytest.mark.parametrize("data", ["", "garbage", "hash=", "a=1&hash=00"])
def test_junk_is_rejected(data):
    assert initdata.user_of(data, TOKEN) is None


def test_no_token_configured_means_nobody():
    assert initdata.user_of(sign(_fields(), TOKEN), "") is None


def test_a_bot_or_missing_user_is_not_a_person():
    f = _fields()
    f["user"] = json.dumps({"id": "4242"})             # a string id: not what Telegram sends
    with pytest.raises(InitDataError):
        verify(sign(f, TOKEN), TOKEN, clock=lambda: NOW)
    f.pop("user")
    with pytest.raises(InitDataError):
        verify(sign(f, TOKEN), TOKEN, clock=lambda: NOW)
