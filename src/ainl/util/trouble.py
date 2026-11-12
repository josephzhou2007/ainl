"""When something goes wrong: keep the scene, tell the owner, file it for the 4 AM review.

Code alone never showed what the page looked like when a step failed, so the
same page problem was found again and again by a student. Now every failure
leaves its scene behind, labelled by WHO failed:

  script — OUR code, on a real page: a selector that missed, a timeout, a page
           that changed. The nightly maintainer root-causes and fixes these.
  jev    — Jev's attempts and misses (the trials, and later its fallback
           choices). Evaluation data: they measure Jev; they are NOT bugs in
           our scripts, and the scripts are never "fixed" for them.

    <AINL_HOME>/failures/<source>/<YYYY-MM-DD>/<HHMMSS>-<chat>-<where>/
        event.json    what, where, whose chat, the bot's own message, the error
        tab-N.png     what the browser showed (each open page, up to 3)
        tab-N.html    that page's DOM
    <AINL_HOME>/failures/index.jsonl   one line per event, with its source

Nothing here may break the bot: every step swallows its own errors.
AINL_TROUBLE=0 turns it off (the tests do).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

SOURCES = ("script", "jev")
DEDUPE_SECONDS = 120        # the same failure again within this: counted, not re-filed
MAX_TABS = 3

# (text, png or None) -> None; set by the bot's entry point. None: nobody is told.
_notify: Optional[Callable[[str, Optional[bytes]], None]] = None
_owner = ""
_recent: dict[tuple, list] = {}      # dedupe key -> [first time, count]
_lock = threading.Lock()
_threads: list[threading.Thread] = []


def enabled() -> bool:
    return os.environ.get("AINL_TROUBLE", "1") != "0"


def root() -> Path:
    from ..config import home
    return home() / "failures"


def set_notifier(fn: Optional[Callable[[str, Optional[bytes]], None]], owner: str = "") -> None:
    """Who hears about failures, and whose chat is the owner's (the owner sees
    their own chat's errors already — those are filed, not re-sent)."""
    global _notify, _owner
    _notify, _owner = fn, str(owner or "")


def owner() -> str:
    return _owner


def wait(timeout: float = 30) -> None:
    """Let background captures finish (tests, and scripts about to exit)."""
    for t in list(_threads):
        t.join(timeout)
    _threads.clear()


# -- which bot messages are failures --------------------------------------------

_LATER_LINE = re.compile(r"^(❌|⚠️.*\b(couldn'?t|didn'?t|empty|different|failed|broke|expired)\b)",
                         re.I)


