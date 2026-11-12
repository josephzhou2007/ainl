"""Start the browser the tooling attaches to, as its own independent process.

Two things this gets right that the earlier browser_serve.py did not.

1. The browser is NOT a child of this script. It is launched detached and this
   script exits. Editing a tool, restarting the bot, or ending a Claude session
   therefore cannot close the window — and closing the window is expensive,
   because the student's Cengage login lives in it.

2. --disable-gpu. Without it this page is unusable: screenshots never return,
   document.fonts.ready never settles, CDP closes the connection mid-capture,
   and eventually the Playwright driver dies with EPIPE. With it, the same code
   reads eight questions in 4.8 s with nothing missing. Every "hang" chased for
   an hour was this one flag.

    python scripts/browser_start.py            # start it (no-op if already up)
    python scripts/browser_start.py --restart  # fresh renderer. Canvas's login survives;
                                               # WebAssign's does not (session-only cookies)
                                               # and is rebuilt over LTI on next use, ~10 s
    python scripts/browser_start.py --foreground
                                               # run it as OUR child and wait: for a
                                               # supervisor (launchd on macOS) that
                                               # restarts it when it dies

Runs on Windows, macOS and Linux. The Chromium is the one Playwright installed
(``playwright install chromium``); the per-OS cache locations are known here,
and PLAYWRIGHT_BROWSERS_PATH is honoured.

On macOS the browser is normally a launchd service (scripts/macos/service.sh).
``--restart`` notices that and asks launchd to bounce it instead of spawning a
second, unsupervised browser next to the first.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ainl.config import home  # noqa: E402

PORT = 9222
START_URL = "https://www.webassign.net/v4cgi/student.pl?action=home/index"

# The launchd label scripts/macos/service.sh installs for this browser.
LAUNCHD_LABEL = "com.ainl.browser"

FLAGS = [
    "--remote-debugging-address=127.0.0.1",
    "--no-proxy-server",              # the system proxy broke Canvas login
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",                  # see module docstring — this one matters
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",   # a background tab must still render
    # ONE --disable-features: Chrome honours only the last one given.
    # CalculateNativeWinOcclusion: Windows freezes rendering for windows it
    # thinks are covered. The window normally IS covered — it sits behind the
    # editor — so screenshot requests waited on a renderer that had stopped
    # painting. That is why every hang "fixed itself" on restart: a fresh
    # window comes to the front. (Harmless on macOS/Linux.)
    # The rest: browser services a bot never uses, each a process or a timer.
    "--disable-features=CalculateNativeWinOcclusion,Translate,OptimizationHints,"
    "MediaRouter,AutofillServerCommunication,InterestFeedContentSuggestions",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # Lean: nothing here is used by a headless bot browser (2026-09-26: a
    # browser holding only Chrome's new-tab page ran 5 renderers).
    "--disable-extensions",
    "--disable-component-update",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--no-pings",
]

# Analytics and tracking, never needed to do homework. Measured 2026-09-26: a
# WebAssign page loaded ~60 resources, 4 of them WebAssign's own; the rest were
# these (Amplitude alone 30+, including its session-replay recorder). They are
# refused at name resolution — no request interception, no per-request cost.
# Consent (OneTrust) and site search (Coveo) are NOT here: they may be doing
# something the page needs.
BLOCKED_HOSTS = (
    "*.amplitude.com",
    "*.googletagmanager.com", "*.google-analytics.com", "analytics.google.com",
    "*.doubleclick.net", "*.googlesyndication.com",
    "*.evergage.com", "*.evgnet.com",
    "*.pendo.io", "pendo-static-*.storage.googleapis.com",
    "*.hotjar.com", "*.fullstory.com", "*.clarity.ms", "*.mixpanel.com",
    "*.heapanalytics.com", "*.nr-data.net",
    # Seen in a HAR of canvas.wisc.edu's dashboard (2026-09-26): DesignPlus
    # (course-page styling, 6.8 MB of CSS — the same file loaded twice) and
    # Instructure's Sentry relay. The bot reads Canvas through its API.
    "*.ciditools.com", "*.sentry.insops.net",
)
FLAGS.append("--host-resolver-rules=" + ", ".join(f"MAP {h} ~NOTFOUND" for h in BLOCKED_HOSTS))


# --- where things are, per OS ------------------------------------------------

def _browsers_root() -> Path:
    """Playwright's browser cache for this OS (or PLAYWRIGHT_BROWSERS_PATH)."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override and override != "0":
        return Path(override).expanduser()
    if sys.platform == "win32":
        return Path.home() / "AppData/Local/ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/ms-playwright"
    return Path.home() / ".cache/ms-playwright"


def _revision(p: Path) -> int:
    """chromium-1243 -> 1243, so 1243 sorts after 999 (lexical order would not)."""
    m = re.search(r"chromium-(\d+)", str(p))
    return int(m.group(1)) if m else -1


def chromium() -> Path:
    """The Chromium executable Playwright installed, newest revision."""
    root = _browsers_root()
    if sys.platform == "win32":
        pattern = "chromium-*/chrome-win*/chrome.exe"
    elif sys.platform == "darwin":
        pattern = "chromium-*/chrome-mac*/*.app/Contents/MacOS/*"
    else:
        pattern = "chromium-*/chrome-linux*/chrome"
    found = sorted((p for p in root.glob(pattern) if p.is_file()), key=_revision)
    if found:
        return found[-1]
    # Unknown layout — ask Playwright itself (costs ~1 s to start its driver).
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = Path(pw.chromium.executable_path)
        if exe.is_file():
            return exe
    except Exception:
        pass
    raise SystemExit("no Playwright Chromium found — run: playwright install chromium")


