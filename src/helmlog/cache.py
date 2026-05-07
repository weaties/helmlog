"""Web response cache (#594).

Two tiers sharing one process-scoped owner:

* **T1** — in-process dict with a monotonic-clock TTL. Cheap reads for small
  hot metadata (session list page, crew, boat settings). Lives for the life
  of the process; cleared by explicit invalidation or TTL.
* **T2** — a SQLite blob table (``web_cache``). Large immutable results
  (session summary, track GeoJSON, wind field). Content-addressed by a
  ``data_hash`` derived from the source race row so writes to the race
  naturally invalidate the entry via the storage mutation hook.

The cache is best-effort: any failure to read or write is logged and
swallowed — a broken cache must never fail a user request (EARS Req. 3).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from loguru import logger

if TYPE_CHECKING:
    import aiosqlite

    from helmlog.storage import Storage


MAX_CACHE_ROWS_DEFAULT: int = 1000


class RaceCache(Protocol):
    """Protocol the storage layer sees when a cache is bound."""

    async def invalidate(self, race_id: int) -> None: ...

    def t1_invalidate_family(self, family: str) -> None: ...


def compute_race_data_hash(
    *,
    race_id: int,
    start_utc: datetime,
    end_utc: datetime | None,
    row_count: int,
    linked_imported_id: int | None = None,
    linked_imported_results: int = 0,
) -> str:
    """Stable 16-char hex digest of the race's cache-relevant inputs.

    A cache entry is valid exactly as long as the hash matches the current
    race row. No TTL is needed: any mutation to the underlying race flows
    through the storage invalidation hook, which drops all entries for the
    race id.

    ``linked_imported_id`` and ``linked_imported_results`` fold the
    imported-results link into the hash so ETag revalidation flips when
    a session's results are linked, unlinked, or rewritten — otherwise
    browsers that cached an empty-results response keep getting 304s.
    """
    payload = {
        "race_id": race_id,
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat() if end_utc is not None else None,
        "row_count": row_count,
        "linked_imported_id": linked_imported_id,
        "linked_imported_results": linked_imported_results,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def resolve_race_data_hash(storage: Storage, race_id: int) -> str | None:
    """Read the race row + instrument row counts and return its data_hash.

    Returns ``None`` when the race doesn't exist. Row count comes from
    ``positions`` (the dominant data source for summary/track/wind-field)
    filtered by ``race_id`` if tagged, else by the race time window. A
    completed race's hash is stable; an open race's hash changes as data
    streams in, which keeps ETag revalidation meaningful for the home page.
    """
    db = storage._conn()
    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (race_id,))
    race = await cur.fetchone()
    if race is None:
        return None
    start_iso = str(race["start_utc"])
    end_iso = str(race["end_utc"]) if race["end_utc"] is not None else None

    # Row count: prefer race_id tagging, fall back to time window. For an
    # active race (end_iso is None), use "now" as the upper bound so the
    # count grows as positions stream in — otherwise the hash would be
    # frozen and the cache would serve stale track blobs forever.
    rid_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM positions WHERE race_id = ?", (race_id,)
    )
    rid_row = await rid_cur.fetchone()
    n_tagged = int(rid_row["cnt"]) if rid_row else 0
    if n_tagged > 0:
        row_count = n_tagged
    else:
        window_end = end_iso or datetime.now(UTC).isoformat()
        win_cur = await db.execute(
            "SELECT COUNT(*) AS cnt FROM positions WHERE ts >= ? AND ts <= ?",
            (start_iso, window_end),
        )
        win_row = await win_cur.fetchone()
        row_count = int(win_row["cnt"]) if win_row else 0

    # Imported-results link: which (if any) imported race points here, and
    # how many results it carries. Folded into the hash so ETag flips on
    # link/unlink even though the live race row itself hasn't changed.
    link_cur = await db.execute(
        "SELECT id FROM races"
        " WHERE local_session_id = ? AND source IS NOT NULL AND source != 'live'"
        " ORDER BY COALESCE(start_utc, date) LIMIT 1",
        (race_id,),
    )
    link_row = await link_cur.fetchone()
    linked_imported_id: int | None = None
    linked_imported_results = 0
    if link_row is not None:
        linked_imported_id = int(link_row["id"])
        rr_cur = await db.execute(
            "SELECT COUNT(*) AS cnt FROM race_results WHERE race_id = ?",
            (linked_imported_id,),
        )
        rr_row = await rr_cur.fetchone()
        linked_imported_results = int(rr_row["cnt"]) if rr_row else 0

    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else None
    return compute_race_data_hash(
        race_id=race_id,
        start_utc=start_dt,
        end_utc=end_dt,
        row_count=row_count,
        linked_imported_id=linked_imported_id,
        linked_imported_results=linked_imported_results,
    )


class _T1Entry:
    __slots__ = ("expires_at", "value")

    def __init__(self, value: object, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


def _t1_race_key(family: str, race_id: int) -> str:
    return f"{family}::race={race_id}"


class WebCache:
    """Two-tier process-scoped cache shared by web routes.

    Storage binds a single instance via ``Storage.bind_race_cache`` so that
    ``races`` INSERT/UPDATE/DELETE paths can call ``invalidate(race_id)``
    in-transaction (EARS Req. 2).
    """

    def __init__(self, storage: Storage, *, max_rows: int = MAX_CACHE_ROWS_DEFAULT) -> None:
        self._storage = storage
        self._max_rows = max_rows
        self._t1: dict[str, _T1Entry] = {}
        # Per-family hit/miss/invalidate counters (EARS Req. 18, #611).
        # Exposed via /api/admin/cache/stats. Fine as a plain dict — the
        # web stack is a single asyncio loop, no concurrent mutation.
        self._counters: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------
    # Stats (EARS Req. 18)
    # ------------------------------------------------------------------

    def _family_of(self, key: str) -> str:
        """Extract the family prefix from a cache key.

        ``sessions_list:abc`` → ``"sessions_list"``; ``session_detail::race=42``
        → ``"session_detail"``. Any key without a separator is its own family.
        """
        race_idx = key.find("::race=")
        colon_idx = key.find(":")
        if race_idx == -1:
            boundary = colon_idx
        elif colon_idx == -1:
            boundary = race_idx
        else:
            boundary = min(race_idx, colon_idx)
        return key[:boundary] if boundary > 0 else key

    def _bump(self, family: str, kind: str) -> None:
        entry = self._counters.setdefault(family, {"hit": 0, "miss": 0, "invalidate": 0})
        entry[kind] = entry.get(kind, 0) + 1

    def stats(self) -> dict[str, dict[str, int]]:
        """Return a snapshot of per-family hit/miss/invalidate counters."""
        return {k: dict(v) for k, v in self._counters.items()}

    def reset_stats(self) -> None:
        """Zero all counters. Admin-visible via the stats endpoint."""
        self._counters.clear()

    def t1_size(self) -> int:
        """Return the number of live T1 entries (includes expired-but-not-reaped)."""
        return len(self._t1)

    # ------------------------------------------------------------------
    # T1 — process dict with TTL
    # ------------------------------------------------------------------

    def t1_get(self, key: str) -> object | None:
        family = self._family_of(key)
        entry = self._t1.get(key)
        if entry is None:
            self._bump(family, "miss")
            return None
        if entry.expires_at <= time.monotonic():
            self._t1.pop(key, None)
            self._bump(family, "miss")
            return None
        self._bump(family, "hit")
        return entry.value

    def t1_put(self, key: str, value: object, *, ttl_seconds: float) -> None:
        self._t1[key] = _T1Entry(value, time.monotonic() + ttl_seconds)

    def t1_get_for_race(self, family: str, *, race_id: int) -> object | None:
        return self.t1_get(_t1_race_key(family, race_id))

    def t1_put_for_race(
        self, family: str, *, race_id: int, value: object, ttl_seconds: float
    ) -> None:
        self.t1_put(_t1_race_key(family, race_id), value, ttl_seconds=ttl_seconds)

    def t1_invalidate_family(self, family: str) -> None:
        """Drop every T1 entry whose key starts with ``family:`` or is race-keyed under it."""
        prefix_a = f"{family}:"
        prefix_b = f"{family}::race="
        dropped = 0
        for key in [k for k in self._t1 if k.startswith(prefix_a) or k.startswith(prefix_b)]:
            self._t1.pop(key, None)
            dropped += 1
        if dropped:
            self._bump(family, "invalidate")

    # ------------------------------------------------------------------
    # T2 — SQLite blob cache
    # ------------------------------------------------------------------

    async def t2_get(self, key_family: str, *, race_id: int, data_hash: str) -> object | None:
        try:
            row = await self._read_row(key_family, race_id)
        except Exception as exc:  # pragma: no cover - exercised via monkeypatch
            logger.warning("web cache read failed ({}): {}", key_family, exc)
            self._bump(key_family, "miss")
            return None
        if row is None:
            self._bump(key_family, "miss")
            return None
        if row["data_hash"] != data_hash:
            self._bump(key_family, "miss")
            return None
        # v74+ schema: expires_utc is always present (NULL for race-keyed
        # rows that rely on invalidation hooks rather than TTL).
        expires = row["expires_utc"]
        if expires is not None:
            try:
                exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            except ValueError:
                exp_dt = None
            if exp_dt is not None and exp_dt <= datetime.now(UTC):
                await self._delete_row(key_family, race_id)
                self._bump(key_family, "miss")
                return None
        try:
            decoded: object = json.loads(row["blob"])
            self._bump(key_family, "hit")
            return decoded
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "web cache blob decode failed for {} race={}: {} — evicting",
                key_family,
                race_id,
                exc,
            )
            await self._delete_row(key_family, race_id)
            self._bump(key_family, "miss")
            return None

    async def t2_put(
        self,
        key_family: str,
        *,
        race_id: int,
        data_hash: str,
        value: object,
        ttl_seconds: float | None = None,
        _now: datetime | None = None,
    ) -> None:
        try:
            blob = json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("web cache encode failed for {}: {}", key_family, exc)
            return
        now = _now or datetime.now(UTC)
        created = now.isoformat()
        expires_iso: str | None = None
        if ttl_seconds is not None and ttl_seconds > 0:
            from datetime import timedelta

            expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        try:
            await self._write_row(key_family, race_id, data_hash, blob, created, expires_iso)
            await self._evict_over_cap()
        except Exception as exc:
            logger.warning("web cache write failed ({}): {}", key_family, exc)

    # ------------------------------------------------------------------
    # T2 — global (non-race-keyed) entries (#610)
    # ------------------------------------------------------------------
    #
    # External API fetches (weather, tides) aren't tied to any race, so
    # they share the T2 table via a race_id=0 sentinel. Invalidation of
    # these entries is TTL-only — the race-mutation hook leaves them
    # alone (race_id=0 never matches a real race).

    async def t2_get_global(self, key_family: str, *, data_hash: str) -> object | None:
        return await self.t2_get(key_family, race_id=0, data_hash=data_hash)

    async def t2_put_global(
        self,
        key_family: str,
        *,
        data_hash: str,
        value: object,
        ttl_seconds: float | None,
    ) -> None:
        await self.t2_put(
            key_family, race_id=0, data_hash=data_hash, value=value, ttl_seconds=ttl_seconds
        )

    # ------------------------------------------------------------------
    # Invalidation — called by storage race-mutation paths
    # ------------------------------------------------------------------

    async def invalidate(self, race_id: int) -> None:
        # T1 — drop any race-keyed entries for this id, bumping a family
        # counter for each distinct family we evict so /admin/cache/stats
        # reflects per-family invalidation pressure.
        suffix = f"::race={race_id}"
        dropped_families: set[str] = set()
        for key in [k for k in self._t1 if k.endswith(suffix)]:
            dropped_families.add(self._family_of(key))
            self._t1.pop(key, None)
        for family in dropped_families:
            self._bump(family, "invalidate")
        # T1 — drop list-shaped families whose contents depend on the set of
        # races (not on any single race_id). Any race insert/update/delete
        # changes /api/sessions output, so its list-family entries must go.
        self.t1_invalidate_family("sessions_list")
        # T2 — drop every row for this race.
        try:
            db = self._storage._conn()  # noqa: SLF001
            cur = await db.execute(
                "SELECT DISTINCT key_family FROM web_cache WHERE race_id = ?",
                (race_id,),
            )
            t2_families = [r["key_family"] for r in await cur.fetchall()]
            await db.execute("DELETE FROM web_cache WHERE race_id = ?", (race_id,))
            await db.commit()
            for family in t2_families:
                self._bump(family, "invalidate")
        except Exception as exc:
            logger.warning("web cache invalidate failed race={}: {}", race_id, exc)

    # ------------------------------------------------------------------
    # Internal DB helpers (patched in tests to simulate failures)
    # ------------------------------------------------------------------

    async def _read_row(self, key_family: str, race_id: int) -> aiosqlite.Row | None:
        db = self._storage._conn()  # noqa: SLF001
        cur = await db.execute(
            "SELECT data_hash, blob, expires_utc FROM web_cache"
            " WHERE key_family = ? AND race_id = ?",
            (key_family, race_id),
        )
        return await cur.fetchone()

    async def _write_row(
        self,
        key_family: str,
        race_id: int,
        data_hash: str,
        blob: str,
        created_utc: str,
        expires_utc: str | None,
    ) -> None:
        db = self._storage._conn()  # noqa: SLF001
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob,"
            " created_utc, expires_utc) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key_family, race_id) DO UPDATE SET"
            "   data_hash = excluded.data_hash,"
            "   blob = excluded.blob,"
            "   created_utc = excluded.created_utc,"
            "   expires_utc = excluded.expires_utc",
            (key_family, race_id, data_hash, blob, created_utc, expires_utc),
        )
        await db.commit()

    async def _delete_row(self, key_family: str, race_id: int) -> None:
        db = self._storage._conn()  # noqa: SLF001
        await db.execute(
            "DELETE FROM web_cache WHERE key_family = ? AND race_id = ?",
            (key_family, race_id),
        )
        await db.commit()

    async def _evict_over_cap(self) -> None:
        db = self._storage._conn()  # noqa: SLF001
        cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache")
        row = await cur.fetchone()
        if row is None:
            return
        total = int(row["n"])
        excess = total - self._max_rows
        if excess <= 0:
            return
        await db.execute(
            "DELETE FROM web_cache WHERE rowid IN ("
            "  SELECT rowid FROM web_cache ORDER BY created_utc ASC LIMIT ?"
            ")",
            (excess,),
        )
        await db.commit()


async def warm_race_cache(storage: Storage, cache: WebCache, race_id: int) -> None:
    """Pre-compute session_summary, session_track, and wind_field for a
    freshly-ended race and store them in T2 (EARS Req. 16).

    Designed to be fire-and-forget — any exception is logged and swallowed
    so a warming failure can't affect the caller (Req. 17). Called from the
    HTTP race-end route via ``asyncio.ensure_future``.
    """
    # Local import to avoid circular: routes.sessions imports from cache.
    from helmlog.routes.sessions import (
        _compute_session_summary,
        _compute_session_track,
        _compute_wind_field,
    )

    data_hash = await resolve_race_data_hash(storage, race_id)
    if data_hash is None:
        logger.debug("warm_race_cache: race {} missing, skipping", race_id)
        return

    async def _warm(family: str, coro: object) -> None:
        try:
            payload = await coro  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "warm_race_cache: compute failed family={} race={}: {}", family, race_id, exc
            )
            return
        try:
            await cache.t2_put(family, race_id=race_id, data_hash=data_hash, value=payload)
        except Exception as exc:  # noqa: BLE001 — cache writes are best-effort
            logger.warning(
                "warm_race_cache: put failed family={} race={}: {}", family, race_id, exc
            )

    # Summary + track use a stable key_family; wind-field bakes the default
    # UI parameters (grid_size=20, elapsed_s=0) into the family, matching
    # the URL the history page will fetch first.
    await _warm("session_summary", _compute_session_summary(storage, race_id))
    await _warm("session_track", _compute_session_track(storage, race_id))
    await _warm(
        "wind_field:grid=20:t=0.000",
        _compute_wind_field(storage, race_id, 0.0, 20),
    )


__all__ = [
    "MAX_CACHE_ROWS_DEFAULT",
    "RaceCache",
    "WebCache",
    "compute_race_data_hash",
    "resolve_race_data_hash",
    "warm_race_cache",
]