def problem_line(text: str) -> str:
    """The line of a bot message that says something went wrong, or ''.

    The first line opening with ❌/⚠️ is a failure message. Later lines count
    only when they say a step failed ("⚠️ Couldn't press submit on: 3") — not
    the notes a good result carries ("⚠️ 2 has 3 blanks")."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if lines and lines[0].startswith(("❌", "⚠️")):
        return lines[0]
    for ln in lines[1:]:
        if _LATER_LINE.search(ln):
            return ln
    return ""


# -- filing ---------------------------------------------------------------------

def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")[:n] or "x"


def redact(url: str) -> str:
    """No query or fragment: one-time launch links carry credentials."""
    try:
        p = urlsplit(url or "")
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    except ValueError:
        return ""


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def report(source: str, *, chat: str = "", where: str = "", message: str = "",
           error=None, cdp_url: Optional[str] = None,
           files: Optional[dict] = None, extra: Optional[dict] = None,
           notify: bool = True, background: bool = True) -> Optional[Path]:
    """File one failure; returns its folder (None: off, a repeat, or broken).

    ``files``: scene files already in hand ({"page.png": bytes, ...}); else,
    with ``cdp_url``, the browser's open pages are captured. The capture and
    the owner's message run on a background thread — a failing step must not
    wait for them. ``error`` defaults to the exception being handled, if any
    (False: none)."""
    if not enabled():
        return None
    try:
        source = source if source in SOURCES else "script"
        if error is None:
            import sys
            error = sys.exc_info()[1]
        elif error is False:                     # explicitly none (not the one being handled)
            error = None
        first = problem_line(message) or _plain(message).strip().split("\n")[0]
        key = (source, str(chat), where, type(error).__name__ if error else "", _plain(first)[:80])
        now = time.time()
        with _lock:
            for k in [k for k, (t0, _) in _recent.items() if now - t0 > DEDUPE_SECONDS]:
                del _recent[k]
            if key in _recent:
                _recent[key][1] += 1
                log.info("trouble repeat (%d×): %s", _recent[key][1], key)
                return None
            _recent[key] = [now, 1]
        stamp = datetime.now()
        d = (root() / source / stamp.strftime("%Y-%m-%d")
             / f"{stamp.strftime('%H%M%S')}-{_slug(str(chat), 16)}-{_slug(where)}")
        d.mkdir(parents=True, exist_ok=True)
        event = {
            "time": stamp.isoformat(timespec="seconds"),
            "source": source,
            "chat": str(chat),
            "owner_chat": bool(_owner) and str(chat) == _owner,
            "where": where,
            "message": _plain(message)[:1500],       # the BOT's words, never the student's
            "error": (f"{type(error).__name__}: {error}"[:600] if error else ""),
            "traceback": ("".join(traceback.format_exception(type(error), error, error.__traceback__))[-4000:]
                          if error else ""),
            "extra": extra or {},
            "dir": str(d),
        }
        (d / "event.json").write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
        with (root() / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({k: event[k] for k in ("time", "source", "chat", "where", "error", "dir")}
                               | {"message": first[:200]}, ensure_ascii=False) + "\n")
        job = lambda: _finish(d, event, files, cdp_url, notify)   # noqa: E731
        if background:
            t = threading.Thread(target=job, name="trouble", daemon=True)
            _threads[:] = [x for x in _threads if x.is_alive()] + [t]
            t.start()
        else:
            job()
        return d
    except Exception:
        log.exception("trouble.report failed")
        return None


def _finish(d: Path, event: dict, files: Optional[dict], cdp_url: Optional[str], notify: bool) -> None:
    shot = None
    try:
        for name, data in (files or {}).items():
            p = d / name
            (p.write_bytes if isinstance(data, bytes) else p.write_text)(data)
            if shot is None and name.endswith(".png") and isinstance(data, bytes):
                shot = data
        if not files and cdp_url:
            tabs = capture(cdp_url, d)
            event["tabs"] = tabs
            (d / "event.json").write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
            p0 = d / "tab-0.png"
            shot = p0.read_bytes() if p0.exists() else None
    except Exception:
        log.exception("trouble capture failed")
    if not notify or _notify is None or event["owner_chat"]:
        return
    try:
        _notify(owner_text(event), shot)
    except Exception:
        log.exception("trouble notify failed")


def owner_text(event: dict) -> str:
    """The owner's message: Chinese, short, the bot's own words only."""
    from html import escape
    label = {"script": "脚本", "jev": "Jev"}.get(event["source"], event["source"])
    rel = event["dir"].split("/failures/", 1)[-1]
    lines = [f"🚨 <b>出问题了 · {label}</b>",
             f"谁：<code>{escape(event['chat'] or '?')}</code>",
             f"在哪：{escape(event['where'] or '?')}"]
    if event["message"]:
        lines.append("学生看到：" + escape(event["message"].split("\n")[0][:200]))
    if event["error"]:
        lines.append("错误：<code>" + escape(event["error"][:200]) + "</code>")
    ex = event.get("extra") or {}
    if event["source"] == "jev" and (ex.get("chose_label") or ex.get("error")):
        if ex.get("chose_label"):
            where_ = " · ".join(x for x in (ex.get("chose_context"), ex.get("chose_frame")) if x)
            lines.append(f"Jev 建议点：“{escape(ex['chose_label'][:60])}”"
                         + (f"（{escape(where_[:60])}）" if where_ else "")
                         + f" 置信度 {ex.get('confidence')}"
                         + ("" if ex.get("sure") else " ⚠️ 不够有把握") + " — 未执行")
        else:
            lines.append("Jev 没给出答案：" + escape(str(ex["error"])[:150]))
    tabs = event.get("tabs") or []
    if tabs:
        lines.append("页面：" + escape(", ".join(t.get("url", "") for t in tabs)[:200]))
    lines.append(f"现场：<code>failures/{escape(rel)}</code>")
    return "\n".join(lines)


# -- the scene: straight from the browser over CDP --------------------------------
#
# Not through Playwright: the failing step may hold that browser on another
# thread, and Playwright objects must stay on theirs. Raw CDP needs neither.

def _skip(url: str) -> bool:
    return not url or url.startswith(("about:", "chrome:", "devtools:", "chrome-extension:"))


def capture(cdp_url: str, into: Path, limit: int = MAX_TABS) -> list[dict]:
    """Screenshot + DOM of each open page (most recent first). Returns
    [{"url": redacted, "title": ...}] for what was saved."""
    from websockets.sync.client import connect
    with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=5) as r:
        targets = json.loads(r.read())
    pages = [t for t in targets if t.get("type") == "page" and not _skip(t.get("url", ""))][:limit]
    saved = []
    for i, t in enumerate(pages):
        info = {"url": redact(t.get("url", "")), "title": (t.get("title") or "")[:120]}
        try:
            with connect(t["webSocketDebuggerUrl"], open_timeout=5, close_timeout=2,
                         max_size=64 * 2**20) as ws:
                def call(n, method, **params):
                    ws.send(json.dumps({"id": n, "method": method, "params": params}))
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
                        if msg.get("id") == n:
                            return msg.get("result") or {}
                    return {}
                import base64
                png = call(1, "Page.captureScreenshot", format="png").get("data")
                if png:
                    (into / f"tab-{i}.png").write_bytes(base64.b64decode(png))
                html = (call(2, "Runtime.evaluate", expression="document.documentElement.outerHTML",
                             returnByValue=True).get("result") or {}).get("value")
                if html:
                    (into / f"tab-{i}.html").write_text(html, encoding="utf-8")
        except Exception as e:
            info["error"] = type(e).__name__
        saved.append(info)
    return saved


def telegram_notifier(tg, chat_id: str) -> Callable[[str, Optional[bytes]], None]:
    """The usual notifier: text, then the page picture if there is one
    (AINL_TROUBLE_PHOTO=0: text only — the picture stays in the folder)."""
    def send(text: str, png: Optional[bytes]) -> None:
        tg.send(chat_id, text)
        if png and os.environ.get("AINL_TROUBLE_PHOTO", "1") != "0":
            tg.send_photo(chat_id, png, filename="failure.png")
    return send
