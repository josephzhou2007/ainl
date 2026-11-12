**English** · [简体中文](ARCHITECTURE.zh-CN.md)

# AI-Native Learning — layered architecture v0.2

## What it is

**Not a replacement for Canvas: an inbox and an agent sitting on top of it.**

A student can use it without the school's approval. The agent goes through Canvas and the 4–5
third-party sites a course hands off to, so that "finding things" disappears and only "doing the work"
and "learning" are left.

## Product thesis (distilled from the author's own words)

> China integrates everything into an app: a few taps and you're done. The US keeps a lot of human
> steps: bureaucratic, but humane, because you can actually talk to a person instead of being
> frustrated by a button you can't find.

Each model has its own failure mode:

| | Integrated (China-style) | Human (US-style) |
|---|---|---|
| Strength | efficient, few steps | you can ask follow-ups, get explanations, a fallback |
| Failure mode | **once something breaks there is nowhere to appeal**; the system doesn't explain itself | **huge friction**; when you can't find the way in, there is nobody to ask |

**The unique value of an LLM layer is that it offers both at once**: integration underneath (automatic
sync, reminders, hand-in) and conversation on top (ask "why", "where", "what does this mean" at any time).
That is the real reason this project exists, not "yet another LMS".

---

## Eight layers

```
 L7  Act        hand in / deep links                 ← exit
 L6  Converse   one Telegram bot + topic routing     ← the only touchpoint
 L5  Schedule   8 AM brief / deadline escalation / plan progress
 L4  Plan       LLM splits a big assignment into multi-day tasks
 L3  Knowledge  slides/PDFs indexed + retrieval = a tutor per course
 L2  Route      per course: "where does everything actually live"
 L1  Normalize  one schema (SQLite)
 L0  Ingest     API ‖ scrape, two channels           ← entry
```

---

### L0 Ingest — two interchangeable channels

The author's call was right: **don't bet everything on the Canvas API**. Not because Instructure would
block it (Canvas is AGPL open source and the API is publicly documented), but because **school admins can
switch capabilities off one by one**. Measured: on this account `/api/v1/users/self/page_views` returns
**403 unauthorized**, switched off by the UW admins.

So both channels exist from day one, behind the same interface:

```python
class CanvasSource(Protocol):
    def courses(self) -> list[Course]: ...
    def assignments(self, course_id) -> list[Item]: ...
    def materials(self, course_id) -> list[Material]: ...

class ApiSource(CanvasSource):     # fast, stable, structured. First choice
class ScrapeSource(CanvasSource):  # slow, brittle, but nobody can switch it off. Fallback
```

Upper layers never know where the data came from. API switched off → swap the implementation, zero changes above.

**Tool choices for the scrape channel:**

| Need | Choice | Why |
|---|---|---|
| Browser driver | **Playwright** (Python) | the only one with native `persistent_context`, so login state survives restarts |
| Login state | `launch_persistent_context(user_data_dir=...)` | log in to SSO by hand once, reuse for a long time, never touch the password |
| Multiple sites | one **adapter class** per site | Canvas / Gradescope / McGraw Hill each get their own, sharing one browser context |
| Resisting drift | semantic selectors (`get_by_role`) instead of CSS paths | an order of magnitude more likely to survive a site redesign |

**How the sites keep "knowing who you are"**: every adapter shares **the same** persistent context. Log in
to Canvas once and Gradescope once on the Mac mini, the cookies live in that profile, and every script uses
it. This is also why it has to run locally: login state can't go to the cloud.

### L1 Normalize — one schema

The four courses have wildly different shapes (MATH222 is all `external_tool`, PHYSICS247 has 2 items),
so everything must be flattened into one table before upper layers can be generic.

```
course(id, code, name, term, source)
item(id, course_id, kind, title, due_at, url, submit_tier, external_host)
material(id, course_id, title, mime, local_path, sha256, fetched_at)
route(course_id, yaml)
task(id, item_id, title, planned_for, status)      -- produced by L4
event(id, kind, payload, at)                        -- audit
```

The key field `submit_tier` ∈ {A, B, C} decides which path L7 takes.

### L2 Route — the project's core asset

Each course's mess comes from **the professor's personal habits**, not from Canvas, and those rules
**live only in the syllabus and the first lecture's spoken instructions; the API can't see them**.

Source: syllabus PDF + transcript of the first lecture → LLM fills in YAML → checked by hand once → used
for the rest of the term. Format: `routes/*.yaml`.

### L3 Knowledge — a tutor per course

- Drop the professor's slides / notes / textbook into the topic and they are indexed, chunked and embedded
- Canvas materials are fetched by L0 automatically, no manual downloading
- One corpus per course → **this is the real implementation of "each bot has its own context window"**
  (no need for several bots; isolating the retrieval scope per topic is enough)
- Supports preparing ("what's this lecture about, what do I need first") and review ("today's three key points")

> **Boundary**: L3 may explain, generate practice problems, notice what you didn't understand and ask you.
> It **does not write answers for graded work**, and L7 **does not hand in anything you didn't do**.
> Once that line is crossed, the project can never be offered to a school.

### L4 Plan — "a task isn't always one day"

The real problem the author pointed out: some assignments give you a week, but the calendar shows only one deadline.

