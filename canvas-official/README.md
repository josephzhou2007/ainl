**English** · [简体中文](README.zh-CN.md)

# canvas-official — the official access-token channel

The main channel. It uses a school-approved Canvas personal access token and calls only REST endpoints from
Instructure's public documentation. **It contains no reverse-engineering**: no browser session, no scraped CSRF
token, no internal GraphQL queries lifted from the dashboard.

The reverse-engineered approach (`../src/ainl/canvas/`, cookie + internal GraphQL) is demoted to a **fallback channel**,
used only if the token is revoked, its scopes are narrowed or the school switches the API off. Both implement the same
`CanvasSource` semantics.

## Why a separate directory

Not for code tidiness, but so it can be **handed over**: this directory can be shown to the school, used by other students
or open-sourced on its own, without also explaining "how I reverse-engineered your Canvas". Mixed together, it couldn't be cut out like this.

The costs are listed under §Gaps below.

## Running it

```
setx CANVAS_TOKEN "<token>"
```

(Canvas → Account → Settings → **+ New Access Token**. `setx` only affects **newly** opened windows. On macOS/Linux: `export CANVAS_TOKEN=...`.)

```
python -m canvasofficial --probe
```

`--probe` is the first thing to run: a token is not all-or-nothing. A token issued through a school's scoped developer key
answers some endpoints with 403 "insufficient scopes" while being perfectly valid. The probe calls every endpoint and prints a
PASS / SCOPE / AUTH / 404 / FAIL table. **That table decides which reverse-engineered code can be retired.**

```
python -m canvasofficial --whoami
python -m canvasofficial --courses
python -m canvasofficial --todo --days 4
python -m canvasofficial --assignment <course_id> <assignment_id>
python -m canvasofficial --submit-files <course_id> <assignment_id> answer.pdf
```

`--submit-files` first prints which assignment it will submit to and how many files of what size, and only proceeds after you type `submit`.

## Boundary

`submit.py` only submits text or files **the student made themselves** to Canvas-native assignments. **This package never touches any third-party platform.**

Work on vendor platforms (WebAssign etc.) lives in `../src/ainl/external/`, under this rule: the agent carries what you made; it neither
produces nor submits anything you didn't do — the answers are the student's handwriting, before submitting the student sees the rendered
result the platform will actually grade, and **nothing is submitted without confirmation**.

## Gaps — what the official REST API can't give you

| Capability | Official REST | How the fallback channel does it |
|---|---|---|
| Cross-course view of assignments by submission state (late / missing / excused in one go) | only per course via `assignments?include[]=submission` | internal GraphQL `courseWorkSubmissionsConnection`, all at once |
| Due / Missing / Submitted counts per course | no endpoint | internal GraphQL `submissionStatistics` |
| Questions and answers inside vendor platforms (WebAssign etc.) | not covered at all | drive the student's own logged-in browser (`ainl.external.webassign`) |

**Two items that are no longer gaps (measured 2026-09-19/20):**

- **The first two rows can actually use the token.** `--probe` includes an undocumented probe of `/api/graphql`, and a Bearer token works
  directly (see `PROBE.md`). It is still an undocumented endpoint, so the per-course REST path stays as the slow equivalent.
- **LTI launch has an official endpoint, which the first version of this table missed.**
  `GET /api/v1/courses/:id/external_tools/sessionless_launch?assignment_id=…&launch_type=assessment`
  works with student permissions and returns a one-time URL; opening it in the browser completes the LTI launch — no need to build a
  signature yourself, and no `target='_self'` trick. It is also the authoritative mapping from Canvas assignment → vendor assignment,
  and can rebuild a vendor session without any vendor password. The returned URL carries `verifier=`: **it is a credential, keep it out of logs**.

Two more official parameters, which brought `/todo` from 12.8 seconds down to 0.9, are worth recording here:
`end_date` on `planner/items`, and `assignment_ids[]` on `courses/:id/assignments` — if a filter can be pushed down to the server, don't keep it on the client.

## Relation to `ainl`

`ainl.canvas.ApiSource.from_env()` already supports Bearer: whenever `CANVAS_TOKEN` exists it uses the token, and only starts a browser
when it doesn't. So once the environment variable is set, the morning brief, the Telegram bot and submissions all switch to the official
channel with no changes above. This directory is that channel's **standalone, auditable, distributable** implementation.
