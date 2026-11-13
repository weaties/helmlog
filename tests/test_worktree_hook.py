"""Tests for the Claude Code PreToolUse hook that enforces the worktree rule.

The hook (`.claude/hooks/require_worktree.py`) blocks Edit/Write on files in the
primary checkout and allows everything else. It is exercised as a subprocess,
the way the harness runs it: JSON on stdin, exit code 2 means "block".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_SRC = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "require_worktree.py"
BLOCK = 2
ALLOW = 0


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A primary checkout with the hook installed, an ignored dir, and one linked worktree."""
    primary = tmp_path / "primary"
    (primary / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy(HOOK_SRC, primary / ".claude" / "hooks" / "require_worktree.py")
    (primary / ".gitignore").write_text("data/\n.claude/worktrees/\n")
    (primary / "tracked.py").write_text("x = 1\n")
    (primary / "data").mkdir()
    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "init")
    _git(primary, "worktree", "add", "-q", ".claude/worktrees/feature", "-b", "feature")
    return primary


def _run(
    repo: Path,
    file_path: Path | str,
    *,
    tool: str = "Edit",
    key: str = "file_path",
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "cwd": str(cwd or repo),
        "tool_input": {key: str(file_path)},
    }
    env = {k: v for k, v in os.environ.items() if k != "HELMLOG_ALLOW_PRIMARY_EDITS"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(repo / ".claude" / "hooks" / "require_worktree.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_blocks_tracked_file_in_primary_checkout(repo: Path) -> None:
    result = _run(repo, repo / "tracked.py")
    assert result.returncode == BLOCK
    assert "worktree" in result.stderr


def test_blocks_new_file_in_primary_checkout(repo: Path) -> None:
    result = _run(repo, repo / "src" / "new_module.py", tool="Write")
    assert result.returncode == BLOCK


def test_blocks_relative_path_resolved_against_cwd(repo: Path) -> None:
    result = _run(repo, "tracked.py", cwd=repo)
    assert result.returncode == BLOCK


def test_blocks_notebook_edit_in_primary_checkout(repo: Path) -> None:
    result = _run(repo, repo / "analysis.ipynb", tool="NotebookEdit", key="notebook_path")
    assert result.returncode == BLOCK


def test_allows_file_in_linked_worktree(repo: Path) -> None:
    worktree = repo / ".claude" / "worktrees" / "feature"
    assert _run(repo, worktree / "tracked.py").returncode == ALLOW
    assert _run(repo, worktree / "src" / "new_module.py", tool="Write").returncode == ALLOW


def test_allows_gitignored_file_in_primary_checkout(repo: Path) -> None:
    assert _run(repo, repo / "data" / "notes.txt", tool="Write").returncode == ALLOW


def test_allows_file_outside_any_repo(repo: Path, tmp_path: Path) -> None:
    assert _run(repo, tmp_path / "scratch" / "notes.md", tool="Write").returncode == ALLOW


def test_allows_file_in_a_different_repo(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("y = 2\n")
    _git(other, "init", "-q", "-b", "main")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "init")
    assert _run(repo, other / "a.py").returncode == ALLOW


def test_env_override_allows_primary_edit(repo: Path) -> None:
    result = _run(repo, repo / "tracked.py", env_extra={"HELMLOG_ALLOW_PRIMARY_EDITS": "1"})
    assert result.returncode == ALLOW


def test_malformed_input_fails_open(repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(repo / ".claude" / "hooks" / "require_worktree.py")],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == ALLOW
