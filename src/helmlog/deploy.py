"""Deployment management — version checking, changelog, and upgrade execution.

Handles polling for updates on the subscribed branch, comparing the running
version to the latest available, and executing upgrades (git pull + uv sync +
service restart). GitHub is the source of truth for branch state; this module
only reads from it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage


@dataclass
class DeployConfig:
    """Deployment configuration — DB overrides → env vars → defaults."""

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

    @staticmethod
    async def from_storage(storage: Storage) -> DeployConfig:
        """Build config with DB overrides taking priority over env vars."""
        from helmlog.storage import get_effective_setting

        config = DeployConfig()
        mode = await get_effective_setting(storage, "DEPLOY_MODE")
        if mode:
            config.mode = mode
        branch = await get_effective_setting(storage, "DEPLOY_BRANCH")
        if branch:
            config.branch = branch
        poll = await get_effective_setting(storage, "DEPLOY_POLL_INTERVAL")
        if poll:
            config.poll_interval = int(poll)
        ws = await storage.get_setting("DEPLOY_WINDOW_START")
        if ws is not None:
            config.window_start = _opt_int(ws)
        we = await storage.get_setting("DEPLOY_WINDOW_END")
        if we is not None:
            config.window_end = _opt_int(we)
        return config


def _opt_int(val: str | None) -> int | None:
    if val is None or val == "":
        return None
    return int(val)


def _repo_dir() -> str:
    """Return the project root directory."""
    return str(Path(__file__).resolve().parents[2])


def _uv_bin() -> str:
    """Return the full path to the uv binary.

    Under systemd, ~/.local/bin is not in PATH, so we resolve it explicitly.
    """
    found = shutil.which("uv")
    if found:
        return found
    # Common install location on Pi (installed by setup.sh for the deploy user)
    home_local = Path.home() / ".local" / "bin" / "uv"
    if home_local.exists():
        return str(home_local)
    # helmlog service account fallback
    svc_local = Path("/home/helmlog/.local/bin/uv")
    if svc_local.exists():
        return str(svc_local)
    # Search common home dirs on the Pi
    for user_dir in Path("/home").iterdir():
        candidate = user_dir / ".local" / "bin" / "uv"
        if candidate.exists():
            return str(candidate)
    return "uv"  # last resort — let it fail with a clear error


def _repo_owner() -> str:
    """Return the username that owns the project directory.

    Git operations that write to .git/ must run as this user to avoid
    ownership conflicts between the service account and the deploy user.
    """
    repo = _repo_dir()
    st = os.stat(repo)
    try:
        import pwd

        return pwd.getpwuid(st.st_uid).pw_name
    except (ImportError, KeyError):
        return str(st.st_uid)


def _git(args: list[str], *, write: bool = False) -> str:
    """Run a git command in the project directory and return stripped stdout.

    When *write* is False (default) ``--no-optional-locks`` is added so that
    read-only commands do not refresh the index or create lock files.

    When *write* is True the command is executed via ``sudo -u <owner>`` if
    the current process is not the repo owner, preventing .git/ files from
    being created with the wrong ownership.
    """
    repo = _repo_dir()
    git_base = ["git", "-c", f"safe.directory={repo}"]

    if write:
        owner = _repo_owner()
        current_user = os.environ.get("USER", "")
        if not current_user:
            try:
                import pwd

                current_user = pwd.getpwuid(os.getuid()).pw_name
            except (ImportError, KeyError):
                current_user = str(os.getuid())
        if current_user != owner:
            cmd = ["sudo", "-n", "-u", owner, *git_base, *args]
        else:
            cmd = [*git_base, *args]
    else:
        cmd = [*git_base, "--no-optional-locks", *args]

    return subprocess.check_output(
        cmd,
        cwd=repo,
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


async def list_remote_branches() -> list[str]:
    """Return sorted list of remote branch names from origin."""
    try:
        await asyncio.to_thread(_git, ["fetch", "--prune", "origin"], write=True)
        raw = await asyncio.to_thread(_git, ["branch", "-r", "--format=%(refname:short)"])
    except Exception:  # noqa: BLE001
        return []
    branches: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("origin/") and not line.endswith("/HEAD"):
            branches.append(line.removeprefix("origin/"))
    return sorted(branches)


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
        await asyncio.to_thread(_git, ["fetch", "origin", config.branch], write=True)
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


async def get_pipeline_status() -> dict[str, Any]:
    """Return the current HEAD of main, stage, and live with commit gap counts.

    Uses remote refs (origin/*) so data is current after a fetch.  Returns a
    dict with ``branches`` (per-branch HEAD info) and ``gaps`` (commit counts
    between tiers).
    """

    def _pipeline() -> dict[str, Any]:
        # Ensure we have fresh remote refs
        import contextlib

        with contextlib.suppress(Exception):
            _git(["fetch", "--prune", "origin"], write=True)

        tiers = ["main", "stage", "live"]
        branches: dict[str, dict[str, str] | None] = {}
        for tier in tiers:
            try:
                sha = _git(["rev-parse", f"origin/{tier}"])
                short = _git(["rev-parse", "--short=7", f"origin/{tier}"])
                msg = _git(["log", "-1", "--format=%s", f"origin/{tier}"])
                ts = _git(["log", "-1", "--format=%cI", f"origin/{tier}"])
                branches[tier] = {"sha": sha, "short_sha": short, "message": msg, "timestamp": ts}
            except Exception:  # noqa: BLE001
                branches[tier] = None

        gaps: dict[str, int | None] = {}
        for ahead, behind in [("main", "stage"), ("stage", "live")]:
            if branches[ahead] and branches[behind]:
                try:
                    count = int(_git(["rev-list", "--count", f"origin/{behind}..origin/{ahead}"]))
                    gaps[f"{ahead}_ahead_of_{behind}"] = count
                except Exception:  # noqa: BLE001
                    gaps[f"{ahead}_ahead_of_{behind}"] = None
            else:
                gaps[f"{ahead}_ahead_of_{behind}"] = None

        return {"branches": branches, "gaps": gaps}

    return await asyncio.to_thread(_pipeline)


async def get_promotion_history(tier: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent promotion tags (``stage/*`` and ``live/*``) with metadata.

    Each entry includes the tag name, promotion timestamp, target SHA, commit
    message, and — when consecutive tags exist — the commit range and count.
    """

    def _history() -> list[dict[str, Any]]:
        patterns: list[str] = []
        if tier in (None, "all", "stage"):
            patterns.extend(["--list", "stage/*"])
        if tier in (None, "all", "live"):
            patterns.extend(["--list", "live/*"])
        if not patterns:
            return []

        try:
            raw = _git(["tag", *patterns, "--sort=-creatordate"])
        except Exception:  # noqa: BLE001
            return []

        if not raw:
            return []

        tags = raw.splitlines()[:limit]
        result: list[dict[str, Any]] = []

        for tag_name in tags:
            tag_name = tag_name.strip()
            if not tag_name:
                continue
            entry: dict[str, Any] = {"tag": tag_name}
            # Determine tier from prefix
            entry["tier"] = "stage" if tag_name.startswith("stage/") else "live"
            # Extract timestamp from tag name (e.g. stage/2026-03-10T22.37.30Z)
            ts_part = tag_name.split("/", 1)[1] if "/" in tag_name else ""
            entry["timestamp"] = ts_part.replace(".", ":").rstrip("Z") + "Z" if ts_part else ""

            # Get the commit the tag points to
            try:
                sha = _git(["rev-list", "-1", tag_name])
                short = _git(["rev-parse", "--short=7", sha])
                msg = _git(["log", "-1", "--format=%s", sha])
                entry["sha"] = sha
                entry["short_sha"] = short
                entry["message"] = msg
            except Exception:  # noqa: BLE001
                entry["sha"] = ""
                entry["short_sha"] = ""
                entry["message"] = ""

            # Try to get tagger info from annotated tags
            try:
                tagger = _git(["tag", "-l", tag_name, "--format=%(taggername)"])
                entry["triggered_by"] = tagger if tagger else ""
            except Exception:  # noqa: BLE001
                entry["triggered_by"] = ""

            result.append(entry)

        # Add commit range between consecutive same-tier tags
        by_tier: dict[str, list[dict[str, Any]]] = {}
        for entry in result:
            by_tier.setdefault(entry["tier"], []).append(entry)

        for tier_entries in by_tier.values():
            for i, entry in enumerate(tier_entries):
                if i + 1 < len(tier_entries) and entry["sha"] and tier_entries[i + 1]["sha"]:
                    prev_sha = tier_entries[i + 1]["sha"]
                    entry["from_sha"] = prev_sha[:7]
                    try:
                        count = int(_git(["rev-list", "--count", f"{prev_sha}..{entry['sha']}"]))
                        entry["commit_count"] = count
                    except Exception:  # noqa: BLE001
                        entry["commit_count"] = None
                else:
                    entry["from_sha"] = ""
                    entry["commit_count"] = None

        return result

    return await asyncio.to_thread(_history)


async def get_pending_changes(from_tier: str, to_tier: str) -> list[dict[str, str]]:
    """Return commits on *to_tier* not yet on *from_tier*.

    For example ``get_pending_changes("stage", "main")`` returns commits on
    ``origin/main`` that are not on ``origin/stage``.
    """

    def _pending() -> list[dict[str, str]]:
        try:
            raw = _git(
                [
                    "log",
                    f"origin/{from_tier}..origin/{to_tier}",
                    "--format=%H|%s|%an|%cI",
                    "--no-merges",
                ]
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

    return await asyncio.to_thread(_pending)


async def execute_deploy(config: DeployConfig) -> dict[str, Any]:
    """Execute a deployment: git pull, uv sync, restart service.

    Returns a dict with deployment result details.
    """
    from_version = get_running_version()
    repo = _repo_dir()
    now = datetime.now(UTC).isoformat()

    try:
        # git fetch + checkout + pull (as repo owner to preserve .git/ ownership)
        await asyncio.to_thread(_git, ["fetch", "origin", config.branch], write=True)
        await asyncio.to_thread(_git, ["checkout", config.branch], write=True)
        await asyncio.to_thread(_git, ["pull", "origin", config.branch], write=True)

        # uv sync (best-effort — may fail on first run if deps changed)
        uv = _uv_bin()
        logger.info("Using uv binary: {}", uv)
        try:
            await asyncio.to_thread(
                subprocess.check_output,
                [uv, "sync", "--no-interaction", "--project", repo],
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
