"""Minimal Telegram Bot API client.

Outbound: sendMessage (HTML, auto-split, inline keyboards), sendDocument.
Inbound:  getUpdates long-polling, getFile + download, answerCallbackQuery.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Optional

import httpx

from .transcript import Transcript

MAX_LEN = 4096          # Telegram hard limit per message


def esc(s: str) -> str:
    """Escape text for parse_mode=HTML."""
    return html.escape(s, quote=False)


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split on line boundaries so no chunk exceeds the limit."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """[[("label", "callback_data"), …], …] -> reply_markup dict."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, bot_token: str, *, transport: Optional[httpx.BaseTransport] = None,
                 transcript: Optional[Transcript] = None):
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._file_base = f"https://api.telegram.org/file/bot{bot_token}"
        from ..util import metrics
        self._client = httpx.Client(timeout=60.0, transport=transport, event_hooks=metrics.hooks())
        # Everything user-visible that goes through this client is written to
        # <AINL_HOME>/transcript.jsonl (see transcript.py). Pass Transcript.disabled()
        # or set AINL_TRANSCRIPT=0 to turn it off. Never raises.
        self.transcript = transcript if transcript is not None else Transcript.from_env()

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, **payload) -> Any:
        r = self._client.post(f"{self._base}/{method}", json=payload)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok"):
            raise TelegramError(f"{method}: {r.status_code} {data.get('description') or r.text[:200]}")
        return data["result"]

    # -- outbound -------------------------------------------------------------

    def send(self, chat_id: str, text: str, *, thread_id: Optional[int] = None,
             reply_markup: Optional[dict] = None) -> list[int]:
        """Send HTML text; splits automatically (keyboard goes on the last chunk)."""
        ids = []
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            payload: dict = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                             "disable_web_page_preview": True}
            if thread_id is not None:
                payload["message_thread_id"] = thread_id
            if reply_markup and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            ids.append(int(self._call("sendMessage", **payload)["message_id"]))
            self.transcript.out_text(chat_id, chunk, reply_markup=payload.get("reply_markup"),
                                     message_id=ids[-1])
        return ids

    def send_document(self, chat_id: str, filename: str, data: bytes, *, caption: str = "") -> int:
        r = self._client.post(
            f"{self._base}/sendDocument",
            data={"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
            files={"document": (filename, data)},
        )
        out = r.json()
        if r.status_code != 200 or not out.get("ok"):
            raise TelegramError(f"sendDocument: {r.status_code} {out.get('description')}")
        mid = int(out["result"]["message_id"])
        self.transcript.out_document(chat_id, filename, data, caption=caption[:1024], message_id=mid)
        return mid

    def send_photo(self, chat_id: str, data: bytes, *, caption: str = "",
                   filename: str = "image.png", reply_markup: Optional[dict] = None) -> int:
        """Photo, not document: it renders inline, which is the point — the
        student has to be able to read a question or check a filled answer
        without downloading anything."""
        payload = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        r = self._client.post(f"{self._base}/sendPhoto", data=payload,
                              files={"photo": (filename, data)})
        out = r.json()
        if r.status_code != 200 or not out.get("ok"):
            raise TelegramError(f"sendPhoto: {r.status_code} {out.get('description')}")
        mid = int(out["result"]["message_id"])
        self.transcript.out_photo(chat_id, data, caption=caption[:1024], reply_markup=reply_markup,
                                  message_id=mid)
        return mid

    def edit_photo(self, chat_id: str, message_id: int, data: bytes, *, caption: str = "",
                   filename: str = "image.png") -> None:
        """Replace the picture of a photo already sent (one in an album too):
        the blank question becomes the filled one where it is, instead of a
        second set of pictures."""
        media = {"type": "photo", "media": "attach://p", "caption": caption[:1024], "parse_mode": "HTML"}
        r = self._client.post(f"{self._base}/editMessageMedia",
                              data={"chat_id": chat_id, "message_id": str(message_id),
                                    "media": json.dumps(media)},
                              files={"p": (filename, data)})
        out = r.json()
        if r.status_code != 200 or not out.get("ok"):
            raise TelegramError(f"editMessageMedia: {r.status_code} {out.get('description')}")
        self.transcript.out_photo(chat_id, data, caption=f"(edited) {caption[:1000]}",
                                  message_id=message_id)

    def send_video(self, chat_id: str, *, path: Optional[Path] = None, file_id: str = "",
                   caption: str = "", width: int = 0, height: int = 0,
                   duration: int = 0) -> tuple[int, str]:
        """A playable video, from a local file (uploaded) or a ``file_id``
        Telegram already has (instant). Returns (message_id, file_id): keep the
        file_id and the next send of the same video needs no upload."""
        data: dict = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML",
                      "supports_streaming": "true"}
        for k, v in (("width", width), ("height", height), ("duration", duration)):
            if v:
                data[k] = str(v)
        if file_id:
            r = self._client.post(f"{self._base}/sendVideo", data={**data, "video": file_id})
        elif path is not None:
            with open(path, "rb") as fh:
                # A 20 MB upload can outlast the client's usual 60 s on a slow line.
                r = self._client.post(f"{self._base}/sendVideo", data=data,
                                      files={"video": (path.name, fh, "video/mp4")},
                                      timeout=300.0)
        else:
            raise ValueError("send_video needs a path or a file_id")
        out = r.json()
        if r.status_code != 200 or not out.get("ok"):
            raise TelegramError(f"sendVideo: {r.status_code} {out.get('description')}")
        res = out["result"]
        mid = int(res["message_id"])
        fid = ((res.get("video") or res.get("animation") or res.get("document") or {})
               .get("file_id") or file_id)
        self.transcript.record("out", chat_id, "video", text=caption[:1024] or None,
                               message_id=mid, file_id=fid)
        return mid, fid

    def send_media_group(self, chat_id: str, images: list[tuple[bytes, str]]) -> list[int]:
        """Up to 10 photos in ONE request, as an album.

        Sending an eight-question assignment one photo at a time cost eight
        round trips — about as long as reading the questions off the page in
        the first place. Telegram caps a group at 10; callers send in batches.

        ``images`` is [(png_bytes, caption), …]; only the first caption shows
        on the album, so the question number is drawn from the order.
        """
        if not images:
            return []
        media, files = [], {}
        for i, (data, caption) in enumerate(images[:10]):
            name = f"p{i}"
            files[name] = (f"{name}.png", data)
            item = {"type": "photo", "media": f"attach://{name}"}
            if caption:
                item["caption"], item["parse_mode"] = caption[:1024], "HTML"
            media.append(item)
        r = self._client.post(f"{self._base}/sendMediaGroup",
                              data={"chat_id": chat_id, "media": json.dumps(media)},
                              files=files)
        out = r.json()
        if r.status_code != 200 or not out.get("ok"):
            raise TelegramError(f"sendMediaGroup: {r.status_code} {out.get('description')}")
        ids = [int(m["message_id"]) for m in out["result"]]
        self.transcript.out_album(chat_id, list(images[:10]), message_ids=ids)
        return ids

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """Register the slash menu. Without this Telegram shows nothing when
        the student types "/", so every command has to be memorised."""
        self._call("setMyCommands", commands=[{"command": c, "description": d}
                                              for c, d in commands])

    def edit_markup(self, chat_id: str, message_id: int, reply_markup: dict) -> None:
        """Redraw the buttons under a message already sent (e.g. tick the chosen one)."""
        self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup=reply_markup)
        self.transcript.out_edit_markup(chat_id, message_id, reply_markup)

    def edit_text(self, chat_id: str, message_id: int, text: str,
                  reply_markup: Optional[dict] = None) -> None:
        """Rewrite a message already sent — one message that updates, not a new one each time."""
        self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                   parse_mode="HTML", disable_web_page_preview=True,
                   **({"reply_markup": reply_markup} if reply_markup else {}))
        self.transcript.out_edit_text(chat_id, message_id, text, reply_markup)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])
        if text:                                     # a toast the student actually saw
            self.transcript.record("out", self.transcript._last_in[1], "other", text=text[:200],
                                   callback_id=callback_id)

    # -- inbound --------------------------------------------------------------

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> list[dict]:
        payload: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", **payload)

    def download(self, file_id: str) -> tuple[str, bytes]:
        """Returns (file_path_on_telegram, bytes). Bot API caps downloads at 20 MB."""
        info = self._call("getFile", file_id=file_id)
        path = info["file_path"]
        r = self._client.get(f"{self._file_base}/{path}")
        if r.status_code != 200:
            raise TelegramError(f"download {path}: {r.status_code}")
        # Saved once here, whoever asked for it: <AINL_HOME>/photos/<ts>-<update_id>.<ext>
        self.transcript.record_download(file_id, path, r.content)
        return path, r.content

    def discover_chats(self) -> list[dict]:
        """Chats that have messaged the bot recently (for finding your chat_id)."""
        seen: dict[str, dict] = {}
        for u in self._call("getUpdates", timeout=0, allowed_updates=["message"]):
            m = u.get("message") or {}
            c = m.get("chat") or {}
            if c.get("id") is not None:
                seen[str(c["id"])] = {
                    "chat_id": str(c["id"]), "type": c.get("type"),
                    "name": c.get("title") or " ".join(filter(None, [c.get("first_name"), c.get("last_name")])),
                    "username": c.get("username"),
                }
        return list(seen.values())
