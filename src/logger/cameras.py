"""Insta360 X4 camera control via Open Spherical Camera (OSC) HTTP API.

Hardware isolation: all camera HTTP communication lives in this module so
the rest of the codebase can be tested without physical cameras on the
network.  Only ``main.py`` imports this module directly.

The X4 runs as a WiFi **access point** (AP) — it cannot join an existing
network.  The Pi connects to each camera's hotspot via a dedicated WiFi
interface and reaches the camera at its AP gateway IP (default
``192.168.42.1``).  See ``docs/camera-setup.md`` for wiring details.

The Pi sends ``POST http://<camera-ip>/osc/commands/execute`` commands to
start/stop recording and query status.

Configuration via environment variable::

    CAMERAS=main:192.168.42.1
    CAMERA_START_TIMEOUT=10
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

if TYPE_CHECKING:
    from logger.storage import Storage

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT: float = float(os.environ.get("CAMERA_START_TIMEOUT", "10"))
_OSC_PATH = "/osc/commands/execute"
_OSC_HEADERS = {"X-XSRF-Protected": "1"}


@dataclass(frozen=True)
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


def parse_cameras_config(cameras_str: str) -> list[Camera]:
    """Parse ``CAMERAS`` env var: ``'name1:ip1,name2:ip2'`` → list of Camera.

    Returns an empty list if *cameras_str* is blank.
    """
    cameras_str = cameras_str.strip()
    if not cameras_str:
        return []

    cameras: list[Camera] = []
    for entry in cameras_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning("Skipping invalid camera entry (missing ':'): {!r}", entry)
            continue
        name, ip = entry.split(":", maxsplit=1)
        name = name.strip()
        ip = ip.strip()
        if not name or not ip:
            logger.warning("Skipping camera entry with empty name or IP: {!r}", entry)
            continue
        cameras.append(Camera(name=name, ip=ip))
    return cameras


# ---------------------------------------------------------------------------
# Single-camera operations
# ---------------------------------------------------------------------------


def _osc_url(camera: Camera) -> str:
    return f"http://{camera.ip}{_OSC_PATH}"


async def start_camera(camera: Camera, timeout: float = _DEFAULT_TIMEOUT) -> CameraStatus:
    """Send ``camera.startCapture`` to a single camera.

    Returns a :class:`CameraStatus` with ``latency_ms`` measuring the
    round-trip time of the HTTP request (used as ``sync_offset_ms``).
    """
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _osc_url(camera),
                headers=_OSC_HEADERS,
                json={"name": "camera.startCapture"},
                timeout=timeout,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            resp.raise_for_status()
            logger.debug("Camera {} startCapture response: {}", camera.name, resp.text)
            return CameraStatus(
                name=camera.name, ip=camera.ip, recording=True, latency_ms=latency_ms
            )
    except (httpx.HTTPError, OSError) as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("Camera {} startCapture failed: {}", camera.name, exc)
        return CameraStatus(
            name=camera.name,
            ip=camera.ip,
            recording=False,
            error=str(exc),
            latency_ms=latency_ms,
        )


async def stop_camera(camera: Camera, timeout: float = _DEFAULT_TIMEOUT) -> CameraStatus:
    """Send ``camera.stopCapture`` to a single camera.

    The X4 firmware rejects ``stopCapture`` for recordings started via the
    physical shutter button (400 / ``disabledCommand``).  Sending a
    ``startCapture`` first "claims" the session for OSC, after which
    ``stopCapture`` succeeds.
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _osc_url(camera),
                headers=_OSC_HEADERS,
                json={"name": "camera.stopCapture"},
                timeout=timeout,
            )
            if resp.status_code == 400:
                body = resp.json()
                code = body.get("error", {}).get("code", "")
                if code == "disabledCommand":
                    # Claim the session via startCapture, then retry stop
                    logger.debug("Camera {} stopCapture rejected, claiming via startCapture", camera.name)
                    await client.post(
                        _osc_url(camera),
                        headers=_OSC_HEADERS,
                        json={"name": "camera.startCapture"},
                        timeout=timeout,
                    )
                    resp = await client.post(
                        _osc_url(camera),
                        headers=_OSC_HEADERS,
                        json={"name": "camera.stopCapture"},
                        timeout=timeout,
                    )
            resp.raise_for_status()
            logger.debug("Camera {} stopCapture response: {}", camera.name, resp.text)
            return CameraStatus(name=camera.name, ip=camera.ip, recording=False)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("Camera {} stopCapture failed: {}", camera.name, exc)
        return CameraStatus(name=camera.name, ip=camera.ip, recording=True, error=str(exc))


async def get_status(camera: Camera, timeout: float = 5.0) -> CameraStatus:
    """Query ``camera.getOptions`` for ``captureStatus``."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _osc_url(camera),
                headers=_OSC_HEADERS,
                json={
                    "name": "camera.getOptions",
                    "parameters": {"optionNames": ["captureStatus"]},
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            results = data.get("results", {})
            options = results.get("options", {})
            recording = options.get("captureStatus") == "shooting"
            return CameraStatus(name=camera.name, ip=camera.ip, recording=recording)
    except (httpx.HTTPError, OSError) as exc:
        return CameraStatus(name=camera.name, ip=camera.ip, recording=False, error=str(exc))


# ---------------------------------------------------------------------------
# Multi-camera operations
# ---------------------------------------------------------------------------


async def start_all(
    cameras: list[Camera],
    session_id: int,
    storage: Storage,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[CameraStatus]:
    """Start all cameras in parallel and write ``camera_sessions`` rows.

    Individual camera failures are logged but never raised — the race must
    not be blocked by a camera that is offline or slow.
    """
    tasks = [start_camera(cam, timeout) for cam in cameras]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    statuses: list[CameraStatus] = []
    now = datetime.now(UTC)
    for cam, result in zip(cameras, results, strict=True):
        if isinstance(result, BaseException):
            status = CameraStatus(name=cam.name, ip=cam.ip, recording=False, error=str(result))
        else:
            status = result

        statuses.append(status)

        # Persist to camera_sessions
        started_utc = now if status.recording else None
        await storage.add_camera_session(
            session_id=session_id,
            camera_name=cam.name,
            camera_ip=cam.ip,
            started_utc=started_utc,
            sync_offset_ms=status.latency_ms,
            error=status.error,
        )

    return statuses


async def stop_all(
    cameras: list[Camera],
    session_id: int,
    storage: Storage,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[CameraStatus]:
    """Stop all cameras in parallel and update ``camera_sessions`` rows."""
    tasks = [stop_camera(cam, timeout) for cam in cameras]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    statuses: list[CameraStatus] = []
    now = datetime.now(UTC)
    for cam, result in zip(cameras, results, strict=True):
        if isinstance(result, BaseException):
            status = CameraStatus(name=cam.name, ip=cam.ip, recording=True, error=str(result))
        else:
            status = result

        statuses.append(status)

        # Update the camera_sessions row
        await storage.update_camera_session_stop(
            session_id=session_id,
            camera_name=cam.name,
            stopped_utc=now,
            error=status.error,
        )

    return statuses
