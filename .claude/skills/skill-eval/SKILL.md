---
name: skill-eval
description: Run evaluation test cases against a skill to measure quality, detect regressions, and benchmark performance. TRIGGER when the user asks to test or evaluate a skill, after refining a skill description, or when checking skill quality. DO NOT trigger for running pytest (use /tdd or /integration-test), general code review, or non-skill-related testing.
---

# Skill Evaluation Runner

Run test cases against a skill to measure whether it produces correct, consistent
output. Use this to establish baselines, detect regressions after model or skill
changes, and decide whether a skill still earns its place.

## Usage

```
/skill-eval <skill-name>           # run all evals for a skill
/skill-eval <skill-name> <case>    # run a single named case
/skill-eval <skill-name> --bare    # also run each case with NO skill (baseline)
/skill-eval all                    # run evals for all skills that have them
```

## How It Works

Each skill can have test cases in `<skill-dir>/evals/cases.yaml`. Each case
defines a scenario, the input the skill would receive, and criteria for judging
the output.

**Every case runs in a fresh subagent.** The context that reads the rubric must
not be the context that produces the answer: an evaluator that has already seen
`expected` will write output that satisfies it, and the eval passes
tautologically. So:

- the **subject** subagent gets the skill text and the case's `scenario` +
  `mock_input`, and never sees `expected` / `anti_expected`;
- the **parent** (you) scores the returned output against the criteria.

Do not "mentally apply" a skill in your own context and score the result. That
measures nothing.

## Workflow

### 1. Load skill and cases

Read the target skill's `SKILL.md` and its `evals/cases.yaml`. If no cases
exist, report "No eval cases found for <skill>" and stop.

### 2. For each test case

**a. Launch the subject subagent** with a prompt containing, in this order:

1. the full text of the skill's `SKILL.md`, introduced as "instructions you have
   been given for this kind of task";
2. the case's `scenario`;
3. the case's `mock_input`, as the request to act on;
4. "Do not use the Skill tool. Reply with your final output only."

Do not include the case name, description, tags or any criterion — they leak the
answer. Run independent cases in parallel.

**b. With `--bare`, launch a second subagent** for the same case with item 1
removed, plus "Do not read anything under `.claude/skills/`." It still has the
repo and `AGENTS.md`. This is the baseline: what the model does with no skill.

**c. Score against criteria.** Check each returned output against every
criterion in `expected` (must be present) and `anti_expected` (must not be).
Score each criterion pass or fail, quoting the part of the output that decided
it.

**d. Record the result:** case name, pass/fail, which criteria passed, which
failed, and a brief note on any failure.

### 3. Report results

```
## Skill Eval: <skill-name>
Date: <date>   Model: <model id>

| Case | With skill | Bare | Notes |
|---|---|---|---|
| case-name-1 | PASS 4/4 | PASS 4/4 | skill adds nothing here |
| case-name-2 | PASS 3/3 | FAIL 2/3 | bare missed: must flag PII exposure |

**Overall: 5/7 criteria passed with skill (71%); 4/7 bare (57%)**

### Failures
- case-name-2 / criterion "must flag PII exposure" (bare):
  Output did not mention PII or personal data in the context of crew emails.
```

Always record the model ID: results are model-dependent, and the reason to
re-run is usually a model change. Omit the Bare column when `--bare` was not
requested.

A case passes only if ALL its `critical` `expected` criteria pass and NONE of
its `critical` `anti_expected` criteria trigger.

### 4. Reading a bare run

| Result | Meaning |
|---|---|
| Passes with skill, fails bare | The skill is load-bearing for this case. Keep it. |
| Passes both | The case does not test the skill — the model, `AGENTS.md` and the repo already cover it. If every case passes bare, the skill is a candidate for slimming or removal (#692). |
| Fails with skill, passes bare | The skill is making things worse: stale or wrong content. Fix or delete that part. |
| Fails both | The case may be ambiguous, or the skill does not actually encode the answer. |

A skill that passes bare can still be worth keeping for its *trigger* — a
reminder that fires at the right moment — but then its body should be short.

### 5. Benchmark mode

When running all cases for a skill, also report:
- **Pass rate:** percentage of cases that fully passed
- **Criteria hit rate:** percentage of individual criteria met across all cases
- **Consistency notes:** any borderline cases. Re-run those two or three times
  in fresh subagents and report the spread rather than a single verdict.

## Test Case Format

Cases live in `<skill-dir>/evals/cases.yaml`:

```yaml
- name: descriptive-kebab-case-name
  description: What this test verifies (one line)
  type: capability | preference | trigger
  scenario: |
    Multi-line description of the setup context.
    What files exist, what the project state is, what just happened.
  mock_input: |
    The specific input the skill would process — a diff, an issue body,
    a user prompt, etc. This is what the skill "sees."
  expected:
    - criterion: "Description of what the output MUST contain or do"
      weight: critical | important | nice-to-have
    - criterion: "Another required element"
      weight: critical
  anti_expected:
    - criterion: "Description of what the output must NOT contain or do"
      weight: critical
  tags: [optional, categorization, tags]
```

### Weight definitions

| Weight | Meaning | Scoring |
|---|---|---|
| `critical` | Missing this means the skill fundamentally failed | Case fails |
| `important` | Should be present for a good result | Noted in report |
| `nice-to-have` | Would improve the result but not essential | Noted if missing |

### Test case types

| Type | What it tests | Example |
|---|---|---|
| `capability` | Can the skill produce correct output for this scenario? | Does `/data-license` catch a PII leak? |
| `preference` | Does the skill follow the encoded workflow correctly? | Does `/pr-checklist` resolve the tier correctly? |
| `trigger` | Does the skill activate (or stay silent) for this prompt? | Does `/pr-checklist` trigger before PR creation? |

`trigger` cases need a subagent that is given only the prompt and no skill
text — see `../trigger-tests/METHODOLOGY.md`.

## Adding Evals to a New Skill

When creating a new skill, add at least 3 eval cases:
1. One **happy path** — a clear-cut scenario where the skill should succeed
2. One **edge case** — a tricky scenario that tests the skill's boundaries
3. One **negative case** — a scenario where the skill should correctly decline
   or produce a "no action needed" result

Then run them with `--bare`. A new skill whose cases all pass bare has not yet
shown that it is needed.

## Tips

- Cases should be **self-contained** — all context needed is in `scenario` and
  `mock_input`. Mock diffs drift from the real code over time (file names,
  helper names), and a subagent with repo access will notice; refresh a case
  when its mock no longer resembles the codebase.
- Write cases that test the **skill's instructions**, not general model
  capability. The `--bare` run is how you find out which you have.
- Review flaky cases — if a case sometimes passes and sometimes fails, the
  criterion may be too vague or the scenario too ambiguous.
- When a real-world skill failure occurs, capture it as a new eval case to
  prevent regression.
