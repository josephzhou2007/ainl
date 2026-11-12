"""Thin HTTP layer over Canvas.

Handles the four things every Canvas call needs and nothing else:
  1. auth (pluggable: Bearer or browser cookies — see auth.py)
  2. `application/json+canvas-string-ids` so IDs arrive as strings
  3. Link-header pagination
  4. the leaky-bucket rate limit (X-Rate-Limit-Remaining)
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator, Optional

import httpx

from .auth import Auth

_JSON_PREFIX = "while(1);"


class CanvasHTTPError(RuntimeError):
    def __init__(self, status: int, path: str, body: str):
        super().__init__(f"{status} on {path}: {body[:200]}")
        self.status = status
        self.path = path


class CanvasAuthError(CanvasHTTPError):
    """Session/token is not (or no longer) valid. Caller should re-authenticate."""


def _parse(text: str) -> Any:
    if text.startswith(_JSON_PREFIX):
        text = text[len(_JSON_PREFIX):]
    return json.loads(text) if text.strip() else None


class CanvasHTTP:
    # Canvas refills ~50 units/sec into a 700-unit bucket; a cheap GET costs ~1.
    # Below this we pause briefly rather than burn the bucket and get 403'd.
    RATE_FLOOR = 100.0

    def __init__(
        self,
        base_url: str,
        auth: Auth,
        *,
        timeout: float = 30.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,          # a redirect to /login means "not logged in"
            event_hooks=__import__("ainl.util.metrics", fromlist=["hooks"]).hooks(),
            cookies=auth.cookies(),
            headers={
                "Accept": "application/json+canvas-string-ids, application/json",
                "User-Agent": "ainl/0.1",
                **auth.headers(),
            },
        )

    def close(self) -> None:
        self._client.close()

    @property
    def cookies(self) -> httpx.Cookies:
        """Live jar — includes any Set-Cookie rotations Canvas sent during this run."""
        return self._client.cookies

    # -- core -----------------------------------------------------------------

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        r = self._client.request(method, path, **kw)
        remaining = r.headers.get("X-Rate-Limit-Remaining")
        if remaining is not None:
            try:
                if float(remaining) < self.RATE_FLOOR:
                    time.sleep(1.0)
            except ValueError:
                pass
        if r.status_code == 403 and "Rate Limit Exceeded" in r.text:
            time.sleep(5.0)
            r = self._client.request(method, path, **kw)

        loc = r.headers.get("location", "")
        if r.status_code == 401 or (r.status_code in (301, 302, 303) and "/login" in loc):
            raise CanvasAuthError(r.status_code, path, r.text or loc)
        if r.status_code >= 400:
            raise CanvasHTTPError(r.status_code, path, r.text)
        return r

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return _parse(self._request("GET", path, params=params).text)

    def post(self, path: str, body: dict) -> Any:
        return _parse(self._request("POST", path, json=body).text)

    def paginate(self, path: str, params: Optional[dict] = None, per_page: int = 100) -> Iterator[Any]:
        """Yield every element across all pages, following Link: rel="next"."""
        params = {**(params or {}), "per_page": per_page}
        r = self._request("GET", path, params=params)
        while True:
            page = _parse(r.text) or []
            yield from page
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                return
            r = self._request("GET", nxt)

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        out = self.post("/api/graphql", {"query": query, "variables": variables or {}})
        if out.get("errors"):
            raise CanvasHTTPError(200, "/api/graphql", json.dumps(out["errors"]))
        return out.get("data") or {}
