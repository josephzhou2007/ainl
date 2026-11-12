"""WebAssign (Cengage) through the student's own logged-in browser window.

Provenance: docs/cengage-webassign-api.md, verified on a live page 2026-09-19
(scripts/wa_inspect_fill.py).

A headless Chromium is started by scripts/browser_start.py as its own process,
with a debugging port; this module attaches, does one thing, and leaves it
running. It never starts or stops the browser.

The WebAssign login does NOT survive a browser restart — its real cookies
(JSESSIONID, cmp-session-id) are session-only — but it needs no password
either: on finding itself logged out, this module re-launches from Canvas over
LTI and carries on (see _webassign_tab).

What it does:
  - list everything on "My Assignments" — homework and quizzes alike. Whether
    a particular quiz is safe to open is the caller's call, not this module's:
    Assignment.needs_confirm says which ones we don't know the rules for.
  - open one and return a picture of each question (innerText flattens the
    maths into nonsense, so the student is shown images, not text)
  - FILL answers: write MathML into each question's hidden RA_ field and
    re-render the visible box from it

  - SUBMIT the student's own answers, but only after they have seen them
    rendered and tapped confirm in chat

Filling costs no attempt ("the number of submissions remaining only changes if
you submit"), so the irreversible step is exactly one tap, on work the student
did by hand. The agent moves that work; it never writes an answer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..util import browserthread
from ..util import timing
from ..util.timing import span, step

log = logging.getLogger("ainl.webassign")

CDP_URL = "http://127.0.0.1:9222"
WA_LIST = "https://www.webassign.net/v4cgi/student.pl?action=home/index"
_DASH = "cengage.com/dashboard"
_REDACT = re.compile(r"(UserPass|CSRFToken|key)=[^&]*")
_QUIZ = re.compile(r"\bquiz\b", re.I)

# Quizzes known not to be timed or attempt-limited, so opening one costs
# nothing and needs no confirmation.
#
# MATH222's Module Quizzes: the student said untimed and unlimited, and the
# page agrees — opening Module Quiz 9 (2026-09-19) showed no timer text and no
# submissions-used limit, only "you submit answers by question parts".
#
# Anything else matching "quiz" still asks first. Elsewhere a quiz may well be
# timed, and starting a clock cannot be undone — so the default stays "ask",
# and this list grows only from evidence like the above.
_UNTIMED_QUIZ = re.compile(r"\bmodule\s+quiz\b", re.I)


class WebAssignUnavailable(RuntimeError):
    """No window to attach to, or it isn't logged in. The message says what to do."""


def redact(url: str) -> str:
    return _REDACT.sub(r"\1=<redacted>", url)


@dataclass
class Assignment:
    dep: str
    title: str
    due: str = ""
    href: str = field(default="", repr=False)   # carries the session; never log it

    @property
    def is_quiz(self) -> bool:
        return bool(_QUIZ.search(self.title))

    @property
    def needs_confirm(self) -> bool:
        """A quiz whose rules we do not know — ask before opening it."""
        return self.is_quiz and not _UNTIMED_QUIZ.search(self.title)


@dataclass
class Option:
    """One choice on a multiple-choice part."""
    value: str
    label: str = ""
    image: str = ""        # some options ARE pictures (direction fields) — no label


@dataclass
class Part:
    """One answer box. The unit is the part, not the question.

    Measured on this course: 7.8 Homework q2-q4 each hold a radio choice
    (convergent / divergent) AND a maths box for the value. Treating a question
    as having one kind would have made those unanswerable.
    """
    kind: str              # math | choice | multi | dropdown | text | essay | step | unknown
    name: str              # the input's name: RA_… , or the radio group, or the select
    options: list[Option] = field(default_factory=list)
    label: str = ""        # "Exercise (a)" on a step button; "Step 5" on a box inside a
                           # Master It step; empty otherwise
    wrong: bool = False    # the page shows ✗ beside this box (a step to retry)
    graded: bool = False   # the page shows ✓ beside it: answered, stays on the page
    text: str = ""         # a step's own text ("Step 5 Ignoring the constant …"), for
                           # the test-only autosolve; empty outside a step

    @property
    def is_math(self) -> bool:
        return self.kind == "math"

    @property
    def is_typed(self) -> bool:
        """A short answer typed as-is: a numeric RN_ box, a plain text box."""
        return self.kind == "text"

    @property
    def written(self) -> bool:
        """Goes on the answer sheet — maths or typed; the two differ only in
        how the value is written into the page (MathML vs the bare text)."""
        return self.kind in ("math", "text")

    @property
    def tappable(self) -> bool:
        """Answerable by tapping a button — no writing, no model call."""
        return self.kind in ("choice", "multi", "dropdown")


@dataclass
class Question:
    n: int                 # 1-based, as printed on the page
    dom_id: str            # question<qid>_<qIdx>
    fields: list[str]      # RA_ names only — the maths boxes, in page order
    parts: list[Part] = field(default_factory=list)
    stem: str = ""

    @property
    def kinds(self) -> list[str]:
        return sorted({p.kind for p in self.parts}) or ["unknown"]



_LIST_JS = r"""() => [...document.querySelectorAll('a[href*="dep="]')].map(a => {
  const t = a.innerText.replace(/\s+/g, ' ').trim();
  const m = t.match(/^(.*?)\s+((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),.*)$/);
  return {dep: (a.href.match(/[?&]dep=(\d+)/) || [])[1], href: a.href,
          title: m ? m[1] : t, due: m ? m[2] : ''};
}).filter(x => x.dep)"""

# Every answer box in a question, with the kind of answer it wants. The kinds
# are named for what the STUDENT has to do, not for WebAssign's markup:
#
#   choice / multi / dropdown -> tap; nothing to write, no model call
#   math                      -> the handwriting -> LaTeX -> MathML path
#   text                      -> type a short answer (RN_ numeric boxes too:
#                                9.3 Homework q7(b) "round to one decimal place")
#   essay                     -> type a long one
#   step                      -> a "Master It" (MI.SA) question: its parts open
#                                one at a time behind "Click here to begin!",
#                                and a skipped part is forfeited. Only the FIRST
#                                unopened step is offered, so nothing is skipped.
#
# Surveyed across this course 2026-09-19: math 35, choice 9, text 2,
# dropdown 1, multi 1, unknown 1 — and three questions carry a choice AND a
# maths box at once, which is why this is per part.
_QUESTIONS_JS = r"""() => {
  const label = el => {
    const l = el.closest('label') || document.querySelector(`label[for="${el.id}"]`);
    const src = l || el.parentElement;
    // 240, not 80: 9.2 Homework's options ended mid-sentence at 80 ("… the
    // slopes are the same along", "on the line y = −"), 2026-09-27.
    return (src ? src.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 240);
  };
  const optImage = el => {
    const l = el.closest('label') || el.parentElement;
    const img = l ? l.querySelector('img') : null;
    return img ? img.src : '';
  };
  // Answer fields in page order: RA_, RQ_, … — anything R<letter>_ that is
  // not a radio or checkbox and is an RA_ or has a WIRIS editor.
  const boxesOf = el => [...el.querySelectorAll('input[name^="R"]')].filter(i =>
    /^R[A-Z]_\d+_\d+_/.test(i.name || '') && i.type !== 'radio' && i.type !== 'checkbox'
    && (/^RA_/.test(i.name) || !!el.querySelector(`#editable-math-${i.name}`)));
  return [...document.querySelectorAll('[id^="question"]')]
    .filter(e => /^question\d+_\d+$/.test(e.id))
    .map(el => {
      const parts = [];
      // radios, grouped by name: one group is one choice part
      const groups = {};
      el.querySelectorAll('input[type=radio]').forEach(r => {
        (groups[r.name] = groups[r.name] || []).push(r);
      });
      for (const [name, rs] of Object.entries(groups)) {
        parts.push({kind: 'choice', name,
                    options: rs.map(r => ({value: r.value, label: label(r),
                                           image: optImage(r)}))});
      }
      // checkboxes, also grouped by name
      const boxes = {};
      el.querySelectorAll('input[type=checkbox]').forEach(c => {
        (boxes[c.name] = boxes[c.name] || []).push(c);
      });
      for (const [name, cs] of Object.entries(boxes)) {
        parts.push({kind: 'multi', name,
                    options: cs.map(c => ({value: c.value, label: label(c),
                                           image: optImage(c)}))});
      }
      el.querySelectorAll('select').forEach(sel => {
        parts.push({kind: 'dropdown', name: sel.name || sel.id,
                    options: [...sel.options].map(o => ({value: o.value,
                                                         label: (o.text || '').trim(),
                                                         image: ''}))});
      });
      // A box inside a Master It step: its heading ("Step 5") and whether the
      // page has marked it wrong (the box stays, with ✗, for another try).
      const stepOf = i => {
        const sec = i.closest('.section');
        if (!sec || !sec.closest('.tutorial')) return '';
        const m = (sec.innerText || '').trim().match(/^(Step\s*\d+)/i);
        return m ? m[1] : '';
      };
      const wrongOf = i => {
        const sec = i.closest('.section');
        return !!(sec && sec.closest('.tutorial')
                  && sec.querySelector('img[src*="mIncorrect"], .mIncorrect'));
      };
      // A step answered right keeps its box (a maths box does; a number box
      // turns into text) with ✓ beside it — done, not open.
      const gradedOf = i => {
        const sec = i.closest('.section');
        return !!(sec && sec.closest('.tutorial') && !wrongOf(i)
                  && sec.querySelector('img[src*="mCorrect"], .mCorrect'));
      };
      const textOf = i => {
        const sec = i.closest('.section');
        if (!sec || !sec.closest('.tutorial')) return '';
        return (sec.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 600);
      };
      // An answer field is R<letter>_<qid>_<idx>_… . RA_ is the usual one, but
      // not the only one: 7.8 Homework q1 (2026-09-25) had an RQ_ box with the
      // same WIRIS editor, and it was never seen — its answer left blank. So:
      // any such field with a WIRIS editor beside it is a maths box; an RA_
      // without one is a plain text answer. (RC_/RS_ are radios/checkboxes,
      // handled above; RN_ is a numeric text box, handled below.)
      const isBox = i => /^R[A-Z]_\d+_\d+_/.test(i.name || '') && i.type !== 'radio'
        && i.type !== 'checkbox' && (/^RA_/.test(i.name) || !!el.querySelector(`#editable-math-${i.name}`));
      boxesOf(el).forEach(i => {
        const hasEditor = !!el.querySelector(`#editable-math-${i.name}`);
        parts.push({kind: hasEditor ? 'math' : 'text', name: i.name, options: [],
                    label: stepOf(i), wrong: wrongOf(i), graded: gradedOf(i), text: textOf(i)});
      });
      el.querySelectorAll('input[type=text]').forEach(t => {
        if (!isBox(t)) parts.push({kind: 'text', name: t.name || t.id, options: [],
                                                     label: stepOf(t), wrong: wrongOf(t),
                                                     graded: gradedOf(t), text: textOf(t)});
      });
      [...el.querySelectorAll('textarea')]
        .filter(t => !/latex-source/.test(t.id))
        .forEach(t => parts.push({kind: 'essay', name: t.name || t.id, options: []}));
      // A tutorial step not yet begun: no boxes exist until its button is
      // pressed. Seen on 9.3 Homework q6 (SCalc9 9.3.047.MI.SA), 2026-09-25.
      const step = [...el.querySelectorAll('input[type=submit][id^="step_button_id_"]')]
        .find(b => b.offsetParent !== null);
      if (step) {
        const tut = step.closest('.tutorial');
        const head = tut && tut.querySelector('.header_title');
        parts.push({kind: 'step', name: step.id, options: [],
                    label: (head ? head.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 40)});
      }
      if (!parts.length) parts.push({kind: 'unknown', name: '', options: []});
      return {
        dom_id: el.id,
        parts,
        fields: boxesOf(el).map(i => i.name),
        stem: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 700),
      };
    });
}"""

