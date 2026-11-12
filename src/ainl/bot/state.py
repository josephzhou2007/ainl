"""Per-chat conversation state, persisted as JSON so a restart doesn't lose
the /todo menu or the getUpdates offset. File bytes for a pending upload are
kept in memory only (re-send the file after a restart)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class MenuItem:
    idx: int
    course_id: str
    id: str                     # assignment id, or discussion topic id
    kind: str                   # assignment | discussion | quiz | external
    title: str
    course: str
    types: list[str]
    url: str
    due: str                    # display string
    code: str = ""              # "C2" = course block C, item 2 — what the user replies with
    vendor: str = ""            # where the files go if not Canvas ("gradescope"); set when opened
    done: str = ""              # "" = still to do; else how it is done ("submitted", "10/10")
    tool: str = ""              # the external tool's url Canvas sent with the item ("" = none / unknown)


@dataclass
class Draft:
    mode: str                   # text | url | file
    text: str = ""
    # mode == "file": one entry per attachment, in send order —
    # {"filename": str, "content_type": str, "size": int}. Bytes live only in
    # Conversation._file_bytes[chat_id] (same order), never on disk.
    files: list[dict] = field(default_factory=list)

    @property
    def filenames(self) -> list[str]:
        return [f["filename"] for f in self.files]

    @classmethod
    def from_dict(cls, raw: dict) -> "Draft":
        """Tolerates the pre-M2-A-02 single-file shape (filename/content_type/size)."""
        d = cls(mode=raw.get("mode") or "text", text=raw.get("text") or "",
                files=[dict(f) for f in raw.get("files") or []])
        if not d.files and raw.get("filename"):
            d.files = [{"filename": raw["filename"], "content_type": raw.get("content_type") or "",
                        "size": int(raw.get("size") or 0)}]
        return d


@dataclass
class MathSession:
    """Where we are in /math — the one flow that spans several messages.

    Everything else in the bot infers its step from whether ``pending`` is set.
    That cannot work here: a photo means "this is my answer sheet" in one step
    and "attach this to a Canvas assignment" in another, and the question ->
    answer-box mapping has to survive between the two.

    ``targets`` maps question number -> RA_ field name, captured when the
    assignment was opened, so an answer never lands in the wrong box.
    Images are not kept: re-send the photo after a restart.
    """
    # pick   -> waiting for the assignment button
    # shoot  -> waiting for the photo of the answer sheet
    # confirm-> read, waiting for "fill it in"
    # filled -> written into the boxes, waiting for submit / modify / cancel
    # fixing -> waiting for the replacement for question `fixing`
    stage: str = "pick"
    dep: str = ""
    title: str = ""
    # question number -> its RA_ boxes, in page order (several if it has blanks)
    targets: dict[int, list[str]] = field(default_factory=dict)
    # question number -> one LaTeX answer per blank, same order
    answers: dict[int, list[str]] = field(default_factory=dict)
    multipart: list[int] = field(default_factory=list)
    fixing: int = 0                     # which question a correction is for
    # Questions answered by tapping rather than writing. One entry per part:
    # {"n": question number, "name": input name, "kind": choice|multi|dropdown,
    #  "options": [{"value","label","image"}]}. Surveyed on this course, a
    # question can hold a choice AND a maths box at once, so these are kept
    # separately from `targets` rather than instead of them.
    taps: list[dict] = field(default_factory=list)
    picked: dict[str, list[str]] = field(default_factory=dict)   # part name -> values
    # The sheet as it was read, kept so that changing a tap can re-derive that
    # question's written answer (convergent <-> DIVERGES) without another model
    # call or another photo. `fixed` = questions the student corrected by hand,
    # which a re-derivation must not overwrite.
    sheet: str = ""
    fixed: list[int] = field(default_factory=list)
    # Written boxes that take the answer as bare text (RN_ numeric boxes, plain
    # text boxes): filled as typed, never converted to MathML.
    typed: list[str] = field(default_factory=list)
    # "Master It" parts not yet begun: {"n", "id" (the button), "label"}.
    # Answerable only after the button is pressed, which adds their boxes to
    # `targets` mid-round.
    steps: list[dict] = field(default_factory=list)
    # Questions that open one part at a time (Master It). Their sheet answers
    # are kept whole — (a), (b), … in order — and spent part by part.
    stepped: list[int] = field(default_factory=list)
    # question number -> boxes already submitted, so the next part's boxes
    # take the next values off the sheet, and a submitted box is never
    # written or submitted twice.
    done: dict[int, list[str]] = field(default_factory=dict)
    # question number -> "right" | "wrong" | "?" after a submit. With a wrong
    # one the round stays open (stage "graded") for corrections.
    graded: dict[int, str] = field(default_factory=dict)
    # TEST ONLY (/auto): the model answers and the taps are made by the bot.
    auto: bool = False
    auto_retries: int = 0
    item_id: str = ""                   # the Canvas item this round came from, if any
    filled: dict[str, str] = field(default_factory=dict)   # box -> the value last written
    # After a submit that was not all right: the questions the Redo button reopens.
    redo: list[int] = field(default_factory=list)
    # A second go (Redo, a typed change, an assignment worked on before): one
    # Submit button fills and submits, no separate Fill step.
    again: bool = False
    # question number -> the chat message holding its picture, so the filled
    # picture replaces it instead of arriving as another set of photos
    shot_msgs: dict[int, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "MathSession":
        return cls(
            stage=raw.get("stage") or "pick",
            dep=raw.get("dep") or "",
            title=raw.get("title") or "",
            # JSON object keys are strings; question numbers are ints
            targets={int(k): list(v) for k, v in (raw.get("targets") or {}).items()},
            answers={int(k): list(v) for k, v in (raw.get("answers") or {}).items()},
            multipart=list(raw.get("multipart") or []),
            fixing=int(raw.get("fixing") or 0),
            taps=[dict(t) for t in (raw.get("taps") or [])],
            picked={k: list(v) for k, v in (raw.get("picked") or {}).items()},
            sheet=raw.get("sheet") or "",
            fixed=[int(n) for n in raw.get("fixed") or []],
            typed=[str(b) for b in raw.get("typed") or []],
            steps=[dict(t) for t in (raw.get("steps") or [])],
            stepped=[int(n) for n in raw.get("stepped") or []],
            done={int(k): list(v) for k, v in (raw.get("done") or {}).items()},
            graded={int(k): str(v) for k, v in (raw.get("graded") or {}).items()},
            auto=bool(raw.get("auto")),
            auto_retries=int(raw.get("auto_retries") or 0),
            item_id=str(raw.get("item_id") or ""),
            filled={str(k): str(v) for k, v in (raw.get("filled") or {}).items()},
            redo=[int(n) for n in raw.get("redo") or []],
            again=bool(raw.get("again")),
            shot_msgs={int(k): int(v) for k, v in (raw.get("shot_msgs") or {}).items()},
        )


@dataclass
class QuizSession:
    """A Canvas quiz being answered in the chat (canvas/quiz.py).

    The attempt's validation token lives here because the answers are saved
    and the attempt completed across several messages. It only works for this
    one attempt of this one quiz."""
    course_id: str
    quiz_id: str
    title: str
    sub_id: str
    attempt: int
    token: str = field(repr=False)
    points: float = 0.0
    unlimited: bool = False
    item_id: str = ""
    questions: list[dict] = field(default_factory=list)
    answers: dict[str, object] = field(default_factory=dict)    # question id -> API answer
    stage: str = "answer"                                       # answer | confirm
    intro: str = ""                                             # the quiz's own instructions

    @classmethod
    def from_dict(cls, raw: dict) -> "QuizSession":
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})


@dataclass
class ChatState:
    menu: list[MenuItem] = field(default_factory=list)
    pending: Optional[MenuItem] = None
    draft: Optional[Draft] = None
    math: Optional[MathSession] = None
    # Rounds set aside with "Later": the answers are saved on WebAssign itself
    # and the round is kept here, so /resume can pick any of them up again.
    parked: list[MathSession] = field(default_factory=list)
    # WebAssign work as last seen by the bot, keyed by the WebAssign dep:
    # {"title", "item_id" (Canvas), "answered", "total", "points", "max", "complete", "at"}.
    # Canvas only learns a WebAssign assignment was done when WebAssign passes
    # the grade back, which can be days later — so /todo asks this first.
    wa_progress: dict[str, dict] = field(default_factory=dict)
    quiz: Optional[QuizSession] = None
    # The last /todo list sent: {"id": message id, "days": n, "all": bool} —
    # edited in place when a round finishes, so its item turns ✅ where it is.
    todo_msg: dict = field(default_factory=dict)


@dataclass
class BotState:
    offset: Optional[int] = None
    chats: dict[str, ChatState] = field(default_factory=dict)

    def chat(self, chat_id: str) -> ChatState:
        return self.chats.setdefault(chat_id, ChatState())

    # -- persistence ----------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "BotState":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        st = cls(offset=raw.get("offset"))
        for cid, c in (raw.get("chats") or {}).items():
            cs = ChatState(menu=[MenuItem(**m) for m in c.get("menu") or []])
            if c.get("pending"):
                cs.pending = MenuItem(**c["pending"])
            if c.get("draft"):
                cs.draft = Draft.from_dict(c["draft"])
            if c.get("math"):
                cs.math = MathSession.from_dict(c["math"])
            cs.parked = [MathSession.from_dict(m) for m in c.get("parked") or []]
            cs.wa_progress = {str(k): dict(v) for k, v in (c.get("wa_progress") or {}).items()}
            if c.get("quiz"):
                cs.quiz = QuizSession.from_dict(c["quiz"])
            cs.todo_msg = dict(c.get("todo_msg") or {})
            st.chats[cid] = cs
        return st

    def save(self, path: Path) -> None:
        raw = {"offset": self.offset, "chats": {
            cid: {"menu": [asdict(m) for m in c.menu],
                  "pending": asdict(c.pending) if c.pending else None,
                  "draft": asdict(c.draft) if c.draft else None,
                  "math": asdict(c.math) if c.math else None,
                  "parked": [asdict(m) for m in c.parked],
                  "wa_progress": c.wa_progress,
                  "quiz": asdict(c.quiz) if c.quiz else None,
                  "todo_msg": c.todo_msg}
            for cid, c in self.chats.items()}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
