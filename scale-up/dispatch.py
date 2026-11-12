"""Each connected classmate gets their own homework assistant, on their own Canvas.

The owner's private bot runs ONE ``Conversation`` on the owner's token and the
owner's browser (9222). Here every approved person who has done "Canvas login"
gets their own Conversation, and everything that would default to the owner is
replaced:

    Canvas API    cookies read from THEIR pooled browser (never CANVAS_TOKEN)
    WebAssign     THEIR browser, and a restart restarts only theirs
    Gradescope    THEIR browser
    worker        browserthread.using(their id): nobody queues behind anybody
    state         <users>/<id>/bot_state.json
    /auto, /sweep off — a model never writes a classmate's work
    owner tools   /channel /stats /timing /math are not theirs to use

When Canvas logs them out, the assistant re-reads their browser once and
otherwise asks them to tap Canvas login again. It never falls back to another
channel: the only other channel on this machine is the owner's.

Updates arrive on the public bot's single getUpdates loop and are handed to a
per-person worker thread, so one person's slow WebAssign page never delays
anyone else's reply.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("ainl.scaleup.dispatch")

# Commands that stay with onboarding even for connected people.
ONBOARDING_COMMANDS = ("/login", "/token", "/forget", "/school")
# The owner's tools: what they report or change is the owner's, not theirs.
OWNER_ONLY = ("/auto", "/sweep", "/stats", "/channel", "/timing", "/math")

HELP = (
    "<b>Your Canvas assistant</b>\n\n"
    "/todo — today → next 4 days, one block per course (A, B, C…)\n"
    "/todo 14 — look further ahead\n"
    "reply a code like C2 — that opens it. Everything starts from a code.\n"
    "  ✍️ 📎 💬  Canvas work: send text / a file / a link and I submit it, after you confirm\n"
    "  🧮  WebAssign: I send the questions; tap the multiple-choice ones, write\n"
    "      the rest on one sheet and send a photo\n"
    "  ↗  somewhere I can't work yet — you get the link\n\n"
    "/resume — WebAssign work you set aside with ⏸ Later\n"
    "/check — after /todo: I try every item and report what I can and can't do yet "
    "(fills nothing, hands in nothing)\n"
    "/cancel — drop the current item\n"
    "/restart — clear everything in flight (use it if I go quiet)\n"
    "/login — sign in to Canvas again if it logged you out\n"
    "/forget — delete everything about you\n\n"
    "I only hand in what you send, and only after you confirm. I never write answers."
)

# () -> the "🔐 Canvas login" keyboard, or None. Set by bot.py once the login
# page is up (onboarding.login_button): every "logged out" message carries the
# button itself, so logging back in is one tap, not a command to remember.
login_keyboard: Callable[[], Optional[dict]] = lambda: None


def _relogin_kb() -> Optional[dict]:
    try:
        return login_keyboard()
    except Exception:
        return None


RELOGIN = ("⚠️ Canvas logged you out. Tap <b>Canvas login</b> (or send /login) and sign in "
           "again — nothing was submitted.")


def _conversation_class():
    from ainl.bot.conversation import Conversation

    class ClassmateConversation(Conversation):
        """Conversation with every owner default swapped for this person's own."""

        def __init__(self, *a, pool, uid: str, reauth: Callable[[], Optional[object]],
                     opener: Optional[Callable[[str, str], Optional[dict]]] = None, **kw):
            super().__init__(*a, **kw)
            self._pool, self._uid, self._reauth, self._opener = pool, uid, reauth, opener

        def _open_elsewhere(self, item):
            if self._opener is None or not item.url:
                return None
            return self._opener(self._uid, self._launch_page(item))

        def _way_out(self, text, keyboard):
            """Say plainly that the owner knows, and — when they were working
            on an item — give them the page itself, to finish by hand."""
            if text.startswith(RELOGIN[:30]):
                return text, keyboard          # the way out is logging in, already said
            text += "\n\n<i>I saved what the page showed and told the bot's owner.</i>"
            item = self.cs.pending
            if keyboard is None and item is not None:
                try:
                    kb = self._open_elsewhere(item)
                except Exception:
                    kb = None
                if kb:
                    text += "\nTo finish it yourself now, open it in your own browser:"
                    keyboard = kb
            return text, keyboard

        def _trouble_cdp(self):
            return getattr(self._pool, "running_cdp_url", lambda c: None)(self._uid)

        def _launch_page(self, item) -> str:
            """Where their browser should start: Canvas's borderless launch of
            the tool — the platform itself, top level, no Canvas page and no
            iframe around it (an iframe is what makes McGraw Hill reach for a
            new window). Without a known tool URL, the assignment page."""
            from urllib.parse import urlencode
            if not getattr(item, "tool", "") or not item.course_id or not item.id:
                return item.url
            return (f"{self.canvas.base_url}/courses/{item.course_id}/external_tools/retrieve?"
                    + urlencode({"assignment_id": item.id, "url": item.tool, "display": "borderless"}))

        def _wa(self):
            if self._webassign is None:
                from ainl.external.webassign import WebAssign
                self._webassign = WebAssign(cdp_url=self._pool.cdp_url(self._uid),
                                            launch_url=self._webassign_launch_url,
                                            restart=lambda: self._pool.restart(self._uid))
            return self._webassign

        def _gs(self):
            if self._gradescope is None:
                from ainl.external.gradescope import Gradescope
                self._gradescope = Gradescope(cdp_url=self._pool.cdp_url(self._uid))
            return self._gradescope

        def _fall_back(self, exc) -> bool:
            """Re-read their browser's cookies once; else ask them to log in again.
            Never ``recover_auth``: its fallback is the owner's channel."""
            new = self._reauth()
            if new is not None:
                self._swap(new)
                self._send("Canvas session refreshed — say that again.")
            else:
                self._send(RELOGIN, _relogin_kb())
            return True

        def _channel(self, arg: str) -> None:
            self._send("That one is only for the bot's owner.")

        def _command(self, cmd: str, arg: str = "") -> None:
            if cmd in ("/start", "/help"):
                return self._send(HELP)
            return super()._command(cmd, arg)

    return ClassmateConversation


