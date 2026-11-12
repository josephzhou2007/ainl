"""The chat: /todo → pick → read → reply → confirm → submitted.

Tier A only (Canvas-native text / url / file / discussion). External-tool items
are listed with their link; quizzes are listed as "open in Canvas". The bot
never composes an answer — it moves what the student sends.
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ..brief.compose import _short_course
from ..canvas.api import ApiSource
from ..canvas._http import CanvasAuthError, CanvasHTTPError
from ..canvas.models import Item, Kind, enrich_submission_types
from ..external.webassign import CDP_URL as WEBASSIGN_CDP_URL
from ..external.webassign import (WebAssignUnavailable, _dom_of, math_targets,
                                  multipart, steps as step_buttons, typed_boxes)
from ..mathinput.latex_to_mathml import latex_to_webassign_mathml
from ..mathinput.pipeline import (final_answer, number_problems, split_parts,
                                  split_problems, transcribe_with_consensus,
                                  without_word, written_choice)
from ..telegram.client import TelegramClient, TelegramError, esc, inline_keyboard
from ..util import trouble
from ..util.html import canvas_file_ids, html_to_text
from .state import BotState, ChatState, Draft, MathSession, MenuItem

log = logging.getLogger("ainl.bot")

_URL_RE = re.compile(r"^https?://\S+$")
_CODE_RE = re.compile(r"^[A-J]\d{1,2}$")
# "3: \frac{1}{2}x" — correct one answer the reading got wrong
_MATH_FIX_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(.+)$", re.S)


_FILL_WORDS = {"fill", "fill in", "fill it", "fill it in", "fill these in", "fill them in", "填", "填写", "填上"}
_SUBMIT_WORDS = {"submit", "submit it", "hand in", "提交", "交", "交了"}


def typed_choice(text: str) -> Optional[tuple[int, Optional[int], str]]:
    """A choice answered by typing: (question, part index or None, letters).

    "3d", "3 D", "3: D", "3. D", "3(a) D", "3a D", "3. C1 D", "3 c2 B, D".
    2026-09-27: after a submit "3: D" said "There's no question 3" and
    "3. C1 D" was not understood at all. None if it is not that shape."""
    from ..autosolve import _letters
    m = re.match(r"^\s*(\d{1,2})\s*(.*)$", text or "", re.S)
    if not m:
        return None
    n, rest, part = int(m.group(1)), m.group(2).strip(), None
    pm = (re.match(r"^\(([a-jA-J])\)\s*(.*)$", rest)
          or re.match(r"^([a-j])[:.)]?\s+(\S.*)$", rest))
    if pm:
        part, rest = ord(pm.group(1).lower()) - 97, pm.group(2)
    else:
        cm = re.match(r"^[.:]?\s*[cC]\s*(\d)\b\s*(.*)$", rest)
        if cm:
            part, rest = int(cm.group(1)) - 1, cm.group(2)
    rest = rest.lstrip(" .:：)")
    letters = _letters(rest)
    if not letters or len(rest) > 24:
        return None
    return n, part, letters
# A quiz may be timed, so it never opens without a confirmation — except
# the ones confirmed untimed, listed in webassign.py with their evidence.
_QUIZ_TITLE = re.compile(r"\bquiz\b", re.I)
_UNTIMED_QUIZ_TITLE = re.compile(r"\bmodule\s+quiz\b", re.I)
DOABLE = {"online_text_entry", "online_upload", "online_url"}
HELP = (
    "<b>ainl</b> — your Canvas inbox.\n\n"
    "/todo — today → next 4 days, one block per course (A, B, C…)\n"
    "/todo 14 — look further ahead\n"
    "reply a code like C2 — that opens it. Everything starts from a code.\n"
    "  ✍️ 📎 💬  Canvas work: send text / a file / a link and I submit it\n"
    "  🧮  WebAssign: I send the questions; tap the multiple-choice ones, write\n"
    "      the rest on one sheet and send a photo\n"
    "  ↗  somewhere I can't work yet — you get the link\n"
    "(several files or photos? send them all, then ✅ Submit all — one attempt)\n\n"
    "/resume — WebAssign work you set aside with ⏸ Later\n"
    "/cancel — drop the current item\n"
    "/restart — clear everything in flight (use it if I go quiet)\n"
    "/timing — how long recent steps took\n"
    "/channel — which way I talk to Canvas (token / cookie / auto)\n\n"
    "I only move what you send. I never write answers."
)
_TEXT_WRAP_RE = re.compile(r"\\(?:text|mathrm|textrm)\{([^{}]*)\}")


_NUMBER_UNIT_RE = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*[A-Za-z%°/²³]*\s*$")


_FRAC_RE = re.compile(r"^\s*([-+]?)\s*\\[dt]?frac\{\s*(-?\d+)\s*\}\{\s*(\d+)\s*\}\s*$")


def number_only(val: str) -> str:
    """"8.1 kg" -> "8.1": a numeric box has its unit printed next to it, and
    a unit typed into it is marked wrong. "-\\frac{57}{2}" -> "-28.5": a
    numeric box takes a number, not LaTeX. Anything else is left alone."""
    f = _FRAC_RE.match(val)
    if f and int(f.group(3)) != 0:
        x = int(f.group(2)) / int(f.group(3)) * (-1 if f.group(1) == "-" else 1)
        return f"{x:.10g}"
    m = _NUMBER_UNIT_RE.match(val)
    return m.group(1) if m else val


def plain_text(tex: str) -> str:
    """A typed box wants the answer as the student would type it: "18.5",
    "kg", "DIVERGES" — not LaTeX. The reader wraps words in \\text{}, marks
    thousands with \\, and escapes a few symbols; strip those, keep the rest."""
    t = _TEXT_WRAP_RE.sub(r"\1", tex)
    t = t.replace("\\,", "")                       # 2\,500 -> 2500: a thousands mark
    t = t.replace("\\;", " ").replace("\\ ", " ").replace("~", " ")
    t = t.replace("\\%", "%").replace("\\$", "$").replace("\\_", "_")
    t = re.sub(r"^\$+|\$+$", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def _clip(title: str, n: int) -> str:
    """A title cut to n characters, with a … so the student can tell it was cut
    ('Read and Annotate: "Sense of Belonging Among' looked like the whole name)."""
    return title if len(title) <= n else title[:n - 1].rstrip() + "…"


def _extensions(allowed) -> list[str]:
    """Canvas's allowed_extensions as a teacher typed them. A teacher who wrote
    "…, webp, and jpg" leaves an extension called "and" in the list (ESL116,
    2026-09-25: "docx, pdf, png, jpeg, gif, svg, webp, and, jpg"). No file is
    called *.and — drop the connecting words rather than show them as formats."""
    return [x for x in allowed if x.lower().strip(". ") not in ("and", "or")]


def _due_str(it: Item, tz: ZoneInfo) -> str:
    d = it.due_at or it.planned_at
    return d.astimezone(tz).strftime("%a %m-%d %H:%M") if d else "no due date"


class _Muted:
    """Stands in for the Telegram client during /check: remembers the text it
    would have sent, sends nothing (question pictures, buttons, the lot)."""

    def __init__(self, real):
        self._real, self.said = real, []

    def send(self, chat_id, text, *a, **k):
        self.said.append(re.sub(r"<[^>]+>", "", text or ""))
        return []

    def __getattr__(self, name):
        if name in ("send_photo", "send_media_group", "send_document", "edit_markup", "edit_text"):
            return lambda *a, **k: 0
        return getattr(self._real, name)


class Conversation:
    def __init__(self, canvas: ApiSource, tg: TelegramClient, chat_id: str, state: BotState,
                 tz: str = "America/Chicago", webassign=None, transcribe=None, gradescope=None,
                 autosolve=None):
        self.canvas, self.tg, self.chat_id, self.state = canvas, tg, chat_id, state
        self.tz = ZoneInfo(tz)
        # injected in tests; the real ones need a browser window and an API key
        self._webassign = webassign
        self._gradescope = gradescope
        self.transcribe = transcribe or transcribe_with_consensus
        # TEST ONLY: a model writes the sheet instead of the student, so the
        # fill/submit path can be exercised end to end. Off unless AINL_AUTOSOLVE
        # is set (see ainl.autosolve). Fill and Submit still need the taps.
        from ..autosolve import from_env as _autosolve_from_env
        self.autosolve = autosolve if autosolve is not None else _autosolve_from_env()
        self._math_prompt: str = ""                  # the last "Write the answers…" text
        import threading
        self._lock = threading.RLock()               # the chat and /auto's worker take turns
        self._auto_stop = threading.Event()
        self._auto_thread = None
        self._auto_pending = None                    # (answers, problems, …) awaiting one repair
        self._auto_repaired = False
        self._state_path = None                      # set by the runner, for /auto's own saves
        self._clashes: list[tuple[int, str, str]] = []   # (tap index, sheet's value, word)
        self._preview_msg: dict[str, int] = {}          # chat_id -> the file preview being updated
        self._file_bytes: dict[str, list[bytes]] = {}   # chat_id -> pending upload bytes, same order as draft.files

    # -- entry ----------------------------------------------------------------

    def handle(self, update: dict) -> None:
        with self._lock:
            self._handle(update)

    def _handle(self, update: dict) -> None:
        msg = update.get("message")
        cb = update.get("callback_query")
        text = ((msg or {}).get("text") or "").strip().lower()
        if text.split()[:1] and text.split()[0] in ("/cancel", "/restart"):
            self._auto_stop.set()                    # a stop by any other name
        try:
            if cb:
                self._callback(cb)
            elif msg:
                self._message(msg)
        except CanvasAuthError as e:
            if not self._fall_back(e):
                self.ask_login()
        except (CanvasHTTPError, TelegramError) as e:
            log.exception("handler failed")
            self._send(f"❌ {esc(str(e))[:300]}")
        except Exception as e:
            # One thread serves the whole chat: an uncaught KeyError here used
            # to end the process, and the student saw only silence.
            log.exception("handler crashed")
            try:
                self._send(f"❌ Something broke on my side ({esc(type(e).__name__)}). "
                           "Nothing was submitted. /restart, then /todo.")
            except Exception:
                pass

    @staticmethod
    def login_keyboard() -> Optional[dict]:
        """The "🔐 Canvas login" button: the login page (AINL_LOGIN_URL, served
        by the public bot) streams this bot's own browser, right in Telegram.
        None when there is no https login page configured."""
        import os
        url = os.environ.get("AINL_LOGIN_URL", "").strip()
        if not url.startswith("https://"):
            return None
        return {"inline_keyboard": [[{"text": "🔐 Canvas login", "web_app": {"url": url}}]]}

    def ask_login(self, detail: str = "") -> None:
        """Canvas needs a person to sign in. With the login page: one tap, and
        the next message picks the new session up — no restart. Without it,
        what to do at the host."""
        kb = self.login_keyboard()
        if kb is not None:
            # 🔐, not ⚠️: an expired session is the normal course of things,
            # not a failure to file a scene and alert the owner about.
            self._send("🔐 Canvas session expired. Tap <b>🔐 Canvas login</b> and sign in "
                       "(NetID + Duo) — your own browser opens right here. I pick the new "
                       "session up by myself; nothing to restart.", kb)
        elif detail:
            self._send("⚠️ Canvas access expired.\n\n" + detail)
        else:
            self._send("⚠️ Canvas access expired and there's no other channel left — run "
                       "<code>python -m ainl.canvas --login</code> on the host.")

    def waiting_for_student(self) -> bool:
        """Is the next move the student's — writing answers on paper, taking
        the photo, finding the file? Then the browser is not needed until they
        send it, and its tabs can go (the next step reopens what it needs)."""
        auto = self._auto_thread
        if auto is not None and auto.is_alive():
            return False                                 # /auto, /sweep, /check are working
        ms = self.cs.math
        if ms is not None:
            return ms.stage == "shoot" and not ms.auto
        return self.cs.pending is not None

    def _send(self, text: str, keyboard: Optional[dict] = None) -> None:
        if getattr(self, "_sweeping", False):
            keyboard = None              # nothing to tap: the sweep moves on by itself
        bad = trouble.problem_line(text)
        if bad:
            text, keyboard = self._way_out(text, keyboard)
        ids = self.tg.send(self.chat_id, text, reply_markup=keyboard)
        if bad and trouble.enabled():
            # After the student has their answer: the scene is captured and the
            # owner told in the background. Inside an except block the error
            # itself comes along (trouble.report reads sys.exc_info()).
            try:
                where, cdp = self._trouble_where(), self._trouble_cdp()
            except Exception:
                where, cdp = "?", None
            trouble.report("script", chat=self.chat_id, where=where, message=text, cdp_url=cdp)
        return ids if isinstance(ids, list) else []

    # -- when something failed ----------------------------------------------------

    def _way_out(self, text: str, keyboard: Optional[dict]) -> tuple[str, Optional[dict]]:
        """What a failure message adds for the person reading it. The owner's
        own bot: nothing (they are the one who gets told). A classmate's
        assistant adds that the owner knows, and a way to finish by hand."""
        return text, keyboard

    def _trouble_where(self) -> str:
        """What the assistant was doing, for the failure report."""
        cs = self.cs
        parts = []
        if getattr(self, "_sweeping", False):
            parts.append("sweep")
        auto = getattr(self, "_auto_thread", None)
        if auto is not None and auto.is_alive():
            parts.append("auto")
        it = cs.pending
        if it is not None:
            parts.append(f"{getattr(it, 'vendor', '') or 'canvas'}:{getattr(it, 'code', '') or '?'}")
        if cs.math is not None:
            parts.append(f"math:{cs.math.stage}")
        return " ".join(parts) or "chat"

    def _trouble_cdp(self) -> Optional[str]:
        """The browser whose pages are the scene (never started for this)."""
        cdp = getattr(self._webassign, "cdp_url", None)
        return cdp if isinstance(cdp, str) else WEBASSIGN_CDP_URL

    @property
    def cs(self) -> ChatState:
        return self.state.chat(self.chat_id)

    # -- messages -------------------------------------------------------------

    def _message(self, m: dict) -> None:
        if str((m.get("chat") or {}).get("id")) != self.chat_id:
            return                                          # not our user; stay silent
        text = (m.get("text") or "").strip()
        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            return self._command(head.lower(), arg.strip().lower())
        if m.get("document") or m.get("photo"):
            ms = self.cs.math
            if ms and ms.stage in ("shoot", "confirm", "filled", "fixing", "graded"):
                return self._math_photo(m)
            return self._incoming_file(m)
        if text:
            return self._incoming_text(text)

    def _command(self, cmd: str, arg: str = "") -> None:
        if cmd in ("/start", "/help"):
            self._send(HELP)
        elif cmd == "/channel":
            self._channel(arg)
        elif cmd == "/math":
            # Not on the menu any more: /todo is the one way in, and a second
            # list of the same work was a second thing to learn. Kept working
            # because it is the only view of WebAssign items Canvas does not
            # put in the planner at all — a back door, not a front one.
            self._math(arg)
        elif cmd == "/todo":
            self._todo(int(arg) if arg.isdigit() else None, show_done=(arg == "all"))
        elif cmd == "/timing":
            from ..util.timing import summary
            self._send("<b>How long things took</b>\n" + summary())
        elif cmd == "/resume":
            self._resume_list()
        elif cmd == "/restart":
            self._restart()
        elif cmd == "/auto":
            self._auto(arg)
        elif cmd == "/sweep":
            self._sweep(arg)
        elif cmd == "/check":
            self._check(arg)
        elif cmd == "/stats":
            self._stats()
        elif cmd == "/cancel":
            self.cs.quiz = None
            self._drop_everything(keep_menu=True)
            self._send("Dropped — nothing kept. Your /todo codes still work.")
        else:
            self._send("Unknown command. /help")

    def _restart(self) -> None:
        """Drop everything in flight and say what was dropped.

        A browser step can take half a minute, and the bot polls on one thread,
        so a slow call leaves the chat looking dead. This clears the state that
        survives such a moment — it cannot interrupt a call already running,
        which is why the browser work has timeouts of its own.
        """
        had = self._drop_everything()
        what = ": " + "; ".join(esc(h) for h in had) if had else " — nothing was in flight."
        parked = (f"\n⏸ {len(self.cs.parked)} round(s) set aside are untouched — /resume."
                  if self.cs.parked else "")
        self._send(f"Cleared{what}\n\nNothing was submitted, and the pictures I was "
                   f"holding are gone. /todo to start again.{parked}")

    def _drop_everything(self, keep_menu: bool = False) -> list[str]:
        """End the round and free what it was holding; return what was dropped.

        Ending has to be final. The question images are several megabytes and
        the student can no longer reach them, so keeping them is a leak with
        extra steps. Said in their words, too — "stage: pick" meant something
        to me and nothing to them.
        """
        cs = self.cs
        had: list[str] = []
        if cs.math:
            had.append(f"the WebAssign round on {cs.math.title or 'an assignment'}")
        if cs.pending:
            had.append(f"{cs.pending.code} {cs.pending.title}")
        if cs.draft:
            had.append("an unsent draft")
        if cs.quiz:
            had.append(f"the quiz {cs.quiz.title} (your attempt stays open on Canvas)")
        cs.math, cs.pending, cs.draft, cs.quiz = None, None, None, None
        if not keep_menu:       # cancelling ONE item must not void the other codes
            cs.menu = []
        self._file_bytes.pop(self.chat_id, None)
        self._preview_msg.pop(self.chat_id, None)
        try:
            if self._webassign is not None and self._webassign.forget():
                had.append("the question pictures")
        except Exception:
            log.debug("could not clear the WebAssign cache", exc_info=True)
        return had

    # -- /math (WebAssign: push questions, read one answer sheet, fill) --------

    def _math(self, arg: str) -> None:
        """Everything on WebAssign in one place — homework and quizzes both.

        A quiz is listed, not hidden, but gets a confirmation step: whether its
        clock starts on open is the student's to know, and it cannot be undone,
        so it is never one tap away.
        """
        wa = self._wa()
        try:
            items = wa.assignments(include_quizzes=True)
        except WebAssignUnavailable as e:
            return self._send(f"⚠️ {esc(str(e))}")
        if not items:
            return self._send("Nothing on My Assignments right now.")
        rows = [[(f"{'⏱ ' if a.needs_confirm else ''}{a.title[:38]} · {a.due[:12]}",
                  f"{'mq' if a.needs_confirm else 'm'}:{a.dep}")] for a in items[:10]]
        self.cs.math = MathSession(stage="pick")
        tail = "\n⏱ = quiz; I ask before opening one." if any(a.is_quiz for a in items[:10]) else ""
        self._send(f"<b>WebAssign · MATH222</b>\nWhich one?{tail}", inline_keyboard(rows))

    def _math_confirm_quiz(self, dep: str) -> None:
        self._send(
            "⏱ That's a <b>quiz</b>. If it's timed, opening it starts the clock, "
            "and that can't be undone.\n\nOpen it?",
            inline_keyboard([[("Yes, open it", f"m:{dep}")], [("✖ No", "cancel")]]))

    def _math_open(self, dep: str, item=None, only: Optional[set] = None) -> None:
        """Open a round. ``only``: just these questions (Redo after a submit),
        shown again with their pictures and buttons, answered like the first time."""
        wa = self._wa()
        self._send("Opening it — a moment." if not only else
                   f"🔁 Opening {', '.join(map(str, sorted(only)))} again — a moment.")
        try:
            title, questions, shots = wa.questions(dep)
        except WebAssignUnavailable as e:
            return self._send(f"⚠️ {esc(str(e))}")
        whole = list(questions)                      # progress is about all of them
        if not only:
            # Worked on before: only what is not right yet, like Redo — the
            # questions already at full marks are not shown again (the owner,
            # 2026-09-27).
            full = {q.n for q in questions if self._full_marks(q.stem)}
            if full:
                rest = {q.n for q in questions} - full
                if not rest:
                    self._record_progress(dep, title, whole, str(getattr(item, "id", "") or ""))
                    self.cs.math = None                  # no round to hold open
                    self._send(f"✅ <b>{esc(title)}</b> — every question is already right. Nothing to do.")
                    return self._refresh_todo()
                only = rest
        if only:
            keep = [i for i, q in enumerate(questions) if q.n in only]
            questions, shots = [questions[i] for i in keep], [shots[i] for i in keep]
        questions, shots, begun, stuck = self._auto_begin(wa, dep, list(questions), list(shots))
        targets = math_targets(questions)
        # Same definition of a blank as `targets` — multipart() counts text
        # boxes too, and indexing targets with its answer was a KeyError.
        multi = [n for n, boxes in targets.items() if len(boxes) > 1]
        typed = typed_boxes(questions)
        # Step questions: their sheet answers are kept whole and spent part by
        # part. A Begin button is offered only where beginning it ourselves
        # failed; a question with an open part is never offered its NEXT one.
        # A step question: a "begin" button, or a box inside a step ("Step 5") —
        # a question opened mid-way has the latter and maybe not the former.
        stepped = sorted({q.n for q in questions
                          if any(p.kind == "step" or (p.written and p.label) for p in q.parts)}
                         | {n for n, _ in begun})
        has_open = {q.n for q in questions if any(p.written or p.tappable for p in q.parts)}
        unopened = [st for st in step_buttons(questions) if st["n"] not in has_open]
        # A step question is not on the sheet: what it asks for changes with
        # every submit, so each step is asked for in chat, after the rest.
        targets = {n: v for n, v in targets.items() if n not in stepped}
        typed = [b for b in typed if _dom_of(b) not in {q.dom_id for q in questions if q.n in stepped}]
        multi = [n for n in multi if n not in stepped]
        # Nothing to write, tap or begin: an essay, or a shape not seen yet.
        on_page = [q.n for q in questions
                   if q.n not in targets and not any(p.tappable or p.kind == "step"
                                                     for p in q.parts)]
        taps = [{"n": q.n, "name": p.name, "kind": p.kind, "stem": q.stem,
                 "options": [{"value": o.value, "label": o.label, "image": o.image}
                             for o in p.options]}
                for q in questions for p in q.parts if p.tappable]
        self.cs.math = MathSession(stage="shoot", dep=dep, title=title,
                                   targets=targets, multipart=multi, taps=taps,
                                   typed=typed, steps=unopened, stepped=stepped,
                                   item_id=str(getattr(item, "id", "") or ""))
        self._record_progress(dep, title, whole, self.cs.math.item_id)
        for q in questions:                          # a step already answered stays on
            if q.n in stepped:                       # the page with ✓: done, not open
                gr = [p.name for p in q.parts if p.graded]
                if gr:
                    self.cs.math.done[q.n] = gr
        log.info("math: dep=%s %d questions, blanks=%s", dep, len(questions),
                 {n: len(f) for n, f in targets.items()})
        # Codes are positions in a list that changes as things fall due: last
        # night's B1 was Module Quiz 10, this morning's is 7.4 Homework. So say
        # which code this was and when it is due, where it cannot be missed.
        tag = f"{item.code} · " if item is not None else ""
        due = f" · due {esc(item.due)}" if item is not None else ""
        self._send(f"<b>{tag}{esc(title)}</b> — " + (f"redo {', '.join(map(str, sorted(only)))}" if only
                                                     else f"{len(questions)} questions") + due)
        # Step questions go last: they are answered after everything else.
        order = sorted(range(len(questions)), key=lambda i: (questions[i].n in stepped, i))
        ms_ = self.cs.math
        ms_.shot_msgs = self._send_questions([questions[i] for i in order], [shots[i] for i in order])
        ms_.again = bool(only)                       # Redo, a typed change, worked on before
        if taps:
            self._send_taps()
        for st in unopened:
            self._send_step(st)
        tail = ("\n\n⚠️ " + "; ".join(f"{n} has {len(targets[n])} blanks" for n in multi)
                + " — write those answers on one line separated by commas, in the order "
                  "the blanks appear." if multi else "")
        tail += self._typed_note(targets, typed)
        if stepped:
            tail += ("\n\n▶ " + ", ".join(map(str, stepped)) + " goes step by step: only one step "
                     "is on the page at a time, so leave it off the sheet. Once the rest is "
                     "submitted I show each step and ask for it — typed, or a photo of just "
                     "that step.")
        if unopened:
            tail += ("\n\n▶ " + ", ".join(str(st["n"]) for st in unopened)
                     + " open one part at a time — tap Begin above when you get to it.")
        if on_page:
            tail += ("\n\n↗ " + ", ".join(map(str, on_page)) + " need doing on the page itself — "
                     "I can't fill those yet.")
        need = sorted(targets)
        if not need and not taps and stepped:
            # Only step questions here: nothing to photograph, start asking.
            self._send("Nothing to write for the sheet — this one goes step by step." + tail)
            self._math_next_part(self.cs.math, [])
            return
        if not need:
            # Everything here is a tap. Asking for a photo of nothing would be
            # asking the student to do work the buttons already did.
            return self._send("Everything here is tap-to-answer — no writing needed." + tail,
                              inline_keyboard([[self._fill_btn()],
                                               [("✖ Cancel", "cancel")]]))
        which = ("all the answers" if len(need) == len(questions)
                 else "the answers for " + ", ".join(str(n) for n in need))
        prompt = (f"Write {which} on <b>one sheet</b>, each with its number, then send me "
                  "<b>one photo</b>.\nI'll read it, show you what I got, and fill it in — "
                  "then you check the rendered answers and tap Submit." + tail)
        self._math_prompt = _html.unescape(re.sub(r"<[^>]+>", "", prompt))
        self._send(prompt)

    # -- /auto: TEST ONLY — the model plays the student ----------------------------
    #
    # A developer switch (AINL_AUTOSOLVE + /auto). It runs on its own thread,
    # one step at a time, so the chat keeps answering while it works — "/auto
    # off" used to wait behind four minutes of model calls (2026-09-25). Each
    # step is what the student would do next: get answers, Fill, Submit, redo
    # the wrong ones, answer the next step. The answers go through the same
    # preview / fill / picture / submit as a photographed sheet; only the taps
    # are made by the bot. Model calls happen outside the lock, so a stop is
    # answered at once and takes effect when that call returns.

    AUTO_REDO_ROUNDS = 2

    def _auto(self, arg: str) -> None:
        if self.cs.quiz is not None and arg not in ("off", "stop"):
            if not self.autosolve:
                return self._send("🧪 no solver configured.")
            return self._bg(self._quiz_dry_run, "quiz")
        ms = self.cs.math
        if arg in ("off", "stop"):
            self._auto_stop.set()
            if ms:
                ms.auto = False
            busy = self._auto_thread is not None and self._auto_thread.is_alive()
            return self._send("🧪 auto off" + (" — stopping after the step in progress" if busy else "")
                              + ". The round is yours again.")
        if not self.autosolve:
            return self._send("🧪 /auto is not available in this build.")
        if not ms or ms.stage == "pick":
            return self._send("Open an assignment first (/todo, then its code), then /auto.")
        if self._auto_thread is not None and self._auto_thread.is_alive():
            return self._send("🧪 auto is already running. /auto off to stop it.")
        ms.auto, ms.auto_retries = True, 0
        self._auto_stop.clear()
        self._send("🧪 <b>auto</b>: the model reads each question's picture and answers; I fill and "
                   "submit, step by step — as a test. /auto off stops it.")
        import threading
        self._auto_thread = threading.Thread(target=self._auto_loop, args=(ms,),
                                             name="auto", daemon=True)
        self._auto_thread.start()

    def _auto_live(self, ms) -> bool:
        return (not self._auto_stop.is_set() and ms.auto and self.cs.math is ms)

    def _auto_loop(self, ms) -> None:
        last = None
        try:
            for _ in range(60):                         # a hard ceiling on steps
                with self._lock:
                    if not self._auto_live(ms):
                        return
                    stage = (ms.stage, ms.fixing, len(ms.done), sum(map(len, ms.done.values())),
                             ms.auto_retries, self._auto_pending is not None, self._auto_repaired)
                    if stage == last:
                        self._send("🧪 auto: nothing changed after the last step — stopping.")
                        return
                    last = stage
                    job = (self._auto_repair_job(ms) if self._auto_pending is not None
                           else self._auto_next(ms))     # a local step, or a model request
                if job is None:
                    continue
                ask, apply = job
                try:
                    reply = ask()                        # the model call, outside the lock
                except Exception as e:                   # noqa: BLE001 — test harness
                    with self._lock:
                        self._send(f"🧪 solver failed: {esc(type(e).__name__)}: {esc(str(e))[:200]}")
                    return
                with self._lock:
                    if not self._auto_live(ms):
                        self._send("🧪 stopped — the model's last answer was not used.")
                        return
                    apply(reply)
            self._send("🧪 auto: step limit reached — stopping.")
        except Exception:
            log.exception("auto loop crashed")
            try:
                self._send("🧪 auto crashed — see the log. The round is yours.")
            except Exception:
                pass
        finally:
            ms.auto = False
            try:
                self.state.save(self._state_path) if self._state_path else None
            except Exception:
                pass

    def _auto_next(self, ms):
        """Under the lock: do the next local step, or return (ask, apply) for a
        model call. None means a local step was done."""
        if ms.stage == "shoot":
            return self._auto_sheet_job(ms)
        if ms.stage == "confirm":
            self._math_fill()
            return None
        if ms.stage == "filled":
            self._math_submit()
            return None
        if ms.stage == "fixing":
            return self._auto_step_job(ms, ms.fixing)
        if ms.stage == "graded":
            return self._auto_sheet_job(ms, redo=True)
        ms.auto = False
        return None

    def _auto_who(self) -> str:
        """Which model really answered, how long, how much it reasoned."""
        last = getattr(self.autosolve, "last", None)
        line = last.line() if last is not None else ""
        return f" ({esc(line)})" if line else ""

    def _auto_sheet_job(self, ms, redo: bool = False):
        from ..autosolve import Item, parse_answers
        self._auto_repaired = False
        if redo:
            wrong = sorted(n for n, v in ms.graded.items() if v == "wrong")
            if not wrong or ms.auto_retries >= self.AUTO_REDO_ROUNDS:
                ms.auto = False
                if wrong:
                    # the round is the student's again: the same two buttons
                    ms.redo = wrong
                    self._send(f"🧪 auto: done — still wrong after {ms.auto_retries} redo(s): "
                               + ", ".join(map(str, wrong)) + ".", inline_keyboard(
                                   [[(f"🔁 Redo {', '.join(map(str, wrong))}", f"mredo:{ms.dep}"),
                                     ("✅ Finish", f"mfinish:{ms.dep}")]]))
                else:
                    self._send("🧪 auto: done.")
                return None
            ms.auto_retries += 1
        try:
            _, qs, shots = self._wa().questions(ms.dep)
        except WebAssignUnavailable as e:
            self._send(f"⚠️ {esc(str(e))}")
            ms.auto = False
            return None
        want = set(wrong) if redo else ({n for n in ms.targets} | {t["n"] for t in ms.taps})
        want -= set(ms.stepped)                      # steps are asked one at a time, later
        if redo:
            # A submit clears the round's choice parts. A wrong choice question
            # is re-read from the page, so the model sees its option pictures
            # (and a picture sheet goes to the vision model) and its letters
            # have somewhere to go — without this a redo of 9.2 q3 got the
            # question picture alone, from the text model, and changed
            # nothing, twice (2026-09-27).
            fresh = [{"n": q.n, "name": p.name, "kind": p.kind, "stem": q.stem,
                      "options": [{"value": o.value, "label": o.label, "image": o.image}
                                  for o in p.options]}
                     for q in qs if q.n in want for p in q.parts if p.tappable]
            ms.taps = [t for t in ms.taps if t["n"] not in want] + fresh
            for t in fresh:
                ms.picked.pop(t["name"], None)
        items = []
        for q, shot in zip(qs, shots):
            if q.n not in want:
                continue
            boxes = ["number" if b in ms.typed else "maths" for b in ms.targets.get(q.n, [])]
            taps = [t for t in ms.taps if t["n"] == q.n]
            choices = [[o.get("label") or "" for o in self._tap_options(t)] for t in taps]
            items.append(Item(n=q.n, stem=q.stem, image=shot, boxes=boxes, choices=choices,
                              option_images=self._option_pictures(taps),
                              multi=[t["kind"] == "multi" for t in taps]))
        if not items:                                # only step questions: straight to them
            if ms.stepped and ms.stage == "shoot":
                self._math_submit()
                return None
            ms.auto = False
            return None
        wrong_answers = {n: ms.answers.get(n, []) for n in want} if redo else None
        self._send(f"🧪 asking the model{' to redo ' + ', '.join(map(str, sorted(want))) if redo else ''}"
                   f" — {len(items)} question picture(s)…")
        title, prompt, solver = ms.title, self._math_prompt, self.autosolve

        def ask():
            return solver.solve(title, items, prompt, wrong_answers)

        def apply(reply):
            self._send(f"🧪 model's answers{self._auto_who()}:\n<code>{esc((reply or '')[:1800])}</code>")
            got = parse_answers(reply or "")
            if got.empty():
                self._send("🧪 couldn't read any answer lines in that — stopping.")
                ms.auto = False
                return
            problems = self._auto_problems(ms, got, items)
            if problems and not self._auto_repaired:
                # Not filled yet: one short request for just the bad lines.
                self._auto_pending = (got, problems, redo, items, reply or "")
                self._send("🧪 not usable as written — " + "; ".join(
                    f"{k}: {esc(v)}" for k, v in problems.items()) + ". Asking for those again.")
                return
            if problems:
                self._send("🧪 still not usable after a repair: " + "; ".join(
                    f"{k}: {esc(v)}" for k, v in problems.items()) + " — filling the rest.")
            self._auto_apply(ms, got, redo)

        return ask, apply

    def _auto_problems(self, ms, got, items) -> dict[str, str]:
        """Line key ("1.2", "2.c1") -> why WebAssign would not take it."""
        from ..autosolve import box_problem
        out: dict[str, str] = {}
        for it in items:
            boxes = got.boxes.get(it.n, {})
            for k, kind in enumerate(it.boxes, 1):
                v = boxes.get(k, "")
                box = ms.targets.get(it.n, [""] * k)[k - 1]
                val = final_answer(v) if v else ""
                if box in ms.typed:
                    val = number_only(plain_text(val)) if box.startswith("RN_") else plain_text(val)
                why = box_problem(val, "number" if box.startswith("RN_") else "maths")
                if why:
                    out[f"{it.n}.{k}"] = why
            chosen = got.choices.get(it.n, {})
            for cidx, opts in enumerate(it.choices, 1):
                letters = chosen.get(cidx) or ""
                multi = cidx <= len(it.multi) and it.multi[cidx - 1]
                bad = [x for x in letters if not 0 <= ord(x) - 65 < len(opts)]
                if not letters:
                    out[f"{it.n}.c{cidx}"] = "no choice given"
                elif bad:
                    out[f"{it.n}.c{cidx}"] = f"no option {bad[0]} (A–{chr(64 + len(opts))})"
                elif len(letters) > 1 and not multi:
                    out[f"{it.n}.c{cidx}"] = "one letter only: this part takes a single answer"
        return out

    def _auto_repair_job(self, ms):
        """Ask once more for only the lines that would not go in as written."""
        from ..autosolve import parse_answers
        got, problems, redo, items, previous = self._auto_pending
        self._auto_pending = None
        self._auto_repaired = True
        wanted = {int(k.split(".")[0]) for k in problems}
        its = [i for i in items if i.n in wanted]
        title, prompt, solver = ms.title, self._math_prompt, self.autosolve

        def ask():
            return solver.solve(title, its, prompt, None, fix=problems, previous=previous)

        def apply(reply):
            self._send(f"🧪 model's repair{self._auto_who()}:\n<code>{esc((reply or '')[:1200])}</code>")
            fixed = parse_answers(reply or "")
            for n, by_box in fixed.boxes.items():
                got.boxes.setdefault(n, {}).update(by_box)
            for n, by_c in fixed.choices.items():
                got.choices.setdefault(n, {}).update(by_c)
            left = self._auto_problems(ms, got, items)
            if left:
                self._send("🧪 still not usable after the repair: " + "; ".join(
                    f"{k}: {esc(v)}" for k, v in left.items()) + " — filling the rest.")
            self._auto_apply(ms, got, redo)

        return ask, apply

    def _auto_apply(self, ms, got, redo: bool) -> None:
        """Put the model's answers where a photographed sheet's would go, box by box."""
        found: dict[int, list[str]] = {}
        for n, by_box in got.boxes.items():
            k = len(ms.targets.get(n, []))
            if not k or n in ms.stepped:
                continue
            found[n] = [by_box.get(i, "") for i in range(1, k + 1)]
            while found[n] and not found[n][-1]:
                found[n].pop()
        for n, by_choice in got.choices.items():
            parts = [(i, t) for i, t in enumerate(ms.taps) if t["n"] == n]
            for c, (i, t) in enumerate(parts, 1):
                opts = self._tap_options(t)
                picks = [opts[ord(x) - 65]["value"] for x in (by_choice.get(c) or "")
                         if 0 <= ord(x) - 65 < len(opts)]
                if picks:
                    # every letter of a "select all" part; one otherwise
                    ms.picked[t["name"]] = picks if t["kind"] == "multi" else picks[:1]
        self._math_sheet("", found=found)

    def _auto_step_job(self, ms, n: int):
        try:
            _, qs, _ = self._wa().questions(ms.dep)
        except WebAssignUnavailable as e:
            self._send(f"⚠️ {esc(str(e))}")
            ms.auto = False
            return None
        q = next((x for x in qs if x.n == n), None)
        part = next((p for p in (q.parts if q else []) if p.written and not p.graded
                     and p.name in ms.targets.get(n, []) and p.name not in ms.done.get(n, [])), None)
        if part is None:
            self._send(f"🧪 can't find the open step of {n} on the page — stopping.")
            ms.auto = False
            return None
        try:
            image = self._wa().step_shot(ms.dep, part.name)
        except Exception:                            # noqa: BLE001 — a picture is a help, not a must
            image = None
        earlier = list(ms.answers.get(n, [])[:len(ms.done.get(n, []))])
        kind = "a number" if part.name in ms.typed else "a maths expression"
        self._send(f"🧪 asking the model for {n} {esc(part.label or 'the open step')}…")
        title, solver, stem = ms.title, self.autosolve, q.stem

        def ask():
            return solver.solve_step(title, stem, part.text or part.label, part.label,
                                     earlier, image, kind)

        def apply(reply):
            from ..autosolve import box_problem
            self._send(f"🧪 model says <code>{esc(reply)}</code>{self._auto_who()}")
            val = final_answer(reply or "")
            if part.name in ms.typed:
                val = number_only(plain_text(val)) if part.name.startswith("RN_") else plain_text(val)
            why = box_problem(val, "number" if part.name.startswith("RN_") else "maths")
            if why:
                self._send(f"🧪 not usable as written ({esc(why)}) — stopping on this step.")
                ms.auto = False
                return
            if ms.stage == "fixing" and ms.fixing == n:
                self._math_apply_fix(reply)

        return ask, apply

    # -- Canvas quizzes, answered by the student in the chat --------------------
    #
    # canvas/quiz.py does the API. Here: show the questions, take the student's
    # answers (buttons for choices, "1C 2A …" for matching, "3. word" lines for
    # blanks), preview, and complete the attempt only on their Submit tap.
    # Nothing here writes an answer: /auto does not apply to quizzes.

    _QUIZ_ID_RE = re.compile(r"/quizzes/(\d+)")
    _PAIR_RE = re.compile(r"(\d{1,2})\s*[-–:=.)]?\s*([A-Za-z])\b")
    _LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[.):]\s*(.+?)\s*$")

    def _quiz_open(self, item) -> None:
        from ..canvas import quiz as cq
        qid = (self._QUIZ_ID_RE.search(item.url or "") or [None, item.id])[1]
        http = self.canvas._http
        try:
            st = cq.quiz(http, item.course_id, qid)
        except (CanvasHTTPError, KeyError) as e:
            return self._send(f"Couldn't read that quiz ({esc(str(e))[:120]}). The page:\n{esc(item.url)}")
        why = ("it needs LockDown Browser" if st.lockdown else "it needs an access code" if st.access_code
               else "it is locked" if st.locked else
               "it shows one question at a time, which Canvas won't let me read" if st.one_at_a_time else
               f"it has question types I can't take here ({', '.join(st.unsupported)})" if st.unsupported
               else "")
        if why:
            # Said BEFORE any attempt starts: nothing is used up.
            return self._send(f"<b>{esc(st.title)}</b> — {why}. Nothing was started. Take it in Canvas:\n"
                              f"{esc(item.url)}", self._open_elsewhere(item))
        try:
            open_one = any(s.get("workflow_state") == "untaken" for s in cq.attempts(http, item.course_id, qid))
        except CanvasHTTPError:
            open_one = False
        if st.risky and not open_one:
            rules = []
            if st.time_limit:
                rules.append(f"a {st.time_limit}-minute time limit that starts when it opens")
            if st.allowed_attempts not in (-1, 0):
                rules.append(f"{st.allowed_attempts} attempt(s) allowed")
            return self._send(f"📝 <b>{esc(st.title)}</b> has " + " and ".join(rules)
                              + ". Opening it here starts an attempt. Start it?",
                              inline_keyboard([[("Start it", f"qs:{item.course_id}:{qid}:{item.id}")],
                                               [("✖ No", "cancel")]]))
        self._quiz_start(item.course_id, qid, item.id, st)

    def _quiz_start(self, course_id: str, qid: str, item_id: str = "", st=None) -> None:
        from ..canvas import quiz as cq
        from .state import QuizSession
        http = self.canvas._http
        st = st or cq.quiz(http, course_id, qid)
        att = cq.attempt(http, course_id, qid)
        try:
            qs = cq.questions(http, att.id)
        except cq.QuestionsUnavailable as e:
            # The attempt exists now — say so plainly, and where to finish it.
            url = f"{self.canvas.base_url}/courses/{course_id}/quizzes/{qid}/take"
            return self._send(f"⚠️ <b>{esc(st.title)}</b>: {esc(str(e))}, and Canvas won't let me read "
                              f"those questions. <b>Your attempt has started</b>"
                              + (f" (time limit {st.time_limit} min)" if st.time_limit else "")
                              + f" — finish it in Canvas now:\n{esc(url)}")
        self.cs.quiz = QuizSession(course_id=course_id, quiz_id=qid, title=st.title, sub_id=att.id,
                                   attempt=att.attempt, token=att.token, points=st.points,
                                   unlimited=st.allowed_attempts in (-1, 0), item_id=item_id,
                                   questions=qs, intro=st.description)
        head = (f"📝 <b>{esc(st.title)}</b> — {len(qs)} question{'s' if len(qs) != 1 else ''}, "
                f"{st.points:g} pts" + (" · unlimited attempts, best score kept" if st.allowed_attempts in (-1, 0) else "")
                + (" · picking up the attempt you already started" if att.reused else ""))
        self._send(head)
        self._quiz_show()

    def _quiz_show(self) -> None:
        for text, kb in self._quiz_messages():
            self._send(text, kb)

    def _quiz_messages(self) -> list:
        """(text, keyboard) for every message that shows the questions."""
        out: list = []
        self._send, real = (lambda t, k=None: out.append((t, k))), self._send
        try:
            self._quiz_show_into()
        finally:
            self._send = real
        return out

    def _quiz_show_into(self) -> None:
        qz = self.cs.quiz
        if qz.intro:
            # The instructions can carry what the questions need (a word bank).
            self._send("<b>Instructions</b>\n" + esc(qz.intro[:3500]))
        typed = []
        for q in qz.questions:
            t = q["type"]
            body = f"<b>{q['n']}.</b> {esc(q['text'])}" if q["text"] else f"<b>{q['n']}.</b>"
            if t in ("multiple_choice_question", "true_false_question", "multiple_answers_question"):
                multi = t == "multiple_answers_question"
                long = any(len(a["text"]) > 28 for a in q["answers"])
                if long:
                    body += "\n" + "\n".join(f"<b>{chr(65 + i)}.</b> {esc(a['text'])}" for i, a in enumerate(q["answers"]))
                rows, row = [], []
                for i, a in enumerate(q["answers"]):
                    row.append((chr(65 + i) if long else a["text"][:28], f"{'qm' if multi else 'qa'}:{q['id']}:{a['id']}"))
                    if len(row) == (6 if long else 2):
                        rows.append(row); row = []
                if row:
                    rows.append(row)
                self._send(body + ("\n<i>Tap every one that applies.</i>" if multi else ""), inline_keyboard(rows))
            elif t == "matching_question":
                words = "\n".join(f"<b>{i}.</b> {esc(a['text'])}" for i, a in enumerate(q["answers"], 1))
                means = "\n".join(f"<b>{chr(65 + i)}.</b> {esc(m['text'])}" for i, m in enumerate(q["matches"]))
                self._send(body + f"\n\nMatch each of these:\n{words}\n\nwith one of these:\n{means}\n\n"
                           f"Reply with the pairs, like <code>1C 2A 3K</code> (any order; all at once or a few at a time)."
                           + (f" Several matching questions here — start with the question number: <code>{q['n']}: 1C 2A</code>."
                              if self._quiz_matchings() > 1 else ""))
            else:
                typed.append(q)
        if typed:
            lines = "\n\n".join(f"<b>{q['n']}.</b> {esc(q['text'])}" for q in typed)
            self._send(lines + "\n\nReply with one line per answer, starting with its number:\n"
                       f"<code>{typed[0]['n']}. your answer</code>\nAll in one message or a few at a time.")

    def _quiz_matchings(self) -> int:
        return sum(1 for q in self.cs.quiz.questions if q["type"] == "matching_question")

    def _quiz_text(self, text: str) -> None:
        qz = self.cs.quiz
        code = text.strip().upper().replace(" ", "")
        if _CODE_RE.match(code) and any(m.code == code for m in self.cs.menu):
            # A /todo code means "open that one", not an answer (2026-09-25).
            self.cs.quiz = None
            return self._open(code)
        by_n = {q["n"]: q for q in qz.questions}
        got = 0
        # matching pairs: "1C 2A …" (optionally "5: 1C 2A" to pick the question)
        matchings = [q for q in qz.questions if q["type"] == "matching_question"]
        head = re.match(r"^\s*(\d{1,2})\s*:\s*(.*)$", text, re.S)
        target, rest = (by_n.get(int(head.group(1))), head.group(2)) if head else (None, text)
        if not (target and target["type"] == "matching_question"):
            target, rest = (matchings[0] if len(matchings) == 1 else None), text
        def only_pairs(s: str) -> list:
            prs = self._PAIR_RE.findall(s)
            return prs if prs and not self._PAIR_RE.sub("", s).strip(" ,;\n\t") else []

        def match(tq, prs) -> int:
            current = {p["answer_id"]: p["match_id"] for p in (qz.answers.get(tq["id"]) or [])}
            n_ok = 0
            for num, letter in prs:
                i, j = int(num) - 1, ord(letter.upper()) - 65
                if 0 <= i < len(tq["answers"]) and 0 <= j < len(tq["matches"]):
                    current[tq["answers"][i]["id"]] = tq["matches"][j]["id"]
                    n_ok += 1
            qz.answers[tq["id"]] = [{"answer_id": a, "match_id": m} for a, m in current.items()]
            return n_ok

        if target and only_pairs(rest):
            got += match(target, only_pairs(rest))
        else:
            # Line by line: blanks and typed choices ("3. word", "2. B"), and —
            # in the same message — a line of pairs for the matching question.
            for line in text.splitlines():
                one = matchings[0] if len(matchings) == 1 else None
                if one and only_pairs(line) and not self._LINE_RE.match(line):
                    got += match(one, only_pairs(line))
                    continue
                m = self._LINE_RE.match(line)
                if not m:
                    continue
                q = by_n.get(int(m.group(1)))
                if q and q["type"] in ("short_answer_question", "numerical_question", "essay_question"):
                    qz.answers[q["id"]] = m.group(2).strip()
                    got += 1
                elif q and q["type"] in ("multiple_choice_question", "true_false_question",
                                         "multiple_answers_question"):
                    # Typed instead of tapped: a letter in page order, or the option's text.
                    picks = []
                    for tok in re.split(r"\s*[,;]\s*", m.group(2).strip()):
                        t = tok.strip().rstrip(".)").strip()
                        if len(t) == 1 and t.isalpha() and 0 <= ord(t.upper()) - 65 < len(q["answers"]):
                            picks.append(q["answers"][ord(t.upper()) - 65]["id"])
                        else:
                            hit = [a["id"] for a in q["answers"] if a["text"].strip().lower() == t.lower()]
                            picks += hit[:1]
                    if picks:
                        qz.answers[q["id"]] = picks if q["type"] == "multiple_answers_question" else picks[0]
                        got += 1
        if not got:
            return self._send("I couldn't match that to a question. Blanks: <code>3. your answer</code> "
                              "(one per line). Matching: <code>1C 2A</code>. /cancel to leave the quiz "
                              "(your attempt stays open on Canvas).")
        self._quiz_progress()

    def _quiz_missing(self) -> list[str]:
        qz = self.cs.quiz
        out = []
        for q in qz.questions:
            a = qz.answers.get(q["id"])
            if q["type"] == "matching_question":
                done = len(a or [])
                if done < len(q["answers"]):
                    out.append(f"{q['n']} ({len(q['answers']) - done} to match)")
            elif a in (None, "", []):
                out.append(str(q["n"]))
        return out

    def _quiz_progress(self) -> None:
        missing = self._quiz_missing()
        if missing:
            return self._send("Got it. Still to answer: " + ", ".join(missing) + ".",
                              inline_keyboard([[("👀 Review what I have", "qp:")]]))
        self._quiz_preview()

    def _quiz_preview(self) -> None:
        qz = self.cs.quiz
        qz.stage = "confirm"
        lines = [f"📝 <b>{esc(qz.title)}</b> — your answers:"]
        for q in qz.questions:
            a = qz.answers.get(q["id"])
            if q["type"] == "matching_question":
                mt = {m["id"]: m["text"] for m in q["matches"]}
                pairs = {p["answer_id"]: p["match_id"] for p in (a or [])}
                lines.append(f"<b>{q['n']}.</b>")
                for i, w in enumerate(q["answers"], 1):
                    lines.append(f"   {i}. {esc(w['text'])} → {esc(mt.get(pairs.get(w['id']), '—'))}")
            elif q["type"] in ("multiple_choice_question", "true_false_question"):
                at = {x["id"]: x["text"] for x in q["answers"]}
                lines.append(f"<b>{q['n']}.</b> {esc(at.get(str(a), '—'))}")
            elif q["type"] == "multiple_answers_question":
                at = {x["id"]: x["text"] for x in q["answers"]}
                lines.append(f"<b>{q['n']}.</b> " + (", ".join(esc(at[x]) for x in (a or []) if x in at) or "—"))
            else:
                lines.append(f"<b>{q['n']}.</b> {esc(str(a)) if a else '—'}")
        missing = self._quiz_missing()
        if missing:
            lines.append("\nNot answered: " + ", ".join(missing))
        lines.append("\nChange one by sending it again. Submit finishes the attempt"
                     + (" — you can take it again later; the best score is kept." if qz.unlimited else "."))
        self._send("\n".join(lines), inline_keyboard(
            [[("✅ Submit" + (" anyway" if missing else ""), f"qS:{qz.sub_id}")], [("✖ Cancel", "cancel")]]))

    def _quiz_callback(self, data: str) -> None:
        qz = self.cs.quiz
        if data.startswith("qs:"):
            _, course, qid, item_id = data.split(":", 3)
            return self._quiz_start(course, qid, item_id)
        if qz is None:
            return self._send("That quiz round is over. /todo to start again.")
        if data.startswith(("qa:", "qm:")):
            kind, qid, aid = data.split(":", 2)
            q = next((x for x in qz.questions if x["id"] == qid), None)
            if q is None:
                return self._send("That button is from another quiz. /todo to start again.")
            if kind == "qa":
                qz.answers[qid] = aid
            else:
                cur = list(qz.answers.get(qid) or [])
                cur.remove(aid) if aid in cur else cur.append(aid)
                qz.answers[qid] = cur
            at = {x["id"]: x["text"] for x in q["answers"]}
            chosen = [aid] if kind == "qa" else qz.answers[qid]
            self._send(f"<b>{q['n']}.</b> " + (", ".join(esc(at.get(x, "?")) for x in chosen) or "(nothing)") + " ✓")
            if not self._quiz_missing():
                self._quiz_preview()
            return
        if data == "qp:":
            return self._quiz_preview()
        if data.startswith("qS:"):
            if data != f"qS:{qz.sub_id}":
                return self._send("That Submit button is from another attempt — nothing was submitted.")
            return self._quiz_submit()

    def _quiz_submit(self) -> None:
        from ..canvas import quiz as cq
        qz = self.cs.quiz
        http = self.canvas._http
        att = cq.Attempt(id=qz.sub_id, attempt=qz.attempt, token=qz.token)
        payload = [{"id": qid, "answer": a} for qid, a in qz.answers.items()]
        self._send("Submitting…")
        try:
            if payload:
                cq.save_answers(http, att, payload)
            done = cq.complete(http, qz.course_id, qz.quiz_id, att)
        except CanvasHTTPError as e:
            return self._send(f"⚠️ Canvas didn't take it: {esc(str(e))[:300]}\n"
                              "Your answers are saved on the attempt; nothing was lost. Try Submit again.")
        score = done.get("score")
        kept = done.get("kept_score")
        line = (f"✅ Submitted <b>{esc(qz.title)}</b>: {score:g} / {qz.points:g}"
                if isinstance(score, (int, float)) else f"✅ Submitted <b>{esc(qz.title)}</b>.")
        if isinstance(kept, (int, float)) and kept != score:
            line += f" (best so far: {kept:g})"
        if qz.unlimited:
            line += "\nYou can take it again from /todo; the best score is kept."
        self.cs.quiz = None
        self._send(line)


    # -- test coverage: a model plays the student up to Submit, never past it ----
    #
    # /sweep walks every code on the current /todo list; /auto on a quiz does
    # the same for that one quiz. The point is to find what the bot does not
    # handle yet — a question type, a box it cannot see, a reply it cannot
    # parse — not to get anything done, so nothing is filled into WebAssign and
    # nothing is submitted anywhere. Findings go to the chat and to
    # ~/.ainl/sweeps/<time>.md for the developer.

    def _bg(self, fn, name: str) -> None:
        if self._auto_thread is not None and self._auto_thread.is_alive():
            return self._send("🧪 something is already running. /auto off to stop it.")
        self._auto_stop.clear()
        import threading

        def run():
            try:
                fn()
            except Exception:
                log.exception("%s crashed", name)
                try:
                    self._send(f"🧪 {name} crashed — see the log.")
                except Exception:
                    pass
            finally:
                try:
                    with self._lock:
                        if self._state_path:
                            self.state.save(self._state_path)
                except Exception:
                    pass
        # Carry the caller's context onto the thread: it holds WHOSE browser
        # worker to use (browserthread.using). A bare Thread starts empty, so a
        # classmate's background job ran on the shared default worker.
        import contextvars
        ctx = contextvars.copy_context()
        self._auto_thread = threading.Thread(target=lambda: ctx.run(run), name=name, daemon=True)
        self._auto_thread.start()

    # -- /check: what works on every platform, without writing anything --------

    _WALL = re.compile(r"\b(sign in|log ?in|password|purchase|payment|buy access|subscribe|"
                       r"access code|free trial|enter your email)\b", re.I)

    def _check(self, arg: str) -> None:
        """Open every /todo item the way I would to help, read it, report what
        works — and never fill, answer or submit anything, and never start a
        quiz attempt. Safe for anyone: it is how the platforms students really
        have get covered (the sweep, by contrast, has a model fill answers)."""
        if arg in ("off", "stop"):
            self._auto_stop.set()
            return self._send("🔍 check: stopping after the current item.")
        if not self.cs.menu:
            return self._send("Run /todo first (or /todo 14): the check walks that list.")
        items = [it for it in self.cs.menu if not it.done]
        self._send(f"🔍 <b>check</b>: {len(items)} item(s). I open each one the way I would to help you, "
                   "see whether I can work there, and report. Nothing is filled in, answered or handed "
                   "in, and no quiz is started. /check off stops it.")
        self._bg(lambda: self._check_run(items), "check")

    def _check_run(self, items) -> None:
        from ..config import home
        from ..util import metrics
        report, found = [], []
        for k, it in enumerate(items, 1):
            if self._auto_stop.is_set():
                report.append("(stopped)")
                break
            t0 = time.monotonic()
            try:
                lines = self._check_one(it)
            except Exception as e:                       # noqa: BLE001 — the report is the point
                log.exception("check %s", it.code)
                lines = [f"CRASH {type(e).__name__}: {str(e)[:200]}"]
            finally:
                with self._lock:
                    self._drop_everything(keep_menu=True)
            metrics.emit("check", code=it.code, item_kind=it.kind, s=round(time.monotonic() - t0, 1),
                         ok=not any(x.startswith(("CRASH", "✗")) for x in lines))
            report += [f"## {it.code} · {it.course} · {it.title} — kind {it.kind}"] + [f"- {x}" for x in lines] + [""]
            found.append(f"{it.code} {_clip(it.title, 32)}: " + "; ".join(lines)[:160])
            self._send(f"🔍 [{k}/{len(items)}] <b>{esc(it.code)}</b> — " + esc("; ".join(lines)[:300]))
        d = home() / "checks"
        d.mkdir(exist_ok=True)
        path = d / f"{self.chat_id}-{datetime.now(self.tz).strftime('%Y%m%d-%H%M')}.md"
        path.write_text("\n".join(report), encoding="utf-8")
        bad = [f for f in found if "✗" in f or "CRASH" in f]
        self._send("🔍 <b>check done</b> — " + (f"{len(bad)} thing(s) I can't do yet:\n" + "\n".join(esc(x) for x in bad[:20])
                                                if bad else "everything opened as expected.")
                   + "\nThanks — this tells the bot's owner what to fix next.")

    def _check_one(self, it) -> list[str]:
        """One item, read-only. Its messages to the chat are held back: the
        check reports one line per item instead of the whole item."""
        if it.kind == "quiz":
            return ["Canvas quiz — not opened (opening can start an attempt)"]
        with self._lock:
            real, self.tg = self.tg, _Muted(self.tg)
            try:
                self._drop_everything(keep_menu=True)
                self._open(it.code)
                ms, pending = self.cs.math, self.cs.pending
                said = self.tg.said
            finally:
                self.tg = real
        if ms is not None and ms.stage == "shoot":
            return self._check_webassign(ms)
        if it.kind == "webassign":
            return ["✗ WebAssign did not open: " + _clip(" / ".join(said[-2:]), 160)]
        if it.kind in ("assignment", "discussion") or pending is not None:
            return [f"Canvas item ({', '.join(it.types) or it.kind}) — I can hand this in here ✓"]
        if it.kind == "gradescope":
            return ["Gradescope — upload works through your own session ✓"]
        return self._check_external(it)

    def _check_webassign(self, ms) -> list[str]:
        with self._lock:
            _, qs, _ = self._wa().questions(ms.dep)
        kinds = sorted({p.kind for q in qs for p in q.parts})
        out = [f"WebAssign: {len(qs)} question(s), parts: {', '.join(kinds) or '?'}"]
        odd = [q.n for q in qs if any(p.kind in ("unknown", "essay") for p in q.parts)]
        if odd:
            out.append(f"✗ parts I can't fill yet in: {', '.join(map(str, odd))}")
        if ms.stepped:
            out.append(f"step-by-step questions: {', '.join(map(str, ms.stepped))}")
        if not odd:
            out.append("✓ everything here is fillable")
        return out

    def _check_external(self, it) -> list[str]:
        """Launch the platform the way Canvas does, in THIS person's browser,
        and see where it lands. Only the landing host and whether it shows a
        sign-in or payment wall are kept — never the page's text."""
        from urllib.parse import urlsplit
        from ..external.platforms import classify
        from ..util import browserthread, pw
        plat = classify(it.tool or "")
        name = plat.label if plat is not None else (urlsplit(it.tool).hostname if it.tool else "unknown tool")
        try:
            url = self.canvas.sessionless_launch(it.course_id, it.id)
        except Exception as e:
            return [f"✗ {name}: Canvas gave no launch link ({type(e).__name__})"]
        cdp = self._wa().cdp_url

        def go():
            with pw.connect(cdp) as browser:
                page = browser.contexts[0].new_page()
                try:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    except Exception:
                        pass                               # auto-submitting LTI forms interrupt goto
                    page.wait_for_timeout(5000)
                    text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''")
                    return urlsplit(page.url).hostname or "?", bool(self._WALL.search(text or ""))
                finally:
                    page.close()
        try:
            host, wall = browserthread.run(go, budget=60)
        except Exception as e:
            return [f"✗ {name}: launch did not finish ({type(e).__name__})"]
        if wall:
            return [f"✗ {name}: lands on {host} but asks to sign in / pay — I can't work there yet"]
        return [f"{name}: lands on {host} signed in — a candidate for the bot to learn next"]

    def _quiz_dry_run(self) -> list[str]:
        """The model reads the quiz as sent to the chat and types a reply; it
        goes through the same parser as the student's. Stops at the preview."""
        with self._lock:
            qz = self.cs.quiz
            if qz is None:
                return ["no quiz open"]
            msgs = []
            for text, kb in self._quiz_messages():
                plain = _html.unescape(re.sub(r"<[^>]+>", "", text))
                if kb:
                    opts = [b["text"] for row in kb["inline_keyboard"] for b in row]
                    plain += "\n[buttons: " + " | ".join(opts) + "]"
                msgs.append(plain)
            title = qz.title
        self._send("🧪 dry run: the model answers this quiz the way a student would type it — "
                   "up to the preview, never submitted.")
        reply = self.autosolve.quiz_reply(title, msgs)
        issues: list[str] = []
        with self._lock:
            if self.cs.quiz is None or self._auto_stop.is_set():
                return ["stopped"]
            self._send(f"🧪 model typed{self._auto_who()}:\n<code>{esc(reply[:1500])}</code>")
            before = dict(self.cs.quiz.answers)
            self._quiz_text(reply)
            qz = self.cs.quiz
            missing = self._quiz_missing()
            if missing:
                issues.append("not understood / not answered: " + ", ".join(missing))
            if qz.answers == before:
                issues.append("the reply changed nothing")
            types = sorted({q["type"] for q in qz.questions})
            self._send("🧪 dry run done — " + ("; ".join(issues) if issues else "every question was understood")
                       + ". Nothing was submitted."
                       + ("" if getattr(self, "_sweeping", False) else " Tap Submit yourself, or /cancel."))
        return [f"types: {', '.join(types)}"] + issues

    def _sweep(self, arg: str) -> None:
        if arg in ("off", "stop"):
            self._auto_stop.set()
            return self._send("🧪 sweep: stopping after the current item.")
        if not self.autosolve:
            return self._send("🧪 /auto is not available in this build.")
        if not self.cs.menu:
            return self._send("Run /todo first (or /todo 14): the sweep walks that list.")
        items = list(self.cs.menu)
        import os
        skip = {s.strip().upper() for s in
                os.environ.get("AINL_SWEEP_SKIP", "SPANISH101").split(",") if s.strip()}
        n = sum(1 for it in items if (it.course or "").upper() not in skip and not it.done)
        self._send(f"🧪 <b>sweep</b>: {n} items to check, one at a time, up to Submit and never past it"
                   + (f" (skipping {', '.join(sorted(skip))})" if skip else "") + ". /sweep off stops it.")
        self._bg(lambda: self._sweep_run(items), "sweep")

    def _sweep_run(self, items) -> None:
        self._sweeping = True
        self._sweep_started = datetime.now(self.tz) - timedelta(minutes=1)
        try:
            self._sweep_items(items)
        finally:
            self._sweeping = False

    def _sweep_items(self, items) -> None:
        report: list[str] = []
        # Courses the student does not want swept (SPANISH101: its own site works
        # well; 2026-09-25). Not opened, no messages, one line in the report.
        import os
        skip = {s.strip().upper() for s in
                os.environ.get("AINL_SWEEP_SKIP", "SPANISH101").split(",") if s.strip()}
        skipped = [it for it in items if (it.course or "").upper() in skip]
        if skipped:
            report += [f"## skipped courses: {', '.join(sorted(skip))} — {len(skipped)} item(s) not swept", ""]
        items = [it for it in items if it not in skipped]
        for it in [x for x in items if x.done]:          # finished: in the report, not in the chat
            report += [f"## {it.code} · {it.course} · {it.title} — kind {it.kind}",
                       f"- done ({it.done}) — skipped", ""]
        items = [it for it in items if not it.done]
        total = len(items)
        pause = float(os.environ.get("AINL_SWEEP_PAUSE", "3"))
        for k, it in enumerate(items, 1):
            if k > 1 and pause > 0 and not self._auto_stop.wait(pause):
                pass                                     # a breath between items, so each reads as one block
            if self._auto_stop.is_set():
                report.append("\n(stopped)")
                break
            if it.done:
                report += [f"## {it.code} · {it.course} · {it.title} — kind {it.kind}",
                           f"- done ({it.done}) — skipped", ""]
                continue
            # One item at a time, strictly in list order; but in the chat the
            # pictures, buttons and model replies of consecutive items ran into
            # each other (2026-09-25). A clear start and end line per item.
            self._send(f"━━━━━━━━━━\n🧪 <b>[{k}/{total}] {esc(it.code)}</b> · {esc(it.course)} · "
                       f"{esc(_clip(it.title, 50))}")
            head = f"## {it.code} · {it.course} · {it.title} — kind {it.kind}"
            lines: list[str] = []
            from ..util import metrics
            t_item = time.monotonic()
            metrics.emit("sweep", phase="start", code=it.code, course=it.course, item_kind=it.kind)
            try:
                lines = self._sweep_one(it)
            except Exception as e:                       # noqa: BLE001 — the report is the point
                log.exception("sweep %s", it.code)
                lines = [f"CRASH {type(e).__name__}: {str(e)[:200]}"]
            finally:
                with self._lock:
                    self._drop_everything(keep_menu=True)
            metrics.emit("sweep", phase="end", code=it.code, course=it.course, item_kind=it.kind,
                         s=round(time.monotonic() - t_item, 1), crashed=any(x.startswith("CRASH") for x in lines))
            report += [head] + [f"- {x}" for x in lines] + [""]
            stopped = any("filled and saved" in x or "dry run" in x or "types:" in x for x in lines)
            self._send(f"⏹ <b>{esc(it.code)}</b> — "
                       + ("stopped before Submit. " if stopped else "")
                       + esc("; ".join(lines)[:400] or "nothing to report"))
        from ..config import home
        d = home() / "sweeps"
        d.mkdir(exist_ok=True)
        path = d / (datetime.now(self.tz).strftime("%Y%m%d-%H%M") + ".md")
        path.write_text("\n".join(report), encoding="utf-8")
        # An item that was already done is skipped on purpose ("- done (1/2) —
        # skipped"); counting it made three "findings" out of two finished
        # quizzes on 2026-09-25 and buried the one real gap.
        issues = [ln for ln in report if ln.startswith("- ") and not ln.startswith("- done (") and any(w in ln for w in
                  ("CRASH", "mismatch", "not usable", "not understood", "unsupported", "changed nothing",
                   "could not", "skipped"))]
        self._send("🧪 <b>sweep done</b> — " + (f"{len(issues)} finding(s):\n" + "\n".join(esc(x) for x in issues[:30])
                                               if issues else "nothing wrong found by the checks.")
                   + f"\nFull report: <code>{esc(str(path))}</code>")
        if self._start_review(path):
            self._send("🔎 A Claude Code review of this sweep is open in a <b>Terminal window on the Mac</b>: "
                       "watch it there, approve the commands it asks to run, Esc to stop it. It messages "
                       "you here when it is done.")

    def _start_review(self, report) -> bool:
        """After a sweep: open a VISIBLE Claude Code session to review it.

        It runs interactively in a Terminal window on this Mac, not headless:
        the student watches what it reads and changes, approves the commands it
        runs (restarting the bot among them), and can stop it with Esc or talk
        to it at any time (2026-09-25 — a headless run could not be supervised).
        File edits are accepted automatically; everything else asks. Off with
        AINL_SWEEP_REVIEW=0.
        """
        import os
        import shutil
        import subprocess
        # Off unless asked for (2026-09-25): the owner reads the sweep report and
        # tells the developer when to review it; nothing starts on its own.
        if os.environ.get("AINL_SWEEP_REVIEW", "0") != "1":
            return False
        exe = os.environ.get("AINL_CLAUDE") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        if not Path(exe).exists():
            log.info("no claude CLI at %s — no review", exe)
            return False
        from ..config import home
        repo = Path(__file__).resolve().parents[3]
        stamp = datetime.now(self.tz).strftime("%Y%m%d-%H%M")
        since = getattr(self, "_sweep_started", datetime.now(self.tz)).isoformat(timespec="seconds")
        tmpl = repo / "scripts" / "sweep_review_prompt.md"
        if not tmpl.exists():                        # not shipped in the public build
            log.info("no %s — no review", tmpl)
            return False
        prompt = tmpl.read_text(encoding="utf-8").format(report=report, since=since, stamp=stamp)
        d = home() / "reviews"
        d.mkdir(exist_ok=True)
        (d / f"{stamp}.prompt.md").write_text(prompt, encoding="utf-8")
        script = d / f"{stamp}.command"
        script.write_text(
            "#!/bin/zsh\n"
            f"cd {str(repo)!r}\n"
            f"echo 'ainl sweep review — {stamp}. Esc stops it; type to talk to it.'\n"
            f"exec {str(exe)!r} -n 'ainl-sweep-review-{stamp}' --permission-mode acceptEdits "
            f"\"$(cat {str(d / (stamp + '.prompt.md'))!r})\"\n", encoding="utf-8")
        script.chmod(0o755)
        try:
            subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(script)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            log.info("sweep review opened in Terminal: %s", script)
            return True
        except Exception as e:                       # noqa: BLE001 — a review is extra, never fatal
            log.warning("could not open the review: %s", e)
            return False

    def _sweep_one(self, it) -> list[str]:
        with self._lock:
            self._drop_everything(keep_menu=True)
            self._open(it.code)
            ms, qz, pending = self.cs.math, self.cs.quiz, self.cs.pending
        if qz is not None:
            return self._quiz_dry_run()
        if ms is not None and ms.stage == "shoot":
            prog = self.cs.wa_progress.get(ms.dep, {})
            if prog.get("complete"):
                return [f"done on WebAssign ({prog['points']:g}/{prog['max']:g}) — skipped; /todo now shows it as done"]
            return self._sweep_webassign(ms)
        if it.kind == "webassign":
            return ["skipped: did not open (a quiz that asks first, or WebAssign unavailable)"]
        if it.kind == "quiz":
            return ["skipped: the quiz asks before starting (timed / limited attempts) or can't be taken here"]
        if pending is not None:
            return [f"Canvas item, hand-in types: {', '.join(pending.types) or '?'} — opened, nothing sent"]
        return ["link only (external platform the bot cannot work yet)"]

    def _sweep_webassign(self, ms) -> list[str]:
        from ..autosolve import Item, parse_answers
        out: list[str] = []
        with self._lock:
            _, qs, shots = self._wa().questions(ms.dep)
            kinds = sorted({p.kind for q in qs for p in q.parts})
            out.append(f"{len(qs)} questions; part kinds: {', '.join(kinds)}")
            odd = [q.n for q in qs if any(p.kind in ("unknown", "essay") for p in q.parts)]
            if odd:
                out.append(f"unsupported parts (unknown/essay) in: {', '.join(map(str, odd))}")
            if ms.stepped:
                out.append(f"step questions: {', '.join(map(str, ms.stepped))} (answered one step at a time; not swept)")
            items = []
            for q, shot in zip(qs, shots):
                if q.n in ms.stepped or not (q.n in ms.targets or any(t['n'] == q.n for t in ms.taps)):
                    continue
                boxes = ["number" if b.startswith("RN_") or b in ms.typed else "maths" for b in ms.targets.get(q.n, [])]
                taps = [t for t in ms.taps if t["n"] == q.n]
                choices = [[o.get("label") or "" for o in self._tap_options(t)] for t in taps]
                items.append(Item(n=q.n, stem=q.stem, image=shot, boxes=boxes, choices=choices,
                                  option_images=self._option_pictures(taps),
                                  multi=[t["kind"] == "multi" for t in taps]))
            title = ms.title
        if not items:
            return out + ["nothing to answer outside step questions"]
        reply = self.autosolve.solve(title, items, self._math_prompt, report_seen=True)
        got = parse_answers(reply)
        out.append(f"model{self._auto_who()}")
        for it in items:
            seen = got.seen.get(it.n)
            if seen is None:
                out.append(f"q{it.n}: model gave no 'seen' line")
            elif seen != (len(it.boxes), len(it.choices)):
                out.append(f"q{it.n}: mismatch — bot found {len(it.boxes)} box(es), {len(it.choices)} choice part(s); "
                           f"model sees {seen[0]} box(es), {seen[1]} choice part(s)")
        with self._lock:
            # What the model answered, as a student's sheet would read.
            shown = "\n".join(ln for ln in (reply or "").splitlines() if ".seen" not in ln)
            self._send(f"🧪 DeepSeek's answers{self._auto_who()}:\n<code>{esc(shown[:1800])}</code>")
            problems = self._auto_problems(ms, got, items)
            for k, why in problems.items():
                out.append(f"{k}: not usable — {why}")
            if not problems and all(got.seen.get(i.n) == (len(i.boxes), len(i.choices)) for i in items):
                out.append("every question: boxes and choices agree with the picture; every answer is fillable")
            # Fill what can be filled — WebAssign saves it, nothing is submitted
            # (the student asked for the round to go right up to Submit).
            if self.cs.math is ms and not self._auto_stop.is_set():
                ms.auto = True                    # quiet: no Fill/Submit prompts; never submits by itself
                try:
                    self._auto_apply(ms, got, False)
                    if ms.stage == "confirm":
                        self._math_fill()
                finally:
                    ms.auto = False
                filled = len(ms.filled)
                out.append(f"filled and saved on WebAssign: {filled} box(es)"
                           + (f" + {len(ms.picked)} choice(s)" if ms.picked else "") + " — not submitted")
        return out


    # -- /stats: how busy the bot is, and what it costs ------------------------------

    def _stats(self) -> None:
        from datetime import timedelta as _td

        from ..util import metrics
        try:
            snap = metrics.snapshot()
        except Exception as e:                       # noqa: BLE001
            return self._send(f"Couldn't read the machine: {esc(str(e))[:200]}")
        recent = metrics.read(since=datetime.now().astimezone() - _td(minutes=10))
        lines = ["<b>Now</b>"]
        b = snap["bot"]
        lines.append(f"bot: {b.get('footprint_mb') or b['rss_mb']:g} MB · {b['threads']} threads")
        for k, v in sorted(snap["browsers"].items()):
            lines.append(f"browser {esc(k)}: {v['footprint_mb'] or v['rss_mb']:g} MB · {v['procs']} processes "
                         f"({v['renderers']} pages) · CPU {v['cpu']:g}%")
        if not snap["browsers"]:
            lines.append("browser: not running")
        mch = snap["machine"]
        lines.append(f"machine: {mch['mem_used_mb'] / 1024:.1f} / {mch['mem_total_mb'] / 1024:.0f} GB used "
                     f"({mch['mem_pct']:g}%) · load {mch['load1']:g}")
        ups = [r for r in recent if r["kind"] == "update"]
        lags = [r["lag_s"] for r in ups if r.get("lag_s") is not None]
        lines.append("\n<b>Last 10 minutes</b>")
        if ups:
            lines.append(f"messages handled: {len(ups)} · waited p50 {metrics.pct(lags, 50) or 0:g}s, "
                         f"worst {max(lags) if lags else 0:g}s · handling p95 "
                         f"{metrics.pct([r['handle_s'] for r in ups], 95):g}s")
        else:
            lines.append("no messages handled")
        for svc in ("telegram", "canvas", "deepseek"):
            calls = [r for r in recent if r["kind"] == "http" and r.get("svc") == svc
                     and r.get("op") != "getUpdates"]           # the long poll waits on purpose
            if calls:
                bad = sum(1 for r in calls if (r.get("status") or 0) >= 400)
                rl = [r["rate_left"] for r in calls if r.get("rate_left")]
                lines.append(f"{svc}: {len(calls)} calls · p50 {metrics.pct([r['ms'] for r in calls], 50):g} ms · "
                             f"p95 {metrics.pct([r['ms'] for r in calls], 95):g} ms"
                             + (f" · {bad} errors" if bad else "")
                             + (f" · rate limit left {rl[-1]}" if rl else ""))
        steps = [r for r in recent if r["kind"] == "step"]
        if steps:
            slow = sorted(steps, key=lambda r: -r["s"])[:3]
            lines.append("slowest browser steps: " + ", ".join(f"{esc(r['name'])} {r['s']:g}s" for r in slow))
        llm = [r for r in recent if r["kind"] == "llm"]
        if llm:
            lines.append(f"model: {len(llm)} calls · {sum(r['prompt'] for r in llm)} in · "
                         f"{sum(r['reasoning'] for r in llm)} reasoning · {sum(r['output'] for r in llm)} out tokens")
        lines.append("\nFull numbers: <code>scripts/metrics_report.py</code>")
        self._send("\n".join(lines))


    def _auto_begin(self, wa, dep: str, questions: list, shots: list):
        """A Master It question shows nothing until its first part is begun.

        Beginning is not answering — no attempt is spent, and nothing can be
        skipped, since only a question with NO open part is begun — so it is
        done here, the way a student reading top to bottom would, rather than
        asked for. Returns the lists with those questions re-read, plus
        (number, label) of the parts begun and the numbers that would not open.
        """
        begun, stuck = [], []
        for i, q in enumerate(questions):
            st = next((p for p in q.parts if p.kind == "step"), None)
            if st is None or any(p.written or p.tappable for p in q.parts):
                continue
            try:
                q2, shot2 = wa.begin_step(dep, st.name)
            except WebAssignUnavailable as e:
                log.info("could not begin %s (%s): %s", q.n, st.name, str(e)[:80])
                stuck.append(q.n)
                continue
            questions[i], shots[i] = q2, shot2 or shots[i]
            begun.append((q.n, st.label))
        return questions, shots, begun, stuck

    @staticmethod
    def _step_label(questions, n: int) -> str:
        """The heading of the step now open on question n ("Step 5"), if any."""
        for q in questions:
            if q.n == n:
                return next((p.label for p in q.parts if p.written and p.label and not p.graded), "")
        return ""

    @staticmethod
    def _pending(ms: "MathSession", n: int) -> list[tuple[str, str]]:
        """(box, answer) pairs still to write for question n: the boxes not
        yet submitted, in page order, with the next values off the sheet —
        so a stepped question's (b) takes the second value, never the first."""
        done = ms.done.get(n, [])
        boxes = [b for b in ms.targets.get(n, []) if b not in done]
        return list(zip(boxes, ms.answers.get(n, [])[len(done):]))

    @staticmethod
    def _set_answer(ms: "MathSession", n: int, vals: list[str]) -> None:
        """A correction to a stepped question mid-way is for the OPEN part:
        the values already spent on submitted parts stay in front of it."""
        spent = len(ms.done.get(n, []))
        pending = len([b for b in ms.targets.get(n, []) if b not in ms.done.get(n, [])])
        if spent and len(vals) <= pending:
            vals = list(ms.answers.get(n, [])[:spent]) + list(vals)
        ms.answers[n] = list(vals)

    @staticmethod
    def _keep_all(ms: "MathSession", n: int, parts: list[str]) -> list[str]:
        """How much of the sheet's line for n to keep: all of it for a stepped
        question (later parts are not on the page yet), else one per blank."""
        return list(parts) if n in ms.stepped else list(parts[:len(ms.targets[n])])

    def _absorb_step(self, ms: "MathSession", q) -> list[str]:
        """Question n as the page shows it after a part opened: its boxes,
        typed boxes, taps and blanks again. Its NEXT step is not offered —
        the page only lets it begin once this part is submitted."""
        n = q.n
        boxes = math_targets([q]).get(n, [])
        ms.targets.pop(n, None)
        if boxes:
            ms.targets[n] = boxes
        ms.typed = [b for b in ms.typed if _dom_of(b) != q.dom_id] + typed_boxes([q])
        pending = [b for b in boxes if b not in ms.done.get(n, [])]
        ms.multipart = [m for m in ms.multipart if m != n] + ([n] if len(pending) > 1 else [])
        ms.taps = [t for t in ms.taps if t["n"] != n] + [
            {"n": n, "name": p.name, "kind": p.kind, "stem": q.stem,
             "options": [{"value": o.value, "label": o.label, "image": o.image}
                         for o in p.options]}
            for p in q.parts if p.tappable]
        ms.steps = [x for x in ms.steps if x["n"] != n]
        if n not in ms.stepped:
            ms.stepped.append(n)
        graded = [p.name for p in q.parts if p.graded and p.written]
        ms.done[n] = [b for b in ms.done.get(n, [])] + [b for b in graded if b not in ms.done.get(n, [])]
        wrong = [p.name for p in q.parts if p.wrong and p.written]
        if wrong:                            # ✗: the box stays for another try
            ms.done[n] = [b for b in ms.done.get(n, []) if b not in wrong]
        return boxes

    @staticmethod
    def _typed_note(targets: dict, typed: list[str]) -> str:
        """Say which blanks are typed as-is (a number, a word) rather than maths."""
        ns = sorted({n for n, boxes in targets.items() if any(b in typed for b in boxes)})
        if not ns:
            return ""
        return ("\n\n⌨️ " + ", ".join(map(str, ns)) + " (or one of its blanks) is a typed box — "
                "a number or a word, exactly as you'd type it. Write it on the sheet like the rest.")

    def _send_step(self, st: dict) -> None:
        """A "Master It" part: nothing to answer until its button is pressed —
        and the page forfeits any part that is skipped, so only the first
        unopened one is ever offered."""
        ms = self.cs.math
        label = st.get("label") or "the next part"
        self._send(
            f"<b>{st['n']}.</b> is a step-by-step question: its parts open one at a time, "
            f"each is answered on its own, and a skipped part can't be revisited.\n"
            f"Tap to open <b>{esc(label)}</b> — I'll show it, and you answer it like the others.",
            inline_keyboard([[(f"▶ Begin {st['n']} {label}", f"mstep:{ms.dep}:{st['id']}")]]))

    def _math_begin_step(self, dep: str, button_id: str) -> None:
        ms = self.cs.math
        st = next((x for x in (ms.steps if ms else []) if x["id"] == button_id), None)
        if not ms or dep != ms.dep or st is None:
            return self._send("That button is from a round that's over. /todo to start again.")
        self._send("Opening that part — a moment.")
        try:
            q, shot = self._wa().begin_step(ms.dep, button_id)
        except WebAssignUnavailable as e:
            return self._send(f"⚠️ {esc(str(e))}")
        n = q.n
        boxes = self._absorb_step(ms, q)
        if ms.stage == "filled":            # the page gained a part since the fill
            ms.stage = "confirm"
        if shot:
            try:
                self.tg.send_photo(self.chat_id, shot, caption=f"{n}. {st.get('label') or ''}".strip())
            except TelegramError as e:
                log.warning("sendPhoto step failed: %s", e)
        new_taps = [i for i, t in enumerate(ms.taps) if t["n"] == n]
        if new_taps:
            self._send_taps(only=new_taps)
        if not boxes and not new_taps:
            return self._send(f"<b>{n}.</b> opened, but I can't see a box I know how to fill — "
                              "do that part on the page.")
        how = (f"write {n}'s answer on the sheet with the others, or send "
               f"<code>{n}: …</code>" if boxes else "tap its answer above")
        multi = (f" It has {len(boxes)} blanks — one line, commas, in order." if len(boxes) > 1 else "")
        self._send(f"<b>{n}.</b> {esc(st.get('label') or 'part')} is open — {how}.{multi}"
                   + self._typed_note({n: boxes}, ms.typed))

    def _send_questions(self, questions, shots) -> dict[int, int]:
        """One album per 10 questions instead of one message per question:
        eight separate sends cost eight round trips, which was most of the
        wait between tapping an assignment and seeing it. Returns question
        number -> the message holding its picture (the filled picture later
        replaces it)."""
        pairs = [(shot, f"{q.n}.") for q, shot in zip(questions, shots) if shot]
        missing = [q.n for q, shot in zip(questions, shots) if not shot]
        where: dict[int, int] = {}
        for i in range(0, len(pairs), 10):
            batch = pairs[i:i + 10]
            try:
                ids = self.tg.send_media_group(self.chat_id, batch) or []
                for (_, caption), mid in zip(batch, ids):
                    where[int(caption.rstrip("."))] = int(mid)
            except TelegramError as e:
                log.warning("sendMediaGroup failed (%s); falling back one by one", e)
                for data, caption in batch:
                    try:
                        where[int(caption.rstrip("."))] = int(self.tg.send_photo(self.chat_id, data,
                                                                                 caption=caption))
                    except TelegramError:
                        missing.append(caption.rstrip("."))
        if missing:
            self._send(f"(no picture for {', '.join(str(n) for n in missing)} — "
                       "open those in the browser)")
        return where

    def _option_pictures(self, taps: list[dict]) -> list[list[Optional[bytes]]]:
        """Each option's own picture, for the model: the same crops the student
        is sent as "1. option A". A failed fetch is just a missing picture."""
        out = []
        for t in taps:
            row = []
            for o in self._tap_options(t):
                pic = None
                if o.get("image"):
                    try:
                        pic = self._fetch(o["image"])
                    except Exception:
                        log.info("option picture fetch failed for q%s", t.get("n"))
                row.append(pic)
            out.append(row)
        return out

    @staticmethod
    def _numbers_by_dom(ms) -> dict[str, int]:
        """question block id -> its number, from the typed boxes AND the tapped
        choices (a choice-only question has no box to learn it from)."""
        by_dom: dict[str, int] = {}
        for n, boxes in (ms.targets or {}).items():
            for b in boxes:
                d = _dom_of(b)
                if d:
                    by_dom.setdefault(d, int(n))
        for t in ms.taps or []:
            d = _dom_of(t.get("name") or "")
            if d:
                by_dom.setdefault(d, int(t["n"]))
        return by_dom

    @staticmethod
    def _verdict(points: Optional[str], marks: list) -> str:
        """right / partial / wrong / pending / ? — from the score WebAssign
        prints ("1 / 2 Points") first: its check-mark images differ between
        typed and choice questions, and choices came back as "?" (2026-09-27)."""
        m = re.search(r"([\d.]+)\s*/\s*([\d.]+)", points or "")
        if m:
            got, of = float(m.group(1)), float(m.group(2))
            if of > 0:
                return "right" if got >= of else "wrong" if got == 0 else "partial"
        if points and re.search(r"[–-]\s*/", points):
            return "pending"
        return ("wrong" if "wrong" in marks else "right" if marks and all(x == "right" for x in marks)
                else "?")

    @staticmethod
    def _shot_captions(ms, doms: list, count: int, what: str) -> list[str]:
        """"3. graded" for each picture, from the question block it is a crop of.
        Without it the filled/graded pictures arrived with no number at all
        (2026-09-26), and a two-question sheet looks like the same page twice."""
        by_dom = Conversation._numbers_by_dom(ms)
        caps = [f"{by_dom[d]}. {what}" if d in by_dom else what for d in doms]
        return (caps + [what] * count)[:count]

    @staticmethod
    def _tap_options(tap: dict) -> list[dict]:
        """The real choices: a dropdown's empty "-- select --" row is not one."""
        return [o for o in tap["options"]
                if tap["kind"] != "dropdown" or str(o.get("value", "")).strip() != ""]

    @staticmethod
    def _lettered(opts: list[dict]) -> bool:
        """Do the buttons need our own A/B/C?

        WebAssign prints no letters, and a button holds ~20 characters. Short
        labels ("convergent") go on the button as they are. Anything longer, or
        a picture, gets a letter WE assign in page order — top to bottom, the
        same order as in the screenshot — with the full text listed above.
        """
        return any(o.get("image") or not o.get("label") or len(o["label"]) > 18 for o in opts)

    def _tap_name(self, tap: dict, o: dict) -> str:
        opts = self._tap_options(tap)
        if self._lettered(opts):
            return chr(65 + opts.index(o))
        return o["label"]

    def _tap_label(self, tap: dict) -> str:
        """"3", or "3(a)" / "3(b)" when question 3 has more than one part to
        tap — two messages both headed "3." read as two question 3s (2026-09-27)."""
        ms = self.cs.math
        same = [t["name"] for t in (ms.taps if ms else []) if t["n"] == tap["n"]]
        if len(same) > 1 and tap["name"] in same:
            return f"{tap['n']}({'abcdefghij'[same.index(tap['name'])]})"
        return str(tap["n"])

    def _tap_keyboard(self, i: int) -> dict:
        ms = self.cs.math
        tap = ms.taps[i]
        chosen = ms.picked.get(tap["name"], [])
        opts = self._tap_options(tap)
        per_row = 4 if self._lettered(opts) else 2
        rows, row = [], []
        for o in opts:
            mark = "✅ " if o["value"] in chosen else ""
            # the dep rides along so a button from an older round cannot land
            # on this round's question of the same index
            row.append((mark + self._tap_name(tap, o)[:22], f"c:{ms.dep}:{i}:{o['value']}"))
            if len(row) == per_row:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return inline_keyboard(rows)

    def _send_taps(self, only: Optional[list[int]] = None) -> None:
        """Questions answered by tapping get buttons, not a place on the sheet.

        A multiple choice costs no writing and no model call. The buttons can
        be tapped at any time — before the photo, after it, twenty minutes
        apart: the picks are kept in the round's state, the tick is drawn on
        the button itself, and tapping another one changes the answer.
        """
        ms = self.cs.math
        for i, tap in enumerate(ms.taps):
            if only is not None and i not in only:
                continue
            opts = self._tap_options(tap)
            lines = [f"<b>{self._tap_label(tap)}.</b> "
                     + ("tap every one that applies" if tap["kind"] == "multi" else "tap your answer")]
            if self._lettered(opts):
                # Picture options are lettered ON the question's picture (red
                # badges, webassign.shoot_all) — not sent one photo each: twelve
                # photos for three questions buried the chat (2026-09-27).
                pics = [chr(65 + j) for j, o in enumerate(opts) if o.get("image")]
                for j, o in enumerate(opts):
                    if not o.get("image"):
                        lines.append(f"<b>{chr(65 + j)}.</b> {esc(o.get('label') or '')}")
                if pics:
                    span_ = pics[0] if len(pics) == 1 else f"{pics[0]}–{pics[-1]}"
                    lines.append(f"<i>The pictures are marked <b>{span_}</b> in red on question "
                                 f"{tap['n']}'s picture above.</i>")
                else:
                    lines.append("<i>Letters are mine, in the order they appear on the page.</i>")
            self._send("\n".join(lines), self._tap_keyboard(i))

    def _fetch(self, url: str) -> bytes:
        import httpx
        return httpx.get(url, timeout=20, follow_redirects=True).content

    def _tap(self, idx: int, value: str, dep: str = "", message_id: Optional[int] = None) -> None:
        ms = self.cs.math
        if not ms or idx >= len(ms.taps) or (dep and dep != ms.dep):
            return self._send("Those buttons are from a round that's over. /todo to start again.")
        tap = ms.taps[idx]
        chosen = ms.picked.setdefault(tap["name"], [])
        if tap["kind"] == "multi":
            chosen.remove(value) if value in chosen else chosen.append(value)
        else:
            ms.picked[tap["name"]] = [value]
        try:                                # tick it in place: no new message per tap
            if message_id is None:
                raise TelegramError("no message to edit")
            self.tg.edit_markup(self.chat_id, message_id, self._tap_keyboard(idx))
        except TelegramError:
            names = [self._tap_name(tap, o) for o in self._tap_options(tap)
                     if o["value"] in ms.picked[tap["name"]]]
            self._send(f"<b>{self._tap_label(tap)}.</b> {esc(', '.join(names)) or '(nothing)'} ✓")
        n = tap["n"]
        if ms.sheet and n not in ms.fixed and n in ms.targets:
            # convergent <-> divergent changes what belongs in the box; work it
            # out again from the sheet already read — no photo, no model call
            again = number_problems(ms.sheet).get(n)
            if again:
                ms.answers[n] = self._keep_all(ms, n, again)
            else:
                ms.answers.pop(n, None)
            self._choices_from_sheet(ms, ms.sheet, ms.answers, only=n)
        if ms.stage in ("confirm", "filled"):
            was_filled, ms.stage = ms.stage == "filled", "confirm"
            head = "Changed after filling — fill again so the page matches.\n\n" if was_filled else ""
            return self._send(head + self._math_preview(ms),
                              inline_keyboard([[self._fill_btn(), ("✖ Cancel", "cancel")]]))
        self._maybe_offer_fill()

    def _typed_choice(self, ms, n: int, part: Optional[int], letters: str) -> bool:
        """Apply "3(a) D" typed in the chat. False: not a choice question —
        let the text be read as a written answer instead.

        After a submit the round no longer holds question n's choices: it is
        opened again (like the Redo button), then the letters are applied."""
        taps = [t for t in ms.taps if t["n"] == n]
        reopened = False
        if not taps:
            if ms.stage != "graded":
                return False
            try:
                _, qs, _ = self._wa().questions(ms.dep)
            except WebAssignUnavailable:
                return False
            if not any(q.n == n and any(p.tappable for p in q.parts) for q in qs):
                return False
            item = next((m for m in self.cs.menu if ms.item_id and m.id == ms.item_id), None)
            self._math_open(ms.dep, item, only={n})
            ms, reopened = self.cs.math, True
            taps = [t for t in ms.taps if t["n"] == n] if ms else []
            if not taps:
                return True                          # reopened; its buttons are there to tap
        if part is None and len(taps) > 1:
            self._send(f"{n} has {len(taps)} parts — tap the buttons above, or type "
                       f"<code>{n}(a) {letters[0]}</code>.")
            return True
        if part is not None and not 0 <= part < len(taps):
            return False
        tap = taps[part or 0]
        opts = self._tap_options(tap)
        picks = [opts[ord(x) - 65]["value"] for x in letters if 0 <= ord(x) - 65 < len(opts)]
        if len(picks) != len(letters) or (len(picks) > 1 and tap["kind"] != "multi"):
            return False                             # not letters of this part: a written answer
        ms.picked[tap["name"]] = picks
        if ms.stage == "graded":
            self._send(f"<b>{self._tap_label(tap)}.</b> {', '.join(letters)} ✓")
            self._math_sheet("", found={})           # the tapped change, to fill and submit
        else:
            # Fill is offered at once: what was not touched keeps what the page
            # holds (typed "3(a) d" in a fresh round waited for a photo of 4
            # and 5 that were already done — 2026-09-27). mfill!: a part left
            # as it is on the page is not "half-answered".
            self._send(f"<b>{self._tap_label(tap)}.</b> {', '.join(letters)} ✓ — anything else, or fill "
                       "in what you've answered (the rest stays as it is on the page).",
                       inline_keyboard([[self._fill_btn(force=True)], [("✖ Cancel", "cancel")]]))
        return True

    def _show_filled(self, ms, shots: list, doms: list) -> None:
        """The filled pictures REPLACE the question pictures sent when the
        round opened (one set of pictures per round, not one per step);
        a question with no picture yet gets one sent."""
        for shot, cap in zip(shots, self._shot_captions(ms, doms, len(shots), "filled")):
            m = re.match(r"(\d+)\.", cap)
            mid = ms.shot_msgs.get(int(m.group(1))) if m else None
            try:
                if mid:
                    try:
                        self.tg.edit_photo(self.chat_id, mid, shot, caption=cap, filename="filled.png")
                        continue
                    except TelegramError as e:
                        log.info("edit picture %s failed (%s): sending it", mid, str(e)[:80])
                self.tg.send_photo(self.chat_id, shot, caption=cap, filename="filled.png")
            except TelegramError as e:
                log.warning("sendPhoto filled failed: %s", e)

    def _fill_btn(self, force: bool = False) -> tuple[str, str]:
        """The button that puts the answers on the page. On a second go it is
        Submit: fill and submit in one tap (the owner, 2026-09-27)."""
        ms = self.cs.math
        if ms is not None and ms.again:
            return ("✅ Submit", "mfill!+")
        return ("✅ Fill these in", "mfill!" if force else "mfill")

    def _maybe_offer_fill(self) -> None:
        """Once nothing is left to write, there is nothing to photograph."""
        ms = self.cs.math
        if ms.targets or ms.stage != "shoot":
            return
        if len(ms.picked) < len({t["name"] for t in ms.taps}):
            return
        self._send("That's all of them.", inline_keyboard(
            [[self._fill_btn()], [("✖ Cancel", "cancel")]]))

    def _math_photo(self, m: dict) -> None:
        ms = self.cs.math
        best = max(m["photo"], key=lambda p: p.get("file_size") or 0) if m.get("photo") else None
        fid = best["file_id"] if best else m["document"]["file_id"]
        try:
            _, data = self.tg.download(fid)
        except TelegramError as e:
            return self._send(f"Couldn't download that ({esc(str(e))[:100]}).")
        self._send("Reading it…")
        if ms.stage == "fixing":
            return self._math_photo_one(data)
        result = self.transcribe(data, hint=(
            "A handwritten answer sheet. Each answer begins with its question number "
            "(1., 2., …). Transcribe every answer verbatim; do not solve, simplify or correct."))
        if result.needs_human or not result.latex:
            errs = "; ".join(f"{k}: {v}" for k, v in result.errors.items())
            return self._send("I couldn't read that one." + (f"\n<code>{esc(errs)[:300]}</code>"
                                                            if errs else "")
                              + "\nTry a straighter, brighter photo.")
        self._math_sheet(result.latex)

    def _math_sheet(self, latex: str, found: Optional[dict] = None) -> None:
        """A whole sheet, read: match its lines to the questions and preview.

        After a submit (stage "graded") the sheet is a set of CORRECTIONS: a
        photo of the whole page with some answers changed, or of just the
        changed ones. Only questions whose answer differs from what was
        submitted are opened again; the rest stay as graded."""
        ms = self.cs.math
        if found is None:
            found = number_problems(latex)
        log.info("math: read %s; raw=%r", sorted(found), (latex or str(found))[:800])
        answers = {n: self._keep_all(ms, n, parts) for n, parts in found.items()
                   if n in ms.targets}
        if ms.stage == "graded":
            changed = [n for n, v in answers.items() if v != ms.answers.get(n)]
            for n in changed:
                if n not in ms.stepped:
                    ms.done.pop(n, None)             # to be written and submitted again
            answers = {**ms.answers, **{n: answers[n] for n in changed}}
            # choices tapped again after the submit are a change too (the
            # submit cleared the round's choice parts: any left were re-read)
            retapped = {t["n"] for t in ms.taps if ms.picked.get(t["name"]) and t["n"] not in ms.stepped}
            if not changed and not retapped:
                return self._send("Nothing on that differs from what was submitted. Change an "
                                  "answer and send it again, or /cancel.")
        ms.sheet, ms.fixed, self._clashes = latex, [], []
        notes = self._choices_from_sheet(ms, latex, answers) if latex else []
        ms.answers = answers
        ms.stage = "confirm"
        if not answers and not ms.picked:
            return self._send(
                "I read the page but couldn't match any answer to a question number. "
                "Make sure each answer starts with <code>1.</code>, <code>2.</code> …\n"
                f"<code>{esc(latex[:300])}</code>")
        self._send(self._math_preview(ms, found) + ("\n\n" + "\n".join(notes) if notes else ""),
                   None if ms.auto else
                   inline_keyboard([[self._fill_btn(), ("✖ Cancel", "cancel")]]))
        for i, value, word in self._clashes:
            # Getting out of a clash is one tap, not a cancel: this button IS
            # the ordinary tap for that option, so nothing else has to change.
            self._send(f"<b>{ms.taps[i]['n']}.</b> sheet and tap disagree.",
                       inline_keyboard([[(f"Use “{word}” from the sheet", f"c:{ms.dep}:{i}:{value}")]]))
        left = [i for i, t in enumerate(ms.taps) if not ms.picked.get(t["name"])]
        if left:                     # they scrolled away while the student worked
            self._send_taps(only=left)

    _ENTER_WORD = re.compile(r"diverges?,?\s+enter\s+([A-Z]{4,})")

    def _choices_from_sheet(self, ms: "MathSession", latex: str, answers: dict,
                            only: Optional[int] = None) -> list[str]:
        """Pick the choices the student wrote out in words; tidy ``answers``.

        "3. Convergent 13/2" answers both parts of question 3 at once, with no
        tap and no second model call. A tap already made wins over the reading
        — it was deliberate, the reading can be wrong — and the clash is said.
        Where the page says "if it diverges, enter DIVERGES" and the choice is
        the divergent one, that word is what goes in the box: shown in the
        preview like everything else, so it is still the student who confirms.
        """
        notes: list[str] = []
        chunks: dict[int, str] = {}
        for chunk in split_problems(latex):
            m = re.match(r"\s*\(?(\d{1,2})[.)]", chunk)
            if m:
                chunks[int(m.group(1))] = chunk[m.end():]
        for tap in ms.taps:
            n, opts = tap["n"], self._tap_options(tap)
            if tap["kind"] != "choice" or n not in chunks or (only is not None and n != only):
                continue
            hit = written_choice(chunks[n], [o.get("label") or "" for o in opts])
            if hit:
                i, word = hit
                if n in answers:
                    answers[n] = [a for a in (without_word(x, word) for x in answers[n]) if a]
                    if not answers[n]:
                        del answers[n]
                tapped = ms.picked.get(tap["name"])
                if tapped and tapped != [opts[i]["value"]]:
                    notes.append(f"⚠️ {n}: the sheet says “{esc(word)}” but you tapped something "
                                 "else — I kept your tap. One tap below switches it.")
                    if only is None:
                        self._clashes.append((ms.taps.index(tap), opts[i]["value"], word))
                elif not tapped:
                    ms.picked[tap["name"]] = [opts[i]["value"]]
            chosen = next((o for o in opts if [o["value"]] == ms.picked.get(tap["name"])), None)
            ask = self._ENTER_WORD.search(tap.get("stem") or "")
            if (ask and chosen and (chosen.get("label") or "").lower().startswith("diverg")
                    and n in ms.targets):
                answers[n] = [ask.group(1)]
                notes.append(f"{n}: divergent, so the box gets <code>{ask.group(1)}</code> — "
                             "the page asks for that word.")
        return notes

    def _math_photo_one(self, data: bytes) -> None:
        """A photo sent while fixing one question answers only that question."""
        ms = self.cs.math
        n = ms.fixing
        result = self.transcribe(data, hint=(
            f"A handwritten answer to one question. Transcribe it verbatim; "
            f"do not solve, simplify or correct."))
        if result.needs_human or not result.latex:
            return self._send(f"Still couldn't read that one. Type {n}'s answer instead.")
        found = number_problems(result.latex)
        raw = found.get(n) or [final_answer(p) for p in split_parts(result.latex)]
        ms.fixing, ms.stage = 0, "confirm"
        self._set_answer(ms, n, [a for a in raw if a])
        self._send(self._math_preview(ms),
                   inline_keyboard([[self._fill_btn(), ("✖ Cancel", "cancel")]]))

    def _math_preview(self, ms: "MathSession", found: Optional[dict] = None) -> str:
        lines = [f"<b>{esc(ms.title)}</b> — I read:"]
        for n in sorted(ms.answers):
            got, want = ms.answers[n], len(ms.targets.get(n, [1]))
            shown = " , ".join(f"<code>{esc(a)}</code>" for a in got)
            short = f"  ⚠️ {want} blanks, I only have {len(got)}" if len(got) < want else ""
            lines.append(f"<b>{n}.</b> {shown}{short}")
        skipped = sorted(set((found or {})) - set(ms.answers) - set(ms.targets)
                         - {t["n"] for t in ms.taps} - set(ms.stepped))
        if skipped:
            lines.append(f"\nIgnored (no such question here): {', '.join(map(str, skipped))}")
        for t in ms.taps:                      # tapped answers, same preview
            # the button's own name: its letter, or its short label — not the
            # page's option value ("1. 👆 3" meant nothing to the student)
            got = [self._tap_name(t, o) for o in self._tap_options(t)
                   if o["value"] in ms.picked.get(t["name"], [])]
            if got:
                lines.append(f"<b>{self._tap_label(t)}.</b> 👆 {esc(', '.join(got))}")
        if self._untapped(ms):
            lines.append("Not tapped yet: " + ", ".join(map(str, self._untapped(ms))))
        missing = sorted(set(ms.targets) - set(ms.answers))
        if missing:
            lines.append(f"Nothing for: {', '.join(map(str, missing))}")
        if ms.graded:
            right = [n for n, v in ms.graded.items() if v == "right"]
            wrong = [n for n, v in ms.graded.items() if v == "wrong"]
            lines.append("Graded so far: " + " · ".join(x for x in (
                "✓ " + ", ".join(map(str, right)) if right else "",
                "✗ " + ", ".join(map(str, wrong)) if wrong else "") if x))
        if ms.steps:
            lines.append("Not begun yet: " + ", ".join(
                f"{st['n']} ({st['label']})" if st.get("label") else str(st["n"]) for st in ms.steps))
        lines.append("\nWrong? Tap a choice again to change it, send "
                     "<code>3: \\frac{1}{2}x</code> to fix one written answer, "
                     "or send a new photo.")
        return "\n".join(lines)

    def _math_fix(self, n: int, latex: str) -> None:
        ms = self.cs.math
        if n not in ms.targets:
            return self._send(f"There's no question {n} in this assignment.")
        if ms.stage == "graded" and n not in ms.stepped:
            ms.done.pop(n, None)                     # graded, now corrected: write it again
        self._set_answer(ms, n, [final_answer(p) for p in split_parts(latex)])
        if n not in ms.fixed:
            ms.fixed.append(n)
        ms.stage = "confirm"
        self._send(self._math_preview(ms),
                   inline_keyboard([[self._fill_btn(), ("✖ Cancel", "cancel")]]))

    @staticmethod
    def _untapped(ms: "MathSession") -> list[int]:
        """Question numbers holding a tappable part nobody has tapped yet."""
        return sorted({t["n"] for t in ms.taps if not ms.picked.get(t["name"])})

    def _half_done(self, ms: "MathSession") -> list[int]:
        """Questions answered in part: a value written but its choice not
        tapped, or the other way round. Filling one of these spends an attempt
        on an answer that cannot be fully right. A question left blank
        altogether is the student's business and is not listed."""
        tapped = {t["n"] for t in ms.taps if ms.picked.get(t["name"])}
        untapped = set(self._untapped(ms))
        written, unwritten = set(ms.answers), set(ms.targets) - set(ms.answers)
        return sorted(((written | tapped) & untapped) | (tapped & unwritten))

    def _math_fill(self, force: bool = False, then_submit: bool = False) -> None:
        ms = self.cs.math
        half = [] if force else self._half_done(ms)
        if half:
            idx = [i for i, t in enumerate(ms.taps)
                   if t["n"] in half and not ms.picked.get(t["name"])]
            self._send("Half-answered: <b>" + ", ".join(map(str, half)) + "</b>. Filling one like "
                       "that spends an attempt on an answer that can't be fully right.",
                       inline_keyboard([[("Fill anyway", "mfill!")], [("✖ Cancel", "cancel")]]))
            if idx:
                self._send_taps(only=idx)
            return
        payload: dict[str, str] = {}
        bad: list[int] = []
        for n in ms.answers:
            for box, tex in self._pending(ms, n):
                if box in ms.typed:              # a number or a word, as typed
                    val = plain_text(tex)
                    if box.startswith("RN_"):    # a numeric box: the unit is printed beside it
                        val = number_only(val)
                    payload[box] = val
                    continue
                try:
                    payload[box] = latex_to_webassign_mathml(tex)
                except Exception:
                    bad.append(n)
        if not payload and not ms.picked:
            return self._send("None of those converted to WebAssign's format. "
                              f"Check {', '.join(map(str, bad))} and re-send.")
        if not ms.auto:
            self._send("Submitting…" if then_submit else "Filling…")
        try:
            tapped: dict = {}
            if ms.picked:                      # the tapped answers first
                written = tuple(filter(None, (_dom_of(b) for b in payload)))
                tapped = self._wa().choose(ms.dep, ms.picked, skip_shots=written) or {}
            info, shots = (self._wa().fill(ms.dep, payload) if payload
                           else ({"readback": {}, "missing": []}, []))
            ms.filled.update(payload)
            shots = list(tapped.get("shots") or []) + list(shots)
            doms = list(tapped.get("shot_doms") or []) + list(info.get("shot_doms") or [])
        except WebAssignUnavailable as e:
            return self._send(f"⚠️ {esc(str(e))}")
        empty = [k for k, v in info["readback"].items() if not v]
        self._show_filled(ms, shots, doms)
        note = ""
        if bad:
            note += f"\nCouldn't convert: {', '.join(map(str, bad))} — do those by hand."
        if empty or info["missing"]:
            note += "\n⚠️ Some boxes read back empty — check the pictures."
        undrawn = [_dom_of(b) for b in info.get("undrawn") or []]
        if undrawn:
            qs_ = sorted({n for n, boxes in ms.targets.items()
                          if any(_dom_of(b) in undrawn for b in boxes)})
            note += ("\n⚠️ " + ", ".join(map(str, qs_)) + ": the page didn't draw the answer "
                     "I wrote — if the picture shows it blank, send that answer again "
                     "(✏️ Modify) before submitting.")
        ms.stage = "filled"
        kept = ("💾 Saved on WebAssign — not submitted, no attempt used.\n"
                if info.get("saved") or tapped.get("saved") else "")
        # Both kinds are answers. Counting only the written ones told a student
        # who had tapped two choices that 0 answers were filled (2026-09-21).
        n_written = len(payload) - len(empty)
        n_tapped = len(tapped.get("done") or []) if tapped else 0
        if tapped.get("missing"):
            note += "\n⚠️ Couldn't tick: " + ", ".join(esc(str(m)) for m in tapped["missing"])
        what = " + ".join(x for x in (f"{n_written} written" if n_written else "",
                                       f"{n_tapped} tapped" if n_tapped else "") if x) or "0"
        if ms.auto:
            # /auto presses Submit itself: asking would only add a message and
            # four buttons nobody is meant to press. Problems are still said.
            if note.strip():
                self._send(f"Filled {what} answer(s).{note}")
            return
        if then_submit and not note.strip():
            return self._math_submit()               # one tap: filled, now submitted
        self._send(
            f"Filled {what} answer(s) into <b>{esc(ms.title)}</b>.{note}\n\n"
            f"{kept}The question pictures above now show your answers — exactly what WebAssign "
            "will grade. Submit?",
            inline_keyboard([[("✅ Submit", f"msubmit:{ms.dep}")],
                             [("✏️ Modify", "mmodify"), ("⏸ Later", "mlater")],
                             [("✖ Cancel", "cancel")]]))

    # -- later / resume: stop half way, come back to it -----------------------

    def _math_later(self) -> None:
        """Set the round aside. Nothing is lost on either side: WebAssign has
        the answers (saved, not submitted) and this chat keeps the round."""
        cs, ms = self.cs, self.cs.math
        cs.parked = [p for p in cs.parked if p.dep != ms.dep] + [ms]
        cs.math = None
        self._send(f"⏸ Set aside <b>{esc(ms.title)}</b>. Your answers are saved on WebAssign, "
                   "nothing was submitted.\n/resume brings it back — today or next week.")

    def _resume_list(self) -> None:
        cs = self.cs
        rounds = list(cs.parked)
        if cs.math and cs.math.dep and cs.math.stage != "pick":
            rounds.append(cs.math)              # the one still open counts too
        if not rounds:
            return self._send("Nothing set aside. /todo to start something.")
        rows = [[(f"{m.title[:40]} · {len(m.answers) + len(m.picked)} answered", f"mres:{m.dep}")]
                for m in rounds]
        self._send("<b>Saved, not submitted</b> — which one?", inline_keyboard(rows))

    def _resume(self, dep: str) -> None:
        cs = self.cs
        ms = next((p for p in cs.parked if p.dep == dep), None)
        if ms is None and cs.math and cs.math.dep == dep:
            ms = cs.math
        if ms is None:
            return self._send("That one isn't set aside any more. /resume to see what is.")
        if cs.math and cs.math.dep != dep and cs.math.stage != "pick":
            cs.parked = [p for p in cs.parked if p.dep != cs.math.dep] + [cs.math]   # swap, don't drop
        cs.parked = [p for p in cs.parked if p.dep != dep]
        cs.math, ms.stage, ms.fixing = ms, "confirm", 0
        self._send(f"▶️ Back in <b>{esc(ms.title)}</b>.")
        if not (ms.answers or ms.picked):
            return self._send("Nothing answered yet — send the photo of your sheet, or tap the choices.")
        # Put the page back exactly as the student left it and show it again:
        # the same fill, which is idempotent, rather than trusting that the
        # page still holds what we think it holds.
        self._math_fill(force=True)

    # -- modify: correcting one answer, without re-reading the page -----------

    def _math_modify(self) -> None:
        """Offer one button per question. Re-reading the whole sheet to fix one
        answer costs another model call and another round of writing and
        photographing; typing the one that is wrong costs nothing."""
        ms = self.cs.math
        ns = sorted(ms.answers)
        rows = [[(str(n), f"mfix:{n}") for n in ns[i:i + 5]] for i in range(0, len(ns), 5)]
        rows.append([("✖ Back", "mback")])
        self._send("Which one is wrong?", inline_keyboard(rows))

    def _math_ask_fix(self, n: int) -> None:
        ms = self.cs.math
        ms.stage, ms.fixing = "fixing", n
        current = " , ".join(f"<code>{esc(a)}</code>" for a in ms.answers.get(n, []))
        blanks = len(ms.targets.get(n, []))
        how = (f"\nThat question has {blanks} blanks — separate them with commas."
               if blanks > 1 else "")
        self._send(
            f"<b>{n}.</b> I have {current}\n\n"
            f"Type the right answer as LaTeX, e.g. <code>\\frac{{1}}{{2}}x^2+C</code>.{how}\n"
            "A photo of just this one works too, but typing costs no model call.")

    _SENTENCE_RE = re.compile(r"^(?:[A-Za-z']+\s+){2,}[A-Za-z'.!?]+$")

    def _math_apply_fix(self, latex: str) -> None:
        ms = self.cs.math
        if self._SENTENCE_RE.match(latex.strip()) and "\\" not in latex:
            # "first one is blank" is a remark, not an answer (2026-09-24): filling
            # it in would put those words into the box. Say what is expected.
            n = ms.fixing
            return self._send(
                f"That reads like a sentence, not an answer, so I haven't changed <b>{n}</b>. "
                f"Type the answer itself (LaTeX for maths, e.g. <code>11e^{{-0.01t}}</code>; "
                f"several blanks separated by commas), send a photo of just that one, "
                f"or /cancel.\nIf a box came out blank in the picture, send the answer "
                f"again and I'll write it again.")
        n, ms.fixing, ms.stage = ms.fixing, 0, "confirm"
        self._set_answer(ms, n, [final_answer(p) for p in split_parts(latex)])
        if n not in ms.fixed:
            ms.fixed.append(n)
        if ms.auto:                          # /auto fills it next; no preview, no buttons
            return
        self._send(self._math_preview(ms),
                   inline_keyboard([[self._fill_btn(), ("✖ Cancel", "cancel")]]))

    # -- submit: only ever after the student has seen the rendered answers ----

    def _math_submit(self) -> None:
        ms = self.cs.math
        boxes = [box for n in sorted(ms.answers) for box, _ in self._pending(ms, n)]
        boxes += [name for name in ms.picked]          # tapped parts count too
        if not boxes:
            if ms.stepped and self._math_next_part(ms, []):
                return
            return self._send("Nothing filled to submit. /todo to start again.")
        if not ms.auto:
            self._send("Submitting — pressing SUBMIT ANSWER for each question…")
        try:
            info, shots = self._wa().submit(ms.dep, boxes,
                                            {b: ms.filled[b] for b in boxes if b in ms.filled})
        except WebAssignUnavailable as e:
            return self._send(f"⚠️ {esc(str(e))}")
        # No graded pictures: the lines below say what was right (three sets of
        # pictures per assignment was too many — the owner, 2026-09-27).
        lines = [f"Submitted {len(info['submitted'])} question(s) in <b>{esc(ms.title)}</b>."]
        by_dom = self._numbers_by_dom(ms)
        rows = []
        verdicts: dict[int, str] = {}
        for dom, out in (info.get("outcome") or {}).items():
            n = by_dom.get(dom)
            verdict = self._verdict((out or {}).get("points"), (out or {}).get("marks") or [])
            if n is not None:
                verdicts[n] = verdict
            if n is not None and n not in ms.stepped:
                # partial = some part wrong: redone like a wrong one
                ms.graded[n] = ("right" if verdict == "right" else
                                "wrong" if verdict in ("wrong", "partial") else "?")
            mark = {"right": "✓", "wrong": "✗", "partial": "◐", "pending": "…"}.get(verdict, "?")
            head = f"<b>{n}.</b> " if n is not None else ""
            pts = (out or {}).get("points") or ""
            rows.append((n if n is not None else 999, f"{mark} {head}{esc(pts)}".rstrip()))
        lines += [r for _, r in sorted(rows)]
        if info["failed"]:
            lines.append("⚠️ Couldn't press submit on: "
                         + ", ".join(esc(k) for k in info["failed"]))
        try:                                         # how far the assignment is now
            _, fresh, _ = self._wa().questions(ms.dep)
            self._record_progress(ms.dep, ms.title, fresh, ms.item_id)
            prog = self.cs.wa_progress.get(ms.dep, {})
            if prog:
                lines.append(f"Assignment: {prog['answered']}/{prog['total']} questions submitted · "
                             f"{prog['points']:g}/{prog['max']:g} points")
        except Exception as e:                       # a summary line is not worth failing for
            log.info("progress read failed: %s", str(e)[:80])
        differs = info.get("differs") or []
        if differs:
            bad = sorted({n for n, bs in ms.targets.items() if any(b in differs for b in bs)})
            for n in bad:
                ms.graded[n] = "?"               # not the answer's fault: never "redo" it
            lines.append("⚠️ " + ", ".join(map(str, bad)) + ": WebAssign graded a DIFFERENT value "
                         "from the one I filled — my problem, not a wrong answer. Check the page "
                         "before submitting that one again.")
        # What is graded now stays graded: never written or submitted again.
        failed = set(info.get("failed") or {})
        for n in sorted(ms.answers):
            sent = [b for b in boxes if b in ms.targets.get(n, []) and _dom_of(b) not in failed]
            if sent:
                ms.done.setdefault(n, []).extend(b for b in sent if b not in ms.done.get(n, []))
        ms.taps = [t for t in ms.taps if t["n"] in ms.stepped]
        ms.picked = {k: v for k, v in ms.picked.items() if k in {t["name"] for t in ms.taps}}
        if ms.stepped and self._math_next_part(ms, lines):
            return                                   # the round goes on with the next part
        wrong = sorted(n for n, v in ms.graded.items() if v == "wrong")
        if ms.auto and wrong:
            ms.stage = "graded"
            lines.append("\n✗ " + ", ".join(map(str, wrong)) + " marked wrong.")
            self._send("\n".join(lines))
            return
        # Everything not fully right this time — wrong, partly right, or not
        # graded at all — is what Redo reopens. The buttons say the rest.
        redo = sorted(n for n, v in verdicts.items() if v != "right" and n not in ms.stepped)
        if not redo or ms.auto:
            if not redo:
                lines.append("\n🎉 All correct — this one is done.")
            self._send("\n".join(lines))
            self.cs.math = None
            self._refresh_todo()                     # its line turns ✅ where it is
            return
        ms.stage, ms.redo = "graded", redo
        self._send("\n".join(lines), inline_keyboard(
            [[(f"🔁 Redo {', '.join(map(str, redo))}", f"mredo:{ms.dep}"),
              ("✅ Finish", f"mfinish:{ms.dep}")]]))

    def _math_next_part(self, ms: "MathSession", lines: list[str]) -> bool:
        """After a submit, a step question is read again and the round goes on:

        - a step marked ✗ stays open for another try — ask for it again;
        - a step marked ✓ is replaced by the next one — show it, ask for it;
        - no step open but a "begin" button — (a) is finished: begin (b);
        - nothing open — the question is done.

        Asking means the fixing stage: what comes next, typed or a photo of
        just that step, is that question's answer. A value still on the sheet
        (a lettered part written ahead) is used first. True when the round
        continues."""
        try:
            _, qs, shots = self._wa().questions(ms.dep)     # fresh: submit dropped the cache
        except WebAssignUnavailable as e:
            lines.append(f"⚠️ Couldn't look for the next step: {esc(str(e))}")
            return False
        todo: list[tuple[int, str, str]] = []            # (n, label, why)
        for q in qs:
            if q.n not in ms.stepped:
                continue
            n = q.n
            wrong = [p for p in q.parts if p.wrong and p.written]
            open_boxes = math_targets([q]).get(n, [])
            graded_names = {p.name for p in q.parts if p.graded}
            new_boxes = [b for b in open_boxes
                         if b not in ms.done.get(n, []) and b not in graded_names]
            focus = wrong[0].name if wrong else (new_boxes[0] if new_boxes else "")
            shot = shots[q.n - 1] if q.n - 1 < len(shots) else None
            if focus:                            # just the open step, not the whole question
                try:
                    shot = self._wa().step_shot(ms.dep, focus) or shot
                except Exception as e:           # a picture is a courtesy, not the answer
                    log.info("step shot failed: %s", str(e)[:80])
            if wrong:
                self._absorb_step(ms, q)          # takes the ✗ box out of `done`
                # The value that was marked wrong must not be filled again:
                # forget it, so the step is asked for rather than repeated.
                ms.answers[n] = ms.answers.get(n, [])[:len(ms.done.get(n, []))]
                todo.append((n, wrong[0].label, "wrong"))
            elif new_boxes:
                self._absorb_step(ms, q)
                lab = next((p.label for p in q.parts if p.name == new_boxes[0]), "")
                todo.append((n, lab, "next"))
            elif (not [b for b in open_boxes if b not in graded_names]
                  and not any(p.tappable for p in q.parts) and step_buttons([q])):
                st = step_buttons([q])[0]
                try:
                    q2, shot = self._wa().begin_step(ms.dep, st["id"])
                except WebAssignUnavailable as e:
                    lines.append(f"⚠️ {n}: couldn't open {esc(st.get('label') or 'the next part')}: {esc(str(e))}")
                    continue
                self._absorb_step(ms, q2)
                todo.append((n, f"{st.get('label') or 'next part'} {self._step_label([q2], n)}".strip(), "begun"))
            else:
                continue                                   # finished, or nothing new
            if shot:
                try:
                    self.tg.send_photo(self.chat_id, shot, caption=f"{n}. {todo[-1][1]}".strip())
                except TelegramError as e:
                    log.warning("sendPhoto step failed: %s", e)
        if not todo:
            return False
        if lines:
            self._send("\n".join(lines))
        for n, label, why in todo:
            if why == "wrong":
                self._send(f"✗ <b>{n}.</b> {esc(label or 'that step')} was marked wrong — the box is "
                           "still open, try again.")
        # A value written ahead on the sheet answers the new step without asking.
        ready = [n for n, _, _ in todo if self._pending(ms, n)]
        ask = [(n, label) for n, label, _ in todo if not self._pending(ms, n)]
        if ready and not ask:
            ms.stage = "confirm"
            self._math_fill()                # writes only the new step, shows it, asks Submit
            return True
        n, label = ask[0]
        ms.stage, ms.fixing = "fixing", n
        more = (" Then " + ", ".join(str(m) for m, _ in ask[1:]) + "." if ask[1:] else "")
        self._send(f"<b>{n}.</b> {esc(label or 'the next step')} is open — type the answer "
                   f"(LaTeX for maths, e.g. <code>\\ln(y)</code>), or send a photo of just that."
                   + more)
        return True

    def _webassign_dep(self, item) -> Optional[str]:
        """Which WebAssign assignment this Canvas item is, or None if unsure.

        Resolved by Canvas's own LTI launch (link.dep_for), which is how the
        browser does it — so it needs no vendor login and works for a student
        who has never opened Cengage. Quiet on failure: an item should still
        open as a link rather than error.
        """
        from ..external import link
        try:
            return link.dep_for(self.canvas, self._wa(),
                                item.course_id, item.id, item.title)
        except Exception as e:
            log.info("no WebAssign link for %r (%s)", item.title[:40], str(e)[:70])
            return None

    def _where_it_goes(self, detail: dict, course_id: str) -> str:
        """One line saying where this is handed in — Canvas, a vendor, or both.

        "Do I have to submit this twice?" has no general answer: in PHYSICS 247
        one problem set is Canvas-only, the next also needs Gradescope, and the
        third is Gradescope alone with the grade flowing back. It is readable
        from the assignment, so the student should be told rather than guess.
        """
        from ..external import routing
        try:
            r = routing.route(detail, routing.has_gradescope(self._course_tabs(course_id)))
            return routing.describe(r)
        except Exception as e:
            log.info("couldn't work out routing for %s (%s)", detail.get("name"), str(e)[:60])
            return ""

    def _course_tabs(self, course_id: str) -> list:
        """Course nav, cached — it changes about once a semester."""
        cache = getattr(self, "_tabs_cache", None)
        if cache is None:
            cache = self._tabs_cache = {}
        if course_id not in cache:
            try:
                cache[course_id] = self.canvas.tabs(course_id)
            except Exception:
                cache[course_id] = []
        return cache[course_id]

    def _wa(self):
        if self._webassign is None:
            from ..external.webassign import WebAssign
            self._webassign = WebAssign(launch_url=self._webassign_launch_url)
        return self._webassign

    def _webassign_launch_url(self) -> str:
        """A fresh Canvas LTI launch for any WebAssign assignment.

        Used to rebuild the WebAssign session when it is gone. Which assignment
        is launched does not matter — every launch creates the same session —
        so the first one Canvas lists is used and remembered.
        """
        from ..external.platforms import classify
        ref = getattr(self, "_wa_launch_ref", None)
        if ref is None:
            for course in self.canvas.courses():
                try:
                    rows = self.canvas._http.paginate(
                        f"/api/v1/courses/{course.id}/assignments", {"per_page": 50})
                    for a in rows:
                        tool = (a.get("external_tool_tag_attributes") or {}).get("url") or ""
                        plat = classify(tool)
                        if plat is not None and plat.key == "webassign":
                            ref = (str(course.id), str(a["id"]))
                            break
                except Exception:
                    continue
                if ref:
                    break
            if ref is None:
                raise RuntimeError("no WebAssign assignment found in Canvas")
            self._wa_launch_ref = ref
        return self.canvas.sessionless_launch(*ref)

    # -- /channel --------------------------------------------------------------

    _CHANNEL_LABEL = {
        "token": "official access token — documented REST + GraphQL",
        "cookie": "browser session — the reverse-engineered fallback",
    }

    def _channel(self, arg: str) -> None:
        """/channel shows where we are; /channel <auto|token|cookie> pins it."""
        from ..canvas import channel

        if not arg:
            pin = channel.preference()
            live = getattr(self.canvas, "_channel", "?")
            return self._send(
                f"<b>Channel</b>\n"
                f"now: <code>{esc(live)}</code> — {esc(self._CHANNEL_LABEL.get(live, ''))}\n"
                f"pin: <code>{esc(pin)}</code>\n\n"
                "/channel auto — token first, fall back if it dies\n"
                "/channel token — token only (no fallback; use this to test it)\n"
                "/channel cookie — force the browser session")
        if arg not in channel.VALID:
            return self._send(f"❌ channel must be one of {', '.join(channel.VALID)}")
        # Open it BEFORE pinning. Pinning first left the preference set to a
        # channel that could not be opened, and the bot then refused to start.
        previous = channel.preference()
        try:
            new = channel.open_source(arg)  # type: ignore[arg-type]
        except Exception as e:
            return self._send(
                f"Can't switch to <code>{esc(arg)}</code>:\n\n{esc(str(e))[:400]}\n\n"
                f"Still on <code>{esc(previous)}</code>.")
        channel.set_preference(arg)  # type: ignore[arg-type]
        self._swap(new)
        self._send(f"✅ Now on <code>{esc(new._channel)}</code> — "
                   f"{esc(self._CHANNEL_LABEL.get(new._channel, ''))}")

    def _swap(self, new: ApiSource) -> None:
        old, self.canvas = self.canvas, new
        try:
            old.close()
        except Exception:
            log.debug("closing the old channel failed", exc_info=True)

    def _fall_back(self, exc: CanvasAuthError) -> bool:
        """Try the other channel once. Never silent: the student is told either way."""
        from ..canvas.channel import NeedsCanvasLogin
        try:
            new = self.canvas.recover_auth(exc)
        except NeedsCanvasLogin as need:
            # Both channels are out: a person has to sign in.
            self.ask_login(esc(str(need)))
            return True
        if new is None:
            return False
        was = self.canvas._channel
        self._swap(new)
        if was == new._channel == "cookie":
            # Signed in again (the login button, or at the host): the browser
            # holds the new session and it has just been picked up.
            self._send("✅ Picked up your new Canvas login — say that again.")
            return True
        self._send(f"⚠️ The <code>{esc(was)}</code> channel stopped working, so I switched to "
                   f"<code>{esc(new._channel)}</code>. Your Canvas token may have been revoked — "
                   "worth checking. Say that again and I'll retry.")
        return True

    # -- /todo ----------------------------------------------------------------

    TODO_DAYS = 4        # today + the next N days; nothing overdue, nothing further out

    _POINTS_RE = re.compile(r"\[\s*([–-]|[\d.]+)\s*/\s*([\d.]+)\s*Points?\s*\]", re.I)

    def _full_marks(self, stem: str) -> bool:
        """"4. [2 / 2 Points]" — graded, and every point of it."""
        m = self._POINTS_RE.search(stem or "")
        if not m or m.group(1) in ("–", "-"):
            return False
        try:
            got, of = float(m.group(1)), float(m.group(2))
        except ValueError:
            return False
        return of > 0 and got >= of

    def _record_progress(self, dep: str, title: str, questions, item_id: str = "") -> None:
        """Remember how far this WebAssign assignment is, from the question
        headers ("6. [0.48 / 1.42 Points]", "[– / 1 Points]" = not submitted).
        A step question with parts still to open is not complete yet."""
        answered = total = 0
        pts = mx = 0.0
        per: list[list] = []                             # [question, points or None, max]
        for q in questions:
            m = self._POINTS_RE.search(q.stem or "")
            if not m:
                continue
            total += 1
            mx += float(m.group(2))
            if m.group(1) in ("–", "-"):
                per.append([q.n, None, float(m.group(2))])
                continue
            pts += float(m.group(1))
            per.append([q.n, float(m.group(1)), float(m.group(2))])
            steps_left = any(p.kind == "step" for p in q.parts) or any(
                p.written and p.label and not p.graded for p in q.parts)
            if not steps_left:
                answered += 1
        if not total:
            return
        old = self.cs.wa_progress.get(dep, {})
        now = datetime.now(self.tz).isoformat(timespec="minutes")
        complete = answered == total
        self.cs.wa_progress[dep] = {
            "title": title, "item_id": item_id or old.get("item_id", ""),
            "answered": answered, "total": total, "points": round(pts, 2), "max": round(mx, 2),
            "complete": complete, "at": now, "per": per,
            # When it FIRST became complete: /todo keeps it a day after that
            # (a new attempt is still one tap away), then hides it. Re-opening
            # it later must not restart the day.
            "done_at": (old.get("done_at") or now) if complete else ""}

    DONE_KEEP = timedelta(days=1)   # a finished item stays on /todo this long

    def _done_long_ago(self, prog: dict, now: datetime) -> bool:
        try:
            at = datetime.fromisoformat(prog.get("done_at") or prog.get("at") or "")
        except ValueError:
            return False
        if at.tzinfo is None:
            at = at.replace(tzinfo=self.tz)
        return now - at > self.DONE_KEEP

    @staticmethod
    def _points_line(prog: dict) -> str:
        """"1/2 (Q1 0/1 · Q2 1/1)": the total, then each question."""
        total = f"{prog['points']:g}/{prog['max']:g}"
        per = prog.get("per") or []
        if not per:
            return total
        return total + " (" + " · ".join(
            f"Q{n} {'–' if p is None else f'{p:g}'}/{m:g}" for n, p, m in per) + ")"

    def _progress_for(self, item_id: str, title: str) -> Optional[dict]:
        """The recorded WebAssign progress for this Canvas item: by its id when
        the round was opened from /todo, else by title — WebAssign's page title
        can be the shorter one ("Module Quiz 13" vs "Module Quiz 13 - Euler's
        Method"), so a prefix match on the normalised titles counts."""
        from ..external.link import normalize
        want = normalize(title)
        for v in self.cs.wa_progress.values():
            if item_id and v.get("item_id") == item_id:
                return v
        for v in self.cs.wa_progress.values():
            got = normalize(v.get("title", ""))
            if got and want and (want == got or want.startswith(got + " ") or got.startswith(want + " ")):
                return v
        return None

    def _refresh_todo(self) -> None:
        """Redraw the last /todo list where it is (a finished round shows ✅
        there). Quietly nothing if there is none, or Telegram won't edit it."""
        tm = self.cs.todo_msg
        if not tm.get("id"):
            return
        try:
            self._todo(tm.get("days"), bool(tm.get("all")), edit_id=int(tm["id"]))
        except Exception as e:                       # a courtesy, never a failure
            if "not modified" not in str(e):         # nothing changed on the list: fine
                log.info("todo refresh failed: %s", str(e)[:100])

    def _todo(self, days: Optional[int] = None, show_done: bool = False,
              edit_id: Optional[int] = None) -> None:
        """The one way in. Everything the student can act on is a code from here.

        ``days`` widens the window (``/todo 14``): /todo is the only list, so
        it has to be able to reach work that is not due this week — otherwise
        getting ahead would need a second command and a second list.
        """
        days = max(1, min(int(days or self.TODO_DAYS), 60))
        now = datetime.now(self.tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        horizon = start + timedelta(days=days + 1)                 # end of day N
        # Ask Canvas for the window we will show, not the rest of the semester.
        items = self.canvas.planner(start.astimezone(timezone.utc),
                                    until=horizon.astimezone(timezone.utc))
        # Only the assignments in the window, not every assignment in each course.
        wanted: dict[str, list[str]] = {}
        for i in items:
            if i.course_id and i.kind == Kind.ASSIGNMENT:
                wanted.setdefault(i.course_id, []).append(i.id)
        meta = self.canvas.assignments_meta_for(wanted)
        items = enrich_submission_types(items, {k: v["types"] for k, v in meta.items()})
        seen: set[str] = set()
        menu: list[MenuItem] = []
        done_here: list[dict] = []                      # finished WebAssign work, hidden
        for it in sorted(items, key=lambda i: (i.due_at or i.planned_at or now)):
            if it.kind == Kind.ANNOUNCEMENT or it.is_ungraded or not it.course_id:
                continue
            # Done work stays on the list, marked, so the list shows the whole
            # week; the sweep and /auto skip it.
            done = "" if it.actionable else ("excused" if getattr(it, "excused", False) else "submitted")
            due = it.due_at or it.planned_at
            if not due:
                continue
            due = due.astimezone(self.tz)
            if due < now or due >= horizon:
                continue
            key = f"{it.kind.value}:{it.id}"
            if key in seen:
                continue
            seen.add(key)
            st = set(it.submission_types)
            if it.kind == Kind.QUIZ:
                kind = "quiz"
            elif it.kind == Kind.DISCUSSION:
                kind = "discussion"
            elif st & DOABLE:
                kind = "assignment"
            elif self._doable_vendor(meta.get(it.id, {}).get("tool", "")):
                kind = "webassign"      # an external tool we can work right here
            elif self._doable_vendor(meta.get(it.id, {}).get("tool", ""), "gradescope"):
                kind = "gradescope"     # files go up through the student's own browser session
            else:
                kind = "external"
            if kind == "webassign" and not done:
                # Canvas hears about WebAssign work only when the grade is passed
                # back, days later — so the bot's own record counts.
                prog = self._progress_for(it.id, it.title)
                if prog and prog.get("complete"):
                    if not show_done and self._done_long_ago(prog, now):
                        done_here.append(prog)          # finished a day ago: off the list
                        continue
                    done = self._points_line(prog)
            menu.append(MenuItem(idx=0, course_id=it.course_id, id=it.id, kind=kind,
                                 title=it.title, course=_short_course(it.course_name),
                                 types=sorted(st), url=it.canonical_url, due=_due_str(it, self.tz),
                                 done=done, tool=meta.get(it.id, {}).get("tool", "")))
        further = inline_keyboard([[("📆 Look 14 days ahead", "todo:14")]]) if days < 14 else None
        done_note = (f"✅ {len(done_here)} finished more than a day ago, waiting for the grade — "
                     "/todo all shows them." if done_here else "")

        def emit(text: str, kb) -> None:
            if edit_id:                              # the same message, redrawn
                self.tg.edit_text(self.chat_id, edit_id, text, reply_markup=kb)
                return
            ids = self._send(text, kb)
            if ids:
                self.cs.todo_msg = {"id": ids[-1], "days": days, "all": show_done}
        if not menu:
            if done_note:
                return emit(f"Nothing else due in the next {days} days. 🎉\n\n" + done_note, further)
            if not edit_id:
                self.cs.menu, self.cs.pending, self.cs.draft = [], None, None
            return emit(f"Nothing due today or in the next {days} days. 🎉", further)

        # one block per course (A, B, C…), items numbered inside the block (A1, A2…);
        # each item is EITHER doable here OR a link — never both, never a second list
        by_course: dict[str, list[MenuItem]] = {}
        for m in menu:                                   # menu is already due-sorted
            by_course.setdefault(m.course, []).append(m)
        blocks = sorted(by_course.items(), key=lambda kv: kv[0])
        lines = [f"📅 <b>Today → +{days} days</b> — reply a code like <b>C2</b>", ""]
        idx = 0
        here = ("assignment", "discussion", "webassign", "gradescope", "quiz")
        for letter, (course, ms) in zip("ABCDEFGHIJ", blocks):
            n_do = sum(1 for m in ms if m.kind in here and not m.done)
            n_done = sum(1 for m in ms if m.done)
            out = sum(1 for m in ms if m.kind not in here)
            lines.append(f"<b>{letter} · {esc(course)}</b>  ({n_do} to do"
                         + (f" · {n_done} done" if n_done else "")
                         + (f" · {out} ↗" if out else "") + ")")
            for i, m in enumerate(ms, 1):
                idx += 1
                m.idx, m.code = idx, f"{letter}{i}"
                when = m.due[:9]                         # "Fri 09-18"
                if m.kind in here:
                    icon = {"discussion": "💬", "webassign": "🧮", "gradescope": "📤", "quiz": "📝"}.get(
                        m.kind, "📎" if "online_upload" in m.types else "✍️")
                    # Done is done: no score on the list (the owner, 2026-09-27)
                    if m.kind == "webassign" and m.done:
                        lines.append(f"  ✅ <b>{m.code}</b> {icon} {esc(_clip(m.title, 44))} · {when}"
                                     f" · <b>Done</b>")
                        continue
                    lines.append(f"  {'✅ ' if m.done else ''}<b>{m.code}</b> {icon} "
                                 f"{esc(_clip(m.title, 44))} · {when}" + (" · <b>Done</b>" if m.done else ""))
                else:
                    tag = " · quiz" if m.kind == "quiz" else ""
                    lines.append(f"  {'✅ ' if m.done else ''}{m.code} ↗ <a href=\"{esc(m.url)}\">{esc(_clip(m.title, 40))}</a>"
                                 f" · {when}{tag}" + (" · <b>Done</b>" if m.done else ""))
            lines.append("")
        if edit_id:
            self.cs.menu = menu                      # same codes; nothing in progress is dropped
        else:
            self.cs.menu, self.cs.pending, self.cs.draft = menu, None, None
        if done_note:
            lines.append(done_note)
        emit("\n".join(lines).rstrip(), further)

    @staticmethod
    def _doable_vendor(tool_url: str, key: str = "webassign") -> bool:
        """Is this external tool the vendor ``key`` — one we can work in the chat?

        Decided from the tool url Canvas already sent with the assignment, so
        /todo can mark the item honestly without opening a browser.
        """
        from ..external.platforms import classify
        p = classify(tool_url)
        return p is not None and p.key == key

    @staticmethod
    def _not_webassign(tool_url: str) -> bool:
        """Does Canvas's tool url point somewhere that is NOT WebAssign?

        Any tool url that isn't WebAssign's counts, known platform or not:
        WebAssign items always carry gateway.cengage.com, so an unknown host
        (Macmillan's bfwpub.com, New Quizzes, …) is simply another platform.
        Only an item with no tool url at all is worth asking WebAssign about."""
        from ..external.platforms import classify
        if not (tool_url or "").strip():
            return False
        p = classify(tool_url)
        return p is None or p.key != "webassign"

    # -- pick an item ---------------------------------------------------------

    def _incoming_text(self, text: str) -> None:
        if self.cs.quiz is not None:
            return self._quiz_text(text)
        ms = self.cs.math
        if ms and ms.stage == "fixing":
            fix = _MATH_FIX_RE.match(text)      # "6: 22.6" out of habit is fine too
            if fix and int(fix.group(1)) == ms.fixing:
                text = fix.group(2).strip()
            return self._math_apply_fix(text)
        if ms and ms.stage in ("shoot", "confirm", "filled", "graded"):
            # "Fill" / "Submit" typed instead of tapped (2026-09-27: both were
            # answered with the help text, and nothing was submitted)
            word = text.strip().lower().rstrip(".!")
            if word in _FILL_WORDS or word in _SUBMIT_WORDS:
                if ms.stage == "filled" and word in _SUBMIT_WORDS:
                    return self._math_submit()
                if ms.answers or ms.picked:
                    return self._math_fill(force=True)
                return self._send("Nothing answered yet — tap a choice, type <code>3: …</code>, "
                                  "or send a photo of the sheet.")
        if ms and ms.stage in ("shoot", "confirm", "filled", "graded"):   # typing works before a photo too
            tc = typed_choice(text)
            if tc and self._typed_choice(ms, *tc):
                return
            fix = _MATH_FIX_RE.match(text)
            if fix:
                return self._math_fix(int(fix.group(1)), fix.group(2).strip())
        if ms and ms.stage in ("shoot", "confirm", "filled", "graded"):
            code = text.strip().upper().replace(" ", "")
            if not (_CODE_RE.match(code) and any(m.code == code for m in self.cs.menu)):
                # Mid-round, "reply a code from /todo" reads as "start over".
                # Say what works here instead; the round stays as it is.
                return self._send(
                    f"You're in <b>{esc(ms.title)}</b> — nothing is lost. To change something:\n"
                    "· a choice: tap its button again (any of them, any time)\n"
                    "· one written answer: <code>3: \\frac{13}{2}</code>\n"
                    "· the whole sheet: send a new photo\n"
                    "/cancel leaves this round.")
        cs = self.cs
        if cs.pending is None:
            code = text.strip().upper().replace(" ", "")
            if _CODE_RE.match(code) or code.isdigit():
                return self._open(code)
            return self._send("Reply a code from /todo (like C2), or /todo to refresh.")
        # something pending: this text is a submission (or a url)
        item = cs.pending
        if item.kind == "discussion":
            new = Draft(mode="text", text=text)
        elif _URL_RE.match(text) and "online_url" in item.types:
            new = Draft(mode="url", text=text)
        elif "online_text_entry" in item.types:
            new = Draft(mode="text", text=text)
        elif "online_url" in item.types:
            return self._send("That doesn't look like a URL. Send a link, or a file.")
        else:
            return self._send("This one takes a file upload. Send the file.")
        # mixed draft: the last type wins — dropping queued files must be said out loud
        dropped = self._drop_files()
        cs.draft = new
        self._preview(note=f"(replaced your {dropped} queued file{'s' if dropped != 1 else ''})" if dropped else "")

    def _drop_files(self) -> int:
        """Forget queued upload bytes + entries; returns how many were dropped."""
        cs = self.cs
        n = len(cs.draft.files) if cs.draft and cs.draft.mode == "file" else 0
        self._file_bytes.pop(self.chat_id, None)
        return n

    def _open(self, code: str) -> None:
        cs = self.cs
        item = next((m for m in cs.menu if m.code == code or (code.isdigit() and m.idx == int(code))), None)
        if not item:
            return self._send("No such code. /todo to refresh.")
        if item.kind in ("external", "quiz", "webassign"):
            # An external item that is WebAssign homework can be done right here
            # — handing back a link is the fragmentation this exists to remove.
            # An "external" item whose tool Canvas already points somewhere
            # other than WebAssign (McGraw Hill, Macmillan, New Quizzes, a host
            # we have never seen) is not WebAssign: asking WebAssign about it
            # only fails (403 on the LTI launch) and then rebuilds the whole
            # WebAssign session to title-match — 16 s + 17 s before "I can't hand
            # this in" on the first Spanish item of the 2026-09-25 sweep, 16.5 s
            # + 13.9 s on a Macmillan ECON101 podcast (2026-09-26), and a chance
            # of matching a wrong assignment. Only an item with no tool url is
            # worth asking.
            other = item.kind == "external" and self._not_webassign(item.tool)
            dep = self._webassign_dep(item) if item.kind in ("external", "webassign") and not other else None
            if dep is None and item.kind == "webassign":
                # /todo promised this one could be done here; say why it can't
                # rather than quietly handing back a link.
                return self._send(
                    f"I couldn't open <b>{esc(item.title)}</b> on WebAssign just now. "
                    f"Try the code again in a moment, or use the page:\n{esc(item.url)}")
            if dep is not None:
                # Straight in. A code from /todo already means "open this one",
                # and a second tap here would protect nothing — except for a
                # quiz whose rules we don't know, which still asks.
                if (_QUIZ_TITLE.search(item.title or "")
                        and not _UNTIMED_QUIZ_TITLE.search(item.title or "")):
                    return self._math_confirm_quiz(dep)
                return self._math_open(dep, item)
            if item.kind == "quiz":
                return self._quiz_open(item)
            # Any other external item falls through. Returning a link here meant
            # the description and the attached PDF — which live on CANVAS, not on
            # the vendor — were never sent (PHYSICS 247 Problem Set 3).

        if item.kind == "discussion":
            cs.pending, cs.draft = item, None
            return self._send(f"💬 <b>{esc(item.title)}</b>  <i>{esc(item.course)}</i> · {item.due}\n"
                              f"{esc(item.url)}\n\nSend your post as text.")

        try:
            d = self.canvas.assignment_detail(item.course_id, item.id)
        except CanvasHTTPError:
            if item.kind != "external":
                raise
            # An external item Canvas won't describe still has its link.
            return self._send(f"<b>{esc(item.title)}</b> is on an external platform:\n{esc(item.url)}")
        text, links = html_to_text(d["description_html"], self.canvas.base_url)
        if d["locked"]:
            return self._send(f"🔒 <b>{esc(d['name'])}</b> is locked: {esc(d['lock_explanation'] or 'no reason given')}")
        from ..external.platforms import classify
        plat = classify(d.get("external_tool_url") or "")
        item.vendor = plat.key if plat is not None and "external_tool" in d["submission_types"] else ""
        external = "external_tool" in d["submission_types"]
        # Something we can hand in stays "pending" so the next file lands on it;
        # an external item we cannot work is shown and let go.
        cs.pending, cs.draft = (item if not external or item.vendor == "gradescope" else None), None
        head = (f"📌 <b>{esc(d['name'])}</b>  <i>{esc(item.course)}</i>\n"
                f"due {item.due} · {d['points_possible'] or 0:g} pts"
                + (f" · attempt {d['attempt']} submitted {d['submitted_at'][:10]}" if d.get("submitted_at") else "")
                + "\n" + self._where_it_goes(d, item.course_id) + "\n")
        body = esc(text[:3000]) if text else "<i>(no description)</i>"
        self._send(head + "\n" + body)

        # attached Canvas files: push them into the chat (≤ 20 MB each)
        for label, cid, fid in canvas_file_ids(links)[:5]:
            try:
                name, data = self.canvas.download_file(fid)
                # a link whose text IS its address (<a href=X>X</a>) has no name
                # to show; the file's own name says what it is
                self.tg.send_document(self.chat_id, name, data,
                                      caption=esc((name if _URL_RE.match(label) else label)[:200]))
            except Exception as e:           # one bad attachment is not a failed open
                self._send(f"(couldn't fetch {esc(label)}: {esc(str(e))[:120]})")

        if item.vendor == "gradescope":
            return self._send("Send me your pages — photos (I'll bind them into one PDF, in the order "
                              "you send them) or a PDF — and I'll upload it to <b>Gradescope</b> after "
                              "you confirm.")
        if external:
            return self._send_external(item, plat, d.get("external_tool_url") or "")
        accepts = []
        if "online_text_entry" in item.types:
            accepts.append("text")
        if "online_upload" in item.types:
            exts = _extensions(d["allowed_extensions"])
            accepts.append("a file" + (f" ({', '.join(exts)})" if exts else ""))
        if "online_url" in item.types:
            accepts.append("a link")
        if not getattr(self, "_sweeping", False):        # a sweep never hands anything in
            self._send("Send " + " / ".join(accepts) + " and I'll submit it for you.")

    def _open_elsewhere(self, item) -> Optional[dict]:
        """A keyboard that opens this item somewhere already signed in, or
        None. The owner's bot has no such place (its token gets the one-time
        link); a classmate's assistant opens it in their own browser."""
        return None

    def _send_external(self, item, plat, tool_url: str) -> None:
        """A platform the bot can't work yet: hand over a ONE-TAP way in.

        The Canvas address alone failed on phones (2026-09-26): the phone's
        browser is not logged in to Canvas, so the tool never loaded. Canvas's
        sessionless launch is the same one-time link Canvas itself uses to hand
        a student to the vendor — it opens the platform directly, no Canvas
        login. It carries a credential: it goes only to this person's chat as a
        button, never into logs (transcript.py strips link queries)."""
        from urllib.parse import urlsplit
        from ..util import metrics
        host = (urlsplit(tool_url).hostname or "?") if tool_url else "?"
        name = plat.label if plat is not None else host
        log.info("external platform for %s: %s", item.code, host)
        metrics.emit("external", host=host, platform=getattr(plat, "key", "") or "")
        try:
            launch = self.canvas.sessionless_launch(item.course_id, item.id)
        except Exception as e:
            log.info("sessionless launch failed for %s: %s", item.code, type(e).__name__)
            launch = ""
        if not launch:
            # Canvas gives no one-time link from a browser login for LTI 1.3
            # tools (McGraw Hill: 403 on a cookie session, fine on a token).
            kb = self._open_elsewhere(item)
            if kb:
                return self._send(f"I can't work on <b>{esc(name)}</b> yet — do it there. The button "
                                  "opens it in <b>your own browser</b>, already signed in to Canvas, "
                                  "right here in Telegram.", kb)
            return self._send(f"I can't hand this one in for you yet — it's done on <b>{esc(name)}</b>:\n"
                              f"{esc(item.url)}")
        self._send(f"I can't work on <b>{esc(name)}</b> yet — do it there. The button opens it "
                   f"directly (no Canvas login needed). It's a one-time link: if it has expired, "
                   f"send <b>{esc(item.code or '')}</b> again for a fresh one.",
                   {"inline_keyboard": [[{"text": f"↗ Open {name[:30]}", "url": launch}]]})

    # -- Gradescope: the same confirm-then-send, to a different place ----------

    def _gs(self):
        if self._gradescope is None:
            from ..external.gradescope import Gradescope
            self._gradescope = Gradescope()
        return self._gradescope

    def _gradescope_submit(self, item: MenuItem, d: Draft, blobs: list[bytes]) -> None:
        """Upload what the student sent, as ONE PDF — only ever after their ✅."""
        from ..external.gradescope import GradescopeUnavailable, as_one_pdf
        cs = self.cs
        try:
            name, pdf = as_one_pdf([(f["filename"], f["content_type"], b) for f, b in zip(d.files, blobs)])
        except ValueError as e:
            cs.draft = None
            return self._send(f"❌ {esc(str(e))} Nothing was uploaded — send them again.")
        self._send(f"Uploading {esc(name)} ({len(pdf) // 1024} KB) to Gradescope…")
        retry = inline_keyboard([[("🔁 Try again", "submit"), ("✖ Cancel", "cancel")]])
        try:
            out = self._gs().upload_pdf(item.url, name, pdf)
        except GradescopeUnavailable as e:
            # Stopped before or during the upload: nothing was submitted, and
            # their file stays here for a one-tap retry.
            self._file_bytes[self.chat_id] = blobs
            return self._send(f"⚠️ {esc(str(e))}\nNothing was uploaded. Your file is still here.", retry)
        if out.get("shot"):
            try:
                self.tg.send_photo(self.chat_id, out["shot"], filename="gradescope.png")
            except TelegramError as e:
                log.warning("sendPhoto gradescope failed: %s", e)
        status = out.get("status") or ("uploaded" if out.get("moved") else "unknown")
        if status == "failed":
            # Definitely not submitted (still the form, or Gradescope's 404):
            # keep their file, so trying again is one tap, not a resend.
            self._file_bytes[self.chat_id] = blobs
            return self._send("❌ It did <b>not</b> go through — Gradescope didn't accept the upload "
                              "(nothing was submitted). Your file is still here.", retry)
        cs.pending, cs.draft = None, None
        if status != "uploaded":
            # Left the form but not for a submission page: saying "uploaded"
            # would be a guess; the picture above is what is known.
            return self._send("⚠️ Gradescope moved on, but not to a submission page, so I can't tell "
                              f"whether it went through. Check here:\n{esc(item.url)}")
        cs.menu = [m for m in cs.menu if m.code != item.code]
        self._send(f"📤 Uploaded to Gradescope for <b>{esc(item.title)}</b>.\n"
                   f"Gradescope now shows: <i>{esc(out.get('title') or '?')}</i>\n\n"
                   "⚠️ Gradescope asks you to mark which pages answer which question — I can't do "
                   f"that part yet. Open it once to finish:\n{esc(item.url)}")

    # -- incoming file --------------------------------------------------------

    def _incoming_file(self, m: dict) -> None:
        cs = self.cs
        if cs.pending is None:
            return self._send("Pick an item first: /todo")
        if "online_upload" not in cs.pending.types and cs.pending.vendor != "gradescope":
            return self._send("This item doesn't take files. Send text" + (" or a link." if "online_url" in cs.pending.types else "."))
        # a file draft is a queue: every new file/photo APPENDS (an album arrives
        # as N messages sharing media_group_id — each one lands here in turn)
        note = ""
        if cs.draft is None or cs.draft.mode != "file":
            if cs.draft is not None:
                note = f"(replaced your {'link' if cs.draft.mode == 'url' else 'text'} draft)"
            cs.draft = Draft(mode="file")
            self._file_bytes[self.chat_id] = []
            self._preview_msg.pop(self.chat_id, None)    # a new queue gets a new preview
        elif self._file_bytes.get(self.chat_id) is None or len(self._file_bytes[self.chat_id]) != len(cs.draft.files):
            # restarted mid-queue: the names survived on disk, the bytes didn't — start over
            lost = cs.draft.filenames
            cs.draft, self._file_bytes[self.chat_id] = Draft(mode="file"), []
            note = f"(I lost {', '.join(lost)} in a restart — please re-send them)"
        d = cs.draft
        if m.get("document"):
            doc = m["document"]
            fid, ctype = doc["file_id"], doc.get("mime_type") or "application/octet-stream"
            name = _unique_name(doc.get("file_name") or "upload", d.filenames)
        else:
            best = max(m["photo"], key=lambda p: p.get("file_size") or 0)
            fid, ctype = best["file_id"], "image/jpeg"
            name = _unique_name(f"photo-{len(d.files) + 1}.jpg", d.filenames)
        try:
            _, data = self.tg.download(fid)
        except TelegramError as e:
            return self._send(f"Couldn't download that ({esc(str(e))[:100]}). Files over 20 MB can't come through Telegram.")
        self._file_bytes[self.chat_id].append(data)
        d.files.append({"filename": name, "content_type": ctype, "size": len(data)})
        self._preview(note=note)

    # -- confirm & submit -----------------------------------------------------

    def _preview(self, note: str = "") -> None:
        cs = self.cs
        item, d = cs.pending, cs.draft
        if not item or not d:
            return
        verb = ("post to" if item.kind == "discussion"
                else "upload to Gradescope for" if item.vendor == "gradescope" else "submit to")
        target = f"→ {verb} <b>{esc(item.title)}</b> <i>{esc(item.course)}</i>"
        tail = f"\n{esc(note)}" if note else ""
        if d.mode == "file":
            n = len(d.files)
            names = ", ".join(esc(x) for x in d.filenames)
            total_kb = sum(f["size"] for f in d.files) // 1024
            what = f"📎 {n} file{'s' if n != 1 else ''} ({total_kb} KB): {names} — send more, or ✅"
            text = f"{what}\n\n{target}\nConfirm?{tail}"
            kb = inline_keyboard([[("✅ Submit all", "submit"), ("🗑 Remove last", "remove_last"),
                                   ("✖ Cancel", "cancel")]])
            # An album arrives as one message per file. Rewrite the same preview
            # instead of sending "1 file… 2 files… 3 files…" one under another.
            mid = self._preview_msg.get(self.chat_id)
            if mid is not None:
                try:
                    return self.tg.edit_text(self.chat_id, mid, text, kb)
                except TelegramError:
                    pass                          # deleted, too old: send a fresh one
            self._preview_msg[self.chat_id] = self.tg.send(self.chat_id, text, reply_markup=kb)[-1]
            return
        if d.mode == "url":
            what = f"🔗 {esc(d.text)}"
        else:
            what = f"✍️ {esc(d.text[:600])}" + (" …" if len(d.text) > 600 else "")
        self._send(f"{what}\n\n{target}\nConfirm?{tail}",
                   inline_keyboard([[("✅ Submit", "submit"), ("✖ Cancel", "cancel")]]))

    def _callback(self, cb: dict) -> None:
        chat = str(((cb.get("message") or {}).get("chat") or {}).get("id"))
        if chat != self.chat_id:
            return
        data = cb.get("data")
        self.tg.answer_callback(cb["id"])
        # The same button tapped again within 3 s is one tap: a triple tap on
        # "Look 14 days ahead" sent three identical lists (2026-09-26).
        # Only for list buttons: an answer button may rightly be tapped twice.
        if (data or "").startswith("todo:"):
            key = (data, (cb.get("message") or {}).get("message_id"))
            now = time.monotonic()
            if getattr(self, "_last_tap", (None, 0.0))[0] == key and now - self._last_tap[1] < 3:
                return
            self._last_tap = (key, now)
        cs = self.cs
        if data == "cancel":
            self._drop_everything(keep_menu=True)
            return self._send("Cancelled. Nothing was sent, nothing kept. Your /todo codes still work.")
        if data.startswith("todo:"):
            return self._todo(int(data[5:]) if data[5:].isdigit() else None)
        if data.startswith("c:"):
            _, dep, idx, value = data.split(":", 3)
            return self._tap(int(idx), value, dep, (cb.get("message") or {}).get("message_id"))
        if data.startswith("cdone:"):
            return self._maybe_offer_fill()
        if data.startswith("mq:"):
            return self._math_confirm_quiz(data[3:])
        if data.startswith("m:"):
            return self._math_open(data[2:])
        if data.startswith("q") and data[:3] in ("qs:", "qa:", "qm:", "qS:", "qp:"):
            return self._quiz_callback(data)
        if data.startswith("mstep:"):
            _, dep, button_id = data.split(":", 2)
            return self._math_begin_step(dep, button_id)
        if data in ("mfill", "mfill!", "mfill!+"):
            if not (cs.math and (cs.math.answers or cs.math.picked)):
                return self._send("Nothing to fill. /todo to start again.")
            return self._math_fill(force="!" in data, then_submit=data.endswith("+"))
        if data.startswith("mres:"):
            return self._resume(data[5:])
        if data.startswith(("mredo:", "mfinish:")):
            ms = cs.math
            if not ms or data.split(":", 1)[1] != ms.dep:
                return self._send("That round is over. /todo to start again.")
            if data.startswith("mfinish:"):
                prog = cs.wa_progress.get(ms.dep, {})
                cs.math = None
                self._send(f"✅ Finished <b>{esc(ms.title)}</b>"
                           + (f" — {prog['points']:g}/{prog['max']:g} points." if prog.get("max") else "."))
                return self._refresh_todo()
            item = next((m for m in cs.menu if ms.item_id and m.id == ms.item_id), None)
            return self._math_open(ms.dep, item, only=set(ms.redo))
        if data == "mlater":
            if not cs.math:
                return self._send("That round is over. /resume to see what's set aside.")
            return self._math_later()
        if data in ("mmodify", "mback") or data.startswith(("msubmit", "mfix:")):
            if not (cs.math and (cs.math.answers or cs.math.picked)):
                return self._send("That round is over. /todo to start again.")
            filled = cs.math.stage == "filled"
            if data == "mmodify":
                return self._math_modify()
            if data == "mback":
                if not filled:              # the page no longer matches: fill first
                    return self._send(self._math_preview(cs.math), inline_keyboard(
                        [[self._fill_btn(), ("✖ Cancel", "cancel")]]))
                return self._send(self._math_preview(cs.math), inline_keyboard(
                    [[("✅ Submit", f"msubmit:{cs.math.dep}")],
                     [("✏️ Modify", "mmodify"), ("✖ Cancel", "cancel")]]))
            if data.startswith("msubmit"):
                # WebAssign grades what the PAGE holds, not what this chat shows.
                # So only this round's button, and only while the page still
                # matches: an old Submit pressed after a fix graded the old answer.
                if data != f"msubmit:{cs.math.dep}":
                    return self._send("That Submit button is from another round — nothing was submitted.")
                if not filled:
                    return self._send(
                        "Changed since filling — nothing was submitted. Fill again so the "
                        "page matches, then submit.\n\n" + self._math_preview(cs.math),
                        inline_keyboard([[self._fill_btn(), ("✖ Cancel", "cancel")]]))
                return self._math_submit()
            return self._math_ask_fix(int(data.split(":")[1]))
        if data == "remove_last":
            d = cs.draft
            if not cs.pending or not d or d.mode != "file" or not d.files:
                return self._send("Nothing queued. /todo")
            gone = d.files.pop()["filename"]
            blobs = self._file_bytes.get(self.chat_id)
            if blobs:
                blobs.pop()
            if not d.files:
                cs.draft = None
                self._file_bytes.pop(self.chat_id, None)
                return self._send(f"Removed {esc(gone)}. Queue is empty — send a file, or /cancel.")
            return self._preview(note=f"(removed {gone})")
        if data != "submit" or not cs.pending or not cs.draft:
            return self._send("Nothing pending. /todo")
        item, d = cs.pending, cs.draft
        if item.kind == "discussion":
            r = self.canvas.post_discussion_entry(item.course_id, item.id, _para(d.text))
            receipt = f"💬 Posted to <b>{esc(item.title)}</b> · {esc((r.get('created_at') or '')[:16])}"
        elif d.mode == "file":
            blobs = self._file_bytes.get(self.chat_id)
            if not d.files or blobs is None or len(blobs) != len(d.files):
                # names persisted across a restart but the bytes live only in memory
                cs.draft = None
                self._file_bytes.pop(self.chat_id, None)
                n = len(d.files)
                return self._send(f"I lost the {n} file{'s' if n != 1 else ''} (restart?). Please re-send "
                                  f"{'them' if n != 1 else 'it'}. Nothing was submitted.")
            self._file_bytes.pop(self.chat_id, None)
            if item.vendor == "gradescope":
                return self._gradescope_submit(item, d, blobs)
            r = self.canvas.submit_files(item.course_id, item.id,
                                         [(f["filename"], b, f["content_type"]) for f, b in zip(d.files, blobs)])
            names = r["attachments"] if len(r["attachments"]) == len(d.files) else d.filenames
            receipt = _receipt(item, r, f"📎 {len(names)} file{'s' if len(names) != 1 else ''}: {', '.join(names)}")
        elif d.mode == "url":
            r = self.canvas.submit_url(item.course_id, item.id, d.text)
            receipt = _receipt(item, r, f"🔗 {d.text}")
        else:
            r = self.canvas.submit_text(item.course_id, item.id, _para(d.text))
            receipt = _receipt(item, r, "✍️ text")
        cs.pending, cs.draft = None, None
        # done → drop it from the menu and point at what's left, so "what next" needs no /todo
        cs.menu = [m for m in cs.menu if m.code != item.code]
        left = [m for m in cs.menu if m.kind in ("assignment", "discussion")]
        if left:
            nxt = " · ".join(f"<b>{m.code}</b> {esc(m.title[:28])}" for m in left[:3])
            receipt += "\n\n⏭ Next: " + nxt + (f" · +{len(left) - 3} more" if len(left) > 3 else "")
        else:
            receipt += "\n\n🎉 Nothing else to do here in the next 4 days. /todo to refresh."
        self._send(receipt)


def _unique_name(name: str, taken: list[str]) -> str:
    """notes.pdf → notes-2.pdf, notes-3.pdf … so Canvas never sees two attachments with one name."""
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    stem, ext = (stem, "." + ext) if dot and stem else (name, "")
    n = 2
    while f"{stem}-{n}{ext}" in taken:
        n += 1
    return f"{stem}-{n}{ext}"


def _para(text: str) -> str:
    """Plain text -> minimal HTML Canvas will render with line breaks intact."""
    return "".join(f"<p>{_html.escape(p.strip())}</p>" for p in re.split(r"\n\s*\n", text.strip()) if p.strip()) or "<p></p>"


def _receipt(item: MenuItem, r: dict, what: str) -> str:
    when = (r.get("submitted_at") or "")[:16].replace("T", " ")
    late = " · <b>late</b>" if r.get("late") else ""
    return (f"✅ Submitted <b>{esc(item.title)}</b> <i>{esc(item.course)}</i>\n"
            f"{esc(what)} · attempt {r.get('attempt') or 1} · {esc(when)} UTC{late}")
