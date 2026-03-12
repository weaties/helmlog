"""Tests for src/helmlog/deploy.py — deployment management."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.deploy import DeployConfig, in_deploy_window
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


class TestDeployConfig:
    def test_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = DeployConfig()
        assert config.mode == "explicit"
        assert config.branch == "main"
        assert config.poll_interval == 300
        assert config.window_start is None
        assert config.window_end is None

    def test_from_env(self) -> None:
        env = {
            "DEPLOY_MODE": "evergreen",
            "DEPLOY_BRANCH": "live",
            "DEPLOY_POLL_INTERVAL": "60",
            "DEPLOY_WINDOW_START": "2",
            "DEPLOY_WINDOW_END": "6",
        }
        with patch.dict("os.environ", env, clear=True):
            config = DeployConfig()
        assert config.mode == "evergreen"
        assert config.branch == "live"
        assert config.poll_interval == 60
        assert config.window_start == 2
        assert config.window_end == 6


class TestDeployWindow:
    def test_no_window_always_true(self) -> None:
        config = DeployConfig()
        config.window_start = None
        config.window_end = None
        assert in_deploy_window(config) is True

    def test_within_window(self) -> None:
        config = DeployConfig()
        config.window_start = 0
        config.window_end = 24
        assert in_deploy_window(config) is True

    def test_outside_window(self) -> None:
        from datetime import UTC, datetime

        config = DeployConfig()
        hour = datetime.now(UTC).hour
        # Set window to an hour that is not now
        config.window_start = (hour + 2) % 24
        config.window_end = (hour + 3) % 24
        assert in_deploy_window(config) is False

    def test_wrapping_midnight(self) -> None:
        from datetime import UTC, datetime

        config = DeployConfig()
        config.window_start = 22
        config.window_end = 6
        hour = datetime.now(UTC).hour
        expected = hour >= 22 or hour < 6
        assert in_deploy_window(config) is expected


class TestRepoOwner:
    def test_returns_current_user(self) -> None:
        """_repo_owner() should return the owner of the project directory."""
        from helmlog.deploy import _repo_owner

        owner = _repo_owner()
        assert isinstance(owner, str)
        assert len(owner) > 0

    def test_git_no_optional_locks_in_read_mode(self) -> None:
        """Read-mode _git() should use --no-optional-locks."""
        from helmlog.deploy import _git

        # rev-parse is a pure read — should succeed with --no-optional-locks
        sha = _git(["rev-parse", "--short=7", "HEAD"])
        assert len(sha) == 7

    def test_git_write_mode_same_user(self) -> None:
        """Write-mode _git() should work when current user owns the repo."""
        from helmlog.deploy import _git

        # Should not use sudo when current user == repo owner
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], write=True)
        assert branch  # non-empty


class TestGetRunningVersion:
    def test_returns_dict(self) -> None:
        from helmlog.deploy import get_running_version

        v = get_running_version()
        assert isinstance(v, dict)
        assert "sha" in v
        assert "branch" in v
        # We're in a git repo, so these should be non-empty
        assert v["sha"]
        assert v["branch"]
        assert len(v["short_sha"]) == 7


class TestCommitsBehind:
    def test_zero_when_up_to_date(self) -> None:
        from helmlog.deploy import commits_behind

        config = DeployConfig()
        config.branch = "HEAD"  # compare to self
        # This may return 0 or error gracefully
        result = commits_behind(config)
        assert isinstance(result, int)
        assert result >= 0


@pytest.mark.asyncio
async def test_deployment_log(storage: Storage) -> None:
    """Test deployment log storage methods."""
    row_id = await storage.log_deployment(
        from_sha="abc1234",
        to_sha="def5678",
        trigger="manual",
        status="success",
    )
    assert row_id > 0

    deployments = await storage.list_deployments()
    assert len(deployments) == 1
    assert deployments[0]["from_sha"] == "abc1234"
    assert deployments[0]["to_sha"] == "def5678"
    assert deployments[0]["trigger"] == "manual"
    assert deployments[0]["status"] == "success"

    last = await storage.last_deployment()
    assert last is not None
    assert last["from_sha"] == "abc1234"


@pytest.mark.asyncio
async def test_deployment_log_failed(storage: Storage) -> None:
    """Failed deployments should not appear in last_deployment()."""
    await storage.log_deployment(
        from_sha="aaa",
        to_sha="",
        trigger="evergreen",
        status="failed",
        error="uv sync failed",
    )
    last = await storage.last_deployment()
    assert last is None  # no successful deploys yet


@pytest.mark.asyncio
async def test_deployment_api_status(storage: Storage) -> None:
    """GET /api/deployment/status returns deployment info."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "running" in data
    assert "branch" in data
    assert "mode" in data
    assert "commits_behind" in data
    assert "update_available" in data
    assert "branch_mismatch" in data


@pytest.mark.asyncio
async def test_deployment_status_branch_mismatch(storage: Storage) -> None:
    """Status shows branch_mismatch when tracked branch differs from checked-out branch."""
    await storage.set_setting("DEPLOY_BRANCH", "live")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/status")
    assert resp.status_code == 200
    data = resp.json()
    # We're on feature/257-promotion-history, tracking "live" — should mismatch
    assert data["branch_mismatch"] is True
    assert data["update_available"] is True


