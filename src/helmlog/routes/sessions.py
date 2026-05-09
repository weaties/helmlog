"""Route handlers for sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger

from helmlog.auth import require_auth, require_developer
from helmlog.current import compute_set_drift
from helmlog.routes._helpers import (
    audit,
    cached_json_response,
    get_storage,
    get_web_cache,
    limiter,
    t1_cached_json_response,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage

router = APIRouter()


async def _resolve_session(request: Request, session_id: int) -> tuple[int | None, int | None]:
    """Return (race_id, audio_session_id) for the given session_id, or raise 404."""
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is not None:
        return session_id, None
    cur2 = await storage._conn().execute(
        "SELECT id FROM audio_sessions WHERE id = ?", (session_id,)
    )
    if await cur2.fetchone() is not None:
        return None, session_id
    raise HTTPException(status_code=404, detail="Session not found")


@router.post("/api/sessions/synthesize")
async def api_synthesize_session(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    _dev: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    from helmlog.courses import (
        CourseMark,
        build_custom_course,
        build_triangle_course,
        build_wl_course,
        validate_course_marks,
    )
    from helmlog.races import build_race_name, local_today
    from helmlog.synthesize import (
        CollisionAvoidanceConfig,
        HeaderResponseConfig,
        SynthConfig,
        generate_boat_settings,
        simulate,
    )

    body = await request.json()
    course_type = body.get("course_type", "windward_leeward")
    wind_dir = float(body.get("wind_direction", 0.0))
    tws_low = float(body.get("wind_speed_low", 8.0))
    tws_high = float(body.get("wind_speed_high", 14.0))
    shift_mag_lo = float(body.get("shift_magnitude_low", 5.0))
    shift_mag_hi = float(body.get("shift_magnitude_high", 14.0))
    start_lat = float(body.get("start_lat", 47.63))
    start_lon = float(body.get("start_lon", -122.40))
    leg_nm = float(body.get("leg_distance_nm", 1.0))
    laps = int(body.get("laps", 2))
    seed = int(body.get("seed", 42))
    raw_wind_seed = body.get("wind_seed")
    wind_seed: int | None = int(raw_wind_seed) if raw_wind_seed is not None else None
    mark_sequence = body.get("mark_sequence", "")
    peer_fingerprint: str | None = body.get("peer_fingerprint") or None
    peer_co_op_id: str | None = body.get("peer_co_op_id") or None
    raw_start_utc: str | None = body.get("start_utc")  # imported source session start

    # Collision avoidance — other boats' tracks to avoid (#246)
    raw_other_tracks: list[list[dict[str, Any]]] | None = body.get("other_tracks")
    min_separation_m = float(body.get("min_separation_m", 30.0))
    collision_avoidance = CollisionAvoidanceConfig(min_separation_m=min_separation_m)

    # Header response model — probabilistic tacking on wind shifts (#247)
    hr_raw = body.get("header_response")
    if isinstance(hr_raw, dict):
        header_response = HeaderResponseConfig(
            reaction_probability=float(hr_raw.get("reaction_probability", 0.70)),
            min_shift_threshold=(
                float(hr_raw.get("min_shift_threshold_low", 3.0)),
                float(hr_raw.get("min_shift_threshold_high", 8.0)),
            ),
            reaction_delay=(
                float(hr_raw.get("reaction_delay_low", 10.0)),
                float(hr_raw.get("reaction_delay_high", 45.0)),
            ),
            fatigue_start_frac=float(hr_raw.get("fatigue_start_frac", 0.70)),
            fatigue_floor=float(hr_raw.get("fatigue_floor", 0.40)),
        )
    else:
        header_response = HeaderResponseConfig()

    # Parse optional mark position overrides from user-dragged map markers
    raw_overrides = body.get("mark_overrides")
    mark_overrides: dict[str, tuple[float, float]] | None = None
    if isinstance(raw_overrides, dict):
        mark_overrides = {
            k: (float(v["lat"]), float(v["lon"]))
            for k, v in raw_overrides.items()
            if isinstance(v, dict) and "lat" in v and "lon" in v
        }

    if course_type == "windward_leeward":
        legs = build_wl_course(start_lat, start_lon, wind_dir, leg_nm, laps, mark_overrides)
    elif course_type == "triangle":
        legs = build_triangle_course(start_lat, start_lon, wind_dir, leg_nm, mark_overrides)
    elif course_type == "custom":
        if not mark_sequence:
            raise HTTPException(status_code=422, detail="mark_sequence required for custom course")
        try:
            legs = build_custom_course(mark_sequence, start_lat, start_lon, wind_dir, leg_nm)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=422, detail=f"Unknown course_type: {course_type}")

    # Validate all course marks are in navigable water (>6 ft deep)
    # Build marks from legs only — they already have correct overridden positions
    # and only include marks actually used in the course (#264)
    all_marks: dict[str, CourseMark] = {}
    for leg in legs:
        key = leg.target.name.split()[-1][0]
        if key not in all_marks:
            all_marks[key] = leg.target
    mark_warnings = validate_course_marks(all_marks)

    if raw_start_utc:
        start_time = datetime.fromisoformat(raw_start_utc.replace("Z", "+00:00"))
    else:
        start_time = datetime.now(UTC)
    config = SynthConfig(
        start_lat=start_lat,
        start_lon=start_lon,
        base_twd=wind_dir,
        tws_low=tws_low,
        tws_high=tws_high,
        shift_interval=(600.0, 1200.0),
        shift_magnitude=(shift_mag_lo, shift_mag_hi),
        legs=legs,
        seed=seed,
        start_time=start_time,
        wind_seed=wind_seed,
        header_response=header_response,
        collision_avoidance=collision_avoidance,
    )

    rows = await asyncio.to_thread(simulate, config, raw_other_tracks)
    if not rows:
        raise HTTPException(status_code=500, detail="Simulation produced no data points")

    today = local_today()
    date_str = today.isoformat()
    race_num = await storage.count_sessions_for_date(date_str, "synthesized") + 1
    source_id = str(uuid.uuid4())

    rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
    from helmlog.races import default_event_for_date

    custom_event = await storage.get_daily_event(date_str)
    default_event = default_event_for_date(today, rules)
    event = custom_event or default_event or "Synthesized"

    name = build_race_name(event, today, race_num, "synthesized")

    start_utc = rows[0].ts
    end_utc = rows[-1].ts

    race_id = await storage.import_race(
        name=name,
        event=event,
        race_num=race_num,
        date_str=date_str,
        start_utc=start_utc,
        end_utc=end_utc,
        session_type="synthesized",
        source="synthesized",
        source_id=source_id,
        peer_fingerprint=peer_fingerprint,
        peer_co_op_id=peer_co_op_id,
    )
    await storage.import_synthesized_data(rows, race_id=race_id)

    duration_s = (end_utc - start_utc).total_seconds()

    # Persist wind field params and course marks for later visualization
    await storage.save_synth_wind_params(
        race_id,
        {
            "seed": wind_seed if wind_seed is not None else seed,
            "base_twd": wind_dir,
            "tws_low": tws_low,
            "tws_high": tws_high,
            "shift_interval_lo": 600.0,
            "shift_interval_hi": 1200.0,
            "shift_magnitude_lo": shift_mag_lo,
            "shift_magnitude_hi": shift_mag_hi,
            "ref_lat": start_lat,
            "ref_lon": start_lon,
            "duration_s": duration_s,
            "course_type": course_type,
            "leg_distance_nm": leg_nm,
            "laps": laps if course_type == "windward_leeward" else None,
            "mark_sequence": mark_sequence if course_type == "custom" else None,
        },
    )
    marks_to_save = [
        {"mark_key": k, "mark_name": m.name, "lat": m.lat, "lon": m.lon}
        for k, m in all_marks.items()
    ]
    await storage.save_synth_course_marks(race_id, marks_to_save)

    # Generate and persist synthesized boat settings
    synth_settings = generate_boat_settings(rows, config)
    boat_level = [s for s in synth_settings if s.race_id_is_null]
    race_level = [s for s in synth_settings if not s.race_id_is_null]
    if boat_level:
        await storage.create_boat_settings(
            None,
            [{"ts": s.ts, "parameter": s.parameter, "value": s.value} for s in boat_level],
            source="synthesized",
        )
    if race_level:
        await storage.create_boat_settings(
            race_id,
            [{"ts": s.ts, "parameter": s.parameter, "value": s.value} for s in race_level],
            source="synthesized",
        )

    # Auto-apply sail defaults for synthesized race (#311)
    try:
        sail_defaults = await storage.get_sail_defaults()
        has_any = any(sail_defaults[t] is not None for t in ("main", "jib", "spinnaker"))
        if has_any:
            await storage.insert_sail_change(
                race_id,
                start_utc.isoformat(),
                main_id=sail_defaults["main"]["id"] if sail_defaults["main"] else None,
                jib_id=sail_defaults["jib"]["id"] if sail_defaults["jib"] else None,
                spinnaker_id=(
                    sail_defaults["spinnaker"]["id"] if sail_defaults["spinnaker"] else None
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to auto-apply sail defaults for synth {}: {}", name, exc)

    # Auto-detect maneuvers for synthesized race
    async def _auto_detect_synth(rid: int) -> None:
        try:
            from helmlog.maneuver_detector import detect_maneuvers

            maneuvers = await detect_maneuvers(storage, rid)
            tacks = sum(1 for m in maneuvers if m.type == "tack")
            gybes = sum(1 for m in maneuvers if m.type == "gybe")
            logger.info("Synth auto-detected {} tacks, {} gybes for race {}", tacks, gybes, rid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Synth auto maneuver detection failed for race {}: {}", rid, exc)

    asyncio.ensure_future(_auto_detect_synth(race_id))

    detail = name + (f" [peer={peer_fingerprint}]" if peer_fingerprint else "")
    await audit(request, "session.synthesize", detail=detail, user=_user)

    resp: dict[str, Any] = {
        "id": race_id,
        "name": name,
        "points": len(rows),
        "duration_s": round(duration_s, 1),
    }
    if mark_warnings:
        resp["mark_warnings"] = mark_warnings
    return JSONResponse(resp, status_code=201)


@router.get("/api/sessions/{session_id}/track")
@limiter.limit("30/minute")
async def api_session_track(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Return GPS track as GeoJSON for map display."""
    storage = get_storage(request)

    async def _compute() -> dict[str, Any]:
        return await _compute_session_track(storage, session_id)

    return await cached_json_response(
        request, race_id=session_id, key_family="session_track", compute=_compute
    )


#: Window of GPS rows to include before the race's start_utc on the
#: session track. The helm's prestart maneuvers (line pings, traffic
#: management, hold patterns) live in this window. Positions inside it
#: that are *unscoped* (race_id IS NULL — captured before start_race
#: was called) get included; positions tagged to a different race do
#: not, so back-to-back starts don't bleed into each other.
_PRESTART_WINDOW_S: int = 1200  # 20 minutes


