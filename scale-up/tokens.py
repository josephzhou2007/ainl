"""The bot's own credential for each person: issued once, stored hashed.

Whatever route brings a classmate's Canvas session to the server later — a
browser extension posting cookies, a remote-browser link — something has to
say WHICH registered person is on the other end. This is that something: a
random token the bot hands them on approval. It is ours, not the school's;
it authenticates the person to this bot and nothing else.

Bearer secrets are stored as a hash (like a password), so a copy of the
users directory does not yield working tokens. The plaintext exists exactly
once, in the message that delivers it; ``issue()`` returns it and forgets it.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from registry import Registry

_FILE = "app-token.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(reg: Registry, chat_id: str) -> str:
    """Mint a fresh token for this person, replacing any earlier one.

    Returns the plaintext — deliver it to them and drop it. Anything issued
    before stops working: ``/token`` is also how someone revokes a leaked one.
    """
    tok = secrets.token_urlsafe(32)
    (reg.user_dir(chat_id) / _FILE).write_text(json.dumps({
        "sha256": _digest(tok),
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }), encoding="utf-8")
    return tok


def revoke(reg: Registry, chat_id: str) -> bool:
    p = reg.user_dir(chat_id) / _FILE
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def verify(reg: Registry, token: str) -> Optional[str]:
    """chat_id of the APPROVED person this token belongs to, or None.

    A classroom is a handful of files, so a linear scan is fine; comparison
    is constant-time so a wrong token learns nothing from how long it took.
    """
    want = _digest(token or "")
    for u in reg.all():
        if not u.approved:
            continue
        p = reg.user_dir(u.chat_id) / _FILE
        try:
            have = json.loads(p.read_text(encoding="utf-8")).get("sha256", "")
        except (OSError, ValueError):
            continue
        if secrets.compare_digest(have, want):
            return u.chat_id
    return None


def has_token(reg: Registry, chat_id: str) -> bool:
    return (reg.user_dir(chat_id) / _FILE).exists()
