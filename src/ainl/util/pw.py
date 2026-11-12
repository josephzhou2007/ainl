"""One long-lived Playwright per thread.

`sync_playwright()` cannot be entered a second time in the same process once
the first has exited — the second start dies with EPIPE while the driver is
still being torn down. That was invisible while each process made a single
browser call, and guaranteed to bite the bot, which makes many.

So the driver is started once and kept. It is also faster: starting it costs
about a second, and that second was being paid on every call.

Thread-local because sync Playwright objects belong to the thread that made
them — the keepalive thread must not borrow the bot's.

    with connect() as browser:      # attached to the running window
        ...
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from .timing import span

log = logging.getLogger("ainl.pw")

CDP_URL = "http://127.0.0.1:9222"
_local = threading.local()


def playwright():
    """The Playwright instance for this thread, started on first use."""
    pw = getattr(_local, "pw", None)
    if pw is None:
        from playwright.sync_api import sync_playwright
        with span("pw.driver_start", "ours"):      # ~1 s, once per thread
            pw = _local.pw = sync_playwright().start()
        log.info("playwright driver started for thread %s", threading.current_thread().name)
    return pw


def stop() -> None:
    _local.browsers = {}
    pw = getattr(_local, "pw", None)
    if pw is not None:
        _local.pw = None
        try:
            pw.stop()
        except Exception:
            pass


@contextmanager
def connect(cdp_url: str = CDP_URL, timeout_ms: float = 8000):
    """Attach to the already-running browser, reusing one connection.

    The connection is cached and deliberately NOT closed on exit. Calling
    browser.close() on a CDP attachment leaves the driver unable to connect
    again — the window survives, but every later call reports "browser isn't
    running". Reconnecting only when is_connected() says so also saves the
    handshake on every call.
    """
    # Keyed by the browser's address as well as by thread: a thread that
    # serves several people (the main thread reading each one's Canvas
    # cookies) would otherwise hand the first person's browser to everyone.
    cache: dict = getattr(_local, "browsers", None)
    if cache is None:
        cache = _local.browsers = {}
    browser = cache.get(cdp_url)
    if browser is not None:
        try:
            if not browser.is_connected():
                browser = None
        except Exception:
            browser = None
    if browser is None:
        driver = playwright()
        with span("cdp.connect", "ours"):
            browser = driver.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
        cache[cdp_url] = browser
    try:
        yield browser
    except Exception:
        # A dead connection must not be handed to the next caller.
        try:
            if not browser.is_connected():
                cache.pop(cdp_url, None)
        except Exception:
            cache.pop(cdp_url, None)
        raise
