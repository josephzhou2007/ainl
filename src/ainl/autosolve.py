"""Answer-line format: parsing and sanity checks.

Every route that fills a WebAssign sheet (a photographed handwritten sheet, a
typed answer) ends in the same "<question>.<box> <LaTeX>" / "<question>.c<k>
<letter>" lines; this module parses them and checks a box value before it is
typed in.

Public build: the private repo also had a developer-only switch here (/auto)
that let a language model produce the answers so the fill/submit path could
be tested end to end. That solver is not included. from_env() always returns
None, so /auto reports that it is unavailable, and the product never
produces answers: the student's own work is what gets transcribed and handed in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


def from_env():
    """No solver ships in the public build."""
    return None

@dataclass
class Item:
    """One question as the model is shown it."""
    n: int
    stem: str = ""
    image: Optional[bytes] = None
    boxes: list[str] = field(default_factory=list)          # "maths" | "number", in reading order
    choices: list[list[str]] = field(default_factory=list)  # per choice part: option labels, page order
    # per choice part, per option: that option's own picture (a graph, a formula)
    # — the whole-question screenshot shrinks four graphs to ~330 px each.
    option_images: list[list[Optional[bytes]]] = field(default_factory=list)
    # per choice part: "select all that apply" (checkboxes) — several letters
    multi: list[bool] = field(default_factory=list)


@dataclass
class Answers:
    """Parsed reply: question -> {box index (1-based): LaTeX}, {choice index: letter}."""
    boxes: dict[int, dict[int, str]] = field(default_factory=dict)
    choices: dict[int, dict[int, str]] = field(default_factory=dict)
    # /sweep only: what the model SAW on each picture — (answer boxes, choice parts)
    seen: dict[int, tuple[int, int]] = field(default_factory=dict)

    def empty(self) -> bool:
        return not self.boxes and not self.choices


def _describe(it: Item) -> str:
    parts = []
    if it.boxes:
        parts.append(f"{len(it.boxes)} answer box{'es' if len(it.boxes) != 1 else ''} ("
                     + ", ".join(f"{k}: {b}" for k, b in enumerate(it.boxes, 1)) + ")")
    for c, opts in enumerate(it.choices, 1):
        letters = ", ".join(f"{chr(64 + i)}) {o or '[picture]'}" for i, o in enumerate(opts, 1))
        multi = c <= len(it.multi) and it.multi[c - 1]
        parts.append(f"choice c{c}" + (" (SELECT ALL that apply: every correct letter)" if multi else "")
                     + f": {letters}")
    # The page text only when there is no picture: flattened it is cut at 700
    # characters, full of button labels, and it carries the boxes' CURRENT
    # contents and score — old answers the model should not be steered by.
    return f"Question {it.n}: " + "; ".join(parts) + (
        f"\nText: {it.stem}" if it.stem and not it.image else "")



_SEEN_LINE = re.compile(r"^\s*(\d{1,2})\s*[.:]\s*seen\s+(\d{1,2})\s+(\d{1,2})\b", re.I)
_BOX_LINE = re.compile(r"^\s*(\d{1,2})\s*[.:]\s*(\d{1,2})\s*[.):]?\s+(.+?)\s*$")
_CHOICE_LINE = re.compile(r"^\s*(\d{1,2})\s*[.:]\s*c\s*(\d{1,2})\s*[.):]?\s*(.+)$", re.I)
# One option letter: "B" or "(B)", standing alone (not the y of "(y = 0.5)").
_LETTER = re.compile(r"\s*(?:\(([A-Za-z])\)|([A-Za-z]))(?=\s|,|;|&|\.|$)")
_JOIN = re.compile(r"\s*(?:,|;|&|\band\b)?\s*", re.I)


def _letters(rest: str) -> str:
    """The option letters at the start of a choice line: "B, D" -> "BD"."""
    out, i = "", 0
    while True:
        m = _LETTER.match(rest, i)
        if not m:
            break
        ch = (m.group(1) or m.group(2)).upper()
        if ch not in out:
            out += ch
        i = _JOIN.match(rest, m.end()).end()
    return out


def parse_answers(text: str) -> Answers:
    """Read "<q>.<box> <answer>" and "<q>.c<k> <letter>" lines; ignore the rest."""
    out = Answers()
    for line in (text or "").splitlines():
        line = line.strip().strip("`").strip()
        m = _SEEN_LINE.match(line)
        if m:
            out.seen[int(m.group(1))] = (int(m.group(2)), int(m.group(3)))
            continue
        m = _CHOICE_LINE.match(line)
        if m and _letters(m.group(3)):
            # several letters for a "select all" part: "1.c2 B, D" -> "BD"
            out.choices.setdefault(int(m.group(1)), {})[int(m.group(2))] = _letters(m.group(3))
            continue
        m = _BOX_LINE.match(line)
        if m:
            val = m.group(3).strip().strip("$").strip()
            out.boxes.setdefault(int(m.group(1)), {})[int(m.group(2))] = val
    return out


# ---- what WebAssign will take -------------------------------------------------
#
# Checked BEFORE anything is filled, so a malformed answer costs one short
# repair request instead of a submission. The converter to MathML accepts
# almost anything (a sentence becomes a row of variables), so the rules are
# ours: a maths box holds an expression — function names and WebAssign's
# keywords are fine, prose is not; a number box holds a number.

_KEYWORDS = {"DIVERGES", "DIVERGE", "DNE", "NONE", "INFINITY", "TRUE", "FALSE"}
_COMMAND = re.compile(r"\\[A-Za-z]+")
_WORD = re.compile(r"[A-Za-z]{4,}")
_NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def box_problem(value: str, kind: str) -> str:
    """Why WebAssign would not take ``value`` in a box of ``kind`` ("maths" or
    "number"), or "" when it is fine. ``value`` is what goes in as written —
    the bot's own clean-up (units, simple fractions) has already been applied."""
    v = (value or "").strip()
    if not v:
        return "empty"
    if kind == "number":
        return "" if _NUMBER.match(v) else f"not a plain number: {v!r}"
    from .mathinput.latex_to_mathml import latex_to_webassign_mathml
    try:
        latex_to_webassign_mathml(v)
    except Exception as e:                       # noqa: BLE001
        return f"not valid LaTeX ({type(e).__name__})"
    bare = _COMMAND.sub(" ", v.replace("\\text{", " ").replace("\\mathrm{", " "))
    prose = [w for w in _WORD.findall(bare) if w.upper() not in _KEYWORDS]
    if prose:
        return f"contains words ({', '.join(prose[:3])}); a box takes only the expression"
    if "=" in v.split(",")[0] and not v.upper().startswith(("DIVERGE", "DNE")):
        return "contains '=': give only what goes in the box"
    return ""


def has_pictures(items: list["Item"]) -> bool:
    """Does any question offer pictures as its choices (graphs, fields)?"""
    return any(p for it in items for group in it.option_images for p in group)