class Dispatcher:
    """chat id -> that person's Conversation, fed by a per-person worker thread."""

    def __init__(self, reg, pool, tg, *, canvas_origin, connected: Callable[[str], bool],
                 make_conversation: Optional[Callable[[str], object]] = None,
                 open_in_browser: Optional[Callable[[str, str], Optional[dict]]] = None,
                 owns_text: Callable[[str], object] = lambda cid: None) -> None:
        """``canvas_origin``: one origin for everyone, or ``uid -> origin | None``
        (their school's Canvas; None = no school chosen). ``owns_text(cid)``:
        truthy while onboarding is waiting for their typed school name (/school)."""
        self.reg, self.pool, self.tg = reg, pool, tg
        self._origin = canvas_origin if callable(canvas_origin) else (lambda uid: canvas_origin)
        self.connected = connected
        self._make = make_conversation or self._build
        self.open_in_browser = open_in_browser   # (uid, url) -> keyboard, set by bot.py
        self.owns_text = owns_text
        self._convs: dict[str, object] = {}
        self._queues: dict[str, "queue.Queue"] = {}
        self._working: set[str] = set()        # handling an update right now
        self._lock = threading.Lock()

    # -- routing (called on the getUpdates thread) -----------------------------

    def wants(self, update: dict) -> bool:
        """Is this update for a connected person's assistant (not onboarding)?"""
        cb = update.get("callback_query")
        m = update.get("message") or (cb or {}).get("message") or {}
        chat = m.get("chat") or {}
        if chat.get("type") not in (None, "private"):
            return False
        cid = str(chat.get("id"))
        try:
            if not (self.reg.is_approved(cid) and self.connected(cid)):
                return False
        except ValueError:                     # not a safe id: not ours
            return False
        if cb is not None:
            return not (cb.get("data") or "").startswith(("reg:", "school:"))
        text = (m.get("text") or "").strip()
        cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        if not cmd and text and self.owns_text(cid):
            return False                       # the answer to /school: a school search
        return cmd not in ONBOARDING_COMMANDS

    def submit(self, cid: str, update: dict) -> None:
        with self._lock:
            q = self._queues.get(cid)
            if q is None:
                q = self._queues[cid] = queue.Queue()
                threading.Thread(target=self._loop, args=(cid, q), name=f"person-{cid}",
                                 daemon=True).start()
        q.put(update)

    def drop(self, cid: str) -> None:
        """Forget their assistant (after /forget, or a fresh login): the next
        message builds a new one on their current cookies."""
        with self._lock:
            conv = self._convs.pop(cid, None)
            q = self._queues.pop(cid, None)
        if q is not None:
            q.put(None)                        # ends that worker thread
        if conv is not None:
            try:
                conv.canvas.close()
            except Exception:
                pass

    def busy(self, cid: str) -> bool:
        """Is their assistant working, or has it work waiting? The pool must
        not tidy or stop their browser under it."""
        with self._lock:
            q = self._queues.get(cid)
            return cid in self._working or (q is not None and q.qsize() > 0)

    def _release_if_waiting(self, cid: str, conv) -> None:
        """The bot now waits for them (a photo, a file): close their tabs now
        instead of after the idle timer. No guessing whether they are still
        working — the conversation knows the next move is theirs."""
        tidy_now = getattr(self.pool, "tidy_now", None)
        waiting = getattr(conv, "waiting_for_student", None)
        if tidy_now is None or waiting is None:
            return
        try:
            if waiting():
                tidy_now(cid)
        except Exception:
            log.exception("releasing tabs for %s failed", cid)

    def _touch(self, cid: str) -> None:
        touch = getattr(self.pool, "touch", None)
        if touch is not None:
            try:
                touch(cid)
            except Exception:
                pass

    def stats(self) -> dict[str, dict]:
        """chat id -> {assistant: built?, queue: updates waiting}. Read-only, for monitor.py."""
        with self._lock:
            ids = set(self._convs) | set(self._queues)
            return {cid: {"assistant": cid in self._convs,
                          "queue": self._queues[cid].qsize() if cid in self._queues else 0}
                    for cid in ids}

    # -- the per-person worker -------------------------------------------------

    def _loop(self, cid: str, q: "queue.Queue") -> None:
        from ainl.util import browserthread, metrics, trouble

        metrics.bind(uid=cid)                  # this thread is theirs: its Canvas calls count for them
        while True:
            update = q.get()
            if update is None:
                return
            text = (((update.get("message") or {}).get("text")) or "").strip().lower()
            cmd = text.split()[0].split("@")[0] if text.startswith("/") else ""
            try:
                if cmd in OWNER_ONLY:
                    self._say(cid, "That one is only for the bot's owner.")
                    continue
                with self._lock:
                    self._working.add(cid)
                self._touch(cid)                 # in use: not idle, not to be tidied
                with browserthread.using(cid):
                    conv = self._conversation(cid)
                    if conv is None:
                        continue
                    conv.handle(update)
                    self._save(conv)
                    self._release_if_waiting(cid, conv)
            except Exception:
                log.exception("update for %s failed", cid)
                metrics.emit("error", where="assistant")
                msg = "❌ Something broke on my side. Nothing was submitted. Try /todo again."
                self._say(cid, msg + "\n\n<i>I saved what the page showed and told the bot's owner.</i>")
                trouble.report("script", chat=cid, where="assistant crash", message=msg,
                               cdp_url=getattr(self.pool, "running_cdp_url", lambda c: None)(cid))
            finally:
                with self._lock:
                    self._working.discard(cid)
                self._touch(cid)                 # idle time counts from the END of the work

    def _conversation(self, cid: str):
        with self._lock:
            conv = self._convs.get(cid)
        if conv is None:
            conv = self._make(cid)
            if conv is None:
                self._say(cid, RELOGIN, _relogin_kb())
                return None
            with self._lock:
                self._convs[cid] = conv
        return conv

    def _save(self, conv) -> None:
        path = getattr(conv, "_state_path", None)
        if path:
            with conv._lock:
                conv.state.save(path)

    def _say(self, cid: str, text: str, keyboard: Optional[dict] = None) -> None:
        try:
            self.tg.send(cid, text, reply_markup=keyboard)
        except Exception as e:
            log.warning("send to %s failed: %s", cid, str(e)[:80])

    # -- building one (real) ----------------------------------------------------

    def _canvas(self, cid: str):
        """An ApiSource on THEIR browser's Canvas cookies, or None if logged out.
        Runs on their worker (the caller is inside browserthread.using(cid))."""
        from ainl.canvas import ApiSource
        from ainl.canvas.session import auth_from_live_browser
        from ainl.util import browserthread

        host_url = (self._origin(cid) or "").rstrip("/")
        if not host_url:
            return None
        cdp = self.pool.cdp_url(cid)
        auth = browserthread.run(lambda: auth_from_live_browser(host_url, cdp), budget=40)
        if auth is None:
            return None
        src = ApiSource(host_url, auth)
        try:
            src.whoami()
        except Exception:
            src.close()
            return None
        return src

    def _build(self, cid: str):
        from ainl.bot.state import BotState

        canvas = self._canvas(cid)
        if canvas is None:
            return None
        state_path = self.reg.user_dir(cid) / "bot_state.json"
        tz = "America/Chicago"
        try:
            tz = canvas.whoami().get("time_zone") or tz
        except Exception:
            pass
        cls = _conversation_class()
        conv = cls(canvas, self.tg, cid, BotState.load(state_path), tz=tz,
                   autosolve=False,               # a model never writes a classmate's work
                   pool=self.pool, uid=cid, reauth=lambda: self._canvas(cid),
                   opener=lambda uid, url: (self.open_in_browser(uid, url)
                                            if self.open_in_browser else None))
        conv._state_path = state_path
        log.info("assistant ready for %s", cid)
        return conv


