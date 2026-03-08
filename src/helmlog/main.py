"""Entry point — wires modules together and runs the async logging loop.

Business logic lives in the other modules; this module only orchestrates them.

Subcommands:
  run     Start the logging loop (default behaviour).
  export  Export a time range to CSV.
  status  Show DB row counts and last-seen timestamps per PGN table.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from datetime import UTC, datetime

from loguru import logger


def _load_env() -> None:
    """Load .env file if present (best-effort).

    When running as a dedicated service account, .env may not be readable
    (systemd injects env vars from EnvironmentFile as root). Ignore that too.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except (ImportError, PermissionError):  # pragma: no cover
        pass


def _setup_logging() -> None:
    import os

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level=log_level)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def _weather_loop(storage: object, fetcher: object) -> None:
    """Background task: fetch weather from Open-Meteo every hour and persist it.

    Best-effort — logs a warning and continues if the fetch fails or if no
    position data is available yet.
    """
    from helmlog.external import ExternalFetcher
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    assert isinstance(fetcher, ExternalFetcher)

    while True:
        try:
            pos = await storage.latest_position()
            if pos is not None:
                now = datetime.now(UTC)
                reading = await fetcher.fetch_weather(
                    float(pos["latitude_deg"]),
                    float(pos["longitude_deg"]),
                    now,
                )
                if reading is not None:
                    await storage.write_weather(reading)
            else:
                logger.debug("No position data yet; skipping weather fetch")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Weather loop error (will retry next hour): {}", exc)

        await asyncio.sleep(3600)


