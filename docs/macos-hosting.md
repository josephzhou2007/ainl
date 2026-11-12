**English** · [简体中文](macos-hosting.zh-CN.md)

# macOS hosting (Mac mini)

Last checked: 2026-09-24. The Windows equivalent is `docs/autoopenexplain.md` (Chinese); the `.ps1` scripts are kept and both setups coexist.

## 1. One-time install

```bash
cd ~/code/AINATIVELEARNINGOS
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests canvas-official/tests scale-up/tests "Cookie Management/tests" -q
```

> The Cookie Management directory has its own dependencies: `.venv/bin/python -m pip install -r "Cookie Management/requirements.txt"`.
> Use the project's own `.venv`; don't `pip install -e` into the system Python. `.venv/` is in `.gitignore`.

## 2. Secrets go in `~/.ainl/env` (the macOS counterpart of Windows `setx` user variables)

```bash
scripts/macos/service.sh install        # the first time, copies scripts/macos/env.example to ~/.ainl/env (chmod 600)
$EDITOR ~/.ainl/env                     # fill in CANVAS_TOKEN / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / GEMINI_API_KEY
scripts/macos/service.sh restart bot
```

`run.sh` `source`s this file before starting each service. **Secrets never go into the plist, code, logs or chats.**

## 3. Three launchd services (the counterparts of the three Windows .ps1 scripts)

| macOS | What it does | Windows counterpart |
|---|---|---|
| `com.ainl.browser` | headless Chromium on port 9222, `--foreground`, supervised by launchd | `browser_start.py` inside `autostart.ps1` |
| `com.ainl.bot` | your own private homework bot, `python -m ainl.bot` | `bot_restart.ps1` |
| `com.ainl.public-bot` | the public bot classmates register with, `scale-up/bot.py` (needs `TELEGRAM_PUBLIC_BOT_TOKEN`) | `public_bot_restart.ps1` |

```bash
scripts/macos/service.sh install                 # browser + bot (default)
scripts/macos/service.sh install public-bot      # add when needed
scripts/macos/service.sh status
scripts/macos/service.sh restart bot             # restart only the bot, leave the browser alone
scripts/macos/service.sh restart browser         # fresh renderer (the WebAssign session is lost; LTI rebuilds it in ~10s)
scripts/macos/service.sh logs bot                # tail ~/.ainl/bot.log.err — the real runtime log is here
scripts/macos/service.sh stop bot / start bot
scripts/macos/service.sh remove                  # uninstall everything
```

Differences from the Windows setup:

- **launchd guarantees one instance per label**, so `restart bot` can never produce two `getUpdates` consumers (on Windows the old process has to be found by command line and killed).
- **Crashes are restarted automatically** (`KeepAlive.SuccessfulExit=false`, 15-second interval). The Windows version has no supervisor; a crashed bot waits for the next login.
  The flip side: a configuration error (say, a missing token) is also retried every 15 seconds — `logs bot` shows why.
- At boot, `run.sh bot` first waits for the network (up to 2 minutes), then for the browser (up to 20 seconds), the same order as `autostart.ps1`.
- `browser_start.py --restart` (called by the watchdog when the browser hangs) detects that launchd manages the browser and uses `launchctl kickstart -k`
  instead of starting a second browser next to launchd's one fighting over 9222.
- Logs are **appended** (the Windows version overwrites on every start), so clean them up now and then.

It runs in the login session (`gui/<uid>`), not as a system-wide daemon: the browser profile and the secrets belong to this user, so the machine must stay logged in.
This machine has `pmset sleep 0` set, so it never sleeps.

## 4. When a human has to log in

The cookie channel needs NetID + Duo the first time. Nobody usually sits at the Mac mini, so:

```bash
scripts/macos/service.sh stop browser
.venv/bin/python scripts/browser_start.py --show      # opens a visible window (log in over Screen Sharing)
# when logged in, close it:
.venv/bin/python -c "import sys;sys.path.insert(0,'scripts');import browser_start as b;from ainl.config import home;b.kill_profile_holders(home()/'chrome-profile')"
scripts/macos/service.sh start browser
```

