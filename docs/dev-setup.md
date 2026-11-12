**English** · [简体中文](dev-setup.zh-CN.md)

# Development setup and running

Last checked: 2026-09-24. Hosted on a Mac mini; a Windows laptop still works for development.

**macOS (the host)** — full guide in `docs/macos-hosting.md`:

```bash
cd ~/code/AINATIVELEARNINGOS
/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests canvas-official/tests scale-up/tests "Cookie Management/tests" -q
scripts/macos/service.sh install        # browser + bot as launchd services; secrets go in ~/.ainl/env
```

**Windows**:

```powershell
cd D:\code\local\projects\AI-native-learning
python -m pip install -e ".[dev]"
python -m pytest tests canvas-official/tests -q
```

> ⚠️ **Don't run `pip install -e` again inside a git worktree or another copy.** It repoints the editable
> install at that copy, and the running bot silently loads old code. Learned the hard way.

## 1. Secrets: Windows *user* environment variables, macOS `~/.ainl/env`

```powershell
setx CANVAS_TOKEN        "<Canvas → Account → Settings → New Access Token>"
setx TELEGRAM_BOT_TOKEN  "<from @BotFather>"
setx TELEGRAM_CHAT_ID    "<printed by python -m ainl.telegram --discover>"
setx GEMINI_API_KEY      "<Google AI Studio>"
```

`setx` only affects **newly opened** terminals. On macOS it is `~/.ainl/env` (chmod 600, read by `scripts/macos/run.sh`
before starting a service). **Secrets never go into chats, code or logs.**

## 2. Canvas: two channels

| | token (main) | cookie (fallback) |
|---|---|---|
| Source | a school-approved personal access token | a logged-in Canvas session in the browser |
| Needs a human | no | NetID + Duo the first time |
| Expires | when revoked | kept alive by activity; a background keep-alive every 30 minutes |
| Capability | REST + GraphQL (`canvas-official/PROBE.md`: 11/13 measured) | the same |

`ApiSource.from_env()` prefers the token and falls back to the cookie on failure, and **says so** in Telegram.
`/channel auto|token|cookie` pins a channel by hand; pinning `token` means "I want to know whether the token
still works", so it never falls back.

The cookie channel reads **the live cookies in the running browser**, not a disk snapshot (a snapshot is only as
fresh as the last save — a 46-hour-old snapshot once caused a 401). If neither channel has a session, the bot opens
the Canvas login page and waits for you.

```bash
python -m ainl.canvas --whoami
python -m ainl.canvas --planner --days 14
python -m canvasofficial --probe          # which endpoints this token can actually call (run in canvas-official/)
```

## 3. The browser: headless, its own process

WebAssign has no API, so it needs a real browser. **It is not a child process of any script**: it sits on debug
port 9222, tools connect, do their work and disconnect.

```bash
python scripts/browser_start.py             # does nothing if already running
python scripts/browser_start.py --restart   # a fresh renderer
python scripts/browser_start.py --show      # open a window only when a human must log in
```

Headless by default, **no window on screen**. Facts paid for with incidents:

- **`--disable-gpu` is required.** Without it screenshots never return, `document.fonts.ready` never resolves and CDP disconnects.
- **A minimised window = opening an assignment hangs 100% of the time** (3/3, 46 seconds). Minimised windows don't paint. Headless removes the problem.
- **Cengage / WebAssign session cookies are session-scoped** (`JSESSIONID`, `cmp-session-id`) and vanish when the browser closes.
  So **every restart logs you out of WebAssign** — but no password is needed: the code rebuilds the session through Canvas's
  LTI launch (about 10 seconds). `research/hosted-browser-poc/REPORT-2026-09-15.md` had already recorded this; it was forgotten and hit again.
- **`sync_playwright()` can only reliably start once per process**, so the driver is a per-thread singleton (`util/pw.py`);
  the CDP connection is cached and reused — **do not close it**.
- **Don't open two browser contexts logged in to Cengage for the same student**; they kick each other out.
- **Don't open a second tab in this headless browser.** Navigations after `ctx.new_page()` often never commit (three times on
  2026-09-21, also hanging the driver connection). Borrow the existing tab and navigate it back to the WebAssign list afterwards —
  `external/gradescope.py` does exactly that.

## 4. Telegram

1. **@BotFather** → `/newbot` → get the token → open the bot and press **Start**
2. `python -m ainl.telegram --discover` prints `TELEGRAM_CHAT_ID`
3. `python -m ainl.telegram --ping` — a ✅ means it works

A bot token can have **only one** `getUpdates` consumer; a second instance gets 409 on both sides.

**Transcript**: the logs only say "sent a message", not what. The bot appends **every** message between student and
bot — incoming text / photos / files / button taps, outgoing text / images / albums / button edits — one JSON per line to
`~/.ainl/transcript.jsonl` (under `AINL_HOME`); every image it handles (the student's answer sheets, question pictures,
filled-in screenshots) goes to `~/.ainl/photos/`, referenced by relative path. This is what debugging relies on.

