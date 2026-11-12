"""The school list, and the little per-person state around finding one, in SQLite.

``<AINL_HOME>/ainl.db`` (the base dir is injectable, like Registry, so tests
use a tmp_path). Tables:

    schools            one row per Canvas: key (slug, what registry.json stores),
                       name, canvas_origin (the API host), start_path, id_name,
                       login_domains (JSON list; the allowed_suffixes), aliases
                       (extra words the local search matches), status
                       (verified | unverified), source (seed | instructure |
                       user_url), added_by (chat id), created_at, verified_at
    school_hosts       every host a school's login tab actually visited, with a
                       count — its real SSO chain, learned from logins
    school_search      who is being asked "type your school's name" (or "send
                       your Canvas address"), so that free text goes to the
                       search and nowhere else
    school_candidates  directory results shown as buttons: a Telegram callback
                       holds 64 bytes, so the button carries this row's id,
                       never a URL

The bot touches this from the getUpdates thread, the viewer's event loop and
each person's worker, so every call opens its own connection under one lock:
the rows are few and the calls rare, and it keeps sqlite3's thread rules out
of the picture.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from ainl.config import home

import schools
from schools import School

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schools (
    key            TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    canvas_origin  TEXT NOT NULL UNIQUE,
    start_path     TEXT NOT NULL DEFAULT '/',
    id_name        TEXT NOT NULL DEFAULT 'school account',
    login_domains  TEXT NOT NULL DEFAULT '[]',
    aliases        TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL CHECK (status IN ('verified', 'unverified')),
    source         TEXT NOT NULL CHECK (source IN ('seed', 'instructure', 'user_url')),
    added_by       TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    verified_at    TEXT
);
CREATE TABLE IF NOT EXISTS school_hosts (
    school      TEXT NOT NULL,
    host        TEXT NOT NULL,
    visits      INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (school, host)
);
CREATE TABLE IF NOT EXISTS school_search (
    chat_id  TEXT PRIMARY KEY,
    mode     TEXT NOT NULL CHECK (mode IN ('name', 'url')),
    since    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS school_candidates (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   TEXT NOT NULL,
    name      TEXT NOT NULL,
    domain    TEXT NOT NULL,
    provider  TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL
);
"""

# "Type your school's name" is forgotten after this: a message sent tomorrow
# is a message, not a search.
SEARCH_TTL_S = 15 * 60
_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug_for(origin: str) -> str:
    """A short key from a Canvas host: ucsb.instructure.com -> "ucsb",
    bruinlearn.ucla.edu -> "bruinlearn-ucla". At most 32 characters, so
    ``school:<key>`` stays far below Telegram's 64-byte callback limit."""
    host = (urlsplit(origin).hostname or origin).lower()
    for tail in (".instructure.com", ".edu", ".org", ".com", ".net"):
        if host.endswith(tail) and host != tail.lstrip("."):
            host = host[: -len(tail)]
            break
    s = re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:32].strip("-")
    return s or "school"


