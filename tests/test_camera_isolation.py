"""Hardware-isolation guard + DI behaviour for the camera subsystem (#780).

The architecture rule (CLAUDE.md / AGENTS.md): hardware modules
(``sk_reader.py``, ``can_reader.py``, ``cameras.py``) are imported only by
``main.py``.  Web routes must reach camera operations through the
``CameraController`` injected on ``app.state.cameras`` — never by importing
``helmlog.cameras`` directly.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

import helmlog.routes._helpers as helpers_mod
import helmlog.routes.cameras as cameras_route_mod
import helmlog.routes.races as races_route_mod
from helmlog.camera_control import NullCameraController
from helmlog.cameras import Camera, CameraStatus
from helmlog.web import create_app

if TYPE_CHECKING:
    from types import ModuleType

    from fastapi import FastAPI

    from helmlog.camera_control import CameraController
    from helmlog.storage import Storage

_GUARDED_MODULES = [helpers_mod, cameras_route_mod, races_route_mod]


def _imports_helmlog_cameras(source: str) -> bool:
    """True if the module source imports ``helmlog.cameras`` in any form."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "helmlog.cameras" or a.name == "cameras" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module in {"helmlog.cameras", "cameras"}:
                return True
            # `from helmlog import cameras`
            if node.module == "helmlog" and any(a.name == "cameras" for a in node.names):
                return True
    return False


@pytest.mark.parametrize("mod", _GUARDED_MODULES, ids=lambda m: m.__name__)
def test_route_modules_do_not_import_camera_hardware(mod: ModuleType) -> None:
    """Route modules must not import the hardware camera module."""
    source = Path(mod.__file__).read_text()
    assert not _imports_helmlog_cameras(source), (
        f"{mod.__name__} imports helmlog.cameras directly — route the call "
        f"through request.app.state.cameras instead (#780)."
    )


class _FakeController:
    """Records calls and returns canned results, no hardware."""

    def __init__(self, cams: list[Camera]) -> None:
        self._cams = cams
        self.calls: list[tuple[str, str]] = []

    async def load(self, storage: Storage) -> list[Camera]:
        return self._cams

    async def statuses(self, cams: list[Camera]) -> list[CameraStatus]:
        self.calls.append(("statuses", ""))
        return [CameraStatus(name=c.name, ip=c.ip, recording=True) for c in cams]

    async def start(self, cam: Camera) -> CameraStatus:
        self.calls.append(("start", cam.name))
        return CameraStatus(name=cam.name, ip=cam.ip, recording=True)

    async def stop(self, cam: Camera) -> CameraStatus:
        self.calls.append(("stop", cam.name))
        return CameraStatus(name=cam.name, ip=cam.ip, recording=False)


def _app_with_controller(storage: Storage, ctl: CameraController) -> FastAPI:
    app = create_app(storage)
    app.state.cameras = ctl
    return app


@pytest.mark.asyncio
async def test_start_route_delegates_to_controller(storage: Storage) -> None:
    ctl = _FakeController([Camera(name="bow", ip="10.0.0.1")])
    app = _app_with_controller(storage, ctl)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/cameras/bow/start")
    assert resp.status_code == 200
    assert resp.json()["recording"] is True
    assert ("start", "bow") in ctl.calls


@pytest.mark.asyncio
async def test_stop_route_delegates_to_controller(storage: Storage) -> None:
    ctl = _FakeController([Camera(name="bow", ip="10.0.0.1")])
    app = _app_with_controller(storage, ctl)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/cameras/bow/stop")
    assert resp.status_code == 200
    assert resp.json()["recording"] is False
    assert ("stop", "bow") in ctl.calls


@pytest.mark.asyncio
async def test_start_unknown_camera_404(storage: Storage) -> None:
    ctl = _FakeController([Camera(name="bow", ip="10.0.0.1")])
    app = _app_with_controller(storage, ctl)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/cameras/stern/start")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_route_uses_controller_statuses(storage: Storage) -> None:
    ctl = _FakeController([Camera(name="bow", ip="10.0.0.1")])
    app = _app_with_controller(storage, ctl)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/cameras")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "bow"
    assert body[0]["recording"] is True
    assert ("statuses", "") in ctl.calls


@pytest.mark.asyncio
async def test_default_app_uses_null_controller(storage: Storage) -> None:
    """create_app with no controller binds a hardware-free default."""
    app = create_app(storage)
    assert isinstance(app.state.cameras, NullCameraController)


@pytest.mark.asyncio
async def test_null_controller_reports_no_recording(storage: Storage) -> None:
    """NullCameraController loads configured cameras but reports them idle."""
    await storage.add_camera("bow", "10.0.0.1", "insta360-x4", None, None)
    ctl = NullCameraController()
    cams = await ctl.load(storage)
    assert [c.name for c in cams] == ["bow"]
    statuses = await ctl.statuses(cams)
    assert all(not s.recording for s in statuses)