def user_agent() -> str:
    """A real-looking UA for THIS OS: the default headless UA says HeadlessChrome."""
    if sys.platform == "win32":
        plat = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        plat = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        plat = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def headless_look() -> list[str]:
    return ["--headless=new", "--window-size=1600,1400", f"--user-agent={user_agent()}"]


def command(profile: Path, port: int = PORT, url: str = START_URL, show: bool = False) -> list[str]:
    """The full command line for a browser on ``profile`` / ``port``."""
    look = [] if show else headless_look()
    cmd = [str(chromium()), f"--user-data-dir={profile}",
           f"--remote-debugging-port={port}", *FLAGS, *look]
    if url:
        cmd.append(url)
    return cmd


# --- processes ---------------------------------------------------------------

def spawn(cmd: list[str], detach: bool = True) -> subprocess.Popen:
    """Start ``cmd``; detached (survives us) unless ``detach`` is False."""
    if not detach:
        return subprocess.Popen(cmd)
    if sys.platform == "win32":
        # DETACHED_PROCESS so it does not die with this script or its console.
        return subprocess.Popen(
            cmd, close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    # Its own session: no controlling terminal, no SIGHUP when ours goes away.
    return subprocess.Popen(cmd, close_fds=True, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def holders(needle: str) -> list[int]:
    """PIDs of browser processes whose command line contains ``needle``.

    Match on the command line — the profile path — NEVER on the process name:
    stopping every "chrome" on the machine has taken down unrelated work.
    """
    needle_l = needle.lower()
    me = os.getpid()
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        out = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                line = " ".join(p.info["cmdline"] or [])
            except Exception:
                continue
            if p.info["pid"] != me and needle_l in line.lower():
                out.append(p.info["pid"])
        return out
    if sys.platform == "win32":
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=15).stdout
    else:
        # -o with '=' suppresses the header; portable across macOS and Linux
        # (macOS pgrep has no way to print the full command line).
        ps = subprocess.run(["ps", "-ax", "-o", "pid=,command="],
                            capture_output=True, text=True, timeout=15).stdout
    out = []
    for line in ps.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and needle_l in parts[1].lower():
            pid = int(parts[0])
            if pid != me:
                out.append(pid)
    return out


def kill_profile_holders(profile: Path) -> int:
    """Stop only the processes using THIS profile. Returns how many were signalled."""
    pids = holders(str(profile))
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(pids)


def alive(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3):
            return True
    except Exception:
        return False


def _wait_alive(port: int, seconds: int = 20) -> bool:
    for _ in range(seconds):
        time.sleep(1)
        if alive(port):
            return True
    return False


# --- launchd (macOS) ---------------------------------------------------------

def _launchd_target() -> str:
    return f"gui/{os.getuid()}/{LAUNCHD_LABEL}"


def launchd_managed() -> bool:
    """Is the browser a loaded launchd service on this Mac?"""
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["launchctl", "print", _launchd_target()],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def launchd_restart() -> bool:
    """Ask launchd to kill and relaunch the browser service."""
    try:
        r = subprocess.run(["launchctl", "kickstart", "-k", _launchd_target()],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


# --- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--url", default=START_URL)
    ap.add_argument("--restart", action="store_true",
                    help="kill the current browser first")
    ap.add_argument("--show", action="store_true",
                    help="open a visible window. Only needed when a human has to sign "
                         "in by hand (Canvas NetID + Duo for the cookie channel).")
    ap.add_argument("--foreground", action="store_true",
                    help="run the browser as a child of this process and wait for it "
                         "(for a supervisor such as launchd). Exits 0 without starting "
                         "anything if a browser already answers on the port.")
    a = ap.parse_args()

    profile = home() / "chrome-profile"

    if a.foreground:
        if alive(a.port):
            print(f"already running on {a.port} — leaving it alone")
            return 0
        proc = spawn(command(profile, a.port, a.url, a.show), detach=False)
        # Forward a supervisor's stop to the browser, then wait for it.
        def _stop(signum, frame):
            proc.terminate()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _stop)
        if _wait_alive(a.port):
            print(f"browser up on {a.port} (foreground, profile {profile})", flush=True)
        rc = proc.wait()
        print(f"browser exited with {rc}", flush=True)
        return rc if rc >= 0 else 1

    if a.restart:
        if launchd_managed():
            # The service owns the process: bounce it there, or we would end up
            # with launchd's browser AND ours fighting over the port.
            if launchd_restart() and _wait_alive(a.port, 30):
                print(f"browser restarted by launchd on {a.port}")
                return 0
            print("launchd did not bring the browser back", file=sys.stderr)
            return 1
        kill_profile_holders(profile)
        time.sleep(3)
    elif alive(a.port):
        print(f"already running on {a.port} — leaving it alone")
        return 0

    # No window unless asked for. A visible one jumps to the front every time
    # it starts — and the watchdog restarts it whenever a page wedges — which
    # on a machine someone is working at means a browser landing on top of
    # their work again and again. Nothing here needs to be seen: Canvas is
    # reached by token, and the WebAssign session is rebuilt over LTI.
    # Screenshots come from the renderer, not from the screen.
    spawn(command(profile, a.port, a.url, a.show), detach=True)

    if _wait_alive(a.port):
        print(f"browser up on {a.port}. Log in if it asks; the login is kept "
              f"in {profile} and survives a restart.")
        return 0
    print("browser did not come up", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
