"""Camera abstraction shared by the web tier and the hardware module (#780).

Hardware isolation: web routes must never import ``helmlog.cameras`` (the OSC
HTTP/hardware module).  Instead they reach camera operations through the
:class:`CameraController` injected on ``app.state.cameras``.  ``main.py`` — the
only module allowed to touch hardware — constructs the concrete
``HardwareCameraController`` (defined in ``cameras.py``) and passes it into
``create_app``.  Tests and headless runs fall back to :class:`NullCameraController`.

This module holds only the pure pieces (dataclasses, the controller protocol,
and the no-op default), so importing it carries no hardware dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from helmlog.storage import Storage


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


@runtime_checkable
class CameraController(Protocol):
    """Operations the web tier needs, with no hardware import on the caller side."""

    async def load(self, storage: Storage) -> list[Camera]: ...

    async def statuses(self, cams: list[Camera]) -> list[CameraStatus]: ...

    async def start(self, cam: Camera) -> CameraStatus: ...

    async def stop(self, cam: Camera) -> CameraStatus: ...

    async def start_all(
        self, cams: list[Camera], session_id: int, storage: Storage
    ) -> list[CameraStatus]: ...

    async def stop_all(
        self, cams: list[Camera], session_id: int, storage: Storage
    ) -> list[CameraStatus]: ...


async def load_cameras_from_storage(storage: Storage) -> list[Camera]:
    """Build :class:`Camera` config objects from the ``cameras`` table rows.

    Pure data assembly — shared by every controller so the web tier never has
    to construct cameras itself.
    """
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


class NullCameraController:
    """Default controller when no camera hardware is wired (tests, headless).

    Lists configured cameras (pure DB read) but performs no network I/O — every
    camera is reported idle and start/stop are no-ops.
    """

    async def load(self, storage: Storage) -> list[Camera]:
        return await load_cameras_from_storage(storage)

    async def statuses(self, cams: list[Camera]) -> list[CameraStatus]:
        return [CameraStatus(name=c.name, ip=c.ip, recording=False) for c in cams]

    async def start(self, cam: Camera) -> CameraStatus:
        return CameraStatus(
            name=cam.name,
            ip=cam.ip,
            recording=False,
            error="no camera controller configured",
        )

    async def stop(self, cam: Camera) -> CameraStatus:
        return CameraStatus(name=cam.name, ip=cam.ip, recording=False)

    async def start_all(
        self, cams: list[Camera], session_id: int, storage: Storage
    ) -> list[CameraStatus]:
        return []

    async def stop_all(
        self, cams: list[Camera], session_id: int, storage: Storage
    ) -> list[CameraStatus]:
        return []
