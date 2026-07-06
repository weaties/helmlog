---
name: dogfood
description: Browser-verify a branch end-to-end before PR — build a journey matrix from the diff, drive the web UI to check flows actually work, run a sailor-persona experiential pass, then propose small low-risk fixes (red-before test, feature branch) or escalate. TRIGGER when a branch touches web.py, templates/, or static/ and the user asks to "dogfood", "verify in the browser", "check the UI actually works", or before opening a UI PR. DO NOT trigger for backend-only diffs (decoders, storage, federation with no UI surface), mid-implementation, or unit-test runs — use /pr-checklist for the final PR gate.
---

# /dogfood — Browser verification of what changed

Closes the gap between "code merged" and "does it actually work for the
crew?". `/pr-checklist` proves the diff is *shippable* (lint/type/test/tier);
this skill proves the diff is *usable* by driving the running web UI through
real journeys. Run it before `/pr-checklist`, not instead of it.

This encodes only the verification *shape*. The journeys live in
`docs/features.md`; the risk tiers live in `AGENTS.md`; the TDD red-before
rule lives in `/tdd`. Read those — don't restate them here.

## Phases

**Scope → Matrix → Serve → Execute → Judge → Fix-or-escalate → Report**

### 1. Scope
`git diff --name-only main...HEAD`. Keep only files with a user-visible
surface (`web.py`, `templates/`, `static/`). If none, stop — this is a
backend diff, `/dogfood` doesn't apply.

### 2. Matrix (journey-first, not page-first)
For each changed surface, name the **journeys** it participates in from
`docs/features.md` (crew / viewer / admin). A journey is an end-to-end flow,
not a page load. "The history page renders" is not a row; **"mark a race →
open it in history → scrub to the start → export GPX → the file downloads
with the right track"** is. Each row: journey, audience, precondition,
expected observable outcome.

### 3. Serve
Start the app against a scratch DB — never the live/Pi DB, never
`helmlog.db` in the repo:

```bash
WEB_PORT=3002 helmlog run   # or a test DB path via StorageConfig
```

Seed the minimum fixture data the journey needs (a race, a user with the
right role). Web-route smoke checks can also use the existing
`httpx.AsyncClient` + `ASGITransport` pattern; use a real browser
(`claude-in-chrome` tools) for anything involving JS, layout, or touch flows.

### 4. Execute
Drive each matrix row in the browser. Assert the **observable outcome**, not
that a request returned 200 — the right data, the right element, the right
place. Record pass/fail per row.

### 5. Judge — two axes
- **Functional:** did the outcome happen? (forms submit, data persists,
  exports match, links resolve).
- **Experiential:** re-walk as a **crew member on a wet phone mid-race** —
  glanceability, one-handed thumb reach, wet-hands tap targets, copy that
  matches what a sailor expects. Log "paper cuts": real friction too minor
  to fail functionally. (`/domain` has the sailing context for this.)

Quadrant → action:

| | Smooth | Paper cuts |
|---|---|---|
| **Pass** | ship | fix-loop |
| **Fail** | fix-loop | fix-loop + note experiential mismatch |

### 6. Fix-or-escalate — governor
Before editing, size the fix: **small, well-understood, low-risk?**

- **Yes → auto-fix**, but only on a feature branch (never `main`), one
  logical change per commit, each with a **red-before / green-after**
  regression test (`/tdd`), then re-run the failing row *and* its neighbours.
- **No → escalate.** Do not decide unasked. Write a **Human decision** row
  (options + recommendation) and stop. Escalate whenever the fix would:
  - touch a **Critical/High-tier** module (`AGENTS.md` risk tiers) —
    `storage.py` migrations, `auth.py`, `federation.py`, `peer_*`,
    `sk_reader.py`, `export.py`, `transcribe.py`;
  - change a schema, a data shape, or a licensing/PII surface
    (`/data-license`);
  - involve a design trade-off or ambiguous intent.

### 7. Report
Keep a running trail at `docs/dogfood-reports/<date>-<branch>.md`: one line
per matrix row (verdict), each fix's commit SHA + the row it closed, and any
open **Human decision** rows. The report is the checkpoint — a later session
resumes from it.

## Exit criteria
Ready = every matrix row green **and** the full suite green **and** one
test per fix **and** zero unresolved Human-decision rows. A green matrix with
red tests is **not** ready. On green, hand off to `/pr-checklist` for the PR.

## Guardrails
- Never drive against the live Pi or a real DB; scratch data only.
- Never commit to `main`; fixes land on the feature branch, merge via PR.
- Never auto-fix across a tier boundary — escalate instead.
- Keep `helmlog.db` and `data/` out of git (`AGENTS.md`).
