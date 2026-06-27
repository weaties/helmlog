# CLAUDE.md — HelmLog

The canonical project guide for all agents (human or AI) lives in `AGENTS.md`.
It is imported here so Claude Code loads it as project instructions:

@AGENTS.md

Everything in `AGENTS.md` applies. The rest of this file is **only** the
Claude-Code-specific mechanics that implement or extend those rules — keep
project conventions in `AGENTS.md`, not here.

## Claude Code specifics

- **Worktrees.** `AGENTS.md`'s "always work in a git worktree" rule is enforced
  here via the **`EnterWorktree`** tool: before any edit, check
  `git worktree list` and `ls .claude/worktrees/`, then enter an existing
  worktree if the branch matches, otherwise `EnterWorktree` a new one. Read-only
  work doesn't need one.

- **Skills.** The harness lists available skills each session; invoke them with
  the Skill tool (`/name`). The ones that back rules in `AGENTS.md`:
  - `/architecture` — module map, data flow, complexity hotspots, "what changed".
  - `/domain` — sailing instrument tribal knowledge (B&G quirks, J/105 polar
    targets, miscalibration symptoms) not grep-recoverable from the code.
  - `/data-license` — review changes against `docs/data-licensing.md`.
  - `/spec` — structured spec (decision table / state diagram / EARS) for
    Critical- and High-tier features.
  - `/tdd` — HelmLog test patterns + the ruff/mypy pre-existing-error allowlist.
  - `/integration-test` — pick the federation test layer (in-process / Pi / Docker).
  - `/diagnose` — systematic Pi troubleshooting runbook.
  - `/release-notes` — draft the `RELEASES.md` entry the promote gate requires.
  - `/debrief`, `/ocs-check`, `/pr-checklist`, `/ideate` — race + workflow helpers.

- **Memory.** File-based memory persists across sessions under
  `~/.claude/projects/-Users-dweatbrook-src-helmlog/memory/`, indexed by
  `MEMORY.md`. Save durable facts there (user prefs, project state, references);
  don't restate what the repo or `AGENTS.md` already records.