# The fill, as established on the live page:
#   renderPreview(boxId) draws the visible box FROM the hidden field's value,
#   so writing the field and re-rendering is enough — no editor involved.
# Deliberately NOT syncAnswerValue(boxId): it copies the shared WIRIS editor's
# current content INTO the field, and when the editor is attached to another
# box that is empty — it would wipe the answer we just wrote.
_FILL_JS = r"""(answers) => {
  const done = [], missing = [];
  for (const [boxId, mathml] of Object.entries(answers)) {
    const f = document.getElementById(boxId) || document.querySelector(`input[name="${boxId}"]`);
    if (!f) { missing.push(boxId); continue; }
    f.value = mathml;
    // NOT `new Event(...)`: MooTools 1.3 replaces the global Event class with
    // its own, whose constructor expects a real event and throws on a string.
    try {
      const ev = document.createEvent('HTMLEvents');
      ev.initEvent('change', true, false);
      f.dispatchEvent(ev);
    } catch (e) {}
    try { window.mathTypeOverlay && window.mathTypeOverlay.renderPreview(boxId); } catch (e) {}
    done.push(boxId);
  }
  return {done, missing};
}"""

# Which of these fields are plain text boxes a student types into (RN_ number
# boxes, text RA_ boxes) — no WIRIS editor, visible. Setting .value is not
# enough for those: 9.2 Homework q4 (2026-09-27) showed 15.9389 in the box
# after the fill, then went in EMPTY, twice — WebAssign keeps what was TYPED.
_PLAIN_JS = r"""(ids) => ids.filter(id => {
  const f = document.getElementById(id) || document.querySelector(`input[name="${id}"]`);
  return !!(f && f.type === 'text' && f.offsetParent !== null
            && !document.getElementById('editable-math-' + id));
})"""

# After a submit the question header carries the score ("1. [1.25 / 1.25 Points]")
# and the page marks each part right or wrong. Read both back so the student is
# told the result in chat instead of having to go and look.
_OUTCOME_JS = r"""(doms) => Object.fromEntries(doms.map(id => {
  const el = document.getElementById(id);
  if (!el) return [id, null];
  const head = (el.innerText || '').slice(0, 120).replace(/\s+/g, ' ').trim();
  const pts = (head.match(/\[([^\]]*Points[^\]]*)\]/i) || [])[1] || null;
  const marks = [...el.querySelectorAll('img[src*="correct"], img[src*="incorrect"], .correct, .incorrect')]
    .map(n => (n.getAttribute('src') || n.className || '')).map(s =>
      /incorrect|wrong|red/i.test(s) ? 'wrong' : /correct|check|green/i.test(s) ? 'right' : '?');
  return [id, {points: pts, marks}];
}))"""

# Which button submits these boxes? A box inside a Master It step has its own
# SUBMIT (tutorialSubmit('ra_…')); everything else is the question's own
# "Submit Answer" (#submit_<qid>_<idx>). The FIRST submit input in a question
# is neither in general — before a step is begun it is "Click here to begin!".
_SUBMIT_BUTTON_JS = r"""([dom, boxes]) => {
  const q = document.getElementById(dom);
  if (!q) return null;
  for (const b of boxes) {
    const key = b.toLowerCase();
    const btn = [...q.querySelectorAll('input[type=submit]')]
      .find(el => (el.getAttribute('onclick') || '').includes('tutorialSubmit') &&
                  (el.getAttribute('onclick') || '').includes(key));
    if (btn) { if (!btn.id) btn.id = 'ainl_submit_' + key; return btn.id; }
  }
  const own = document.getElementById('submit_' + dom.replace(/^question/, ''));
  if (own) return own.id;
  const any = q.querySelector('input[type=submit][name="submit"]');
  if (any) { if (!any.id) any.id = 'ainl_submit_' + dom; return any.id; }
  return null;
}"""

# Answering a tappable part. Click rather than set .checked: MooTools has its
# own handlers on these inputs, and a value set behind their back is a value
# the page does not know about.
_CHOOSE_JS = r"""(picks) => {
  const done = [], missing = [];
  for (const [name, values] of Object.entries(picks)) {
    const want = Array.isArray(values) ? values.map(String) : [String(values)];
    const sel = document.querySelector(`select[name="${name}"], select#${CSS.escape(name)}`);
    if (sel) {
      sel.value = want[0];
      try {
        const ev = document.createEvent('HTMLEvents');   // MooTools owns Event
        ev.initEvent('change', true, false);
        sel.dispatchEvent(ev);
      } catch (e) {}
      done.push(name);
      continue;
    }
    const inputs = [...document.querySelectorAll(`input[name="${name}"]`)];
    if (!inputs.length) { missing.push(name); continue; }
    let hit = false;
    for (const i of inputs) {
      const shouldBeOn = want.includes(String(i.value));
      if (i.type === 'radio') {
        if (shouldBeOn && !i.checked) { i.click(); hit = true; }
        else if (shouldBeOn) hit = true;
      } else if (i.type === 'checkbox') {
        if (i.checked !== shouldBeOn) i.click();
        if (shouldBeOn) hit = true;
      }
    }
    (hit ? done : missing).push(name);
  }
  return {done, missing};
}"""

_CHOICE_READBACK_JS = r"""(names) => Object.fromEntries(names.map(name => {
  const sel = document.querySelector(`select[name="${name}"]`);
  if (sel) return [name, sel.value ? [sel.value] : []];
  return [name, [...document.querySelectorAll(`input[name="${name}"]`)]
                  .filter(i => i.checked).map(i => i.value)];
}))"""

