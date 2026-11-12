import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet

from registry import Registry
import tokens
from cookie_management.sessions import PooledBrowser, SessionManager, State
from cookie_management.store import SnapshotError, SnapshotStore
from cookie_management.tickets import LoginTickets
from cookie_management.integration import build_services

ORIGIN = "https://canvas.example.edu"


def cookie(value, domain="canvas.example.edu"):
    return {"name": "canvas_session", "value": value, "domain": domain,
            "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}


class Response:
    def __init__(self, context):
        self.context, self.status, self.disposed = context, context.code, False

    def text(self):
        return self.context.body or json.dumps({"id": self.context.account})

    def dispose(self):
        self.disposed = True


class Context:
    def __init__(self, account):
        self.account, self.code, self.body = account, 200, None
        self.request = self
        self.jar = [cookie("initial"), cookie("SSO-SECRET", "login.example.edu")]
        self.responses, self.imports = [], []

    def get(self, url, **kwargs):
        assert url == ORIGIN + "/api/v1/users/self"
        assert kwargs["max_redirects"] == 0
        # Emulate Set-Cookie rotation in the browser's shared cookie jar.
        if self.code == 200:
            self.jar[0] = cookie("rotated")
        response = Response(self)
        self.responses.append(response)
        return response

    def cookies(self):
        return list(self.jar)

    def add_cookies(self, cookies):
        self.imports.append(cookies)
        self.jar = cookies
        self.code = 200


class Browser:
    def __init__(self):
        self.contexts = {"111": Context("101"), "222": Context("202")}
        self.calls = []
        self.timeout = False

    def run(self, user_id, operation):
        self.calls.append(user_id)
        if self.timeout:
            raise TimeoutError()
        return operation(self.contexts[user_id])


@pytest.fixture
def setup(tmp_path):
    reg = Registry(tmp_path)
    for cid in ("111", "222"):
        reg.add(cid, cid, "display-only")
        reg.approve(cid)
    store = SnapshotStore(reg, Fernet.generate_key())
    browser = Browser()
    manager = SessionManager(reg, browser, store, ORIGIN)
    return reg, store, browser, manager


def test_unapproved_cannot_start_browser_or_create_directory(setup):
    reg, _, browser, manager = setup
    with pytest.raises(PermissionError):
        manager.inspect("333")
    assert not browser.calls
    assert not (reg.path.parent / "333").exists()


def test_binding_rotation_encryption_and_foreign_cookie_exclusion(setup):
    reg, store, browser, manager = setup
    assert manager.inspect("111").state == State.UNBOUND
    assert store.load("111", ORIGIN) is None
    assert manager.complete_login("111", "101").state == State.ACTIVE
    saved = store.load("111", ORIGIN)
    assert saved["cookies"] == [cookie("rotated")]
    raw = (reg.user_dir("111") / "canvas-session.enc").read_bytes()
    assert b"rotated" not in raw and b"canvas_session" not in raw
    assert manager.heartbeat("111").state == State.ACTIVE
    assert store.load("111", ORIGIN)["version"] == 2
    assert all(r.disposed for r in browser.contexts["111"].responses)


def test_wrong_login_and_later_account_switch_do_not_overwrite_binding(setup):
    _, store, browser, manager = setup
    assert manager.complete_login("111", "202").state == State.IDENTITY_MISMATCH
    assert store.load("111", ORIGIN) is None
    manager.complete_login("111", "101")
    browser.contexts["111"].account = "202"
    assert manager.heartbeat("111").state == State.IDENTITY_MISMATCH
    assert manager.complete_login("111", "202").state == State.IDENTITY_MISMATCH
    assert store.load("111", ORIGIN)["canvas_id"] == "101"


def test_two_users_are_independent(setup):
    _, store, browser, manager = setup
    barrier = threading.Barrier(2)
    original = browser.run

    def parallel(cid, fn):
        barrier.wait(timeout=3)
        return original(cid, fn)

    browser.run = parallel
    with ThreadPoolExecutor(2) as executor:
        jobs = [executor.submit(manager.complete_login, cid, account)
                for cid, account in (("111", "101"), ("222", "202"))]
        assert all(job.result().state == State.ACTIVE for job in jobs)
    assert store.load("111", ORIGIN)["canvas_id"] == "101"
    assert store.load("222", ORIGIN)["canvas_id"] == "202"


@pytest.mark.parametrize("code,state", [(401, State.REAUTH_REQUIRED), (302, State.REAUTH_REQUIRED),
                                         (403, State.BLOCKED), (429, State.DEGRADED),
                                         (500, State.DEGRADED)])
def test_probe_failure_does_not_replace_snapshot(setup, code, state):
    _, store, browser, manager = setup
    manager.complete_login("111", "101")
    browser.contexts["111"].code = code
    assert manager.heartbeat("111").state == state
    assert store.load("111", ORIGIN)["version"] == 1


def test_html_200_is_not_a_success(setup):
    _, store, browser, manager = setup
    browser.contexts["111"].body = "<html>Sign in</html>"
    assert manager.complete_login("111", "101").state == State.DEGRADED
    assert store.load("111", ORIGIN) is None


def test_restore_only_after_auth_failure_and_check_restored_identity(setup):
    _, _, browser, manager = setup
    manager.complete_login("111", "101")
    ctx = browser.contexts["111"]
    ctx.code = 500
    assert manager.restore("111").state == State.DEGRADED
    assert not ctx.imports
    ctx.code = 401
    assert manager.restore("111").state == State.ACTIVE
    assert ctx.imports == [[cookie("rotated")]]
    ctx.account = "202"
    assert manager.restore("111").state == State.IDENTITY_MISMATCH
    assert len(ctx.imports) == 1


def test_copied_or_corrupted_snapshot_fails_closed(setup):
    reg, store, _, manager = setup
    manager.complete_login("111", "101")
    a, b = (reg.user_dir(cid) / "canvas-session.enc" for cid in ("111", "222"))
    b.write_bytes(a.read_bytes())
    with pytest.raises(SnapshotError):
        store.load("222", ORIGIN)
    a.write_bytes(b"corrupt")
    with pytest.raises(SnapshotError):
        manager.heartbeat("111")


def test_failed_atomic_replace_keeps_old_snapshot(setup, monkeypatch):
    _, store, _, manager = setup
    manager.complete_login("111", "101")

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("cookie_management.store.os.replace", fail)
    with pytest.raises(OSError):
        manager.heartbeat("111")
    assert store.load("111", ORIGIN)["version"] == 1


def test_timeout_blocks_new_work_for_that_user_only(setup):
    _, _, browser, manager = setup
    browser.timeout = True
    assert manager.inspect("111").state == State.QUARANTINED
    browser.timeout = False
    assert manager.inspect("111").state == State.QUARANTINED
    assert browser.calls == ["111"]
    assert manager.inspect("222").canvas_id == "202"


def test_revoked_approval_blocks_existing_binding(setup):
    reg, _, browser, manager = setup
    manager.complete_login("111", "101")
    reg.get("111").approved = False
    count = len(browser.calls)
    with pytest.raises(PermissionError):
        manager.heartbeat("111")
    assert len(browser.calls) == count


def test_tickets_enforce_owner_single_use_expiry_and_token_rotation(setup):
    reg, _, _, _ = setup
    a, b = tokens.issue(reg, "111"), tokens.issue(reg, "222")
    now = [0]
    tickets = LoginTickets(lambda token: tokens.verify(reg, token), clock=lambda: now[0])
    ticket = tickets.issue(a)
    with pytest.raises(PermissionError):
        tickets.redeem(ticket, b)
    assert tickets.redeem(ticket, a) == "111"
    with pytest.raises(PermissionError):
        tickets.redeem(ticket, a)
    ticket = tickets.issue(a)
    now[0] = 300
    with pytest.raises(PermissionError):
        tickets.redeem(ticket, a)
    ticket = tickets.issue(a)
    new_a = tokens.issue(reg, "111")
    for token in (a, new_a):
        with pytest.raises(PermissionError):
            tickets.redeem(ticket, token)


def test_pool_adapter_uses_matching_key_and_endpoint(monkeypatch):
    from ainl.util import browserthread, pw
    observed = []
    ctx = object()

    class Pool:
        def cdp_url(self, user_id):
            observed.append(user_id)
            return "http://127.0.0.1:9301"

    @contextmanager
    def connect(url):
        observed.append(url)
        yield type("Browser", (), {"contexts": [ctx]})()

    def run(fn, *, budget, key):
        observed.append(key)
        return fn()

    monkeypatch.setattr(pw, "connect", connect)
    monkeypatch.setattr(browserthread, "run", run)
    assert PooledBrowser(Pool()).run("222", lambda c: c) is ctx
    assert observed == ["222", "222", "http://127.0.0.1:9301"]


def test_composition_is_lazy_and_reuses_existing_pool(setup):
    reg, _, _, _ = setup
    pool = object()
    services = build_services(reg, pool, lambda value: tokens.verify(reg, value),
                              origin=ORIGIN, encryption_key=Fernet.generate_key())
    assert services.sessions.browser.pool is pool
    app_token = tokens.issue(reg, "111")
    ticket = services.tickets.issue(app_token)
    assert services.tickets.redeem(ticket, app_token) == "111"


def test_expired_and_parent_domain_cookies_are_not_persisted(setup):
    _, store, browser, manager = setup
    expired = {**cookie("expired"), "expires": 1}
    browser.contexts["111"].jar.extend([expired, cookie("parent", ".example.edu")])
    manager.complete_login("111", "101")
    assert store.load("111", ORIGIN)["cookies"] == [cookie("rotated")]


def test_inspect_carries_the_canvas_name_for_the_confirmation(setup):
    _, store, browser, manager = setup
    browser.contexts["111"].body = json.dumps({"id": 101, "name": "Ada Lovelace"})
    st = manager.inspect("111")
    assert (st.state, st.canvas_id, st.name) == (State.UNBOUND, "101", "Ada Lovelace")
    assert store.load("111", ORIGIN) is None                # inspecting saves nothing
    browser.contexts["111"].body = json.dumps({"id": 101, "name": ["not", "a", "name"]})
    assert manager.inspect("111").name == ""
