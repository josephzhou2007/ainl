"""A written record of the whole conversation, for the developer.

Python logging says *that* a message went out; it never says *what*. When the
student reports "the bot answered the wrong thing" two days later, the only
way to know what happened is to have kept the messages. So every incoming
update (text, photo, document, button tap) and every outgoing message, photo,
album and button edit is appended as one JSON object per line to

    <AINL_HOME>/transcript.jsonl

and every image that passes through (student's photos, question screenshots,
filled/graded screenshots) is saved under <AINL_HOME>/photos/ and referenced
from the record by relative path.

Rules:
  * Never a token, never an Authorization header, never a URL with a token in
    it. Records carry chat ids, message text, captions, callback data, button
    labels and file paths — nothing else.
  * Writing can NEVER break the bot: every failure is logged and swallowed.
  * AINL_TRANSCRIPT=0 disables it entirely (tests set this by default).

Read it back with ``python -m ainl.telegram --transcript [N]``.

The transcript contains the student's homework. Do not share it.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("ainl.transcript")

FILE = "transcript.jsonl"
PHOTOS = "photos"
KINDS = ("text", "photo", "document", "callback", "album", "edit_markup", "edit_text", "other")


def _now() -> datetime:
    return datetime.now().astimezone()


def _stamp(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).strftime("%Y%m%d-%H%M%S")


def _no_query(url: Any) -> str:
    """A link button's address without its query: a one-time launch link
    (sessionless_launch) carries its credential there and must not be logged."""
    if not url:
        return ""
    from urllib.parse import urlsplit
    u = urlsplit(str(url))
    return f"{u.scheme}://{u.netloc}{u.path}" + ("?…" if u.query else "")


def compact_markup(reply_markup: Any) -> Optional[list]:
    """Inline keyboard -> [[["label", "callback_data"], …], …]. Anything else -> None.

    ``reply_markup`` may arrive as a dict or as the JSON string sendPhoto uses.
    """
    if not reply_markup:
        return None
    if isinstance(reply_markup, str):
        try:
            reply_markup = json.loads(reply_markup)
        except ValueError:
            return None
    rows = (reply_markup or {}).get("inline_keyboard") if isinstance(reply_markup, dict) else None
    if not rows:
        return None
    out = []
    for row in rows:
        out.append([[str(b.get("text", "")), str(b.get("callback_data") or _no_query(b.get("url")) or "")]
                    for b in row if isinstance(b, dict)])
    return out


class Transcript:
    """Appends records; saves images. ``enabled=False`` makes every method a no-op.

    ``root`` defaults to ``ainl.config.home()``, resolved lazily on first write
    so that importing / constructing never touches the disk.
    """

    def __init__(self, root: Optional[Path] = None, *, enabled: bool = True):
        self._root = Path(root) if root is not None else None
        self.enabled = enabled
        self._n = 0                       # outgoing image counter (unique names within a second)
        self._last_in: tuple[Optional[int], Optional[str]] = (None, None)  # (update_id, chat_id)
        self._warned = False

    @classmethod
    def from_env(cls) -> "Transcript":
        """Enabled unless AINL_TRANSCRIPT=0; root from AINL_HOME (via config.home())."""
        flag = os.environ.get("AINL_TRANSCRIPT", "1").strip().lower()
        return cls(enabled=flag not in ("0", "false", "no", "off"))

    @classmethod
    def disabled(cls) -> "Transcript":
        return cls(enabled=False)

    # -- paths ----------------------------------------------------------------

    @property
    def root(self) -> Path:
        if self._root is None:
            from ..config import home
            self._root = home()
        return self._root

    @property
    def path(self) -> Path:
        return self.root / FILE

    # -- writing --------------------------------------------------------------

    def _fail(self, what: str, e: Exception) -> None:
        # Loud once, quiet afterwards: an unwritable disk must not flood the log
        # with one warning per message.
        (log.debug if self._warned else log.warning)("transcript: %s failed: %s", what, e)
        self._warned = True

    def record(self, dir: str, chat_id: Any, kind: str, *, text: Optional[str] = None,
               file: Optional[str] = None, reply_markup: Any = None,
               update_id: Optional[int] = None, **extra: Any) -> Optional[dict]:
        """Append one line. Returns the record (or None when disabled/failed)."""
        if not self.enabled:
            return None
        try:
            rec: dict = {"ts": _now().isoformat(timespec="seconds"), "dir": dir,
                         "chat_id": str(chat_id) if chat_id is not None else None,
                         "kind": kind if kind in KINDS else "other"}
            if text is not None:
                rec["text"] = text
            if file:
                rec["file"] = file
            km = compact_markup(reply_markup)
            if km:
                rec["reply_markup"] = km
            if update_id is not None:
                rec["update_id"] = update_id
            for k, v in extra.items():
                if v is not None:
                    rec[k] = v
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec
        except Exception as e:                       # never let the record break the bot
            self._fail("write", e)
            return None

    def save_bytes(self, name: str, data: bytes) -> Optional[str]:
        """Save under photos/ and return the path relative to root (None on failure)."""
        if not self.enabled:
            return None
        try:
            d = self.root / PHOTOS
            d.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:120] or "file"
            (d / safe).write_bytes(data)
            return f"{PHOTOS}/{safe}"
        except Exception as e:
            self._fail("save", e)
            return None

    def _next_name(self, prefix: str, ext: str) -> str:
        self._n += 1
        return f"{prefix}-{_stamp()}-{self._n}{ext}"

    # -- incoming -------------------------------------------------------------

    def record_update(self, update: dict) -> Optional[dict]:
        """One record per incoming update, classified by its content.

        Photos are recorded with their Telegram file_id here; the bytes are
        saved when (if) the bot downloads them — see ``record_download`` — and
        that second record carries the same update_id, so the two join.
        """
        if not self.enabled:
            return None
        try:
            uid = update.get("update_id")
            cb = update.get("callback_query")
            if cb:
                m = cb.get("message") or {}
                chat = str((m.get("chat") or {}).get("id"))
                self._last_in = (uid, chat)
                return self.record("in", chat, "callback", text=cb.get("data"), update_id=uid,
                                   message_id=m.get("message_id"))
            m = update.get("message") or update.get("edited_message") or {}
            chat = str((m.get("chat") or {}).get("id"))
            self._last_in = (uid, chat)
            extra: dict = {"message_id": m.get("message_id")}
            if m.get("photo"):
                best = max(m["photo"], key=lambda p: p.get("file_size") or 0)
                extra["file_id"] = best.get("file_id")
                return self.record("in", chat, "photo", text=m.get("caption"), update_id=uid, **extra)
            if m.get("document"):
                doc = m["document"]
                extra["file_id"] = doc.get("file_id")
                extra["file_name"] = doc.get("file_name")
                return self.record("in", chat, "document", text=m.get("caption"), update_id=uid, **extra)
            if m.get("text") is not None:
                return self.record("in", chat, "text", text=m.get("text"), update_id=uid, **extra)
            keys = sorted(k for k in m if k not in ("message_id", "chat", "from", "date"))
            return self.record("in", chat, "other", text=",".join(keys) or None, update_id=uid, **extra)
        except Exception as e:
            self._fail("record_update", e)
            return None

    def record_download(self, file_id: str, tg_path: str, data: bytes) -> Optional[str]:
        """Save what TelegramClient.download fetched; record it against the
        update being handled. Returns the saved relative path."""
        if not self.enabled:
            return None
        try:
            ext = Path(tg_path).suffix.lower() or ".bin"
            uid, chat = self._last_in
            tag = str(uid) if uid is not None else (file_id or "file")[:12]
            rel = self.save_bytes(f"{_stamp()}-{tag}{ext}", data)
            kind = "photo" if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic") else "document"
            self.record("in", chat, kind, text=f"downloaded {tg_path}", file=rel, update_id=uid,
                        file_id=file_id, bytes=len(data))
            return rel
        except Exception as e:
            self._fail("record_download", e)
            return None

    # -- outgoing -------------------------------------------------------------

    def out_text(self, chat_id: Any, text: str, *, reply_markup: Any = None,
                 message_id: Optional[int] = None) -> None:
        self.record("out", chat_id, "text", text=text, reply_markup=reply_markup, message_id=message_id)

    def out_photo(self, chat_id: Any, data: bytes, *, caption: str = "", reply_markup: Any = None,
                  message_id: Optional[int] = None, ext: str = ".png") -> None:
        if not self.enabled:
            return
        rel = self.save_bytes(self._next_name("out", ext), data)
        self.record("out", chat_id, "photo", text=caption or None, file=rel,
                    reply_markup=reply_markup, message_id=message_id)

    def out_document(self, chat_id: Any, filename: str, data: bytes, *, caption: str = "",
                     message_id: Optional[int] = None) -> None:
        if not self.enabled:
            return
        rel = self.save_bytes(self._next_name("out", "-" + (filename or "file")), data)
        self.record("out", chat_id, "document", text=caption or None, file=rel,
                    message_id=message_id, file_name=filename)

    def out_album(self, chat_id: Any, images: list[tuple[bytes, str]],
                  message_ids: Optional[list[int]] = None) -> None:
        if not self.enabled:
            return
        files = [self.save_bytes(self._next_name("out", ".png"), data) for data, _ in images]
        captions = [c for _, c in images if c]
        self.record("out", chat_id, "album", text="\n".join(captions) or None,
                    file=files[0] if files else None, files=files, message_ids=message_ids)

    def out_edit_markup(self, chat_id: Any, message_id: int, reply_markup: Any) -> None:
        self.record("out", chat_id, "edit_markup", reply_markup=reply_markup, message_id=message_id)

    def out_edit_text(self, chat_id: Any, message_id: int, text: str, reply_markup: Any = None) -> None:
        self.record("out", chat_id, "edit_text", text=text, reply_markup=reply_markup, message_id=message_id)

    # -- reading --------------------------------------------------------------

    def read(self) -> Iterator[dict]:
        """Every parseable record, oldest first. A missing file yields nothing."""
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except FileNotFoundError:
            return

    def tail(self, n: int = 40) -> list[dict]:
        recs = list(self.read())
        return recs[-n:] if n > 0 else recs

    def since(self, when: datetime | str) -> list[dict]:
        if isinstance(when, str):
            when = datetime.fromisoformat(when)
        if when.tzinfo is None:
            when = when.astimezone()                 # naive = local time
        out = []
        for r in self.read():
            try:
                ts = datetime.fromisoformat(r["ts"])
            except (KeyError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.astimezone()
            if ts >= when:
                out.append(r)
        return out


def format_record(r: dict, width: int = 300) -> str:
    """One line per record: time, arrow, kind, text, file, buttons."""
    ts = str(r.get("ts", ""))[:19].replace("T", " ")
    arrow = "→" if r.get("dir") == "out" else "←"
    parts = [ts, arrow, f"{r.get('kind', '?'):<11}"]
    text = r.get("text") or r.get("file_name")           # a document with no caption: show its name
    if text:
        t = " ".join(str(text).split())
        parts.append(repr(t[:width] + ("…" if len(t) > width else "")))
    files = r.get("files") or ([r["file"]] if r.get("file") else [])
    if files:
        parts.append("[" + ", ".join(str(f) for f in files) + "]")
    km = r.get("reply_markup")
    if km:
        parts.append("{" + " | ".join(" ".join(f"{t}({d})" if d and d != t else t for t, d in row)
                                      for row in km) + "}")
    return " ".join(parts)