_READBACK_JS = r"""(ids) => Object.fromEntries(ids.map(id => {
  const f = document.getElementById(id) || document.querySelector(`input[name="${id}"]`);
  return [id, f ? f.value : null];
}))"""


# Has every box we wrote finished drawing? renderPreview hands the MathML to
# MathJax, which typesets asynchronously. A fixed 700 ms was not enough on a
# five-box fill (2026-09-21): the student was sent a picture of an EMPTY box
# for an answer that was in fact there, which is worse than no picture.
_RENDERED_JS = r"""(boxes) => boxes.every(id => {
  const el = document.getElementById('editable-math-' + id);
  if (!el) return true;                       // not a maths box: nothing to wait for
  const f = document.getElementById(id) || document.querySelector(`input[name="${id}"]`);
  const mj = el.querySelector('.mtAnswer .MathJax');
  const norm = s => (s || '').replace(/\s+/g, '');
  // Drawn AND drawn from what we just wrote. renderPreview is debounced ~0.3 s,
  // during which the PREVIOUS rendering is still on screen — width alone said
  // "done" about the old answer (measured 2026-09-21).
  return !!mj && mj.offsetWidth > 0 && !!f && norm(mj.getAttribute('data-mathml')) === norm(f.value);
})"""

# WebAssign's own "Save Progress": question.save() posts only the parts the page
# considers changed, to /web/Rest/Response/save. Established live 2026-09-21:
#   - writing every box on the page and saving afterwards loses most of them
#     (one part per question survived);
#   - writing ONE question's parts and saving that question at once persists
#     them all, survives a reload, and spends no submission.
# So saving is done per question, straight after that question is written.
_SAVE_JS = r"""(dom) => { const q = $(dom) && $(dom).retrieve('question');
  if (!q || typeof q.save !== 'function') return false; q.save(); return true; }"""


def _type_into(tab, box: str, value: str) -> None:
    """Replace a text box's content by typing it: keydown/keypress/input/keyup
    for every character, then change and blur — the events WebAssign's own
    scripts listen to."""
    loc = tab.locator(f'[id="{box}"]')
    try:
        loc.click(timeout=3000)
        loc.press("ControlOrMeta+A")
        loc.press("Backspace")
        loc.press_sequentially(value)
        # NOT loc.dispatch_event("change"): Playwright builds it with `new
        # Event(…)`, and MooTools has replaced Event — "Cannot read properties
        # of undefined (reading 'indexOf')", so no change and no blur, and the
        # save left the box out (2026-09-27). createEvent, as _FILL_JS does.
        loc.evaluate("""e => { const ev = document.createEvent('HTMLEvents');
                              ev.initEvent('change', true, false); e.dispatchEvent(ev); e.blur(); }""")
    except Exception as e:                     # the .value written before still stands
        log.warning("typing into %s failed: %s", box, str(e)[:100])


def _question(n: int, raw: dict) -> Question:
    """One Question from one row of _QUESTIONS_JS."""
    return Question(n, raw["dom_id"], raw["fields"],
                    [Part(p["kind"], p["name"],
                          [Option(o["value"], o["label"], o["image"]) for o in p["options"]],
                          p.get("label", ""), bool(p.get("wrong")),
                          bool(p.get("graded")), p.get("text", ""))
                     for p in raw["parts"]],
                    raw.get("stem", ""))


# The step's button is the page's own: tutorialStart(...) fetches that part and
# puts its boxes into the question. Done when the button is gone and a box is
# there; the newly mounted editors then get the usual _wait_ready.
_STEP_OPENED_JS = r"""([id, dom]) => {
  const b = document.getElementById(id), q = document.getElementById(dom);
  const gone = !b || b.offsetParent === null;
  const box = q && q.querySelector('input[name^="RA_"], input[name^="RQ_"], input[name^="RN_"], '
                                   + 'input[type=radio], input[type=checkbox], select, textarea');
  return gone && !!box;
}"""


def _norm(v) -> str:
    """Compare two box values the way the page stores them: whitespace aside."""
    return re.sub(r"\s+", "", v or "")


def _dom_of(box: str) -> Optional[str]:
    m = re.match(r"R[A-Z]_(\d+)_(\d+)_", box)
    return f"question{m.group(1)}_{m.group(2)}" if m else None


def _save_question(tab, dom: str) -> bool:
    """Save one question through the page's own function; True if the server said OK."""
    try:
        with span("save.post", "network", dom=dom):
            with tab.expect_response(lambda r: "/Response/save" in r.url, timeout=5000) as resp:
                if not tab.evaluate(_SAVE_JS, dom):
                    return False
            return resp.value.status == 200
    except Exception as e:
        log.info("save %s: %s", dom, str(e)[:80])
        return False


# Hide the page chrome by setting inline styles on the elements themselves.
# Injecting a stylesheet (add_style_tag, or Playwright's style= argument) makes
# a screenshot wait for fonts to reload. Every change is recorded to be undone.
_HIDE_JS = r"""(selectors) => {
  const touched = [];
  for (const sel of selectors)
    for (const el of document.querySelectorAll(sel)) {
      touched.push([el, el.style.visibility]);
      el.style.visibility = 'hidden';
    }
  window.__ainl_hidden = touched;
  return touched.length;
}"""

_RESTORE_JS = r"""() => {
  for (const [el, prev] of (window.__ainl_hidden || [])) el.style.visibility = prev;
  window.__ainl_hidden = null;
}"""

# Bring one question to the top of the viewport and report where it landed,
# in viewport coordinates — which is the coordinate system of a viewport-sized
# screenshot, so no scroll arithmetic has to survive a round trip.
# The page is usable once its question blocks exist; waiting a fixed six
# seconds spent that long even when it was ready in one.
_READY_JS = r"""() => document.querySelectorAll('[id^="question"]').length > 0
  || /Assignment-Responses/.test(location.href) === false"""

_SCROLL_JS = r"""(id) => {
  const el = document.getElementById(id);
  if (!el) return null;
  el.scrollIntoView({block: 'start', behavior: 'instant'});
  const r = el.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height,
          vw: window.innerWidth, vh: window.innerHeight};
}"""

# Scroll so the question's top sits ``offset`` px above the viewport's top:
# the next slice of a question taller than one screen.
_SCROLL_TO_JS = r"""([id, offset]) => {
  const el = document.getElementById(id);
  if (!el) return null;
  const top = el.getBoundingClientRect().top + window.scrollY;
  window.scrollTo({top: top + offset, behavior: 'instant'});
  const r = el.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height, vw: window.innerWidth, vh: window.innerHeight};
}"""

# Where each PICTURE option's radio/checkbox sits inside its question, in the
# order the bot letters them (DOM order within each group; groups in page
# order, radios before checkboxes — as _QUESTIONS_JS builds the parts). The
# letter goes on the question's own picture instead of one photo per option
# (9.2 Homework, 2026-09-27: twelve option photos for three questions).
_OPTION_MARKS_JS = r"""(id) => {
  const q = document.getElementById(id);
  if (!q) return [];
  const qr = q.getBoundingClientRect();
  const hasPic = i => { const l = i.closest('label') || i.parentElement; return !!(l && l.querySelector('img')); };
  const out = [];
  let g = 0;
  for (const type of ['radio', 'checkbox']) {
    const groups = {};
    q.querySelectorAll(`input[type=${type}]`).forEach(i => { (groups[i.name] = groups[i.name] || []).push(i); });
    for (const ins of Object.values(groups)) {
      if (!ins.some(hasPic)) continue;
      g++;
      ins.forEach((i, k) => {
        const r = i.getBoundingClientRect();
        out.push({group: g, k, x: r.left - qr.left + r.width / 2, y: r.top - qr.top + r.height / 2});
      });
    }
  }
  return out;
}"""

# The answer boxes stay IN the question pictures. They were hidden at first
# (2026-09-19) to leave only the question; but then the student cannot see how
# many blanks there are or where — q5 of 9.3 Homework has two, on two lines,
# and a picture without them read as one question with one answer (2026-09-25).
_HIDE_QUESTION = ["#onetrust-banner-sdk", ".resources", "[class*=\"resource\"]",
                  "input[type=submit]"]
_HIDE_FILLED = ["#onetrust-banner-sdk", ".resources", "[class*=\"resource\"]"]


