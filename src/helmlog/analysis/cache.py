"""SQLite cache layer for analysis results (#283).

Stores serialized AnalysisResult per (session_id, plugin_name) with a
data_hash for invalidation when session data changes.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage


def _compute_data_hash(data: dict[str, Any]) -> str:
    """Stable hash of the session data used by a plugin."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class AnalysisCache:
    """Read/write cache backed by the analysis_cache table."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def get(
        self,
        session_id: int,
        plugin_name: str,
        *,
        data_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """Return cached result or None if miss/stale."""
        row = await self._storage.get_analysis_cache(session_id, plugin_name)
        if row is None:
            return None
        if data_hash is not None and row["data_hash"] != data_hash:
            logger.debug(
                "Cache stale for session={} plugin={} (hash mismatch)", session_id, plugin_name
            )
            return None
        try:
            return json.loads(row["result_json"])  # type: ignore[no-any-return]
        except (json.JSONDecodeError, TypeError):
            return None

    async def put(
        self,
        session_id: int,
        plugin_name: str,
        plugin_version: str,
        data_hash: str,
        result: dict[str, Any],
    ) -> None:
        """Write or update the cache entry."""
        result_json = json.dumps(result, default=str)
        await self._storage.upsert_analysis_cache(
            session_id, plugin_name, plugin_version, data_hash, result_json
        )

    async def invalidate(self, session_id: int) -> None:
        """Remove all cached results for a session."""
        await self._storage.invalidate_analysis_cache(session_id)