async def _compute_session_track(storage: Storage, session_id: int) -> dict[str, Any]:
    """Build the GeoJSON FeatureCollection for a session's GPS track.

    Includes a prestart prefix (last :data:`_PRESTART_WINDOW_S` seconds
    before ``start_utc``) so the session map shows the helm's pre-gun
    maneuvers, not just the race itself.

    Called by the HTTP endpoint and by the warm-on-complete hook in
    ``cache.warm_race_cache`` (#611). Raises ``HTTPException(404)`` when
    the race doesn't exist — the HTTP path surfaces that; the warmer
    catches and logs it.
    """
    from datetime import UTC, datetime, timedelta

    db = storage._conn()
    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Race not found")
    start_utc = row["start_utc"]
    # Active race (end_utc IS NULL): bound the upper window at "now" so live
    # positions written after the gun appear on the map. Falling back to
    # start_utc here would collapse the window and freeze the track at the
    # gun (the bug behind this regression).
    end_utc = row["end_utc"] or datetime.now(UTC).isoformat()

    # Prestart cutoff: start_utc shifted back by the configured window.
    try:
        start_dt = datetime.fromisoformat(start_utc)
        prestart_cutoff = (start_dt - timedelta(seconds=_PRESTART_WINDOW_S)).isoformat()
    except ValueError:
        prestart_cutoff = start_utc  # bad timestamp — degrade to no prestart

    # Prefer race_id filter (exact match for synthesized sessions);
    # fall back to time-range query for real instrument data. In both
    # branches we include unscoped positions in the prestart window so
    # the helm's pre-gun maneuvers show up.
    rid_cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE race_id = ?", (session_id,)
    )
    rid_row = await rid_cur.fetchone()
    has_race_id = rid_row["cnt"] > 0 if rid_row else False

    if has_race_id:
        pos_cur = await db.execute(
            "SELECT latitude_deg, longitude_deg, ts FROM positions"
            " WHERE race_id = ?"
            "    OR (race_id IS NULL AND ts >= ? AND ts < ?)"
            " ORDER BY ts",
            (session_id, prestart_cutoff, start_utc),
        )
    else:
        pos_cur = await db.execute(
            "SELECT latitude_deg, longitude_deg, ts FROM positions"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (prestart_cutoff, end_utc),
        )
    positions = await pos_cur.fetchall()
    if not positions:
        return {"type": "FeatureCollection", "features": []}

    # Per-second mean averaging. The SK reader currently records every fix
    # with source_addr=0 even when Signal K is multiplexing two physical
    # GPS antennas, so the raw rows zig-zag between antennas (~3m apart).
    # Bucketing to 1Hz and averaging within the bucket collapses the
    # zig-zag into a smooth single line midway between the antennas — what
    # you'd get from a single GPS anyway. Also gives the frontend a
    # naturally Vakaros-density polyline so its dash style reads cleanly.
    buckets: dict[str, list[tuple[float, float]]] = {}
    bucket_order: list[str] = []
    for r in positions:
        ts_raw = r["ts"]
        if not ts_raw:
            continue
        key = str(ts_raw)[:19]  # truncate to whole-second precision
        if key not in buckets:
            buckets[key] = []
            bucket_order.append(key)
        buckets[key].append((float(r["latitude_deg"]), float(r["longitude_deg"])))

    coords: list[list[float]] = []
    timestamps: list[str] = []
    for key in bucket_order:
        rows = buckets[key]
        avg_lat = sum(p[0] for p in rows) / len(rows)
        avg_lng = sum(p[1] for p in rows) / len(rows)
        coords.append([avg_lng, avg_lat])
        timestamps.append(key + ("" if key.endswith("Z") or "+" in key else "Z"))

    feature = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "session_id": session_id,
            "points": len(coords),
            "timestamps": timestamps,
        },
    }
    return {"type": "FeatureCollection", "features": [feature]}


