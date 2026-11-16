# HelmLog — A Linear Code Walkthrough

*2026-03-15T01:49:24Z by Showboat 0.6.1*
<!-- showboat-id: c6883409-cc2a-4141-85c7-fa2129f4f0ec -->

## What is HelmLog?

HelmLog is a Raspberry Pi-based sailing data logger. It reads live instrument data from a B&G system via Signal K Server, stores everything in SQLite, and serves a web UI for race control, debrief, and performance analysis. Data can be exported as CSV, GPX, or JSON for tools like Sailmon.

This walkthrough follows the data from the moment it arrives on the wire to the moment it leaves as an export file. We'll trace every major module in the order the data flows through it.

## 1. Startup — `main.py`

Everything begins in `main.py`. It's the CLI entry point and the wiring layer — it imports hardware modules, initialises storage, and spawns the async event loop. By design, `main.py` contains no business logic. It just connects the pieces.

The CLI is built with a simple subcommand dispatch. The default command is `run`, which starts the logging loop:

```bash
grep -n 'def main\|subcommand\|argparse\|parser\.\|def _run' src/helmlog/main.py | head -25
```

```output
13:import argparse
220:async def _run() -> None:
1321:def _build_parser() -> argparse.ArgumentParser:
1322:    parser = argparse.ArgumentParser(
1326:    sub = parser.add_subparsers(dest="command", required=True)
1353:        formatter_class=argparse.RawDescriptionHelpFormatter,
1439:    id_sub = id_parser.add_subparsers(dest="identity_command", required=True)
1456:    coop_sub = coop_parser.add_subparsers(dest="coop_command", required=True)
1486:def main() -> None:
```

The `main()` entry point loads environment, sets up logging, and dispatches to the appropriate subcommand. For the `run` command, it calls `_run()` which is the async heart of the system. Let's see what `_run()` sets up:

```bash
sed -n '220,327p' src/helmlog/main.py
```

```output
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

    from helmlog.deploy import DeployConfig
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
        deploy_config = DeployConfig()
        if deploy_config.mode == "evergreen":
            deploy_task = asyncio.create_task(_deploy_loop(storage, deploy_config))
        else:
            deploy_task = asyncio.create_task(asyncio.sleep(1e9))
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
            deploy_task.cancel()
            await asyncio.gather(
                weather_task,
                tide_task,
                web_task,
                monitor_task,
                deploy_task,
                return_exceptions=True,
            )
            await storage.close()
            logger.info("Logger stopped")
```

This is the entire system in one function. The pattern is:

1. **Connect storage** — SQLite via aiosqlite, schema migrations run on connect
2. **Seed settings** — DB-persisted admin overrides get pushed into `os.environ`
3. **Spawn background tasks** — weather, tides, web server, system monitor, auto-deploy
4. **Enter the core loop** — read instrument data, decode, persist

The core loop is an `async for` over either `SKReader` (Signal K WebSocket, the default) or `CANReader` (legacy direct CAN bus). Each iteration yields one decoded record. Every record updates the live display cache via `update_live()`, and if a session is active, it's also written to disk.

On shutdown (`SIGTERM`), the `finally` block cancels all background tasks and flushes storage. This is critical on a Pi — power can cut at any time.

## 2. Data Ingestion — `sk_reader.py`

Signal K Server runs on the same Pi and owns the CAN bus. HelmLog connects to its WebSocket to receive decoded instrument deltas. This is the primary data path.

```bash
sed -n '129,146p' src/helmlog/sk_reader.py
```

```output
_SIMPLE: dict[str, Callable[[float, datetime], PGNRecord]] = {
    "navigation.headingTrue": _mk_heading,
    "navigation.speedThroughWater": _mk_speed,
    "environment.depth.belowKeel": _mk_depth,
    "environment.water.temperature": _mk_env,
}

_PAIR: dict[str, Callable[[dict[str, float], datetime], PGNRecord | None]] = {
    "navigation.courseOverGroundTrue": _try_cogsog,
    "navigation.speedOverGround": _try_cogsog,
    "environment.wind.speedTrue": _try_true_wind,
    "environment.wind.angleTrue": _try_true_wind,
    "environment.wind.angleTrueWater": _try_true_wind,
    "environment.wind.angleTrueGround": _try_true_wind,
    "environment.wind.directionTrue": _try_true_wind,
    "environment.wind.speedApparent": _try_app_wind,
    "environment.wind.angleApparent": _try_app_wind,
}
```

Two dispatch tables map Signal K paths to record constructors. **Simple** paths (heading, speed, depth, water temp) produce a record from a single value. **Paired** paths (COG+SOG, wind speed+angle) buffer values until both halves arrive, then emit one record.

The key insight is that `sk_reader.py` emits the exact same `PGNRecord` dataclasses as the legacy CAN path — `HeadingRecord`, `SpeedRecord`, `WindRecord`, etc. This means everything downstream (storage, export, web) doesn't care which data source is active. The adapter pattern in action.

```bash
sed -n '154,227p' src/helmlog/sk_reader.py
```

```output
def process_delta(raw: str, buf: dict[str, float]) -> list[PGNRecord]:
    """Parse a Signal K delta message; return any records it produces.

    Updates *buf* in-place for multi-field records (COG+SOG, wind speed+angle).
    Unknown paths are silently ignored at DEBUG level.
    Malformed numeric values are logged at WARNING and skipped.
    """
    try:
        delta: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("SK: malformed JSON: {}", exc)
        return []

    records: list[PGNRecord] = []

    # Reject other-vessel data — only process self-vessel deltas (#208)
    context: str = delta.get("context", "vessels.self")
    if context and context != "vessels.self" and not context.endswith(".self"):
        logger.warning("SK: rejecting non-self delta (context={!r})", context)
        return []

    for update in delta.get("updates", []):
        ts_str: str = update.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = datetime.now(UTC)

        for entry in update.get("values", []):
            path: str = entry.get("path", "")
            value: Any = entry.get("value")
            if value is None:
                continue

            # Block AIS-related paths (#208)
            if "ais" in path.lower() or path.startswith("vessels.urn:"):
                logger.warning("SK: rejecting AIS/other-vessel path {!r}", path)
                continue

            if path == "navigation.position":
                try:
                    records.append(
                        PositionRecord(
                            PGN_POSITION_RAPID,
                            SK_SOURCE_ADDR,
                            ts,
                            float(value["latitude"]),
                            float(value["longitude"]),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("SK: bad position value {!r}: {}", value, exc)
                continue

            if simple_fn := _SIMPLE.get(path):
                try:
                    records.append(simple_fn(float(value), ts))
                except (TypeError, ValueError) as exc:
                    logger.warning("SK: non-numeric value for {!r}: {}", path, exc)
                continue

            if pair_fn := _PAIR.get(path):
                try:
                    buf[path] = float(value)
                    rec = pair_fn(buf, ts)
                    if rec is not None:
                        records.append(rec)
                except (TypeError, ValueError) as exc:
                    logger.warning("SK: non-numeric value for {!r}: {}", path, exc)
                continue

            logger.debug("SK: unknown path {!r} — ignoring", path)

    return records
```

