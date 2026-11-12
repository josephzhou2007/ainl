"""The one-minute intro video, sent once to each person while they sign in.

Signing in (NetID, Duo, "that's me") takes about 20 seconds in the Canvas
login Mini App, and the chat sits unused behind it. So the moment the login
page opens, the video goes to the chat in the background; when they come
back to the chat it is there, just above "✅ Canvas connected… /todo".

    video       <AINL_HOME>/intro.mp4, or AINL_INTRO_VIDEO; missing = off
    file_id     <AINL_HOME>/intro_video.json. The first send uploads the file
                (~17 MB — the sign-in covers it); after that Telegram's
                file_id is sent, which is instant. Replacing the video file
                (a different size or mtime) uploads it again.
    sent        a marker in the person's own directory, so each person gets it
                once; /forget deletes that directory, and the marker with it.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("ainl.scaleup.intro")

CAPTION = ("▶️ <b>While you sign in</b> — here's what I can do for you, in under a minute.\n\n"
           "When you're connected, send /todo.")
MARKER = "intro-video-sent"


def _probe(path: Path) -> dict:
    """width/height/duration, so Telegram shows the right shape before it has
    processed the file. Best effort: no ffprobe, no numbers."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10).stdout
        d = json.loads(out)
        s = (d.get("streams") or [{}])[0]
        return {"width": int(s.get("width") or 0), "height": int(s.get("height") or 0),
                "duration": round(float((d.get("format") or {}).get("duration") or 0))}
    except Exception:
        return {}


class IntroVideo:
    def __init__(self, tg, user_dir: Callable[[str], Path], *, home: Path,
                 path: Optional[Path] = None,
                 background: Optional[Callable[[Callable[[], None]], None]] = None) -> None:
        self.tg, self.user_dir = tg, user_dir
        env = os.environ.get("AINL_INTRO_VIDEO", "").strip()
        self.path = Path(path or env or home / "intro.mp4").expanduser()
        self.cache_path = home / "intro_video.json"
        self.background = background or (lambda fn: threading.Thread(
            target=fn, name="intro-video", daemon=True).start())
        self._lock = threading.Lock()
        self._sending: set[str] = set()
        self._meta: Optional[dict] = None

    @property
    def enabled(self) -> bool:
        return self.path.is_file()

    def _source(self) -> str:
        st = self.path.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    def _cached_file_id(self) -> str:
        try:
            d = json.loads(self.cache_path.read_text())
            return d.get("file_id", "") if d.get("source") == self._source() else ""
        except (OSError, ValueError):
            return ""

    def _remember(self, file_id: str) -> None:
        try:
            self.cache_path.write_text(json.dumps({"file_id": file_id, "source": self._source(),
                                                   "saved": time.strftime("%Y-%m-%d %H:%M:%S")}))
        except OSError:
            log.warning("could not save the intro video's file_id")

    def sent(self, uid: str) -> bool:
        return (self.user_dir(uid) / MARKER).exists()

    def send_once(self, uid: str) -> None:
        """Start sending the video to ``uid`` unless they have had it. Returns
        at once: the upload runs in the background."""
        if not self.enabled:
            return
        with self._lock:
            if uid in self._sending or self.sent(uid):
                return
            self._sending.add(uid)
        self.background(lambda: self._send(uid))

    def _send(self, uid: str) -> None:
        try:
            if self._meta is None:
                self._meta = _probe(self.path)
            fid = self._cached_file_id()
            if fid:
                try:
                    self.tg.send_video(uid, file_id=fid, caption=CAPTION, **self._meta)
                except Exception as e:
                    # A file_id can go stale (another bot token, Telegram-side
                    # cleanup): upload the file instead of giving up.
                    log.info("intro file_id refused (%s); uploading", type(e).__name__)
                    fid = ""
            if not fid:
                t0 = time.monotonic()
                _, fid = self.tg.send_video(uid, path=self.path, caption=CAPTION, **self._meta)
                log.info("intro video uploaded in %.1fs", time.monotonic() - t0)
                if fid:
                    self._remember(fid)
            (self.user_dir(uid) / MARKER).write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            log.info("intro video sent to user %s", uid)
        except Exception as e:
            log.warning("intro video to user %s failed: %s", uid, type(e).__name__)
        finally:
            with self._lock:
                self._sending.discard(uid)
