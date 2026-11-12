"""tidy(): one blank tab left, everything else closed, over the HTTP endpoints."""
from __future__ import annotations

from ainl.util import tidy as T


def _fake(monkeypatch, pages):
    calls = []

    def get(url, method="GET", timeout=3):
        calls.append((method, url.split("127.0.0.1:9300")[1]))
        if url.endswith("/json/list"):
            return pages
        if "/json/new" in url:
            return {"id": "NEW", "url": "about:blank", "type": "page"}
        return b"Target is closing"

    monkeypatch.setattr(T, "_get", get)
    return calls


def test_opens_a_blank_tab_first_then_closes_the_rest(monkeypatch):
    calls = _fake(monkeypatch, [
        {"id": "A", "type": "page", "url": "https://account.cengage.com/x"},
        {"id": "B", "type": "page", "url": "chrome://newtab/"},
        {"id": "W", "type": "service_worker", "url": "https://x/sw.js"}])
    assert T.tidy("http://127.0.0.1:9300") == 2
    assert calls[1] == ("PUT", "/json/new?about:blank")     # before any close
    assert [c for c in calls if "close" in c[1]] == [("GET", "/json/close/A"), ("GET", "/json/close/B")]


def test_keeps_an_existing_blank_tab(monkeypatch):
    calls = _fake(monkeypatch, [{"id": "Z", "type": "page", "url": "about:blank"},
                                {"id": "A", "type": "page", "url": "https://x/"}])
    assert T.tidy("http://127.0.0.1:9300") == 1
    assert not any("new" in c[1] for c in calls)


def test_the_block_list_is_one_flag_and_never_blocks_the_sites_we_work_on():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import browser_start as b
    rules = [f for f in b.FLAGS if f.startswith("--host-resolver-rules=")]
    assert len(rules) == 1 and sum(f.startswith("--disable-features") for f in b.FLAGS) == 1
    for keep in ("webassign", "cengage.com\"", "instructure", "canvas", "wisc.edu",
                 "duosecurity", "macmillanlearning", "gradescope", "cookielaw"):
        assert keep not in rules[0]
