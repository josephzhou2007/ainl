"""The PUBLIC bot — a second bot, separate from the owner's own.

The owner's homework bot (python -m ainl.bot) stays private and untouched.
This one, under its own BotFather token, is the door for everyone else:
registration, approval, tokens — and later each person's own Conversation.
The owner watches applications in THIS bot's chat window, so admin traffic
never lands next to their own homework.

    setx TELEGRAM_PUBLIC_BOT_TOKEN "<a second bot from @BotFather>"
    python scale-up/bot.py

Canvas login (viewer.py) runs in THIS process, on its own thread: it has to
share the registry and the browser pool with the rest of the bot. It needs

    AINL_SESSION_KEY   a Fernet key (encrypts each person's Canvas session)
    AINL_LOGIN_URL     the public HTTPS page, e.g. https://example.com/native-bot-learning/login
                       (the part before /login becomes the viewer's path prefix)
                       (Cloudflare Tunnel -> 127.0.0.1:AINL_VIEWER_PORT, default 8790)

Without both, everything else works and approved people are told the login
step isn't open yet.

Owner = TELEGRAM_CHAT_ID, the same person: a private chat id is the user's
id, identical across bots.

/monitor (owner only) opens the live page — memory, processes, people — served
by the same viewer under <base>/monitor; /monitor text is the same as a message.
The owner is also told when memory crosses 80% / 90% (monitor.py).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from browsers import BrowserPool                         # noqa: E402
from onboarding import Onboarding                        # noqa: E402
from registry import Registry                            # noqa: E402
import schools                                           # noqa: E402
import tokens                                            # noqa: E402

from ainl.bot.state import BotState                      # noqa: E402
from ainl.config import home                             # noqa: E402
from ainl.telegram import TelegramClient                 # noqa: E402

log = logging.getLogger("ainl.scaleup.bot")


def _chat_of(update: dict) -> str:
    m = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return str((m.get("chat") or {}).get("id"))


def _what(update: dict) -> str:
    """What kind of update, for metrics — a command name at most, never text."""
    msg = update.get("message") or {}
    text = msg.get("text") or ""
    if update.get("callback_query"):
        return "tap"
    if text.startswith("/"):
        return text.split()[0].split("@")[0][:20]
    return "file" if (msg.get("photo") or msg.get("document")) else "text"


def _monitor_url(login_url: str) -> str:
    return login_url.rsplit("/login", 1)[0] + "/monitor"


def _session_services(reg, pool, school_of):
    """Cookie Management, one SessionManager per school; None when there is no key."""
    key = os.environ.get("AINL_SESSION_KEY", "").strip()
    if not key:
        return None
    from schools import SchoolSessions
    return SchoolSessions(reg, pool, key.encode("ascii"), school_of)


def _owner_login(owner_id: str):
    """The login page for the OWNER, opened from the private bot's button when
    its Canvas session expired: it streams the owner's own browser (9222). The
    private bot then reads the new cookies from that browser by itself; here we
    only check Canvas knows who this is and tell the owner in that chat.
    None when the private bot's token is not in the environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not owner_id:
        return None
    from viewer import OwnerLogin
    from ainl.config import CanvasConfig
    from ainl.telegram.client import TelegramClient, esc
    try:
        origin = CanvasConfig.from_env().base_url.rstrip("/")
    except Exception:
        origin = schools.SCHOOLS[schools.LEGACY].canvas_origin
    school = next((sc for sc in schools.all() if sc.canvas_origin.rstrip("/") == origin), None)
    cdp = "http://127.0.0.1:9222"

    def check() -> "str | None":
        from ainl.canvas._http import CanvasHTTP
        from ainl.canvas.session import auth_from_live_browser
        auth = auth_from_live_browser(origin, cdp)
        if auth is None:
            return None
        http = CanvasHTTP(origin, auth)
        try:
            me = http.get("/api/v1/users/self")
            return (me.get("name") or me.get("short_name") or "you") if isinstance(me, dict) else "you"
        except Exception:
            return None
        finally:
            http.close()

    def on_login(name: str) -> None:
        private = TelegramClient(token)
        try:
            private.send(owner_id, f"✅ Canvas login done as <b>{esc(name)}</b>. "
                                   "Say that again, or send /todo.")
        finally:
            private.close()

    kw = {"allowed_suffixes": school.allowed_suffixes} if school is not None else {}
    return OwnerLogin(bot_token=token, cdp_url=cdp, canvas_origin=origin,
                      check=check, on_login=on_login, **kw)