# -- measurement --------------------------------------------------------------
#
# Our own stopwatch cannot tell "the server was slow" from "the page was slow
# to settle": goto() returns at DOMContentLoaded and both are inside it. The
# browser can — it timestamps every phase of the navigation itself. Read after
# the page is ready, this is what splits a goto span into network and render.
#
# Only host + path of any URL leaves the page: WebAssign carries the session in
# the query string (UserPass=), so the query is dropped, not redacted.
_NAVTIMING_JS = r"""(top) => {
  const n = performance.getEntriesByType('navigation')[0];
  if (!n) return null;
  const r = x => Math.round(x * 10) / 10;
  const strip = u => { try { const p = new URL(u); return p.host + p.pathname; }
                       catch (e) { return String(u).split('?')[0]; } };
  const res = performance.getEntriesByType('resource');
  const now = performance.now();
  // For how long, inside [a, b], was at least one subresource in flight?
  const busy = (a, b) => {
    const iv = res.map(e => [Math.max(a, e.startTime), Math.min(b, e.responseEnd || now)])
                  .filter(p => p[1] > p[0]).sort((p, q) => p[0] - q[0]);
    let total = 0, end = -1;
    for (const [s, t] of iv) {
      if (s > end) { total += t - s; end = t; }
      else if (t > end) { total += t - end; end = t; }
    }
    return r(total);
  };
  const dcl = n.domContentLoadedEventEnd || now;
  return {
    type: n.type, redirects: n.redirectCount, doc: strip(n.name),
    redirect: r(n.redirectEnd - n.redirectStart),
    dns: r(n.domainLookupEnd - n.domainLookupStart),
    connect: r(n.connectEnd - n.connectStart),
    tls: r(n.secureConnectionStart > 0 ? n.connectEnd - n.secureConnectionStart : 0),
    request_start: r(n.requestStart),
    ttfb: r(n.responseStart - n.requestStart),
    download: r(n.responseEnd - n.responseStart),
    response_end: r(n.responseEnd),
    dom_interactive: r(n.domInteractive),
    dcl_end: r(n.domContentLoadedEventEnd),
    load_end: r(n.loadEventEnd),
    read_at: r(now),
    doc_transfer: n.transferSize, doc_decoded: n.decodedBodySize,
    resources: res.length,
    resources_transfer: res.reduce((a, e) => a + (e.transferSize || 0), 0),
    resources_from_cache: res.filter(e => e.transferSize === 0 && e.decodedBodySize > 0).length,
    last_resource_end: r(res.reduce((a, e) => Math.max(a, e.responseEnd || 0), 0)),
    net_busy_before_dcl: busy(n.responseEnd, dcl),
    net_busy_after_dcl: busy(dcl, now),
    slowest: [...res].sort((a, b) => b.duration - a.duration).slice(0, top).map(e => ({
      url: strip(e.name), kind: e.initiatorType, start: r(e.startTime),
      duration: r(e.duration), ttfb: r(e.responseStart > 0 ? e.responseStart - e.requestStart : 0),
      transfer: e.transferSize || 0})),
  };
}"""


def _strip_url(url: str) -> str:
    """host + path, nothing else — the query is where the credentials live."""
    from urllib.parse import urlsplit
    try:
        p = urlsplit(url)
        return f"{p.netloc}{p.path}"
    except ValueError:
        return url.split("?")[0]


def _nav_timing(tab, goto_span, top: int = 6) -> None:
    """Hang the browser's own Navigation Timing on the goto span that caused it.

    network = everything up to the last byte of the document (redirects, DNS,
    connect, TLS, server think time, download); render = from there to
    DOMContentLoaded. What is left of the span is the driver's overhead, ours.
    One extra evaluate, so only when detail is switched on.
    """
    if not timing.detail():
        return
    try:
        with span("navtiming", "ours"):
            nav = tab.evaluate(_NAVTIMING_JS, top)
        if not nav:
            return
        goto_span.set(nav=nav)
        if nav.get("dcl_end", 0) > 0 and nav.get("response_end", 0) > 0:
            goto_span.split(network=nav["response_end"] / 1000.0,
                            render=max(0.0, nav["dcl_end"] - nav["response_end"]) / 1000.0)
    except Exception as e:                       # measuring must never break the op
        log.debug("navtiming failed: %s", str(e)[:80])


class _DocHops:
    """Every main-frame document request during a navigation, with its timing.

    An LTI launch is a chain — Canvas, an auto-submitted form, Cengage, several
    redirects, WebAssign — and Navigation Timing only describes the last hop.
    The handler touches nothing but local properties: a sync call made from
    inside a Playwright event handler can deadlock the dispatcher.
    """

    def __init__(self, tab, label: str):
        self.tab, self.label, self.hops = tab, label, []
        self.on = timing.detail()

    def _finished(self, req) -> None:
        try:
            if not req.is_navigation_request() or req.frame != self.tab.main_frame:
                return
            t = dict(req.timing)
            self.hops.append({"url": _strip_url(req.url), "method": req.method,
                              "epoch_ms": t.get("startTime"),
                              "dns": round(max(0.0, t.get("domainLookupEnd", -1) - t.get("domainLookupStart", -1)), 1),
                              "connect": round(max(0.0, t.get("connectEnd", -1) - t.get("connectStart", -1)), 1),
                              "request_start": round(t.get("requestStart", -1), 1),
                              "ttfb": round(t.get("responseStart", -1) - t.get("requestStart", -1), 1),
                              "response_end": round(t.get("responseEnd", -1), 1)})
        except Exception:
            pass

    def __enter__(self):
        if self.on:
            self.tab.on("requestfinished", self._finished)
        return self

    def __exit__(self, *exc):
        if self.on:
            try:
                self.tab.remove_listener("requestfinished", self._finished)
            except Exception:
                pass
            if self.hops:
                first = min(h["epoch_ms"] or 0 for h in self.hops)
                for h in self.hops:
                    h["at"] = round((h.pop("epoch_ms") or first) - first, 1)
                timing.note(f"doc_hops:{self.label}", self.hops)
        return False


def _restart_browser() -> bool:
    """Relaunch the browser process with a clean renderer.

    NOT free: WebAssign's session cookies are session-only, so every restart
    logs the student out and the next call pays ~10s rebuilding the session
    over LTI (see _webassign_tab). Canvas's login does survive. Still the only
    thing that reliably clears a wedged renderer, so it is a last resort.
    """
    import subprocess
    import sys
    from pathlib import Path

    from ..util import browserthread

    script = Path(__file__).resolve().parents[3] / "scripts" / "browser_start.py"
    if not script.exists():
        return False
    try:
        subprocess.run([sys.executable, str(script), "--restart"],
                       capture_output=True, timeout=60)
    except Exception as e:
        log.warning("browser restart failed: %s", str(e)[:100])
        return False
    browserthread.reset()          # the old driver is pointed at a dead browser
    log.info("browser restarted")
    return True


# Cengage's cookie banner sits over the bottom of the page. It used to be
# hidden only while a picture was taken; the student asked for it to be
# accepted instead (2026-09-25), so it is pressed once and, the profile being
# persistent, stays gone.
_ACCEPT_COOKIES_JS = r"""() => {
  const b = document.getElementById('onetrust-accept-btn-handler');
  if (b && b.offsetParent !== null) { b.click(); return true; }
  return false;
}"""


def _accept_cookies(tab) -> None:
    try:
        if tab.evaluate(_ACCEPT_COOKIES_JS):
            log.info("cookie banner accepted")
    except Exception:
        pass


def _wait_ready(tab, timeout_ms: float = 8000) -> bool:
    """Wait for the page to have questions, not for a guessed number of seconds."""
    try:
        tab.wait_for_function(_READY_JS, timeout=timeout_ms)
        return True
    except Exception:
        return False


# Every maths box has its WIRIS editor mounted and laid out. The editors mount
# after the questions do; a picture taken in between shows no boxes at all.
_BOXES_JS = r"""() => [...document.querySelectorAll('input[name^="R"]')].filter(i =>
    /^R[A-Z]_\d+_\d+_/.test(i.name || '')).every(i => {
  const el = document.getElementById('editable-math-' + i.name);
  return !el || el.offsetWidth > 0;
})"""


def _wait_boxes(tab, timeout_ms: float = 4000) -> bool:
    try:
        tab.wait_for_function(_BOXES_JS, timeout=timeout_ms, polling=150)
        return True
    except Exception:
        log.info("answer boxes not all drawn after %.0f ms — shooting anyway", timeout_ms)
        return False


def _mark_options(img, marks: list[dict], scale: float):
    """A red lettered badge on each picture option's radio: A, B, C… in the
    bot's order (with the group number first when a question has two)."""
    from PIL import ImageDraw, ImageFont
    if not marks:
        return img
    draw = ImageDraw.Draw(img)
    groups = len({m["group"] for m in marks})
    size = max(18, int(20 * scale))          # readable on a phone
    font = None
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/Library/Fonts/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(size=size)
    for m in marks:
        text = (f"{m['group']}" if groups > 1 else "") + chr(65 + m["k"])
        cx, cy = m["x"] * scale, m["y"] * scale
        l, t, r_, b = draw.textbbox((0, 0), text, font=font)
        w, h = r_ - l, b - t
        pad = int(5 * scale)
        box = (cx - w / 2 - pad, cy - h / 2 - pad, cx + w / 2 + pad, cy + h / 2 + pad)
        draw.rounded_rectangle(box, radius=int(6 * scale), fill=(229, 72, 77))
        draw.text((cx - w / 2 - l, cy - h / 2 - t), text, font=font, fill=(255, 255, 255))
    return img