class SchoolDB:
    def __init__(self, base: Optional[Path] = None) -> None:
        self._base = Path(base) if base is not None else home()
        self._base.mkdir(parents=True, exist_ok=True)
        self.path = self._base / "ainl.db"
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
            for s in schools.SEEDS.values():
                # OR IGNORE: the seed never overwrites what the owner changed.
                c.execute("INSERT OR IGNORE INTO schools (key, name, canvas_origin, start_path, "
                          "id_name, login_domains, aliases, status, source, created_at, verified_at) "
                          "VALUES (?,?,?,?,?,?,?,'verified','seed',?,?)",
                          (s.key, s.name, s.canvas_origin, s.start_path, s.id_name,
                           json.dumps(list(s.domains)), schools.SEED_ALIASES.get(s.key, ""),
                           _now(), _now()))

    @contextmanager
    def _conn(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=10)
            c.row_factory = sqlite3.Row
            try:
                with c:                        # commit on success, roll back on error
                    yield c
            finally:
                c.close()

    # -- schools ---------------------------------------------------------------

    @staticmethod
    def _school(r) -> School:
        try:
            domains = tuple(d for d in json.loads(r["login_domains"] or "[]") if isinstance(d, str))
        except ValueError:
            domains = ()
        return School(r["key"], r["name"], r["canvas_origin"], r["start_path"], r["id_name"],
                      domains, r["status"], r["source"])

    def get(self, key: Optional[str]) -> Optional[School]:
        if not key:
            return None
        with self._conn() as c:
            r = c.execute("SELECT * FROM schools WHERE key = ?", (key,)).fetchone()
        return self._school(r) if r else None

    def by_origin(self, origin: str) -> Optional[School]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM schools WHERE canvas_origin = ?",
                          (origin.rstrip("/").lower(),)).fetchone()
        return self._school(r) if r else None

    def all(self) -> list[School]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM schools ORDER BY rowid").fetchall()
        return [self._school(r) for r in rows]

    def row(self, key: str) -> Optional[dict]:
        """Everything stored about a school (for the owner's views)."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM schools WHERE key = ?", (key,)).fetchone()
        return dict(r) if r else None

    def search(self, text: str, limit: int = 5) -> list[School]:
        """Schools already here whose name, aliases or host contain every word typed."""
        words = [w for w in re.split(r"[\s,]+", text.lower()) if w]
        if not words:
            return []
        with self._conn() as c:
            rows = c.execute("SELECT * FROM schools ORDER BY rowid").fetchall()
        out = []
        for r in rows:
            hay = " ".join((r["name"], r["aliases"] or "",
                            urlsplit(r["canvas_origin"]).hostname or "")).lower().replace("–", "-")
            if all(w.replace("–", "-") in hay for w in words):
                out.append(self._school(r))
        # Verified first: a hand-checked school beats one somebody pasted.
        out.sort(key=lambda s: s.status != "verified")
        return out[:limit]

    def add(self, *, name: str, canvas_origin: str, start_path: str = "/",
            id_name: str = "school account", domains: tuple[str, ...] = (),
            status: str = "unverified", source: str = "user_url", added_by: str = "",
            aliases: str = "") -> School:
        """Add a school, or return the one already on this origin (never two
        rows for one Canvas: the key is what people's records point at)."""
        origin = canvas_origin.rstrip("/").lower()
        with self._conn() as c:
            r = c.execute("SELECT * FROM schools WHERE canvas_origin = ?", (origin,)).fetchone()
            if r:
                return self._school(r)
            base = slug_for(origin)
            key, n = base, 2
            while c.execute("SELECT 1 FROM schools WHERE key = ?", (key,)).fetchone():
                key, n = f"{base[:29]}-{n}", n + 1
            c.execute("INSERT INTO schools (key, name, canvas_origin, start_path, id_name, "
                      "login_domains, aliases, status, source, added_by, created_at, verified_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (key, name.strip()[:120] or origin, origin, start_path or "/",
                       id_name, json.dumps(list(domains)), aliases, status, source,
                       str(added_by or ""), _now(), _now() if status == "verified" else None))
            r = c.execute("SELECT * FROM schools WHERE key = ?", (key,)).fetchone()
        return self._school(r)

    def set_status(self, key: str, status: str) -> bool:
        with self._conn() as c:
            n = c.execute("UPDATE schools SET status = ?, verified_at = ? WHERE key = ?",
                          (status, _now() if status == "verified" else None, key)).rowcount
        return n > 0

    # -- the login chains they really take --------------------------------------

    def learn_host(self, key: str, host: str) -> None:
        """Count one visit of the login tab to ``host`` for this school."""
        host = (host or "").lower().rstrip(".")
        if not key or not _HOST.match(host):
            return
        now = _now()
        with self._conn() as c:
            c.execute("INSERT INTO school_hosts (school, host, visits, first_seen, last_seen) "
                      "VALUES (?,?,1,?,?) ON CONFLICT(school, host) DO UPDATE SET "
                      "visits = visits + 1, last_seen = excluded.last_seen",
                      (key, host, now, now))

    def hosts(self, key: str) -> list[tuple[str, int]]:
        with self._conn() as c:
            rows = c.execute("SELECT host, visits FROM school_hosts WHERE school = ? "
                             "ORDER BY visits DESC, host", (key,)).fetchall()
        return [(r["host"], r["visits"]) for r in rows]

    # -- "type your school's name" ----------------------------------------------

    def set_pending(self, chat_id: str, mode: str = "name") -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO school_search (chat_id, mode, since) VALUES (?,?,?)",
                      (str(chat_id), mode, time.time()))

    def pending(self, chat_id: str) -> Optional[str]:
        """"name" / "url" while they are being asked; None otherwise (or after the TTL)."""
        with self._conn() as c:
            r = c.execute("SELECT mode, since FROM school_search WHERE chat_id = ?",
                          (str(chat_id),)).fetchone()
        if r is None or time.time() - r["since"] > SEARCH_TTL_S:
            return None
        return r["mode"]

    def clear_pending(self, chat_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM school_search WHERE chat_id = ?", (str(chat_id),))

    def put_candidate(self, chat_id: str, name: str, domain: str, provider: str = "") -> int:
        with self._conn() as c:
            # Old buttons of theirs stop working after a day; keeps the table small.
            c.execute("DELETE FROM school_candidates WHERE created < ?", (time.time() - 86400,))
            cur = c.execute("INSERT INTO school_candidates (chat_id, name, domain, provider, created) "
                            "VALUES (?,?,?,?,?)", (str(chat_id), name[:120], domain, provider or "",
                                                   time.time()))
            return int(cur.lastrowid)

    def candidate(self, cand_id: int, chat_id: str) -> Optional[dict]:
        """The directory entry behind a button — only for the chat it was shown in."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM school_candidates WHERE id = ? AND chat_id = ?",
                          (int(cand_id), str(chat_id))).fetchone()
        return dict(r) if r else None

    def forget(self, chat_id: str) -> None:
        """/forget: nothing about them stays here either (schools they added stay)."""
        with self._conn() as c:
            c.execute("DELETE FROM school_search WHERE chat_id = ?", (str(chat_id),))
            c.execute("DELETE FROM school_candidates WHERE chat_id = ?", (str(chat_id),))
