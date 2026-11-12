"""Two cheap readings in parallel -> agree? accept : escalate -> still no match? human.

Implements sec. 6 "Recommended tiered chain" and sec. 7 "Confidence-check
trick" of docs/research/handwriting-ocr-options-2026-09.md.  Two independent
model families (Gemini + Qwen) are used so correlated over-corrections are less
likely; the verbatim prompt in ``prompts.py`` is the other half of that mitigation.
"""

from __future__ import annotations

import os.path
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .latex_to_mathml import latex_to_webassign_mathml
from .providers import registry
from .providers.base import ProviderNotConfigured, VisionProvider

# Measured 2026-09-19 on 7 pages of this student's handwriting
# (docs/research/gemini-vs-pix2text-2026-09-19.md): Gemini Flash-Lite ~95% vs
# Pix2Text ~45-75%, with none of Pix2Text's systematic errors. Where the two
# disagreed, Pix2Text was essentially always the wrong one — so a second cheap
# vote carries no information. The real second gate is the student confirming
# the rendered result, which has to happen anyway (it is their answer being
# submitted). Escalation now triggers on self-reported doubt instead: the
# reading contains \? , fails to parse, or the student rejects it.
DEFAULT_CHEAP: tuple[str, ...] = ("gemini-flash-lite",)
DEFAULT_ESCALATE = "claude-sonnet-5"

# A page of homework is several problems; WebAssign wants one answer. Split on a
# leading "1." / "(1)" / "1)" so the student can pick which one to submit.
# A number is a question label when it starts a chunk and is followed by "." or
# ")". No space is required after it — handwriting often transcribes as
# "4.\frac{..}" — but a digit must not follow, or "3.5" would start question 3.
_PROBLEM_SPLIT = re.compile(r"(?=(?:^|\s)\(?\d{1,2}[.)](?!\d))")


def split_problems(latex: str) -> list[str]:
    """Break a whole-page transcription into per-problem chunks.

    Returns [whole] when no numbering is found, so callers always get >= 1 item.
    """
    parts = [p.strip() for p in _PROBLEM_SPLIT.split(latex or "") if p.strip()]
    return parts or ([latex.strip()] if latex.strip() else [])


_LEADING_NUMBER = re.compile(r"^\s*\(?(\d{1,2})[.)]\s*")


def number_problems(latex: str) -> dict[int, list[str]]:
    """One photo of a numbered answer sheet -> {question number: [answer per blank]}.

    The student writes every answer on one page with its number, so a single
    reading covers the whole assignment. Chunks without a leading number are
    dropped rather than guessed at: a mis-attributed answer would be filled
    into the wrong question.
    """
    out: dict[int, list[str]] = {}
    for chunk in split_problems(latex):
        m = _LEADING_NUMBER.match(chunk)
        if not m:
            continue
        body = chunk[m.end():].strip()
        answers = [final_answer(p) for p in split_parts(body)]
        answers = [a for a in answers if a]
        if answers:
            out[int(m.group(1))] = answers
    return out


_SUBLABEL = re.compile(r"(?:^|(?<=[\s,]))\(?([a-h])\)\s*")


def split_parts(latex: str) -> list[str]:
    """One question's answer line -> one answer per blank.

    The student's convention: a question with several blanks gets its answers on
    one line, separated by commas, in the order the blanks appear. Commas inside
    brackets are part of an answer — ``(1,2)`` is an interval, not two answers —
    so only commas at bracket depth zero split.

    A question with lettered parts is written the way the page shows it,
    "(a) 25e^{-t/100} (b) 22.6": the labels split it too, in order, and are
    dropped — the answer is what follows the label, never the label.
    """
    labels = list(_SUBLABEL.finditer(latex))
    if labels and labels[0].group(1) == "a" and _in_order(labels):
        out: list[str] = []
        for i, m in enumerate(labels):
            end = labels[i + 1].start() if i + 1 < len(labels) else len(latex)
            seg = latex[m.end():end].strip().rstrip(",").strip()
            out.extend(split_parts(seg))     # "(a) 1, 2 (b) 3": (a) may have blanks of its own
        return out
    parts, depth, buf = [], 0, []
    for ch in latex:
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) == 1:
        return _split_equations(parts[0])
    return parts


# "P(t)=M-Ce^{-kt} \lim_{t\to\infty}P(t)=M": two answers on two lines of the
# sheet, no comma, no label — the reader joins lines with a space. A new
# equation starts where, after whitespace, a left-hand side and "=" follow.
_EQ_START = re.compile(r"\s+(?=(?:\\lim|\\int|\\sum|[A-Za-z](?:_\{?\w+\}?)?(?:\([^()]*\))?)\s*(?:_\{[^{}]*\})?[^=\s]*\s*=)")


