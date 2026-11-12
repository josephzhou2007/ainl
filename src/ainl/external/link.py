"""Which Canvas item is which WebAssign assignment.

Canvas shows MATH222 homework as an external_tool item — a link that launches
WebAssign. Tapping it in chat used to hand back a URL, which is the fragmented
experience the whole project exists to remove. To go straight from "C2" in
/todo into the /math flow, the two sides have to be tied together.

There are two ways to know which is which, and the order matters.

The authoritative one is Canvas's own LTI launch (``dep_for``). Asking Canvas
for a sessionless launch and following it lands on the vendor's copy of that
exact assignment — measured 5.9s, and Canvas resolves the mapping, so nothing
is guessed. It also needs no vendor password: Canvas hands the student over
with a signed identity, which is what makes this work for a student who has
never opened Cengage. The result is cached by id, because ids do not change.

The fallback is the title (``find``). Measured 2026-09-19, the titles are
identical on both sides — "7.4 Homework-Partial Fractions" in Canvas, the same
string in WebAssign — with the section number ("7.4") below that. If neither
can say, we say we don't know rather than guess: sending the student into the
wrong assignment wastes a submission attempt.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional

log = logging.getLogger("ainl.link")

_PUNCT = re.compile(r"[^a-z0-9.]+")
_SECTION = re.compile(r"\b(\d{1,2}\.\d{1,2})\b")


def normalize(title: str) -> str:
    """Lowercase, punctuation to single spaces — "7.4 Homework-Partial Fractions"
    and "7.4 homework - partial fractions" have to land on the same string."""
    return _PUNCT.sub(" ", (title or "").lower()).strip()


def section(title: str) -> Optional[str]:
    """The textbook section, "7.4", if the title carries one."""
    m = _SECTION.search(title or "")
    return m.group(1) if m else None


def find(canvas_title: str, assignments: Iterable) -> Optional[object]:
    """The WebAssign assignment this Canvas item is, or None.

    ``assignments`` are webassign.Assignment objects (anything with .title).
    Exact normalised title first; then the section number, but only when it
    identifies exactly one assignment — an ambiguous match is no match.
    """
    items = list(assignments)
    want = normalize(canvas_title)
    if not want:
        return None
    for a in items:
        if normalize(a.title) == want:
            return a

    sec = section(canvas_title)
    if sec:
        hits = [a for a in items if section(a.title) == sec]
        if len(hits) == 1:
            return hits[0]
    return None


# --------------------------------------------------------------------------- #
# the authoritative route: let Canvas do the mapping
# --------------------------------------------------------------------------- #
_MAP_FILE = "lti-map.json"


def _load_map() -> dict:
    from ..config import home
    try:
        return json.loads((home() / _MAP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_map(m: dict) -> None:
    from ..config import home
    try:
        (home() / _MAP_FILE).write_text(json.dumps(m), encoding="utf-8")
    except OSError:
        pass


def dep_for(canvas, wa, course_id: str, assignment_id: str, title: str = "") -> Optional[str]:
    """Which WebAssign assignment a Canvas item is, resolved properly.

    Order matters:

    1. A mapping we already resolved — the ids never change, so this is free.
    2. The LTI launch. Canvas hands the student to the vendor exactly as it
       does in the browser: no vendor password, and it works for any student
       with a Canvas session, not just one with a Cengage tab already open.
    3. Title matching, as a fallback for when the launch is unavailable.

    Returns None when none of the three can say, rather than guessing: opening
    the wrong assignment can cost a submission attempt.
    """
    key = f"{course_id}:{assignment_id}"
    cached = _load_map()
    if key in cached:
        return cached[key]

    try:
        dep = wa.open_launch(canvas.sessionless_launch(course_id, assignment_id))
    except Exception as e:                       # tool disabled, launch refused…
        log.info("LTI launch didn't resolve %s (%s); trying the title",
                 key, str(e)[:80])
        match = find(title, wa.assignments()) if title else None
        return match.dep if match else None

    cached[key] = dep
    _save_map(cached)
    return dep
