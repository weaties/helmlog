"""Admin routes for race results import (#459)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

if TYPE_CHECKING:
    from helmlog.storage import Storage

router = APIRouter()


@router.get("/admin/race-results", response_class=HTMLResponse, include_in_schema=False)
async def admin_race_results_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    storage = get_storage(request)
    regattas = await _list_regattas(storage)
    return templates.TemplateResponse(
        request,
        "admin/race_results.html",
        tpl_ctx(request, "/admin/race-results", regattas=regattas),
    )


@router.get("/api/results/regattas", response_class=JSONResponse)
async def api_list_regattas(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    regattas = await _list_regattas(storage)
    return JSONResponse(regattas)


@router.post("/api/results/regattas", response_class=JSONResponse)
async def api_add_regatta(
    request: Request,
    source: str = Form(...),
    source_id: str = Form(...),
    name: str = Form(...),
    url: str = Form(""),
    default_class: str = Form(""),
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    from helmlog.results.base import get_provider

    if not get_provider(source):
        raise HTTPException(400, f"Unknown source: {source!r}")

    import sqlite3
    from datetime import UTC, datetime

    storage = get_storage(request)
    db = storage._conn()

    now = datetime.now(UTC).isoformat()
    try:
        cur = await db.execute(
            "INSERT INTO regattas (source, source_id, name, url, default_class, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, source_id, name, url or None, default_class or None, now),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This regatta has already been added") from None
    await audit(
        request,
        "results_regatta_add",
        detail=json.dumps(
            {
                "regatta_id": cur.lastrowid,
                "source": source,
                "name": name,
            }
        ),
    )
    return JSONResponse({"ok": True, "id": cur.lastrowid})


@router.post("/api/results/regattas/discover", response_class=JSONResponse)
async def api_discover_regatta(
    request: Request,
    url: str = Form(...),
    source: str = Form(""),
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Discover a regatta's metadata from a pasted URL.

    Supports both Clubspot (returns the regatta's class list so the admin
    can pick which to import) and STYC (scrapes the race/series HTML for
    the regatta name).  The source is auto-detected from the URL when not
    supplied explicitly.
    """
    import httpx

    detected = source or _detect_source(url)
    if detected not in ("clubspot", "styc"):
        raise HTTPException(400, f"Cannot detect a known results source in URL {url!r}")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            if detected == "clubspot":
                from helmlog.results.clubspot import ClubspotProvider

                provider = ClubspotProvider(client=client)
                info = await provider.discover_regatta(url)
                return JSONResponse(
                    {
                        "source": "clubspot",
                        "source_id": info.source_id,
                        "name": info.name,
                        "url": info.url,
                        "classes": [{"id": c.id, "name": c.name} for c in info.classes],
                    }
                )

            from helmlog.results.styc import discover_styc_url

            discovery = await discover_styc_url(url, client=client)
            return JSONResponse(
                {
                    "source": "styc",
                    "source_id": discovery.source_id,
                    "name": discovery.name,
                    "url": discovery.url,
                    "classes": [],
                }
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Upstream error: {exc}") from exc


def _detect_source(url: str) -> str:
    """Map a URL to a known results source, or ``""`` if unrecognized."""
    lowered = url.lower()
    if "styc.org" in lowered:
        return "styc"
    if "clubspot" in lowered or "theclubspot" in lowered:
        return "clubspot"
    return ""


@router.delete("/api/results/regattas/{regatta_id}", response_class=JSONResponse)
async def api_delete_regatta(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._conn()
    await db.execute("DELETE FROM series_results WHERE regatta_id = ?", (regatta_id,))
    await db.execute(
        "DELETE FROM race_results WHERE race_id IN (SELECT id FROM races WHERE regatta_id = ?)",
        (regatta_id,),
    )
    await db.execute("DELETE FROM races WHERE regatta_id = ?", (regatta_id,))
    await db.execute("DELETE FROM regattas WHERE id = ?", (regatta_id,))
    await db.commit()
    await audit(
        request,
        "results_regatta_delete",
        detail=json.dumps(
            {
                "regatta_id": regatta_id,
            }
        ),
    )
    return JSONResponse({"ok": True})


@router.post("/api/results/regattas/{regatta_id}/fetch", response_class=JSONResponse)
async def api_fetch_results(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    import httpx

    from helmlog.results.base import Regatta, get_provider
    from helmlog.results.importer import import_results

    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT * FROM regattas WHERE id = ?", (regatta_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Regatta not found")

    provider = get_provider(row["source"])
    if not provider:
        raise HTTPException(400, f"No provider for source {row['source']!r}")

    regatta = Regatta(
        source=row["source"],
        source_id=row["source_id"],
        name=row["name"],
        url=row["url"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        default_class=row["default_class"],
        id=row["id"],
    )

    async with httpx.AsyncClient() as client:
        from helmlog.results.clubspot import ClubspotProvider
        from helmlog.results.styc import StycProvider

        providers_map: dict[str, type[ClubspotProvider] | type[StycProvider]] = {
            "clubspot": ClubspotProvider,
            "styc": StycProvider,
        }
        cls = providers_map.get(row["source"])
        if not cls:
            raise HTTPException(400, f"No provider class for {row['source']!r}")
        provider_instance = cls(client=client)
        results = await provider_instance.fetch(regatta)

    counts = await import_results(storage, results, user_id=_user.get("id"))
    return JSONResponse({"ok": True, **counts})


@router.get("/api/sessions/{session_id}/imported-candidates", response_class=JSONResponse)
async def api_session_imported_candidates(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List imported races that could be linked to this local session.

    Returns imported races (``source IS NOT NULL AND source != 'live'``)
    whose published date is within ±2 days of the local session's date,
    and which are either unlinked or already linked to this session.
    The UI uses this to offer a manual picker when the auto-linker
    missed a match (typically because STYC publishes local dates while
    helmlog stores venue-local dates that can differ by one day for
    evening races).
    """
    storage = get_storage(request)
    db = storage._read_conn()

    cur = await db.execute("SELECT date FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    session_date = row["date"]
    if not session_date:
        return JSONResponse([])

    from datetime import date as _date
    from datetime import timedelta as _td

    try:
        center = _date.fromisoformat(session_date)
    except ValueError:
        return JSONResponse([])

    window = [(center + _td(days=delta)).isoformat() for delta in (-2, -1, 0, 1, 2)]
    placeholders = ",".join("?" * len(window))
    cur = await db.execute(
        f"SELECT r.id, r.name, r.date, r.event AS class_name, r.race_num, "  # noqa: S608
        f"r.regatta_id, r.local_session_id, reg.name AS regatta_name, "
        f"(SELECT COUNT(*) FROM race_results rr WHERE rr.race_id = r.id) AS result_count "
        f"FROM races r LEFT JOIN regattas reg ON reg.id = r.regatta_id "
        f"WHERE r.source IS NOT NULL AND r.source != 'live' "
        f"AND r.date IN ({placeholders}) "
        f"AND (r.local_session_id IS NULL OR r.local_session_id = ?) "
        f"ORDER BY r.date, r.race_num, r.event",
        (*window, session_id),
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.post("/api/sessions/{session_id}/link-imported", response_class=JSONResponse)
async def api_session_link_imported(
    request: Request,
    session_id: int,
    imported_race_id: int = Form(...),
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Link an imported race to this local session so its results show up.

    Passing ``imported_race_id=0`` clears any existing link for races
    previously pointed at this session.
    """
    storage = get_storage(request)
    db = storage._conn()

    cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "Session not found")

    # Collect ids whose cached session_summary depends on the link before
    # mutating: the live session itself, the new imported race, and any
    # imported race already pointing here (whose link is about to be cleared).
    # Without this, /history keeps serving the pre-link results: [] blob.
    invalidation_ids: set[int] = {session_id}
    cur = await db.execute("SELECT id FROM races WHERE local_session_id = ?", (session_id,))
    for row in await cur.fetchall():
        invalidation_ids.add(row[0])

    if imported_race_id == 0:
        await db.execute(
            "UPDATE races SET local_session_id = NULL WHERE local_session_id = ?",
            (session_id,),
        )
        await db.commit()
        for rid in invalidation_ids:
            await storage._invalidate_race_cache(rid)
        await audit(
            request,
            "results_session_unlink",
            detail=json.dumps({"session_id": session_id}),
        )
        return JSONResponse({"ok": True, "linked": 0})

    cur = await db.execute(
        "SELECT id FROM races WHERE id = ? AND source IS NOT NULL AND source != 'live'",
        (imported_race_id,),
    )
    if not await cur.fetchone():
        raise HTTPException(404, "Imported race not found")
    invalidation_ids.add(imported_race_id)

    # Clear any previous link on this session so we don't end up pointing
    # multiple imported rows at the same local session.
    await db.execute(
        "UPDATE races SET local_session_id = NULL WHERE local_session_id = ?",
        (session_id,),
    )
    await db.execute(
        "UPDATE races SET local_session_id = ? WHERE id = ?",
        (session_id, imported_race_id),
    )
    await db.commit()

    for rid in invalidation_ids:
        await storage._invalidate_race_cache(rid)

    await audit(
        request,
        "results_session_link",
        detail=json.dumps({"session_id": session_id, "imported_race_id": imported_race_id}),
    )
    return JSONResponse({"ok": True, "linked": 1})


@router.post("/api/results/regattas/{regatta_id}/rematch", response_class=JSONResponse)
async def api_rematch_regatta(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Re-run local-session matching over an already-imported regatta's races.

    Pairs imported races to local race-type sessions by order within
    each venue-local date.  Re-links existing rows so an admin can fix
    up a regatta that was imported before zip-in-order matching was
    introduced (see #550).
    """
    from helmlog.results.importer import _link_regatta_races_to_local_sessions

    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT id FROM regattas WHERE id = ?", (regatta_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "Regatta not found")

    cur = await db.execute(
        "SELECT COUNT(*) FROM races"
        " WHERE regatta_id = ? AND source IS NOT NULL AND source != 'live'",
        (regatta_id,),
    )
    row = await cur.fetchone()
    races_checked = int(row[0]) if row else 0

    linked, touched_sessions = await _link_regatta_races_to_local_sessions(
        db, regatta_id, force=True
    )

    # Drop cached session_summary blobs for every live session we rewrote
    # the link of (old link and new link) plus the imported race rows
    # themselves — see #666.
    invalidation_ids: set[int] = set(touched_sessions)
    cur = await db.execute(
        "SELECT id, local_session_id FROM races WHERE regatta_id = ?",
        (regatta_id,),
    )
    for row in await cur.fetchall():
        invalidation_ids.add(row[0])
        if row[1] is not None:
            invalidation_ids.add(row[1])

    await db.commit()

    for rid in invalidation_ids:
        await storage._invalidate_race_cache(rid)

    await audit(
        request,
        "results_regatta_rematch",
        detail=json.dumps(
            {"regatta_id": regatta_id, "races_checked": races_checked, "linked": linked}
        ),
    )
    return JSONResponse({"ok": True, "races_checked": races_checked, "linked": linked})


@router.get("/api/results/races", response_class=JSONResponse)
async def api_list_results(
    request: Request,
    regatta_id: int | None = None,
    class_name: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()

    where = []
    params: list[str | int] = []
    if regatta_id is not None:
        where.append("r.regatta_id = ?")
        params.append(regatta_id)
    if class_name:
        where.append("r.event = ?")
        params.append(class_name)

    where_sql = " AND ".join(where) if where else "1=1"

    cur = await db.execute(
        f"SELECT r.id, r.name, r.date, r.event AS class_name, r.race_num, "  # noqa: S608
        f"r.regatta_id, r.local_session_id, "
        f"ls.slug AS session_slug, ls.name AS session_name, "
        f"(SELECT COUNT(*) FROM race_results rr WHERE rr.race_id = r.id) AS result_count "
        f"FROM races r "
        f"LEFT JOIN races ls ON ls.id = r.local_session_id "
        f"WHERE {where_sql} AND r.source IS NOT NULL "
        f"ORDER BY r.date DESC, r.race_num, r.event",
        params,
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/results/races/{race_id}/results", response_class=JSONResponse)
async def api_race_results(
    request: Request,
    race_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT rr.place, rr.points, rr.status_code, rr.finish_time, "
        "rr.elapsed_seconds, rr.corrected_seconds, rr.fleet, "
        "b.sail_number, b.name AS boat_name, b.skipper, b.yacht_club "
        "FROM race_results rr JOIN boats b ON rr.boat_id = b.id "
        "WHERE rr.race_id = ? ORDER BY rr.place",
        (race_id,),
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/results/series", response_class=JSONResponse)
async def api_series_standings(
    request: Request,
    regatta_id: int,
    class_name: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()

    where = ["sr.regatta_id = ?"]
    params: list[str | int] = [regatta_id]
    if class_name:
        where.append("sr.class = ?")
        params.append(class_name)

    where_sql = " AND ".join(where)
    cur = await db.execute(
        f"SELECT sr.*, b.sail_number, b.name AS boat_name, "  # noqa: S608
        f"b.skipper, b.yacht_club "
        f"FROM series_results sr JOIN boats b ON sr.boat_id = b.id "
        f"WHERE {where_sql} "
        f"ORDER BY sr.place_in_class NULLS LAST",
        params,
    )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/results/classes", response_class=JSONResponse)
async def api_list_classes(
    request: Request,
    regatta_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT DISTINCT event FROM races "
        "WHERE regatta_id = ? AND event IS NOT NULL ORDER BY event",
        (regatta_id,),
    )
    rows = await cur.fetchall()
    return JSONResponse([r["event"] for r in rows])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _list_regattas(storage: Storage) -> list[dict[str, Any]]:
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT r.*, "
        "(SELECT COUNT(*) FROM races rc WHERE rc.regatta_id = r.id) AS race_count, "
        "(SELECT COUNT(*) FROM race_results rr "
        "  JOIN races rc2 ON rr.race_id = rc2.id WHERE rc2.regatta_id = r.id) AS result_count "
        "FROM regattas r ORDER BY r.created_at DESC"
    )
    return [dict(row) for row in await cur.fetchall()]
