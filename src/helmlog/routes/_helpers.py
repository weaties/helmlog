"""Shared helpers and models for route handlers.

Routers import from here instead of web.py to avoid circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Response
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from fastapi import Request

    from helmlog.cache import WebCache
    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Shared rate limiter — the same instance is also stored on app.state.limiter by create_app()
limiter = Limiter(key_func=get_remote_address, config_filename="/dev/null")

# ---------------------------------------------------------------------------
# Git version info — read once at import time
# ---------------------------------------------------------------------------


def _get_git_info() -> str:
    """Return 'branch @ shortsha · clean/dirty' from the current git repo."""
    import subprocess

    try:
        _repo = str(Path(__file__).resolve().parents[3])
        _git = ["git", "-c", f"safe.directory={_repo}", "--no-optional-locks"]

        def _run(args: list[str]) -> str:
            return subprocess.check_output(
                [*_git, *args], cwd=_repo, stderr=subprocess.DEVNULL, text=True
            ).strip()

        branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])
        sha = _run(["rev-parse", "--short=7", "HEAD"])

        dirty = bool(_run(["status", "--porcelain"]))

        if not dirty:
            try:
                unpushed = _run(["rev-list", "@{upstream}..HEAD", "--count"])
                if int(unpushed) > 0:
                    dirty = True
            except Exception:  # noqa: BLE001
                pass

        import socket

        hostname = socket.gethostname()
        status = "dirty" if dirty else "clean"
        return f"{hostname} · {branch} @ {sha} · {status}"
    except Exception:  # noqa: BLE001
        return ""


GIT_INFO: str = _get_git_info()
GIT_SHORT_SHA: str = ""
STARTUP_SHA: str = ""
try:
    import subprocess as _sp

    _repo_dir = str(Path(__file__).resolve().parents[3])
    STARTUP_SHA = _sp.check_output(  # noqa: S603, S607
        ["git", "-c", f"safe.directory={_repo_dir}", "rev-parse", "HEAD"],
        cwd=_repo_dir,
        text=True,
        stderr=_sp.DEVNULL,
    ).strip()
    GIT_SHORT_SHA = STARTUP_SHA[:7]
    del _sp, _repo_dir
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------------------
# Shared request helpers
# ---------------------------------------------------------------------------


def get_storage(request: Request) -> Storage:
    """Return the Storage instance from the app state."""
    return request.app.state.storage  # type: ignore[no-any-return]


def get_web_cache(request: Request) -> WebCache | None:
    """Return the WebCache bound to the app, or None if not configured."""
    return getattr(request.app.state, "web_cache", None)


_CACHE_CONTROL = "private, max-age=0, must-revalidate"


async def t1_cached_json_response(
    request: Request,
    *,
    cache_key: str,
    ttl_seconds: float,
    compute: Callable[[], Awaitable[Any]],
) -> Response:
    """T1 (in-process dict + TTL) caching wrapper for JSON endpoints (#594/#608).

    Unlike :func:`cached_json_response`, there's no ETag negotiation — T1 is
    process-scoped and typical payloads are thin enough that a full-body
    round-trip is acceptable. Used for list endpoints (the session list)
    where the key space is large (many filter combinations) and correctness
    beyond ``ttl_seconds`` is handled by explicit invalidation from the
    race-mutation hook.

    Cache failures degrade to un-cached behaviour — the request never fails
    because of a cache problem.
    """
    cache = get_web_cache(request)
    if cache is not None:
        hit = cache.t1_get(cache_key)
        if hit is not None:
            return JSONResponse(hit)
    payload = await compute()
    if cache is not None:
        cache.t1_put(cache_key, payload, ttl_seconds=ttl_seconds)
    return JSONResponse(payload)


async def cached_json_response(
    request: Request,
    *,
    race_id: int,
    key_family: str,
    compute: Callable[[], Awaitable[Any]],
) -> Response:
    """ETag + 304 + T2 caching wrapper for per-race JSON endpoints (#594).

    Flow:
      1. Resolve the race's ``data_hash``. If the race doesn't exist, let
         ``compute`` run so it can raise the correct 404.
      2. If ``If-None-Match`` matches ``data_hash``, return 304 immediately.
      3. If the T2 cache has an entry for ``(key_family, race_id)`` with
         matching hash, return it as JSON.
      4. Otherwise call ``compute``, store the result in T2, emit the
         ``ETag`` + ``Cache-Control`` headers, and return JSON.

    Cache failures never fail the request — a missing ``WebCache`` or any
    raise from cache read/write degrades to un-cached behaviour.
    """
    from helmlog.cache import resolve_race_data_hash

    cache = get_web_cache(request)
    storage = get_storage(request)

    data_hash: str | None = None
    if cache is not None:
        try:
            data_hash = await resolve_race_data_hash(storage, race_id)
        except Exception:  # noqa: BLE001 — cache failures must never fail a request
            data_hash = None

    if data_hash is not None:
        if_none_match = request.headers.get("if-none-match", "").strip().strip('"')
        if if_none_match == data_hash:
            return Response(
                status_code=304,
                headers={"ETag": f'"{data_hash}"', "Cache-Control": _CACHE_CONTROL},
            )
        cached = None
        if cache is not None:
            cached = await cache.t2_get(key_family, race_id=race_id, data_hash=data_hash)
        if cached is not None:
            return JSONResponse(
                cached,
                headers={"ETag": f'"{data_hash}"', "Cache-Control": _CACHE_CONTROL},
            )

    payload = await compute()

    if cache is not None and data_hash is not None:
        await cache.t2_put(key_family, race_id=race_id, data_hash=data_hash, value=payload)

    headers = (
        {"ETag": f'"{data_hash}"', "Cache-Control": _CACHE_CONTROL} if data_hash is not None else {}
    )
    return JSONResponse(payload, headers=headers)


def tpl_ctx(request: Request, page: str, **extra: Any) -> dict[str, Any]:  # noqa: ANN401
    """Build the standard template context dict."""
    theme_css: str = getattr(request.state, "theme_css", "")
    return {
        "request": request,
        "active_page": page,
        "git_info": GIT_INFO,
        "git_sha": GIT_SHORT_SHA,
        "theme_css": theme_css,
        **extra,
    }


async def audit(
    request: Request,
    action: str,
    detail: str | None = None,
    user: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget audit log entry."""
    storage = get_storage(request)
    uid = user.get("id") if user else None
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    await storage.log_action(action, detail=detail, user_id=uid, ip_address=ip, user_agent=ua)


async def load_cameras(request: Request) -> list[Any]:
    """Load cameras from the database and return Camera objects."""
    from helmlog.cameras import Camera

    storage = get_storage(request)
    rows = await storage.list_cameras()
    return [
        Camera(
            name=r["name"],
            ip=r["ip"],
            model=r["model"],
            wifi_ssid=r.get("wifi_ssid"),
            wifi_password=r.get("wifi_password"),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Settings definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingDef:
    """Metadata for one admin-configurable setting."""

    key: str
    label: str
    input_type: str  # "text", "number", "select"
    default: str
    help_text: str = ""
    options: tuple[str, ...] = ()
    sensitive: bool = False


SETTINGS_DEFS: tuple[SettingDef, ...] = (
    SettingDef(
        key="TRANSCRIBE_URL",
        label="Remote transcription URL",
        input_type="text",
        default="",
        help_text="Base URL for the remote transcription worker (e.g. http://macbook:8321). Leave blank for local transcription.",
    ),
    SettingDef(
        key="WHISPER_MODEL",
        label="Whisper model size",
        input_type="select",
        default="base",
        options=("tiny", "base", "small", "medium", "large"),
        help_text="Larger models are more accurate but slower.",
    ),
    SettingDef(
        key="PI_API_URL",
        label="Pi API URL",
        input_type="text",
        default="http://localhost:3002",
        help_text="Base URL for the HelmLog API (used by the video pipeline).",
    ),
    SettingDef(
        key="TIMEZONE",
        label="Display timezone",
        input_type="text",
        default="America/Los_Angeles",
        help_text="IANA timezone name for display (e.g. America/Los_Angeles).",
    ),
    SettingDef(
        key="VIDEO_PRIVACY",
        label="YouTube upload privacy",
        input_type="select",
        default="private",
        options=("private", "unlisted", "public"),
        help_text="Privacy status for auto-uploaded YouTube videos. Default is 'private' per data policy.",
    ),
    SettingDef(
        key="EXTERNAL_DATA_ENABLED",
        label="External data fetching",
        input_type="select",
        default="true",
        options=("true", "false"),
        help_text="Enable weather/tide fetching (sends GPS position to Open-Meteo and NOAA).",
    ),
    SettingDef(
        key="VIDEO_CLEANUP_AFTER_UPLOAD",
        label="Delete video after upload",
        input_type="select",
        default="false",
        options=("true", "false"),
        help_text="Delete stitched MP4 from Mac after successful YouTube upload.",
    ),
    SettingDef(
        key="PI_SESSION_COOKIE",
        label="Pi session cookie",
        input_type="text",
        default="",
        sensitive=True,
        help_text="Session cookie for the Pi API (used by the video pipeline to link videos to sessions).",
    ),
    SettingDef(
        key="CAMERA_START_TIMEOUT",
        label="Camera timeout (seconds)",
        input_type="number",
        default="10",
        help_text="Timeout in seconds for camera start/stop HTTP commands.",
    ),
    SettingDef(
        key="MONITOR_INTERVAL_S",
        label="Health monitor interval (seconds)",
        input_type="number",
        default="2",
        help_text="How often to collect Pi health metrics for the dashboard (1\u2013300 seconds).",
    ),
    SettingDef(
        key="NETWORK_AUTO_SWITCH",
        label="Auto-switch WLAN for races",
        input_type="select",
        default="false",
        options=("true", "false"),
        help_text="Automatically switch WLAN to camera Wi-Fi on race start and revert on race end.",
    ),
    SettingDef(
        key="NETWORK_DEFAULT_PROFILE",
        label="Default WLAN profile ID",
        input_type="text",
        default="",
        help_text="WLAN profile ID to revert to after a race ends (used with auto-switch).",
    ),
    SettingDef(
        key="RUDDER_STORAGE_HZ",
        label="Rudder angle storage rate (Hz)",
        input_type="number",
        default="2",
        help_text="How often to store rudder angle readings (Hz). ESP32 publishes at 10 Hz; default stores at 2 Hz. Live display always shows latest value.",
    ),
    SettingDef(
        key="AUDIO_CHANNELS",
        label="Audio channels",
        input_type="select",
        default="1",
        options=("1", "2", "4"),
        help_text="Number of channels to record. Use '4' for multi-mic isolation.",
    ),
    SettingDef(
        key="AUDIO_CH1_POS",
        label="Audio Channel 1 Position",
        input_type="text",
        default="helm",
        help_text="Crew position assigned to audio channel 1.",
    ),
    SettingDef(
        key="AUDIO_CH2_POS",
        label="Audio Channel 2 Position",
        input_type="text",
        default="tactician",
        help_text="Crew position assigned to audio channel 2.",
    ),
    SettingDef(
        key="AUDIO_CH3_POS",
        label="Audio Channel 3 Position",
        input_type="text",
        default="main",
        help_text="Crew position assigned to audio channel 3.",
    ),
    SettingDef(
        key="AUDIO_CH4_POS",
        label="Audio Channel 4 Position",
        input_type="text",
        default="pit",
        help_text="Crew position assigned to audio channel 4.",
    ),
    SettingDef(
        key="AUDIO_STREAM_THRESHOLD_MINUTES",
        label="Audio streaming threshold (minutes)",
        input_type="number",
        default="45",
        help_text=(
            "Sibling-mode sessions at or above this length stream audio via HTTP "
            "range requests on the session page instead of decoding the whole WAV "
            "into memory. Lower this if long sessions hang on ‘Loading audio…’; "
            "raise it if sample-accurate scrubbing matters more than memory. "
            "Takes effect on the next session page load."
        ),
    ),
    SettingDef(
        key="AUDIO_SPEAKERS_PER_CHANNEL",
        label="Expected speakers per sibling mic",
        input_type="number",
        default="2",
        help_text=(
            "Number of crew voices sharing each sibling-mode mono WAV (one USB "
            "receiver mixing N mics into one stream). Passed to pyannote as a "
            "num_speakers hint so it doesn't over-split noisy audio into 5–9 "
            "spurious labels. 0 lets pyannote pick the count automatically. "
            "Only used for sibling captures; takes effect on retranscribe."
        ),
    ),
)

SETTINGS_BY_KEY: dict[str, SettingDef] = {s.key: s for s in SETTINGS_DEFS}

# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class EventRequest(BaseModel):
    event_name: str


class CrewEntry(BaseModel):
    position_id: int
    user_id: int | None = None
    attributed: bool = True
    body_weight: float | None = None
    gear_weight: float | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class WeightUpdate(BaseModel):
    weight_lbs: float | None = None
    gear_default_lbs: float | None = None


class BulkCrewBackfillEntry(BaseModel):
    race_id: int
    crew: list[CrewEntry]
    gear_preset: str | None = None


class BulkCrewBackfill(BaseModel):
    updates: list[BulkCrewBackfillEntry]


class PositionEntry(BaseModel):
    name: str
    display_order: int


class BoatCreate(BaseModel):
    sail_number: str
    name: str | None = None
    class_name: str | None = None


class BoatUpdate(BaseModel):
    sail_number: str | None = None
    name: str | None = None
    class_name: str | None = None


class RaceResultEntry(BaseModel):
    place: int
    boat_id: int | None = None
    sail_number: str | None = None
    finish_time: str | None = None
    dnf: bool = False
    dns: bool = False
    notes: str | None = None


class VideoCreate(BaseModel):
    youtube_url: str
    label: str = ""
    sync_utc: str  # UTC ISO 8601
    sync_offset_s: float = 0.0


class VideoUpdate(BaseModel):
    label: str | None = None
    sync_utc: str | None = None
    sync_offset_s: float | None = None


class SailCreate(BaseModel):
    type: str  # 'main' | 'jib' | 'spinnaker'
    name: str
    notes: str | None = None
    point_of_sail: str | None = None  # 'upwind' | 'downwind' | 'both'


class SailUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    active: bool | None = None
    point_of_sail: str | None = None  # 'upwind' | 'downwind' | 'both'


class RaceSailsSet(BaseModel):
    main_id: int | None = None
    jib_id: int | None = None
    spinnaker_id: int | None = None


# ---------------------------------------------------------------------------
# App session state (replaces closure variables in create_app)
# ---------------------------------------------------------------------------


@dataclass
class AppSessionState:
    """Mutable state shared across race/debrief route handlers."""

    audio_session_id: int | None = None
    debrief_audio_session_id: int | None = None
    debrief_race_id: int | None = None
    debrief_race_name: str | None = None
    debrief_start_utc: datetime | None = None
    schedule_task: asyncio.Task[None] | None = field(default=None, repr=False)
    schedule_first_check_done: bool = False
