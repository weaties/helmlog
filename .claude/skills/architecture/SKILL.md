---
name: architecture
description: Codebase comprehension and complexity tracking — module map, data flow, recent changes, and complexity hotspots. Run with no arguments for a full snapshot or with a date/commit/tag for a delta briefing. TRIGGER when the user asks for a system overview, wants to understand the architecture, asks "what changed while I was away", or needs to orient before a large task. DO NOT trigger for specific code questions, bug fixes, or implementation tasks — those are better served by reading the relevant module directly.
---

# /architecture — Codebase Briefing

Produce a concise, scannable system overview. The methodology (which files
to count, which imports to grep, how to derive responsibilities) is left to
the model — this skill specifies the **output shape and conventions**, not
the steps to get there.

## Mode

- No `$ARGUMENTS` → **full snapshot**.
- `$ARGUMENTS` is a date / commit SHA / tag → **delta briefing** since that
  ref. If ambiguous, ask.

## Severity thresholds (use these exact labels, not synonyms)

The 200-line module convention from AGENTS.md applies. Classify hotspots:

| Severity | Lines | Stance |
|---|---|---|
| **Watch**   | 200–300 | Note, may be fine if cohesive |
| **Warning** | 300–500 | Recommend reviewing for split opportunities |
| **Alert**   | 500+    | Strongly recommend splitting |

## Risk-tier escalation rule

A complexity hotspot in a Critical or High tier module is more urgent than
the same severity in a Standard module — call this out explicitly. The
canonical risk-tier mapping is in `docs/risk-tiers.md`; do not re-state it,
just apply it.

## Output: full snapshot

Target 80–150 lines. Structure:

```
## Module Map
<table grouped by risk tier (Critical → High → Standard → Low/Unclassified):
 module · lines · one-line responsibility · key helmlog-internal deps>

## Data Flow
<ASCII diagram derived from actual imports — instrument ingest, storage
 reads, federation, audio pipeline, external fetches>

## Complexity Hotspots
<table: module · lines · severity · risk tier · notes — Critical/High
 tier hotspots called out as more urgent>

## Recommendations
<2–5 bullets, most actionable observations>
```

## Output: delta briefing

Target 30–80 lines. Show only what changed; do not produce a full snapshot.

```
## Changes Since <ref> (<date>)
<commit count, commit-shape breakdown by feat/fix/docs/perf/refactor>

## Structural Changes
<new/removed modules; modules with significant line-count changes;
 any module that crossed the 200-line threshold in either direction>

## Change Clustering
<files appearing in many commits — complexity magnets>

## Affected Data Flow
<which connections were added/removed/altered>

## Recommendations
<2–5 bullets>
```

## Out of scope

This skill does not run lint/type/tests, modify code, or open issues/PRs.
Its job is to orient — to tell the reader *where* to read, not to replace
reading.
