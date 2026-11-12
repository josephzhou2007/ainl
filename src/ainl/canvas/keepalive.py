"""Keep the Canvas session alive, so it never has to be re-created.

A Canvas session dies from being left alone, not from age — every request
extends it and re-issues the cookie. The old failure ("session missing or
expired", 46 hours stale) happened because nothing touched it between one
sitting and the next.

So a background thread makes one cheap authenticated GET every N minutes and
writes the rotated cookie back to disk. That is enough to keep the browser
window, the saved snapshot and the bot all holding the same live session.

It never opens a browser and never asks for a password: if the session is
already gone, only the student can fix that, and the bot tells them when they
next use it. This only preserves a session that is still alive.

    start(base_url, minutes=30)      # from the bot, once
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("ainl.keepalive")

DEFAULT_MINUTES = 30.0
_thread: Optional[threading.Thread] = None


def ping(base_url: str) -> bool:
    """One authenticated GET using the freshest cookies available. True if alive."""
    from .session import BrowserSession, auth_from_live_browser

    sess = BrowserSession(base_url)
    auth = auth_from_live_browser(base_url) or sess.load()
    if auth is None:
        return False
    from ._http import CanvasHTTP

    http = CanvasHTTP(base_url, auth)
    try:
        http.get("/api/v1/users/self")
    except Exception:
        return False
    else:
        # Persist whatever Canvas rotated during that request, so the snapshot
        # on disk stays usable even if the browser window is closed later.
        try:
            sess.save_cookies(http.cookies)
        except Exception:
            pass
        return True
    finally:
        http.close()


def observe(alive: bool, told: bool, on_gone: Optional[Callable[[], None]]) -> bool:
    """One check's outcome -> whether the student has been told. Tells once per
    loss: again only after the session has been alive in between."""
    if alive:
        return False
    if not told and on_gone is not None:
        try:
            on_gone()
        except Exception:
            log.exception("keepalive on_gone failed")
    return True


def start(base_url: str, minutes: float = DEFAULT_MINUTES, *,
          on_gone: Optional[Callable[[], None]] = None,
          first_after_s: float = 60.0) -> None:
    """Run ping() forever on a daemon thread. Safe to call once per process.

    ``on_gone`` is called once when the session goes from alive (or unknown,
    at start) to gone — the moment to put the login button in front of the
    student instead of waiting for their next command to fail. It is called
    again only after the session has been alive in between. The first check
    runs ``first_after_s`` after start, so a bot started on a dead session
    says so within a minute."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return

    def loop() -> None:
        told = False
        wait = first_after_s
        while True:
            time.sleep(wait)
            wait = minutes * 60
            try:
                alive = ping(base_url)
            except Exception as e:                    # never take the bot down
                log.warning("keepalive error: %s", str(e)[:120])
                continue
            log.info("keepalive: session %s", "alive" if alive else "GONE")
            told = observe(alive, told, on_gone)

    _thread = threading.Thread(target=loop, name="canvas-keepalive", daemon=True)
    _thread.start()
    log.info("keepalive every %g min", minutes)