`process_delta()` is a pure function — no I/O, no side effects beyond the mutable buffer. This makes it trivially testable. It guards against two hazards:

- **AIS data** (issue #208): Other-vessel positions from AIS transponders are blocked at ingest until session-scoped capture (#394) is built. Both the context check (`vessels.self` only) and the path check (`ais` substring) enforce this.
- **Malformed values**: Every numeric conversion is wrapped in try/except. Bad data from the instrument bus gets logged and skipped, never crashes the loop.

## 3. NMEA 2000 Decoding — `nmea2000.py`

This module defines the record dataclasses that flow through the entire system, plus a raw-bytes decoder for the legacy CAN path. Even when using Signal K, these dataclasses are the lingua franca.

```bash
grep -n 'class.*Record\|^PGN_' src/helmlog/nmea2000.py | head -20
```

```output
29:PGN_VESSEL_HEADING: Final[int] = 127250
30:PGN_SPEED_THROUGH_WATER: Final[int] = 128259
31:PGN_WATER_DEPTH: Final[int] = 128267
32:PGN_POSITION_RAPID: Final[int] = 129025
33:PGN_COG_SOG_RAPID: Final[int] = 129026
34:PGN_WIND_DATA: Final[int] = 130306
35:PGN_ENVIRONMENTAL: Final[int] = 130310
83:class HeadingRecord:
95:class SpeedRecord:
105:class DepthRecord:
116:class PositionRecord:
127:class COGSOGRecord:
138:class WindRecord:
150:class EnvironmentalRecord:
```

```bash
sed -n '70,161p' src/helmlog/nmea2000.py
```

```output
        129807,  # AIS Class B Group Assignment
        129808,  # DSC Call Information
        129809,  # AIS Class B CS Static Data Report, Part A
        129810,  # AIS Class B CS Static Data Report, Part B
    }
)

# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadingRecord:
    """PGN 127250 — Vessel Heading."""

    pgn: int
    source_addr: int
    timestamp: datetime
    heading_deg: float  # degrees true (converted from radians)
    deviation_deg: float | None  # magnetic deviation, degrees
    variation_deg: float | None  # magnetic variation, degrees


@dataclass(frozen=True)
class SpeedRecord:
    """PGN 128259 — Speed Through Water."""

    pgn: int
    source_addr: int
    timestamp: datetime
    speed_kts: float  # knots (converted from m/s)


@dataclass(frozen=True)
class DepthRecord:
    """PGN 128267 — Water Depth."""

    pgn: int
    source_addr: int
    timestamp: datetime
    depth_m: float  # metres below transducer
    offset_m: float | None  # transducer offset (positive = above keel)


@dataclass(frozen=True)
class PositionRecord:
    """PGN 129025 — Position Rapid Update."""

    pgn: int
    source_addr: int
    timestamp: datetime
    latitude_deg: float  # degrees, positive North
    longitude_deg: float  # degrees, positive East


@dataclass(frozen=True)
class COGSOGRecord:
    """PGN 129026 — COG & SOG Rapid Update."""

    pgn: int
    source_addr: int
    timestamp: datetime
    cog_deg: float  # course over ground, degrees true
    sog_kts: float  # speed over ground, knots


@dataclass(frozen=True)
class WindRecord:
    """PGN 130306 — Wind Data."""

    pgn: int
    source_addr: int
    timestamp: datetime
    wind_speed_kts: float  # knots
    wind_angle_deg: float  # degrees (apparent or true per reference field)
    reference: int  # 0=true, 1=magnetic, 2=apparent, 3=boat (see spec)


@dataclass(frozen=True)
class EnvironmentalRecord:
    """PGN 130310 — Environmental Parameters."""

    pgn: int
    source_addr: int
    timestamp: datetime
    water_temp_c: float  # Celsius (converted from Kelvin)


# Union type for all PGN record types
PGNRecord = (
    HeadingRecord
```

Every record is a frozen dataclass — immutable once created. They all share the same three-field header: `pgn` (the NMEA 2000 parameter group number), `source_addr` (which device on the bus sent it), and `timestamp` (UTC). The union type `PGNRecord` is what flows through the system.

Note the units are already converted to human-friendly values: degrees (not radians), knots (not m/s), Celsius (not Kelvin). The 'decode early, store clean' principle — raw instrument units never escape the ingestion layer.

## 4. The Legacy CAN Path — `can_reader.py`

When `DATA_SOURCE=can`, HelmLog reads raw CAN frames directly from the NMEA 2000 bus instead of going through Signal K. This is the original path, kept for fallback.

```bash
sed -n '61,86p' src/helmlog/can_reader.py
```

```output
def extract_pgn(arbitration_id: int) -> int:
    """Extract the NMEA 2000 PGN from a 29-bit J1939 CAN arbitration ID.

    NMEA 2000 uses the J1939 29-bit extended CAN ID format:
        bits 28-26: priority (3 bits)
        bit  25:    reserved
        bit  24:    data page
        bits 23-16: PDU format (PF)
        bits 15-8:  PDU specific (PS)
        bits  7-0:  source address

    For PDU2 (PF >= 240, broadcast messages):
        PGN = (data_page << 16) | (PF << 8) | PS

    For PDU1 (PF < 240, addressed messages):
        PGN = (data_page << 16) | (PF << 8)
        (PS is the destination address, not part of the PGN)
    """
    data_page = (arbitration_id >> 24) & 0x1
    pdu_format = (arbitration_id >> 16) & 0xFF
    pdu_specific = (arbitration_id >> 8) & 0xFF

    if pdu_format >= 240:  # PDU2 — broadcast
        return (data_page << 16) | (pdu_format << 8) | pdu_specific
    else:  # PDU1 — peer-to-peer
        return (data_page << 16) | (pdu_format << 8)
```

This is bit-level protocol work. NMEA 2000 rides on J1939 CAN, which packs the PGN into a 29-bit arbitration ID. The PDU format byte determines whether the message is broadcast (`PF >= 240`, PGN includes the PS byte) or peer-to-peer (`PF < 240`, PS is a destination address and not part of the PGN).

Note the hardware isolation: `python-can` is lazy-imported so the test suite runs without a CAN interface. The blocking `bus.recv()` is wrapped in `asyncio.to_thread()` to keep the event loop responsive. This module is the only place in the entire codebase that touches the physical bus.

## 5. Storage — `storage.py`

SQLite is the single source of truth. All data is written to disk with UTC timestamps. Export and web functions read from SQLite, never from live data. The storage module handles schema migrations, buffered writes, and every query the system needs.

```bash
sed -n '1047,1070p' src/helmlog/storage.py
```

```output
class Storage:
    """Async SQLite storage for all logger data."""

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._db: aiosqlite.Connection | None = None
        self._pending: int = 0
        self._last_flush: float = 0.0
        self._session_active: bool = False
        self._live: dict[str, float | None] = dict.fromkeys(_LIVE_KEYS)
        self._live_tw_ref: int | None = None
        self._live_tw_angle_raw: float | None = None

    @property
    def session_active(self) -> bool:
        """True when a race or practice session is currently in progress."""
        return self._session_active

    # ------------------------------------------------------------------
    # In-memory live instrument cache (always updated, no DB I/O)
    # ------------------------------------------------------------------

    def _recompute_true_wind(self) -> None:
        ref = self._live_tw_ref
```

The `Storage` class maintains two data paths:

1. **Live cache** (`_live` dict) — updated on every record via `update_live()`, always available for the web UI's real-time display. No disk I/O.
2. **Persistent writes** — buffered in batches. Records accumulate until either 200 records queue up or 1 second elapses, then they're flushed to SQLite in a single transaction. This balances write throughput against crash-safety on the Pi.

The `session_active` flag controls whether records are persisted. The web UI toggles this when a user starts/stops a race. Outside a session, instruments still update the live cache but nothing goes to disk.

```bash
sed -n '1383,1430p' src/helmlog/storage.py
```

```output
    async def write(self, record: PGNRecord) -> None:
        """Buffer a decoded PGN record; flushes to disk periodically."""
        match record:
            case HeadingRecord():
                await self._write_heading(record)
            case SpeedRecord():
                await self._write_speed(record)
            case DepthRecord():
                await self._write_depth(record)
            case PositionRecord():
                await self._write_position(record)
            case COGSOGRecord():
                await self._write_cogsog(record)
            case WindRecord():
                await self._write_wind(record)
            case EnvironmentalRecord():
                await self._write_environmental(record)
        self._pending += 1
        await self._auto_flush()

    async def _auto_flush(self) -> None:
        """Commit if the batch size or time interval threshold is reached."""
        now = time.monotonic()
        if self._pending >= _FLUSH_BATCH_SIZE or now - self._last_flush >= _FLUSH_INTERVAL_S:
            await self._flush()

    async def _flush(self) -> None:
        """Commit all pending writes to disk."""
        if self._pending == 0:
            return
        db = self._conn()
        await db.commit()
        logger.debug("Flushed {} records to SQLite", self._pending)
        self._pending = 0
        self._last_flush = time.monotonic()

    async def _write_heading(self, r: HeadingRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO headings (ts, source_addr, heading_deg, deviation_deg, variation_deg)"
            " VALUES (?, ?, ?, ?, ?)",
            (_ts(r.timestamp), r.source_addr, r.heading_deg, r.deviation_deg, r.variation_deg),
        )

    async def _write_speed(self, r: SpeedRecord) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO speeds (ts, source_addr, speed_kts) VALUES (?, ?, ?)",
```

The `write()` method uses Python 3.10+ `match` to dispatch each record type to its table-specific writer. After each record, `_auto_flush()` checks whether to commit — either 200 records have accumulated (`_FLUSH_BATCH_SIZE`) or 1 second has passed (`_FLUSH_INTERVAL_S`). This is the 'flush frequently to survive crashes' principle.

Let's look at the schema. The table creation happens in `connect()`, which runs migrations up to the current version:

```bash
grep -c 'CREATE TABLE' src/helmlog/storage.py
```

```output
71
```

```bash
grep 'CREATE TABLE' src/helmlog/storage.py | sed 's/.*CREATE TABLE IF NOT EXISTS //' | sed 's/ (.*//' | sort
```

```output
                is_create = upper.startswith("CREATE TABLE IF NOT EXISTS") or upper.startswith(
        #  - CREATE TABLE/INDEX IF NOT EXISTS
analysis_cache
analysis_preferences
app_settings
audio_sessions
audit_log
auth_sessions
boat_identity
boat_settings
boats
camera_sessions
cameras
co_op_audit
co_op_memberships
co_op_peers
cogsog
comment_read_state
comment_threads
comments
crew_consents
crew_consents_new
crew_defaults
crew_positions
daily_events
deployment_log
depths
environmental
event_rules
headings
invitations
invite_tokens
invite_tokens_new
maneuvers
note_tags
notification_preferences
notifications
password_reset_tokens
polar_baseline
positions
race_crew
race_results
race_results_new
race_sails
race_videos
race_videos_new
races
recent_sailors
request_nonces
sail_changes
sail_changes
sail_defaults
sail_defaults
sails
schema_version
schema_version
session_notes
session_notes_new
session_sharing
session_tags
speeds
synth_course_marks
synth_wind_params
tags
tides
transcripts
user_credentials
users
video_sessions
weather
winds
```

71 `CREATE TABLE` statements — though many are migration renames (`_new` suffixes that get renamed). The real schema has about 50 tables covering:

- **Instrument data**: headings, speeds, depths, positions, cogsog, winds, environmental
- **Session management**: races, session_notes, session_tags, maneuvers, sail_changes
- **Audio/video**: audio_sessions, transcripts, video_sessions, race_videos
- **Auth/users**: users, auth_sessions, invite_tokens, user_credentials, audit_log
- **External data**: weather, tides
- **Federation**: boat_identity, co_op_memberships, co_op_peers, session_sharing, co_op_audit
- **Analysis**: polar_baseline, analysis_cache, analysis_preferences
- **Configuration**: app_settings, cameras, boats, sails, crew_positions

Schema versioning is simple integer-based — each version adds tables or columns, and the migration runs on connect.

## 6. Web Interface — `web.py`

FastAPI serves the web UI and API endpoints. The app is created via a factory pattern — `create_app(storage, recorder, audio_config)` — so it's testable with an in-memory database and no running server.

```bash
grep '@app\.\(get\|post\|put\|patch\|delete\)' src/helmlog/web.py | sed 's/.*@app\.//' | head -50
```

```output
get("/healthz", include_in_schema=False)
get("/api/me")
patch("/api/me/weight", status_code=204)
patch("/api/me/name", status_code=204)
get("/login", response_class=HTMLResponse, include_in_schema=False)
post("/auth/login", include_in_schema=False)
get("/auth/accept-invite", response_class=HTMLResponse, include_in_schema=False)
post("/auth/register", include_in_schema=False)
get("/auth/forgot-password", response_class=HTMLResponse, include_in_schema=False)
post("/auth/forgot-password", include_in_schema=False)
get("/auth/reset-password", response_class=HTMLResponse, include_in_schema=False)
post("/auth/reset-password", include_in_schema=False)
get("/auth/oauth/{provider}", include_in_schema=False)
get("/auth/oauth/{provider}/callback", include_in_schema=False)
post("/logout", include_in_schema=False)
get("/", response_class=HTMLResponse, include_in_schema=False)
get("/history", response_class=HTMLResponse, include_in_schema=False)
get("/session/{session_id}", response_class=HTMLResponse, include_in_schema=False)
get("/sails", response_class=HTMLResponse, include_in_schema=False)
get("/admin/boats", response_class=HTMLResponse, include_in_schema=False)
get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
post("/admin/users/invite", status_code=201, include_in_schema=False)
post("/admin/invitations/{invitation_id}/revoke", status_code=204, include_in_schema=False)
put("/admin/users/{user_id}/role", status_code=204, include_in_schema=False)
put("/admin/users/{user_id}/developer", status_code=204, include_in_schema=False)
put("/admin/users/{user_id}", status_code=204, include_in_schema=False)
delete("/admin/sessions/{session_id}", status_code=204, include_in_schema=False)
get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
get("/api/audit")
get("/admin/cameras", response_class=HTMLResponse, include_in_schema=False)
get("/admin/events", response_class=HTMLResponse, include_in_schema=False)
get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
get("/api/settings")
put("/api/settings")
get("/api/cameras")
post("/api/cameras/{camera_name}/start")
post("/api/cameras/{camera_name}/stop")
get("/api/cameras/sessions")
get("/api/sessions/{session_id}/cameras")
post("/api/cameras", status_code=201)
put("/api/cameras/{camera_name}")
delete("/api/cameras/{camera_name}", status_code=204)
get("/admin/network", response_class=HTMLResponse, include_in_schema=False)
get("/api/network/status")
get("/api/network/profiles")
post("/api/network/profiles", status_code=201)
put("/api/network/profiles/{profile_id}")
delete("/api/network/profiles/{profile_id}", status_code=204)
post("/api/network/connect")
post("/api/network/disconnect")
```

196 route handlers in `web.py`. The routes break into clear groups:

- **Auth** (`/login`, `/auth/*`) — magic-link invites, password auth, OAuth providers
- **Pages** (`/`, `/history`, `/session/{id}`, `/sails`) — Jinja2 HTML templates
- **Admin** (`/admin/*`) — user management, cameras, settings, audit, federation
- **API** (`/api/*`) — JSON endpoints for the JS frontend (sessions, instruments, notes, marks)
- **Peer API** (mounted from `peer_api.py` at `/co-op`) — inter-boat federation

The home page (`/`) is the race control page — start/stop sessions, see live instruments, manage crew and sails. `/history` shows past sessions. `/session/{id}` is the debrief page with track map, maneuvers, transcripts, and discussion threads.

## 7. Authentication — `auth.py`

Auth is optional (disabled by `AUTH_DISABLED=true` for Tailscale-only deployments) and role-based (viewer, crew, admin).

```bash
sed -n '30,56p' src/helmlog/auth.py
```

```output
# ---------------------------------------------------------------------------
# Role ordering
# ---------------------------------------------------------------------------

_ROLE_RANK: dict[str, int] = {"viewer": 0, "crew": 1, "admin": 2}

SESSION_TTL_DAYS = int(os.getenv("AUTH_SESSION_TTL_DAYS", "90"))


def _is_auth_disabled() -> bool:
    return os.getenv("AUTH_DISABLED", "false").lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Mock admin user returned when auth is disabled
# ---------------------------------------------------------------------------

_MOCK_ADMIN: dict[str, Any] = {
    "id": None,
    "email": "admin@local",
    "name": "Local Admin",
    "role": "admin",
    "is_developer": 1,
    "created_at": "1970-01-01T00:00:00+00:00",
    "last_seen": None,
    "is_active": 1,
}
```

Three roles: viewer (read-only), crew (can add notes, manage sails), admin (full control). When auth is disabled, every request gets the mock admin — useful for development and Tailscale-only deployments where the network itself is the security boundary.

Session cookies have a 90-day TTL by default. The `require_auth(min_role)` dependency is injected into route handlers to enforce role checks.

## 8. Export — `export.py`

The export module joins all instrument tables by timestamp and outputs CSV, GPX, or JSON. This is how data gets into regatta analysis tools like Sailmon.

```bash
sed -n '36,75p' src/helmlog/export.py
```

```output
_COLUMNS = [
    "timestamp",
    "HDG",  # heading (degrees true)
    "BSP",  # boatspeed through water (knots)
    "DEPTH",  # water depth (metres)
    "LAT",  # latitude (degrees)
    "LON",  # longitude (degrees)
    "COG",  # course over ground (degrees true)
    "SOG",  # speed over ground (knots)
    "TWS",  # true wind speed (knots)
    "TWA",  # true wind angle (degrees)
    "AWA",  # apparent wind angle (degrees)
    "AWS",  # apparent wind speed (knots)
    "WTEMP",  # water temperature (Celsius)
    "video_url",
    "WX_TWS",  # synoptic wind speed (knots) — Open-Meteo
    "WX_TWD",  # synoptic wind direction (°) — Open-Meteo
    "AIR_TEMP",  # air temperature (°C) — Open-Meteo
    "PRESSURE",  # surface pressure (hPa) — Open-Meteo
    "TIDE_HT",  # tide height above MLLW (metres) — NOAA CO-OPS
    "crew_helm",
    "crew_main",
    "crew_jib",
    "crew_spin",
    "crew_tactician",
    "BSP_BASELINE",
    "BSP_DELTA",
]

_WIND_REF_TRUE = 0
_WIND_REF_APPARENT = 2

# Sailing extension namespace used in GPX <extensions>
_GPX_NS = "http://www.topografix.com/GPX/1/1"
_SAIL_NS = "http://github.com/weaties/helmlog"

# ---------------------------------------------------------------------------
# Internal: shared data loading
# ---------------------------------------------------------------------------

```

27 columns per row, one row per second. The export merges data from 7+ instrument tables plus weather, tides, crew assignments, video sync points, and polar baselines. The algorithm:

1. Load all tables for the time range, building per-table lookup indexes keyed by second
2. For each second in the range, merge values from all tables (latest-value-wins for instruments)
3. Fill gaps with empty strings (CSV) or null (JSON)
4. Compute derivations: `BSP_DELTA = BSP - BSP_BASELINE` (how far off your polar target)
5. Write the chosen format (CSV, GPX 1.1 with sailing extensions, or structured JSON)

The export is the final shape of the data for downstream tools. Everything upstream — ingestion, storage, external data, polar baselines — feeds into this merge.

## 9. External Data — `external.py`

While the boat is logging, background tasks fetch weather from Open-Meteo and tides from NOAA CO-OPS. These are stored alongside instrument data and show up in exports.

```bash
sed -n '24,50p' src/helmlog/external.py
```

```output
class TideReading:
    """A single hourly tide height prediction from NOAA CO-OPS."""

    timestamp: datetime  # UTC time of the reading (truncated to hour)
    height_m: float  # metres above MLLW chart datum
    type: str  # "prediction" | "observation"
    station_id: str  # NOAA station ID, e.g. "8461490"
    station_name: str  # Human-readable station name


@dataclass(frozen=True)
class WeatherReading:
    """A single weather observation from Open-Meteo (hourly resolution)."""

    timestamp: datetime  # UTC time of the reading (truncated to hour)
    lat: float  # latitude used for the query
    lon: float  # longitude used for the query
    wind_speed_kts: float  # 10 m wind speed in knots
    wind_direction_deg: float  # 10 m wind direction (degrees true, 0 = N)
    air_temp_c: float  # 2 m air temperature (°C)
    pressure_hpa: float  # surface pressure (hPa)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

```

Both reading types are frozen dataclasses — immutable value objects. Weather comes from Open-Meteo (free, no API key) at hourly resolution. Tides come from NOAA CO-OPS by auto-selecting the nearest station to the boat's GPS position.

A privacy detail: lat/lon precision is reduced to 0.01° (~1.1 km) before querying Open-Meteo, so the API never learns the boat's exact position.

## 10. Audio & Transcription — `audio.py` + `transcribe.py`

HelmLog records debrief audio via USB microphone and transcribes it with faster-whisper, optionally with speaker diarisation via pyannote.

```bash
sed -n '85,120p' src/helmlog/audio.py
```

```output
class AudioRecorder:
    """Records audio from a USB input device to a WAV file.

    Usage::

        recorder = AudioRecorder()
        session = await recorder.start(config)
        ...
        completed = await recorder.stop()
    """

    def __init__(self) -> None:
        self._stream: Any | None = None
        self._sound_file: Any | None = None
        self._writer_thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()
        self._chunk_queue: queue.Queue[Any] = queue.Queue()
        self._session: AudioSession | None = None

    # ------------------------------------------------------------------
    # Device enumeration
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices() -> list[dict[str, object]]:
        """Return a list of available audio input devices.

        Each entry is a dict with keys: index, name, max_input_channels,
        default_samplerate.
        """
        import sounddevice as sd

        devices = sd.query_devices()
        result: list[dict[str, object]] = []
        for idx, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
```

The recorder uses a producer-consumer pattern: a `sounddevice` input stream pushes audio chunks to a `queue.Queue`, and a writer thread drains the queue to a WAV file via `soundfile`. This keeps audio capture on its own thread, decoupled from the asyncio event loop.

Transcription has two paths: remote offload (`TRANSCRIBE_URL` → POST the WAV to a more powerful machine) or local (`faster-whisper` on the Pi itself). The transcription output is segments with timestamps and optional speaker labels, stored in the `transcripts` table.

## 11. Polar Performance — `polar.py`

A polar diagram maps boat speed against true wind angle and speed. HelmLog builds a statistical baseline from historical races, then compares live speed to the baseline during new sessions.

```bash
sed -n '36,73p' src/helmlog/polar.py
```

```output
def _tws_bin(tws_kts: float) -> int:
    """Return the integer TWS bin (floor of knots, min 0)."""
    return max(0, int(math.floor(tws_kts)))


def _twa_bin(twa_deg: float) -> int:
    """Return the TWA bin: fold to [0, 180) and floor to nearest _TWA_BIN_SIZE."""
    twa_abs = abs(twa_deg) % 360
    if twa_abs > 180:
        twa_abs = 360 - twa_abs
    return int(math.floor(twa_abs / _TWA_BIN_SIZE)) * _TWA_BIN_SIZE


def _compute_twa(
    wind_angle_deg: float,
    reference: int,
    heading_deg: float | None,
) -> float | None:
    """Derive TWA magnitude from a wind record.

    Returns the absolute TWA in [0, 180], or None if the reference is
    unsupported or the required heading is absent.
    """
    if reference == _WIND_REF_BOAT:
        return abs(wind_angle_deg) % 360
    if reference == _WIND_REF_NORTH:
        if heading_deg is None:
            return None
        twa_raw = (wind_angle_deg - heading_deg + 360) % 360
        return twa_raw if twa_raw <= 180 else 360 - twa_raw
    return None  # apparent wind or unknown reference


# ---------------------------------------------------------------------------
# Baseline builder
# ---------------------------------------------------------------------------


```

TWS is binned to 1-knot buckets, TWA to 5-degree buckets folded to [0°, 180°) (port/starboard symmetric). For each (TWS, TWA) bin, the baseline stores mean and 90th-percentile boat speed from historical races.

The `_compute_twa()` function handles two wind references: boat-referenced (TWA comes directly) and north-referenced (TWD — subtract heading to get TWA). This matters because B&G systems report wind differently depending on configuration.

The result: `BSP_DELTA` in exports tells you 'I was 0.3 knots below my typical speed at this wind angle and speed.' That's the core of performance debrief.

## 12. Maneuver Detection — `maneuver_detector.py`

After a session ends, the maneuver detector scans the heading time series to find tacks, gybes, and mark roundings.

```bash
sed -n '37,43p' src/helmlog/maneuver_detector.py && echo '---' && sed -n '72,99p' src/helmlog/maneuver_detector.py
```

```output
_HDG_THRESHOLD: float = 60.0  # minimum heading change to detect any maneuver (degrees)
_DETECTION_WINDOW_S: int = 15  # sliding window to accumulate heading change (seconds)
_PRE_WINDOW_S: int = 30  # look-back for BSP baseline (seconds)
_BSP_RECOVERY_FRACTION: float = 0.90  # fraction of baseline BSP to call "recovered"
_MIN_MANEUVER_GAP_S: int = 20  # minimum gap between consecutive maneuvers (seconds)

# State-classification buffers: measure TWA this many seconds before/after
---
class Maneuver:
    """A single detected sailing maneuver."""

    type: str  # tack | gybe | rounding | maneuver
    ts: datetime  # UTC start of maneuver
    end_ts: datetime | None  # UTC end (BSP recovery), or None
    duration_sec: float | None  # seconds from start to recovery
    loss_kts: float | None  # BSP loss vs pre-maneuver baseline (kts)
    vmg_loss_kts: float | None  # VMG loss (future use)
    tws_bin: int | None  # TWS bin at maneuver time
    twa_bin: int | None  # TWA bin at maneuver time (folded [0,180])
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _heading_change(h1: float, h2: float) -> float:
    """Signed heading change from h1 to h2 along the shortest arc, in (−180, 180]."""
    diff = (h2 - h1 + 360.0) % 360.0
    return diff if diff <= 180.0 else diff - 360.0


def _abs_total_change(hdg_series: list[float]) -> float:
    """Absolute value of the summed signed heading changes in a series."""
    if len(hdg_series) < 2:
```

The detection algorithm is two-phase:

**Phase 1 — Event detection:** Slide a 15-second window over the heading time series. Wherever the accumulated heading change exceeds 60°, mark an event. Enforce a 20-second minimum gap between events to avoid double-counting.

**Phase 2 — Classification:** For each event, measure the mean TWA before and after (skipping 3 seconds on each side for settling). If the boat stays upwind (TWA < 90°) on both sides → tack. Stays downwind (TWA > 90°) on both sides → gybe. Crosses 90° → mark rounding.

Each maneuver records the BSP loss (how much speed was lost relative to the 30-second pre-maneuver baseline) and duration (how long until 90% speed recovery). This is the data that makes debrief sessions actionable: 'your tack at 14:32 lost 1.2 knots and took 8 seconds to recover.'

## 13. Federation — `federation.py`, `peer_auth.py`, `peer_api.py`, `peer_client.py`

The federation system lets boats in a co-op (e.g., a yacht club fleet) share track data securely. It uses Ed25519 cryptography for boat identity and request authentication.

```bash
sed -n '49,100p' src/helmlog/federation.py
```

```output
class BoatCard:
    """Public identity of a boat — freely shareable."""

    pub_key: str  # base64-encoded Ed25519 public key
    fingerprint: str  # SHA-256 truncated base64url
    sail_number: str
    boat_name: str
    owner_email: str | None = None  # required for co-op, optional standalone
    tailscale_ip: str | None = None  # auto-detected Tailscale IPv4

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "pub": self.pub_key,
            "fingerprint": self.fingerprint,
            "sail_number": self.sail_number,
            "name": self.boat_name,
        }
        if self.owner_email:
            d["owner_email"] = self.owner_email
        if self.tailscale_ip:
            d["tailscale_ip"] = self.tailscale_ip
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def get_tailscale_ip() -> str | None:
    """Detect the local Tailscale IPv4 address, or None if unavailable."""
    try:
        return (
            subprocess.check_output(
                ["tailscale", "ip", "-4"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            or None
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


@dataclass(frozen=True)
class Charter:
    """Signed co-op charter record."""

    co_op_id: str  # fingerprint of admin's public key (single-moderator mode)
    name: str
    areas: list[str]
    admin_boat_pub: str
    admin_boat_fingerprint: str
    created_at: str
```

A `BoatCard` is the boat's public identity — Ed25519 public key, fingerprint (SHA-256 truncated to 16 chars base64url), sail number, and boat name. The fingerprint is the boat's unique ID in the co-op.

The federation lifecycle:
1. `helmlog identity init` generates an Ed25519 keypair and creates the boat card
2. `helmlog co-op create` creates a co-op charter (signed by the admin boat)
3. `helmlog co-op invite` generates an invite bundle for another boat
4. The invited boat joins, and both boats can now query each other's shared sessions

```bash
sed -n '58,90p' src/helmlog/peer_auth.py
```

```output
def sign_request(
    private_key: Ed25519PrivateKey,
    fingerprint: str,
    method: str,
    path: str,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Build signed X-HelmLog-* headers for an outbound peer request.

    Returns a dict of headers to include in the HTTP request.
    """
    from helmlog.federation import sign_message

    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()
    if nonce is None:
        nonce = os.urandom(16).hex()

    canonical = f"{method.upper()} {path} {timestamp} {nonce}".encode()
    sig = sign_message(private_key, canonical)

    return {
        HDR_BOAT: fingerprint,
        HDR_TIMESTAMP: timestamp,
        HDR_NONCE: nonce,
        HDR_SIG: base64.b64encode(sig).decode(),
    }


# ---------------------------------------------------------------------------
# Verification (inbound)
# ---------------------------------------------------------------------------
```

The peer auth protocol signs every inter-boat HTTP request with Ed25519. The canonical message is `METHOD /path timestamp nonce`, signed with the boat's private key. Four custom headers carry the authentication:

- `X-HelmLog-Boat`: the sender's fingerprint
- `X-HelmLog-Timestamp`: ISO 8601 (must be within 5 minutes)
- `X-HelmLog-Nonce`: 16 random hex bytes (one-time use, prevents replay)
- `X-HelmLog-Sig`: base64 Ed25519 signature

The verifier looks up the sender's public key from the co-op membership, reconstructs the canonical message, and verifies the signature. Nonces are cached in-memory with automatic pruning every 60 seconds. This is a lightweight alternative to TLS client certificates that works over Tailscale.

The peer API (`peer_api.py`) is strict about what it shares. Only these fields cross the boat-to-boat boundary:

```bash
sed -n '37,50p' src/helmlog/peer_api.py
```

```output
SHARED_TRACK_FIELDS = frozenset(
    {
        "LAT",
        "LON",
        "BSP",
        "HDG",
        "COG",
        "SOG",
        "TWS",
        "TWA",
        "AWS",
        "AWA",
    }
)
```

Only 10 fields — the minimum needed for performance comparison. Audio, notes, crew, sails, transcripts, and video links are never exposed to peers. This is the data licensing policy in code: your boat owns its data, and co-op sharing is strictly view-only with an explicit allowlist.

Every co-op data access is also audit-logged (`co_op_audit` table) with the requesting boat's fingerprint, timestamp, resource accessed, and number of data points returned. Rate limiting (100 requests per 15 minutes per IP) prevents bulk scraping.

## 14. Camera Control — `cameras.py`

HelmLog controls Insta360 X4 cameras via the Open Spherical Camera (OSC) HTTP API. The Pi connects to the camera's WiFi hotspot through a dedicated wireless interface.

```bash
sed -n '51,75p' src/helmlog/cameras.py
```

```output
class Camera:
    """A configured camera with a human-readable name and network address."""

    name: str
    ip: str
    model: str = "insta360-x4"
    wifi_ssid: str | None = None
    wifi_password: str | None = None


@dataclass
class CameraStatus:
    """Result of a camera operation."""

    name: str
    ip: str
    recording: bool
    error: str | None = None
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

```

The camera module is another hardware isolation layer. Start/stop recording is a POST to the camera's HTTP endpoint at its AP gateway IP (typically 192.168.42.1). The web UI's camera admin page lets you ping cameras, start/stop recording, and see session history.

## 15. Race Management — `races.py`

Pure domain logic for session naming and configuration. No database access — just functions.

```bash
sed -n '88,112p' src/helmlog/races.py
```

```output
def build_race_name(event: str, d: date, race_num: int, session_type: str = "race") -> str:
    """Build a race identifier string.

    Example: build_race_name("BallardCup", date(2025, 8, 10), 2)
             → "20250810-BallardCup-2"
             build_race_name("BallardCup", date(2025, 8, 10), 1, "practice")
             → "20250810-BallardCup-P1"
    """
    if session_type == "practice":
        num_str = f"P{race_num}"
    elif session_type == "synthesized":
        num_str = f"S{race_num}"
    else:
        num_str = str(race_num)
    return f"{d.strftime('%Y%m%d')}-{event}-{num_str}"


def build_grafana_url(
    base_url: str,
    uid: str,
    start_ms: int,
    end_ms: int | None,
    *,
    org_id: int = 1,
) -> str:
```

Clean naming conventions: `20250810-BallardCup-2` for race 2, `20250810-BallardCup-P1` for practice 1, `20250810-BallardCup-S1` for synthesized. The `default_event_for_date()` function maps days of the week to event names (e.g., Tuesday = 'DuckDodge', Thursday = 'BYCSpinnaker') so you don't have to type the event name every time.

## 16. Deployment — `deploy.py`

The Pi can auto-update itself from GitHub. In 'evergreen' mode, it polls for new commits, pulls during a configurable off-peak window, and restarts the service.

```bash
sed -n '27,63p' src/helmlog/deploy.py
```

```output
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
```

The deploy system has a config hierarchy: DB-persisted settings (admin overrides) → environment variables → defaults. The deploy window restricts updates to off-peak hours (e.g., 0–6am) so the boat doesn't restart mid-race. Deployment logs go to the `deployment_log` table for audit.

## 17. System Monitoring — `monitor.py` + `influx.py`

A background task samples CPU, memory, disk, temperature, and network throughput every 2 seconds and writes the metrics to InfluxDB.

```bash
sed -n '30,50p' src/helmlog/monitor.py
```

```output
async def monitor_loop() -> None:
    """Collect CPU/mem/disk/temp/network metrics and write to InfluxDB periodically."""
    while True:
        try:
            await asyncio.to_thread(_collect_and_write)
        except Exception as exc:  # noqa: BLE001
            logger.warning("monitor_loop error (non-fatal): {}", exc)
        await asyncio.sleep(_get_interval())


def _collect_and_write() -> None:
    global _prev_net, _prev_net_time

    import psutil  # type: ignore[import-untyped]

    from helmlog.influx import _client

    cpu_pct: float = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    db_path = os.environ.get("DB_PATH", "data/logger.db")
    disk_root = db_path.split("/")[0] or "/"
```

The monitor runs `psutil` collection in a thread (via `asyncio.to_thread`) to avoid blocking the event loop. Errors are caught and logged but never crash the logger — monitoring is non-critical. The metrics feed a Grafana dashboard that the oncall watches during races.

## 18. Triggers — `triggers.py`

Triggers auto-create timestamped notes from transcript keywords. When the transcription mentions 'protest' or 'capsize', a note gets created at that exact timestamp for review.

```bash
sed -n '139,180p' src/helmlog/triggers.py
```

```output
async def scan_transcript(
    storage: Storage,
    audio_session_id: int,
    session_started_at: str,
    segments: list[dict[str, Any]],
    *,
    rules: list[TriggerRule] | None = None,
) -> int:
    """Scan transcript segments and create auto-notes. Returns count of notes created."""
    if rules is None:
        rules = load_trigger_rules()
    if not rules or not segments:
        return 0

    matches = _scan_segments(segments, rules)
    matches = _dedup_matches(matches)

    if not matches:
        return 0

    # Resolve the race_id for this audio session
    row = await storage.get_audio_session_row(audio_session_id)
    race_id: int | None = row["race_id"] if row and row.get("race_id") else None

    session_start = datetime.fromisoformat(session_started_at)
    if session_start.tzinfo is None:
        session_start = session_start.replace(tzinfo=UTC)

    created = 0
    for m in matches:
        # Compute wall-clock timestamp
        note_dt = session_start + timedelta(seconds=m.segment_start)
        note_ts = note_dt.isoformat()

        # Dedup check: existing auto-note within 5 seconds
        existing = await _check_existing_note(storage, race_id, audio_session_id, note_ts)
        if existing:
            continue

        context = _build_context(segments, m.segment_start)
        note_id = await storage.create_note(
            note_ts,
```

Triggers bridge audio transcription and the notes system. After a transcript is generated, `scan_transcript()` runs each segment against configurable keyword rules, deduplicates matches within a 30-second window, and creates timestamped notes. Each note includes surrounding context from the transcript. These notes surface in the session debrief page and the 'attention' page, flagging moments that need review.

## 19. Courses & Geography — `courses.py`

Defines race course marks and provides land/water detection for the Puget Sound racing area.

```bash
sed -n '51,80p' src/helmlog/courses.py
```

```output
CYC_MARKS: dict[str, CourseMark] = {
    "D": CourseMark("Duwamish Head Lt.", 47.5935, -122.3920),
    "E": CourseMark("Shilshole Bay Entrance", 47.6838, -122.4128),
    "H": CourseMark("0.3nm E of Skiff Pt", 47.6410, -122.4180),
    "I": CourseMark("0.5nm N of Alki Pt", 47.5847, -122.4210),
    "J": CourseMark("0.25nm SSW of marina N entrance", 47.6790, -122.4074),
    "K": CourseMark("Blakely Rock", 47.5877, -122.4873),
    "L": CourseMark("0.5nm SW of marina S entrance", 47.6728, -122.4125),
    "M": CourseMark("Meadow Pt. Buoy", 47.6940, -122.4080),
    "N": CourseMark("1.5nm E of TSS Buoy SF", 47.7300, -122.3800),
    "P": CourseMark("0.5nm NNE of Pt. Monroe", 47.7183, -122.4400),
    "Q": CourseMark("3.0nm 340\u00b0 from Meadow Pt", 47.7410, -122.4200),
    "R": CourseMark("0.5nm SW of Pt. Wells", 47.7680, -122.4032),
    "T": CourseMark("0.5nm SE of Pt. Jefferson", 47.7433, -122.4100),
    "U": CourseMark("U Mark", 47.7400, -122.3825),
    "V": CourseMark("0.3nm NNE of Wing Pt", 47.6295, -122.4900),
}


# ---------------------------------------------------------------------------
# Real coastline data — loaded from OSM-derived GeoJSON
# ---------------------------------------------------------------------------
# The land polygon covers the CYC racing area bounding box (lat 47.55–47.80,
# lon -122.53 – -122.34).  It was built from OpenStreetMap ``natural=coastline``
# ways, clipped to the bounding box, and simplified to ~30 m tolerance.
# Points inside the land polygon are on land; points inside the bounding box
# but outside the land polygon are in navigable water.

_BBOX_N = 47.80
_BBOX_S = 47.55
```

Real CYC (Corinthian Yacht Club) racing marks with lat/lon coordinates. The land detection uses an OSM-derived GeoJSON polygon of the Puget Sound coastline, loaded via Shapely, to ensure synthesized courses don't place marks on land. This is also used by the course builder when generating windward/leeward or triangle courses from a race committee position and wind direction.

## 20. The Frontend — Templates + JavaScript

The web UI is server-rendered Jinja2 templates with vanilla JavaScript for interactivity. No React, no build step.

```bash
find src/helmlog/templates -name '*.html' | sort
```

```output
src/helmlog/templates/admin/audit.html
src/helmlog/templates/admin/boats.html
src/helmlog/templates/admin/cameras.html
src/helmlog/templates/admin/deployment.html
src/helmlog/templates/admin/events.html
src/helmlog/templates/admin/federation.html
src/helmlog/templates/admin/settings.html
src/helmlog/templates/admin/users.html
src/helmlog/templates/attention.html
src/helmlog/templates/auth/forgot_password.html
src/helmlog/templates/auth/register.html
src/helmlog/templates/auth/reset_password.html
src/helmlog/templates/base.html
src/helmlog/templates/history.html
src/helmlog/templates/home.html
src/helmlog/templates/login.html
src/helmlog/templates/profile.html
src/helmlog/templates/sails.html
src/helmlog/templates/session.html
```

```bash
find src/helmlog/static -type f | sort
```

```output
src/helmlog/static/base.css
src/helmlog/static/history.js
src/helmlog/static/home.js
src/helmlog/static/session.js
src/helmlog/static/shared.js
```

19 templates, 5 static files. The architecture is intentionally simple:

- `base.html` is the master layout (nav, footer, CSS/JS includes) — all pages except `login.html` extend it
- `home.html` is the race control page: start/stop sessions, crew selection, sail configuration, live instruments
- `history.html` lists past sessions with search and filtering
- `session.html` is the debrief page: track map, maneuver timeline, transcripts, discussion threads, video sync
- `attention.html` surfaces flagged items needing review
- Admin pages manage users, cameras, settings, federation, and deployment

JavaScript files are page-specific (`home.js`, `history.js`, `session.js`) with shared utilities in `shared.js` (time formatting, nav initialization, etc.). No framework, no bundler.

## 21. Testing — `tests/`

The test suite runs on any machine with no hardware required.

```bash
find tests -name '*.py' | sort
```

```output
tests/conftest.py
tests/integration/__init__.py
tests/integration/conftest.py
tests/integration/seed.py
tests/integration/serve.py
tests/integration/test_auth_e2e.py
tests/integration/test_data_license_e2e.py
tests/integration/test_embargo_e2e.py
tests/integration/test_federation_e2e.py
tests/test_analysis.py
tests/test_audio.py
tests/test_auth.py
tests/test_cameras.py
tests/test_courses.py
tests/test_deploy.py
tests/test_export.py
tests/test_external.py
tests/test_federation_storage.py
tests/test_federation.py
tests/test_gaigps_import.py
tests/test_gaigps.py
tests/test_insta360.py
tests/test_maneuver_detector.py
tests/test_monitor.py
tests/test_new_features.py
tests/test_nmea2000.py
tests/test_notifications.py
tests/test_peer_api_security.py
tests/test_peer_api.py
tests/test_peer_auth.py
tests/test_pipeline.py
tests/test_polar.py
tests/test_race_classifier.py
tests/test_races.py
tests/test_sail_vmg.py
tests/test_settings.py
tests/test_sk_reader.py
tests/test_storage.py
tests/test_synthesize.py
tests/test_transcribe.py
tests/test_video.py
tests/test_web_federation_join.py
tests/test_web_federation.py
tests/test_web_synthesize.py
tests/test_web_wind_field.py
tests/test_web.py
tests/test_wind_field.py
tests/test_youtube.py
```

```bash
uv run pytest --co -q 2>&1 | tail -5
```

```output
src/helmlog/wind_field.py                          180    146    19%   85-104, 112-150, 166-179, 192-224, 233-240, 250-266, 275-297, 308-334, 350-386
src/helmlog/youtube.py                              66     43    35%   60-79, 84, 107-112, 131, 175-225
------------------------------------------------------------------------------
TOTAL                                            10213   8727    15%
1083 tests collected in 4.41s
```

47 test files, 1083 tests. The suite covers:

- **Unit tests** — pure function tests for decoding, export, polars, maneuver detection, races, courses, etc. Use in-memory SQLite fixtures from `conftest.py`. Hardware modules are mocked.
- **Web tests** (`test_web.py` and friends) — use `httpx.AsyncClient` with `ASGITransport` to exercise all API routes without a running server.
- **Integration tests** (`tests/integration/`) — two-boat federation simulation with real Ed25519 keypairs, real signing, real nonce replay protection. 32 tests covering co-op lifecycle, auth, embargo enforcement, and data licensing compliance.

The test strategy is proportional to risk: critical modules (auth, federation, storage) have extensive coverage; low-risk modules (templates, CSS) get smoke tests.

## 22. Putting It All Together — The Data Flow

Let's trace a single instrument reading from the B&G display head to a CSV export:

1. **B&G → CAN bus** — The instrument system broadcasts NMEA 2000 PGNs on the CAN bus
2. **CAN bus → Signal K Server** — Signal K reads the bus, decodes PGNs, serves WebSocket deltas
3. **Signal K → `sk_reader.py`** — `process_delta()` parses JSON, converts units, emits a `SpeedRecord(speed_kts=6.2)`
4. **Record → `main.py`** — The core loop calls `storage.update_live(record)` (updates live cache for the web UI) and `storage.write(record)` (buffers for disk)
5. **Buffer → SQLite** — After 200 records or 1 second, `_auto_flush()` commits the batch
6. **Session ends** — User clicks 'Stop' in the web UI; `session_active` goes false
7. **Post-session** — Maneuver detection runs, polar baseline updates, audio transcription queues
8. **Export** — `export_to_file()` joins all tables by timestamp, adds weather/tides/polars, writes CSV

The same `SpeedRecord` might also flow to:
- The **live display** on `home.html` (via the `_live` cache, polled by `home.js`)
- A **polar delta** calculation (`BSP - BSP_BASELINE`)
- A **peer boat** requesting track data via the federation API (if shared)
- A **maneuver's** BSP loss metric (speed before/after a tack)

## 23. Architecture Principles — Why It's Built This Way

**Hardware isolation** — Only three modules touch hardware: `can_reader.py` (CAN bus), `cameras.py` (Insta360 HTTP), `audio.py` (USB microphone). Everything else works with decoded data structures. This is why 1083 tests run on a Mac with no Pi attached.

**Decode early, store clean** — Raw instrument data is decoded to named dataclasses as soon as it arrives. No module downstream handles raw bytes, SI units, or SK JSON. Heading is always in degrees, speed is always in knots, temperature is always in Celsius.

**SQLite is the single source of truth** — All data is written to SQLite with UTC timestamps. The live cache is a convenience for the web UI; the database is what survives reboots. Flush frequently (every second) to minimize data loss on power cuts.

**Async throughout** — Every I/O operation (storage, web server, HTTP fetches, WebSocket) is async. The event loop is never blocked. Hardware operations that are inherently blocking (CAN recv, psutil, sounddevice) run in threads via `asyncio.to_thread()`.

**Configuration hierarchy** — Environment variables (from `.env`) → database overrides (admin settings page) → hardcoded defaults. This lets the admin tune the system from the web UI without SSH access.

**Data licensing as code** — The allowlist in `peer_api.py`, the audit logging, the embargo checks, and the PII exclusion rules aren't policy documents — they're enforced in the code path. Every co-op data access is logged. The data licensing policy document (`docs/data-licensing.md`) is the spec; the code is the implementation.