def _start_login(reg, pool, tg, token: str, owner_id: str, school_of,
                 after_login=lambda uid: None, monitor=None, on_open=None):
    """(viewer, login_url, sessions) — viewer/login_url None if not configured."""
    from ainl.telegram.client import esc
    services = None
    try:
        services = _session_services(reg, pool, school_of)
    except Exception as e:
        log.warning("Canvas session store unavailable (%s): Canvas login is off", type(e).__name__)
    login_url = os.environ.get("AINL_LOGIN_URL", "").strip()
    if services is None or not login_url.startswith("https://"):
        log.warning("Canvas login is off: set AINL_SESSION_KEY and an https AINL_LOGIN_URL")
        return None, None, services

    def connected(uid: str, name: str) -> None:
        after_login(uid)
        tg.send(uid, f"✅ Canvas connected as <b>{esc(name)}</b>. Your session is saved, "
                     "encrypted; /forget deletes it.\n\nSend /todo to see what's due.")
        u = reg.get(uid)
        school = school_of(uid)
        tg.send(owner_id, f"🔐 {esc(u.name if u else uid)} connected Canvas"
                          f"{' (' + esc(school.name) + ')' if school else ''}.")

    def visited(uid: str, host: str) -> None:
        # Learn each school's real SSO chain from the logins people do.
        school = school_of(uid)
        if school is not None:
            schools.db().learn_host(school.key, host)

    from urllib.parse import urlsplit
    from viewer import LoginServer
    parts = urlsplit(login_url)
    owner = _owner_login(owner_id)
    viewer = LoginServer(
        bot_token=token,
        canvas_origin=schools.SCHOOLS[schools.LEGACY].canvas_origin,
        allowed=reg.is_approved,
        cdp_url=pool.cdp_url,
        sessions=services,
        school_of=school_of,
        on_visit=visited,
        on_open=on_open,
        on_connected=connected,
        public_origin=f"{parts.scheme}://{parts.netloc}",
        # https://example.com/native-bot-learning/login -> serve under /native-bot-learning
        base_path=parts.path.rsplit("/login", 1)[0] if parts.path.endswith("/login") else "",
        port=int(os.environ.get("AINL_VIEWER_PORT") or 8790),
        monitor=monitor.json if monitor is not None else None,
        owner_id=owner_id,
        owner=owner,
    )
    log.info("owner login from the private bot: %s", "on" if owner is not None else "off")
    viewer.start_in_thread()
    return viewer, login_url, services


def _site_cookies(reg, school_of):
    """Website session cookies kept across browser stops; None without a key."""
    key = os.environ.get("AINL_SESSION_KEY", "").strip()
    if not key:
        return None
    from urllib.parse import urlsplit
    from sitecookies import SiteCookies
    return SiteCookies(reg, key.encode("ascii"), canvas_host_of=lambda uid: urlsplit(
        getattr(school_of(uid), "canvas_origin", "") or "").hostname or "")