```bash
python -m ainl.telegram --transcript          # last 40: time ←/→ type text (first 300 chars) [image path] {buttons}
python -m ainl.telegram --transcript 200
python -m ainl.telegram --transcript-since 2026-09-24T23:00     # from a local time
AINL_TRANSCRIPT=0 python -m ainl.bot          # turn it off (off by default in tests)
```

A write failure logs one warning and never affects the bot; the transcript **contains no tokens**.
> ⚠️ The transcript and `photos/` contain the student's homework and answer photos. **Don't share them, don't commit them, don't paste them into chats.**

## 5. The conversation bot

```bash
python -m ainl.bot          # long-running (restart: Windows scripts/bot_restart.ps1, macOS scripts/macos/service.sh restart bot; neither touches the browser), long polling; serves only TELEGRAM_CHAT_ID
```

**There is one entry point: `/todo`.** Everything else starts from the codes it gives you.

```
/todo            today → +4 days, one block per course (A, B, C…)
/todo 14         look 14 days ahead (also a button under the list)
reply B6         open that item:
   ✍️ 📎 💬      Canvas-native assignment — send text / a file / a link; handed in through the official API after you confirm
   🧮            WebAssign — the bot sends a picture of each question; tap buttons for choices, write the rest on one sheet and send one photo
   ↗             a platform it can't handle yet — you get the link
/cancel          drop the current item (and release the images it holds)
/restart         clear all half-finished state (use when the bot stops responding)
/timing          recent per-step timings
/channel         is Canvas using the token or the cookie
```

A WebAssign round: question pictures → you photograph your answer sheet → **Gemini reads it once** (the only LLM call in the
whole system) → per-question preview → `Fill` → look at the rendered screenshot → `Submit / Modify / Cancel`.
**Nothing is submitted until you confirm.** Modify prefers typed corrections (0 model calls).

State lives in `~/.ainl/bot_state.json`. Images are kept only in memory and released when the round ends.

## 5.5 `/auto` and `/sweep` (not in the public build)

The private repository had a developer-only switch that let a language model answer in the student's place, to exercise the
fill / submit / grade path end to end. The public build **does not include that solver**; `/auto` and `/sweep` only reply that
they are unavailable. The product itself never produces answers: what gets handed in is always the student's own work.

## 5.6 After submitting

After a submit the bot reports ✓ / ✗ per question. With any ✗ the round doesn't end (stage `graded`): type `2: …`, or send another
photo (the whole corrected page, or just the corrected questions); only questions that **differ** from the submitted answers are
refilled and resubmitted. Step-by-step questions (Master It) come last, asked one step at a time after the others are submitted. `/cancel` exits.

## 6. Morning brief

```bash
python -m ainl.brief --preview | --now | --serve     # --serve: every day at 08:00 (Canvas time zone)
```

## 7. Timing and troubleshooting

- Every browser operation has a budget; on timeout the watchdog (`util/browserthread.py`) gives up and retries, restarting the browser if needed.
- Per-step timings: set `AINL_TRACE_DETAIL=1`, run `python scripts/timing_trace.py`, then `python scripts/timing_report.py`.
- Status and to-dos: `docs/optimization-goals.md`; measurements: `docs/timing-analysis.md` (both Chinese).
- Scripts that use Playwright often print a Node `EPIPE` stack trace **at the end** — exit noise from an abandoned driver,
  not a failure. **Read the start of the output.**

## Code layout

```
src/ainl/
  config.py                 environment variables; secrets never repr'd
  canvas/
    api.py                  ApiSource: REST + GraphQL, submissions, sessionless_launch
    channel.py              token / cookie choice and pinning
    session.py              BrowserSession; reads cookies from the live browser; opens the login page
    keepalive.py            background Canvas session keep-alive
    _http.py auth.py models.py source.py
  external/
    webassign.py            list assignments / question pictures / question types / fill / choose / submit / LTI self-heal
    link.py                 Canvas assignment ↔ WebAssign assignment (LTI first, title as fallback)
    routing.py              where this assignment actually goes (Canvas / Gradescope / both)
    platforms.py            registry of external platforms
  mathinput/                handwriting → LaTeX → WebAssign-dialect MathML; pluggable vision models
  bot/                      conversation.py (dialogue), state.py (persistent state)
  telegram/client.py        send / send_photo / send_media_group / set_commands
  brief/                    morning brief
  util/                     timing (categorised spans), browserthread (watchdog), pw (driver singleton)
canvas-official/            standalone token client using only documented REST + a capability probe
scripts/                    browser_start, timing_*, wa_survey (question-type census), etc.
tests/                      all offline, MockTransport / Fake*
```
