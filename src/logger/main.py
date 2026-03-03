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
    from logger.external import ExternalFetcher
    from logger.storage import Storage

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

    from logger.external import ExternalFetcher
    from logger.storage import Storage

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
    race start/end events via the web interface.
    """
    import uvicorn

    from logger.audio import AudioConfig, AudioRecorder
    from logger.races import RaceConfig
    from logger.storage import Storage
    from logger.web import create_app

    assert isinstance(storage, Storage)
    _recorder = recorder if isinstance(recorder, AudioRecorder) else None
    _audio_config = audio_config if isinstance(audio_config, AudioConfig) else None
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
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise


async def _run() -> None:
    """Main async loop: read instrument data, decode, persist.

    Data source is selected by the DATA_SOURCE environment variable:
      signalk (default) — consume the Signal K WebSocket feed (SK owns can0)
      can               — read raw CAN frames directly (legacy mode)
    """
    import os

    from logger.external import ExternalFetcher
    from logger.storage import Storage, StorageConfig

    # Cancel this task on SIGTERM so finally blocks run and storage is flushed.
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    assert current is not None
    loop.add_signal_handler(signal.SIGTERM, current.cancel)

    data_source = os.environ.get("DATA_SOURCE", "signalk").lower()
    storage_config = StorageConfig()
    storage = Storage(storage_config)
    await storage.connect()

    from logger.audio import AudioConfig, AudioRecorder

    audio_config = AudioConfig()
    recorder = AudioRecorder()

    from logger.monitor import monitor_loop

    async with ExternalFetcher() as fetcher:
        weather_task = asyncio.create_task(_weather_loop(storage, fetcher))
        tide_task = asyncio.create_task(_tide_loop(storage, fetcher))
        web_task = asyncio.create_task(_web_loop(storage, recorder, audio_config))
        monitor_task = asyncio.create_task(monitor_loop())
        try:
            if data_source == "signalk":
                from logger.sk_reader import SKReader, SKReaderConfig

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
                from logger.can_reader import CANReader, CANReaderConfig, extract_pgn
                from logger.nmea2000 import decode

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
    from logger.export import export_to_file
    from logger.storage import Storage, StorageConfig

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
    from logger.storage import Storage, StorageConfig

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
    from logger.storage import Storage, StorageConfig
    from logger.video import VideoLinker

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
    from logger.storage import Storage, StorageConfig

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
# list-audio
# ---------------------------------------------------------------------------


async def _list_audio() -> None:
    """Print all recorded audio sessions."""
    from logger.storage import Storage, StorageConfig

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
    from logger.auth import generate_token, invite_expires_at
    from logger.storage import Storage, StorageConfig

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
        base = os.environ.get("PUBLIC_URL", f"http://localhost:{os.environ.get('WEB_PORT', '3002')}").rstrip(".")
        login_url = f"{base}/login?token={token}"
        logger.info("Login link (expires in 7 days):\n  {}", login_url)

        from logger.email import send_welcome_email, smtp_configured

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
    import logger.polar as polar
    from logger.storage import Storage, StorageConfig

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

    from logger.storage import Storage, StorageConfig
    from logger.triggers import scan_transcript

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
    from logger.audio import AudioRecorder

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
        prog="j105-logger",
        description="J105 NMEA 2000 Data Logger",
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
            "  j105-logger link-video --url URL --start 2025-08-10T13:45:00\n\n"
            "Option B — you know a specific moment in both the video and the log\n"
            "  (e.g. starting gun at video t=5:30, UTC 14:05:30):\n"
            "  j105-logger link-video --url URL --sync-utc 2025-08-10T14:05:30 --sync-offset 330"
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

    logger.info("J105 Logger — command={}", args.command)

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
