---
name: skill-compare
description: Blind A/B comparison of two skill versions using eval cases — measures relative improvement across correctness, completeness, conciseness, and actionability.
---

# /skill-compare — Blind A/B testing

Run the same eval prompts against two skill versions and blind-score
which produces better results. Proves a skill change is an improvement,
not just a change.

## Usage

```
/skill-compare <skill-name>                # current branch vs main
/skill-compare <skill-name> --file         # SKILL.md vs SKILL.md.new
/skill-compare <skill-name> <case-name>    # single named case only
```

If the target has no `evals/cases.yaml`, stop with: "No eval cases
found for <skill>. Run `/skill-eval` to add cases first."

## Why three contexts

One context cannot generate both outputs and then score them blind: it
knows which instructions produced which output, and it has read the
rubric before writing either. So the work is split:

| Role | Sees | Never sees |
|---|---|---|
| **Subject** subagent (one per version, per case) | one version's `SKILL.md`, the case's `scenario` + `mock_input` | the other version, the criteria, the case name |
| **Grader** subagent (one per case) | the two outputs labelled Alpha / Beta, the criteria | either `SKILL.md`, which version is which |
| **Parent** (you) | everything, including the label mapping | — and therefore does no scoring |

## Workflow

### 1. Load the two versions

- Default: `git show main:<path-to-SKILL.md>` vs working-tree file.
- `--file`: `SKILL.md` vs `SKILL.md.new` in the same directory.

If the two versions are identical, stop. Print a unified diff of the two
before running cases, so the user sees what changed.

### 2. For each eval case

a. **Launch two subject subagents**, one per version, with the prompt
   shape from `/skill-eval` step 2a (skill text, scenario, mock input,
   "do not use the Skill tool, reply with your final output only").

b. **Assign labels at random, per case** — Alpha / Beta, never "old/new"
   or "A/B". Keep the mapping to yourself.

c. **Launch a grader subagent** with the two labelled outputs and the
   case's `expected` / `anti_expected`. Ask it to:

   - score each output on four dimensions, 1–5, scoring each output
     fully before looking at the other;

     | Dimension | What to evaluate |
     |---|---|
     | Correctness | Hits `expected`, avoids `anti_expected` |
     | Completeness | All relevant aspects of the scenario covered |
     | Conciseness | Right-sized — neither bloated nor skeletal |
     | Actionability | User can act without further clarification |

   - mark every criterion pass/fail for each output — this separates
     "feels better" from "passes more criteria";
   - name a winner, or a tie when the totals are within 1 point on all
     four dimensions. When in doubt, tie.

### 3. Aggregate and report

Un-blind the labels, then report: cases won per version, ties, dimension
averages, criteria pass rate, and a per-case breakdown. Record the model
ID. Append the summary to `<skill-dir>/evals/compare-log.md` as a
historical record.

### 4. Interpreting results

| Result | Action |
|---|---|
| New version wins ≥70% of cases | Strong improvement — ship it |
| New version wins 50–70% | Marginal — check for trade-offs (e.g., correctness up but conciseness down) |
| Tie within 1 case | A shorter version that ties is a win; a longer one is not worth the diff |
| Old version wins | Regression — revert or rethink |

If the result is a tie but you believe the change is better, the eval
cases may be too coarse — add cases that target the specific
improvement, then re-run.