class Heartbeat:
    """Every ``minutes``, check each RUNNING classmate browser's Canvas session
    and save its rotated cookies. A browser the pool has reaped is left alone
    (restored on its next start). Someone logged out is told once."""

    def __init__(self, reg, pool, sessions, tg, *, connected, minutes: float = 30,
                 on_logged_out: Optional[Callable[[str], None]] = None) -> None:
        self.reg, self.pool, self.sessions, self.tg = reg, pool, sessions, tg
        self.connected, self.minutes = connected, minutes
        self.on_logged_out = on_logged_out
        self._told: set[str] = set()
        self._stop = threading.Event()

    def tick(self) -> None:
        for u in self.reg.all():
            cid = u.chat_id
            if not (u.approved and self.connected(cid) and self.pool.running(cid)):
                continue
            try:
                st = self.sessions.heartbeat(cid)
            except Exception:
                log.exception("heartbeat for %s failed", cid)
                continue
            state = getattr(st.state, "value", str(st.state))
            if state == "active":
                self._told.discard(cid)
            elif state == "reauth_required" and cid not in self._told:
                self._told.add(cid)
                if self.on_logged_out:
                    self.on_logged_out(cid)
                try:
                    kb = _relogin_kb()
                    self.tg.send(cid, RELOGIN, reply_markup=kb)
                except Exception:
                    pass

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.minutes * 60):
                self.tick()
        threading.Thread(target=loop, name="classmate-heartbeat", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
