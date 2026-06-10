"""Route handlers for the instrument timer-PGN audit (#789).

Read-only, admin-only. The page polls /api/pgn-audit/state, which reads the
pgn_audit table (written by the sniffer wired in main.py). The web layer never
touches the CAN bus — see docs/specs/pgn-audit.md.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.pgn_audit import PgnAuditConfig, verdict_from_summary
from helmlog.routes._helpers import get_storage, templates, tpl_ctx

router = APIRouter()


@router.get("/admin/pgn-audit", response_class=HTMLResponse, include_in_schema=False)
async def pgn_audit_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    return templates.TemplateResponse(
        request, "admin/pgn_audit.html", tpl_ctx(request, "/admin/pgn-audit")
    )


@router.get("/api/pgn-audit/state")
async def api_pgn_audit_state(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Per-PGN counts, recent observations, verdict, and the sniffer's on/off
    state — everything the page needs to render in one poll."""
    summary = await get_storage(request).get_pgn_audit_summary()
    return JSONResponse(
        {
            "enabled": PgnAuditConfig.from_env().enabled,
            "verdict": verdict_from_summary(summary),
            "summary": summary,
        }
    )