def shoot_all(tab, dom_ids: list[str], hide: list[str], mark_options: bool = False) -> dict[str, bytes]:
    """A picture of each question: scroll it to the top, grab the viewport, crop.

    Every gentler approach failed on this page, and each failure said something:

      - locator.screenshot() waits for the element to be "stable"; MathJax never
        stops re-laying out, so it blocked until timeout and, the bot being
        single threaded, hung the whole bot.
      - page.screenshot() always waits for document.fonts.ready, and on this
        page that promise never settles.
      - Page.captureScreenshot with captureBeyondViewport (the whole page at
        once) closes the connection: the page is ~6,500 px tall and the
        renderer does not survive it.
      - A tab left open for a long time degrades until even a plain viewport
        capture kills the connection; reloading it fixes that, which is why
        _open always reloads.

    So: one viewport-sized CDP capture per question. Small, quick, and it never
    waits on anything the page has to promise. A question taller than the
    viewport is taken in slices, one viewport each, and stitched: 9.2 Homework
    q1 (2026-09-27) lost its last two picture options and part (b) to the crop.

    ``mark_options``: letter the picture options on the picture itself.
    """
    import base64
    from io import BytesIO

    from PIL import Image

    out: dict[str, bytes] = {}
    cdp = None
    try:
        with span("shot.hide_chrome", "ours"):
            tab.evaluate(_HIDE_JS, hide)
        with span("shot.cdp_session", "ours"):
            cdp = tab.context.new_cdp_session(tab)
        for n, dom_id in enumerate(dom_ids, 1):
            try:
                with span("shot.scroll", "ours", q=n):
                    r = tab.evaluate(_SCROLL_JS, dom_id)
                if not r or r["w"] < 1 or r["h"] < 1:
                    continue
                # No sleep here. There used to be a 120 ms "let the scroll
                # settle" — a full second across eight questions, a third of the
                # whole operation (measured: shot.settle 1.04 s of 3.27 s). The
                # scroll is behavior:'instant' and the evaluate() that performs
                # it only returns once layout is done, so there was never
                # anything to wait for.
                marks = tab.evaluate(_OPTION_MARKS_JS, dom_id) if mark_options else []
                slices, offset, scale = [], 0.0, 1.0
                for _ in range(6):                        # at most six screens tall
                    with span("shot.capture", "render", q=n) as cap:
                        data = cdp.send("Page.captureScreenshot", {"format": "png"})
                        cap.set(b64_bytes=len(data["data"]))
                    img = Image.open(BytesIO(base64.b64decode(data["data"])))
                    scale = img.height / max(r["vh"], 1)      # HiDPI
                    # The part of the question not taken yet: from ``offset``
                    # into it. Near the page bottom the scroll falls short, so
                    # this is not always the viewport's top edge.
                    top = max(0.0, r["y"] + offset)
                    bottom = min(r["vh"], r["y"] + r["h"])
                    if bottom - top < 1:
                        break
                    slices.append(img.crop((max(0, int(r["x"] * scale)), int(top * scale),
                                            min(img.width, int((r["x"] + r["w"]) * scale)),
                                            int(bottom * scale))))
                    offset += bottom - top
                    if offset >= r["h"] - 1:
                        break
                    r = tab.evaluate(_SCROLL_TO_JS, [dom_id, offset])
                    if not r:
                        break
                with span("shot.crop", "ours", q=n) as crop:
                    slices = [sl for sl in slices if sl.width >= 2 and sl.height >= 2]
                    if not slices:
                        continue
                    whole = Image.new("RGB", (max(sl.width for sl in slices),
                                              sum(sl.height for sl in slices)), "white")
                    y = 0
                    for sl in slices:
                        whole.paste(sl, (0, y))
                        y += sl.height
                    whole = _mark_options(whole, marks, scale)
                    buf = BytesIO()
                    whole.save(buf, format="PNG")
                    out[dom_id] = buf.getvalue()
                    crop.set(slices=len(slices), crop=[whole.width, whole.height],
                             png_bytes=len(out[dom_id]), marks=len(marks))
            except Exception as e:
                if any(d in str(e).lower() for d in
                       ("connection closed", "target closed", "browser has been closed")):
                    raise                      # let _with_tab reconnect and retry
                log.warning("shot %s failed: %s: %s", dom_id, type(e).__name__, str(e)[:100])
    except Exception as e:
        if any(d in str(e).lower() for d in
               ("connection closed", "target closed", "browser has been closed")):
            raise
        log.warning("screenshots failed: %s: %s", type(e).__name__, str(e)[:120])
    finally:
        with span("shot.restore", "ours"):
            for fn in (lambda: tab.evaluate(_RESTORE_JS), lambda: cdp and cdp.detach()):
                try:
                    fn()
                except Exception:
                    pass
    return out


