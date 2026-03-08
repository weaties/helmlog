"""Deployment management — version checking, changelog, and upgrade execution.

Handles polling for updates on the subscribed branch, comparing the running
version to the latest available, and executing upgrades (git pull + uv sync +
service restart). GitHub is the source of truth for branch state; this module
only reads from it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class DeployConfig:
    """Deployment configuration from environment variables."""

    mode: str = field(default_factory=lambda: os.environ.get("DEPLOY_MODE", "explicit"))
    branch: str = field(default_factory=lambda: os.environ.get("DEPLOY_BRANCH", "main"))
    poll_interval: int = field(
        default_factory=lambda: int(os.environ.get("DEPLOY_POLL_INTERVAL", "300"))
    )
    window_start: int | None = field(
        default_factory=lambda: _opt_int(os.environ.get("DEPLOY_WINDOW_START"))
    )
    window_end: int | None = field(
        default_factory=lambda: _opt_int(os.environ.get("DEPLOY_WINDOW_END"))
    )
    github_repo: str = field(
        default_factory=lambda: os.environ.get("GITHUB_REPO", "weaties/helmlog")
    )
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))


def _opt_int(val: str | None) -> int | None:
    if val is None or val == "":
        return None
    return int(val)


def _repo_dir() -> str:
    """Return the project root directory."""
    return str(Path(__file__).resolve().parents[2])


def _git(args: list[str]) -> str:
    """Run a git command in the project directory and return stripped stdout."""
    repo = _repo_dir()
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def get_running_version() -> dict[str, str]:
    """Return the currently running commit SHA, branch, and timestamp."""
    try:
        sha = _git(["rev-parse", "HEAD"])
        short_sha = _git(["rev-parse", "--short=7", "HEAD"])
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
        commit_ts = _git(["log", "-1", "--format=%cI"])
        return {
            "sha": sha,
            "short_sha": short_sha,
            "branch": branch,
            "commit_timestamp": commit_ts,
        }
    except Exception:  # noqa: BLE001
        return {"sha": "", "short_sha": "", "branch": "", "commit_timestamp": ""}


async def fetch_latest(config: DeployConfig) -> dict[str, Any] | None:
    """Fetch the latest commit on the subscribed branch from origin.

    Returns None if the fetch fails (offline, no remote, etc.).
    """
    try:
        await asyncio.to_thread(_git, ["fetch", "origin", config.branch])
        sha = _git(["rev-parse", f"origin/{config.branch}"])
        short_sha = _git(["rev-parse", "--short=7", f"origin/{config.branch}"])
        commit_ts = _git(["log", "-1", "--format=%cI", f"origin/{config.branch}"])
        return {"sha": sha, "short_sha": short_sha, "commit_timestamp": commit_ts}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch latest from origin/{}: {}", config.branch, exc)
        return None


async def get_changelog(config: DeployConfig) -> list[dict[str, str]]:
    """Get commits between the running version and origin/{branch}.

    Tries the GitHub API first (richer data — PR numbers, authors), falls back
    to local git log.
    """
    running = get_running_version()
    if not running["sha"]:
        return []

    # Try GitHub API first
    if config.github_token:
        try:
            return await _github_changelog(config, running["sha"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("GitHub API changelog failed, falling back to git log: {}", exc)

    # Fall back to local git log
    return await _git_changelog(config, running["sha"])


async def _github_changelog(config: DeployConfig, from_sha: str) -> list[dict[str, str]]:
    """Fetch changelog from GitHub compare API."""
    import httpx

    url = f"https://api.github.com/repos/{config.github_repo}/compare/{from_sha}...{config.branch}"
    headers = {
        "Authorization": f"Bearer {config.github_token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    commits: list[dict[str, str]] = []
    for c in data.get("commits", []):
        msg = c["commit"]["message"].split("\n")[0]  # first line only
        commits.append(
            {
                "sha": c["sha"][:7],
                "message": msg,
                "author": c["commit"]["author"]["name"],
                "timestamp": c["commit"]["author"]["date"],
            }
        )
    return commits


async def _git_changelog(config: DeployConfig, from_sha: str) -> list[dict[str, str]]:
    """Fetch changelog from local git log."""
    try:
        raw = await asyncio.to_thread(
            _git,
            [
                "log",
                f"{from_sha}..origin/{config.branch}",
                "--format=%H|%s|%an|%cI",
                "--no-merges",
            ],
        )
    except Exception:  # noqa: BLE001
        return []

    if not raw:
        return []

    commits: list[dict[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append(
                {
                    "sha": parts[0][:7],
                    "message": parts[1],
                    "author": parts[2],
                    "timestamp": parts[3],
                }
            )
    return commits


def commits_behind(config: DeployConfig) -> int:
    """Return how many commits the running version is behind origin/{branch}."""
    try:
        running = get_running_version()
        if not running["sha"]:
            return 0
        count = _git(
            [
                "rev-list",
                "--count",
                f"{running['sha']}..origin/{config.branch}",
            ]
        )
        return int(count)
    except Exception:  # noqa: BLE001
        return 0


def in_deploy_window(config: DeployConfig) -> bool:
    """Check if the current UTC hour is within the deploy window.

    Returns True if no window is configured (always deploy).
    """
    if config.window_start is None or config.window_end is None:
        return True
    hour = datetime.now(UTC).hour
    if config.window_start <= config.window_end:
        return config.window_start <= hour < config.window_end
    # Wraps midnight, e.g. 22–06
    return hour >= config.window_start or hour < config.window_end


async def execute_deploy(config: DeployConfig) -> dict[str, Any]:
    """Execute a deployment: git pull, uv sync, restart service.

    Returns a dict with deployment result details.
    """
    from_version = get_running_version()
    repo = _repo_dir()
    now = datetime.now(UTC).isoformat()

    try:
        # git fetch + checkout + pull
        await asyncio.to_thread(_git, ["fetch", "origin", config.branch])
        await asyncio.to_thread(_git, ["checkout", config.branch])
        await asyncio.to_thread(_git, ["pull", "origin", config.branch])

        # uv sync (best-effort — may fail on first run if deps changed)
        try:
            await asyncio.to_thread(
                subprocess.check_output,
                ["uv", "sync", "--no-interaction", "--project", repo],
                cwd=repo,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("uv sync returned non-zero (continuing): {}", exc.output)

        to_version = get_running_version()

        # Restart the service — sudoers allows this without a password
        try:
            await asyncio.to_thread(
                subprocess.check_output,
                ["sudo", "systemctl", "restart", "helmlog"],
                stderr=subprocess.STDOUT,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("Service restart failed (may be dev environment): {}", exc.output)

        return {
            "status": "success",
            "from_sha": from_version["sha"],
            "to_sha": to_version["sha"],
            "timestamp": now,
        }

    except Exception as exc:  # noqa: BLE001
        logger.error("Deployment failed: {}", exc)
        return {
            "status": "failed",
            "from_sha": from_version["sha"],
            "to_sha": "",
            "error": str(exc),
            "timestamp": now,
        }
