"""Registration over Telegram: a stranger becomes an approved person with a token.

    stranger  --/start-->  registered, unapproved  --owner taps Approve-->  approved + token
                                                   --owner taps Reject--->  removed

Every Telegram bot is public: anyone who finds it can write to it. So the
gate is here, in code — an unknown chat gets one sentence and nothing else,
and only the owner's chat can approve. Private chats only: in a group the
chat id is the group's, not a person's.

What this deliberately does NOT do: touch Canvas, cookies or passwords. It
establishes who someone is *to this bot* and gives them the bot's own token
(see tokens.py). Once approved, it offers the "Canvas login" button, which
opens viewer.py inside Telegram; the login itself happens there.

Before that, their school — any school on Canvas, found the way the Canvas app
does it: they TYPE its name, and the next text they send is a search (the
schools already known here first, then Instructure's public directory),
answered with buttons. Not listed? They paste their Canvas address, and
detect.py has to prove it is a real, public Canvas before it is stored (as
"unverified", and the owner is told). Searching and checking run off the
update loop: a slow school site must not hold up everyone else's messages.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from typing import Callable, Optional
from urllib.parse import urlsplit

from registry import Registry
import detect
import schools
import tokens

log = logging.getLogger("ainl.scaleup.onboarding")

WELCOME = (
    "Hi — I'm a study bot for Canvas: your to-do list, and I hand in what you "
    "send me, after you confirm.\n\n"
    "Send /start to ask for access. The owner approves each person by hand."
)
PENDING = "You're registered and waiting for approval — I'll message you when that happens."
APPROVED = (
    "✅ Approved. Here is your personal token — it identifies you to me when you "
    "connect your Canvas session later. Keep it private; /token issues a new one "
    "(and cancels this one) if it ever leaks.\n\n<code>{token}</code>\n\n"
    "/forget removes everything about you at any time."
)
CONNECT = (
    "🔐 <b>Connect your Canvas</b> — {school}\n\n"
    "Tap <b>Canvas login</b> below. Your own browser opens right here in Telegram, "
    "on {school}'s login page: sign in with your {id_name} (and approve Duo or your school's "
    "second step on your phone, if it asks).\n\n"
    "Your password goes only to your school's page — I don't see it or keep it. "
    "Afterwards I keep your Canvas session (encrypted) to work for you; "
    "/forget deletes it. Wrong school? /school"
)
PICK_SCHOOL = ("🎓 <b>Which school's Canvas do you use?</b>\n\n"
               "Type your school's name — for example <i>UC Santa Barbara</i>.")
NOT_LISTED = "Not listed? Send your Canvas address"
ASK_URL = ("Send the address you open Canvas at — like <code>canvas.myschool.edu</code> "
           "(copy it from your browser). I'll check that it's a real Canvas first.")
RESULTS = "Tap your school — or type another name:"
NO_RESULTS = ("I found no school called “{q}”. Try another name (or a shorter one), "
              "or send your Canvas address.")
CHECKING = "🔎 Checking {host}…"
NOT_USABLE = "I couldn't use that: {why}"
SWITCHED_SCHOOL = ("Switched to {school}. Your saved Canvas session for the old school "
                   "was deleted.")
CONNECTED = ("✅ Your Canvas is connected — /todo shows what's due. Tap Canvas login "
             "again only if Canvas logged you out.")
LOGIN_SOON = ("Your Canvas isn't connected yet — the login step isn't open yet. "
              "/token, /forget are available.")
REJECTED = "The owner didn't approve this request. Nothing about you was kept."


def auto_approve() -> bool:
    """Approve every /start at once. The by-hand step (owner taps Approve) is
    kept, only switched off: AINL_AUTO_APPROVE=0 brings it back."""
    import os
    return os.environ.get("AINL_AUTO_APPROVE", "1").strip() != "0"
NOT_YET = "Not approved yet — hold on. (/forget to withdraw.)"
FORGOTTEN = "Done — your registration, token and data directory are gone."


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Onboarding:
    """Handles every update that is NOT the owner's own chat."""

    def __init__(self, reg: Registry, tg, owner_chat_id: str,
                 on_approved: Optional[Callable[[str], None]] = None,
                 login_url: Optional[str] = None,
                 connected: Callable[[str], bool] = lambda cid: False,
                 on_forget: Optional[Callable[[str], None]] = None,
                 on_school_change: Optional[Callable[[str], None]] = None,
                 directory: Optional[Callable[[str], list]] = None,
                 detect_school: Optional[Callable[..., object]] = None,
                 background: Optional[Callable[[Callable[[], None]], None]] = None):
        self.reg, self.tg, self.owner = reg, tg, str(owner_chat_id)
        self.on_approved = on_approved     # hook for the dispatcher, if any
        # The HTTPS page Telegram opens for "Canvas login" (viewer.py behind
        # the tunnel). None = the login step isn't running; say so instead.
        self.login_url = login_url
        self.connected = connected         # has this person a saved Canvas session?
        self.on_forget = on_forget         # stop their viewer + browser before deleting
        # A new school means a different Canvas: drop the old session first.
        self.on_school_change = on_school_change
        # The network, injectable for tests: Instructure's directory search,
        # the Canvas check, and where the slow parts run (a thread by default).
        self.directory = directory or (lambda term: detect.search_directory(term))
        self.detect_school = detect_school or detect.detect
        self.background = background or (lambda fn: threading.Thread(
            target=fn, name="school-search", daemon=True).start())

    def school_of(self, cid: str):
        u = self.reg.get(cid)
        return schools.get(u.school) if u else None

    def pending_school_search(self, cid: str) -> Optional[str]:
        """"name" / "url" while their next text is a school search — the
        dispatcher asks this so a connected person's answer to /school
        reaches here, not their homework assistant."""
        if not _safe(cid) or not self.reg.is_approved(cid):
            return None
        return schools.db().pending(cid)

    def _ask_school(self, cid: str, note: str = "") -> None:
        schools.db().set_pending(cid, "name")
        self._send(cid, PICK_SCHOOL + (f"\n\n{note}" if note else ""),
                   {"inline_keyboard": [[{"text": NOT_LISTED, "callback_data": "school:?"}]]})

    def login_button(self) -> Optional[dict]:
        if not self.login_url:
            return None
        return {"inline_keyboard": [[{"text": "🔐 Canvas login",
                                      "web_app": {"url": self.login_url}}]]}

    def _offer_login(self, cid: str) -> None:
        if self.connected(cid):
            return self._send(cid, CONNECTED, self.login_button())
        if not self.login_url:
            return self._send(cid, LOGIN_SOON)
        school = self.school_of(cid)
        if school is None:
            return self._ask_school(cid)
        self._send(cid, CONNECT.format(school=esc(school.name), id_name=esc(school.id_name)),
                   self.login_button())

    # -- entry ---------------------------------------------------------------

    def handle(self, update: dict) -> None:
        cb = update.get("callback_query")
        if cb:
            return self._callback(cb)
        m = update.get("message") or {}
        chat = m.get("chat") or {}
        if chat.get("type") != "private":
            return                         # groups: not a person, stay silent
        cid = str(chat.get("id"))
        text = (m.get("text") or "").strip()
        cmd = text.split()[0].lower() if text.startswith("/") else ""
        user = self.reg.get(cid) if _safe(cid) else None

        if cmd == "/forget" and user is not None:
            if self.on_forget:
                try:
                    self.on_forget(cid)
                except Exception:
                    log.exception("on_forget failed for %s", cid)
            self.reg.remove(cid)
            schools.db().forget(cid)
            return self._send(cid, FORGOTTEN)
        if user is None:
            if cmd == "/start":
                return self._register(cid, m.get("from") or {})
            return self._send(cid, WELCOME)
        if not user.approved:
            if not auto_approve():
                return self._send(cid, NOT_YET)
            self._approve(cid, user)             # asked while approval was by hand
            return
        if cmd == "/token":
            tok = tokens.issue(self.reg, cid)
            return self._send(cid, APPROVED.format(token=esc(tok)))
        if cmd == "/school":
            note = ("Switching logs you out of Canvas here; you'll sign in again."
                    if self.connected(cid) else "")
            return self._ask_school(cid, note)
        mode = self.pending_school_search(cid)
        if mode and not cmd and text:
            return self._school_text(cid, text, mode)
        if mode:
            schools.db().clear_pending(cid)     # another command: the question is dropped
        # Approved: everything else leads to the Canvas login (or says it's done).
        self._offer_login(cid)

    # -- registration --------------------------------------------------------

    def _register(self, cid: str, frm: dict) -> None:
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "?"
        handle = frm.get("username") or ""
        self.reg.add(cid, name=name, login_id=handle)   # login_id: Telegram @handle for now
        log.info("registration request from chat %s", cid)
        who = esc(name) + (f" (@{esc(handle)})" if handle else "")
        if auto_approve():
            # Approval by hand is switched off (AINL_AUTO_APPROVE, on by default
            # since 2026-09-27: ten classmates at once). The owner is told, no
            # buttons to press.
            self._send(self.owner, f"🙋 <b>{who}</b> joined (chat {esc(cid)}) — approved automatically.")
            return self._approve(cid, self.reg.get(cid), tell_owner=False)
        self._send(cid, PENDING)
        self.tg.send(self.owner, f"🙋 <b>{who}</b> asks for access (chat {esc(cid)}).",
                     reply_markup={"inline_keyboard": [[
                         {"text": "✅ Approve", "callback_data": f"reg:ok:{cid}"},
                         {"text": "✖ Reject", "callback_data": f"reg:no:{cid}"}]]})

    def _callback(self, cb: dict) -> None:
        data = cb.get("data") or ""
        from_chat = str(((cb.get("message") or {}).get("chat") or {}).get("id"))
        try:
            self.tg.answer_callback(cb["id"])
        except Exception:
            pass
        if data.startswith("school:"):
            return self._pick_school(from_chat, data.split(":", 1)[1])
        if not data.startswith("reg:") or from_chat != self.owner:
            return                          # only the owner's own buttons count
        _, verdict, cid = data.split(":", 2)
        user = self.reg.get(cid) if _safe(cid) else None
        if user is None:
            return self._send(self.owner, "That request is gone already.")
        if verdict == "ok":
            self._approve(cid, user)
        else:
            self.reg.remove(cid)
            self._send(cid, REJECTED)
            self._send(self.owner, f"Rejected {esc(user.name)}; their record is deleted.")

    def _approve(self, cid: str, user, tell_owner: bool = True) -> None:
        self.reg.approve(cid)
        tok = tokens.issue(self.reg, cid)
        self._send(cid, APPROVED.format(token=esc(tok)))
        self._offer_login(cid)
        if tell_owner:
            self._send(self.owner, f"Approved {esc(user.name)}. They have their token.")
        if self.on_approved:
            self.on_approved(cid)

    # -- finding their school --------------------------------------------------

    def _school_text(self, cid: str, text: str, mode: str) -> None:
        if mode == "url" or _looks_like_address(text):
            self.background(lambda: self._add_by_address(cid, text))
        else:
            self.background(lambda: self._search(cid, text))

    def _search(self, cid: str, q: str) -> None:
        """Known schools first, then Instructure's directory; one button per
        Canvas (de-duplicated by host), five at most."""
        q = " ".join(q.split())[:80]
        if len(q) < 2:
            return self._send(cid, "Type a few letters of your school's name.")
        db = schools.db()
        rows, hosts = [], set()
        for s in db.search(q, 5):
            host = urlsplit(s.canvas_origin).hostname
            hosts.add(host)
            rows.append([{"text": _label(s.name, host), "callback_data": f"school:{s.key}"}])
        if len(rows) < 5:
            for r in self._directory_quiet(q):
                if len(rows) >= 5:
                    break
                if r["domain"] in hosts:
                    continue
                hosts.add(r["domain"])
                known = db.by_origin("https://" + r["domain"])
                if known is not None:
                    data = f"school:{known.key}"
                else:
                    cand = db.put_candidate(cid, r["name"], r["domain"],
                                            r.get("authentication_provider") or "")
                    data = f"school:+{cand}"
                rows.append([{"text": _label(r["name"], r["domain"]), "callback_data": data}])
        rows.append([{"text": NOT_LISTED, "callback_data": "school:?"}])
        self._send(cid, RESULTS if len(rows) > 1 else NO_RESULTS.format(q=esc(q)),
                   {"inline_keyboard": rows})

    def _directory_quiet(self, term: str) -> list:
        try:
            return self.directory(term) or []
        except Exception:
            log.exception("school directory search failed")
            return []

    def _add_from_directory(self, cid: str, cand: dict) -> None:
        """A directory result they tapped: check it is a Canvas we can use, then add it."""
        self._send(cid, CHECKING.format(host=esc(cand["domain"])))
        try:
            d = self.detect_school(cand["domain"], hint=cand.get("provider") or None)
        except detect.Rejected as e:
            return self._send(cid, NOT_USABLE.format(why=esc(str(e))) +
                              " Try again later, or send your Canvas address.")
        db = schools.db()
        school = db.by_origin(d.origin) or db.add(
            name=cand["name"], canvas_origin=d.origin, start_path=d.start_path,
            domains=d.domains, status="verified", source="instructure", added_by=cid,
            aliases=cand["domain"])
        log.info("school %s from the directory for %s: %s", school.key, cid, "; ".join(d.notes))
        self._choose(cid, school)

    def _add_by_address(self, cid: str, text: str) -> None:
        """A pasted address: stored only once detect.py says it's a public Canvas."""
        db = schools.db()
        db.set_pending(cid, "url")              # a wrong address: they can just send another
        try:
            origin = detect.parse_origin(text)
        except detect.Rejected as e:
            return self._send(cid, NOT_USABLE.format(why=esc(str(e))))
        known = db.by_origin(origin)
        if known is not None:
            return self._choose(cid, known)
        self._send(cid, CHECKING.format(host=esc(urlsplit(origin).hostname)))
        try:
            d = self.detect_school(origin)
        except detect.Rejected as e:
            return self._send(cid, NOT_USABLE.format(why=esc(str(e))))
        school = db.by_origin(d.origin)
        if school is None:
            # In Instructure's directory after all (they searched another name):
            # as good as picking it there.
            final = urlsplit(d.origin).hostname
            listed = next((r for r in self._directory_quiet(final) if r["domain"] == final), None)
            if listed:
                school = db.add(name=listed["name"], canvas_origin=d.origin,
                                start_path=d.start_path, domains=d.domains, status="verified",
                                source="instructure", added_by=cid, aliases=final)
            else:
                school = db.add(name=final, canvas_origin=d.origin, start_path=d.start_path,
                                domains=d.domains, status="unverified", source="user_url",
                                added_by=cid)
                u = self.reg.get(cid)
                self._send(self.owner,
                           f"🏫 {esc(u.name if u else cid)} added a school by its address: "
                           f"<b>{esc(final)}</b> (key {esc(school.key)}, login starts at "
                           f"{esc(d.start_path)}). It is <b>unverified</b> — /verify "
                           f"{esc(school.key)} once you've checked it; /schools lists them all.")
        log.info("school %s by address for %s: %s", school.key, cid, "; ".join(d.notes))
        self._choose(cid, school)

    def _pick_school(self, cid: str, key: str) -> None:
        """Their own button, in their own chat: only an approved person picks,
        and only for themselves (the chat the button was pressed in)."""
        user = self.reg.get(cid) if _safe(cid) else None
        if user is None or not user.approved:
            return
        if key == "?":
            schools.db().set_pending(cid, "url")
            return self._send(cid, ASK_URL)
        if key.startswith("+"):
            cand = schools.db().candidate(int(key[1:]), cid) if key[1:].isdigit() else None
            if cand is None:
                return self._send(cid, "That list is out of date — type your school's name again.")
            return self.background(lambda: self._add_from_directory(cid, cand))
        school = schools.get(key)
        if school is not None:
            self._choose(cid, school)

    def _choose(self, cid: str, school) -> None:
        user = self.reg.get(cid)
        if user is None or not user.approved:
            return
        schools.db().clear_pending(cid)
        key = school.key
        if user.school and user.school != key:
            if self.on_school_change:
                try:
                    self.on_school_change(cid)
                except Exception:
                    log.exception("on_school_change failed for %s", cid)
            self.reg.set_school(cid, key)
            self._send(cid, SWITCHED_SCHOOL.format(school=esc(school.name)))
        else:
            self.reg.set_school(cid, key)
        self._offer_login(cid)

    # -- admin ---------------------------------------------------------------

    def admin_command(self, cmd: str, arg: str = "") -> Optional[str]:
        """Owner-only commands the dispatcher routes here. None = not ours."""
        if cmd == "/users":
            rows = [f"· {esc(u.name)} {'✅' if u.approved else '⏳'} "
                    f"{'🔑' if tokens.has_token(self.reg, u.chat_id) else ''}"
                    f"{'🔐' if self.connected(u.chat_id) else ''}"
                    f"{' · ' + esc(schools.get(u.school).name) if schools.get(u.school) else ''}"
                    f" (chat {esc(u.chat_id)})"
                    for u in self.reg.all()]
            return "<b>People</b>\n" + ("\n".join(rows) if rows else "nobody yet")
        if cmd == "/schools":
            db = schools.db()
            people = Counter(u.school for u in self.reg.all() if u.school)
            rows = []
            for s in db.all():
                seen = ", ".join(h for h, _ in db.hosts(s.key)[:4])
                rows.append(f"· <b>{esc(s.name)}</b> {'✅' if s.status == 'verified' else '⚠️ unverified'}"
                            f" · {people.get(s.key, 0)} people · {esc(s.source)}\n"
                            f"   {esc(urlsplit(s.canvas_origin).hostname)}{esc(s.start_path)}"
                            f" (key {esc(s.key)})" + (f"\n   login via {esc(seen)}" if seen else ""))
            return "<b>Schools</b>\n" + "\n".join(rows)
        if cmd == "/verify":
            key = arg.strip().lower()
            if key and schools.db().set_status(key, "verified"):
                return f"✅ {esc(key)} is verified."
            return "Usage: /verify &lt;key&gt; (keys are in /schools)"
        return None

    def _send(self, cid: str, text: str, reply_markup: Optional[dict] = None) -> None:
        try:
            if reply_markup:
                self.tg.send(cid, text, reply_markup=reply_markup)
            else:
                self.tg.send(cid, text)
        except Exception as e:              # a blocked bot must not stop the loop
            log.warning("send to %s failed: %s", cid, str(e)[:80])


def _label(name: str, host: str) -> str:
    """A button: the school's name and its Canvas host, so two "State
    University"s can be told apart."""
    text = f"{name} · {host}"
    return text if len(text) <= 60 else text[:59] + "…"


_ADDRESS = re.compile(r"^(https?://)?[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?(/\S*)?$", re.I)
_TLDS = ("edu", "com", "org", "net", "io", "us", "ca", "uk", "au", "nz", "ie", "sg", "hk")


def _looks_like_address(text: str) -> bool:
    """A name search unless it is clearly an address: "st. olaf" stays a name,
    canvas.myschool.edu and https://… are addresses."""
    t = text.strip()
    if "://" in t:
        return True
    if not _ADDRESS.match(t):
        return False
    host = t.split("/", 1)[0].split(":", 1)[0].lower()
    return host.rsplit(".", 1)[-1] in _TLDS


def _safe(cid: str) -> bool:
    try:
        from registry import _sanitize
        _sanitize(cid)
        return True
    except ValueError:
        return False
