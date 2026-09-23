"""Seed a synthetic, PII-free demo DB for screenshots.

CI has no real ``logger.db`` (and must not — screenshots of real data leak crew
names and audio transcripts). This builds a disposable DB populated entirely
with *synthesised* data using HelmLog's own simulator, so the web UI pages —
track map, gauges, wind field, polar scatter, maneuvers table, history — render
with realistic content and zero PII.

It reuses the exact building blocks behind the "Synthesize Race" web action
(`routes/sessions.py::api_synthesize_session`): `simulate` → `import_race` →
`import_synthesized_data` → boat settings → `detect_maneuvers`, then builds the
polar baseline (needs >= 3 sessions) and adds a few sails, crew slots, and
moments. Secondary panels are best-effort so a signature drift degrades the
screenshot rather than failing the seed.

Usage:
    uv run python scripts/screenshots/seed_demo.py --db /tmp/demo.db [--sessions 4]

Then point the harness at it:
    SHOTS_DB=/tmp/demo.db uvx shot-scraper multi scripts/screenshots/shots.yml
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from loguru import logger


async def _seed_sails(storage: object) -> None:
    """Create a small sail inventory and set boat-level defaults."""
    ids: dict[str, int] = {}
    for sail_type, name in (("main", "2024 main"), ("jib", "2024 jib"), ("spinnaker", "2024 A2")):
        with contextlib.suppress(ValueError):  # ValueError → already exists (re-seed)
            ids[sail_type] = await storage.add_sail(sail_type, name)  # type: ignore[attr-defined]
    if len(ids) == 3:
        await storage.set_sail_defaults(  # type: ignore[attr-defined]
            main_id=ids["main"], jib_id=ids["jib"], spinnaker_id=ids["spinnaker"]
        )


async def _seed_crew(storage: object) -> None:
    """Populate boat-level crew slots by position (no user names → no PII)."""
    db = storage._read_conn()  # noqa: SLF001
    cur = await db.execute("SELECT id FROM crew_positions ORDER BY id LIMIT 5")
    position_ids = [row["id"] for row in await cur.fetchall()]
    weights = [82, 75, 90, 78, 85]
    crew = [
        {"position_id": pid, "body_weight": w, "attributed": True}
        for pid, w in zip(position_ids, weights, strict=False)
    ]
    if crew:
        await storage.set_crew_defaults(None, crew)  # type: ignore[attr-defined]


async def _synthesize_session(storage: object, *, day_offset: int, seed: int) -> int:
    """Simulate one windward/leeward race and import it. Returns race_id."""
    from helmlog.maneuver_detector import detect_maneuvers
    from helmlog.races import build_race_name
    from helmlog.synthesize import (
        CollisionAvoidanceConfig,
        HeaderResponseConfig,
        SynthConfig,
        generate_boat_settings,
        simulate,
    )

    start_lat, start_lon = 47.63, -122.40
    wind_dir = 200.0 + seed * 7.0
    day = (datetime.now(UTC) - timedelta(days=day_offset)).replace(
        hour=18, minute=25, second=0, microsecond=0
    )
    date_str = day.date().isoformat()

    from helmlog.courses import build_wl_course

    legs = build_wl_course(start_lat, start_lon, wind_dir, leg_nm=1.0, laps=2)
    config = SynthConfig(
        start_lat=start_lat,
        start_lon=start_lon,
        base_twd=wind_dir,
        tws_low=8.0,
        tws_high=14.0,
        shift_interval=(600.0, 1200.0),
        shift_magnitude=(5.0, 14.0),
        legs=legs,
        seed=seed,
        start_time=day,
        wind_seed=seed,
        header_response=HeaderResponseConfig(),
        collision_avoidance=CollisionAvoidanceConfig(min_separation_m=30.0),
    )
    rows = await asyncio.to_thread(simulate, config, None)
    if not rows:
        raise RuntimeError("simulate produced no rows")

    race_num = await storage.count_sessions_for_date(date_str, "synthesized") + 1  # type: ignore[attr-defined]
    event = "Demo Regatta"
    name = build_race_name(event, day.date(), race_num, "synthesized")
    race_id = await storage.import_race(  # type: ignore[attr-defined]
        name=name,
        event=event,
        race_num=race_num,
        date_str=date_str,
        start_utc=rows[0].ts,
        end_utc=rows[-1].ts,
        session_type="synthesized",
        source="synthesized",
        source_id=f"demo-{seed}",
    )
    await storage.import_synthesized_data(rows, race_id=race_id)  # type: ignore[attr-defined]

    # Boat settings (rig/trim params shown on the session page)
    try:
        settings = generate_boat_settings(rows, config)
        for scope, race_arg in ((True, None), (False, race_id)):
            batch = [
                {"ts": s.ts, "parameter": s.parameter, "value": s.value}
                for s in settings
                if s.race_id_is_null is scope
            ]
            if batch:
                await storage.create_boat_settings(race_arg, batch, source="synthesized")  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo-seed: boat settings skipped ({})", exc)

    # Apply default sails to this race
    try:
        defaults = await storage.get_sail_defaults()  # type: ignore[attr-defined]
        if any(defaults[t] for t in ("main", "jib", "spinnaker")):
            await storage.insert_sail_change(  # type: ignore[attr-defined]
                race_id,
                rows[0].ts.isoformat(),
                main_id=defaults["main"]["id"] if defaults["main"] else None,
                jib_id=defaults["jib"]["id"] if defaults["jib"] else None,
                spinnaker_id=defaults["spinnaker"]["id"] if defaults["spinnaker"] else None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("demo-seed: sail change skipped ({})", exc)

    # Maneuvers (tacks/gybes table)
    maneuvers = await detect_maneuvers(storage, race_id)  # type: ignore[arg-type]
    logger.info("demo-seed: race {} ({}) — {} maneuvers", race_id, name, len(maneuvers))
    return race_id


async def _seed_moments(storage: object, race_id: int) -> None:
    """Add a few generic moments to the latest session (no real names)."""
    db = storage._read_conn()  # noqa: SLF001
    cur = await db.execute("SELECT start_utc FROM races WHERE id = ?", (race_id,))
    row = await cur.fetchone()
    if row is None:
        return
    start = datetime.fromisoformat(str(row["start_utc"]))
    notes = [
        (2, "Good start at the boat end, clear lane off the line."),
        (11, "Header on the left — tacked to consolidate."),
        (19, "Nice leeward mark rounding, held the inside lane."),
    ]
    for minutes, subject in notes:
        try:
            await storage.create_moment(  # type: ignore[attr-defined]
                session_id=race_id,
                anchor_kind="timestamp",
                anchor_t_start=(start + timedelta(minutes=minutes)).isoformat(),
                subject=subject,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo-seed: moment skipped ({})", exc)


async def main(db_path: str, num_sessions: int) -> None:
    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig(db_path=db_path))
    await storage.connect()
    try:
        await _seed_sails(storage)
        await _seed_crew(storage)

        race_ids: list[int] = []
        for i in range(num_sessions):
            rid = await _synthesize_session(storage, day_offset=num_sessions - i, seed=42 + i)
            race_ids.append(rid)

        # Polar baseline needs >= 3 sessions of racing data
        try:
            from helmlog.polar import build_polar_baseline

            points = await build_polar_baseline(storage, min_sessions=2)
            logger.info("demo-seed: polar baseline built from {} points", points)
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo-seed: polar baseline skipped ({})", exc)

        if race_ids:
            await _seed_moments(storage, race_ids[-1])

        logger.info("demo-seed: done — {} sessions ({})", len(race_ids), race_ids)
    finally:
        await storage.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed a synthetic demo DB for screenshots")
    parser.add_argument("--db", required=True, help="Path to the DB file to create/populate")
    parser.add_argument("--sessions", type=int, default=4, help="Number of synthetic races")
    args = parser.parse_args()
    asyncio.run(main(args.db, args.sessions))
