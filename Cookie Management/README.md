**English** · [简体中文](README.zh-CN.md)

# Cookie Management: implementation notes

> Re-checked against Claude's latest code. This round only rewrote this README and added standalone code and tests in this directory;
> `src/`, `scale-up/` and the existing browser prototype were not modified. The new module is not yet wired into the running bot, and the
> remote login page is not deployed.

## 1. Starting point and scope of this round

Capabilities Claude had already built are reused directly:

| Existing module | Current capability |
|---|---|
| `scale-up/browsers.py` | per-user profile, CDP port, started on demand; `restart(chat_id)` restarts only that user |
| `src/ainl/util/browserthread.py` | workers assigned by key; `using(key)` / `run(key=...)`, different users can run concurrently |
| `src/ainl/util/pw.py` | connection cache keyed by thread and CDP address |
| `src/ainl/external/webassign.py` | `cdp_url`, `launch_url`, `restart` can be injected |
| `scale-up/onboarding.py`, `tokens.py` | private-chat registration, approval, hashed storage and verification of app tokens |
| `scale-up/bot.py` | a single Telegram receiver; owner business traffic and other users' onboarding are split |

The top of `browsers.py` still has an old comment saying "per-user worker not done"; the latest `browserthread.py` implementation is authoritative.
New users still get the "Canvas not connected yet" message: finished browser isolation does not mean login handover and task dispatch are wired up.

This round implements the next layer, a **Canvas session-management library**, not a new browser pool. No real browser was started,
no existing personal cookies were read, nothing logged in to the school or submitted homework.

## 2. New files

| File | What it implements |
|---|---|
| `cookie_management/integration.py` | `build_services()`: injects the existing registry/pool/token verifier and builds the services |
| `cookie_management/sessions.py` | per-user probe, first-time identity confirmation, keep-alive check, cookie restore; plugs into the existing keyed workers |
| `cookie_management/store.py` | Fernet encryption, identity binding, versioned snapshots, encrypted temp file and atomic replace |
| `cookie_management/tickets.py` | after app-token verification, issues 5-minute, single-use login tickets bound to the user and the token version |
| `tests/test_sessions.py` | fake-browser tests for authentication, isolation, rotation, restore, errors and tickets; never contacts the school |
| `requirements.txt` | adds the encryption dependency; root dependencies unchanged |

`SessionManager` is an in-process single-instance service: one user at a time, different users independent. Its lock covers only calls into this
library; when wiring in the viewer/WebAssign, the scheduling layer also needs a shared human-control and task lock.

## 3. Composing the services in the existing program

The following is an example of future wiring, not a live entry point. The new directory is not inside the root project's setuptools package
discovery; the entry point has to add the path explicitly, and the final packaging location is decided later.

```python
# Put this where the future scale-up entry point initialises; never recreate it per request.
import os
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[1]  # this example assumes the entry point is in scale-up/
sys.path.insert(0, str(repo / 'Cookie Management'))

from cookie_management.integration import build_services
import tokens

# reg / pool must reuse the Registry / BrowserPool this process uses for its business browsers.
services = build_services(
    reg,
    pool,
    lambda token: tokens.verify(reg, token),
    origin='https://canvas.wisc.edu',
    encryption_key=os.environ['AINL_SESSION_KEY'].encode('ascii'),
)
```

