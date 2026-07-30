"""Route handlers for cameras."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, load_cameras

router = APIRouter()


@router.get("/api/cameras")
async def api_list_cameras(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List configured cameras with live status."""
    get_storage(request)
    cams = await load_cameras(request)
    if not cams:
        return JSONResponse([])

    statuses = await request.app.state.cameras.statuses(cams)
    result: list[dict[str, Any]] = []
    for cam, st in zip(cams, statuses, strict=True):
        # Mask WiFi passwords in API responses (#210)
        masked_pw = "••••••••" if cam.wifi_password else None
        result.append(
            {
                "name": st.name,
                "ip": st.ip,
                "model": cam.model,
                "wifi_ssid": cam.wifi_ssid,
                "wifi_password": masked_pw,
                "recording": st.recording,
                "error": st.error,
            }
        )
    return JSONResponse(result)


@router.post("/api/cameras/{camera_name}/start")
async def api_start_camera(
    request: Request,
    camera_name: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Manually start recording on a single camera."""
    get_storage(request)
    cams = await load_cameras(request)
    cam = next((c for c in cams if c.name == camera_name), None)
    if cam is None:
        raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
    status = await request.app.state.cameras.start(cam)
    return JSONResponse(
        {
            "name": status.name,
            "ip": status.ip,
            "recording": status.recording,
            "error": status.error,
        }
    )


@router.post("/api/cameras/{camera_name}/stop")
async def api_stop_camera(
    request: Request,
    camera_name: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Manually stop recording on a single camera."""
    get_storage(request)
    cams = await load_cameras(request)
    cam = next((c for c in cams if c.name == camera_name), None)
    if cam is None:
        raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
    status = await request.app.state.cameras.stop(cam)
    return JSONResponse(
        {
            "name": status.name,
            "ip": status.ip,
            "recording": status.recording,
            "error": status.error,
        }
    )


@router.get("/api/cameras/sessions")
async def api_camera_sessions_all(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """List recent camera sessions across all cameras."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute(
        "SELECT cs.id, cs.session_id, cs.camera_name, cs.camera_ip,"
        " cs.recording_started_utc, cs.recording_stopped_utc,"
        " cs.sync_offset_ms, cs.error, r.name AS race_name"
        " FROM camera_sessions cs"
        " JOIN races r ON r.id = cs.session_id"
        " ORDER BY cs.id DESC LIMIT 50",
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/sessions/{session_id}/cameras")
async def api_session_cameras(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """List camera sessions for a specific race."""
    storage = get_storage(request)
    rows = await storage.list_camera_sessions(session_id)
    return JSONResponse(rows)


@router.post("/api/cameras", status_code=201)
async def api_add_camera(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Add a new camera configuration."""
    storage = get_storage(request)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    ip = str(body.get("ip", "")).strip()
    model = str(body.get("model", "insta360-x4")).strip()
    wifi_ssid = str(body.get("wifi_ssid", "")).strip() or None
    wifi_password = str(body.get("wifi_password", "")).strip() or None
    if not name or not ip:
        raise HTTPException(400, detail="name and ip are required")
    try:
        cam_id = await storage.add_camera(name, ip, model, wifi_ssid, wifi_password)
    except Exception:  # noqa: BLE001
        raise HTTPException(409, detail=f"Camera {name!r} already exists") from None
    await audit(request, "camera.add", detail=name, user=_user)
    return JSONResponse(
        {"id": cam_id, "name": name, "ip": ip, "model": model, "wifi_ssid": wifi_ssid},
        status_code=201,
    )


@router.put("/api/cameras/{camera_name}")
async def api_update_camera(
    request: Request,
    camera_name: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Update a camera's IP, model, name, or WiFi credentials."""
    storage = get_storage(request)
    body = await request.json()
    ip = str(body.get("ip", "")).strip()
    model = body.get("model")
    new_name = str(body.get("name", "")).strip()
    wifi_ssid = str(body.get("wifi_ssid", "")).strip() or None
    wifi_password = str(body.get("wifi_password", "")).strip() or None
    if not ip:
        raise HTTPException(400, detail="ip is required")
    if new_name and new_name != camera_name:
        ok = await storage.rename_camera(
            camera_name,
            new_name,
            ip,
            model=model if model else None,
            wifi_ssid=wifi_ssid,
            wifi_password=wifi_password,
        )
    else:
        ok = await storage.update_camera(
            camera_name,
            ip,
            model=model if model else None,
            wifi_ssid=wifi_ssid,
            wifi_password=wifi_password,
        )
    if not ok:
        raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
    await audit(request, "camera.update", detail=camera_name, user=_user)
    return JSONResponse({"name": new_name or camera_name, "ip": ip, "wifi_ssid": wifi_ssid})


@router.delete("/api/cameras/{camera_name}", status_code=204)
async def api_delete_camera(
    request: Request,
    camera_name: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Delete a camera configuration."""
    storage = get_storage(request)
    ok = await storage.delete_camera(camera_name)
    if not ok:
        raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
    await audit(request, "camera.delete", detail=camera_name, user=_user)


@router.get("/api/cameras/status")
async def api_camera_status_crew(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return camera recording status (available to all authenticated users)."""
    get_storage(request)
    cams = await load_cameras(request)
    if not cams:
        return JSONResponse({"recording": False, "cameras": []})
    statuses = await request.app.state.cameras.statuses(cams)
    recording_cams: list[str] = [st.name for st in statuses if st.recording]
    return JSONResponse(
        {
            "recording": bool(recording_cams),
            "cameras": recording_cams,
        }
    )
