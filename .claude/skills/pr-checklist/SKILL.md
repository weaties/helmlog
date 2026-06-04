---
name: pr-checklist
description: Run pre-PR verification checks before creating a pull request. TRIGGER when implementation is complete and the user is ready to create or push a PR — e.g., "create a PR", "ready for review", "push this up", or running /pr-checklist. DO NOT trigger mid-implementation, during TDD cycles, when exploring code, or when the user is still writing features.
---

# /pr-checklist — Pre-PR verification

The lint / format / type / test commands and the worktree-and-feature-branch
rule are already in CLAUDE.md. This skill encodes only the pre-PR steps
that are NOT obvious from CLAUDE.md alone — tier resolution rules with
their special cases, the documentation-update map, and the per-tier
checklist matrix.

## Mark issue in-progress (if not already)

```bash
gh issue edit <N> --add-label "in-progress"
gh issue comment <N> --body "In progress on \`<branch>\` (Claude Code on $(hostname))"
```

## Resolve the PR's risk tier

The PR's tier is the **highest** tier of any changed file. Check with
`git diff --name-only main...HEAD`.

| Tier | Files matching |
|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (if migrations changed), `can_reader.py` |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py`, `boat_settings.py` |
| **Standard** | other `.py` files |
| **Low** | templates, CSS, JS, docs, config, scripts |

Report the resolved tier — e.g., "This PR touches `auth.py` → **Critical**."

### Special cases

- **`storage.py` migration check.** If `storage.py` is in the diff,
  examine the diff content — look for `schema_version`, `CREATE TABLE`,
  `ALTER TABLE`, or migration-dict entries. If only query methods or
  non-migration code changed, classify as **Standard**, not Critical.
- **New / unclassified module.** Any changed `.py` not in the tier list
  defaults to **Standard**. Flag it: "Module `X` is not explicitly
  classified — defaults to Standard. Consider adding it to the Risk
  Tiers table in CLAUDE.md."

## Per-tier checks

| Check | Critical | High | Standard | Low |
|---|---|---|---|---|
| Tests | Required | Required | Required | Optional |
| Lint + format | Required | Required | Required | Required |
| mypy | Required | Required | Required | Optional |
| Integration tests | Required | If federation/PII | No | No |
| `/data-license` | Required | If data/PII | No | No |
| Spec review (`/spec`) | Required | No | No | No |
| Complexity check | Required | Required | Required | No |
| Issue linking | Required | Required | Required | Optional |

For Critical-tier without an existing approved `/spec` comment on the
issue, **stop and flag** — the spec must be approved before merge.

## Documentation updates

If the change involved any of these, update accordingly:

| Change | Update |
|---|---|
| New module | Project structure tree in `CLAUDE.md` |
| New env vars | `.env.example` |
| New CLI command | Common Commands in `CLAUDE.md` |
| New stack tool | Stack & Tooling table in `CLAUDE.md` |
| Schema migration | Schema version in `CLAUDE.md` Stack table |
| Data handling change | Verify against `docs/data-licensing.md` |
| New dependency | In `pyproject.toml` and installs via `uv sync` |
| New module with risk implications | Risk Tiers table in `CLAUDE.md` |

## Complexity check

Use the `/architecture` severity thresholds (Watch 200–300, Warning
300–500, Alert 500+) on changed `.py` files. Cross-reference with risk
tier — Critical/High hotspots are more urgent than Standard. Also flag
files that grew by more than 50 lines in this PR even if they were
already over 200.

## Don't rationalize skipping a check

The tier matrix is resolved by file, not by how the diff feels. Common
excuses and their rebuttals:

| Rationalization | Rebuttal |
|---|---|
| "The `storage.py` change is tiny, skip the spec." | Tier is set by file + migration content, not diff size. If migrations changed, it's Critical and needs an approved `/spec` before merge. |
| "Unit tests pass — integration tests are overkill." | For federation/peer/auth/PII changes, integration tests are Required regardless of unit coverage. Run them. |
| "This Pi hotfix is urgent, just commit to main." | Never. Branch + PR + merge — even for hotfixes (CLAUDE.md). Urgency is not an exception. |
| "Module isn't in the tier table, so it's low-risk." | Unclassified `.py` defaults to **Standard**, not Low — and you must flag it for classification. |
| "Docs are out of scope for this PR." | If you added a module / env var / CLI command / migration / dep, the documentation-update map below is part of the PR. |

## Final steps

- PR body must include `Closes #N` (or `Fixes #N` for bugs).
- After merge: remove the `in-progress` label (`gh issue edit <N>
  --remove-label "in-progress"`).
- PR target is `main`. Title is concise; body has summary + test plan.
