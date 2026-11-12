"""How a request proves who it is.

Two strategies, same API surface:

  BearerAuth   Authorization: Bearer <token>. No CSRF, no cookies. Only if the
               institution lets students mint tokens (UW does not, as of 2026-09).

  CookieAuth   The browser's own session: canvas_session + _csrf_token cookies,
               plus X-CSRF-Token on writes. Obtained once via Playwright after a
               human completes SAML + Duo (see session.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import unquote


class Auth(Protocol):
    def headers(self) -> dict[str, str]: ...
    def cookies(self) -> dict[str, str]: ...


@dataclass(frozen=True)
class BearerAuth:
    token: str = field(repr=False)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def cookies(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True)
class CookieAuth:
    canvas_session: str = field(repr=False)
    csrf_token: str = field(repr=False)      # raw cookie value (still URL-encoded)

    def headers(self) -> dict[str, str]:
        # Canvas compares this header to the URL-decoded _csrf_token cookie.
        return {
            "X-CSRF-Token": unquote(self.csrf_token),
            "X-Requested-With": "XMLHttpRequest",
        }

    def cookies(self) -> dict[str, str]:
        return {"canvas_session": self.canvas_session, "_csrf_token": self.csrf_token}

    @classmethod
    def from_cookie_list(cls, cookies: list[dict], domain_hint: str) -> "CookieAuth | None":
        """Build from Playwright's storage_state()['cookies'] shape."""
        want = {"canvas_session": None, "_csrf_token": None}
        for c in cookies:
            if c.get("name") in want and domain_hint.endswith(c.get("domain", "").lstrip(".")):
                want[c["name"]] = c.get("value")
        if not all(want.values()):
            return None
        return cls(canvas_session=want["canvas_session"], csrf_token=want["_csrf_token"])
