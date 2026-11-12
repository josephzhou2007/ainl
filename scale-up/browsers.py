"""One headless browser per user, started on demand.

The single-user bot attaches to one window on port 9222 (scripts/browser_start.py,
src/ainl/util/pw.py). To serve a classroom on one laptop, each student needs
their OWN Canvas cookies, which means their own --user-data-dir, which means
their own browser on its own debugging port. WebAssign already takes a
``cdp_url``, so "per user" reduces to "per port + per profile".

This pool maps chat_id -> a running headless Chromium on its own port, using
that user's directory from the registry as the profile. It starts one lazily
the first time someone needs it, hands back the cdp_url, and reaps browsers
nobody has used for a while — stopping ONLY that user's process (matched by its
profile path on the command line, the same discipline as
browser_start._kill_profile_holders), never every Chrome on the machine.

The launcher, the liveness probe and the stopper are injected (defaulting to
the real ones that reuse the browser_start recipe), so the whole pool is unit-
tested with fakes: no real Chrome, no real ports, no subprocesses, no threads.

Per-user Playwright workers live in util/browserthread.py (``using(key)``).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("ainl.scaleup.browsers")

# The single-user browser owns this one. Never hand it to a pooled user.
RESERVED_PORT = 9222
DEFAULT_BASE_PORT = 9300

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _browser_start():
    """Import scripts/browser_start lazily, so tests with fakes never need it.

    Importing it is side-effect-free (its launch lives behind ``if __name__``),
    it just gives us the vetted FLAGS list, the Chromium finder and the ``alive``
    probe — reused rather than duplicated.
    """
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import browser_start          # type: ignore
    return browser_start


# --- the real implementations (defaults; swapped for fakes in tests) -------- #

def _real_launch(port: int, user_data_dir: Path):
    """Launch a detached headless Chromium on ``port`` with ``user_data_dir``.

    Same recipe as browser_start.main(): the shared FLAGS, the headless "look"
    (new headless, sized window, a real UA for this OS so the page doesn't see
    HeadlessChrome), detached so it outlives this process — on any OS.
    """
    bs = _browser_start()
    # A browser left over from a crashed bot may still hold this profile; a
    # second Chrome on the same profile just hands off to it and exits.
    bs.kill_profile_holders(user_data_dir)
    # about:blank, not Chrome's new-tab page: that one pulls Google's content
    # into a remote iframe and costs renderers for nothing (2026-09-26).
    return bs.spawn(bs.command(user_data_dir, port, url="about:blank"), detach=True)


def _real_tidy(cdp_http: str) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from ainl.util.tidy import tidy
    return tidy(cdp_http)


def _real_scan() -> list[tuple[int, str]]:
    """(port, --user-data-dir) of every running Chromium main process
    (renderers etc. carry --type= and are skipped)."""
    import re
    import subprocess
    out = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        if "--remote-debugging-port=" not in line or "--type=" in line:
            continue
        port = re.search(r"--remote-debugging-port=(\d+)", line)
        prof = re.search(r"--user-data-dir=(\S+)", line)
        if port and prof:
            found.append((int(port.group(1)), prof.group(1)))
    return found


def _real_alive(port: int) -> bool:
    return _browser_start().alive(port)


def _real_stop(port: int, user_data_dir: Path) -> None:
    """Stop ONLY the browser on this user's profile.

    Every user's profile is named "chrome-profile", so a name match would kill
    everyone. browser_start.kill_profile_holders matches the FULL profile path
    on the command line, which is unique per user — and never the process name.
    """
    _browser_start().kill_profile_holders(user_data_dir)


@dataclass
class _Entry:
    chat_id: str
    port: int
    profile: Path
    last_used: float
    handle: object = None
    tidied: bool = False       # back to one blank tab since it was last used


class BrowserPool:
    """chat_id -> a running per-user headless browser."""

    def __init__(
        self,
        registry,                                   # scale_up.registry.Registry
        base_port: int = DEFAULT_BASE_PORT,
        *,
        launch: Callable[[int, Path], object] = _real_launch,
        alive: Callable[[int], bool] = _real_alive,
        stop: Callable[[int, Path], None] = _real_stop,
        tidy: Callable[[str], int] = _real_tidy,
        clock: Callable[[], float] = time.monotonic,
        start_timeout: float = 20.0,
        poll_interval: float = 1.0,
        on_start: Optional[Callable[[str], None]] = None,
        checkpoint: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._reg = registry
        self._base_port = base_port
        self._launch = launch
        self._alive = alive
        self._stop = stop
        self._tidy = tidy
        self._clock = clock
        self._start_timeout = start_timeout
        self._poll = poll_interval
        # Called with the chat_id after a FRESH start, outside the pool lock:
        # a new browser has lost the session cookies, and putting them back
        # (Cookie Management's restore) goes through cdp_url() again.
        self._on_start = on_start
        # Called with (chat_id, port) while the browser still runs, just before
        # it is stopped or tidied: the moment to save what a stop would lose
        # (sitecookies.py). Must not call back into the pool.
        self._checkpoint = checkpoint
        self._browsers: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._reaper: Optional[threading.Thread] = None
        self._reaper_stop = threading.Event()

    # -- ports ---------------------------------------------------------------

    def _alloc_port(self) -> int:
        """First free port from the base, skipping held ports, 9222, and any
        port something is already listening on.

        Deterministic and collision-free across the users currently held: the
        first user lands on the base, the next on the base+1, and a port freed
        by reaping is reused by the next start. The listening check matters
        after a crash: another person's orphaned browser may still sit on a
        port, and handing that out would show them someone else's session.
        """
        held = {e.port for e in self._browsers.values()}
        port = self._base_port
        while port in held or port == RESERVED_PORT or self._alive(port):
            port += 1
        return port

    # -- lifecycle -----------------------------------------------------------

    def cdp_url(self, chat_id: str) -> str:
        """Return ``http://127.0.0.1:<port>`` for this user, starting it if needed."""
        started = False
        with self._lock:
            entry = self._browsers.get(chat_id)
            if entry is None:
                entry = self._start(chat_id)
                started = True
            elif not self._alive(entry.port):
                # Died on us (crash, external kill). Bring it back, same port.
                log.info("user %s browser gone from port %s — restarting",
                         chat_id, entry.port)
                entry = self._start(chat_id, port=entry.port)
                started = True
            entry.last_used = self._clock()
            entry.tidied = False
            url = f"http://127.0.0.1:{entry.port}"
        if started:
            self._started(chat_id)
        return url

    def running_cdp_url(self, chat_id: str) -> Optional[str]:
        """Their browser's address if it is already running — never starts one
        (a failure's scene is what is on screen, not a fresh blank browser)."""
        with self._lock:
            entry = self._browsers.get(chat_id)
            return f"http://127.0.0.1:{entry.port}" if entry is not None else None

    def _started(self, chat_id: str) -> None:
        if self._on_start is None:
            return
        try:
            self._on_start(chat_id)
        except Exception:                       # a failed restore must not fail the caller
            log.exception("on_start hook failed for user %s", chat_id)

    def _save(self, chat_id: str, port: int) -> None:
        if self._checkpoint is None:
            return
        try:
            self._checkpoint(chat_id, port)
        except Exception:
            log.exception("checkpoint failed for user %s", chat_id)

    def port_of(self, chat_id: str) -> Optional[int]:
        with self._lock:
            e = self._browsers.get(chat_id)
            return e.port if e else None

    def running(self, chat_id: str) -> bool:
        with self._lock:
            return chat_id in self._browsers

    def stats(self) -> dict[str, dict]:
        """chat_id -> {port, idle_s} for every browser held. Read-only, for monitor.py."""
        now = self._clock()
        with self._lock:
            return {cid: {"port": e.port, "idle_s": round(now - e.last_used)}
                    for cid, e in self._browsers.items()}

    def _start(self, chat_id: str, port: Optional[int] = None) -> _Entry:
        profile = self._reg.user_dir(chat_id) / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        port = port if port is not None else self._alloc_port()
        log.info("starting browser for user %s on port %s", chat_id, port)
        handle = self._launch(port, profile)

        deadline = time.monotonic() + self._start_timeout
        while not self._alive(port):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"browser for user {chat_id} did not come up on port {port}")
            time.sleep(self._poll)

        entry = _Entry(chat_id=chat_id, port=port, profile=profile,
                       last_used=self._clock(), handle=handle)
        self._browsers[chat_id] = entry
        return entry

    def restart(self, chat_id: str) -> bool:
        """Stop and relaunch ONE person's browser, same port.

        What WebAssign's watchdog calls when a page wedges. The single-user
        default would restart the browser on 9222 — someone else's, here.
        The person's Cengage session dies with the process (it is session-only)
        and is rebuilt over LTI on the next use, exactly as for the single bot.
        """
        with self._lock:
            entry = self._browsers.pop(chat_id, None)
            if entry is None:
                return False
            self._save(chat_id, entry.port)
            self._stop(entry.port, entry.profile)
            time.sleep(self._poll)              # let the port go before reusing it
            self._start(chat_id, port=entry.port)
        self._started(chat_id)
        return True

    def stop(self, chat_id: str) -> bool:
        """Stop ONE person's browser (``/forget`` does this before deleting
        their profile). Returns whether one was running."""
        with self._lock:
            entry = self._browsers.pop(chat_id, None)
            if entry is None:
                return False
            self._stop(entry.port, entry.profile)       # /forget: nothing is kept
            log.info("stopped browser for user %s (port %s)", chat_id, entry.port)
            return True

    def adopt(self, scan: Callable[[], list] = _real_scan) -> list[str]:
        """Take back the browsers a previous run of this process started.

        The pool lives in memory: after a bot restart every classmate's browser
        was still running but nobody's — never tidied, never reaped, holding
        ~500 MB each until the next reboot (2026-09-26). Adopting them keeps
        their Canvas and WebAssign sessions alive across a bot restart, which
        stopping them would not. Only a browser on a registered person's OWN
        profile path is taken, on a port that answers."""
        users = {str(self._reg.user_dir(u.chat_id) / "chrome-profile"): u.chat_id
                 for u in self._reg.all()}
        taken: list[str] = []
        with self._lock:
            for port, profile in scan():
                cid = users.get(profile.rstrip("/"))
                if (cid is None or cid in self._browsers or port == RESERVED_PORT
                        or any(e.port == port for e in self._browsers.values())
                        or not self._alive(port)):
                    continue
                self._browsers[cid] = _Entry(chat_id=cid, port=port, profile=Path(profile),
                                             last_used=self._clock())
                taken.append(cid)
                log.info("adopted the running browser for user %s (port %s)", cid, port)
        return taken

    def touch(self, chat_id: str) -> None:
        """This person's browser is in use right now (their assistant calls it
        around every message). Without it, "idle" meant "nobody asked for the
        address lately" — and WebAssign asks once, then works for as long as
        it likes: a 30-minute session was reaped mid-work."""
        with self._lock:
            entry = self._browsers.get(chat_id)
            if entry is not None:
                entry.last_used = self._clock()
                entry.tidied = False

    def tidy_idle(self, after_s: float = 600,
                  busy: Callable[[str], bool] = lambda cid: False) -> list[str]:
        """Close the tabs of browsers idle for ``after_s`` (and not busy), once
        per idle spell: every open page is a renderer running its scripts.
        The browser, its cookies and its sessions stay."""
        now = self._clock()
        with self._lock:
            due = [(cid, e.port) for cid, e in self._browsers.items()
                   if not e.tidied and now - e.last_used >= after_s]
        done: list[str] = []
        for cid, port in due:
            if busy(cid):
                continue
            self._save(cid, port)
            try:
                n = self._tidy(f"http://127.0.0.1:{port}")
            except Exception as e:
                log.warning("tidy for user %s failed: %s", cid, str(e)[:80])
                continue
            with self._lock:
                entry = self._browsers.get(cid)
                if entry is not None and entry.port == port:
                    entry.tidied = True
            done.append(cid)
            if n:
                log.info("tidied browser for user %s: closed %d tab(s)", cid, n)
        return done

    def tidy_now(self, chat_id: str) -> bool:
        """Close this person's tabs right away — they are between steps (the
        bot waits for their photo or file) and the next step reopens what it
        needs. Their logins stay; their idle clock is not touched."""
        with self._lock:
            entry = self._browsers.get(chat_id)
            port = entry.port if entry else None
        if port is None:
            return False
        self._save(chat_id, port)
        try:
            n = self._tidy(f"http://127.0.0.1:{port}")
        except Exception as e:
            log.info("tidy_now for user %s failed: %s", chat_id, str(e)[:80])
            return False
        with self._lock:
            entry = self._browsers.get(chat_id)
            if entry is not None and entry.port == port:
                entry.tidied = True
        if n:
            log.info("released %d tab(s) for user %s while they work", n, chat_id)
        return True

    def reap(self, idle_seconds: float = 1800,
             busy: Callable[[str], bool] = lambda cid: False) -> list[str]:
        """Stop browsers unused for ``idle_seconds``. Returns the chat_ids reaped."""
        now = self._clock()
        reaped: list[str] = []
        with self._lock:
            for chat_id, entry in list(self._browsers.items()):
                if now - entry.last_used >= idle_seconds and not busy(chat_id):
                    self._save(chat_id, entry.port)
                    self._stop(entry.port, entry.profile)
                    del self._browsers[chat_id]
                    reaped.append(chat_id)
                    log.info("reaped idle browser for user %s (port %s)",
                             chat_id, entry.port)
        return reaped

    def shutdown_all(self) -> None:
        with self._lock:
            for entry in self._browsers.values():
                self._save(entry.chat_id, entry.port)
                self._stop(entry.port, entry.profile)
            self._browsers.clear()

    # -- optional background reaper (never started by tests) -----------------

    def start_reaper(self, interval: float = 60, idle_seconds=1800,
                     tidy_after: float = 600,
                     busy: Callable[[str], bool] = lambda cid: False) -> None:
        """Every ``interval`` seconds: tidy browsers idle ``tidy_after``, stop
        those idle ``idle_seconds``. ``busy(uid)`` = someone is using it now.

        Optional and injectable-off: the pool works fully without it, and the
        unit tests call reap() directly rather than spawning a thread.
        """
        if self._reaper is not None:
            return
        self._reaper_stop.clear()

        def _loop() -> None:
            while not self._reaper_stop.wait(interval):
                try:
                    self.tidy_idle(tidy_after, busy)
                    # A number, or a callable: T can follow the data (bot.py).
                    self.reap(idle_seconds() if callable(idle_seconds) else idle_seconds, busy)
                except Exception:               # a reap failure must not kill the loop
                    log.exception("reaper pass failed")

        self._reaper = threading.Thread(target=_loop, name="browser-reaper",
                                        daemon=True)
        self._reaper.start()

    def stop_reaper(self) -> None:
        self._reaper_stop.set()
        self._reaper = None