class WebAssign:
    """One attach per call. Cheap enough (~1 s) and never leaves a stale handle."""

    # Each cached assignment holds one PNG per question — a few megabytes. Keep
    # only a couple, and forget them when the student ends a round.
    CACHE_MAX = 2

    def __init__(self, cdp_url: str = CDP_URL, cache_seconds: float = 900,
                 launch_url=None, restart=None):
        self.cdp_url = cdp_url
        # What the watchdog calls when this browser is wedged. The default
        # restarts THE single-user browser on 9222 — which, for a browser
        # from the per-user pool, would be the wrong one. The pool passes its
        # own "restart this person's" instead.
        self._restart = restart or _restart_browser
        # A callable returning a fresh Canvas LTI launch URL. WebAssign's real
        # session cookies (JSESSIONID, cmp-session-id) are session-only: they
        # die with the browser process, so EVERY restart — including the
        # watchdog's own — logs the student out. With this, the session is
        # rebuilt from Canvas in ~8s and no vendor password is ever needed.
        self._launch_url = launch_url
        # The questions on an assignment do not change; re-reading them costs
        # ~5s of browser work for nothing. Anything that writes to the page
        # drops the entry, so a filled box is never shown as empty.
        self._cache: dict[str, tuple[float, tuple]] = {}
        self._cache_seconds = cache_seconds

    def forget(self, dep: Optional[str] = None) -> int:
        """Drop cached question images — one assignment, or all of them.

        Called when a round is cancelled or restarted: the student has said
        they are done with it, and holding several megabytes of screenshots
        they can no longer reach is just a leak with extra steps.
        """
        n = len(self._cache) if dep is None else (1 if dep in self._cache else 0)
        if dep is None:
            self._cache.clear()
        else:
            self._cache.pop(dep, None)
        return n

    # -- plumbing -------------------------------------------------------------

    # A tab under repeated screenshot load eventually stops answering and takes
    # the driver connection down with it ("Connection closed while reading from
    # the driver"). It is not recoverable in place: the whole connection is
    # gone. So an operation that dies this way is abandoned and run again from
    # a fresh connection, with the page reloaded first — abort and retry rather
    # than wait, which is also why the budgets above are short.
    _DEAD = ("connection closed", "target closed", "browser has been closed")

    def _with_tab(self, fn, attempts: int = 2, budget: float = 25.0, need_tab: bool = True):
        """Run on the browser thread under a deadline; retry once, fresh.

        A wedged page does not raise, it simply never answers, so the deadline
        is enforced from outside (browserthread) rather than hoped for. Between
        attempts the worker and its driver are thrown away, and if the browser
        is wedged rather than the driver, the process is restarted — which
        costs the WebAssign session (rebuilt over LTI, ~10s, no password).
        """
        # Make sure there is a session BEFORE starting the clock on the real
        # work. Rebuilding one takes ~8s; charged to the operation's own budget
        # it blew the deadline, and the deadline handler restarts the browser —
        # which kills the session that had just been rebuilt.
        if need_tab:
            try:
                with step("wa.session", budget=30, cat="session"):
                    browserthread.run(
                        lambda: self._attempt(lambda ctx, tab: None, reload_first=False),
                        budget=30)
            except WebAssignUnavailable:
                raise
            except Exception as e:
                log.warning("session check failed (%s); carrying on", str(e)[:80])

        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return browserthread.run(
                    lambda: self._attempt(fn, reload_first=attempt > 0, need_tab=need_tab),
                    budget=budget)
            except WebAssignUnavailable:
                raise
            except browserthread.Timeout as e:
                last = e
                log.warning("attempt %d/%d hit the %.0fs deadline", attempt + 1, attempts, budget)
                # Evidence first: the restart below destroys it. Opt-in, since
                # the probe delays the recovery by a few seconds.
                if browserthread.wedge_capture_enabled():
                    with span("wedge.capture", "ours"):
                        browserthread.capture_wedge(self.cdp_url, timing.current(),
                                                    op=f"attempt {attempt + 1}/{attempts}")
                with span("browser.restart", "session"):
                    self._restart()
            except Exception as e:
                if not any(d in str(e).lower() for d in self._DEAD):
                    raise
                last = e
                log.warning("browser connection died (attempt %d/%d): %s",
                            attempt + 1, attempts, str(e)[:90])
                browserthread.reset()
                if attempt + 1 < attempts:
                    with span("browser.restart", "session"):
                        self._restart()
        raise WebAssignUnavailable(
            "The browser stopped responding twice, even after a restart. "
            "Check the window on the computer.") from last

    def _attempt(self, fn, reload_first: bool, need_tab: bool = True):
        from ..util import pw

        try:
            cm = pw.connect(self.cdp_url)
            browser = cm.__enter__()
        except Exception as e:
            raise WebAssignUnavailable(
                "The WebAssign browser window isn't running. On the computer, run\n"
                "  python scripts/browser_start.py\n"
                "and sign in to Cengage in the window it opens.") from e
        try:
            ctx = browser.contexts[0]
            # open_launch exists to CREATE the WebAssign session, so demanding
            # a working WebAssign tab first made it fail exactly when needed:
            # after Cengage had logged the student out.
            if need_tab:
                with span("session.find_tab", "session"):
                    tab = self._webassign_tab(ctx)
            else:
                tab = None
            if reload_first and tab is not None:
                try:
                    with span("retry.reload", "network") as g:
                        tab.reload(wait_until="domcontentloaded", timeout=20_000)
                    with span("retry.wait_ready", "render"):
                        _wait_ready(tab)
                    _nav_timing(tab, g)
                except Exception:
                    pass
            return fn(ctx, tab)
        finally:
            cm.__exit__(None, None, None)

    @staticmethod
    def _logged_in(page) -> bool:
        u = page.url.lower()
        return ("webassign.net" in u and "login" not in u and "logout" not in u
                and "forbidden" not in (page.title() or "").lower())

    @staticmethod
    def _close_dead_tabs(pages, keep) -> int:
        """Close tabs left on Cengage's logged-out page (or blank) beside the one in use.

        Two such tabs, left by session rebuilds, had been open for 45 hours on
        2026-09-25, each spinning a full CPU core and holding ~850 MB — nearly
        half of the browser's memory, all of it doing nothing. They hold no
        login (that is what the logout page means), so closing them costs
        nothing. Canvas pages are left alone: the cookie channel's login lives
        there."""
        n = 0
        for pg in pages:
            if pg is keep or pg.is_closed():
                continue
            if "account.cengage.com/logout" in pg.url or pg.url in ("about:blank", ""):
                try:
                    pg.close()
                    n += 1
                except Exception:
                    pass
        if n:
            log.info("closed %d dead tab(s)", n)
        return n

    def _webassign_tab(self, ctx):
        pages = [pg for pg in ctx.pages if not pg.is_closed()]
        tab = next((pg for pg in pages if self._logged_in(pg)), None)
        if tab is not None:
            self._close_dead_tabs(pages, tab)
            return tab

        # No usable WebAssign tab: make one rather than telling the student to.
        # Reuse a dead WebAssign/Cengage tab if there is one, so logged-out
        # pages do not pile up next to the fresh session.
        stale = next((pg for pg in pages if "webassign.net" in pg.url
                      or "account.cengage.com/logout" in pg.url), None)
        fresh = stale or (pages[0] if len(pages) == 1 and pages[0].url in ("about:blank", "")
                          else ctx.new_page())
        try:
            with span("session.goto_list", "session"):
                fresh.goto(WA_LIST, wait_until="domcontentloaded", timeout=25_000)
            with span("session.wait_ready", "session"):
                _wait_ready(fresh)
        except Exception:
            pass
        if self._logged_in(fresh):
            self._close_dead_tabs(pages, fresh)
            return fresh

        # Logged out — the normal state after any browser restart, because the
        # real session cookies (JSESSIONID, cmp-session-id) are session-only.
        # Rebuild it the way Canvas itself does, instead of showing the student
        # a Cengage password form: the account is often LTI-provisioned, so
        # they may not have a password to type there at all.
        if self._launch_url is not None:
            try:
                log.info("WebAssign session gone; re-launching from Canvas")
                timing.note("session_rebuild", "lti")
                with span("session.lti_url", "session"):       # Canvas HTTP
                    launch = self._launch_url()
                try:
                    with span("session.lti_goto", "session"), _DocHops(fresh, "session"):
                        fresh.goto(launch, wait_until="commit", timeout=30_000)
                except Exception as nav:
                    # The launch page auto-submits a form, which interrupts the
                    # navigation we started. That interruption is the launch
                    # working, not failing — keep waiting for where it lands.
                    if "interrupted" not in str(nav).lower():
                        raise
                with span("session.lti_wait", "session"):      # polls once a second
                    for _ in range(20):
                        if "webassign.net" in fresh.url:
                            break
                        fresh.wait_for_timeout(1000)
                if self._logged_in(fresh):
                    log.info("WebAssign session rebuilt from Canvas")
                    return fresh
            except Exception as e:
                log.warning("LTI re-launch failed: %s", str(e)[:100])

        dash = next((pg for pg in pages if _DASH in pg.url), None)
        if dash is None or "auth.cengage.com" in (dash.url if dash else ""):
            # No window is raised here: the browser normally has none, and
            # pulling one over the student's work to show a password form they
            # may have no password for helps nobody. Say what went wrong.
            raise WebAssignUnavailable(
                "I couldn't get into WebAssign: the session is gone and the "
                "Canvas launch that normally rebuilds it didn't work. Try the code "
                "again in a minute; if it keeps failing, Canvas itself may need "
                "a fresh sign-in (/channel).")
        # OPEN WEBASSIGN is a plain <a target=_blank>. Follow it in this tab:
        # a popup would start paused and can hang at about:blank.
        href = dash.evaluate("""() => {
            const a = [...document.querySelectorAll('a[data-testid="home-page-launch-course-link"]')]
              .find(e => /open\\s*webassign/i.test(e.innerText));
            return a ? a.href : null; }""")
        if not href:
            raise WebAssignUnavailable("Couldn't find OPEN WEBASSIGN on the Cengage dashboard.")
        dash.goto(re.sub(r"&_gl=[^&]*", "", href), wait_until="domcontentloaded",
                  timeout=25_000)
        _wait_ready(dash)
        if "webassign.net" not in dash.url:
            raise WebAssignUnavailable("WebAssign didn't open — is the Cengage login still valid?")
        return dash

    @staticmethod
    def _to_list(tab) -> None:
        with span("list.probe", "ours"):
            on_list = tab.evaluate(_LIST_JS)
        if on_list:
            return
        with span("goto_list", "network") as g, _DocHops(tab, "list"):
            tab.goto(WA_LIST, wait_until="domcontentloaded", timeout=20_000)
        with span("wait_ready_list", "render"):
            _wait_ready(tab)
        _nav_timing(tab, g)

    @classmethod
    def _open(cls, tab, dep: str) -> None:
        if re.search(rf"[?&]dep={dep}\b", tab.url):
            timing.note("already_open", dep)
            return
        cls._to_list(tab)
        with span("list.read", "ours"):
            rows = tab.evaluate(_LIST_JS)
        row = next((r for r in rows if r["dep"] == dep), None)
        if row is None:
            raise WebAssignUnavailable(f"Assignment {dep} isn't on My Assignments any more.")
        # No quiz check here on purpose. Whether a quiz may be opened is the
        # caller's decision — /math asks first for the ones whose rules we
        # don't know — and a second refusal down here only contradicted the
        # answer the student had already given.
        with span("goto_assignment", "network") as g, _DocHops(tab, "assignment"):
            tab.goto(row["href"], wait_until="domcontentloaded", timeout=25_000)
        with span("wait_ready", "render"):
            _wait_ready(tab)              # MathJax + WIRIS mount late
        _accept_cookies(tab)
        _nav_timing(tab, g)

    # -- public ---------------------------------------------------------------

    def assignments(self, include_quizzes: bool = True) -> list[Assignment]:
        """Everything on My Assignments. Budget 8s: attach + maybe one navigation."""
        def go(ctx, tab):
            self._to_list(tab)
            seen: dict[str, Assignment] = {}
            with span("list.read", "ours"):
                rows = tab.evaluate(_LIST_JS)
            for r in rows:
                seen.setdefault(r["dep"], Assignment(r["dep"], r["title"], r["due"], r["href"]))
            return [a for a in seen.values() if include_quizzes or not a.is_quiz]
        with step("wa.assignments", budget=8):
            return self._with_tab(go, budget=8)

    def open_launch(self, launch_url: str) -> str:
        """Follow a Canvas LTI launch and report which assignment it landed on.

        This is the authoritative Canvas -> WebAssign mapping: rather than
        guessing from titles, the launch itself resolves it. Measured 5.5s.
        """
        def go(ctx, tab_unused):
            ctx_pages = [pg for pg in ctx.pages if not pg.is_closed()]
            tab = next((pg for pg in ctx_pages if "webassign.net" in pg.url), None)                 or (ctx_pages[0] if ctx_pages else ctx.new_page())
            with _DocHops(tab, "launch"):
                with span("lti.goto", "network") as g:
                    tab.goto(launch_url, wait_until="domcontentloaded", timeout=30_000)
                with span("lti.wait_redirects", "network") as w:   # polls once a second
                    polls = 0
                    for _ in range(14):
                        if "webassign.net" in tab.url:
                            break
                        polls += 1
                        tab.wait_for_timeout(1000)
                    w.set(polls=polls)
                m = re.search(r"[?&]dep=(\d+)", tab.url)
                if not m:
                    raise WebAssignUnavailable(
                        "The Canvas launch didn't reach WebAssign. Open the assignment "
                        "in the browser window once, then try again.")
                with span("lti.wait_ready", "render"):
                    _wait_ready(tab)
            # Describes the LAST document only (the WebAssign page); the hops
            # before it are in the doc_hops note. No split: the goto span
            # covers a different document than the one this timing is for.
            if timing.detail():
                try:
                    with span("navtiming", "ours"):
                        g.set(final_nav=tab.evaluate(_NAVTIMING_JS, 6))
                except Exception:
                    pass
            return m.group(1)

        with step("wa.launch", budget=25):
            return self._with_tab(go, budget=25, need_tab=False)

    def questions(self, dep: str) -> tuple[str, list[Question], list[bytes]]:
        """(assignment title, questions in page order, one PNG per question)."""
        def go(ctx, tab):
            self._open(tab, dep)
            with span("page.title", "ours"):
                title = tab.title().split(" - ")[0]
            with span("extract_questions", "ours") as ex:
                raw = tab.evaluate(_QUESTIONS_JS)
                ex.set(questions=len(raw))
            qs = [_question(n, q) for n, q in enumerate(raw, 1)]
            with span("wait_boxes", "render"):
                _wait_boxes(tab)
            with span("shoot_all", "ours", questions=len(qs)):
                cropped = shoot_all(tab, [q.dom_id for q in qs], _HIDE_QUESTION, mark_options=True)
            return title, qs, [cropped.get(q.dom_id) for q in qs]
        import time as _time
        hit = self._cache.get(dep)
        if hit and _time.monotonic() - hit[0] < self._cache_seconds:
            log.info("questions for dep=%s served from cache", dep)
            return hit[1]
        with step("wa.questions", budget=15):       # measured 5s for 8 questions
            out = self._with_tab(go, budget=15)
        self._cache[dep] = (_time.monotonic(), out)
        while len(self._cache) > self.CACHE_MAX:        # oldest first
            self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]), None)
        return out

    def begin_step(self, dep: str, button_id: str) -> tuple[Question, Optional[bytes]]:
        """Press one "Click here to begin!" and read that question again.

        Nothing is answered or submitted: the part's boxes merely appear, and
        the round goes on as for any other question. Returns the question as
        it now is (same number, new parts) and a fresh picture of it.
        """
        def go(ctx, tab):
            self._open(tab, dep)
            dom = tab.evaluate("(id) => { const b = document.getElementById(id);"
                               " const q = b && b.closest('[id^=\"question\"]');"
                               " return q ? q.id : null; }", button_id)
            if not dom:
                raise WebAssignUnavailable(
                    "That part is already open, or the page has changed — "
                    "open the assignment again from /todo.")
            with span("step.click", "ours", dom=dom):
                tab.click(f"#{button_id}", timeout=5000)
            with span("step.wait_opened", "render"):
                tab.wait_for_function(_STEP_OPENED_JS, arg=[button_id, dom],
                                      timeout=10_000, polling=200)
            _wait_ready(tab)                      # its editors mount late, as on load
            raw = tab.evaluate(_QUESTIONS_JS)
            n = next((i for i, q in enumerate(raw, 1) if q["dom_id"] == dom), None)
            if n is None:
                raise WebAssignUnavailable("Lost the question after opening its part.")
            q = _question(n, raw[n - 1])
            _wait_boxes(tab)
            with span("shoot_all", "ours", questions=1):
                cropped = shoot_all(tab, [dom], _HIDE_QUESTION)
            return q, cropped.get(dom)
        self._cache.pop(dep, None)              # the page has a part it did not have
        with step("wa.begin_step", budget=25):
            return self._with_tab(go, budget=25)

    def step_shot(self, dep: str, box: str) -> Optional[bytes]:
        """A picture of just the step that holds ``box`` — the student should
        see the one thing being asked, not the whole question above it."""
        def go(ctx, tab):
            self._open(tab, dep)
            handle = tab.evaluate_handle(
                "(name) => { const f = document.getElementById('editable-math-' + name)"
                " || document.getElementById(name) || document.querySelector(`[name=\"${name}\"]`);"
                " return f ? (f.closest('.section') || f.closest('[id^=\"question\"]')) : null; }", box)
            el = handle.as_element()
            if el is None:
                return None
            el.scroll_into_view_if_needed(timeout=3000)
            return el.screenshot(type="png")
        with step("wa.step_shot", budget=12):
            return self._with_tab(go, budget=12)

    def fill(self, dep: str, answers: dict[str, str]) -> tuple[dict, list[bytes]]:
        """Write MathML into RA_ fields; nothing is saved or submitted.

        ``answers`` maps RA_ field name -> MathML. Returns (read-back of every
        field we wrote, a screenshot of each question that was filled).
        """
        def go(ctx, tab):
            self._open(tab, dep)
            # One question at a time: write its boxes, then save that question.
            by_dom: dict[str, dict[str, str]] = {}
            for box, mathml in answers.items():
                by_dom.setdefault(_dom_of(box) or "", {})[box] = mathml
            result = {"done": [], "missing": []}
            saved: list[str] = []
            for dom, boxes in by_dom.items():
                with span("fill.write", "ours", boxes=len(boxes)):
                    r = tab.evaluate(_FILL_JS, boxes)
                    # A plain box is TYPED into, key by key, as a student would.
                    for box in tab.evaluate(_PLAIN_JS, list(boxes)):
                        _type_into(tab, box, boxes[box])
                result["done"] += r["done"]
                result["missing"] += r["missing"]
                if dom and r["done"] and _save_question(tab, dom):
                    saved.append(dom)
            with span("fill.wait_rendered", "render"):      # a signal, not a guess
                # polling= an interval, not the default requestAnimationFrame: a
                # headless page that nobody is looking at is not obliged to
                # produce frames, and then the check never runs at all.
                # On a freshly loaded page the FIRST renderPreview is simply
                # dropped (measured: nothing after 5 s, drawn 0.5 s after a
                # second call), so ask again quickly rather than wait long.
                undrawn: list[str] = []
                for attempt in (1, 2, 3, 4):
                    try:
                        tab.wait_for_function(_RENDERED_JS, arg=result["done"],
                                              timeout=1500, polling=150)
                        undrawn = []
                        break
                    except Exception:
                        undrawn = [b for b in result["done"]
                                   if not tab.evaluate(_RENDERED_JS, [b])]
                        log.info("filled boxes not drawn after 1.5s (try %d): %s — asking again",
                                 attempt, undrawn)
                        tab.evaluate("(ids) => ids.forEach(i => { try { mathTypeOverlay.renderPreview(i); }"
                                     " catch (e) {} })", result["done"])
            with span("fill.readback", "ours"):
                readback = tab.evaluate(_READBACK_JS, list(answers))
            doms = []
            for box in result["done"]:
                qid = re.match(r"R[A-Z]_(\d+)_(\d+)_", box)
                if qid and f"question{qid.group(1)}_{qid.group(2)}" not in doms:
                    doms.append(f"question{qid.group(1)}_{qid.group(2)}")
            with span("shoot_all", "ours", questions=len(doms)):
                cropped = shoot_all(tab, doms, _HIDE_FILLED)
            shots = [cropped[d] for d in doms if d in cropped]
            # Which question each picture is, so the chat can caption it.
            return {"readback": readback, "missing": result["missing"], "saved": saved,
                    "undrawn": undrawn, "shot_doms": [d for d in doms if d in cropped]}, shots
        self._cache.pop(dep, None)              # the page now shows answers
        with step("wa.fill", budget=25):        # writes + one save per question + drawing
            return self._with_tab(go, budget=25)

    def choose(self, dep: str, picks: dict, skip_shots: tuple = ()) -> dict:
        """Tick the tapped answers. Nothing is submitted.

        ``picks`` maps an input name (RC_… radio, RS_… checkbox, or a select)
        to the value or values chosen. Clicking is deliberate: these inputs
        carry MooTools handlers, and a value set behind their back is one the
        page does not know it has.
        """
        self._cache.pop(dep, None)              # the page now shows answers

        def go(ctx, tab):
            self._open(tab, dep)
            by_dom: dict[str, dict] = {}
            for name, values in picks.items():
                by_dom.setdefault(_dom_of(name) or "", {})[name] = values
            out = {"done": [], "missing": [], "saved": []}
            for dom, group in by_dom.items():           # click, then save, per question
                with span("choose.click", "ours", picks=len(group)):
                    r = tab.evaluate(_CHOOSE_JS, group)
                out["done"] += r["done"]
                out["missing"] += r["missing"]
                if dom and r["done"] and _save_question(tab, dom):
                    out["saved"].append(dom)
            with span("choose.readback", "ours"):
                out["readback"] = tab.evaluate(_CHOICE_READBACK_JS, list(picks))
            # A picture of each question answered ONLY by tapping. Questions
            # that also get a written answer are photographed by fill(), after
            # both parts are in — so they are skipped here, not shown twice.
            doms = [d for d in by_dom if d and d not in skip_shots]
            with span("shoot_all", "ours", questions=len(doms)):
                cropped = shoot_all(tab, doms, _HIDE_FILLED) if doms else {}
            out["shots"] = [cropped[d] for d in doms if d in cropped]
            out["shot_doms"] = [d for d in doms if d in cropped]
            return out

        with step("wa.choose", budget=20):
            return self._with_tab(go, budget=20)

    def submit(self, dep: str, boxes: list[str],
               expect: Optional[dict] = None) -> tuple[dict, list[bytes]]:
        """Press SUBMIT ANSWER for the questions owning ``boxes``.

        Only ever called after the student has seen the filled answers rendered
        and tapped confirm. It clicks the page's own button rather than calling
        question.submit() directly: the button's onclick IS that call, so a
        click is the same code path the student's own click takes, and it can't
        drift from whatever WebAssign changes around it.

        One question at a time, each waited out, because every submit is an
        AJAX round trip that re-renders that question with its score.
        """
        doms: list[str] = []
        for box in boxes:      # R?_<qid>_<qIdx>_… -> question<qid>_<qIdx>
            m = re.match(r"R[A-Z]_(\d+)_(\d+)_", box)
            if m and f"question{m.group(1)}_{m.group(2)}" not in doms:
                doms.append(f"question{m.group(1)}_{m.group(2)}")

        pressed: list[str] = []                 # noted BEFORE each click: a click
                                                # that times out may still have landed
        def go(ctx, tab):
            self._open(tab, dep)
            done, failed = [], {}
            differs: list[str] = []
            for dom in doms:
                mine = [b for b in boxes if _dom_of(b) == dom]
                # The page must hold what was filled and shown. 2026-09-25, 9.1
                # Homework q1: the picture after Fill showed the new answers, yet
                # twice WebAssign graded the OLDER saved ones. So look again right
                # before pressing, and write back anything that is not there.
                want = {b: expect[b] for b in mine if expect and b in expect}
                if want:
                    now = tab.evaluate(_READBACK_JS, list(want))
                    stale = {b: v for b, v in want.items() if _norm(now.get(b)) != _norm(v)}
                    if stale:
                        log.info("submit: %s held other values for %s — writing them again",
                                 dom, list(stale))
                        tab.evaluate(_FILL_JS, stale)
                        # a plain box again TYPED: a written .value alone went
                        # in empty (9.2 Homework q4, 2026-09-27)
                        for box in tab.evaluate(_PLAIN_JS, list(stale)):
                            _type_into(tab, box, stale[box])
                        _save_question(tab, dom)
                btn_id = tab.evaluate(_SUBMIT_BUTTON_JS, [dom, mine])
                if not btn_id:
                    failed[dom] = "no submit button found"
                    # Where would Jev have clicked? Recorded for the owner, never done.
                    from .. import jev
                    num = (tab.evaluate("(d) => { const e = document.getElementById(d);"
                                        " const m = e && e.innerText.match(/^\\s*(\\d+)\\./);"
                                        " return m ? m[1] : ''; }", dom) or "")
                    jev.shadow(tab, f"Submit the answer to question {num or dom} only "
                                    "(its own Submit button) — not the whole assignment.",
                               "webassign.submit: no submit button")
                    continue
                btn = tab.locator(f"#{btn_id}")
                pressed.append(dom)
                try:
                    with span("submit.click", "ours"):
                        btn.click(timeout=8_000)
                    with span("submit.grade_sleep", "network"):   # fixed 2.5 s, not a signal
                        tab.wait_for_timeout(2500)      # the answer is graded in place
                    done.append(dom)
                except Exception as e:
                    failed[dom] = f"{type(e).__name__}: {str(e)[:80]}"
            with span("submit.final_sleep", "ours"):        # a fixed sleep we chose
                tab.wait_for_timeout(2000)
            with span("submit.outcome", "ours"):
                outcome = tab.evaluate(_OUTCOME_JS, doms)
            if expect:                                  # what was graded, box by box
                graded = tab.evaluate(_READBACK_JS, [b for b in boxes if b in expect])
                differs = [b for b, v in graded.items()
                           if v is not None and _norm(v) != _norm(expect[b])]
                if differs:
                    log.warning("submit: graded values differ from the filled ones: %s", differs)
            with span("shoot_all", "ours", questions=len(doms)):
                cropped = shoot_all(tab, doms, _HIDE_FILLED)
            shots = [cropped[d] for d in doms if d in cropped]
            return {"submitted": done, "failed": failed, "outcome": outcome,
                    "differs": differs, "shot_doms": [d for d in doms if d in cropped]}, shots
        def look(ctx, tab):                     # read-only: opens and reads, never clicks
            self._open(tab, dep)
            return tab.evaluate(_OUTCOME_JS, doms)

        self._cache.pop(dep, None)              # the page now shows scores — and a
                                                # step may have opened; re-read it
        # one click plus ~2.5s of grading per question, so this one scales
        with step("wa.submit", budget=15 + 4 * len(doms)):
            try:
                # ONE attempt. A retry runs go again, which presses SUBMIT ANSWER
                # again — and every press spends one of the student's submissions.
                return self._with_tab(go, attempts=1, budget=15 + 4 * len(doms))
            except Exception as e:
                if not pressed:                 # failed before any click: nothing sent
                    raise
                try:
                    seen = self._with_tab(look, attempts=1, budget=30)
                except Exception:
                    seen = {}
                points = [o["points"] for o in (seen or {}).values() if o and o.get("points")]
                shows = (" The page now shows: " + "; ".join(points) + "." if points
                         else " I couldn't read the page afterwards either.")
                raise WebAssignUnavailable(
                    "The browser stopped answering while submitting. I don't know whether "
                    "every answer went up." + shows + " Check the page before "
                    "submitting again — each press uses an attempt.") from e