`AINL_SESSION_KEY` must be a valid Fernet key supplied by secure server-side configuration; never store it in a user directory, Git or a chat,
and never generate a new one on every restart. Fernet is authenticated encryption: if the key is lost, old snapshots cannot be recovered. [Official docs](https://cryptography.io/en/latest/fernet/)

The existing `Registry` holds in-memory state, so the first version should share one registry/pool/services inside a single host process.
Separate web processes must not each load a stale registry and allocate ports on their own; splitting into processes later needs a single supervisor/RPC or shared-state coordination.

## 4. Login handover call sequence

```text
an approved classmate's app token
  → LoginTickets.issue(app_token)
  → authenticated POST redeem(ticket, app_token)
  → returns the server-confirmed user_id
  → the viewer attaches to pool.cdp_url(user_id) and takes human control
  → the classmate completes NetID + Duo themselves
  → sessions.inspect(user_id)
  → show the returned canvas_id to the classmate to confirm it is the account to connect
  → sessions.complete_login(user_id, confirmed_canvas_id)
  → ACTIVE, encrypted snapshot saved, viewer control ends
```

- `tokens.py` issues this app's own credential, not a Canvas token, and does not replace NetID/Duo.
- The ticket text is never stored, only its hash; at most one pending ticket per person. Rotating the app token invalidates old tickets; a ticket can be redeemed once, and a service restart naturally invalidates it.
- `redeem()` is not an HTTP route; the future gateway must use POST and protect Origin/CSRF. A GET link preview must not be able to consume a ticket.
- Redeeming a ticket only establishes the user; it does not create a persistent viewer session. The gateway must also issue a short-lived viewer session and keep authorising the WebSocket connection and every later action.
- `SessionManager`'s `user_id` methods are internal; never pass a user number from an external URL straight in. Derive it from the app-token verification result or a verified viewer session.
- When unbound, `inspect()` returns `UNBOUND` and a Canvas ID to confirm, without binding automatically; once bound, silently switching accounts is not allowed.

## 5. How cookies are saved and restored

### Requests and rotation

`PooledBrowser.run()` uses the existing `browserthread.run(key=user_id)` and then connects to `pool.cdp_url(user_id)`. The dedicated browser must have exactly
one context, so there is never an ambiguous automatic context choice.

The probe issues `request.get('/api/v1/users/self')` through that context, with `max_redirects=0`, a 10-second request timeout and a 45-second outer worker budget.
Playwright's context request shares cookies with the browser, so rotated cookies in the response go back into the same cookie jar. [Official API docs](https://playwright.dev/python/docs/api/class-apirequestcontext)

A 200 must also parse to a numeric account ID; the `while(1);` JSON prefix the repo uses is supported. A login HTML page is never taken as success, and error results never return the response body, login input or cookies.

### Snapshots

Storage follows the registry layout:

```text
<AINL_HOME>/users/<chat_id>/
  chrome-profile/       # managed by the existing BrowserPool
  app-token.json        # managed by the existing tokens.py
  canvas-session.enc    # the session snapshot added this round
```

The encrypted content includes `schema=1`, the app user_id, the Canvas origin, the Canvas ID, an increasing version and the cookie list. The envelope identity
is checked on read, so copying A's ciphertext into B's directory is rejected.

**Only cookies whose domain exactly matches the Canvas host are saved**, keeping path/expiry/secure and other attributes and excluding other domains and expired entries.
SSO/Duo cookies, localStorage and IndexedDB are not saved, and this does not claim to be a complete storage state. That browser state is still kept by the existing
profile; actual Duo behaviour after a restart needs real-world testing.

Writes go encrypted temp file in the same directory → flush/fsync → `os.replace`; no plaintext temp file is ever created. Corruption, a key mismatch or an identity
mismatch raises `SnapshotError`; it is never silently treated as "no file" and used to create a new identity.

The profile itself still contains credentials; the encrypted snapshot does not replace service-account ACLs and disk protection. Multi-process write locking, key
rotation and a backup policy are not implemented; the first version has a single service instance writing exclusively.

### Restore

`sessions.restore(user_id)` first probes the current browser:

- Already logged in to the right account: save the current fresh state, don't overwrite it with the old snapshot.
- Logged in to a different account: return `IDENTITY_MISMATCH`.
- Confirmed 401 or redirect: import the Canvas cookies from the current snapshot and probe the account again; the snapshot is updated only if restore succeeds.
- Network error, 403, 429, 5xx or invalid JSON: old cookies are not imported.
- No snapshot: return `UNBOUND` and wait for the first login.

A snapshot cannot revive a session the school has revoked; it still has to be verified after restore. Any 3xx on the probe is currently treated as needing
re-authentication, without following the redirect target; if the school adds non-authentication redirects, they should be classified further.

## 6. Keep-alive and failure states

`heartbeat(user_id)` runs one probe and saves the rotated cookies; there is no automatic thread or schedule yet. A future central scheduler would call it, starting at
roughly 30-minute intervals and adjusting to real behaviour; when there is business activity anyway, fewer extra requests are needed.

| State | Meaning and what the layer above should do |
|---|---|
| `ACTIVE` | probe succeeded and identity matches; returned only after the snapshot operation succeeded |
| `UNBOUND` | no confirmed binding yet; offer the login entry |
| `REAUTH_REQUIRED` | 401/3xx; pause tasks, tell the person to re-authenticate |
| `IDENTITY_MISMATCH` | an existing account binding doesn't match the current browser; refuse to update |
| `BLOCKED` | 403; check permissions, never blindly trigger Duo |
| `DEGRADED` | network / other HTTP / format error; back off |
| `QUARANTINED` | worker timeout; this instance refuses further operations for that user |

Quarantine on timeout exists because the existing browserthread timeout only stops waiting; the old thread may still be running. After a timeout this library will not
accept a late snapshot or dispatch more tasks for that user. Recovery requires the host to confirm the old task has stopped and do a controlled restart, then rebuild the
services; there is deliberately no interface to blindly clear the quarantine flag.

Approval-revocation checks happen before browser access and before saving, but cancelling network requests already in flight is not promised. When `/forget` is properly
wired, it must first stop that user's queue, viewer and browser, then delete the directory; deleting only the snapshot is not a global logout on the school side.

## 7. Existing places that need changes later

**The table below is a wiring plan; none of these files were modified this round.**

| Location | What needs to be wired in |
|---|---|
| `scale-up/bot.py` | initialise and reuse registry/pool/services; single receiver dispatching per user; separate Conversation/BotState for new users; add probe scheduling and de-duplicated re-login notices |
| `scale-up/onboarding.py` | replace the post-approval "coming soon" message with a connect entry; `/forget` revokes active sessions and tasks before deleting the directory |
| `scale-up/browsers.py` | coordinate restart/reap/shutdown with checkpoint/restore; save before reaping, restore after starting; verify port ownership and leftover processes. Don't call back into a SessionManager that calls the pool again while holding the pool's internal lock, to avoid deadlock |
| `src/ainl/bot/conversation.py` | inject the user's own Canvas client, WebAssign factory and channel policy; new users no longer use the default 9222 or the global channel |
| `src/ainl/canvas/api.py`, `_http.py` | a business transport that can be injected with a shared BrowserContext, keeping pagination, upload, CSRF, error and receipt semantics; today only this library's probe shares cookies, business requests are not migrated |
| `src/ainl/canvas/channel.py`, `keepalive.py` | pass user context explicitly for the multi-user entry, or replace their global behaviour entirely with the new entry; keep the owner's old entry compatible |
| root `pyproject.toml` | include the new package and dependencies and make test collection explicit when wiring in; no change needed now |

`registry.py`, `tokens.py`, the keyed browserthread, the CDP cache and WebAssign's injection points can be reused as they are. The global fallback in
`ApiSource.recover_auth()` must be checked later so it can't wrongly switch back to the owner's account.

## 8. The remote viewer to add next

The new viewer should attach to the pool's existing browser, not call `launch_persistent_context()` again on the same profile.

It keeps the prototype's CDP screencast + input passthrough technique, but behind a new controlled gateway:

1. HTTPS/WSS gateway authentication; CDP stays internal only.
2. Each viewer session maps to exactly one verified user and browser; frames are sent only to that user.
3. Background work pauses during the human-control lease; new pages opened by the real SSO/Duo flow are supported.
4. Do not copy the prototype's `/status` reading of `activeElement.value`; no logging of login input, page bodies, tickets or cookies.
5. The prototype's `/agent/read`, `/snapshot`, `/shutdown` are not exposed directly; state-changing interfaces need authorisation and CSRF/Origin checks.
6. Restrict navigation/redirects and network egress, and block access to the host's internal services; measure the school's actual login domains before configuring.
7. When login completes, call this library to confirm the binding, revoke viewer control, then resume the task queue.

This layer, a complete business transport, idempotent submission checks and production revocation orchestration are still to be built. What runs today is the tests
and the internal session interface, not a login product classmates can open directly.

## 9. How to verify

From the repository root:

```powershell
python -m pip install -r 'Cookie Management/requirements.txt'
python -m pytest 'Cookie Management/tests' scale-up/tests tests/test_browserthread_keys.py tests/test_session.py tests/test_auth.py -q
```

In this environment the default pytest temp path hit a Windows permission problem, so verification used a dedicated temp directory inside the repo:

```powershell
python -m pytest 'Cookie Management/tests' scale-up/tests tests/test_browserthread_keys.py tests/test_session.py tests/test_auth.py -q -p no:cacheprovider --basetemp='D:/code/local/projects/AI-native-learning/Cookie Management/.test-tmp-01'
```

`--basetemp` must point at a directory used only for tests; pytest cleans it, so never point it at real user data. The tests cover first binding, two users isolated
under concurrency, cookie rotation, ciphertext tampering / cross-user copying, write failure, authentication-error classification, restore, timeout quarantine,
revoked approval, and tickets across users / replay / expiry / token rotation.

Result for the related tests this round: **67 passed**, including the new session tests and the existing browser-pool, registry, worker-thread and auth tests; `git diff --check` passed.

Real Canvas/Duo login, restore after a browser restart, resource use over long runs and homework submission have not been tested for real.
