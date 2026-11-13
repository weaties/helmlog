# CLAUDE.md — HelmLog

The canonical project guide for all agents (human or AI) lives in `AGENTS.md`.
It is imported here so Claude Code loads it as project instructions:

@AGENTS.md

Everything in `AGENTS.md` applies. The rest of this file is **only** the
Claude-Code-specific mechanics that implement or extend those rules — keep
project conventions in `AGENTS.md`, not here.

## Claude Code specifics

- **Worktrees.** Before any edit, check `git worktree list`, then
  `EnterWorktree` — `path=` for an existing worktree whose branch matches the
  task, `name=` for a new one. A `PreToolUse` hook
  (`.claude/hooks/require_worktree.py`, wired in `.claude/settings.json`)
  blocks Edit/Write on files in the primary checkout. If it fires, enter a
  worktree; don't route around it with a shell edit.

- **Skills.** `/name` in `AGENTS.md` refers to a skill under `.claude/skills/`.
  The harness lists every skill with its description each session, so they are
  not catalogued here. `/deploy-pi`, `/new-migration` and `/new-module` are
  manual-only (`disable-model-invocation`) — invoke them by name when relevant.

- **Memory is private; the repo is shared.** File-based memory is per-user and
  invisible to other agents and tools. A gotcha or convention that another
  agent working here would need belongs in the repo — a skill, `AGENTS.md`, or
  `docs/` — not only in memory.
