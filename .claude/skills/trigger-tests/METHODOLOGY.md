# Skill Trigger Testing Methodology

## Purpose

Measure whether skill descriptions cause correct triggering behavior —
activating when they should (recall) and staying silent when they shouldn't
(precision).

## Test suite

`cases.yaml` contains prompt scenarios with expected trigger outcomes:

- `should_trigger`: skills that MUST activate for this prompt
- `should_not_trigger`: skills that must NOT activate
- `tags`: for filtering by skill or category

## How to run

### Manual spot-check

1. Pick a case from `cases.yaml`
2. Start a fresh conversation and paste the prompt
3. Observe which skills activate (shown in the system reminder)
4. Compare against expected outcomes

### Systematic evaluation

Run each case in a **fresh subagent** (one per prompt, no shared context):
give it only the prompt, let it start the task, and stop it after its first
few tool calls. Record which skills it invoked through the Skill tool and
compare against `should_trigger` / `should_not_trigger`. A fresh subagent
sees the same skill listing a new session does, so this measures the
descriptions rather than the evaluator's memory of them. Never judge
triggering by asking one context "would this trigger?" — that tests the
evaluator, not the description.

Skills with `disable-model-invocation: true` (`deploy-pi`, `new-migration`,
`new-module`) never auto-trigger and are out of scope.

## When to re-run

- After changing any skill description
- After adding a new skill (add trigger test cases for it too)
- Periodically as the codebase evolves (new modules may need new trigger paths)

## Metrics

- **Recall** (false negative rate): % of should_trigger cases that actually triggered.
  Target: > 90%. False negatives are costlier — a missed `/data-license` check
  on a PII endpoint is worse than an unnecessary trigger.
- **Precision** (false positive rate): % of activations that were in should_trigger.
  Target: > 95%. False positives waste context but are less dangerous.

## Adding new cases

Follow the existing format. Every new skill should have at least:
- 2 positive trigger cases (clear activation scenarios)
- 2 negative trigger cases (adjacent but wrong scenarios)
- 1 ambiguous/edge case
