"""Who the bot serves, and where each person's data lives.

One JSON file, ``<home>/users/registry.json``, holds an identity + approval
record per Telegram chat. NO secrets live here: cookies, tokens and passwords
are a separate, later concern — this file only answers "who is this, are they
allowed, and which directory is theirs". Losing it costs nothing but the
approvals; it never holds anything a leak could abuse.

Load/save is tolerant the same way BotState is (src/ainl/bot/state.py): a
missing or corrupt file reads back as an empty registry rather than crashing
the bot on start.

The base directory is injectable (defaults to config.home()) so tests point it
at a tmp_path without touching the real ~/.ainl or setting global env.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ainl.config import home

# A chat_id becomes a single path segment (the user's directory name), so it
# must be exactly that — no separators, no "..", nothing that could climb out
# of the users dir. Telegram ids are digits, optionally leading "-" for group
# chats; underscores are allowed for headroom. Anything else is rejected.
_SAFE_ID = re.compile(r"^-?[A-Za-z0-9_]+$")
_SAVE_LOCK = threading.Lock()


def _sanitize(chat_id: str) -> str:
    s = str(chat_id).strip()
    if not _SAFE_ID.match(s):
        raise ValueError(f"unsafe chat_id for a path segment: {chat_id!r}")
    return s


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def user_dir(chat_id: str, base: Optional[Path] = None) -> Path:
    """The directory that belongs to this user, created on demand.

    ``<base>/users/<sanitized chat_id>``. Base defaults to config.home(); tests
    pass a tmp_path. Raises ValueError on a chat_id that isn't a safe segment.
    """
    root = Path(base) if base is not None else home()
    d = root / "users" / _sanitize(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class User:
    chat_id: str
    name: str
    login_id: str          # their Canvas/institution login, for display only
    approved: bool
    created_at: str        # ISO 8601, UTC
    school: str = ""       # a key in the schools table (schooldb.py); "" = not chosen yet


class Registry:
    """Per-user records backed by a single JSON file.

    Held in memory and rewritten on every mutation — the set is tiny (a
    classroom), so simplicity beats cleverness here.
    """

    def __init__(self, base: Optional[Path] = None) -> None:
        self._base = Path(base) if base is not None else home()
        self._dir = self._base / "users"
        self._path = self._dir / "registry.json"
        self._users: dict[str, User] = self._load()

    # -- paths ---------------------------------------------------------------

    def user_dir(self, chat_id: str) -> Path:
        return user_dir(chat_id, self._base)

    @property
    def path(self) -> Path:
        return self._path

    # -- persistence (tolerant, like BotState.load) --------------------------

    def _load(self) -> dict[str, User]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Corrupt or unreadable: start empty rather than crash the bot.
            return {}
        out: dict[str, User] = {}
        for cid, u in (raw.get("users") or {}).items():
            try:
                out[cid] = User(
                    chat_id=u["chat_id"],
                    name=u.get("name") or "",
                    login_id=u.get("login_id") or "",
                    approved=bool(u.get("approved")),
                    created_at=u.get("created_at") or "",
                    school=u.get("school") or "",
                )
            except (KeyError, TypeError):
                continue          # skip a mangled record, keep the rest
        return out

    def _save(self) -> None:
        # A school pick is saved from a background thread (onboarding checks
        # the school's site off the update loop): copy the dict before walking
        # it, and write under a lock so two saves never interleave.
        with _SAVE_LOCK:
            raw = {"users": {cid: asdict(u) for cid, u in dict(self._users).items()}}
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    # -- records -------------------------------------------------------------

    def add(self, chat_id: str, name: str, login_id: str) -> User:
        """Register a user, UNAPPROVED. Sanitizes chat_id and makes their dir."""
        cid = _sanitize(chat_id)
        u = User(chat_id=cid, name=name, login_id=login_id,
                 approved=False, created_at=_now())
        self._users[cid] = u
        self.user_dir(cid)        # create the dir up front
        self._save()
        return u

    def approve(self, chat_id: str) -> bool:
        cid = _sanitize(chat_id)
        u = self._users.get(cid)
        if u is None:
            return False
        u.approved = True
        self._save()
        return True

    def set_school(self, chat_id: str, school: str) -> bool:
        u = self._users.get(_sanitize(chat_id))
        if u is None:
            return False
        u.school = school
        self._save()
        return True

    def remove(self, chat_id: str) -> bool:
        """Forget the user and delete their directory tree."""
        cid = _sanitize(chat_id)
        existed = self._users.pop(cid, None) is not None
        d = self._dir / cid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        if existed:
            self._save()
        return existed

    def get(self, chat_id: str) -> Optional[User]:
        return self._users.get(_sanitize(chat_id))

    def all(self) -> list[User]:
        return list(self._users.values())

    def is_approved(self, chat_id: str) -> bool:
        u = self.get(chat_id)
        return bool(u and u.approved)
