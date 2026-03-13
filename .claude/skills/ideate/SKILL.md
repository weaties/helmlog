---
name: ideate
description: Capture half-baked ideas into the ideation log for future reference
---

# Ideation Capture: `$ARGUMENTS`

Capture ideas from the current conversation into `docs/ideation-log.md`. Ideas
are too early for GitHub issues but worth recording so they aren't lost.

## 1. Read the Current Ideation Log

Read `docs/ideation-log.md` to understand existing entries and find the next
available `IDX-NNN` number.

## 2. Check for Related or Conflicting Ideas

Scan existing entries for ideas that:

- **Overlap** with the new idea — consider merging or cross-referencing
- **Conflict** with the new idea — note the conflict in both entries
- **Could be superseded** by the new idea — if so, update the old entry's
  status to `superseded` and link to the new entry

Report any related ideas found before adding the new entry.

## 3. Assess Maturity

Before adding, evaluate whether the idea is actually mature enough to skip
the ideation log and go straight to a GitHub issue. Signs of maturity:

- [ ] Clear scope — you can describe what "done" looks like
- [ ] No unresolved design questions
- [ ] No blocking dependencies on other unbuilt features
- [ ] Estimable effort (even roughly)

If all four are true, suggest promoting directly to an issue instead of
logging as an idea. Otherwise, proceed with capture.

## 4. Add the Entry

Append a new entry to `docs/ideation-log.md` using this template:

```markdown
---

## IDX-NNN: <Title>

- **Date captured:** YYYY-MM-DD
- **Origin:** <conversation context where this came up>
- **Status:** `raw`
- **Related:** <related features, docs, or issue numbers>

**Description:**
<What the idea is and why it matters. Include enough context that someone
reading this months later understands the thinking.>

**Notes:**
- *YYYY-MM-DD:* Initial capture. <Any immediate observations or constraints.>
```

## 5. Update Existing Entries (if requested)

When asked to update an existing idea:

- **Evolving:** Change status to `evolving` and add a dated note explaining
  what changed or what new thinking emerged.
- **Superseded:** Change status to `superseded` and add a note linking to
  the replacement (another IDX entry or a GitHub issue).
- **Promoted:** Change status to `promoted`, add the GitHub issue number(s),
  and add a dated note. Create the GitHub issue(s) using `gh issue create`.

## 6. Commit

Commit the changes with a message like:
`docs: capture idea IDX-NNN — <short title>`

Or for updates:
`docs: update IDX-NNN status to <new-status>`