@router.get("/api/sessions/{session_id}/summary")
@limiter.limit("60/minute")
async def api_session_summary(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Compact per-race summary for the history page thumbnails.

    Returns a downsampled track, event markers (tacks, gybes, roundings,
    start, finish) indexed into that track, average wind, and top-3
    finishers. Designed to be cheap enough for per-row lazy fetch.
    """

    storage = get_storage(request)

    async def _compute() -> dict[str, Any]:
        return await _compute_session_summary(storage, session_id)

    return await cached_json_response(
        request, race_id=session_id, key_family="session_summary", compute=_compute
    )


async def _compute_session_summary(storage: Storage, session_id: int) -> dict[str, Any]:
    import math
    from bisect import bisect_left

    db = storage._conn()

    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    race = await cur.fetchone()
    if race is None:
        raise HTTPException(status_code=404, detail="Race not found")
    start_utc = race["start_utc"]
    # Active race: use "now" so the post-gun positions and wind samples are
    # included in the summary. Falling back to start_utc would clip them.
    end_utc = race["end_utc"] or datetime.now(UTC).isoformat()

    rid_cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE race_id = ?", (session_id,)
    )
    rid_row = await rid_cur.fetchone()
    has_race_id = bool(rid_row and rid_row["cnt"] > 0)

    if has_race_id:
        pos_cur = await db.execute(
            "SELECT latitude_deg, longitude_deg, ts FROM positions WHERE race_id = ? ORDER BY ts",
            (session_id,),
        )
    else:
        pos_cur = await db.execute(
            "SELECT latitude_deg, longitude_deg, ts FROM positions"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start_utc, end_utc),
        )
    positions = await pos_cur.fetchall()

    track: list[list[float]] = []
    track_epochs: list[float] = []
    if positions:
        buckets: dict[str, list[tuple[float, float]]] = {}
        order: list[str] = []
        for r in positions:
            ts_raw = r["ts"]
            if not ts_raw:
                continue
            key = str(ts_raw)[:19]
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append((float(r["latitude_deg"]), float(r["longitude_deg"])))

        full_track: list[tuple[float, float, float]] = []
        for key in order:
            pts = buckets[key]
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            iso = key + ("Z" if not (key.endswith("Z") or "+" in key) else "")
            try:
                epoch = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            full_track.append((lat, lon, epoch))

        max_points = 80
        n = len(full_track)
        if n <= max_points:
            sampled = full_track
        else:
            step = n / max_points
            sampled = [full_track[min(int(i * step), n - 1)] for i in range(max_points)]
            if sampled[-1] != full_track[-1]:
                sampled.append(full_track[-1])

        for lat, lon, epoch in sampled:
            track.append([lon, lat])
            track_epochs.append(epoch)

    events: list[dict[str, Any]] = []
    if track_epochs:
        events.append({"type": "start", "idx": 0})
        events.append({"type": "finish", "idx": len(track_epochs) - 1})

        man_cur = await db.execute(
            "SELECT type, ts FROM maneuvers WHERE session_id = ?"
            " AND type IN ('tack', 'gybe', 'rounding') ORDER BY ts",
            (session_id,),
        )
        for m in await man_cur.fetchall():
            try:
                ep = datetime.fromisoformat(str(m["ts"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            idx = bisect_left(track_epochs, ep)
            if idx >= len(track_epochs):
                idx = len(track_epochs) - 1
            elif idx > 0 and (ep - track_epochs[idx - 1]) < (track_epochs[idx] - ep):
                idx -= 1
            events.append({"type": str(m["type"]), "idx": idx})

    if has_race_id:
        wind_cur = await db.execute(
            "SELECT wind_speed_kts, wind_angle_deg FROM winds"
            " WHERE race_id = ? AND reference IN (0, 4)",
            (session_id,),
        )
    else:
        wind_cur = await db.execute(
            "SELECT wind_speed_kts, wind_angle_deg FROM winds"
            " WHERE ts >= ? AND ts <= ? AND reference IN (0, 4)",
            (start_utc, end_utc),
        )
    wind_rows = await wind_cur.fetchall()
    wind: dict[str, float] | None = None
    if wind_rows:
        speeds = [float(w["wind_speed_kts"]) for w in wind_rows]
        angles = [math.radians(float(w["wind_angle_deg"])) for w in wind_rows]
        avg_speed = sum(speeds) / len(speeds)
        sin_sum = sum(math.sin(a) for a in angles)
        cos_sum = sum(math.cos(a) for a in angles)
        avg_dir = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0
        wind = {"avg_tws_kts": round(avg_speed, 1), "avg_twd_deg": round(avg_dir, 0)}

    results_race_cur = await db.execute(
        "SELECT id FROM races"
        " WHERE local_session_id = ? AND source IS NOT NULL AND source != 'live'"
        " ORDER BY COALESCE(start_utc, date) LIMIT 1",
        (session_id,),
    )
    results_race_row = await results_race_cur.fetchone()
    results_race_id = results_race_row["id"] if results_race_row else session_id

    res_cur = await db.execute(
        "SELECT rr.place, rr.dnf, rr.dns, rr.status_code,"
        " b.sail_number, b.name AS boat_name"
        " FROM race_results rr JOIN boats b ON rr.boat_id = b.id"
        " WHERE rr.race_id = ?"
        " ORDER BY CASE WHEN rr.dnf = 0 AND rr.dns = 0 THEN 0 ELSE 1 END, rr.place",
        (results_race_id,),
    )
    all_results: list[dict[str, Any]] = [dict(r) for r in await res_cur.fetchall()]
    finishers: list[dict[str, Any]] = [r for r in all_results if not r["dnf"] and not r["dns"]]
    top3: list[dict[str, Any]] = finishers[:3]

    own_sail: str | None = None
    try:
        from helmlog.federation import load_identity

        _, own_card = load_identity()
        own_sail = own_card.sail_number
    except Exception:
        own_sail = None

    own_result: dict[str, Any] | None = None
    if own_sail:
        for row in all_results:
            if str(row.get("sail_number") or "") == str(own_sail):
                own_result = row
                break

    results: list[dict[str, Any]] = list(top3)
    if own_result and not any(str(row.get("sail_number") or "") == str(own_sail) for row in top3):
        results.append(own_result)

    return {
        "track": track,
        "events": events,
        "wind": wind,
        "results": results,
    }


@router.get("/api/sessions/{session_id}/vakaros-overlay")
@limiter.limit("30/minute")
async def api_session_vakaros_overlay(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return Vakaros overlay data (track, start line, race events) for a race (#458).

    Used by the session detail page to augment the SK track with Vakaros-native
    overlays when a matched Vakaros session exists. Returns ``matched: false``
    with empty collections when the race is valid but has no matched session,
    and 404 when the race itself does not exist.
    """
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Race not found")

    overlay = await storage.get_vakaros_overlay_for_race(session_id)
    if overlay is None:
        return JSONResponse(
            {
                "matched": False,
                "vakaros_session_id": None,
                "track": None,
                "line_positions": [],
                "race_events": [],
                "line": None,
                "race_start_context": None,
            }
        )
    return JSONResponse({"matched": True, **overlay})


@router.get("/api/sessions/{session_id}/race-start-overlay")
@limiter.limit("30/minute")
async def api_session_race_start_overlay(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return HelmLog start-line ping history + computed line for the session.

    Used by the session detail page to draw HelmLog's own start-line markers
    (boat + pin) and a time-synced bias indicator that updates as the user
    scrubs the replay. Distinct from the Vakaros overlay so both can coexist
    on the same map.
    """
    import math

    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Race not found")

    pings = await storage.list_start_line_pings(race_id=session_id)
    line_row = await storage.get_latest_start_line(race_id=session_id)

    line_payload: dict[str, Any] | None = None
    if line_row and all(
        line_row.get(k) is not None
        for k in ("boat_end_lat", "boat_end_lon", "pin_end_lat", "pin_end_lon")
    ):
        boat_lat = line_row["boat_end_lat"]
        boat_lon = line_row["boat_end_lon"]
        pin_lat = line_row["pin_end_lat"]
        pin_lon = line_row["pin_end_lon"]
        # Bearing from boat-end to pin-end (matches race_start.line_metrics).
        phi1 = math.radians(boat_lat)
        phi2 = math.radians(pin_lat)
        dlam = math.radians(pin_lon - boat_lon)
        bearing = (
            math.degrees(
                math.atan2(
                    math.sin(dlam) * math.cos(phi2),
                    math.cos(phi1) * math.sin(phi2)
                    - math.sin(phi1) * math.cos(phi2) * math.cos(dlam),
                )
            )
            + 360.0
        ) % 360.0
        # Length via haversine.
        dphi = math.radians(pin_lat - boat_lat)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        length_m = 2 * 6_371_008.8 * math.asin(math.sqrt(a))
        line_payload = {
            "boat": [boat_lat, boat_lon],
            "pin": [pin_lat, pin_lon],
            "length_m": length_m,
            "bearing_deg": bearing,
            "boat_end_carried_over_from_race_id": (
                line_row.get("boat_end_race_id")
                if line_row.get("boat_end_race_id") not in (None, session_id)
                else None
            ),
            "pin_end_carried_over_from_race_id": (
                line_row.get("pin_end_race_id")
                if line_row.get("pin_end_race_id") not in (None, session_id)
                else None
            ),
        }

    return JSONResponse(
        {
            "session_id": session_id,
            "pings": [
                {
                    "id": p["id"],
                    "end_kind": p["end_kind"],
                    "lat": p["latitude_deg"],
                    "lon": p["longitude_deg"],
                    "ts": p["captured_at"],
                }
                for p in pings
            ],
            "line": line_payload,
        }
    )


@router.get("/api/sessions/{session_id}/course-overlay")
@limiter.limit("30/minute")
async def api_session_course_overlay(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return course marks + start/finish line geometry for the replay map.

    The session detail page draws this as a single "Marks & lines" layer so
    the user can see where they were going relative to the course. Synthesized
    races have explicit course marks in synth_course_marks; real races may have
    a Vakaros-derived start line. Both are merged into one response so the
    frontend can render with a single fetch.
    """
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Race not found")

    marks_rows = await storage.get_synth_course_marks(session_id)
    marks = [
        {
            "key": m["mark_key"],
            "name": m["mark_name"],
            "lat": m["lat"],
            "lon": m["lon"],
        }
        for m in marks_rows
        if m.get("lat") is not None and m.get("lon") is not None
    ]

    start_line: dict[str, Any] | None = None
    overlay = await storage.get_vakaros_overlay_for_race(session_id)
    if overlay and overlay.get("line"):
        line = overlay["line"]
        if line.get("pin") and line.get("boat"):
            start_line = {
                "pin": line["pin"],
                "boat": line["boat"],
                "length_m": line.get("length_m"),
                "bearing_deg": line.get("bearing_deg"),
            }

    return JSONResponse(
        {
            "session_id": session_id,
            "marks": marks,
            "start_line": start_line,
            "finish_line": None,  # not yet captured separately
        }
    )


_SESSION_DETAIL_TTL_S: float = 60.0


@router.get("/api/sessions/{session_id}/detail")
async def api_session_detail(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Return full metadata for a single session."""
    # Race-mutation hook invalidates the race-keyed T1 entry directly; the
    # 60s TTL guards against audio / wind-field / video-link changes that
    # don't flow through the races-row invalidation hook.
    cache_key = f"session_detail::race={session_id}"
    storage = get_storage(request)

    async def _compute() -> dict[str, Any]:
        return await _compute_session_detail(storage, session_id)

    return await t1_cached_json_response(
        request,
        cache_key=cache_key,
        ttl_seconds=_SESSION_DETAIL_TTL_S,
        compute=_compute,
    )


def _audio_stream_threshold_minutes() -> int:
    """Minutes of session length at/above which the session page streams audio
    instead of decoding (#648). Tunable via the admin settings page or env."""
    try:
        return int(os.environ.get("AUDIO_STREAM_THRESHOLD_MINUTES", "45"))
    except ValueError:
        return 45


async def _compute_session_detail(storage: Storage, session_id: int) -> dict[str, Any]:
    db = storage._conn()
    cur = await db.execute(
        "SELECT r.id, r.name, r.event, r.race_num, r.date,"
        " r.start_utc, r.end_utc, r.session_type,"
        " r.peer_fingerprint, r.peer_co_op_id,"
        " r.shared_name, r.match_group_id, r.match_confirmed,"
        " (SELECT COUNT(*) > 0 FROM positions p"
        "   WHERE p.ts >= r.start_utc AND p.ts <= COALESCE(r.end_utc, r.start_utc)"
        " ) AS has_track,"
        " (SELECT rv.youtube_url FROM race_videos rv"
        "   WHERE rv.race_id = r.id LIMIT 1) AS first_video_url"
        " FROM races r WHERE r.id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    start_utc = datetime.fromisoformat(row["start_utc"])
    end_utc = datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None
    duration_s = (end_utc - start_utc).total_seconds() if end_utc else None

    # Check for audio — prefer the primary sibling (ordinal 0) so
    # single-session callers keep the scalar audio_session_id.
    acur = await db.execute(
        "SELECT id, start_utc, channels, capture_group_id, capture_ordinal"
        " FROM audio_sessions"
        " WHERE race_id = ? AND session_type IN ('race','practice')"
        " ORDER BY capture_ordinal ASC, id ASC",
        (session_id,),
    )
    arow = await acur.fetchone()

    # Sibling-card block (#509): when the primary row is part of a capture
    # group, surface all siblings so session.js can drive N-source Web
    # Audio playback and show a merged transcript.
    audio_siblings: list[dict[str, Any]] = []
    if arow and arow["capture_group_id"]:
        group_rows = await storage.list_capture_group_siblings(str(arow["capture_group_id"]))
        for sr in group_rows:
            cmap = await storage.get_channel_map_for_audio_session(int(sr["id"]))
            position_name = cmap.get(0, f"R{int(sr['capture_ordinal']) + 1}")
            audio_siblings.append(
                {
                    "audio_session_id": int(sr["id"]),
                    "ordinal": int(sr["capture_ordinal"]),
                    "position_name": position_name,
                    "stream_url": f"/api/audio/{int(sr['id'])}/stream",
                }
            )

    # Debrief audio (#546): debriefs are stored as separate audio_sessions rows
    # with session_type='debrief' and the same race_id. They're sequential
    # recordings, not capture-group siblings, so surface them as their own
    # block the session page can render alongside the main audio card.
    debrief_audio: dict[str, Any] | None = None
    dcur = await db.execute(
        "SELECT id, start_utc, end_utc, capture_group_id, capture_ordinal"
        " FROM audio_sessions"
        " WHERE race_id = ? AND session_type = 'debrief'"
        " ORDER BY capture_ordinal ASC, id ASC LIMIT 1",
        (session_id,),
    )
    drow = await dcur.fetchone()
    if drow is not None:
        debrief_audio = {
            "audio_session_id": int(drow["id"]),
            "start_utc": datetime.fromisoformat(drow["start_utc"]).isoformat(),
            "stream_url": f"/api/audio/{int(drow['id'])}/stream",
        }
        # #648: surface debrief siblings so the session page can render a
        # multi-sibling player (sticky isolation + channel mixing) for
        # debriefs, just like the race player.
        debrief_siblings: list[dict[str, Any]] = []
        if drow["capture_group_id"]:
            debrief_group_rows = await storage.list_capture_group_siblings(
                str(drow["capture_group_id"])
            )
            for sr in debrief_group_rows:
                cmap = await storage.get_channel_map_for_audio_session(int(sr["id"]))
                position_name = cmap.get(0, f"R{int(sr['capture_ordinal']) + 1}")
                debrief_siblings.append(
                    {
                        "audio_session_id": int(sr["id"]),
                        "ordinal": int(sr["capture_ordinal"]),
                        "position_name": position_name,
                        "stream_url": f"/api/audio/{int(sr['id'])}/stream",
                    }
                )
        debrief_audio["siblings"] = debrief_siblings
        # Decide streaming vs decode for the debrief based on its own duration.
        debrief_duration_s: float | None = None
        if drow["end_utc"]:
            try:
                deb_start = datetime.fromisoformat(drow["start_utc"])
                deb_end = datetime.fromisoformat(drow["end_utc"])
                debrief_duration_s = (deb_end - deb_start).total_seconds()
            except (TypeError, ValueError):
                debrief_duration_s = None
        debrief_audio["use_streaming_audio"] = bool(
            len(debrief_siblings) > 1
            and debrief_duration_s is not None
            and debrief_duration_s >= _audio_stream_threshold_minutes() * 60
        )

    # Check for wind field params (synthesized sessions)
    wf_cur = await db.execute(
        "SELECT 1 FROM synth_wind_params WHERE session_id = ?",
        (session_id,),
    )
    has_wind_field = await wf_cur.fetchone() is not None

    # #648: hint the session page whether to stream audio via <audio> elements
    # instead of decoding each sibling into a memory buffer. Long multi-sibling
    # sessions (>~45 min × 2 siblings) blow Chrome's per-tab memory budget when
    # decodeAudioData expands PCM s16 into Float32 per channel.
    use_streaming_audio = bool(
        audio_siblings
        and duration_s is not None
        and duration_s >= _audio_stream_threshold_minutes() * 60
    )

    return {
        "id": row["id"],
        "type": row["session_type"],
        "name": row["name"],
        "event": row["event"],
        "race_num": row["race_num"],
        "date": row["date"],
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat() if end_utc else None,
        "duration_s": round(duration_s, 1) if duration_s is not None else None,
        "has_track": bool(row["has_track"]),
        "first_video_url": row["first_video_url"],
        "has_audio": arow is not None,
        "audio_session_id": arow["id"] if arow else None,
        "audio_start_utc": (
            datetime.fromisoformat(arow["start_utc"]).isoformat() if arow else None
        ),
        "audio_channels": (
            len(audio_siblings) if audio_siblings else (arow["channels"] if arow else None)
        ),
        "audio_siblings": audio_siblings,
        "use_streaming_audio": use_streaming_audio,
        "debrief_audio": debrief_audio,
        "peer_fingerprint": row["peer_fingerprint"],
        "has_wind_field": has_wind_field,
        "shared_name": row["shared_name"],
        "match_group_id": row["match_group_id"],
        "match_status": "confirmed"
        if row["match_confirmed"]
        else ("candidate" if row["match_group_id"] else "unmatched"),
    }


@router.get("/api/sessions/{session_id}/wind-field")
async def api_session_wind_field(
    request: Request,
    session_id: int,
    elapsed_s: float = 0.0,
    grid_size: int = 20,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Return a spatial grid of TWD/TWS values and course marks."""
    # The wind field is a function of (session_id, elapsed_s, grid_size). Bake
    # the non-race parameters into the T2 key_family so distinct query shapes
    # don't collide. data_hash still comes from the race row so race-mutation
    # invalidation works without bespoke hooks per parameter.
    clamped_grid = min(max(grid_size, 5), 40)
    key_family = f"wind_field:grid={clamped_grid}:t={elapsed_s:.3f}"
    storage = get_storage(request)

    async def _compute() -> dict[str, Any]:
        return await _compute_wind_field(storage, session_id, elapsed_s, clamped_grid)

    return await cached_json_response(
        request, race_id=session_id, key_family=key_family, compute=_compute
    )


async def _compute_wind_field(
    storage: Storage, session_id: int, elapsed_s: float, grid_size: int
) -> dict[str, Any]:
    from helmlog.wind_field import WindField

    params = await storage.get_synth_wind_params(session_id)
    if params is None:
        raise HTTPException(status_code=404, detail="No wind field for this session")

    marks = await storage.get_synth_course_marks(session_id)
    elapsed_s = max(0.0, min(elapsed_s, params["duration_s"]))

    # Compute bounding box from marks + 0.5 nm padding on all sides
    import math

    pad_nm = 0.5
    if marks:
        mark_lats = [m["lat"] for m in marks]
        mark_lons = [m["lon"] for m in marks]
        center_lat = (min(mark_lats) + max(mark_lats)) / 2
        cos_ref = math.cos(math.radians(center_lat))
        lat_min = min(mark_lats) - pad_nm / 60.0
        lat_max = max(mark_lats) + pad_nm / 60.0
        lon_min = min(mark_lons) - pad_nm / 60.0 / cos_ref
        lon_max = max(mark_lons) + pad_nm / 60.0 / cos_ref
    else:
        cos_ref = math.cos(math.radians(params["ref_lat"]))
        lat_min = params["ref_lat"] - pad_nm / 60.0
        lat_max = params["ref_lat"] + pad_nm / 60.0
        lon_min = params["ref_lon"] - pad_nm / 60.0 / cos_ref
        lon_max = params["ref_lon"] + pad_nm / 60.0 / cos_ref

    # Capture bounds for the thread
    bounds = (lat_min, lat_max, lon_min, lon_max)

    def _compute() -> list[dict[str, float]]:
        wf = WindField(
            base_twd=params["base_twd"],
            tws_low=params["tws_low"],
            tws_high=params["tws_high"],
            duration_s=params["duration_s"],
            shift_interval=(params["shift_interval_lo"], params["shift_interval_hi"]),
            shift_magnitude=(params["shift_magnitude_lo"], params["shift_magnitude_hi"]),
            ref_lat=params["ref_lat"],
            ref_lon=params["ref_lon"],
            seed=params["seed"],
        )
        b_lat_min, b_lat_max, b_lon_min, b_lon_max = bounds

        cells: list[dict[str, float]] = []
        for r in range(grid_size):
            lat = b_lat_min + (b_lat_max - b_lat_min) * r / (grid_size - 1)
            for c in range(grid_size):
                lon = b_lon_min + (b_lon_max - b_lon_min) * c / (grid_size - 1)
                twd, tws = wf.at(elapsed_s, lat, lon)
                cells.append(
                    {
                        "lat": round(lat, 6),
                        "lon": round(lon, 6),
                        "twd": round(twd, 1),
                        "tws": round(tws, 2),
                    }
                )
        return cells

    cells = await asyncio.to_thread(_compute)

    return {
        "elapsed_s": elapsed_s,
        "duration_s": params["duration_s"],
        "base_twd": params["base_twd"],
        "tws_low": params["tws_low"],
        "tws_high": params["tws_high"],
        "grid": {
            "rows": grid_size,
            "cols": grid_size,
            "lat_min": round(lat_min, 6),
            "lat_max": round(lat_max, 6),
            "lon_min": round(lon_min, 6),
            "lon_max": round(lon_max, 6),
            "cells": cells,
        },
        "marks": marks,
    }


@router.get("/api/sessions/{session_id}/wind-timeseries")
async def api_session_wind_timeseries(
    request: Request,
    session_id: int,
    step_s: int = 10,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return TWD/TWS time series at port, center, and starboard positions."""
    storage = get_storage(request)
    from helmlog.wind_field import WindField

    step_s = min(max(step_s, 5), 60)
    params = await storage.get_synth_wind_params(session_id)
    if params is None:
        raise HTTPException(status_code=404, detail="No wind field for this session")

    def _compute() -> dict[str, Any]:
        import math

        wf = WindField(
            base_twd=params["base_twd"],
            tws_low=params["tws_low"],
            tws_high=params["tws_high"],
            duration_s=params["duration_s"],
            shift_interval=(params["shift_interval_lo"], params["shift_interval_hi"]),
            shift_magnitude=(params["shift_magnitude_lo"], params["shift_magnitude_hi"]),
            ref_lat=params["ref_lat"],
            ref_lon=params["ref_lon"],
            seed=params["seed"],
        )
        cos_ref = math.cos(math.radians(params["ref_lat"]))
        offset_lon = 0.3 / 60.0 / cos_ref  # 0.3 nm cross-course

        positions = [
            {
                "label": "Port side",
                "lat": params["ref_lat"],
                "lon": round(params["ref_lon"] - offset_lon, 6),
            },
            {"label": "Center", "lat": params["ref_lat"], "lon": params["ref_lon"]},
            {
                "label": "Starboard side",
                "lat": params["ref_lat"],
                "lon": round(params["ref_lon"] + offset_lon, 6),
            },
        ]

        series: list[dict[str, Any]] = []
        t = 0.0
        while t <= params["duration_s"]:
            twd_vals = []
            tws_vals = []
            for p in positions:
                twd, tws = wf.at(t, p["lat"], p["lon"])
                twd_vals.append(round(twd, 1))
                tws_vals.append(round(tws, 2))
            series.append({"t": round(t, 1), "twd": twd_vals, "tws": tws_vals})
            t += step_s

        return {"positions": positions, "series": series}

    result = await asyncio.to_thread(_compute)

    return JSONResponse(
        {
            "duration_s": params["duration_s"],
            "step_s": step_s,
            "base_twd": params["base_twd"],
            "positions": result["positions"],
            "series": result["series"],
        }
    )


@router.get("/api/sessions/{session_id}/polar")
async def api_session_polar(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    import helmlog.polar as _polar

    data = await _polar.session_polar_comparison(storage, session_id)
    if data is None:
        return JSONResponse(
            {"cells": [], "tws_bins": [], "twa_bins": [], "session_sample_count": 0}
        )
    return JSONResponse(
        {
            "cells": [
                {
                    "tws": c.tws_bin,
                    "twa": c.twa_bin,
                    "point_of_sail": c.point_of_sail,
                    "tack": c.tack,
                    "baseline_mean": c.baseline_mean_bsp,
                    "baseline_p90": c.baseline_p90_bsp,
                    "session_mean": c.session_mean_bsp,
                    "samples": c.session_sample_count,
                    "delta": c.delta,
                }
                for c in data.cells
            ],
            "tws_bins": data.tws_bins,
            "twa_bins": data.twa_bins,
            "session_sample_count": data.session_sample_count,
        }
    )


@router.get("/api/sessions/{session_id}/replay")
@limiter.limit("30/minute")
async def api_session_replay(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """Return the payload the replay UI needs: session bounds, a per-second
    instrument series for the HUD, and per-segment polar grades (#464/#469).

    The instrument series is downsampled to 1 Hz keyed on the first 19 chars
    of the ISO timestamp so the frontend can binary-search by cursor position
    without loading the full raw tables. Fields are nulled out when a given
    sensor had no reading for that second.
    """

    storage = get_storage(request)

    async def _compute() -> dict[str, Any]:
        return await _compute_session_replay(storage, session_id)

    # v3: payload schema changed (prestart_start_utc + samples extended
    # backwards into the prestart window so the scrubber can range over
    # pre-gun data). Bump the family suffix to force recompute on the
    # cached blobs that lack the new field.
    return await cached_json_response(
        request, race_id=session_id, key_family="session_replay_v3", compute=_compute
    )


async def _compute_session_replay(storage: Storage, session_id: int) -> dict[str, Any]:
    db = storage._conn()
    cur = await db.execute("SELECT id, start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Race not found")
    start_utc = row["start_utc"]
    # Active race (end_utc IS NULL): use "now" as the upper bound so the
    # scrubber range and the instrument-sample queries cover everything up
    # to the current moment. Falling back to start_utc would collapse the
    # window to the prestart prefix only.
    end_utc = row["end_utc"] or datetime.now(UTC).isoformat()

    # Replay scrubber spans the prestart prefix too — same window the track
    # is extended by (#707 / prestart-scrub). Instrument series queries use
    # this lower bound so the HUD has data when the scrubber drops below
    # the gun. The race_gun marker still points at start_utc.
    try:
        _start_dt_for_prestart = datetime.fromisoformat(start_utc)
        prestart_start_utc = (
            _start_dt_for_prestart - timedelta(seconds=_PRESTART_WINDOW_S)
        ).isoformat()
    except ValueError:
        prestart_start_utc = start_utc

    # Effective race gun: for Vakaros-matched races, prefer the latest
    # race_start event inside the race window. Races that were recalled
    # have an earlier stored start_utc but a later real gun; the frontend
    # needs the real gun time to filter pre-gun "roundings" out of the
    # replay laylines. Falls back to start_utc when no Vakaros event is
    # available.
    gun_cur = await db.execute(
        """
        SELECT vre.ts
        FROM races r
        JOIN vakaros_race_events vre ON vre.session_id = r.vakaros_session_id
        WHERE r.id = ?
          AND vre.event_type = 'race_start'
          AND vre.ts BETWEEN ? AND ?
        ORDER BY vre.ts DESC
        LIMIT 1
        """,
        (
            session_id,
            start_utc,
            end_utc,
        ),
    )
    gun_row = await gun_cur.fetchone()
    race_gun_utc = gun_row["ts"] if gun_row is not None else start_utc

    # Graded segments (cached) — may be empty if session hasn't ended
    import helmlog.polar as _polar

    try:
        graded = await _polar.grade_session_segments(storage, session_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("replay: grading failed for session {}: {}", session_id, e)
        graded = []

    # Enrich each grade with tack + point_of_sail (#534). Cached grades carry
    # only unsigned TWA, so we re-derive tack from raw wind records. Parse
    # once, then sweep segments with a single pointer — O(n + m), not O(n*m).
    start_dt = datetime.fromisoformat(str(start_utc)).replace(tzinfo=UTC)
    end_dt = datetime.fromisoformat(str(end_utc)).replace(tzinfo=UTC)
    tack_winds_raw = await storage.query_range("winds", start_dt, end_dt)
    tack_hdgs_raw = await storage.query_range("headings", start_dt, end_dt)

    hdg_by_key: dict[str, float] = {}
    for hrec in tack_hdgs_raw:
        hdg_by_key.setdefault(str(hrec["ts"])[:19], float(hrec["heading_deg"]))

    wind_tacks: list[tuple[datetime, str]] = []
    for w in tack_winds_raw:
        ref = int(w.get("reference", -1))
        if ref not in (0, 4):
            continue
        heading = hdg_by_key.get(str(w["ts"])[:19]) if ref == 4 else None
        result = _polar._compute_twa_with_tack(float(w["wind_angle_deg"]), ref, heading)
        if result is None:
            continue
        _, tk = result
        wind_tacks.append((datetime.fromisoformat(str(w["ts"])).replace(tzinfo=UTC), tk))
    wind_tacks.sort(key=lambda x: x[0])

    grades_sorted = sorted(graded, key=lambda g: g.t_start)
    tacks_by_segment: dict[int, str | None] = {}
    wi = 0
    for g in grades_sorted:
        port = 0
        stbd = 0
        while wi < len(wind_tacks) and wind_tacks[wi][0] < g.t_start:
            wi += 1
        j = wi
        while j < len(wind_tacks) and wind_tacks[j][0] < g.t_end:
            if wind_tacks[j][1] == "port":
                port += 1
            else:
                stbd += 1
            j += 1
        if port == 0 and stbd == 0:
            tacks_by_segment[g.segment_index] = None
        else:
            tacks_by_segment[g.segment_index] = "starboard" if stbd >= port else "port"

    grades_out = []
    for g in graded:
        tack = tacks_by_segment.get(g.segment_index)
        pos = _polar._point_of_sail(g.twa_deg) if g.twa_deg is not None else None
        grades_out.append(
            {
                "i": g.segment_index,
                "t_start": g.t_start.isoformat(),
                "t_end": g.t_end.isoformat(),
                "lat": g.lat,
                "lon": g.lon,
                "tws": g.tws_kts,
                "twa": g.twa_deg,
                "bsp": g.bsp_kts,
                "target": g.target_bsp_kts,
                "pct": g.pct_target,
                "delta": g.delta_kts,
                "grade": g.grade,
                "tack": tack,
                "point_of_sail": pos,
            }
        )

    # Thin instrument series for HUD. 1 Hz dedup by truncated timestamp key.
    # Lower bound is prestart_start_utc so the scrubber has gauges in the
    # pre-gun window.
    async def _series(table: str, fields: list[str]) -> dict[str, dict[str, Any]]:
        cols = ", ".join(["ts", *fields])
        q = f"SELECT {cols} FROM {table} WHERE ts >= ? AND ts <= ? ORDER BY ts"
        qcur = await db.execute(q, (prestart_start_utc, end_utc))
        rows = await qcur.fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = str(r["ts"])[:19]
            if key in out:
                continue
            out[key] = {f: r[f] for f in fields}
        return out

    # Wind table holds both true (ref 0/4) and apparent (ref 2) rows; keep them
    # separated so the replay HUD can surface TWS/TWA and AWS/AWA independently.
    async def _wind_series(where: str) -> dict[str, dict[str, Any]]:
        q = (
            "SELECT ts, wind_speed_kts, wind_angle_deg, reference FROM winds "
            f"WHERE ts >= ? AND ts <= ? AND {where} ORDER BY ts"
        )
        qcur = await db.execute(q, (prestart_start_utc, end_utc))
        rows = await qcur.fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = str(r["ts"])[:19]
            if key in out:
                continue
            out[key] = {
                "wind_speed_kts": r["wind_speed_kts"],
                "wind_angle_deg": r["wind_angle_deg"],
                "reference": r["reference"],
            }
        return out

    speeds_by_s = await _series("speeds", ["speed_kts"])
    true_winds_by_s = await _wind_series("reference IN (0, 4)")
    app_winds_by_s = await _wind_series("reference = 2")
    hdgs_by_s = await _series("headings", ["heading_deg"])
    cogsog_by_s = await _series("cogsog", ["cog_deg", "sog_kts"])
    attitudes_by_s = await _series("attitudes", ["heel_deg", "trim_deg"])

    keys = sorted(
        set(speeds_by_s.keys())
        | set(true_winds_by_s.keys())
        | set(app_winds_by_s.keys())
        | set(hdgs_by_s.keys())
        | set(cogsog_by_s.keys())
        | set(attitudes_by_s.keys())
    )

    samples: list[dict[str, Any]] = []
    for k in keys:
        tw = true_winds_by_s.get(k)
        tws: float | None = None
        twa: float | None = None
        twd: float | None = None
        if tw is not None:
            raw_ref = tw.get("reference")
            ref = int(raw_ref) if raw_ref is not None else -1
            tws = float(tw["wind_speed_kts"]) if tw["wind_speed_kts"] is not None else None
            h = hdgs_by_s.get(k)
            heading = float(h["heading_deg"]) if h and h["heading_deg"] is not None else None
            wind_angle = float(tw["wind_angle_deg"])
            twa = _polar._compute_twa(wind_angle, ref, heading)
            # TWD is the compass direction the true wind is coming FROM.
            # ref NORTH: wind_angle is already TWD. ref BOAT: add heading.
            if ref == _polar._WIND_REF_NORTH:
                twd = wind_angle % 360
            elif ref == _polar._WIND_REF_BOAT and heading is not None:
                twd = (heading + wind_angle) % 360

        aw = app_winds_by_s.get(k)
        aws: float | None = None
        awa: float | None = None
        if aw is not None:
            aws = float(aw["wind_speed_kts"]) if aw["wind_speed_kts"] is not None else None
            awa = float(aw["wind_angle_deg"]) if aw["wind_angle_deg"] is not None else None

        sp = speeds_by_s.get(k)
        cs = cogsog_by_s.get(k)
        hd = hdgs_by_s.get(k)
        at = attitudes_by_s.get(k)
        stw_v = float(sp["speed_kts"]) if sp else None
        sog_v = float(cs["sog_kts"]) if cs else None
        cog_v = float(cs["cog_deg"]) if cs else None
        hdg_v = float(hd["heading_deg"]) if hd else None
        heel_v = float(at["heel_deg"]) if at and at["heel_deg"] is not None else None
        trim_v = float(at["trim_deg"]) if at and at["trim_deg"] is not None else None
        sd = compute_set_drift(
            sog=sog_v,
            cog=cog_v,
            stw=stw_v,
            hdg=hdg_v,
            heel_deg=heel_v,
            leeway_k=storage._leeway_k,
        )
        set_v: float | None = sd[0] if sd is not None else None
        drift_v: float | None = sd[1] if sd is not None else None
        samples.append(
            {
                "ts": k + "Z",
                "stw": stw_v,
                "sog": sog_v,
                "cog": cog_v,
                "hdg": hdg_v,
                "tws": tws,
                "twa": twa,
                "twd": twd,
                "aws": aws,
                "awa": awa,
                "heel": heel_v,
                "trim": trim_v,
                "set": set_v,
                "drift": drift_v,
            }
        )

    return {
        "session_id": session_id,
        # Normalize for the JS Date() parser: if the row already carries a
        # timezone indicator (isoformat on an aware UTC datetime produces
        # "...+00:00") leave it alone, otherwise append "Z". The previous
        # unconditional-append produced the invalid "...+00:00Z" that made
        # new Date() return Invalid Date and silently broke the scrubber,
        # time label, and YT sync.
        "start_utc": (start_utc if ("Z" in start_utc or "+" in start_utc) else start_utc + "Z"),
        "end_utc": end_utc if ("Z" in end_utc or "+" in end_utc) else end_utc + "Z",
        # Lower bound for the scrubber: start_utc minus the prestart window.
        # The scrubber uses this as its "0"; race_gun_utc still flags the
        # actual race start moment.
        "prestart_start_utc": (
            prestart_start_utc
            if ("Z" in prestart_start_utc or "+" in prestart_start_utc)
            else prestart_start_utc + "Z"
        ),
        # Effective race gun (prefers the latest Vakaros race_start
        # event inside the race window). Frontend uses this to filter
        # pre-gun "roundings" out of the replay laylines.
        "race_gun_utc": (
            race_gun_utc if ("Z" in race_gun_utc or "+" in race_gun_utc) else race_gun_utc + "Z"
        ),
        "segment_seconds": _polar.POLAR_SEGMENT_SECONDS,
        "grades": grades_out,
        "samples": samples,
    }


@router.get("/api/sessions/{session_id}/maneuvers")
async def api_session_maneuvers(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return detected maneuvers for a session with full per-maneuver metrics.

    Each entry carries entry/exit state (BSP, TWA, TWS, HDG), min BSP during
    the maneuver, turn angle and rate, distance-loss against an entry-vector
    reference, nearest GPS position, a quartile rank (good/avg/bad), and a
    ``youtube_url`` deep-link to the linked session video at the maneuver
    timestamp when a video is linked.
    """
    storage = get_storage(request)
    from helmlog.analysis.maneuvers import enrich_session_maneuvers

    enriched, _video_sync = await enrich_session_maneuvers(storage, session_id)
    # Attach tags in one batch query so the client-side tag filter can work
    # without N+1 round-trips (#587).
    ids = [m["id"] for m in enriched if m.get("id") is not None]
    tag_map = await storage.list_tags_for_entities("maneuver", ids)
    for m in enriched:
        mid = m.get("id")
        m["tags"] = tag_map.get(mid, []) if mid is not None else []
    return JSONResponse(enriched)


_MANEUVER_CSV_COLUMNS = [
    "ts",
    "type",
    "rank",
    "duration_sec",
    "entry_hdg",
    "exit_hdg",
    "turn_angle_deg",
    "turn_rate_deg_s",
    "entry_bsp",
    "exit_bsp",
    "min_bsp",
    "loss_kts",
    "distance_loss_m",
    "entry_twa",
    "exit_twa",
    "entry_tws",
    "exit_tws",
    "time_to_recover_s",
    "lat",
    "lon",
    "youtube_url",
]


@router.get("/api/sessions/{session_id}/maneuvers.csv")
async def api_session_maneuvers_csv(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """CSV export of the enriched maneuver list for a session."""
    from helmlog.analysis.maneuvers import enrich_session_maneuvers

    storage = get_storage(request)
    enriched, _ = await enrich_session_maneuvers(storage, session_id)

    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_MANEUVER_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for m in enriched:
        writer.writerow(
            {k: m.get(k, "") if m.get(k) is not None else "" for k in _MANEUVER_CSV_COLUMNS}
        )

    filename = f"session_{session_id}_maneuvers.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/sessions/{session_id}/maneuvers/compare")
async def api_session_maneuvers_compare(
    request: Request,
    session_id: int,
    ids: str = Query(..., description="Comma-separated maneuver IDs"),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return enriched maneuver data and video sync info for a set of maneuver IDs.

    Used by the maneuver comparison page to render multiple synced YouTube
    embeds side-by-side.
    """
    storage = get_storage(request)
    from helmlog.analysis.maneuvers import enrich_session_maneuvers

    try:
        requested_ids = {int(x.strip()) for x in ids.split(",") if x.strip()}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers") from exc

    if not requested_ids:
        raise HTTPException(status_code=422, detail="ids must not be empty")

    enriched, video_sync = await enrich_session_maneuvers(storage, session_id)
    selected = [m for m in enriched if m.get("id") in requested_ids]

    return JSONResponse({"maneuvers": selected, "video_sync": video_sync})


# Sentinel for synthesized race-start pseudo-maneuvers in cross-session URLs
# and browser payloads. Starts are not detected by maneuver_detector; they
# are generated on demand from the session's Vakaros race_start event so
# users can compare the gun moment across races.
_START_TOKEN = "S"


def _classify_rounding_mark(m: dict[str, Any]) -> str | None:
    """Tag a rounding as ``weather`` / ``leeward`` based on exit heading.

    After a weather (windward) mark the boat is on a downwind leg
    (exit_twa >= 90°). After a leeward mark the boat is on an upwind
    leg (exit_twa < 90°). Falls back to entry_twa with inverted logic
    when exit_twa is missing. Returns ``None`` for non-rounding
    maneuvers or when TWA data is unavailable.
    """
    if m.get("type") != "rounding":
        return None
    exit_twa = m.get("exit_twa")
    if exit_twa is not None:
        return "weather" if exit_twa >= 90 else "leeward"
    entry_twa = m.get("entry_twa")
    if entry_twa is not None:
        # Mirror logic: exiting inverts the mode — a rounding entered
        # downwind exits upwind (leeward mark) and vice versa.
        return "leeward" if entry_twa >= 90 else "weather"
    return None


def _parse_cross_session_ids(ids: str) -> list[tuple[int, int | str]]:
    """Parse ``ids`` query param for cross-session compare (#584).

    Accepts two forms in the same list:
      * ``<session_id>:<maneuver_id>`` — real detected maneuver
      * ``<session_id>:S`` — synthesized race start for that session

    Returns a list of ``(session_id, maneuver_id_or_token)`` tuples.
    Raises ``ValueError`` on malformed input.
    """
    pairs: list[tuple[int, int | str]] = []
    for raw in ids.split(","):
        token = raw.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(f"missing session_id in {token!r}")
        sid_str, _, mid_str = token.partition(":")
        if not sid_str or not mid_str:
            raise ValueError(f"malformed id {token!r}")
        sid = int(sid_str)
        if mid_str == _START_TOKEN:
            pairs.append((sid, _START_TOKEN))
        else:
            pairs.append((sid, int(mid_str)))
    return pairs


async def _vakaros_gun_times(
    db: Any,  # noqa: ANN401 — aiosqlite.Connection, kept generic to avoid import
    session_ids: list[int],
) -> dict[int, str | None]:
    """Return ``{session_id: gun_utc_or_None}`` for each requested session.

    The gun is the latest Vakaros ``race_start`` event inside the race
    window; ``None`` when no Vakaros event is matched (e.g. practice
    sessions or races sailed without a Vakaros device). Callers that
    need a best-effort fallback to ``start_utc`` should apply it on
    their own.
    """
    if not session_ids:
        return {}
    placeholders = ",".join("?" * len(session_ids))
    cur = await db.execute(
        f"""
        SELECT r.id AS session_id,
               (SELECT MAX(vre.ts)
                  FROM vakaros_race_events vre
                 WHERE vre.session_id = r.vakaros_session_id
                   AND vre.event_type = 'race_start'
                   AND vre.ts BETWEEN r.start_utc
                                  AND COALESCE(r.end_utc, r.start_utc)) AS gun_utc
          FROM races r
         WHERE r.id IN ({placeholders})
        """,
        session_ids,
    )
    return {r["session_id"]: r["gun_utc"] for r in await cur.fetchall()}


def _synth_start_entry(
    *,
    session_id: int,
    session_name: str,
    session_slug: str,
    session_start_utc: str | None,
    gun_utc: str,
    video_sync: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a synthetic ``type='start'`` maneuver payload for session gun.

    The cell covers a 60-second window starting at the gun so the compare
    page can render the crucial pre/post-gun moment. Video offset is
    computed against the race video's ``sync_utc`` so the YouTube player
    cues to the right point, matching how real maneuvers are deep-linked.
    """
    end_utc = None
    try:
        gun_dt = datetime.fromisoformat(gun_utc.replace(" ", "T").replace("Z", "+00:00"))
        end_utc = (gun_dt + timedelta(seconds=60)).isoformat()
    except ValueError:
        pass

    video_offset_s: float | None = None
    youtube_url: str | None = None
    if video_sync and video_sync.get("sync_utc"):
        try:
            sync_dt = datetime.fromisoformat(
                str(video_sync["sync_utc"]).replace(" ", "T").replace("Z", "+00:00")
            )
            gun_dt2 = datetime.fromisoformat(gun_utc.replace(" ", "T").replace("Z", "+00:00"))
            computed = (
                float(video_sync.get("sync_offset_s") or 0.0) + (gun_dt2 - sync_dt).total_seconds()
            )
            # Only emit a YouTube link when the gun actually lies inside
            # the video's recorded window.
            duration = float(video_sync.get("duration_s") or 0.0)
            if 0 <= computed <= (duration or computed + 1):
                video_offset_s = round(computed, 1)
                youtube_url = (
                    f"https://www.youtube.com/watch?v={video_sync['video_id']}"
                    f"&t={int(video_offset_s)}s"
                )
        except (ValueError, TypeError):
            pass

    return {
        "id": _START_TOKEN,
        "session_id": session_id,
        "session_name": session_name,
        "session_slug": session_slug,
        "session_start_utc": session_start_utc,
        "type": "start",
        "ts": gun_utc,
        "end_ts": end_utc,
        "duration_sec": 60.0,
        "loss_kts": None,
        "vmg_loss_kts": None,
        "tws_bin": None,
        "twa_bin": None,
        "details": {},
        "entry_bsp": None,
        "exit_bsp": None,
        "entry_hdg": None,
        "exit_hdg": None,
        "entry_twa": None,
        "exit_twa": None,
        "entry_tws": None,
        "exit_tws": None,
        "entry_sog": None,
        "min_bsp": None,
        "turn_angle_deg": None,
        "turn_rate_deg_s": None,
        "distance_loss_m": None,
        "time_to_recover_s": None,
        "track": None,
        "track_vakaros": None,
        "twd_deg": None,
        "ghost_m": None,
        "lat": None,
        "lon": None,
        "rank": None,
        "video_offset_s": video_offset_s,
        "youtube_url": youtube_url,
    }


@router.get("/api/maneuvers/compare")
async def api_cross_session_maneuvers_compare(
    request: Request,
    ids: str = Query(..., description="Comma-separated <session_id>:<maneuver_id> pairs"),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Cross-session version of the compare-page data feed (#584).

    ``ids`` is a comma-separated list of ``<session_id>:<maneuver_id>``
    pairs. Returns enriched maneuvers plus per-session ``video_sync`` so
    the compare page can render cells drawn from different sessions.
    """
    storage = get_storage(request)
    from helmlog.analysis.maneuvers import enrich_maneuvers_for_ids

    try:
        pairs = _parse_cross_session_ids(ids)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail="ids must be comma-separated <session_id>:<maneuver_id> pairs",
        ) from exc

    if not pairs:
        raise HTTPException(status_code=422, detail="ids must not be empty")

    # Split real maneuver ids from start pseudo-ids so the enrichment
    # helper only sees real rows.
    real_pairs: list[tuple[int, int]] = [(sid, mid) for sid, mid in pairs if isinstance(mid, int)]
    start_session_ids: list[int] = [sid for sid, mid in pairs if mid == _START_TOKEN]

    maneuvers: list[dict[str, Any]] = []
    video_sync_by_session: dict[int, dict[str, Any] | None] = {}
    if real_pairs:
        maneuvers, video_sync_by_session = await enrich_maneuvers_for_ids(storage, real_pairs)
        for m in maneuvers:
            if m.get("type") == "rounding":
                m["mark"] = _classify_rounding_mark(m)

    if start_session_ids:
        db = storage._conn()
        # Sessions that didn't contribute real maneuvers aren't in the
        # video_sync map yet — pull their race_video row directly.
        missing = [sid for sid in set(start_session_ids) if sid not in video_sync_by_session]
        for sid in missing:
            video_cur = await db.execute(
                "SELECT video_id, sync_utc, sync_offset_s, duration_s, youtube_url"
                " FROM race_videos WHERE race_id = ? ORDER BY id LIMIT 1",
                (sid,),
            )
            video_row = await video_cur.fetchone()
            if video_row is not None:
                video_sync_by_session[sid] = {
                    "video_id": video_row["video_id"],
                    "sync_utc": str(video_row["sync_utc"]),
                    "sync_offset_s": float(video_row["sync_offset_s"] or 0.0),
                    "duration_s": float(video_row["duration_s"] or 0.0),
                    "youtube_url": video_row["youtube_url"],
                }
            else:
                video_sync_by_session[sid] = None

        guns = await _vakaros_gun_times(db, list(set(start_session_ids)))
        for sid in start_session_ids:
            gun = guns.get(sid)
            if not gun:
                # No Vakaros gun → no synthetic start (would be meaningless
                # without a real race-start timestamp to anchor the clip).
                continue
            race = await storage.get_race(sid)
            if race is None:
                continue
            maneuvers.append(
                _synth_start_entry(
                    session_id=sid,
                    session_name=race.name,
                    session_slug=race.slug or "",
                    session_start_utc=race.start_utc.isoformat() if race.start_utc else None,
                    gun_utc=gun,
                    video_sync=video_sync_by_session.get(sid),
                )
            )

    return JSONResponse(
        {
            "maneuvers": maneuvers,
            "video_sync_by_session": {str(k): v for k, v in video_sync_by_session.items()},
        }
    )


@router.get("/api/maneuvers/overlay")
async def api_maneuvers_overlay(
    request: Request,
    ids: str = Query(..., description="Comma-separated <session_id>:<maneuver_id> pairs"),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Time-aligned multi-maneuver overlay series (#619).

    Takes ``<session_id>:<maneuver_id>`` pairs and returns per-maneuver
    boatspeed, heading-rate, and TWA series resampled to 1 Hz over
    ``[-20 s, +30 s]`` relative to each maneuver's ``head_to_wind_ts``
    (#613). Maneuvers with no HTW (roundings, stalls) are excluded with
    their ids listed in ``excluded_ids`` so the UI can show a notice.

    Cached via the T2 global path (``race_id=0`` sentinel) with a
    content-addressed ``data_hash`` that folds the sorted selection,
    every touched session's current ``compute_race_data_hash`` (so any
    race mutation changes the hash), and ``ENRICH_CACHE_VERSION`` (so
    enrichment-code bumps self-invalidate). Orphaned entries age out
    via TTL.
    """
    from helmlog.analysis.maneuvers import ENRICH_CACHE_VERSION, build_maneuvers_overlay
    from helmlog.cache import resolve_race_data_hash

    try:
        parsed = _parse_cross_session_ids(ids)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail="ids must be comma-separated <session_id>:<maneuver_id> pairs",
        ) from exc

    pairs: list[tuple[int, int]] = [(sid, mid) for sid, mid in parsed if isinstance(mid, int)]
    if not pairs:
        return JSONResponse(
            {"axis_s": list(range(-20, 31)), "channels": [], "maneuvers": [], "excluded_ids": []}
        )

    storage = get_storage(request)
    cache = get_web_cache(request)

    sorted_pairs = sorted(pairs)
    data_hash: str | None = None
    if cache is not None:
        try:
            session_hashes: dict[int, str] = {}
            for sid in sorted({sid for sid, _ in sorted_pairs}):
                h = await resolve_race_data_hash(storage, sid)
                if h:
                    session_hashes[sid] = h
            hash_input = json.dumps(
                {
                    "pairs": sorted_pairs,
                    "session_hashes": session_hashes,
                    "enrich_version": ENRICH_CACHE_VERSION,
                },
                sort_keys=True,
            )
            data_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
            hit = await cache.t2_get_global("maneuvers_overlay", data_hash=data_hash)
            if hit is not None:
                return JSONResponse(hit)
        except Exception:  # noqa: BLE001 — cache must never fail a request
            data_hash = None

    payload = await build_maneuvers_overlay(storage, sorted_pairs)

    if cache is not None and data_hash is not None:
        # 24 h TTL — orphaned entries (from races that mutate and
        # leave behind stale composite hashes) age out naturally.
        # Cache writes are best-effort; any failure is logged by the
        # cache layer and swallowed there.
        import contextlib

        with contextlib.suppress(Exception):
            await cache.t2_put_global(
                "maneuvers_overlay",
                data_hash=data_hash,
                value=payload,
                ttl_seconds=86400,
            )

    return JSONResponse(payload)


@router.get("/api/maneuvers/sessions")
async def api_maneuver_browse_sessions(
    request: Request,
    regatta_id: int | None = None,
    session_type: str | None = None,
    limit: int = 50,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Recent sessions with maneuver counts, for the browser picker (#584)."""
    storage = get_storage(request)
    limit = max(1, min(limit, 200))
    db = storage._conn()
    # Imported-results rows (source='clubspot' etc.) appear as races but were
    # never sailed by this boat — filter them out so the picker only shows
    # live-sailed sessions, and require maneuvers to also be present so
    # ghost races (duplicated across classes in the same regatta window)
    # don't clutter the list.
    sql = (
        "SELECT r.id, r.name, r.slug, r.start_utc, r.regatta_id, "
        "       reg.name AS regatta_name, "
        "       (SELECT COUNT(*) FROM maneuvers m WHERE m.session_id = r.id) AS maneuver_count "
        "  FROM races r "
        "  LEFT JOIN regattas reg ON reg.id = r.regatta_id "
    )
    if session_type is not None and session_type not in ("race", "practice"):
        raise HTTPException(status_code=422, detail="session_type must be race|practice")
    conds: list[str] = [
        "(r.source IS NULL OR r.source IN ('live', 'synthesized'))",
        "EXISTS (SELECT 1 FROM maneuvers m WHERE m.session_id = r.id)",
    ]
    params: list[Any] = []
    if regatta_id is not None:
        conds.append("r.regatta_id = ?")
        params.append(regatta_id)
    if session_type is not None:
        conds.append("r.session_type = ?")
        params.append(session_type)
    sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY r.start_utc DESC LIMIT ? "
    params.append(limit)
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    sessions = [
        {
            "id": r["id"],
            "name": r["name"],
            "slug": r["slug"] or "",
            "start_utc": r["start_utc"],
            "regatta_id": r["regatta_id"],
            "regatta_name": r["regatta_name"],
            "maneuver_count": r["maneuver_count"] or 0,
        }
        for r in rows
    ]
    return JSONResponse({"sessions": sessions})


@router.get("/api/maneuvers/regattas")
async def api_maneuver_browse_regattas(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Regattas that have at least one linked session, for the picker (#584)."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute(
        "SELECT reg.id, reg.name, reg.start_date, reg.end_date, "
        "       COUNT(r.id) AS session_count "
        "  FROM regattas reg "
        "  JOIN races r ON r.regatta_id = reg.id "
        " WHERE (r.source IS NULL OR r.source = 'live') "
        " GROUP BY reg.id "
        " ORDER BY reg.start_date DESC NULLS LAST, reg.id DESC"
    )
    rows = await cur.fetchall()
    regattas = [
        {
            "id": r["id"],
            "name": r["name"],
            "start_date": r["start_date"],
            "end_date": r["end_date"],
            "session_count": r["session_count"],
        }
        for r in rows
    ]
    return JSONResponse({"regattas": regattas})


@router.get("/api/maneuvers/browse")
async def api_maneuver_browse(
    request: Request,
    regatta_id: int | None = None,
    session_ids: str | None = None,
    session_type: str | None = None,
    type: str | None = None,
    direction: str | None = None,
    tws_min: float | None = None,
    tws_max: float | None = None,
    tws_bands: str | None = None,
    has_video: int = 0,
    post_start: int = 0,
    session_limit: int = 20,
    tags: str | None = None,
    tag_mode: str = "and",
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Cross-session maneuver browser feed (#584).

    Resolves a set of sessions from either ``regatta_id`` or an explicit
    comma-separated ``session_ids`` list. When neither is given, falls
    back to the most recent ``session_limit`` sessions. Returns enriched
    maneuvers filtered by type/direction/wind-range, with session context
    attached for rendering.
    """
    storage = get_storage(request)
    from helmlog.analysis.maneuvers import enrich_maneuvers_for_ids

    if type is not None and type not in (
        "tack",
        "gybe",
        "rounding",
        "weather",
        "leeward",
        "start",
    ):
        raise HTTPException(
            status_code=422,
            detail="type must be tack|gybe|rounding|weather|leeward|start",
        )
    if direction is not None and direction not in ("PS", "SP"):
        raise HTTPException(status_code=422, detail="direction must be PS|SP")
    if session_type is not None and session_type not in ("race", "practice"):
        raise HTTPException(status_code=422, detail="session_type must be race|practice")

    # Parse optional multi-band wind filter. Each band is "min-max" (e.g.
    # "8-10") or "min-" for an open-ended upper bound (e.g. "15-" for 15+
    # knots). Empty tokens are ignored.
    bands: list[tuple[float, float | None]] = []
    if tws_bands:
        for raw in tws_bands.split(","):
            token = raw.strip()
            if not token or "-" not in token:
                continue
            lo_s, _, hi_s = token.partition("-")
            try:
                lo = float(lo_s)
                hi: float | None = float(hi_s) if hi_s else None
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail=f"tws_bands token {token!r} must be numeric"
                ) from exc
            bands.append((lo, hi))

    resolved_session_ids: list[int] = []
    db = storage._conn()
    if session_ids:
        try:
            ids_in = [int(x.strip()) for x in session_ids.split(",") if x.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="session_ids must be integers") from exc
        # Apply session_type filter against the requested ids too so the
        # pill narrows the result set even when specific sessions are picked.
        if session_type is not None and ids_in:
            placeholders = ",".join("?" * len(ids_in))
            cur = await db.execute(
                f"SELECT id FROM races WHERE id IN ({placeholders}) AND session_type = ?",
                [*ids_in, session_type],
            )
            resolved_session_ids = [r["id"] for r in await cur.fetchall()]
        else:
            resolved_session_ids = ids_in
    elif regatta_id is not None:
        sql = "SELECT id FROM races  WHERE regatta_id = ?    AND (source IS NULL OR source IN ('live', 'synthesized')) "
        qparams: list[Any] = [regatta_id]
        if session_type is not None:
            sql += "   AND session_type = ? "
            qparams.append(session_type)
        sql += " ORDER BY start_utc DESC"
        cur = await db.execute(sql, qparams)
        resolved_session_ids = [r["id"] for r in await cur.fetchall()]
    else:
        session_limit = max(1, min(session_limit, 100))
        sql = "SELECT id FROM races WHERE (source IS NULL OR source IN ('live', 'synthesized')) "
        qparams = []
        if session_type is not None:
            sql += "  AND session_type = ? "
            qparams.append(session_type)
        sql += " ORDER BY start_utc DESC LIMIT ?"
        qparams.append(session_limit)
        cur = await db.execute(sql, qparams)
        resolved_session_ids = [r["id"] for r in await cur.fetchall()]

    if not resolved_session_ids:
        return JSONResponse({"maneuvers": [], "session_ids": []})

    # Pull every maneuver_id from the resolved sessions and enrich via the
    # shared helper — that keeps one code path for enrichment and reuses
    # the per-session cache. Skip this work when the user has narrowed
    # the filter to starts only, since starts are synthesized below.
    enriched: list[dict[str, Any]] = []
    browse_video_sync: dict[int, dict[str, Any] | None] = {}
    if type != "start":
        placeholders = ",".join("?" * len(resolved_session_ids))
        cur = await db.execute(
            f"SELECT session_id, id FROM maneuvers WHERE session_id IN ({placeholders})",
            resolved_session_ids,
        )
        pairs = [(r["session_id"], r["id"]) for r in await cur.fetchall()]
        enriched, browse_video_sync = await enrich_maneuvers_for_ids(storage, pairs)
        # Tag each rounding with weather/leeward so the client can display
        # the mark type and the weather/leeward type pills can filter.
        for m in enriched:
            if m.get("type") == "rounding":
                m["mark"] = _classify_rounding_mark(m)

    # Synthesize one "start" entry per resolved session that has a Vakaros
    # race_start event. Skip sessions where no gun was recorded — synthetic
    # starts are only useful when anchored to a real gun time.
    if type in (None, "start"):
        start_guns = await _vakaros_gun_times(db, resolved_session_ids)
        for sid, gun in start_guns.items():
            if not gun:
                continue
            race = await storage.get_race(sid)
            if race is None:
                continue
            vs = browse_video_sync.get(sid)
            if vs is None:
                # Sessions without real maneuvers aren't in the map; pull
                # video_sync directly.
                video_cur = await db.execute(
                    "SELECT video_id, sync_utc, sync_offset_s, duration_s, youtube_url"
                    " FROM race_videos WHERE race_id = ? ORDER BY id LIMIT 1",
                    (sid,),
                )
                video_row = await video_cur.fetchone()
                if video_row is not None:
                    vs = {
                        "video_id": video_row["video_id"],
                        "sync_utc": str(video_row["sync_utc"]),
                        "sync_offset_s": float(video_row["sync_offset_s"] or 0.0),
                        "duration_s": float(video_row["duration_s"] or 0.0),
                        "youtube_url": video_row["youtube_url"],
                    }
            enriched.append(
                _synth_start_entry(
                    session_id=sid,
                    session_name=race.name,
                    session_slug=race.slug or "",
                    session_start_utc=race.start_utc.isoformat() if race.start_utc else None,
                    gun_utc=gun,
                    video_sync=vs,
                )
            )

    # Optional: compute each session's effective race gun (latest Vakaros
    # race_start event inside the race window, falling back to start_utc)
    # so we can drop pre-gun maneuvers when post_start=1.
    gun_by_session: dict[int, str] = {}
    if post_start and resolved_session_ids:
        placeholders = ",".join("?" * len(resolved_session_ids))
        gun_cur = await db.execute(
            f"""
            SELECT r.id AS session_id,
                   COALESCE(
                     (SELECT MAX(vre.ts)
                        FROM vakaros_race_events vre
                       WHERE vre.session_id = r.vakaros_session_id
                         AND vre.event_type = 'race_start'
                         AND vre.ts BETWEEN r.start_utc
                                        AND COALESCE(r.end_utc, r.start_utc)),
                     r.start_utc
                   ) AS gun_utc
              FROM races r
             WHERE r.id IN ({placeholders})
            """,
            resolved_session_ids,
        )
        gun_by_session = {r["session_id"]: r["gun_utc"] for r in await gun_cur.fetchall()}

    def _keep(m: dict[str, Any]) -> bool:
        if type in ("weather", "leeward"):
            if m.get("type") != "rounding":
                return False
            if m.get("mark") != type:
                return False
        elif type is not None and m.get("type") != type:
            return False
        if direction is not None:
            ang = m.get("turn_angle_deg")
            if ang is None:
                return False
            is_ps = ang < 0
            if direction == "PS" and not is_ps:
                return False
            if direction == "SP" and is_ps:
                return False
        if tws_min is not None or tws_max is not None or bands:
            t = m.get("entry_tws")
            if t is None:
                return False
            if tws_min is not None and t < tws_min:
                return False
            if tws_max is not None and t > tws_max:
                return False
            if bands and not any(t >= lo and (hi is None or t <= hi) for lo, hi in bands):
                return False
        if post_start:
            sid = m.get("session_id")
            gun = gun_by_session.get(sid) if isinstance(sid, int) else None
            if gun and str(m.get("ts") or "") < gun:
                return False
        return not (has_video and not m.get("youtube_url"))

    filtered = [m for m in enriched if _keep(m)]
    filtered.sort(key=lambda m: str(m.get("ts") or ""))

    # Attach tags in one batch query so client-side tag filter chips can
    # render and filter without N+1 lookups (#587).
    maneuver_ids = [m["id"] for m in filtered if m.get("id") is not None]
    tag_map = await storage.list_tags_for_entities("maneuver", maneuver_ids)
    for m in filtered:
        mid = m.get("id")
        m["tags"] = tag_map.get(mid, []) if mid is not None else []

    # Compute available_tags (counts per tag) across the non-tag-filtered
    # set so the client-side chip row can always offer every tag that
    # could be added to the filter, regardless of which tags are active.
    available_counts: dict[int, dict[str, Any]] = {}
    for m in filtered:
        for t in m.get("tags") or []:
            entry = available_counts.setdefault(
                t["id"], {"id": t["id"], "name": t["name"], "color": t["color"], "count": 0}
            )
            entry["count"] += 1
    available_tags = sorted(available_counts.values(), key=lambda t: t["name"])

    # Optional tag filter — AND/OR over the selected tag ids against each
    # maneuver's attached tags. Unknown tag ids are silently dropped.
    if tags:
        try:
            tag_ids = [int(s) for s in tags.split(",") if s.strip()]
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="tags must be comma-separated ints"
            ) from exc
        if tag_mode not in {"and", "or"}:
            raise HTTPException(status_code=400, detail="tag_mode must be 'and' or 'or'")
        if tag_ids:
            wanted = set(tag_ids)

            def _tag_match(m: dict[str, Any]) -> bool:
                have = {t["id"] for t in m.get("tags") or []}
                if tag_mode == "or":
                    return bool(have & wanted)
                return wanted.issubset(have)

            filtered = [m for m in filtered if _tag_match(m)]

    return JSONResponse(
        {
            "maneuvers": filtered,
            "session_ids": resolved_session_ids,
            "available_tags": available_tags,
        }
    )


@router.post("/api/sessions/{session_id}/detect-maneuvers", status_code=202)
async def api_detect_maneuvers(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Trigger maneuver detection (or re-detection) for a session.

    Returns immediately with the count of detected maneuvers.
    """
    storage = get_storage(request)
    from helmlog.maneuver_detector import detect_maneuvers

    # Verify session exists
    db = storage._conn()
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    maneuvers = await detect_maneuvers(storage, session_id)
    return JSONResponse(
        {
            "session_id": session_id,
            "detected": len(maneuvers),
            "tacks": sum(1 for m in maneuvers if m.type == "tack"),
            "gybes": sum(1 for m in maneuvers if m.type == "gybe"),
            "roundings": sum(1 for m in maneuvers if m.type == "rounding"),
        },
        status_code=202,
    )


_SESSIONS_LIST_TTL_S: float = 60.0


@router.get("/api/sessions")
async def api_sessions(
    request: Request,
    q: str | None = None,
    type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    tags: str | None = None,
    tag_mode: str = "and",
    limit: int = 25,
    offset: int = 0,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    if type is not None and type not in ("race", "practice", "debrief", "synthesized"):
        raise HTTPException(
            status_code=422,
            detail="type must be 'race', 'practice', 'debrief', or 'synthesized'",
        )
    limit = max(1, min(limit, 200))

    # Build a cache key from the filter tuple. 60s TTL keeps this bounded
    # even across many filter combinations; race mutations also drop the
    # whole `sessions_list` family via the invalidation hook in cache.py.
    import hashlib

    key_payload = f"q={q}|type={type}|from={from_date}|to={to_date}|tags={tags}|mode={tag_mode}|limit={limit}|offset={offset}"
    key_hash = hashlib.sha256(key_payload.encode()).hexdigest()[:16]
    cache_key = f"sessions_list:{key_hash}"

    async def _compute() -> dict[str, Any]:
        storage = get_storage(request)
        # Tag filter — if tag ids are supplied, narrow to sessions whose row OR
        # any constituent entity (maneuver / bookmark / thread) carries the
        # tags. We pre-compute the full matching-id set up front, then page
        # through it after list_sessions applies the other filters, so total
        # is accurate.
        tag_filter_ids: set[int] | None = None
        if tags:
            try:
                tag_ids = [int(s) for s in tags.split(",") if s.strip()]
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="tags must be comma-separated ints"
                ) from exc
            if tag_ids:
                try:
                    tag_filter_ids = set(
                        await storage.sessions_matching_tags(tag_ids, mode=tag_mode)
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                if not tag_filter_ids:
                    return {"total": 0, "sessions": []}

        # Pull the full set of sessions matching non-tag filters so we can
        # compute available_tags across it. The chip row shows every tag
        # reachable from the current non-tag filters — without this,
        # selecting one tag would collapse the chip row down to just that
        # tag and the user couldn't add a second one for AND/OR.
        _all_total, all_non_tag_matches = await storage.list_sessions(
            q=q or None,
            session_type=type,
            from_date=from_date,
            to_date=to_date,
            limit=10_000,
            offset=0,
        )
        if tag_filter_ids is not None:
            filtered = [s for s in all_non_tag_matches if s["id"] in tag_filter_ids]
            total = len(filtered)
            sessions = filtered[offset : offset + limit]
        else:
            total = _all_total
            sessions = all_non_tag_matches[offset : offset + limit]

        # Aggregate available tags across ALL non-tag-filtered sessions so
        # the chip row doesn't collapse when the user applies a tag filter.
        all_ids = [s["id"] for s in all_non_tag_matches]
        all_tag_summary = await storage.list_session_tag_summary(all_ids)
        available_counts: dict[int, dict[str, Any]] = {}
        for sid in all_ids:
            for r in all_tag_summary.get(sid, []):
                entry = available_counts.setdefault(
                    r["id"],
                    {"id": r["id"], "name": r["name"], "color": r["color"], "count": 0},
                )
                entry["count"] += r["count"]
        available_tags = sorted(available_counts.values(), key=lambda t: t["name"])

        # Per-session tag summary only needed for the visible page.
        page_tag_summary = await storage.list_session_tag_summary([s["id"] for s in sessions])
        for s in sessions:
            s["tag_summary"] = page_tag_summary.get(s["id"], [])

        return {"total": total, "sessions": sessions, "available_tags": available_tags}

    return await t1_cached_json_response(
        request,
        cache_key=cache_key,
        ttl_seconds=_SESSIONS_LIST_TTL_S,
        compute=_compute,
    )


@router.get("/api/grafana/annotations")
async def api_grafana_annotations(
    request: Request,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = None,
    sessionId: int | None = None,  # noqa: N803
) -> JSONResponse:
    """Grafana SimpleJSON annotation feed.

    Grafana passes epoch milliseconds as ``from`` and ``to``.
    Optional ``sessionId`` scopes results to a single race or practice.
    """
    storage = get_storage(request)
    if from_ is None or to is None:
        return JSONResponse([])
    start = datetime.fromtimestamp(from_ / 1000.0, tz=UTC)
    end = datetime.fromtimestamp(to / 1000.0, tz=UTC)
    race_id: int | None = None
    if sessionId is not None:
        race_id, _audio = await _resolve_session(request, sessionId)

    # Query moments that fall in the requested window. If sessionId was
    # supplied, narrow to that race.
    db = storage._conn()
    where = "m.anchor_t_start IS NOT NULL AND m.anchor_t_start >= ?"
    params: list[Any] = [start.isoformat()]
    where += " AND m.anchor_t_start <= ?"
    params.append(end.isoformat())
    if race_id is not None:
        where += " AND m.session_id = ?"
        params.append(race_id)
    cur = await db.execute(
        f"SELECT m.id, m.subject, m.anchor_t_start,"  # noqa: S608
        f" (SELECT c.body FROM comments c WHERE c.moment_id = m.id"
        f"  ORDER BY c.created_at LIMIT 1) AS first_comment,"
        f" (SELECT ma.path FROM moment_attachments ma WHERE ma.moment_id = m.id"
        f"  AND ma.kind = 'photo' ORDER BY ma.id LIMIT 1) AS photo_path"
        f" FROM moments m WHERE {where}"
        f" ORDER BY m.anchor_t_start",
        params,
    )
    rows = [dict(r) for r in await cur.fetchall()]
    result = []
    for n in rows:
        ts_ms = int(datetime.fromisoformat(n["anchor_t_start"]).timestamp() * 1000)
        body = n["first_comment"] or n["subject"] or ""
        photo_path = n["photo_path"]
        title = "Photo" if photo_path else "Moment"
        text = body
        if photo_path:
            photo_url = f"/attachments/{photo_path}"
            text = f'<img src="{photo_url}" style="max-width:300px"/>'
            if body:
                text = body + "<br/>" + text
        result.append(
            {
                "time": ts_ms,
                "timeEnd": ts_ms,
                "title": title,
                "text": text,
                "tags": [title.lower()],
            }
        )
    return JSONResponse(result, headers={"Access-Control-Allow-Origin": "*"})


# ------------------------------------------------------------------
# /api/boat-settings
# ------------------------------------------------------------------


@router.delete("/api/sessions/{session_id}", status_code=204)
async def api_delete_session(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Delete a session and all related data (admin only)."""
    storage = get_storage(request)
    cur = await storage._conn().execute(
        "SELECT name, end_utc FROM races WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["end_utc"] is None:
        raise HTTPException(status_code=409, detail="Cannot delete an active session")
    files = await storage.delete_race_session(session_id)
    # Clean up physical files
    for f in files:
        p = Path(f)
        if p.exists():
            await asyncio.to_thread(p.unlink)
            logger.info("Deleted file: {}", p)
    await audit(request, "session.delete", detail=row["name"], user=_user)


@router.patch("/api/sessions/{session_id}")
async def api_rename_session(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Rename a race session and regenerate its slug (#449).

    Body: ``{"name": str?, "event": str?, "race_num": int?}`` — at least one
    field is required. When *name* is omitted but *event* or *race_num*
    changes, the name is regenerated from the standard ``build_race_name``
    template. Admin only.
    """
    storage = get_storage(request)
    body = await request.json()
    raw_name = body.get("name")
    raw_event = body.get("event")
    raw_race_num = body.get("race_num")
    if raw_name is None and raw_event is None and raw_race_num is None:
        raise HTTPException(status_code=422, detail="name, event, or race_num required")

    new_name = str(raw_name) if raw_name is not None else None
    new_event = str(raw_event).strip() if raw_event is not None else None
    if new_event == "":
        raise HTTPException(status_code=422, detail="event must not be blank")
    try:
        new_race_num = int(raw_race_num) if raw_race_num is not None else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="race_num must be an integer") from exc

    try:
        updated, retired_slug = await storage.rename_race(
            session_id,
            new_name=new_name,
            new_event=new_event,
            new_race_num=new_race_num,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        if str(exc) == "name_taken":
            raise HTTPException(status_code=409, detail={"error": "name_taken"}) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    changed = retired_slug is not None or updated.renamed_at is not None
    if changed:
        detail = f"{session_id}: {retired_slug!r} → {updated.slug!r} ({updated.name!r})"
        await audit(request, "race.rename", detail=detail, user=_user)
    return JSONResponse(
        {
            "id": updated.id,
            "name": updated.name,
            "event": updated.event,
            "race_num": updated.race_num,
            "slug": updated.slug,
            "renamed_at": updated.renamed_at.isoformat() if updated.renamed_at else None,
            "retired_slug": retired_slug,
            "url": (
                f"/session/{updated.id}/{updated.slug}"
                if updated.slug
                else f"/session/{updated.id}"
            ),
        }
    )
