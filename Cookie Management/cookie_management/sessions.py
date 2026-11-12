"""Canvas-only session checkpoint/recovery layered on Claude's BrowserPool.

No password inspection, arbitrary HTTP methods, scheduler, or public viewer.
The host application must serialize viewer/business access with this service.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from .store import SnapshotStore


class State(str, Enum):
    ACTIVE = "active"
    UNBOUND = "unbound"
    REAUTH_REQUIRED = "reauth_required"
    IDENTITY_MISMATCH = "identity_mismatch"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class Status:
    state: State
    canvas_id: str | None = None
    reason: str = ""
    name: str = ""          # Canvas display name, only for the person's own confirmation


def probe(context, origin: str) -> Status:
    """No redirects, no response bodies or exception strings in status/logs."""
    response = None
    try:
        response = context.request.get(
            origin + "/api/v1/users/self", max_redirects=0, timeout=10_000,
            headers={"Accept": "application/json+canvas-string-ids, application/json"})
        code = response.status
        if code == 401:
            return Status(State.REAUTH_REQUIRED, reason="unauthorized")
        if 300 <= code < 400:
            return Status(State.REAUTH_REQUIRED, reason="authentication_redirect")
        if code == 403:
            return Status(State.BLOCKED, reason="forbidden")
        if code != 200:
            return Status(State.DEGRADED, reason="http_error")
        raw = response.text()
        data = json.loads(raw[len("while(1);"):] if raw.startswith("while(1);") else raw)
        account = data.get("id") if isinstance(data, dict) else None
        if type(account) not in (str, int) or not str(account).isdigit():
            return Status(State.DEGRADED, reason="invalid_identity_response")
        name = data.get("name")
        return Status(State.ACTIVE, str(account), name=name if isinstance(name, str) else "")
    except Exception:
        return Status(State.DEGRADED, reason="probe_failed")
    finally:
        if response is not None:
            response.dispose()


class PooledBrowser:
    """Uses the existing pool and per-user worker; never starts another profile."""
    def __init__(self, pool):
        self.pool = pool

    def run(self, user_id: str, operation):
        from ainl.util import browserthread, pw

        def work():
            with pw.connect(self.pool.cdp_url(user_id)) as browser:
                if len(browser.contexts) != 1:
                    raise RuntimeError("Expected exactly one dedicated browser context")
                return operation(browser.contexts[0])

        try:
            return browserthread.run(work, budget=45, key=user_id)
        except browserthread.Timeout:
            raise TimeoutError("Browser worker exceeded its deadline") from None


class SessionManager:
    """One instance per service process; operations for a user are serialized.

    Internal user_id methods are not public API authorization. The future gateway
    must derive user_id from tokens.verify(), never from a caller-supplied path.
    """
    def __init__(self, registry, browser, store: SnapshotStore, origin: str):
        url = urlsplit(origin)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("Canvas origin must be a configured HTTPS origin")
        self.origin = origin.rstrip("/")
        self.host = url.hostname
        self.registry, self.browser, self.store = registry, browser, store
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._quarantined: set[str] = set()

    def _approved(self, user_id):
        if not self.registry.is_approved(user_id):
            raise PermissionError("User is not approved")

    def _lock(self, user_id):
        self._approved(user_id)
        with self._guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def _cookies(self, cookies):
        """Strict host-only subset; never persist/replay parent-domain SSO cookies."""
        result = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise ValueError("Invalid cookie record")
            if cookie.get("domain", "").lstrip(".").lower() != self.host:
                continue
            if cookie.get("expires", -1) not in (-1, None) and cookie["expires"] <= time.time():
                continue
            result.append(cookie)
        return result

    def inspect(self, user_id: str) -> Status:
        """Probe without binding or saving. May start this approved user's browser."""
        return self._operate(user_id, "inspect")

    def complete_login(self, user_id: str, confirmed_canvas_id: str) -> Status:
        """Called after the user confirms inspect()'s identity; cannot rebind."""
        if not confirmed_canvas_id or not confirmed_canvas_id.isdigit():
            raise ValueError("A confirmed Canvas user ID is required")
        return self._operate(user_id, "complete", confirmed_canvas_id)

    def heartbeat(self, user_id: str) -> Status:
        """One tick only. Validates the bound account and checkpoints rotated cookies."""
        return self._operate(user_id, "heartbeat")

    def restore(self, user_id: str) -> Status:
        """Explicit restore after startup. Never overwrite a live/different identity."""
        return self._operate(user_id, "restore")

    def _operate(self, user_id, action, confirmed_id=None):
        with self._lock(user_id):
            self._approved(user_id)
            if user_id in self._quarantined:
                return Status(State.QUARANTINED, reason="worker_restart_required")
            saved = self.store.load(user_id, self.origin)
            expected = saved["canvas_id"] if saved else confirmed_id
            if action in ("restore", "heartbeat") and saved is None:
                return Status(State.UNBOUND, reason="login_confirmation_required")
            if confirmed_id and saved and confirmed_id != expected:
                return Status(State.IDENTITY_MISMATCH)

            def work(context):
                status = probe(context, self.origin)
                # Only restore after a confirmed auth failure; network errors and
                # a live account must never trigger a stale-cookie overwrite.
                if action == "restore" and status.state == State.REAUTH_REQUIRED:
                    cookies = self._cookies(saved["cookies"])
                    if not cookies:
                        return status, None
                    context.add_cookies(cookies)
                    status = probe(context, self.origin)
                if status.state != State.ACTIVE:
                    return status, None
                if expected and status.canvas_id != expected:
                    return Status(State.IDENTITY_MISMATCH), None
                if action == "inspect":
                    return Status(State.ACTIVE if saved else State.UNBOUND, status.canvas_id,
                                  name=status.name), None
                return status, self._cookies(context.cookies())

            try:
                status, cookies = self.browser.run(user_id, work)
            except TimeoutError:
                # Existing worker timeout abandons rather than kills a thread.
                # Do not let another operation overlap it or save late results.
                self._quarantined.add(user_id)
                return Status(State.QUARANTINED, reason="browser_timeout")
            except Exception:
                return Status(State.DEGRADED, reason="browser_operation_failed")
            self._approved(user_id)
            if cookies is not None:
                self.store.save(user_id, self.origin, status.canvas_id, cookies)
            return status
