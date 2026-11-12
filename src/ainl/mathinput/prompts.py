"""Prompts shared by every vision provider.

Correctness requirement, not a style choice: the FERMAT over-correction study
(arXiv 2604.22774, cited in docs/research/handwriting-ocr-options-2026-09.md
sec. 3) found that vision-language models silently "fix" a student's wrong
answer 42-66% of the time, and stronger models do it more.  A transcription
that corrects the student's mistake would let a wrong answer be graded as
right.  So every provider gets the same VERBATIM instruction: transcribe what
is on the page, never simplify, never correct, never fill in missing steps.
Uncertain glyphs are marked ``\\?`` so a human can resolve them instead of
the model guessing.
"""

from __future__ import annotations

SYSTEM_PROMPT = (
    "You transcribe a photo of a handwritten math answer into LaTeX, VERBATIM. "
    "Output exactly one line containing only the LaTeX expression: no prose, "
    "no explanation, no $ delimiters, no code fences, no \\[ \\] or \\( \\). "
    "Use standard commands (\\frac, \\sqrt, \\sin, \\ln, \\pi, e^{...}, ^, _). "
    "Transcribe exactly what is written, symbol for symbol: do NOT simplify, "
    "do NOT correct mistakes, do NOT add missing steps or constants, do NOT "
    "solve. If the student wrote something wrong, reproduce it wrong. "
    "If a symbol is unreadable or ambiguous, write \\? in its place instead "
    "of guessing."
)

USER_PROMPT = "Transcribe this handwritten answer to one line of LaTeX, verbatim."


def user_prompt(hint: str = "") -> str:
    """User-turn text; ``hint`` describes the question type (e.g. 'indefinite integral')."""
    if hint:
        return f"{USER_PROMPT} Question context: {hint}"
    return USER_PROMPT