The token channel (the current main channel) doesn't need this step.

## 5. Public endpoint (webhook / OAuth callback, only needed after M9)

Decided in `ONBOARDING.md`: Cloudflare Tunnel. `cloudflared` is already installed here, with another tunnel running
(`~/Library/LaunchAgents/com.example.cloudflared.plist`); when the time comes, add an ingress rule to the existing tunnel instead of opening another.

## 6. Cross-platform change log (2026-09-24)

| File | What changed |
|---|---|
| `scripts/browser_start.py` | finds Playwright's Chromium per OS; detaches with `start_new_session` on POSIX; kills processes by the profile's **full path** (`ps -ax -o pid=,command=`, not by process name); per-OS user agent; new `--foreground`; `--restart` recognises launchd |
| `scale-up/browsers.py` | reuses `command()` / `spawn()` / `kill_profile_holders()` above, dropping its own Windows branch |
| `src/ainl/canvas/session.py` | the non-Windows "is the profile in use" check moved from `pgrep -af` to `ps -ax -o command=`. On macOS `pgrep -a` means "include ancestors" and prints only PIDs, so the old check **never matched** on a Mac |
| `pyproject.toml` | declares `pillow` (used all along by `gradescope.py` / `webassign.py`, but missing) |
| `.gitignore` | `.venv/` |

## 7. Live monitoring (`/monitor`, 2026-09-26)

The more classmates, the more headless Chromiums (one each; memory is mostly in the renderer/GPU/utility child processes), and 24 GB quietly runs out.
Only the owner can use this in the public bot:

- `/monitor` — sends a "📊 Monitor" Mini App button that opens `https://example.com/native-bot-learning/monitor`, refreshing every 3 seconds:
  a memory bar (orange at 80%, red at 90%), swap, CPU, load, "~N more browsers before 80%" (estimated from the average of current classmates' browsers);
  per person: school, approved / Canvas connected, browser MB and port, whether they are logging in, whether the assistant is up, queued items, last activity, today's messages / submissions / Canvas requests / errors;
  process groups: public bot, private bot, the owner's 9222 browser, each classmate's browser (recognised by `--user-data-dir=.../users/<id>/chrome-profile`
  or port 93xx, **summed over the whole process tree**), Playwright driver, cloudflared, orphaned browsers (a 93xx left behind by a crash), and "Everything else".
  MB prefers macOS's footprint (`top`, matching Activity Monitor) and falls back to RSS.
- `/monitor text` — the same content as one message (for when the page won't open; `/monitor` also answers in text when the login page isn't configured).
- Memory alerts: checked every minute, one alert each when crossing 80% / 90% (with the biggest consumers), one "recovered" below 75%; no repeat of the same level within 30 minutes.

Auth: the page is served by LoginServer (8790, the only port the tunnel exposes); every request to `<base>/monitor.json` must carry Telegram-signed initData
in the `X-Telegram-Init-Data` header, verified with the public bot's token, and the user id must equal `TELEGRAM_CHAT_ID`; otherwise it is always 403 and nothing is collected.
initData in the URL doesn't count; signatures expire after 2 hours and the page asks you to send `/monitor` again.
The response contains no command lines (cloudflared's command line may carry the tunnel token), cookies, tokens or message contents; people appear only as Telegram display name and chat id (as in `/users`).

Data sources: `scale-up/monitor.py` (psutil + read-only `stats()` from BrowserPool/Dispatcher/LoginServer + incremental reads of `~/.ainl/metrics`).
Public-bot metrics records carry `bot=public` and records from classmates' worker threads carry `uid`, so counts are per person; a submission = a successful Canvas
`POST …/assignments/:id/submissions` or discussion reply (WebAssign submits in the browser and is not counted).

A quick local look (bypassing the bot: only people in the registry, no live state): `.venv/bin/python scale-up/monitor.py` (`--json` for every field).
