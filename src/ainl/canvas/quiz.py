"""Canvas classic quizzes, answered in the chat by the student.

Tier A, like the rest of Canvas: the official Quiz Submissions API, no
browser. The student answers every question themselves; the bot shows the
questions, collects the answers, previews them, and completes the attempt
only after the student taps Submit. No model is involved anywhere here.

Measured on ESL116, 2026-09-25:
  - GET /courses/:c/quizzes (the list) is 404 when the course hides the
    Quizzes page — but a quiz fetched by id still works.
  - GET /courses/:c/quizzes/:q/questions is 403 for a student. The questions
    come from the ATTEMPT: /quiz_submissions/:id/questions, once one exists.
  - An attempt that was opened and never finished stays "untaken", and the
    submissions list still returns its validation_token — so an attempt the
    student began in the browser is picked up, not duplicated.

Flow:  quiz()  → attempt() [reuse untaken, else start one]  → questions()
       → save_answers()  → complete()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..util.html import html_to_text

log = logging.getLogger("ainl.quiz")

# What the chat can take an answer for. Anything else: the student gets the link.
SUPPORTED = {"multiple_choice_question", "true_false_question", "multiple_answers_question",
             "matching_question", "short_answer_question", "numerical_question",
             "essay_question"}


@dataclass
class Settings:
    id: str
    title: str
    time_limit: Optional[int]           # minutes
    allowed_attempts: int               # -1 = unlimited
    question_types: list[str]
    points: float
    lockdown: bool
    access_code: bool
    locked: bool
    due_at: str = ""
    # The quiz's own instructions. ESL116's fill-in-the-blanks keeps its WORD
    # BANK here, not in the questions (2026-09-25) — without it the questions
    # cannot be answered from the chat.
    description: str = ""
    # "Show one question at a time": the Quiz Submissions API refuses to list
    # its questions (401 "Cannot receive one question at a time questions in
    # the API", Study habits quiz, 2026-09-27) — AFTER the attempt has begun.
    one_at_a_time: bool = False

    @property
    def unsupported(self) -> list[str]:
        return [t for t in self.question_types if t not in SUPPORTED]

    @property
    def risky(self) -> bool:
        """Starting it costs something the student should agree to first."""
        return bool(self.time_limit) or self.allowed_attempts not in (-1, 0)


@dataclass
class Attempt:
    id: str
    attempt: int
    token: str = field(repr=False)      # validation_token: needed to answer and to finish
    started_at: str = ""
    reused: bool = False


def _text(html: str) -> str:
    out = html_to_text(html or "")
    return (out[0] if isinstance(out, tuple) else out).strip()


def quiz(http, course_id: str, quiz_id: str) -> Settings:
    q = http.get(f"/api/v1/courses/{course_id}/quizzes/{quiz_id}")
    return Settings(id=str(q["id"]), title=q.get("title") or "",
                    time_limit=q.get("time_limit"),
                    allowed_attempts=int(q.get("allowed_attempts") if q.get("allowed_attempts") is not None else -1),
                    question_types=list(q.get("question_types") or []),
                    points=float(q.get("points_possible") or 0),
                    lockdown=bool(q.get("require_lockdown_browser")),
                    access_code=bool(q.get("access_code")),
                    locked=bool(q.get("locked_for_user")),
                    due_at=q.get("due_at") or "",
                    description=_text(q.get("description") or ""),
                    one_at_a_time=bool(q.get("one_question_at_a_time")))


def attempts(http, course_id: str, quiz_id: str) -> list[dict]:
    return (http.get(f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/submissions") or {}).get(
        "quiz_submissions") or []


def attempt(http, course_id: str, quiz_id: str) -> Attempt:
    """The attempt in progress if there is one, else a new one (starts it)."""
    for s in attempts(http, course_id, quiz_id):
        if s.get("workflow_state") == "untaken" and s.get("validation_token"):
            return Attempt(id=str(s["id"]), attempt=int(s["attempt"]), token=s["validation_token"],
                           started_at=s.get("started_at") or "", reused=True)
    r = http.post(f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/submissions", {})
    s = (r.get("quiz_submissions") or [r])[0]
    return Attempt(id=str(s["id"]), attempt=int(s["attempt"]), token=s["validation_token"],
                   started_at=s.get("started_at") or "")


class QuestionsUnavailable(RuntimeError):
    """Canvas will not list this attempt's questions (not a login problem)."""


def questions(http, submission_id: str) -> list[dict]:
    """The attempt's questions, reduced to what the chat needs, in order."""
    from ._http import CanvasAuthError
    try:
        raw = (http.get(f"/api/v1/quiz_submissions/{submission_id}/questions") or {}).get(
            "quiz_submission_questions") or []
    except CanvasAuthError as e:
        # A 401 whose body names the quiz setting is not an expired login.
        if "one question at a time" in str(e).lower():
            raise QuestionsUnavailable("it shows one question at a time") from e
        raise
    out = []
    for n, q in enumerate(raw, 1):
        out.append({
            "n": n, "id": str(q["id"]), "type": q.get("question_type") or "",
            "text": _text(q.get("question_text") or ""),
            "answers": [{"id": str(a.get("id")), "text": _text(a.get("text") or a.get("html") or "")}
                        for a in q.get("answers") or []],
            "matches": [{"id": str(m.get("match_id")), "text": _text(m.get("text") or "")}
                        for m in q.get("matches") or []],
            "points": q.get("points_possible"),
        })
    return out


def save_answers(http, att: Attempt, answers: list[dict]) -> dict:
    """answers: [{"id": question id, "answer": <per type>}] — saved, not submitted."""
    return http.post(f"/api/v1/quiz_submissions/{att.id}/questions",
                     {"attempt": att.attempt, "validation_token": att.token,
                      "quiz_questions": answers})


def complete(http, course_id: str, quiz_id: str, att: Attempt) -> dict:
    """Finish the attempt — this is the submit. Returns the graded submission."""
    r = http.post(f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/submissions/{att.id}/complete",
                  {"attempt": att.attempt, "validation_token": att.token})
    return (r.get("quiz_submissions") or [r])[0]