def _split_equations(line: str) -> list[str]:
    """One line holding several whole equations -> one per equation.

    Only when every piece has its own "=": "y = 2x = 4" (one chained answer)
    is left alone, as is anything without whitespace before a new left side.
    """
    pieces = [p.strip() for p in _EQ_START.split(line) if p.strip()]
    if len(pieces) > 1 and all("=" in p for p in pieces):
        return pieces
    return [line]


def _in_order(labels) -> bool:
    """(a) (b) (c)…: consecutive letters, so a stray "(b)" inside an answer
    does not get taken for a part label."""
    return all(ord(m.group(1)) == ord("a") + i for i, m in enumerate(labels))


_WORD = re.compile(r"[A-Za-z]{4,}")


def written_choice(chunk: str, labels: list[str]) -> tuple[int, str] | None:
    """A choice the student wrote out in words -> (index into labels, the word).

    Students write "3. Convergent  13/2" because that is how the question
    reads; making them also tap a button for a word already on the page is
    work the reading can do for free. A word counts when it opens like
    exactly one label for at least four letters ("Conver.", "converges" and
    "convergent" all mean the same button) — ambiguous or absent means None,
    never a guess. LaTeX commands (\frac, \text) are not words.
    """
    text = re.sub(r"\\[A-Za-z]+", " ", chunk or "")
    for m in _WORD.finditer(text):
        w = m.group(0).lower()
        # shared opening letters: "diverges" and "divergent" agree for 7
        share = [len(os.path.commonprefix([w, lab.lower()])) for lab in labels]
        best = max(share, default=0)
        if best >= 4 and share.count(best) == 1:
            return share.index(best), m.group(0)
    return None


def without_word(answer: str, word: str) -> str:
    """The answer with the written-out choice removed ("divergent -1/9" -> "-1/9")."""
    return re.sub(rf"(?:\\text\{{\s*)?{re.escape(word)}\.?(?:\s*\}})?", "", answer, count=1,
                  flags=re.I).strip(" ,;:")


_TRAILING_TEXT = re.compile(r"(?:\\text\{[^{}]*\}|\\quad|\\,|\s)+$")


def final_answer(chunk: str) -> str:
    """The part of a worked chunk that is the answer.

    If the student showed working, the answer is what follows the last ``=``;
    a bare expression is itself the answer. Line breaks are treated as the end
    of the working, so only the last line is considered.
    """
    last_line = [ln for ln in re.split(r"\\\\|\n", chunk) if ln.strip()]
    s = (last_line[-1] if last_line else chunk).strip()
    if "=" in s:
        s = s.rsplit("=", 1)[1]
    return _TRAILING_TEXT.sub("", s).strip()


@dataclass
class Result:
    latex: str = ""  # empty when needs_human
    mathml: str = ""
    agreed: bool = False  # the two cheap readings agreed (no escalation needed)
    candidates: dict[str, str] = field(default_factory=dict)  # provider name -> raw LaTeX
    cost_usd: float = 0.0
    needs_human: bool = False
    escalated: bool = False
    errors: dict[str, str] = field(default_factory=dict)  # provider name -> error text


# --------------------------------------------------------------------------- #
# normalisation (report sec. 7, first bullet)
# --------------------------------------------------------------------------- #
_SIZE_CMDS = re.compile(r"\\(?:left|right|[bB]ig{1,2}[lrm]?|middle)(?![a-zA-Z])")
_SPACING_CMDS = re.compile(r"\\(?:[,;:!>]|quad|qquad|displaystyle|textstyle|\s)")
_SINGLE_BRACE = re.compile(r"\{([A-Za-z0-9])\}")
_MATHRM = re.compile(r"\\(?:mathrm|text|operatorname)\{([^{}]*)\}")
_REPLACEMENTS = (
    (r"\dfrac", r"\frac"),
    (r"\tfrac", r"\frac"),
    (r"\lbrack", "["),
    (r"\rbrack", "]"),
    (r"\lbrace", r"\{"),
    (r"\rbrace", r"\}"),
    (r"\lvert", "|"),
    (r"\rvert", "|"),
    (r"\vert", "|"),
    (r"\cdot", "*"),
    (r"\times", "*"),
)