```
Item(due 7 days out, estimated 6h of work)
   └─ LLM split ─► Task(D-6: read ch.3)
                   Task(D-4: problems 1-10)
                   Task(D-2: problems 11-20 + check)
                   Task(D-1: upload)
```

The split goes into the `task` table and L5 pushes it day by day. The student can change it in the topic
directly ("move step two to the weekend").

### L5 Schedule

- **Every day 08:00** — brief: what's due today, what the plan says to move forward today, any new materials
- **Deadline escalation** — D-3 / D-1 / D-0, sent per course to its own topic
- **Plan progress** — reminders for L4's subtasks when they come due
- **Change detection** — poll L0; new assignments / materials / date changes are pushed immediately
  (this is what replaces "the teacher posted one line in the group chat")

Implementation: APScheduler (Python) with a persistent job store in SQLite, so a Mac mini restart loses nothing.

### L6 Converse — one bot, topic routing

**Decision: one bot + Forum Topics, not several bots.**

Telegram's hard constraints:
1. In groups, bots default to **privacy mode** and only see messages that @mention them, reply to them, or are `/commands`
2. **Bots cannot see each other's messages**, so multi-bot collaboration doesn't work on Telegram
3. Turning privacy mode off makes every bot see every message in the group, which is worse

The right shape:

```
Supergroup "My courses" (Topics on)
 ├─ 📌 Overview    ← the 08:00 brief goes here
 ├─ MATH 222      ← every message in this topic goes into the MATH222 context
 ├─ PHYSICS 247
 ├─ SPANISH 101
 └─ ESL 116
```

The bot tells the course from `message.message_thread_id`: **you just talk, no @, no switching buttons**.
Each topic keeps its own history and retrieval scope = the "different bots with different context windows" the author wanted.

Cost: all four courses share one bot avatar and name (a purely visual loss).

**File-size limits (important, affects pushing textbooks in L3)**:
the standard Bot API uploads up to **50 MB** and downloads up to 20 MB. For 2 GB you must run a
**local Bot API server** on the Mac mini (officially supported, open source). Textbook PDFs often exceed
50 MB, so this step is essentially required and belongs on the milestone list.

### L7 Act — three tiers

| Tier | For | Mechanism | Status |
|---|---|---|---|
| **A fully automatic** | Canvas-native `online_upload` / `online_text_entry` / `online_url` / discussions | official REST API | ✅ live |
| **B browser hand-in** | **WebAssign (MATH222)**; Gradescope (PHYSICS247) | in the student's own logged-in session, fill in **the student's own** answers, send the rendered result back to the chat, submit **only after the student confirms** | WebAssign ✅; Gradescope not done |
| **C one-tap deep link** | McGraw Hill (SPANISH101) and other platforms not yet connected | deep link only | ✅ |

Even tier C alone removes about 80% of the pain, because the pain was never "pressing submit", it was "finding where to submit".

> **Revised 2026-09-19.** The first version put WebAssign in tier C ("never hand in graded assessments"),
> and tier B said "Gradescope (all of MATH222)" — the latter was simply wrong: MATH222 uses WebAssign.
> The C→B change came from the student: if the final Submit still has to be pressed back in the browser,
> the middleman is back. The line held is unchanged — **the agent carries what you made; it never produces,
> and never hands in, what you didn't do**: the answers are the student's handwriting, before submitting the
> student sees the rendered result the platform will actually grade, and nothing is submitted without confirmation.
>
> Where to hand in isn't fixed within a course either: PHYSICS247 PSets 1/2 are `online_upload` (handed in
> **once on Canvas and once on Gradescope**), PSet 3 is `external_tool` (Gradescope only, grade passed back
> automatically). This can be read from `submission_types`; see `src/ainl/external/routing.py`.

---

## Milestones

| | Content | Risk | Status (2026-09-20) |
|---|---|---|---|
| **M0** | recorder collects real click paths (evidence) | none | ✅ 547 events, 21 domains |
| **M1** | L0(API) + L1 + L5 + L6 → brief and reminders in Telegram | none, read-only | ✅ |
| **M2** | L3 materials indexed + pushed (needs local Bot API server) | low | partial: assignment attachments are pushed to the chat; textbooks not done |
| **M3** | L2 route table (from professor interviews) | low | changed to **reading Canvas**: `routing.py` decides from `submission_types` + tool URL, no interviews needed |
| **M4** | L7-A Canvas-native hand-in | low | ✅ incl. multiple files, discussions |
| **M5** | L4 planning + L3 tutor Q&A | medium | not done |
| **M6** | L7-C deep links | low | ✅ |
| **M7** | L7-B browser hand-in | high, needs ongoing maintenance | WebAssign ✅ (handwriting → LaTeX → MathML, choices by tap, submit after confirmation); Gradescope not done |
| **M8** | L0 fallback channel (insurance if the API is switched off) | medium | ✅ cookie channel + automatic fallback + session keep-alive |
| **M9** | OAuth2 + multi-user → something a school could adopt | policy | not done. The token is approved by the school (single user) |

Authentication today: **the school-approved access token is the main channel**, the browser cookie is the
fallback (the first version assumed the opposite). WebAssign sessions don't rely on a password; they are rebuilt
through Canvas's LTI sessionless launch, which works for any student with a Canvas session.
