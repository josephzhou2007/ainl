"""App-token-authenticated, single-use handoff tickets; no HTTP server yet."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class _Ticket:
    user_id: str
    credential_digest: str
    expires: float


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LoginTickets:
    def __init__(self, verify_token, *, ttl: float = 300, clock=time.monotonic):
        if not 0 < ttl <= 900:
            raise ValueError("Ticket TTL must be in (0, 900] seconds")
        self._verify, self._ttl, self._clock = verify_token, ttl, clock
        self._tickets: dict[str, _Ticket] = {}
        self._lock = threading.Lock()

    def issue(self, app_token: str) -> str:
        user_id = self._verify(app_token)
        if user_id is None:
            raise PermissionError("Invalid app credential")
        raw = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            # One pending ticket per user; also bound memory to active users.
            self._tickets = {k: v for k, v in self._tickets.items()
                             if v.expires > now and v.user_id != user_id}
            self._tickets[_digest(raw)] = _Ticket(user_id, _digest(app_token), now + self._ttl)
        return raw

    def redeem(self, ticket: str, app_token: str) -> str:
        """Call only from authenticated POST, never from link-preview GET."""
        user_id = self._verify(app_token)  # rotation/deletion/approval checked again
        with self._lock:
            entry = self._tickets.get(_digest(ticket))
            if (entry is None or entry.expires <= self._clock() or user_id is None
                    or entry.user_id != user_id
                    or not secrets.compare_digest(entry.credential_digest, _digest(app_token))):
                raise PermissionError("Invalid or expired login ticket")
            del self._tickets[_digest(ticket)]
            return user_id
