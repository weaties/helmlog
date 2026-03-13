---
name: release-notes
description: Draft a curated RELEASES.md entry from commits since the last stage/* tag
---

# Release Notes: Draft Entry

Draft a new RELEASES.md entry summarizing commits on `main` since the latest
`stage/*` tag. Run this skill before promoting `main` to `stage`.

## 1. Find the Commit Range

Run `git tag -l 'stage/*' --sort=-creatordate | head -1` to find the latest
stage tag. If no stage tags exist, use the initial commit as the base.

Save the tag as `$LAST_TAG` and collect the commit range:

```bash
git log --oneline "$LAST_TAG"..HEAD
```

## 2. Filter Out Ideation-Log-Only Commits

For each commit in the range, check which files it touches:

```bash
git diff-tree --no-commit-id --name-only -r <sha>
```

- **Exclude entirely:** Commits where every changed file is `docs/ideation-log.md`
- **Include (code changes only):** Commits that touch `docs/ideation-log.md` AND
  other files — describe only the non-ideation changes

If no non-ideation commits remain after filtering, tell the user:
> All commits since the last stage tag only touch the ideation log — nothing to
> document. The promote workflow will allow this through automatically.

And stop here.

## 3. Analyze Changes and Group by Theme

For each included commit, examine:
- The commit message and any PR title (from `(#NNN)` references)
- The files changed and the nature of the diff

Group changes into logical themes (e.g., "Performance analysis", "Synthesizer
improvements", "Deploy & infrastructure", "Bug fixes"). Use your judgment — aim
for 2–5 groups. A single-commit release can have just one group.

## 4. Draft the RELEASES.md Entry

Read `RELEASES.md` to match the existing format. Draft a new entry following
this pattern:

```markdown
## Title — Description (YYYY-MM-DD)

Optional 1–2 sentence summary of the release.

### Theme group
- **Feature name** (#issue) — one-line description
- **Feature name** (#issue) — one-line description with enough context to
  understand the change without reading the code
```

Rules:
- Use today's date (YYYY-MM-DD format)
- Bold feature names, reference issue/PR numbers as `(#NNN)`
- One bullet per logical change — merge related commits into a single bullet
- Use sub-bullets only when a change needs a brief clarification
- Do NOT mention ideation log updates anywhere in the entry

## 5. Insert into RELEASES.md

Prepend the new entry immediately after the `# Release Notes` heading (line 1),
with a blank line separating it from the next entry. Do not modify existing
entries.

## 6. Present for Review

Show the user the drafted entry and ask them to review and edit before
committing. Do NOT commit automatically — the user will commit when satisfied.