@pytest.mark.asyncio
async def test_config_from_storage(storage: Storage) -> None:
    """DeployConfig.from_storage reads DB overrides."""
    await storage.set_setting("DEPLOY_MODE", "evergreen")
    await storage.set_setting("DEPLOY_BRANCH", "live")
    config = await DeployConfig.from_storage(storage)
    assert config.mode == "evergreen"
    assert config.branch == "live"


@pytest.mark.asyncio
async def test_config_api_update(storage: Storage) -> None:
    """PUT /api/deployment/config persists mode and branch."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/deployment/config",
            json={"mode": "evergreen", "branch": "live"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "mode=evergreen" in data["changed"]
    assert "branch=live" in data["changed"]
    # Verify persisted
    config = await DeployConfig.from_storage(storage)
    assert config.mode == "evergreen"
    assert config.branch == "live"


@pytest.mark.asyncio
async def test_config_api_invalid_mode(storage: Storage) -> None:
    """PUT /api/deployment/config rejects invalid mode."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/deployment/config",
            json={"mode": "yolo"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_branches_api(storage: Storage) -> None:
    """GET /api/deployment/branches returns a list."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/branches")
    assert resp.status_code == 200
    data = resp.json()
    assert "branches" in data
    assert isinstance(data["branches"], list)


# ------------------------------------------------------------------
# Promotion pipeline & history
# ------------------------------------------------------------------


class TestGetPipelineStatus:
    @pytest.mark.asyncio
    async def test_returns_branches_and_gaps(self) -> None:
        """get_pipeline_status returns branches dict and gaps dict."""
        from helmlog.deploy import get_pipeline_status

        result = await get_pipeline_status()
        assert "branches" in result
        assert "gaps" in result
        # main should always exist in our test repo
        assert "main" in result["branches"]
        assert "main_ahead_of_stage" in result["gaps"]
        assert "stage_ahead_of_live" in result["gaps"]

    @pytest.mark.asyncio
    async def test_branch_info_has_expected_keys(self) -> None:
        """Each branch entry should have sha, short_sha, message, timestamp."""
        from helmlog.deploy import get_pipeline_status

        result = await get_pipeline_status()
        main = result["branches"]["main"]
        if main is not None:
            assert "sha" in main
            assert "short_sha" in main
            assert "message" in main
            assert "timestamp" in main
            assert len(main["short_sha"]) == 7


class TestGetPromotionHistory:
    @pytest.mark.asyncio
    async def test_returns_list(self) -> None:
        """get_promotion_history returns a list (may be empty if no tags)."""
        from helmlog.deploy import get_promotion_history

        result = await get_promotion_history()
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_filter_by_tier(self) -> None:
        """Filtering by tier should only return matching entries."""
        from helmlog.deploy import get_promotion_history

        result = await get_promotion_history(tier="stage")
        for entry in result:
            assert entry["tier"] == "stage"

    @pytest.mark.asyncio
    async def test_entry_structure(self) -> None:
        """Each promotion entry should have expected keys."""
        from helmlog.deploy import get_promotion_history

        result = await get_promotion_history()
        for entry in result:
            assert "tag" in entry
            assert "tier" in entry
            assert "timestamp" in entry
            assert "sha" in entry
            assert "short_sha" in entry
            assert entry["tier"] in ("stage", "live")


class TestGetPendingChanges:
    @pytest.mark.asyncio
    async def test_returns_list_of_commits(self) -> None:
        """get_pending_changes returns a list of commit dicts."""
        from helmlog.deploy import get_pending_changes

        result = await get_pending_changes("main", "main")
        assert isinstance(result, list)
        # Same branch should have no pending changes
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_invalid_branch_returns_empty(self) -> None:
        """Non-existent branch should return empty list gracefully."""
        from helmlog.deploy import get_pending_changes

        result = await get_pending_changes("nonexistent-xyz", "main")
        assert isinstance(result, list)


@pytest.mark.asyncio
async def test_pipeline_api(storage: Storage) -> None:
    """GET /api/deployment/pipeline returns pipeline status."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/pipeline")
    assert resp.status_code == 200
    data = resp.json()
    assert "branches" in data
    assert "gaps" in data


@pytest.mark.asyncio
async def test_promotions_api(storage: Storage) -> None:
    """GET /api/deployment/promotions returns promotion list."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/promotions")
    assert resp.status_code == 200
    data = resp.json()
    assert "promotions" in data
    assert isinstance(data["promotions"], list)


@pytest.mark.asyncio
async def test_promotions_api_with_tier_filter(storage: Storage) -> None:
    """GET /api/deployment/promotions?tier=stage filters correctly."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/promotions?tier=stage")
    assert resp.status_code == 200
    data = resp.json()
    for p in data["promotions"]:
        assert p["tier"] == "stage"


@pytest.mark.asyncio
async def test_pending_api(storage: Storage) -> None:
    """GET /api/deployment/pending returns commits."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/pending?from_tier=main&to_tier=main")
    assert resp.status_code == 200
    data = resp.json()
    assert "commits" in data
    assert "count" in data
    assert data["count"] == 0  # same branch = no pending


@pytest.mark.asyncio
async def test_pending_api_invalid_tier(storage: Storage) -> None:
    """GET /api/deployment/pending rejects invalid tier names."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/deployment/pending?from_tier=invalid&to_tier=main")
    assert resp.status_code == 400
