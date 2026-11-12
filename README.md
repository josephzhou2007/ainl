# AI-Native Learning (ainl)

A Telegram assistant that sits on top of Canvas and the third-party homework platforms a course
hands off to (WebAssign, Gradescope, ...). It finds what is due, opens it, and hands in **work
the student has already done**, so the student spends time on the work, not on finding buttons.

> 中文简介：一个架在 Canvas 和各家作业平台之上的 Telegram 助手。它负责找出要交什么、打开它，
> 再把**学生自己做完的作业**交上去：手写答案拍照 → 识别 → 逐题预览 → 学生确认后才提交。
> 设计文档大多是中文：`ARCHITECTURE.md`、`ONBOARDING.md`、`docs/`。

## What it does

- `/todo`: everything due in the next days across all courses, one block per course, each item one tap away.
- **Canvas-native work** (text, file, link): sent through the official Canvas API after you confirm.
- **WebAssign**: the bot sends each question's picture; you answer choices with buttons and write the rest
  on paper, photograph the sheet, and a vision model **transcribes your handwriting** (the only model call in
  the normal flow). You see every answer filled in on a rendered screenshot and nothing is submitted until
  you tap Submit.
- **Gradescope**: PDF hand-in.
- Morning brief, deadline reminders, a session keep-alive, timing and metrics for every step.
- `scale-up/`: an experimental multi-student version (one public bot, per-person encrypted Canvas sessions).

## What it deliberately does not do

**It never produces answers.** The private development repo had a test-only switch that let a language
model play the student, so the fill-and-submit path could be exercised without a human. That solver is
not part of this repository; `/auto` and `/sweep` answer that they are unavailable. What gets handed in is
the student's own work.

## Setup (short version)

Python 3.11+.

```sh
python -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
```

Secrets go in environment variables (Windows user env) or `~/.ainl/env` (macOS; template in
`scripts/macos/env.example`), never in the repo:

| Variable | What |
|---|---|
| `CANVAS_TOKEN` | Canvas → Account → Settings → New Access Token (check your school allows personal tokens) |
| `CANVAS_BASE_URL` | your Canvas, e.g. `https://canvas.<school>.edu` |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_CHAT_ID` | `python -m ainl.telegram --discover` prints it; the bot serves only this chat |
| `GEMINI_API_KEY` | handwriting transcription (other providers in `src/ainl/mathinput/providers/`) |

Then `python -m ainl.bot`. Full guide: `docs/dev-setup.md` (Chinese); macOS hosting: `docs/macos-hosting.md`.

Tests (offline):

```sh
.venv/bin/python -m pytest tests canvas-official/tests scale-up/tests "Cookie Management/tests" -q
```

## Before you use it

- **Unofficial.** Not affiliated with or endorsed by Instructure (Canvas), Cengage (WebAssign), Gradescope or
  any university. Automating these sites may conflict with their terms or with your school's policies.
  Check both; you are responsible for how you use it.
- **Academic integrity is your job.** The tool hands in what you give it. Use it for work you did yourself,
  under your course's rules.
- **The multi-student mode handles other people's Canvas sessions.** If you run it for anyone but yourself,
  you are holding their credentials: keep `AINL_SESSION_KEY` secret, host it on a machine you control, and tell
  them exactly what it stores.
- Platform adapters were built against one university's Canvas and course setup (UW–Madison, fall 2026) and
  break when pages change.

## Layout

```
src/ainl/          the bot: canvas/ (API + cookie channel), external/ (WebAssign, Gradescope),
                   mathinput/ (handwriting -> LaTeX -> MathML), bot/, brief/, telegram/, util/
canvas-official/   minimal official-API client and submit probe
Cookie Management/ encrypted per-person session store
scale-up/          multi-student public bot (experimental)
scripts/           service management (macOS launchd, Windows), probes, reports
docs/              architecture, platform protocols, research notes
tests/             offline tests with small hand-made page fixtures
```

## License

MIT, see [LICENSE](LICENSE).
