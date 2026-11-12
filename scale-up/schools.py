"""Which Canvas each person uses: their school.

Every school runs its own Canvas and its own single sign-on, so "Canvas login"
has to know where to start, where the login may wander, and which Canvas to
ask "who is this?". A person picks their school once in the bot; everything
after that (the login page, the identity check, the saved session, /todo)
uses that school's Canvas.

The list lives in SQLite (schooldb.py, ``<AINL_HOME>/ainl.db``) because it
grows while the bot runs: people find their school by name in Instructure's
public directory, or paste their Canvas address, and detect.py works out the
rest. The two schools below are the seed — checked by hand, keys ``uw`` and
``ucsb`` kept so existing registry entries keep pointing at them.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

# Shared by every school: Duo, and Instructure's own SSO hops.
_COMMON = ("duosecurity.com", "duo.com", "instructure.com", "canvaslms.com")


@dataclass(frozen=True)
class School:
    key: str
    name: str                    # shown on the buttons and in messages
    canvas_origin: str           # https://host, no trailing slash
    start_path: str = "/"        # where the login tab starts (straight into SSO if possible)
    id_name: str = "NetID"       # what they sign in with, for the instructions
    domains: tuple[str, ...] = ()
    status: str = "verified"     # verified | unverified (added from a pasted address)
    source: str = "seed"         # seed | instructure | user_url

    @property
    def start_url(self) -> str:
        return self.canvas_origin + self.start_path

    @property
    def allowed_suffixes(self) -> tuple[str, ...]:
        return self.domains + _COMMON


SEEDS: dict[str, School] = {s.key: s for s in (
    # canvas.wisc.edu -> sso.canvaslms.com -> login.wisc.edu -> Duo (seen 2026-09-26)
    School("uw", "UW–Madison", "https://canvas.wisc.edu", "/", "NetID", ("wisc.edu",)),
    # ucsb.instructure.com/login sends people to a landing page on
    # www.canvas.ucsb.edu; /login/saml is its "UCSB NetID" button.
    School("ucsb", "UC Santa Barbara", "https://ucsb.instructure.com", "/login/saml",
           "UCSB NetID", ("ucsb.edu",)),
)}
# Other names people type for the seeds (the local search matches these too).
SEED_ALIASES = {"uw": "University of Wisconsin-Madison Wisconsin Madison UW wisc",
                "ucsb": "University of California Santa Barbara UCSB"}
SCHOOLS = SEEDS                  # the old name; the whole list is all()

# Everyone who connected before there was a choice did so on UW's Canvas.
LEGACY = "uw"

_db = None
_db_lock = threading.Lock()


def db():
    """The process-wide school database (``<AINL_HOME>/ainl.db``), opened on
    first use. Tests call use() with one in a tmp dir first."""
    global _db
    with _db_lock:
        if _db is None:
            from schooldb import SchoolDB
            _db = SchoolDB()
        return _db


def use(database) -> None:
    """Point get()/by_origin()/all() at this SchoolDB (tests, or another home)."""
    global _db
    with _db_lock:
        _db = database


def get(key: Optional[str]) -> Optional[School]:
    return db().get(key) if key else None


def by_origin(origin: str) -> Optional[School]:
    return db().by_origin(origin)


def all() -> list[School]:          # noqa: A001 — schools.all(), like reg.all()
    return db().all()


class SchoolSessions:
    """Cookie Management's SessionManager, one per school, picked per person.

    A SessionManager is bound to one Canvas origin (its probe, its cookie
    filter and the snapshot envelope all use it), so each school gets its own,
    sharing the same browser pool and encrypted store. Schools are added while
    the bot runs, so a manager is made on first use and cached by origin.
    ``school_of(uid)`` decides; a person without a school has nothing to
    inspect or restore.
    """

    def __init__(self, registry, pool, key: bytes, school_of) -> None:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Cookie Management"))
        from cookie_management.sessions import PooledBrowser, SessionManager
        from cookie_management.store import SnapshotStore

        self._registry, self._make = registry, SessionManager
        self._browser, self._store = PooledBrowser(pool), SnapshotStore(registry, key)
        self._by_origin: dict[str, object] = {}
        self._lock = threading.Lock()
        self._school_of = school_of

    def manager(self, uid: str):
        school = self._school_of(uid)
        if school is None:
            raise LookupError("no school chosen")
        with self._lock:
            m = self._by_origin.get(school.canvas_origin)
            if m is None:
                m = self._by_origin[school.canvas_origin] = self._make(
                    self._registry, self._browser, self._store, school.canvas_origin)
            return m

    def origin(self, uid: str) -> str:
        return self.manager(uid).origin

    def inspect(self, uid):
        return self.manager(uid).inspect(uid)

    def complete_login(self, uid, canvas_id):
        return self.manager(uid).complete_login(uid, canvas_id)

    def heartbeat(self, uid):
        return self.manager(uid).heartbeat(uid)

    def restore(self, uid):
        return self.manager(uid).restore(uid)
