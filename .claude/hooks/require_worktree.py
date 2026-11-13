#!/usr/bin/env python3
"""Claude Code PreToolUse hook: enforce "always work in a git worktree".

AGENTS.md requires every edit to happen in a linked worktree, because several
agents share this repository and a deploy can pick up a half-finished change
from the primary checkout. This hook turns that rule into a check: it blocks
Edit / Write / NotebookEdit when the target file is in the primary checkout of
this repository.

Allowed without comment:
  - files in a linked worktree (`.claude/worktrees/<name>/...`)
  - gitignored files in the primary checkout (`.env`, `data/`, local settings)
  - files outside this repository (memory, scratch files, other repos)

Protocol: the harness sends the tool call as JSON on stdin. Exit 0 allows the
call. Exit 2 blocks it and shows stderr to the agent. The hook fails open — any
unexpected error allows the edit, so a broken hook can never brick editing.

Deliberate primary-checkout edit: set HELMLOG_ALLOW_PRIMARY_EDITS=1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ALLOW = 0
BLOCK = 2
OVERRIDE_ENV = "HELMLOG_ALLOW_PRIMARY_EDITS"


def _git(cwd: Path, *args: str) -> str | None:
    """Run a git query in `cwd`. Return stripped stdout, or None if git fails."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _nearest_existing_dir(path: Path) -> Path | None:
    for candidate in path.parents:
        if candidate.is_dir():
            return candidate
    return None


def _git_dirs(cwd: Path) -> tuple[Path, Path] | None:
    """Return (git_dir, common_dir) for the checkout containing `cwd`, resolved."""
    git_dir = _git(cwd, "rev-parse", "--git-dir")
    common_dir = _git(cwd, "rev-parse", "--git-common-dir")
    if git_dir is None or common_dir is None:
        return None
    return (cwd / git_dir).resolve(), (cwd / common_dir).resolve()


def block_reason(file_path: str, cwd: str) -> str | None:
    """Return why this edit must be blocked, or None if it is allowed."""
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    target_dir = _nearest_existing_dir(path)
    if target_dir is None:
        return None

    target = _git_dirs(target_dir)
    if target is None:
        return None  # not inside any git repository
    git_dir, common_dir = target
    if git_dir != common_dir:
        return None  # a linked worktree — exactly where edits belong

    own = _git_dirs(Path(__file__).resolve().parent)
    if own is None or own[1] != common_dir:
        return None  # a different repository; this rule is not ours to enforce there

    top = _git(target_dir, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(path)],
        cwd=top,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if ignored.returncode == 0:
        return None  # gitignored: local config or data, never committed

    return (
        f"Blocked: {path} is in the primary checkout ({top}).\n"
        "AGENTS.md: always work in a git worktree — other agents share this checkout.\n"
        "Enter a worktree (Claude Code: the EnterWorktree tool; otherwise "
        "`git worktree add .claude/worktrees/<name> -b <branch>`) and edit the file there.\n"
        f"For a deliberate primary-checkout edit, set {OVERRIDE_ENV}=1."
    )


def main() -> int:
    if os.environ.get(OVERRIDE_ENV) == "1":
        return ALLOW
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(file_path, str) or not file_path:
            return ALLOW
        reason = block_reason(file_path, str(payload.get("cwd") or Path.cwd()))
    except Exception:  # noqa: BLE001 — fail open: never brick editing on a hook bug
        return ALLOW
    if reason is None:
        return ALLOW
    print(reason, file=sys.stderr)
    return BLOCK


if __name__ == "__main__":
    sys.exit(main())
