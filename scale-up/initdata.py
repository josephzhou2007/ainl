"""Who opened the Canvas login page: Telegram's signed ``initData``.

When someone taps "Canvas login" in the public bot, Telegram opens our page
inside the app and hands it ``Telegram.WebApp.initData`` — a query string
signed with a key only Telegram and this bot share. The page forwards it
verbatim; this module checks the signature and returns the Telegram user id.
That id is the ONLY way the viewer learns whose browser to show: nothing in the
URL, nothing the page claims about itself.

    secret     = HMAC_SHA256(key="WebAppData", msg=bot_token)
    check      = "\\n".join(f"{k}={v}" for k, v in sorted(fields) if k != "hash")
    valid iff  hex(HMAC_SHA256(key=secret, msg=check)) == hash

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Newer clients also send ``signature`` (an Ed25519 signature for third
parties). The docs are ambiguous about whether it belongs in the check string
for the HMAC, so both forms are tried; either one still requires the bot token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Callable, Optional
from urllib.parse import parse_qsl

# Long enough to read the page and start typing; short enough that a copied
# initData is useless soon after. The viewer session has its own, shorter cap.
MAX_AGE_S = 15 * 60


class InitDataError(ValueError):
    """initData is missing, forged, stale or not about a person."""


def _check_string(fields: dict[str, str], drop: tuple[str, ...]) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(fields.items()) if k not in drop)


def verify(init_data: str, bot_token: str, *, max_age_s: float = MAX_AGE_S,
           clock: Callable[[], float] = time.time) -> str:
    """The Telegram user id this initData was signed for, or InitDataError."""
    if not init_data or not bot_token:
        raise InitDataError("no initData")
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise InitDataError("initData is not a query string") from None
    got = fields.get("hash", "")
    if not got:
        raise InitDataError("initData has no hash")

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    ok = False
    for drop in (("hash",), ("hash", "signature")):
        want = hmac.new(secret, _check_string(fields, drop).encode(), hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(want, got) or ok
    if not ok:
        raise InitDataError("initData signature does not match")

    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError:
        raise InitDataError("initData has no auth_date") from None
    age = clock() - auth_date
    if age > max_age_s or age < -60:           # a minute of clock skew, no more
        raise InitDataError("initData is stale")

    try:
        user = json.loads(fields.get("user") or "")
        uid = user["id"]
    except (ValueError, TypeError, KeyError):
        raise InitDataError("initData has no user") from None
    if type(uid) is not int or uid <= 0:
        raise InitDataError("initData user id is not a person")
    return str(uid)


def sign(fields: dict[str, str], bot_token: str) -> str:
    """Build a valid initData string — for tests and the local dev page only."""
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, _check_string(fields, ("hash",)).encode(), hashlib.sha256).hexdigest()
    from urllib.parse import urlencode
    return urlencode({**fields, "hash": h})


def user_of(init_data: str, bot_token: str, **kw) -> Optional[str]:
    """verify(), but None instead of an exception."""
    try:
        return verify(init_data, bot_token, **kw)
    except InitDataError:
        return None