async def _tide_loop(storage: object, fetcher: object) -> None:
    """Background task: fetch NOAA tide predictions daily and persist them.

    Fetches today's and tomorrow's hourly predictions at startup, then every
    24 hours. Using two days ensures full coverage when logging spans midnight.
    INSERT OR IGNORE makes re-fetching idempotent.
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _datetime

    from helmlog.external import ExternalFetcher
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    assert isinstance(fetcher, ExternalFetcher)

    while True:
        try:
            pos = await storage.latest_position()
            if pos is not None:
                lat = float(pos["latitude_deg"])
                lon = float(pos["longitude_deg"])
                now = _datetime.now(UTC)
                for delta in (0, 1):  # today and tomorrow
                    target_date = (now + timedelta(days=delta)).date()
                    readings = await fetcher.fetch_tide_predictions(lat, lon, target_date)
                    for reading in readings:
                        await storage.write_tide(reading)
            else:
                logger.debug("No position data yet; skipping tide fetch")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Tide loop error (will retry in 24h): {}", exc)

        await asyncio.sleep(86400)  # re-fetch once per day


async def _web_loop(
    storage: object,
    recorder: object | None = None,
    audio_config: object | None = None,
) -> None:
    """Background task: serve the race-marking web interface on WEB_PORT (default 3002).

    If *recorder* and *audio_config* are provided, audio recording is tied to
    race start/end events via the web interface.  Cameras are loaded from the
    database dynamically.
    """
    import uvicorn

    from helmlog.audio import AudioConfig, AudioRecorder
    from helmlog.races import RaceConfig
    from helmlog.storage import Storage
    from helmlog.web import create_app

    assert isinstance(storage, Storage)
    _recorder = recorder if isinstance(recorder, AudioRecorder) else None
    _audio_config = audio_config if isinstance(audio_config, AudioConfig) else None
    try:
        cfg = RaceConfig()
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(storage, _recorder, _audio_config),
                host=cfg.web_host,
                port=cfg.web_port,
                log_level="warning",
                access_log=False,
            )
        )
        server.install_signal_handlers = False  # type: ignore[attr-defined]
        logger.info("Web interface: http://{}:{}", cfg.web_host, cfg.web_port)
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
    except Exception:
        logger.exception("Web server failed to start")
        raise


async def _run() -> None:
    """Main async loop: read instrument data, decode, persist.

    Data source is selected by the DATA_SOURCE environment variable:
      signalk (default) — consume the Signal K WebSocket feed (SK owns can0)
      can               — read raw CAN frames directly (legacy mode)
    """
    import os

    from helmlog.external import ExternalFetcher
    from helmlog.storage import Storage, StorageConfig

    # Cancel this task on SIGTERM so finally blocks run and storage is flushed.
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    assert current is not None
    loop.add_signal_handler(signal.SIGTERM, current.cancel)

    data_source = os.environ.get("DATA_SOURCE", "signalk").lower()
    storage_config = StorageConfig()
    storage = Storage(storage_config)
    await storage.connect()

    # Seed os.environ from DB-persisted settings so synchronous consumers
    # (cameras, races, etc.) pick up admin overrides without refactoring.
    for row in await storage.list_settings():
        os.environ.setdefault(row["key"], row["value"])

    from helmlog.audio import AudioConfig, AudioRecorder

    audio_config = AudioConfig()
    recorder = AudioRecorder()

    # Seed cameras table from env var on first run, then load from DB
    cameras_str = os.environ.get("CAMERAS", "")
    if cameras_str:
        await storage.seed_cameras_from_env(cameras_str)

    from helmlog.external import external_data_enabled
    from helmlog.monitor import monitor_loop

    async with ExternalFetcher() as fetcher:
        if external_data_enabled():
            weather_task = asyncio.create_task(_weather_loop(storage, fetcher))
            tide_task = asyncio.create_task(_tide_loop(storage, fetcher))
        else:
            logger.info("External data fetching disabled (EXTERNAL_DATA_ENABLED=false)")
            weather_task = asyncio.create_task(asyncio.sleep(1e9))  # no-op placeholder
            tide_task = asyncio.create_task(asyncio.sleep(1e9))
        web_task = asyncio.create_task(_web_loop(storage, recorder, audio_config))
        monitor_task = asyncio.create_task(monitor_loop())
        try:
            if data_source == "signalk":
                from helmlog.sk_reader import SKReader, SKReaderConfig

                sk_config = SKReaderConfig()
                logger.info(
                    "Logger starting: source=signalk host={}:{} db={}",
                    sk_config.host,
                    sk_config.port,
                    storage_config.db_path,
                )
                async for record in SKReader(sk_config):
                    storage.update_live(record)
                    if storage.session_active:
                        await storage.write(record)
            else:
                from helmlog.can_reader import CANReader, CANReaderConfig, extract_pgn
                from helmlog.nmea2000 import decode

                can_config = CANReaderConfig()
                logger.info(
                    "Logger starting: source=can interface={} db={}",
                    can_config.interface,
                    storage_config.db_path,
                )
                async for frame in CANReader(can_config):
                    pgn = extract_pgn(frame.arbitration_id)
                    src = frame.arbitration_id & 0xFF
                    decoded = decode(pgn, frame.data, src, frame.timestamp)
                    if decoded is not None:
                        storage.update_live(decoded)
                        if storage.session_active:
                            await storage.write(decoded)
        except asyncio.CancelledError:
            logger.info("Shutdown signal received — flushing and stopping")
        finally:
            weather_task.cancel()
            tide_task.cancel()
            web_task.cancel()
            monitor_task.cancel()
            await asyncio.gather(
                weather_task, tide_task, web_task, monitor_task, return_exceptions=True
            )
            await storage.close()
            logger.info("Logger stopped")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


async def _export(start_iso: str, end_iso: str, out: str) -> None:
    """Export a time range from the DB to CSV."""
    from helmlog.export import export_to_file
    from helmlog.storage import Storage, StorageConfig

    try:
        start = datetime.fromisoformat(start_iso).replace(tzinfo=UTC)
        end = datetime.fromisoformat(end_iso).replace(tzinfo=UTC)
    except ValueError as exc:
        logger.error("Invalid datetime: {}", exc)
        sys.exit(1)

    if end <= start:
        logger.error("--end must be after --start")
        sys.exit(1)

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        rows = await export_to_file(storage, start, end, out)
        logger.info("Wrote {} rows to {}", rows, out)
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def _status() -> None:
    """Print row counts and last-seen timestamps for each data table."""
    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        summary = await storage.status_summary()
    finally:
        await storage.close()

    print(f"{'Table':<20} {'Rows':>8}  {'Last seen'}")
    print("-" * 55)
    for table, info in summary.items():
        print(f"{table:<20} {info['count']:>8}  {info['last_seen']}")


# ---------------------------------------------------------------------------
# link-video
# ---------------------------------------------------------------------------


async def _link_video(url: str, sync_utc_iso: str, sync_offset_s: float) -> None:
    """Fetch YouTube metadata and store a VideoSession sync point."""
    from helmlog.storage import Storage, StorageConfig
    from helmlog.video import VideoLinker

    try:
        sync_utc = datetime.fromisoformat(sync_utc_iso).replace(tzinfo=UTC)
    except ValueError as exc:
        logger.error("Invalid datetime: {}", exc)
        sys.exit(1)

    linker = VideoLinker()
    session = await linker.create_session(url, sync_utc, sync_offset_s)

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        await storage.write_video_session(session)
    finally:
        await storage.close()

    # Print a quick sanity-check URL at the sync point itself
    check = session.url_at(sync_utc)
    logger.info("Linked. Verify sync point: {}", check)


# ---------------------------------------------------------------------------
# list-videos
# ---------------------------------------------------------------------------


async def _list_videos() -> None:
    """Print all linked YouTube video sessions."""
    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        sessions = await storage.list_video_sessions()
    finally:
        await storage.close()

    if not sessions:
        print("No videos linked.")
        return

    print(f"{'Title':<42} {'Duration':>8}  {'Sync UTC'}")
    print("-" * 80)
    for s in sessions:
        h, rem = divmod(int(s.duration_s), 3600)
        m, sec = divmod(rem, 60)
        dur = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        print(f"{s.title[:42]:<42} {dur:>8}  {s.sync_utc.isoformat()}")
        print(f"  {s.url}")


# ---------------------------------------------------------------------------
# list-cameras
# ---------------------------------------------------------------------------


async def _list_cameras() -> None:
    """Print configured cameras and ping each for status."""
    from helmlog.cameras import Camera, get_status
    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        rows = await storage.list_cameras()
    finally:
        await storage.close()

    if not rows:
        print("No cameras configured. Use the admin UI or CAMERAS env var.")
        return

    cameras = [
        Camera(
            name=r["name"],
            ip=r["ip"],
            model=r["model"],
            wifi_ssid=r.get("wifi_ssid"),
            wifi_password=r.get("wifi_password"),
        )
        for r in rows
    ]

    print(f"{'Name':<16} {'IP':<18} {'WiFi SSID':<22} {'Recording':>10}  {'Status'}")
    print("-" * 85)

    for camera in cameras:
        status = await get_status(camera)
        rec = "YES" if status.recording else "no"
        err = status.error or "OK"
        ssid = camera.wifi_ssid or "—"
        print(f"{camera.name:<16} {camera.ip:<18} {ssid:<22} {rec:>10}  {err}")


# ---------------------------------------------------------------------------
# link-channel-videos
# ---------------------------------------------------------------------------


async def _link_channel_videos(channel_id: str, *, auto_confirm: bool, dry_run: bool) -> None:
    """Match YouTube channel videos to Gaia GPS-imported races by date + title keywords."""
    import re
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from helmlog.storage import Storage, StorageConfig

    pacific = ZoneInfo("America/Los_Angeles")

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        # 1. Fetch all races imported from Gaia GPS
        db = storage._conn()
        cur = await db.execute(
            "SELECT id, name, event, date, start_utc, end_utc"
            " FROM races WHERE source = 'gaiagps' ORDER BY date",
        )
        races = [dict(r) for r in await cur.fetchall()]
        if not races:
            print("No Gaia GPS races found in the database.")
            return

        # 2. Fetch all videos from the YouTube channel
        print(f"Fetching videos from channel {channel_id}...")
        yt_videos = await asyncio.to_thread(_fetch_all_channel_videos, channel_id)
        if not yt_videos:
            print("No videos found on channel.")
            return
        print(f"Found {len(yt_videos)} videos on channel.")

        # 3. Check which video_ids are already linked
        cur2 = await db.execute("SELECT video_id FROM race_videos")
        already_linked = {r["video_id"] for r in await cur2.fetchall()}

        # 4. Parse date/time from each video title and match to races
        # Title patterns: "VID YYYYMMDD HHMMSS ...", "YYYYMMDD HHMMSS ...",
        #                  "YYYYMMDD ...", "YYMMDD ..."
        date_time_re = re.compile(r"(?:VID\s+)?(\d{8})\s+(\d{6})\b", re.IGNORECASE)
        date_only_re = re.compile(r"(?:VID\s+)?(\d{8})\b", re.IGNORECASE)
        short_date_re = re.compile(r"^(\d{6})\b")  # YYMMDD

        matches: list[tuple[dict[str, object], dict[str, object], str]] = []
        unmatched_videos: list[dict[str, object]] = []

        for vid in yt_videos:
            vid_id = str(vid.get("id", ""))
            if vid_id in already_linked:
                continue

            title = str(vid.get("title", ""))
            duration = float(str(vid.get("duration") or 0))
            if duration < 60:
                continue  # skip short clips/trailers

            # Parse date and optional time from title
            vid_date_str: str | None = None
            vid_utc: datetime | None = None

            m = date_time_re.search(title)
            if m:
                vid_date_str = m.group(1)
                # Convert local recording time to UTC
                try:
                    local_dt = datetime.strptime(
                        f"{m.group(1)} {m.group(2)}", "%Y%m%d %H%M%S"
                    ).replace(tzinfo=pacific)
                    vid_utc = local_dt.astimezone(UTC)
                except ValueError:
                    pass
            else:
                m2 = date_only_re.search(title)
                if m2:
                    vid_date_str = m2.group(1)
                else:
                    m3 = short_date_re.search(title)
                    if m3:
                        vid_date_str = "20" + m3.group(1)

            if not vid_date_str:
                unmatched_videos.append(vid)
                continue

            # Find races on the same date
            # The video date might be the local date (PDT), while race date is
            # based on UTC. A race at 7pm PDT = 2am UTC next day. So check
            # both the video date and the day before.
            try:
                vd = datetime.strptime(vid_date_str, "%Y%m%d").date()
            except ValueError:
                unmatched_videos.append(vid)
                continue

            from datetime import date as _date

            candidates = [
                r for r in races if _date.fromisoformat(r["date"]) in (vd, vd + timedelta(days=1))
            ]

            if not candidates:
                unmatched_videos.append(vid)
                continue

            # Score candidates by keyword overlap with video title
            best_race = _best_race_match(title, candidates, vid_utc)
            if best_race:
                reason = "date+time" if vid_utc else "date+keywords"
                matches.append((vid, best_race, reason))
            else:
                unmatched_videos.append(vid)

        # 5. Print report
        if matches:
            print(f"\n{'Video Title':<55} {'Race Event':<35} {'Match'}")
            print("-" * 100)
            for vid, race, reason in matches:
                vt = str(vid.get("title", ""))[:55]
                re_ = str(race["event"])[:35]
                print(f"{vt:<55} {re_:<35} {reason}")

        if unmatched_videos:
            print(f"\n--- {len(unmatched_videos)} unmatched video(s) ---")
            for vid in unmatched_videos[:10]:
                print(f"  {vid.get('title')}")
            if len(unmatched_videos) > 10:
                print(f"  ... and {len(unmatched_videos) - 10} more")

        print(
            f"\n{len(matches)} match(es), {len(unmatched_videos)} unmatched,"
            f" {len(already_linked)} already linked."
        )

        if dry_run:
            print("\nDry run — no changes written.")
            return

        if not matches:
            return

        if not auto_confirm:
            print("\nUse --yes to write these links to the database.")
            return

        # 6. Write matches to DB
        linked = 0
        for vid, race, _reason in matches:
            vid_id = str(vid.get("id", ""))
            vid_url = f"https://www.youtube.com/watch?v={vid_id}"
            title = str(vid.get("title", ""))
            duration = float(str(vid.get("duration") or 0))
            race_id = int(str(race["id"]))

            # Use race start_utc as sync point
            sync_utc = datetime.fromisoformat(str(race["start_utc"]))
            if sync_utc.tzinfo is None:
                sync_utc = sync_utc.replace(tzinfo=UTC)

            await storage.add_race_video(
                race_id=race_id,
                youtube_url=vid_url,
                video_id=vid_id,
                title=title,
                label="youtube",
                sync_utc=sync_utc,
                sync_offset_s=0.0,
                duration_s=duration,
            )
            linked += 1

        print(f"\n{linked} video(s) linked to races.")
    finally:
        await storage.close()


def _fetch_all_channel_videos(channel_id: str) -> list[dict[str, object]]:
    """Fetch all videos from a YouTube channel using yt-dlp (runs in thread)."""
    import json
    import subprocess

    result = subprocess.run(
        [
            "yt-dlp",
            "--flat-playlist",
            "--dump-json",
            f"https://www.youtube.com/channel/{channel_id}/videos",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    videos: list[dict[str, object]] = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            videos.append(json.loads(line))
    return videos


def _best_race_match(
    video_title: str,
    candidates: list[dict[str, object]],
    vid_utc: datetime | None,
) -> dict[str, object] | None:
    """Pick the best matching race from candidates using keyword overlap + time proximity."""
    import re

    # Normalize title for keyword matching
    title_lower = video_title.lower()

    # Extract race/round indicators from title: r1, r2, d1, d2, pt1, pt2
    round_re = re.compile(r"\b[rd](\d+)\b", re.IGNORECASE)
    title_rounds = set(round_re.findall(title_lower))

    # Common keyword stems to check
    title_words = set(re.findall(r"[a-z]{3,}", title_lower))

    best: dict[str, object] | None = None
    best_score = -1.0

    for race in candidates:
        event_lower = str(race["event"]).lower()
        event_words = set(re.findall(r"[a-z]{3,}", event_lower))
        event_rounds = set(round_re.findall(event_lower))

        # Keyword overlap score
        common = title_words & event_words
        score = len(common) * 2.0

        # Bonus for matching round numbers
        if title_rounds and event_rounds and title_rounds & event_rounds:
            score += 5.0

        # Time proximity bonus (if we have a parsed video UTC time)
        if vid_utc:
            race_start = datetime.fromisoformat(str(race["start_utc"]))
            if race_start.tzinfo is None:
                race_start = race_start.replace(tzinfo=UTC)
            diff_s = abs((vid_utc - race_start).total_seconds())
            # Strong bonus if within 30 min, moderate if within 2 hours
            if diff_s < 1800:
                score += 10.0
            elif diff_s < 7200:
                score += 5.0
            elif diff_s < 14400:
                score += 2.0

        if score > best_score:
            best_score = score
            best = race

    # Require at least some signal to match
    if best_score < 2.0:
        return None

    return best


# ---------------------------------------------------------------------------
# sync-videos
# ---------------------------------------------------------------------------


async def _sync_videos(channel_id: str | None, tolerance: int, auto_confirm: bool) -> None:
    """Match recent YouTube uploads to unlinked camera sessions."""
    from helmlog.storage import Storage, StorageConfig

    yt_channel = channel_id or os.environ.get("YOUTUBE_CHANNEL_ID", "")
    if not yt_channel:
        print("No YouTube channel ID. Use --channel-id or set YOUTUBE_CHANNEL_ID.")
        return

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        unlinked = await storage.list_unlinked_camera_sessions()
        if not unlinked:
            print("No unlinked camera sessions found.")
            return

        # Fetch recent videos from channel via yt-dlp
        import asyncio as _asyncio
        import json as _json

        def _fetch_channel_videos() -> list[dict[str, object]]:
            import subprocess

            result = subprocess.run(
                [
                    "yt-dlp",
                    "--flat-playlist",
                    "--dump-json",
                    f"https://www.youtube.com/channel/{yt_channel}/videos",
                    "--playlist-end",
                    "20",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            videos = []
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    videos.append(_json.loads(line))
            return videos

        print(f"Fetching recent videos from channel {yt_channel}...")
        yt_videos = await _asyncio.to_thread(_fetch_channel_videos)
        if not yt_videos:
            print("No videos found on channel.")
            return

        # Match videos to unlinked camera sessions
        matches: list[tuple[dict[str, object], dict[str, object]]] = []
        for vid in yt_videos:
            duration = float(str(vid.get("duration") or 0))
            # Use upload_date as rough proxy for recording time
            upload_date_str = str(vid.get("upload_date", ""))
            if not upload_date_str or len(upload_date_str) != 8:
                continue

            for session in unlinked:
                started = session.get("recording_started_utc")
                if not started:
                    continue
                # Simple heuristic: check if the session's start date matches
                # the video's upload date (within tolerance days)
                from datetime import datetime as _dt

                session_dt = _dt.fromisoformat(str(started))
                # Parse upload_date YYYYMMDD
                upload_dt = _dt.strptime(upload_date_str, "%Y%m%d")
                diff_days = abs((upload_dt.date() - session_dt.date()).days)
                if diff_days <= 1:  # same day or next day (upload lag)
                    matches.append((vid, session))

        if not matches:
            print("No matches found within tolerance.")
            return

        print(f"\n{'YouTube Video':<45} {'Camera Session':<25} {'Race'}")
        print("-" * 90)
        for vid, sess in matches:
            title = str(vid.get("title", ""))[:45]
            cam = str(sess.get("camera_name", ""))
            race = str(sess.get("race_name", ""))
            print(f"{title:<45} {cam:<25} {race}")

        if not auto_confirm:
            print(f"\n{len(matches)} match(es) found. Use --yes to auto-link.")
            return

        linked = 0
        for vid, sess in matches:
            vid_url = f"https://www.youtube.com/watch?v={vid.get('id', '')}"
            vid_id = str(vid.get("id", ""))
            title = str(vid.get("title", ""))
            duration = float(str(vid.get("duration") or 0))
            session_id = int(str(sess["session_id"]))
            started = str(sess["recording_started_utc"])
            sync_utc = datetime.fromisoformat(started).replace(tzinfo=UTC)

            await storage.add_race_video(
                race_id=session_id,
                youtube_url=vid_url,
                video_id=vid_id,
                title=title,
                label=str(sess.get("camera_name", "")),
                sync_utc=sync_utc,
                sync_offset_s=0.0,
                duration_s=duration,
            )
            linked += 1

        print(f"\n{linked} video(s) linked to races.")
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# list-audio
# ---------------------------------------------------------------------------


async def _list_audio() -> None:
    """Print all recorded audio sessions."""
    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        sessions = await storage.list_audio_sessions()
    finally:
        await storage.close()

    if not sessions:
        print("No audio sessions recorded.")
        return

    print(f"{'File':<45} {'Duration':>9}  {'Start UTC'}")
    print("-" * 80)
    for s in sessions:
        if s.end_utc is not None:
            dur_s = int((s.end_utc - s.start_utc).total_seconds())
            h, rem = divmod(dur_s, 3600)
            m, sec = divmod(rem, 60)
            dur = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        else:
            dur = "in progress"
        short_path = s.file_path[-45:] if len(s.file_path) > 45 else s.file_path
        print(f"{short_path:<45} {dur:>9}  {s.start_utc.isoformat()}")


# ---------------------------------------------------------------------------
# add-user
# ---------------------------------------------------------------------------


async def _add_user(email: str, name: str | None, role: str) -> None:
    """Create a user directly in the DB (admin bootstrap; no email required)."""
    from helmlog.auth import generate_token, invite_expires_at
    from helmlog.storage import Storage, StorageConfig

    valid_roles = {"admin", "crew", "viewer"}
    if role not in valid_roles:
        logger.error("Invalid role {!r} — must be one of {}", role, sorted(valid_roles))
        sys.exit(1)

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        existing = await storage.get_user_by_email(email)
        if existing:
            logger.info(
                "User {} already exists (id={} role={})",
                email,
                existing["id"],
                existing["role"],
            )
            user_id = existing["id"]
        else:
            user_id = await storage.create_user(email, name, role)
            logger.info("Created user id={} email={} name={!r} role={}", user_id, email, name, role)

        # Generate an invite token so the user can log in
        token = generate_token()
        await storage.create_invite_token(token, email, role, user_id, invite_expires_at())
        base = os.environ.get(
            "PUBLIC_URL", f"http://localhost:{os.environ.get('WEB_PORT', '3002')}"
        ).rstrip(".")
        login_url = f"{base}/login?token={token}"
        logger.info("Login link (expires in 7 days):\n  {}", login_url)

        from helmlog.email import send_welcome_email, smtp_configured

        if smtp_configured() and email:
            sent = await send_welcome_email(name, email, role, login_url)
            if sent:
                logger.info("Welcome email sent to {}", email)
            else:
                logger.warning("Welcome email to {} failed — link printed above", email)
        else:
            logger.debug("SMTP not configured — skipping welcome email")
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# build-polar
# ---------------------------------------------------------------------------


async def _build_polar(min_sessions: int) -> None:
    """Rebuild the polar performance baseline from historical session data."""
    import helmlog.polar as polar
    from helmlog.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        count = await polar.build_polar_baseline(storage, min_sessions=min_sessions)
        print(f"Polar baseline built: {count} bins")
    finally:
        await storage.close()


async def _scan_transcript(session_id: int | None, scan_all: bool) -> None:
    """Scan transcripts for trigger keywords and create auto-notes."""
    import json as _json

    from helmlog.storage import Storage, StorageConfig
    from helmlog.triggers import scan_transcript

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        if session_id is not None:
            ids = [session_id]
        else:
            # Get all audio sessions that have transcripts
            db = storage._conn()
            cur = await db.execute(
                "SELECT a.id FROM audio_sessions a"
                " JOIN transcripts t ON a.id = t.audio_session_id"
                " WHERE t.status = 'done'"
            )
            ids = [row["id"] for row in await cur.fetchall()]

        total = 0
        for aid in ids:
            t = await storage.get_transcript(aid)
            if t is None or t.get("status") != "done":
                logger.info("Skipping audio session {} (no completed transcript)", aid)
                continue
            if t.get("segments_json"):
                segments = _json.loads(t["segments_json"])
            elif t.get("text"):
                # Plain whisper (no diarisation) — synthesize a single segment
                segments = [{"start": 0.0, "end": 0.0, "text": t["text"]}]
            else:
                logger.info("Skipping audio session {} (no text or segments)", aid)
                continue
            row = await storage.get_audio_session_row(aid)
            if row is None:
                continue
            count = await scan_transcript(storage, aid, row["start_utc"], segments)
            total += count
            logger.info("Audio session {}: {} auto-notes created", aid, count)

        print(f"Scan complete: {total} auto-notes created across {len(ids)} session(s)")
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# list-devices
# ---------------------------------------------------------------------------


async def _list_devices() -> None:
    """Print available audio input devices."""
    from helmlog.audio import AudioRecorder

    devices = AudioRecorder.list_devices()
    if not devices:
        print("No audio input devices found.")
        return

    print(f"{'Idx':>3}  {'Name':<40}  {'Ch':>3}  {'Default rate':>12}")
    print("-" * 65)
    for dev in devices:
        print(
            f"{dev['index']:>3}  {str(dev['name']):<40}  "
            f"{dev['max_input_channels']:>3}  {dev['default_samplerate']:>12.0f}"
        )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helmlog",
        description="HelmLog — open-source sailing data platform",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Start the CAN logging loop")

    exp = sub.add_parser("export", help="Export a time range to CSV")
    exp.add_argument("--start", required=True, metavar="ISO", help="Start time (UTC ISO 8601)")
    exp.add_argument("--end", required=True, metavar="ISO", help="End time (UTC ISO 8601)")
    exp.add_argument(
        "--out",
        default="data/export.csv",
        metavar="FILE",
        help="Output file path; format inferred from extension (.csv, .gpx, .json)",
    )

    sub.add_parser("status", help="Show DB row counts and last-seen timestamps")

    lv = sub.add_parser(
        "link-video",
        help="Link a YouTube video to logged data via a time sync point",
        description=(
            "Associates a YouTube video with your instrument log by providing a sync point.\n\n"
            "Option A — you know when you pressed Record:\n"
            "  helmlog link-video --url URL --start 2025-08-10T13:45:00\n\n"
            "Option B — you know a specific moment in both the video and the log\n"
            "  (e.g. starting gun at video t=5:30, UTC 14:05:30):\n"
            "  helmlog link-video --url URL --sync-utc 2025-08-10T14:05:30 --sync-offset 330"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    lv.add_argument("--url", required=True, metavar="URL", help="YouTube video URL")
    lv_sync = lv.add_mutually_exclusive_group(required=True)
    lv_sync.add_argument(
        "--start",
        metavar="ISO",
        help="UTC time when you pressed Record (sets sync offset to 0)",
    )
    lv_sync.add_argument(
        "--sync-utc",
        metavar="ISO",
        help="UTC time of a known sync point (pair with --sync-offset)",
    )
    lv.add_argument(
        "--sync-offset",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Seconds into the video at --sync-utc (default: 0)",
    )

    sub.add_parser("list-videos", help="List linked YouTube videos")

    sub.add_parser("list-cameras", help="Show configured cameras and their status")

    lcv = sub.add_parser(
        "link-channel-videos",
        help="Match YouTube channel videos to Gaia GPS-imported races by date",
    )
    lcv.add_argument(
        "--channel-id",
        metavar="ID",
        help="YouTube channel ID (or set YOUTUBE_CHANNEL_ID)",
    )
    lcv.add_argument("--yes", action="store_true", help="Write matches to DB (otherwise dry run)")
    lcv.add_argument(
        "--dry-run", action="store_true", help="Show matches without writing (default behaviour)"
    )

    sv = sub.add_parser("sync-videos", help="Auto-associate YouTube uploads with camera sessions")
    sv.add_argument(
        "--channel-id", metavar="ID", help="YouTube channel ID (or set YOUTUBE_CHANNEL_ID)"
    )
    sv.add_argument(
        "--tolerance",
        type=int,
        default=30,
        metavar="SEC",
        help="Match tolerance in seconds (default: 30)",
    )
    sv.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    sub.add_parser("list-audio", help="List recorded audio sessions")
    sub.add_parser("list-devices", help="List available audio input devices")

    au = sub.add_parser("add-user", help="Create a user directly in the DB (admin bootstrap)")
    au.add_argument("--email", required=True, metavar="EMAIL", help="User email address")
    au.add_argument("--name", default=None, metavar="NAME", help="Display name")
    au.add_argument(
        "--role",
        default="viewer",
        choices=["admin", "crew", "viewer"],
        help="Role (default: viewer)",
    )

    bp = sub.add_parser("build-polar", help="Rebuild polar baseline from historical session data")
    bp.add_argument("--min-sessions", type=int, default=3, metavar="N")

    st = sub.add_parser(
        "scan-transcript",
        help="Scan transcripts for trigger keywords and create auto-notes",
    )
    st_target = st.add_mutually_exclusive_group(required=True)
    st_target.add_argument("--session", type=int, metavar="ID", help="Audio session ID to scan")
    st_target.add_argument("--all", action="store_true", help="Scan all sessions with transcripts")

    return parser


def main() -> None:
    """CLI entry point."""
    _load_env()
    _setup_logging()

    args = _build_parser().parse_args()

    logger.info("HelmLog — command={}", args.command)

    try:
        match args.command:
            case "run":
                asyncio.run(_run())
            case "export":
                asyncio.run(_export(args.start, args.end, args.out))
            case "status":
                asyncio.run(_status())
            case "link-video":
                sync_utc_iso = args.start if args.start else args.sync_utc
                sync_offset = 0.0 if args.start else args.sync_offset
                asyncio.run(_link_video(args.url, sync_utc_iso, sync_offset))
            case "list-videos":
                asyncio.run(_list_videos())
            case "list-cameras":
                asyncio.run(_list_cameras())
            case "link-channel-videos":
                ch = args.channel_id or os.environ.get("YOUTUBE_CHANNEL_ID", "")
                if not ch:
                    print("No channel ID. Use --channel-id or set YOUTUBE_CHANNEL_ID.")
                    sys.exit(1)
                asyncio.run(_link_channel_videos(ch, auto_confirm=args.yes, dry_run=not args.yes))
            case "sync-videos":
                asyncio.run(_sync_videos(args.channel_id, args.tolerance, args.yes))
            case "list-audio":
                asyncio.run(_list_audio())
            case "list-devices":
                asyncio.run(_list_devices())
            case "add-user":
                asyncio.run(_add_user(args.email, args.name, args.role))
            case "build-polar":
                asyncio.run(_build_polar(args.min_sessions))
            case "scan-transcript":
                asyncio.run(_scan_transcript(args.session, getattr(args, "all", False)))
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")
    except Exception as exc:
        logger.exception("Fatal error: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
