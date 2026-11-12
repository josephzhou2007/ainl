"""Which of the two channels talks to Canvas, and who decides.

  token   the school-approved access token. Primary. Documented REST +
          GraphQL (measured usable: canvas-official/PROBE.md).
  cookie  the browser session obtained through SAML + Duo. Fallback. Survives
          the token being revoked or its scopes narrowed, at the cost of a
          ~10h lifetime and a human being present to re-login.

The preference lives in ~/.ainl/channel.json so it outlives a bot restart and
can be flipped from Telegram (/channel) without touching the host. "auto" means
token first, cookie on failure — which is the whole point of having two.
"""
from __future__ import annotations

import json
import logging
from typing import Literal, Optional

from ..config import CanvasConfig, home

log = logging.getLogger("ainl.canvas.channel")

Pref = Literal["auto", "token", "cookie"]
VALID: tuple[Pref, ...] = ("auto", "token", "cookie")
_FILE = "channel.json"


def preference() -> Pref:
    try:
        p = json.loads((home() / _FILE).read_text(encoding="utf-8")).get("prefer")
    except (OSError, ValueError):
        return "auto"
    return p if p in VALID else "auto"


def set_preference(pref: Pref) -> Pref:
    if pref not in VALID:
        raise ValueError(f"channel must be one of {', '.join(VALID)}")
    (home() / _FILE).write_text(json.dumps({"prefer": pref}), encoding="utf-8")
    return pref


def open_source(prefer: Optional[Pref] = None, *, interactive: bool = False):
    """Build an ApiSource on the preferred channel.

    "auto" and "token" both start at the token; only "auto" is allowed to fall
    back, because a student who explicitly pinned "token" is usually trying to
    find out whether the token still works.
    """
    from .api import ApiSource  # local: api imports this module back

    prefer = prefer or preference()
    cfg = CanvasConfig.from_env()

    if prefer in ("auto", "token") and cfg.token:
        from .auth import BearerAuth
        return ApiSource(cfg.base_url, BearerAuth(cfg.token))
    if prefer == "token" and not cfg.token:
        raise ConfigMissing("channel is pinned to 'token' but CANVAS_TOKEN is not set")

    from .session import (BrowserSession, auth_from_live_browser,  # lazy: playwright
                          open_canvas_login)

    sess = BrowserSession(cfg.base_url)
    # Prefer the window that is open now over the snapshot on disk: the snapshot
    # is only as fresh as the last save, the window as fresh as the last click.
    live = auth_from_live_browser(cfg.base_url)
    if live is not None and sess.probe(live):
        log.info("cookie channel: using the live browser window")
        return ApiSource(cfg.base_url, live, session=sess)
    try:
        return ApiSource(cfg.base_url, sess.ensure(interactive=interactive), session=sess)
    except Exception as e:
        raise NeedsCanvasLogin(
            "The Canvas browser session has expired and there's no live one to use.\n"
            "In the browser window that's already open, go to\n"
            f"  {cfg.base_url}\n"
            "and sign in (NetID + Duo). Then send /channel cookie again.\n"
            "Nothing else is needed — I read the cookies from that window."
        ) from e


class ConfigMissing(RuntimeError):
    pass


class NeedsCanvasLogin(RuntimeError):
    """The cookie channel has no usable session and a human has to sign in."""
