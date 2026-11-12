**English** · [简体中文](ONBOARDING.zh-CN.md)

# Onboarding design — the 60-second goal

## Principle

> Anything the developer can script is never left to the user.

Every step that asks the user to "open settings", "copy and paste" or "create something" counts as a design failure.
The measure: **number of input fields = 0, number of taps ≤ 4**.

---

## Two phases

> **Status (2026-09-20):** Phase 1 is already the live path — the school approved personal access tokens, and
> the token is the main channel. The browser cookie session is the fallback. Vendor platforms like WebAssign
> **need no extra password from the student**: the session is created by Canvas's LTI sessionless launch, so the
> only onboarding step is "give us a Canvas token". Phase 2 (OAuth2 developer key) and multi-user are not done;
> the three constraints for multi-user are in `docs/optimization-goals.md` (Chinese).

### Phase 1 — Pilot (no school approval needed, ~90 seconds)

Uses a Canvas **personal access token**.

```
1. /start                                        3s
2. bot: "Tap this link → New Access Token at the bottom
         → send me the string it gives you" + an annotated screenshot
3. student generates and pastes it               60s
4. bot verifies → "Found 4 courses ✓"            5s
```

- Downside: one copy-paste, and the student has to make sense of the Canvas settings page
- Upside: **runs today**, nobody has to approve anything
- For: 10–50 seed users, to collect real usage data and word of mouth

**Trick to reduce friction at this step**: the bot sends a screenshot with a red circle instead of a text description.
In practice this cuts the "couldn't find where to click" drop-off by more than half.

### Phase 2 — a school-issued developer key (~30 seconds, zero input)

Ask the UW Canvas admins for an **OAuth2 developer key** and get a `client_id` / `client_secret`.

```
1. tap t.me/<YourBot>                            3s
2. bot sends a "Connect Canvas" button
3. tap → canvas.<school>.edu/login/oauth2/auth?...
   student already logged in → just one Authorize button   15s
4. callback → back in Telegram: "Found 4 courses ✓"         5s
```

The OAuth callback carries the `telegram_user_id` in Telegram's `state` parameter, so after authorization the
bot knows who it was without the user linking anything.

**Getting a developer key is itself the first gate of school acceptance** — if you get one, IT at least doesn't
object; if you don't, you need professors or student government to vouch first. So onboarding friction is the
project's political progress bar.

---

## Default shape: private chat, not a group

v0.2 designed "supergroup + one topic per course", but that requires the user to:
create a group → add the bot → make it admin → convert to supergroup → turn on Topics → create 5 topics.

**6 steps, and the bot can't do them for you (the Bot API doesn't let bots create groups).** That directly conflicts with the 60-second goal.

### Final decision

| Shape | Setup cost | For |
|---|---|---|
| **Private chat (default)** | 0 steps | everyone |
| Group + topics (optional) | guided, several steps | power users who ask for it |

How a private chat gets "a separate context per course" (which topics used to provide):

1. **Automatic routing** — most messages reveal the subject from their content (mentions "Gramática" → SPANISH101)
2. **Sticky focus** — a persistent row of inline buttons `[MATH222] [PHY247] [SPA101] [ESL116]`;
   tap once to switch the current course, then keep talking to it without tapping again
3. **Reply sets the target** — replying to an assignment message enters that course's context

Together these feel almost the same as topics, at zero setup cost.

> To verify: Telegram's minimum requirement for enabling Topics (historically 200 members, later relaxed; needs testing).
> Even with no requirement, 6 setup steps are over budget, so private-chat-first stands.

---

## What this changes in the architecture (v0.2 → v0.3)

From single-tenant to multi-tenant; three things have to change with it:

| | v0.2 (personal use) | v0.3 (a service) |
|---|---|---|
| Instance | one Mac mini per person | **one backend, N users** |
| Database | one SQLite per person | every table gets `user_id`, row-level isolation |
| Bot | everyone builds their own | **one shared bot** |
| Credentials | your own token on your own machine | **holding other people's credentials → must be encrypted + revocable + have a privacy notice** |

⚠️ **The "your data never leaves your own device" argument no longer holds.** You are now keeping other people's
Canvas credentials. That is a real security obligation, not a formality:

- tokens encrypted with a separate key (not plaintext, not environment variables)
- a `/disconnect` that revokes and deletes in one step
- a one-page privacy notice: what is stored, what isn't, how to delete it
- auditable outbound logs

### Hosting

For the pilot (around 50 users) a Mac mini is plenty. It does need a public HTTPS endpoint for the Telegram
webhook and the OAuth callback.

**Cloudflare Tunnel** — free, no port forwarding, no public IP, no domain to buy. One command exposes a
local port on the Mac mini as HTTPS. At this scale it is the right answer.