# --------------------------------------------------------------------------- #
# pure helpers (no browser) — unit-tested
# --------------------------------------------------------------------------- #

def math_targets(questions: list[Question]) -> dict[int, list[str]]:
    """Question number -> the boxes that need WRITING, in page order.

    Maths and typed parts both: the student writes each on the sheet; only the
    way the value goes into the page differs (typed_boxes says which). A
    question whose every part is a tap has nothing for the student to write,
    and asking them to put it on the answer sheet would be asking for work
    the buttons already did.

    Several boxes are fine: the student writes that question's answers on one
    line separated by commas, in the same order, so answer k goes to box k.
    DOM order is reading order, which is what makes the two line up.
    """
    out = {}
    for q in questions:
        names = [p.name for p in q.parts if p.written]
        if names:
            out[q.n] = names
    return out


answer_targets = math_targets          # old name, kept for callers not yet moved


def typed_boxes(questions: list[Question]) -> list[str]:
    """The written boxes that take the answer as bare text, not MathML."""
    return [p.name for q in questions for p in q.parts if p.is_typed]


def steps(questions: list[Question]) -> list[dict]:
    """The "begin" buttons on offer: one per question, the first unopened part."""
    return [{"n": q.n, "id": p.name, "label": p.label}
            for q in questions for p in q.parts if p.kind == "step"]


def multipart(questions: list[Question]) -> list[int]:
    return [q.n for q in questions if len(q.fields) > 1]


def unanswerable(questions: list[Question]) -> list[int]:
    return [q.n for q in questions if not q.fields]
