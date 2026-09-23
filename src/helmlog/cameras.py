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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

# Camera / CameraStatus live in the pure ``camera_control`` module so the web
# tier can reference them without importing this hardware module.  Re-exported
# here for backward compatibility (existing callers import them from
# ``helmlog.cameras``).
from helmlog.camera_control import (
    Camera,
    CameraStatus,
    load_cameras_from_storage,
)

if TYPE_CHECKING:
    from helmlog.storage import Storage

__all__ = [
    "Camera",
    "CameraStatus",
    "HardwareCameraController",
    "get_status",
    "parse_cameras_config",
    "start_all",
    "start_camera",
    "stop_all",
    "stop_camera",
]


def _default_timeout() -> float:
    """Read camera timeout each call so admin changes take effect without restart."""
    return float(os.environ.get("CAMERA_START_TIMEOUT", "10"))


def _default_stop_timeout() -> float:
    # Short by design: at end-race we expect the camera to be reachable. If it's
    # not (Pi off the camera's hotspot, camera off, dead battery), waiting the
    # full start timeout × multiple endpoints can wedge end-race for minutes (#773).
    return float(os.environ.get("CAMERA_STOP_TIMEOUT", "2"))


_OSC_PATH = "/osc/commands/execute"
_OSC_HEADERS = {"X-XSRF-Protected": "1"}


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


def _error_msg(exc: Exception, camera: Camera, action: str) -> str:
    """Build a human-readable error string, even when ``str(exc)`` is empty."""
    msg = str(exc).strip()
    if msg:
        return msg
    return f"Camera {camera.name} unreachable ({type(exc).__name__} during {action})"


async def start_camera(camera: Camera, timeout: float | None = None) -> CameraStatus:
    """Send ``camera.startCapture`` to a single camera.

    Returns a :class:`CameraStatus` with ``latency_ms`` measuring the
    round-trip time of the HTTP request (used as ``sync_offset_ms``).
    """
    timeout = timeout if timeout is not None else _default_timeout()
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
        err = _error_msg(exc, camera, "startCapture")
        logger.warning("Camera {} startCapture failed: {}", camera.name, err)
        return CameraStatus(
            name=camera.name,
            ip=camera.ip,
            recording=False,
            error=err,
            latency_ms=latency_ms,
        )


async def stop_camera(camera: Camera, timeout: float | None = None) -> CameraStatus:
    """Send ``camera.stopCapture`` to a single camera.

    The X4 firmware rejects ``stopCapture`` for recordings started via the
    physical shutter button (400 / ``disabledCommand``).  Sending a
    ``startCapture`` first "claims" the session for OSC, after which
    ``stopCapture`` succeeds.
    """
    timeout = timeout if timeout is not None else _default_stop_timeout()
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
                    logger.debug(
                        "Camera {} stopCapture rejected, claiming via startCapture", camera.name
                    )
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
        err = _error_msg(exc, camera, "stopCapture")
        logger.warning("Camera {} stopCapture failed: {}", camera.name, err)
        return CameraStatus(name=camera.name, ip=camera.ip, recording=True, error=err)


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
        return CameraStatus(
            name=camera.name,
            ip=camera.ip,
            recording=False,
            error=_error_msg(exc, camera, "getStatus"),
        )


# ---------------------------------------------------------------------------
# Multi-camera operations
# ---------------------------------------------------------------------------


async def start_all(
    cameras: list[Camera],
    session_id: int,
    storage: Storage,
    timeout: float | None = None,
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
    timeout: float | None = None,
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


# ---------------------------------------------------------------------------
# Controller — the hardware-backed adapter injected on app.state.cameras (#780)
# ---------------------------------------------------------------------------


class HardwareCameraController:
    """Concrete :class:`~helmlog.camera_control.CameraController` over real OSC
    cameras.  Constructed by ``main.py`` and injected so web routes never import
    this hardware module.
    """

    async def load(self, storage: Storage) -> list[Camera]:
        return await load_cameras_from_storage(storage)

    async def statuses(self, cams: list[Camera]) -> list[CameraStatus]:
        results = await asyncio.gather(
            *(get_status(cam) for cam in cams), return_exceptions=True
        )
        out: list[CameraStatus] = []
        for cam, st in zip(cams, results, strict=True):
            if isinstance(st, BaseException):
                out.append(
                    CameraStatus(name=cam.name, ip=cam.ip, recording=False, error=str(st))
                )
            else:
                out.append(st)
        return out

    async def start(self, cam: Camera) -> CameraStatus:
        return await start_camera(cam)

    async def stop(self, cam: Camera) -> CameraStatus:
        return await stop_camera(cam)

    async def start_all(
        self, cams: list[Camera], session_id: int, storage: Storage
    ) -> list[CameraStatus]:
        return await start_all(cams, session_id, storage)

    async def stop_all(
        self, cams: list[Camera], session_id: int, storage: Storage
    ) -> list[CameraStatus]:
        return await stop_all(cams, session_id, storage)