def _stop_after(default_min: float = 30, lo_min: float = 10, hi_min: float = 60):
    """T — how long an idle browser is kept before it is stopped — from the data.

    People's pauses between messages have peaks (tapping through a list;
    working a sheet; gone). The valley after the first peak is the cut
    (scripts/gap_report.py). Until there are enough pauses to trust it, the
    safe default holds; then the valley, clamped to [lo, hi], recomputed hourly.
    AINL_STOP_MIN pins it."""
    pinned = os.environ.get("AINL_STOP_MIN", "").strip()
    if pinned:
        return float(pinned) * 60
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import gap_report
    state = {"at": 0.0, "t": default_min * 60}

    def t() -> float:
        if time.time() - state["at"] < 3600:
            return state["t"]
        state["at"] = time.time()
        try:
            gaps = gap_report.gaps_from(home() / "transcript.jsonl")
            new = default_min * 60
            if len(gaps) >= gap_report.ENOUGH:
                cut = gap_report.valley(gap_report.histogram(gaps))
                if cut is not None:
                    new = min(max(gap_report.EDGES[cut + 1], lo_min), hi_min) * 60
            if new != state["t"]:
                log.info("idle stop T: %.0f -> %.0f min (%d pauses)", state["t"] / 60, new / 60, len(gaps))
            state["t"] = new
        except Exception:
            log.exception("could not recompute T; keeping %.0f min", state["t"] / 60)
        return state["t"]
    return t


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_PUBLIC_BOT_TOKEN", "").strip()
    owner_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not owner_id:
        print("TELEGRAM_PUBLIC_BOT_TOKEN (a second bot from @BotFather) and TELEGRAM_CHAT_ID "
              "must be set. This is NOT the private homework bot's token.", file=sys.stderr)
        return 2
    if token == os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        print("TELEGRAM_PUBLIC_BOT_TOKEN is the same as TELEGRAM_BOT_TOKEN — the public bot "
              "must be a different bot, or the two would fight over getUpdates.", file=sys.stderr)
        return 2

    from ainl.util import metrics
    metrics.tag(bot="public")                         # tells its records from the private bot's
    state_path = home() / "public_bot_state.json"
    state = BotState.load(state_path)                 # only its update offset is used here
    tg = TelegramClient(token)
    reg = Registry()
    holder: dict = {}

    def school_of(uid: str):
        u = reg.get(uid)
        return schools.get(u.school) if u else None

    site = _site_cookies(reg, school_of)

    def checkpoint(uid: str, port: int) -> None:
        # About to stop or tidy: keep the website sessions a stop would lose.
        if site is not None and reg.is_approved(uid):
            n = site.save(uid, port)
            if n:
                log.info("saved %d site session cookie(s) for user %s", n, uid)

    def restore(uid: str) -> None:
        # A fresh browser has lost every session cookie. Websites first
        # (WebAssign & co: no re-launch from Canvas needed), then Canvas,
        # whose restore checks the account before it trusts anything.
        if site is not None and reg.is_approved(uid):
            port = pool.port_of(uid)
            n = site.restore(uid, port) if port else 0
            if n:
                log.info("restored %d site session cookie(s) for user %s", n, uid)
        if holder.get("services") is not None and reg.is_approved(uid) and school_of(uid):
            st = holder["services"].restore(uid)
            log.info("restore for user %s: %s", uid, st.state.value)

    def connected(uid: str) -> bool:
        return (reg.user_dir(uid) / "canvas-session.enc").exists() if reg.get(uid) else False

    pool = BrowserPool(reg, on_start=restore, checkpoint=checkpoint)
    pool.adopt()                  # browsers from before a restart are still ours
    # Each connected classmate's own assistant (their Canvas, their browser).
    from dispatch import Dispatcher, Heartbeat
    # Everyone who connected before there was a school choice did so on UW's Canvas.
    for u in reg.all():
        if not u.school and connected(u.chat_id):
            reg.set_school(u.chat_id, schools.LEGACY)
    dispatcher = Dispatcher(reg, pool, tg, connected=connected,
                            canvas_origin=lambda uid: getattr(school_of(uid), "canvas_origin", None),
                            # After /school, their next text is the school's name.
                            owns_text=lambda uid: onboarding.pending_school_search(uid))
    import monitor as mon
    monitor = mon.Monitor(reg, pool, dispatcher, connected=connected, school_of=school_of)
    # A fresh login: rebuild their assistant on the new cookies.
    # The intro video goes out while they sign in: the login takes ~20 s, the
    # upload fits inside it, and it is waiting in the chat when they return.
    from intro import IntroVideo
    intro = IntroVideo(tg, reg.user_dir, home=home())
    log.info("intro video: %s", intro.path if intro.enabled else "off (no file)")
    viewer, login_url, services = _start_login(reg, pool, tg, token, owner_id, school_of,
                                               after_login=dispatcher.drop, monitor=monitor,
                                               on_open=intro.send_once)
    monitor.viewer = viewer
    alerts_stop = mon.start_alerts(monitor, lambda text: tg.send(owner_id, text))
    # Any failure a classmate meets: its scene is filed and the owner is told at once.
    from ainl.util import trouble
    if owner_id:
        trouble.set_notifier(trouble.telegram_notifier(tg, owner_id), owner=owner_id)
    holder["services"] = services
    def browser_busy(uid: str) -> bool:
        # Their assistant is working, or their Canvas login page is open.
        return dispatcher.busy(uid) or (viewer is not None and uid in viewer.stats())

    # Tidy a browser 10 min after its last use (close its tabs: each is a
    # renderer running scripts); stop it after 30.
    pool.start_reaper(tidy_after=float(os.environ.get("AINL_TIDY_MIN") or 10) * 60,
                      idle_seconds=_stop_after(), busy=browser_busy)
    if viewer is not None and login_url:
        # Platforms Canvas won't give a one-time link to from a browser login:
        # open the item in their own signed-in browser, streamed like the login.
        dispatcher.open_in_browser = lambda uid, url: {"inline_keyboard": [[{
            "text": "↗ Open in my browser",
            "web_app": {"url": f"{login_url}?open={viewer.issue_open(uid, url)}"}}]]}
    heartbeat = None
    if services is not None:
        heartbeat = Heartbeat(reg, pool, services, tg, connected=connected,
                              minutes=float(os.environ.get("AINL_KEEPALIVE_MIN") or 30),
                              on_logged_out=dispatcher.drop)
        heartbeat.start()

    def forget(uid: str) -> None:
        dispatcher.drop(uid)
        if viewer is not None:
            viewer.kick(uid)
        pool.stop(uid)
        from ainl.util import browserthread
        browserthread.reset(uid)

    def school_change(uid: str) -> None:
        # A different school is a different Canvas: the old session must go.
        forget(uid)
        (reg.user_dir(uid) / "canvas-session.enc").unlink(missing_ok=True)

    onboarding = Onboarding(reg, tg, owner_id, login_url=login_url,
                            connected=connected, on_forget=forget,
                            on_school_change=school_change)
    import dispatch as _dispatch
    _dispatch.login_keyboard = onboarding.login_button   # "logged out" messages carry the button
    try:
        tg.set_commands([
            ("todo", "What's due — reply a code like B1 to do it"),
            ("resume", "WebAssign work you saved for later"),
            ("cancel", "Drop the current item"),
            ("check", "Try every /todo item, report what works (hands in nothing)"),
            ("login", "Canvas login"),
            ("school", "Pick your school"),
            ("start", "Ask for access / what I do"),
            ("token", "A fresh personal token"),
            ("forget", "Delete everything about me"),
        ])
        # The owner's own menu, for their chat only (Telegram's per-chat scope):
        # the global list above is what applicants see.
        tg._call("setMyCommands", scope={"type": "chat", "chat_id": int(owner_id)},
                 commands=[{"command": "users", "description": "Everyone registered, and their state"},
                           {"command": "monitor", "description": "Live memory, processes, people"},
                           {"command": "schools", "description": "Every school, verified or not, and who uses it"},
                           {"command": "start", "description": "What this bot does for you"}])
    except Exception as e:
        log.warning("setMyCommands failed: %s", e)
    log.info("public bot up; owner chat %s; %d people registered", owner_id, len(reg.all()))
    try:
        while True:
            try:
                updates = tg.get_updates(offset=state.offset, timeout=30)
            except Exception as e:
                log.warning("getUpdates failed: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                state.offset = int(u["update_id"]) + 1
                tg.transcript.record_update(u)
                t_start = time.time()
                try:
                    if _chat_of(u) == owner_id:
                        text = ((u.get("message") or {}).get("text") or "").strip().lower()
                        cmd = text.split()[0] if text else ""
                        reply = onboarding.admin_command(cmd, text[len(cmd):].strip())
                        if cmd in ("/start", "/help"):
                            reply = ("You're the owner of this bot — nothing to register. "
                                     "Applications arrive here with Approve / Reject buttons; "
                                     "/users lists everyone, /monitor shows memory and who's on.")
                        elif cmd == "/monitor":
                            if login_url and text.split()[1:2] != ["text"]:
                                tg.send(owner_id, "📊 Memory, processes and who's on, live. "
                                                  "(/monitor text: the same as a message)",
                                        reply_markup={"inline_keyboard": [[{
                                            "text": "📊 Monitor",
                                            "web_app": {"url": _monitor_url(login_url)}}]]})
                                reply = ""
                            else:                     # no page to open (login off), or asked for text
                                reply = mon.text_summary(monitor.snapshot())
                        if reply:
                            tg.send(owner_id, reply)
                        elif reply is None:
                            onboarding.handle(u)      # the Approve/Reject buttons
                    elif dispatcher.wants(u):
                        dispatcher.submit(_chat_of(u), u)   # their own assistant, own thread
                    else:
                        onboarding.handle(u)
                except Exception:
                    log.exception("update %s failed", u.get("update_id"))
                    metrics.emit("error", where="public-bot", uid=_chat_of(u))
                # Who wrote, and what kind of thing (never its text): the
                # monitor's "active" and per-person counts come from these.
                sent = (u.get("message") or {}).get("date")
                metrics.emit("update", what=_what(u), uid=_chat_of(u),
                             lag_s=round(t_start - sent, 2) if sent else None,
                             handle_s=round(time.time() - t_start, 2))
                state.save(state_path)
    except KeyboardInterrupt:
        pass
    finally:
        state.save(state_path)
        if viewer is not None:
            viewer.stop()
        if heartbeat is not None:
            heartbeat.stop()
        alerts_stop.set()
        pool.stop_reaper()
        pool.shutdown_all()
        tg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