def normalize_latex(latex: str) -> str:
    """Canonical form for equality checks only (not for display or MathML).

    Removes whitespace and spacing commands, ``\\left``/``\\right``/``\\big`` sizing,
    maps ``\\dfrac``->``\\frac``, bracket aliases to plain brackets, ``\\mathrm{..}``
    to its content, and single-token braces ``{x}``->``x``.
    """
    s = latex.strip()
    for old, new in _REPLACEMENTS:
        s = re.sub(re.escape(old) + r"(?![a-zA-Z])", new.replace("\\", "\\\\"), s)
    s = _MATHRM.sub(r"\1", s)
    s = _SIZE_CMDS.sub("", s)
    s = _SPACING_CMDS.sub("", s)
    s = re.sub(r"\s+", "", s)
    prev = None
    while prev != s:  # {{x}} -> x
        prev, s = s, _SINGLE_BRACE.sub(r"\1", s)
    return s


def same_expression(a: str, b: str) -> bool:
    return bool(a.strip()) and normalize_latex(a) == normalize_latex(b)


def _parses(latex: str) -> bool:
    """Sanity check from the chain: a reading that does not convert is not a vote."""
    if not latex.strip() or "\\?" in latex:
        return False
    try:
        latex_to_webassign_mathml(latex)
    except Exception:  # latex2mathml raises a mix of its own error types
        return False
    return True


# --------------------------------------------------------------------------- #
# consensus
# --------------------------------------------------------------------------- #
def _load(names: tuple[str, ...] | list[str], get: Callable[[str], VisionProvider]) -> dict[str, VisionProvider]:
    loaded: dict[str, VisionProvider] = {}
    for n in names:
        try:
            loaded[n] = get(n)
        except ProviderNotConfigured:
            continue  # brief: providers without a key are skipped silently
    return loaded


def _run(provider: VisionProvider, image_bytes: bytes, hint: str, result: Result) -> str | None:
    try:
        latex = provider.transcribe(image_bytes, hint=hint)
    except Exception as exc:  # network / HTTP / SDK errors: record, keep going
        result.errors[provider.name] = f"{type(exc).__name__}: {exc}"
        return None
    result.cost_usd += provider.cost_per_1k / 1000.0
    result.candidates[provider.name] = latex
    return latex


def _accept(result: Result, latex: str, *, agreed: bool) -> Result:
    result.latex = latex
    result.agreed = agreed
    result.mathml = latex_to_webassign_mathml(latex)
    return result


def transcribe_with_consensus(
    image_bytes: bytes,
    *,
    hint: str = "",
    cheap: tuple[str, ...] = DEFAULT_CHEAP,
    escalate: str = DEFAULT_ESCALATE,
    get: Callable[[str], VisionProvider] | None = None,
) -> Result:
    """Rules (revised 2026-09-19 after the Gemini/Pix2Text measurement):

    * one cheap reader configured (the default) and its reading parses -> accept;
    * several configured and they agree after ``normalize_latex`` -> accept;
    * a reading that contains ``\\?`` or does not parse is not a vote, so a
      doubtful page falls through to ``escalate``;
    * escalated reading parses and (if there were votes) matches one -> accept;
    * otherwise ``needs_human=True`` and no ``latex`` is returned.

    Accepting a single reading is deliberate: the caller must still show the
    rendered maths to the student before anything is submitted.
    """
    result = Result()
    get = get or registry.get
    cheap_providers = _load(cheap, get)

    votes: dict[str, str] = {}
    if cheap_providers:
        with ThreadPoolExecutor(max_workers=len(cheap_providers)) as pool:
            futures = {
                n: pool.submit(_run, p, image_bytes, hint, result)
                for n, p in cheap_providers.items()
            }
            for n, fut in futures.items():
                latex = fut.result()
                if latex is not None and _parses(latex):
                    votes[n] = latex

    readings = list(votes.values())
    if len(readings) == 1:
        # single trusted reader: accept it; the student's confirmation is the gate
        return _accept(result, readings[0], agreed=True)
    if len(readings) >= 2 and all(same_expression(readings[0], r) for r in readings[1:]):
        return _accept(result, readings[0], agreed=True)

    # escalation tier
    result.escalated = True
    tier2 = _load((escalate,), get).get(escalate)
    if tier2 is None:
        result.errors[escalate] = "ProviderNotConfigured"
        result.needs_human = True
        return result
    final = _run(tier2, image_bytes, hint, result)
    if final is not None and _parses(final):
        if not readings or any(same_expression(final, r) for r in readings):
            return _accept(result, final, agreed=False)

    result.needs_human = True
    return result
