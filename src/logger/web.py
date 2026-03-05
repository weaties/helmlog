"""FastAPI web interface for race marking.

Provides a mobile-optimised single-page app at http://corvopi:3002 that lets
crew tap a button to start/end races. The app factory pattern (create_app)
keeps this testable without running a live server.

Security:
  Magic-link invite-token auth with three roles: admin, crew, viewer.
  Set AUTH_DISABLED=true to bypass auth entirely (e.g. Tailscale-only deployments).
  See src/logger/auth.py and docs/https-deployment.md for details.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from loguru import logger
from pydantic import BaseModel

if TYPE_CHECKING:
    from logger.audio import AudioConfig, AudioRecorder
    from logger.cameras import Camera
    from logger.storage import Storage

# ---------------------------------------------------------------------------
# Git version info — read once at import time
# ---------------------------------------------------------------------------


def _get_git_info() -> str:
    """Return 'branch @ shortsha · clean/dirty' from the current git repo."""
    import subprocess

    try:
        _repo = str(Path(__file__).resolve().parents[2])
        _git = ["git", "-c", f"safe.directory={_repo}"]

        def _run(args: list[str]) -> str:
            return subprocess.check_output(
                [*_git, *args], cwd=_repo, stderr=subprocess.DEVNULL, text=True
            ).strip()

        branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])
        sha = _run(["rev-parse", "--short=7", "HEAD"])

        # Check for uncommitted changes
        dirty = bool(_run(["status", "--porcelain"]))

        # Check for unpushed commits (best-effort; skip if no upstream)
        if not dirty:
            try:
                unpushed = _run(["rev-list", "@{upstream}..HEAD", "--count"])
                if int(unpushed) > 0:
                    dirty = True
            except Exception:  # noqa: BLE001
                pass  # no upstream configured

        status = "dirty" if dirty else "clean"
        return f"{branch} @ {sha} · {status}"
    except Exception:  # noqa: BLE001
        return ""


_GIT_INFO: str = _get_git_info()

# ---------------------------------------------------------------------------
# Shared nav bar + footer
# ---------------------------------------------------------------------------

_NAV_CSS = """\
nav.site-nav{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:8px 0;margin-bottom:8px;border-bottom:1px solid #1e3a5f}
nav.site-nav a{color:#8892a4;text-decoration:none;font-size:.82rem;padding:4px 8px;border-radius:6px}
nav.site-nav a:hover{color:#e8eaf0;background:#1e3a5f}
nav.site-nav a.active{color:#7eb8f7;font-weight:600}
nav.site-nav .spacer{flex:1}
nav.site-nav .admin-link{display:none}
nav.site-nav .profile-link{display:none}
"""


def _nav_html(current: str = "/") -> str:
    """Return the shared navigation bar HTML.

    *current* is the path of the active page (e.g. "/", "/history").
    Admin-only links are hidden by default and revealed via JS /api/me check.
    """

    def _cls(path: str) -> str:
        return ' class="active"' if path == current else ""

    return f"""\
<nav class="site-nav" id="site-nav">
  <a href="/"{_cls("/")}>Home</a>
  <a href="/history"{_cls("/history")}>History</a>
  <a href="/admin/boats"{_cls("/admin/boats")}>Boats</a>
  <a href="/admin/users" class="admin-link{" active" if current == "/admin/users" else ""}">Users</a>
  <a href="/admin/audit" class="admin-link{" active" if current == "/admin/audit" else ""}">Audit</a>
  <a href="/admin/cameras" class="admin-link{" active" if current == "/admin/cameras" else ""}">Cameras</a>
  <span class="spacer"></span>
  <a href="/profile" class="profile-link" id="nav-profile"{_cls("/profile")}>\
<img id="nav-avatar" src="" alt="" \
style="width:18px;height:18px;border-radius:50%;vertical-align:middle;margin-right:3px">\
<span id="nav-profile-name">Profile</span></a>
</nav>
<script>
fetch('/api/me').then(r=>r.json()).then(u=>{{
  if(u.role==='admin')document.querySelectorAll('.admin-link').forEach(el=>el.style.setProperty('display','inline','important'));
  if(u.id){{const p=document.getElementById('nav-profile');if(p){{p.style.display='';document.getElementById('nav-avatar').src='/avatars/'+u.id+'.jpg';document.getElementById('nav-profile-name').textContent=u.name||'Profile';}}}}
}}).catch(()=>{{}});
</script>"""


_FOOTER_HTML = (
    '<footer style="text-align:center;padding:12px 0 8px;font-size:.7rem;color:#4a5568">'
    "__GIT_INFO__</footer>"
)

# ---------------------------------------------------------------------------
# HTML — inline mobile-first single-page app
# ---------------------------------------------------------------------------

_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>J105 Logger</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;font-size:clamp(.85rem,2vw,1rem)}
__NAV_CSS__
.page{max-width:480px;margin:0 auto;padding:16px 12px}
@media(min-width:768px){.page{max-width:720px}}
@media(min-width:1200px){.page{max-width:900px}}
@media(min-width:768px){#controls{display:grid;grid-template-columns:1fr 1fr;gap:8px}#btn-end{grid-column:1/-1}}
h1{font-size:1.3rem;font-weight:700;color:#7eb8f7;margin-bottom:2px}
.sub{font-size:.9rem;color:#8892a4;margin-bottom:20px}
.card{background:#131f35;border-radius:12px;padding:16px;margin-bottom:16px}
.race-name{font-size:1rem;font-weight:600;color:#e8eaf0;margin-bottom:4px}
.race-meta{font-size:.8rem;color:#8892a4}
.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;
background:#22c55e;margin-right:6px;animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#8892a4;margin-bottom:8px}
.duration{font-size:1.6rem;font-weight:700;color:#22c55e;font-variant-numeric:tabular-nums}
.btn{display:block;width:100%;padding:18px;border:none;border-radius:10px;font-size:1.1rem;font-weight:700;cursor:pointer;margin-bottom:10px;letter-spacing:.02em}
.btn-primary{background:#2563eb;color:#fff}
.btn-primary:active{background:#1d4ed8}
.btn-secondary{background:#1e3a5f;color:#7eb8f7;border:1px solid #2563eb}
.btn-secondary:active{background:#163252}
.btn-danger{background:#7f1d1d;color:#fca5a5;border:1px solid #dc2626}
.btn-practice{background:#1a3a2a;color:#4ade80;border:1px solid #16a34a}
.btn-practice:active{background:#14532d}
.btn-debrief{background:#2d1b4e;color:#c084fc;border:1px solid #7c3aed}
.btn-debrief:active{background:#1e1236}
.btn-note{background:#1a3a4e;color:#7eb8f7;border:1px solid #2563eb}
.btn-note:active{background:#163252}
.badge{font-size:.7rem;padding:1px 6px;border-radius:3px;margin-left:4px;vertical-align:middle}
.badge-race{background:#1e3a5f;color:#7eb8f7}
.badge-practice{background:#14532d;color:#4ade80}
.event-row{display:flex;gap:8px;margin-bottom:16px}
.event-input{flex:1;background:#0a1628;border:1px solid #2563eb;
border-radius:8px;padding:12px;color:#e8eaf0;font-size:1rem}
.btn-save{padding:12px 18px;border:none;border-radius:8px;background:#2563eb;
color:#fff;font-weight:700;cursor:pointer;font-size:1rem}
.race-list{margin-top:8px}
.race-item{padding:10px 0;border-bottom:1px solid #1e3a5f}
.race-item:last-child{border-bottom:none}
.race-item-name{font-weight:600;font-size:.9rem;margin-bottom:4px}
.race-item-time{font-size:.8rem;color:#8892a4}
.race-exports{margin-top:6px;display:flex;gap:8px}
.btn-export{padding:8px 12px;border:1px solid #2563eb;border-radius:6px;
background:#131f35;color:#7eb8f7;font-size:.8rem;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center}
.btn-grafana{border-color:#b45309;color:#fbbf24}
.hidden{display:none}
.instruments-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 8px;margin-top:6px}
.inst-stale .inst-value{color:#4a5568}
.inst-item{display:flex;flex-direction:column}
.inst-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:#8892a4}
.inst-value{font-size:1.3rem;font-weight:700;color:#7eb8f7;font-variant-numeric:tabular-nums}
.inst-unit{font-size:.75rem;color:#8892a4;margin-left:2px}
.polar-delta-pos{color:#22c55e}
.polar-delta-neg{color:#f87171}
.inst-time{font-size:1rem;font-weight:600;color:#e8eaf0;font-variant-numeric:tabular-nums;margin-bottom:8px}
.crew-header{display:flex;align-items:center;justify-content:space-between;cursor:pointer;-webkit-user-select:none;user-select:none}
.crew-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.crew-pos{min-width:48px;font-size:.8rem;color:#8892a4;text-transform:uppercase;letter-spacing:.06em}
.crew-input{flex:1;background:#0a1628;border:1px solid #2563eb;border-radius:6px;padding:8px 10px;color:#e8eaf0;font-size:.9rem}
.sailor-chip{padding:6px 12px;border:1px solid #2563eb;border-radius:16px;background:#0a1628;color:#7eb8f7;font-size:.82rem;cursor:pointer;white-space:nowrap;-webkit-tap-highlight-color:transparent}
.sailor-chip:active{background:#1e3a5f}
.race-item-crew{font-size:.75rem;color:#8892a4;margin-top:2px}
.results-section{margin-top:8px;border-top:1px solid #1e3a5f;padding-top:6px}
.results-header{display:flex;align-items:center;gap:6px;cursor:pointer;-webkit-user-select:none;user-select:none;font-size:.8rem;color:#8892a4}
.results-header:active{opacity:.7}
.results-row{display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid #0d1a2e}
.results-row:last-child{border-bottom:none}
.results-place{min-width:22px;font-size:.82rem;font-weight:700;color:#7eb8f7}
.results-boat{flex:1;font-size:.82rem}
.flag-btn{padding:2px 7px;border:1px solid #374151;border-radius:4px;background:#0a1628;color:#8892a4;font-size:.72rem;cursor:pointer}
.flag-btn.active-dnf{background:#7f1d1d;color:#fca5a5;border-color:#dc2626}
.flag-btn.active-dns{background:#1c1f2e;color:#818cf8;border-color:#4338ca}
.btn-del-result{padding:2px 7px;border:1px solid #374151;border-radius:4px;background:#0a1628;color:#ef4444;font-size:.72rem;cursor:pointer}
.boat-picker-input{width:100%;background:#0a1628;border:1px solid #374151;border-radius:6px;padding:6px 9px;color:#e8eaf0;font-size:.82rem}
.boat-dropdown{position:absolute;top:calc(100% + 2px);left:0;right:0;background:#131f35;border:1px solid #2563eb;border-radius:6px;max-height:190px;overflow-y:auto;z-index:200;box-shadow:0 4px 14px rgba(0,0,0,.6)}
.boat-option{padding:8px 12px;font-size:.82rem;cursor:pointer;border-bottom:1px solid #1e3a5f}
.boat-option:last-child{border-bottom:none}
.boat-option:active{background:#1e3a5f}
.boat-option-new{color:#4ade80}
.note-tab{padding:5px 12px;border:1px solid #2563eb;border-radius:6px;background:#131f35;color:#8892a4;font-size:.8rem;cursor:pointer}
.note-tab.active{background:#2563eb;color:#fff}
.field{background:#0a1628;border:1px solid #2563eb;border-radius:6px;color:#e8eaf0}
</style>
</head>
<body>
<div class="page">
<div id="health-banner" style="display:none;background:#7f1d1d;color:#fca5a5;border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:.85rem"></div>
__NAV__
<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:2px">
  <h1>J105 Logger</h1>
  <div style="display:flex;gap:6px;margin-top:2px">
    <a id="grafana-nav" class="btn-export btn-grafana" href="#" target="_blank">📊 Grafana</a>
    <a id="signalk-nav" class="btn-export" href="#" target="_blank" style="display:none">⚙ Signal K</a>
  </div>
</div>
<div class="sub" id="header-sub">Loading…</div>

<div id="event-section" class="hidden">
  <div class="label">Event name</div>
  <div class="event-row">
    <input id="event-input" class="event-input" placeholder="e.g. Regatta" maxlength="40"/>
    <button class="btn-save" onclick="saveEvent()">Save</button>
  </div>
</div>

<div id="current-card" class="card hidden">
  <div class="label"><span class="status-dot"></span>Race in progress</div>
  <div class="race-name" id="cur-name">—</div>
  <div class="race-meta" id="cur-meta">—</div>
  <div class="label" style="margin-top:12px">Duration</div>
  <div class="duration" id="cur-duration">—</div>
  <button class="btn btn-note" id="btn-note" onclick="toggleNotePanel()" style="margin-top:10px;display:none">+ Note</button>
  <div id="note-panel" style="display:none;margin-top:8px">
    <div style="display:flex;gap:4px;margin-bottom:8px">
      <button class="note-tab active" id="note-tab-text"     onclick="selectNoteType('text')">Text</button>
      <button class="note-tab"        id="note-tab-settings" onclick="selectNoteType('settings')">Settings</button>
      <button class="note-tab"        id="note-tab-photo"    onclick="selectNoteType('photo')">Photo</button>
    </div>
    <div id="note-pane-text">
      <textarea id="note-body" rows="3"
        style="width:100%;background:#0a1628;border:1px solid #2563eb;border-radius:6px;
               padding:8px;color:#e8eaf0;font-size:.9rem;resize:vertical"
        placeholder="Race observation…"></textarea>
    </div>
    <div id="note-pane-settings" style="display:none">
      <datalist id="settings-key-suggestions"></datalist>
      <div id="settings-rows"></div>
      <button onclick="addSettingsRow()" style="font-size:.8rem;color:#7eb8f7;background:none;border:none;cursor:pointer;padding:4px 0">+ Add field</button>
    </div>
    <div id="note-pane-photo" style="display:none;text-align:center">
      <input type="file" id="photo-file" accept="image/*,video/*" capture="environment"
        style="display:none" onchange="onPhotoSelected(this)"/>
      <button class="btn btn-secondary" style="width:100%" onclick="document.getElementById('photo-file').click()">📷 Take Photo / Choose File</button>
      <div id="photo-preview" style="margin-top:8px"></div>
    </div>
    <button class="btn btn-primary" style="margin-top:8px;font-size:.9rem;padding:10px;width:100%"
      onclick="saveNote()">Save Note</button>
  </div>
</div>

<div id="debrief-card" class="card hidden">
  <div class="label">
    <span class="status-dot" style="background:#c084fc"></span>Debrief in progress
  </div>
  <div class="race-name" id="debrief-name">—</div>
  <div class="label" style="margin-top:12px">Duration</div>
  <div class="duration" id="debrief-duration" style="color:#c084fc">—</div>
  <button class="btn btn-danger" style="margin-top:12px" onclick="stopDebrief()">⏹ STOP DEBRIEF</button>
</div>

<div class="card" id="instruments-card">
  <div class="crew-header" onclick="toggleInstruments()">
    <span class="label" style="margin-bottom:0">Instruments</span>
    <span style="display:flex;align-items:center;gap:8px">
      <span class="inst-time" id="inst-time" style="margin-bottom:0">--:--:-- UTC</span>
      <span id="inst-chevron" style="color:#8892a4;font-size:.85rem">▶</span>
    </span>
  </div>
  <div id="inst-body" style="display:none">
  <div class="instruments-grid" id="inst-grid">
    <div class="inst-item"><span class="inst-label">BSP</span>
      <span><span class="inst-value" id="iv-bsp">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">TWS</span>
      <span><span class="inst-value" id="iv-tws">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">TWA</span>
      <span><span class="inst-value" id="iv-twa">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">HDG</span>
      <span><span class="inst-value" id="iv-hdg">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">COG</span>
      <span><span class="inst-value" id="iv-cog">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">SOG</span>
      <span><span class="inst-value" id="iv-sog">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">AWS</span>
      <span><span class="inst-value" id="iv-aws">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">AWA</span>
      <span><span class="inst-value" id="iv-awa">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">TWD</span>
      <span><span class="inst-value" id="iv-twd">—</span><span class="inst-unit">°</span></span></div>
  </div>
  </div>
</div>

<div class="card" id="crew-card">
  <div class="crew-header" onclick="toggleCrew()">
    <span class="label" style="margin-bottom:0">Crew</span>
    <span id="crew-chevron" style="color:#8892a4;font-size:.85rem">▶</span>
  </div>
  <div id="crew-body" style="display:none;margin-top:10px">
    <div class="crew-row"><span class="crew-pos">Helm</span><input class="crew-input" id="crew-helm" list="recent-sailors" placeholder="Name…" maxlength="40"/></div>
    <div class="crew-row"><span class="crew-pos">Main</span><input class="crew-input" id="crew-main" list="recent-sailors" placeholder="Name…" maxlength="40"/></div>
    <div class="crew-row"><span class="crew-pos">Pit</span><input class="crew-input" id="crew-pit" list="recent-sailors" placeholder="Name…" maxlength="40"/></div>
    <div class="crew-row"><span class="crew-pos">Bow</span><input class="crew-input" id="crew-bow" list="recent-sailors" placeholder="Name…" maxlength="40"/></div>
    <div class="crew-row"><span class="crew-pos">Tac</span><input class="crew-input" id="crew-tac" list="recent-sailors" placeholder="Name…" maxlength="40"/></div>
    <div class="crew-row"><span class="crew-pos">Guest</span><input class="crew-input" id="crew-guest" list="recent-sailors" placeholder="Name…" maxlength="40"/></div>
    <datalist id="recent-sailors"></datalist>
    <div id="sailor-chips" style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 4px"></div>
    <button class="btn btn-secondary" style="margin-top:6px;font-size:.9rem;padding:12px" onclick="saveCrew()">Save Crew</button>
  </div>
</div>

<div id="controls">
  <button class="btn btn-primary"  id="btn-start-race"     onclick="startSession('race')">▶ START RACE 1</button>
  <button class="btn btn-practice" id="btn-start-practice" onclick="startSession('practice')">▶ START PRACTICE</button>
  <button class="btn btn-secondary hidden" id="btn-end" onclick="endRace()">■ END RACE</button>
</div>

<div class="card" id="history-card" style="display:none">
  <div class="label">Today's races</div>
  <div class="race-list" id="race-list"></div>
</div>

<div class="card" id="sails-inventory-card">
  <div class="crew-header" onclick="toggleSailInventory()">
    <span class="label" style="margin-bottom:0">⛵ Sail Inventory</span>
    <span id="sails-inventory-chevron" style="color:#8892a4;font-size:.85rem">▶</span>
  </div>
  <div id="sails-inventory-body" style="display:none;margin-top:10px">
    <div id="sail-inventory-list"></div>
    <div style="margin-top:8px;padding-top:8px;border-top:1px solid #1e3a5f">
      <div style="font-size:.78rem;color:#8892a4;margin-bottom:6px">Add sail</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <select id="new-sail-type" style="background:#1a2840;color:#e0e8f0;border:1px solid #2563eb;border-radius:4px;padding:5px 8px;font-size:.82rem">
          <option value="main">Main</option>
          <option value="jib">Jib</option>
          <option value="spinnaker">Spinnaker</option>
        </select>
        <input id="new-sail-name" class="field" placeholder="Sail name" maxlength="60" style="flex:1;padding:5px 8px;font-size:.82rem"/>
        <button class="btn btn-primary" style="font-size:.82rem;padding:5px 12px" onclick="addSail()">Add</button>
      </div>
    </div>
  </div>
</div>

<div class="card" id="polar-card">
  <div class="label">Boatspeed vs Baseline</div>
  <div class="instruments-grid" style="margin-top:6px">
    <div class="inst-item"><span class="inst-label">BSP</span>
      <span><span class="inst-value" id="pv-bsp">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">Baseline</span>
      <span><span class="inst-value" id="pv-baseline">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">Delta</span>
      <span><span class="inst-value" id="pv-delta">—</span><span class="inst-unit">kts</span></span></div>
  </div>
  <div id="polar-no-data" style="display:none;font-size:.78rem;color:#8892a4;margin-top:4px">
    No baseline data for this wind condition (need ≥3 sessions)
  </div>
</div>

<script>
const _GRAFANA_PORT = '__GRAFANA_PORT__';
const _GRAFANA_UID = '__GRAFANA_UID__';
const _SK_PORT = '__SK_PORT__';
// When served behind a reverse proxy (default HTTPS/HTTP port), use path-based
// routing (/grafana/, /signalk/).  On the LAN use direct hostname:port.
const _isDefaultPort = !location.port || location.port === '443' || location.port === '80';
const GRAFANA_BASE = _isDefaultPort
  ? location.origin + '/grafana'
  : location.protocol + '//' + location.hostname + ':' + _GRAFANA_PORT;
const SK_BASE = _isDefaultPort
  ? location.origin + '/signalk'
  : location.protocol + '//' + location.hostname + ':' + _SK_PORT;
(function initNavLinks() {
  const g = document.getElementById('grafana-nav');
  if (g) g.href = GRAFANA_BASE + '/d/' + _GRAFANA_UID + '/sailing-data?refresh=10s';
  const s = document.getElementById('signalk-nav');
  if (s) { s.href = SK_BASE; s.style.display = ''; }
})();
let state = null;
let tickInterval = null;
let curRaceStartMs = null;
let debriefStartMs = null;
let lastInstrumentDataMs = 0;

async function loadState() {
  try {
    const r = await fetch('/api/state');
    state = await r.json();
    render(state);
  } catch(e) { console.error('state error', e); }
}

function fmt(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
  if(h) return `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
  return `${m}:${String(ss).padStart(2,'0')}`;
}

let _tz = 'UTC';
function fmtTime(iso) {
  if(!iso) return '—';
  try {
    return new Date(iso).toLocaleTimeString('en-US',{timeZone:_tz,hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  } catch(e) { return new Date(iso).toISOString().substring(11,19) + ' UTC'; }
}

function render(s) {
  if(s.timezone) _tz = s.timezone;
  document.getElementById('header-sub').textContent =
    `${s.weekday} · ${s.event || '(no event)'}`;

  const evSec = document.getElementById('event-section');
  if(!s.event_is_default) {
    evSec.classList.remove('hidden');
    if(!document.getElementById('event-input').value && s.event) {
      document.getElementById('event-input').value = s.event;
    }
  } else {
    evSec.classList.add('hidden');
  }

  const cur = s.current_race;
  const curCard = document.getElementById('current-card');
  const btnEnd = document.getElementById('btn-end');
  const btnStartRace = document.getElementById('btn-start-race');
  const btnStartPractice = document.getElementById('btn-start-practice');

  if(cur) {
    curCard.classList.remove('hidden');
    btnEnd.classList.remove('hidden');
    btnStartRace.classList.add('hidden');
    btnStartPractice.classList.add('hidden');
    document.getElementById('cur-name').textContent = cur.name;
    document.getElementById('cur-meta').textContent =
      'Started ' + fmtTime(cur.start_utc);
    curRaceStartMs = new Date(cur.start_utc).getTime();
    btnEnd.textContent = '■ END ' + cur.name;
    if(cur.id !== _crewLoadedForRaceId) {
      setCrewInputs(cur.crew || []);
      _crewLoadedForRaceId = cur.id;
    }
    document.getElementById('btn-note').style.display = '';
  } else {
    curCard.classList.add('hidden');
    btnEnd.classList.add('hidden');
    btnStartRace.classList.remove('hidden');
    btnStartPractice.classList.remove('hidden');
    curRaceStartMs = null;
    _crewLoadedForRaceId = null;
    clearInterval(tickInterval);
    document.getElementById('btn-note').style.display = 'none';
    document.getElementById('note-panel').style.display = 'none';
  }

  const debriefCard = document.getElementById('debrief-card');
  if(s.current_debrief) {
    debriefCard.classList.remove('hidden');
    document.getElementById('debrief-name').textContent = s.current_debrief.race_name + ' — debrief';
    debriefStartMs = new Date(s.current_debrief.start_utc).getTime();
  } else {
    debriefCard.classList.add('hidden');
    debriefStartMs = null;
  }

  btnStartRace.textContent = `▶ START RACE ${s.next_race_num}`;

  const hist = document.getElementById('history-card');
  const list = document.getElementById('race-list');
  if(s.today_races && s.today_races.length) {
    hist.style.display = '';
    // Don't re-render while the user has focus inside the race list (e.g.
    // typing in the boat picker). Replacing innerHTML destroys the active
    // element and fires onblur, which closes the picker prematurely (#36).
    // Also skip if any expandable panel is open — re-rendering wipes the form.
    const anyPanelOpen = [...list.querySelectorAll('[id^="videos-list-"],[id^="notes-list-"]')]
      .some(el => el.style.display !== 'none');
    if (list.contains(document.activeElement) || anyPanelOpen) return;
    list.innerHTML = s.today_races.slice().reverse().map(r => {
      const start = fmtTime(r.start_utc);
      const end = r.end_utc ? fmtTime(r.end_utc) : 'in progress';
      const dur = (r.end_utc && r.duration_s != null)
        ? ` (${fmt(Math.round(r.duration_s))})` : '';
      const badge = r.session_type === 'practice'
        ? '<span class="badge badge-practice">PRACTICE</span>'
        : '<span class="badge badge-race">RACE</span>';
      const from = new Date(r.start_utc).getTime();
      const to   = r.end_utc ? new Date(r.end_utc).getTime() : 'now';
      const refresh = r.end_utc ? 'refresh=' : 'refresh=10s';
      const grafanaBtn = `<a class="btn-export btn-grafana" href="${GRAFANA_BASE}/d/${_GRAFANA_UID}/sailing-data?from=${from}&to=${to}&orgId=1&${refresh}" target="_blank">📊 ${r.end_utc ? 'Grafana' : 'Live'}</a>`;
      const debriefBtn = (r.end_utc && s.has_recorder && !s.current_debrief && !s.current_race)
        ? `<button class="btn-export btn-debrief" onclick="startDebrief(${r.id})">🎙 Debrief</button>`
        : '';
      const wavBtn = (r.end_utc && r.has_audio && r.audio_session_id)
        ? `<a class="btn-export" href="/api/audio/${r.audio_session_id}/download">&#8595; WAV</a>`
        : '';
      const exports = r.end_utc
        ? `<div class="race-exports">
             <a class="btn-export" href="/api/races/${r.id}/export.csv">↓ CSV</a>
             <a class="btn-export" href="/api/races/${r.id}/export.gpx">↓ GPX</a>
             ${grafanaBtn}
             ${wavBtn}
             ${debriefBtn}
           </div>`
        : `<div class="race-exports">${grafanaBtn}</div>`;
      const crewLine = r.crew && r.crew.length
        ? r.crew.map(c => c.position.charAt(0).toUpperCase() + c.position.slice(1) + ': ' + c.sailor).join(' · ')
        : '';
      const crewHtml = crewLine ? `<div class="race-item-crew">${crewLine}</div>` : '';
      const resultsHtml = renderResultsSection(r);
      const notesHtml = r.end_utc
        ? '<div style="margin-top:4px;border-top:1px solid #1e3a5f;padding-top:4px">'
          + '<span style="font-size:.78rem;color:#8892a4;cursor:pointer" '
          + 'onclick="toggleNotes(' + r.id + ')">Notes ▶</span>'
          + '<div id="notes-list-' + r.id + '" style="display:none;margin-top:4px"></div>'
          + '</div>'
        : '';
      const videosHtml = r.end_utc
        ? '<div style="margin-top:4px;border-top:1px solid #1e3a5f;padding-top:4px">'
          + '<span style="font-size:.78rem;color:#8892a4;cursor:pointer" '
          + 'onclick="toggleVideos(' + r.id + ')">🎬 Videos ▶</span>'
          + '<div id="videos-list-' + r.id + '" data-start-utc="' + r.start_utc + '" style="display:none;margin-top:4px"></div>'
          + '</div>'
        : '';
      const sailsHtml = r.end_utc
        ? '<div style="margin-top:4px;border-top:1px solid #1e3a5f;padding-top:4px">'
          + '<span style="font-size:.78rem;color:#8892a4;cursor:pointer" '
          + 'onclick="toggleSails(' + r.id + ')">⛵ Sails ▶</span>'
          + '<div id="sails-list-' + r.id + '" style="display:none;margin-top:4px"></div>'
          + '</div>'
        : '';
      return `<div class="race-item">
        <div class="race-item-name">${r.name}${badge}</div>
        <div class="race-item-time">${start} → ${end}${dur}</div>
        ${crewHtml}
        ${resultsHtml}
        ${notesHtml}
        ${videosHtml}
        ${sailsHtml}
        ${exports}
      </div>`;
    }).join('');
  } else {
    hist.style.display = 'none';
  }
}

function tick() {
  const now = new Date();
  document.getElementById('inst-time').textContent =
    now.toISOString().substring(11,19) + ' UTC';
  if(curRaceStartMs) {
    const elapsed = Math.floor((Date.now() - curRaceStartMs) / 1000);
    document.getElementById('cur-duration').textContent = fmt(elapsed);
  }
  if(debriefStartMs) {
    const elapsed = Math.floor((Date.now() - debriefStartMs) / 1000);
    document.getElementById('debrief-duration').textContent = fmt(elapsed);
  }
  const grid = document.getElementById('inst-grid');
  if (grid) {
    grid.classList.toggle('inst-stale',
      lastInstrumentDataMs > 0 && Date.now() - lastInstrumentDataMs > 5000);
  }
}

async function loadInstruments() {
  try {
    const r = await fetch('/api/instruments');
    const d = await r.json();
    const set = (id, val, decimals=1) => {
      const el = document.getElementById(id);
      el.textContent = val != null ? Number(val).toFixed(decimals) : '—';
    };
    set('iv-sog', d.sog_kts, 1);
    set('iv-cog', d.cog_deg, 0);
    set('iv-hdg', d.heading_deg, 0);
    set('iv-bsp', d.bsp_kts, 1);
    set('iv-aws', d.aws_kts, 1);
    set('iv-awa', d.awa_deg, 0);
    set('iv-tws', d.tws_kts, 1);
    set('iv-twa', d.twa_deg, 0);
    set('iv-twd', d.twd_deg, 0);
    if (Object.values(d).some(v => v != null)) {
      lastInstrumentDataMs = Date.now();
    }
  } catch(e) { console.error('instruments error', e); }
}

let pendingCrew = null;
let crewExpanded = false;
let focusedCrewInput = null;
let _crewLoadedForRaceId = null;

let instExpanded = false;

function toggleInstruments() {
  instExpanded = !instExpanded;
  document.getElementById('inst-body').style.display = instExpanded ? '' : 'none';
  document.getElementById('inst-chevron').textContent = instExpanded ? '▼' : '▶';
}

function toggleCrew() {
  crewExpanded = !crewExpanded;
  document.getElementById('crew-body').style.display = crewExpanded ? '' : 'none';
  document.getElementById('crew-chevron').textContent = crewExpanded ? '▼' : '▶';
}

function getCrewFromInputs() {
  const positions = ['helm','main','pit','bow','tactician','guest'];
  const ids = ['crew-helm','crew-main','crew-pit','crew-bow','crew-tac','crew-guest'];
  const crew = [];
  positions.forEach((pos, i) => {
    const val = document.getElementById(ids[i]).value.trim();
    if(val) crew.push({position: pos, sailor: val});
  });
  return crew;
}

function setCrewInputs(crew) {
  const posToId = {helm:'crew-helm',main:'crew-main',pit:'crew-pit',bow:'crew-bow',tactician:'crew-tac',guest:'crew-guest'};
  Object.values(posToId).forEach(id => { document.getElementById(id).value = ''; });
  if(crew) crew.forEach(c => {
    const id = posToId[c.position];
    if(id) document.getElementById(id).value = c.sailor;
  });
}

function tapSailor(name) {
  let target = focusedCrewInput;
  if(!target) {
    const inputs = [...document.querySelectorAll('.crew-input')];
    target = inputs.find(i => !i.value.trim()) || inputs[0];
  }
  if(!target) return;
  target.value = name;
  const inputs = [...document.querySelectorAll('.crew-input')];
  const idx = inputs.indexOf(target);
  const nextEmpty = inputs.slice(idx + 1).find(i => !i.value.trim());
  if(nextEmpty) { nextEmpty.focus(); focusedCrewInput = nextEmpty; }
}

async function loadRecentSailors() {
  try {
    const r = await fetch('/api/sailors/recent');
    const d = await r.json();
    const dl = document.getElementById('recent-sailors');
    dl.innerHTML = d.sailors.map(s => '<option value="' + s.replace(/&/g,'&amp;').replace(/"/g,'&quot;') + '">').join('');
    const chips = document.getElementById('sailor-chips');
    chips.innerHTML = d.sailors.map(s => {
      const display = s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const attr = s.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
      return '<button class="sailor-chip" onpointerdown="event.preventDefault()" onclick="tapSailor(this.dataset.name)" data-name="' + attr + '">' + display + '</button>';
    }).join('');
  } catch(e) { console.error('sailors error', e); }
}

async function saveCrew() {
  const crew = getCrewFromInputs();
  if(state && state.current_race) {
    await fetch('/api/races/' + state.current_race.id + '/crew', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(crew)
    });
    await loadRecentSailors();
  } else {
    pendingCrew = crew;
  }
}

async function startSession(type) {
  const resp = await fetch(`/api/races/start?session_type=${type}`, {method:'POST'});
  if(resp.ok) {
    const data = await resp.json();
    const crew = pendingCrew && pendingCrew.length ? pendingCrew : getCrewFromInputs();
    if(crew.length && data.id) {
      await fetch('/api/races/' + data.id + '/crew', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify(crew)
      });
      await loadRecentSailors();
    }
    pendingCrew = null;
  }
  await loadState();
  clearInterval(tickInterval);
  if(curRaceStartMs) tickInterval = setInterval(tick, 1000);
}

async function endRace() {
  if(!state || !state.current_race) return;
  await fetch(`/api/races/${state.current_race.id}/end`, {method:'POST'});
  await loadState();
}

async function startDebrief(raceId) {
  await fetch(`/api/races/${raceId}/debrief/start`, {method: 'POST'});
  await loadState();
}

async function stopDebrief() {
  await fetch('/api/debrief/stop', {method: 'POST'});
  await loadState();
}

async function saveEvent() {
  const name = document.getElementById('event-input').value.trim();
  if(!name) return;
  await fetch('/api/event', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({event_name: name})
  });
  await loadState();
}

// ---- Race results ----
const expandedResults = {};
const _pickerBoats = {};

function renderResultRow(res, raceId) {
  const name = res.boat_name
    ? res.sail_number + ' <span style="color:#8892a4;font-size:.78rem">' + res.boat_name + '</span>'
    : res.sail_number;
  const dnfCls = res.dnf ? ' active-dnf' : '';
  const dnsCls = res.dns ? ' active-dns' : '';
  return '<div class="results-row">'
    + '<span class="results-place">' + res.place + '.</span>'
    + '<span class="results-boat">' + name + '</span>'
    + '<div class="results-flags">'
    + '<button class="flag-btn' + dnfCls + '" onmousedown="event.preventDefault()" onclick="toggleResultFlag(' + raceId + ',' + res.place + ',' + res.boat_id + ',' + (!res.dnf) + ',' + res.dns + ')">DNF</button>'
    + '<button class="flag-btn' + dnsCls + '" onmousedown="event.preventDefault()" onclick="toggleResultFlag(' + raceId + ',' + res.place + ',' + res.boat_id + ',' + res.dnf + ',' + (!res.dns) + ')">DNS</button>'
    + '</div>'
    + '<button class="btn-del-result" onmousedown="event.preventDefault()" onclick="deleteResult(' + raceId + ',' + res.id + ')">✕</button>'
    + '</div>';
}

function renderResultsSection(race) {
  const results = race.results || [];
  const summary = results.length
    ? results.slice(0,3).map(r => r.place + '. ' + r.sail_number).join(' · ') + (results.length > 3 ? ' +' + (results.length-3) + ' more' : '')
    : 'No results yet';
  const rows = results.map(r => renderResultRow(r, race.id)).join('');
  return '<div class="results-section">'
    + '<div class="results-header" onclick="toggleResults(' + race.id + ')">'
    + '<span id="results-chevron-' + race.id + '" style="font-size:.7rem">▶</span>'
    + '<span id="results-summary-' + race.id + '">' + summary + '</span>'
    + '</div>'
    + '<div id="results-body-' + race.id + '" style="display:none;margin-top:4px">'
    + '<div id="results-list-' + race.id + '">' + rows + '</div>'
    + '<div class="results-row" style="border-bottom:none;margin-top:4px">'
    + '<span class="results-place" id="add-place-' + race.id + '">' + (results.length+1) + '.</span>'
    + '<div style="position:relative;flex:1">'
    + '<input class="boat-picker-input" id="picker-input-' + race.id + '" placeholder="Search boat…" autocomplete="off"'
    + ' oninput="filterBoats(' + race.id + ',this.value)"'
    + ' onfocus="openPicker(' + race.id + ')"'
    + ' onblur="closePicker(' + race.id + ')"/>'
    + '<div class="boat-dropdown" id="picker-dropdown-' + race.id + '" style="display:none"></div>'
    + '</div></div></div></div>';
}

function toggleResults(raceId) {
  expandedResults[raceId] = !expandedResults[raceId];
  const body = document.getElementById('results-body-' + raceId);
  const chevron = document.getElementById('results-chevron-' + raceId);
  if (body) body.style.display = expandedResults[raceId] ? '' : 'none';
  if (chevron) chevron.textContent = expandedResults[raceId] ? '▼' : '▶';
}

async function openPicker(raceId) {
  const r = await fetch('/api/boats?exclude_race=' + raceId);
  _pickerBoats[raceId] = await r.json();
  const input = document.getElementById('picker-input-' + raceId);
  showBoatDropdown(raceId, input ? input.value : '');
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.style.display = '';
}

function closePicker(raceId) {
  setTimeout(() => {
    const dd = document.getElementById('picker-dropdown-' + raceId);
    if (dd) dd.style.display = 'none';
  }, 200);
}

function filterBoats(raceId, searchText) {
  if (_pickerBoats[raceId]) {
    // Boats are cached — show/update the dropdown even if it isn't visible
    // yet (user typed before the openPicker fetch completed) (#36).
    showBoatDropdown(raceId, searchText);
    const dd = document.getElementById('picker-dropdown-' + raceId);
    if (dd) dd.style.display = '';
  }
  // If boats aren't cached yet the openPicker fetch is still in flight;
  // it will call showBoatDropdown with the current input value on arrival.
}

function showBoatDropdown(raceId, searchText) {
  const boats = _pickerBoats[raceId] || [];
  const q = searchText.trim().toLowerCase();
  const filtered = q
    ? boats.filter(b => b.sail_number.toLowerCase().includes(q) || (b.name||'').toLowerCase().includes(q))
    : boats;
  let html = filtered.slice(0,15).map(b => {
    const label = b.name ? b.sail_number + ' — ' + b.name : b.sail_number;
    const esc = label.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return '<div class="boat-option" onmousedown="event.preventDefault()" onclick="selectBoat(' + raceId + ',' + b.id + ')">' + esc + '</div>';
  }).join('');
  const exactMatch = filtered.some(b => b.sail_number.toLowerCase() === searchText.trim().toLowerCase());
  if (searchText.trim() && !exactMatch) {
    const esc = searchText.trim().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const js = searchText.trim().replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'");
    html += '<div class="boat-option boat-option-new" onmousedown="event.preventDefault()" onclick="selectNewBoat(' + raceId + ',\\'' + js + '\\')">+ Add &ldquo;' + esc + '&rdquo;</div>';
  }
  if (!html) html = '<div class="boat-option" style="color:#8892a4;cursor:default">No boats found</div>';
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.innerHTML = html;
}

async function selectBoat(raceId, boatId) {
  const listEl = document.getElementById('results-list-' + raceId);
  const nextPlace = listEl ? listEl.children.length + 1 : 1;
  await fetch('/api/sessions/' + raceId + '/results', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({place: nextPlace, boat_id: boatId})
  });
  const input = document.getElementById('picker-input-' + raceId);
  if (input) input.value = '';
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.style.display = 'none';
  delete _pickerBoats[raceId];
  await refreshResults(raceId);
  // Pre-populate the boat cache immediately so the next entry works without
  // requiring a focus event. On mobile, the input may retain focus after
  // selection so onfocus never re-fires — openPicker here ensures filterBoats
  // has boats to display when the user starts typing the next entry (#36).
  openPicker(raceId);
}

async function selectNewBoat(raceId, sailNumber) {
  const listEl = document.getElementById('results-list-' + raceId);
  const nextPlace = listEl ? listEl.children.length + 1 : 1;
  await fetch('/api/sessions/' + raceId + '/results', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({place: nextPlace, sail_number: sailNumber})
  });
  const input = document.getElementById('picker-input-' + raceId);
  if (input) input.value = '';
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.style.display = 'none';
  delete _pickerBoats[raceId];
  await refreshResults(raceId);
  // Same fix as selectBoat — pre-populate cache for the next entry (#36).
  openPicker(raceId);
}

async function toggleResultFlag(raceId, place, boatId, dnf, dns) {
  await fetch('/api/sessions/' + raceId + '/results', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({place, boat_id: boatId, dnf, dns})
  });
  await refreshResults(raceId);
}

async function deleteResult(raceId, resultId) {
  await fetch('/api/results/' + resultId, {method:'DELETE'});
  delete _pickerBoats[raceId];
  await refreshResults(raceId);
}

async function refreshResults(raceId) {
  const r = await fetch('/api/sessions/' + raceId + '/results');
  const results = await r.json();
  const listEl = document.getElementById('results-list-' + raceId);
  if (listEl) listEl.innerHTML = results.map(r => renderResultRow(r, raceId)).join('');
  const addPlace = document.getElementById('add-place-' + raceId);
  if (addPlace) addPlace.textContent = (results.length + 1) + '.';
  const summary = results.length
    ? results.slice(0,3).map(r => r.place + '. ' + r.sail_number).join(' · ') + (results.length > 3 ? ' +' + (results.length-3) + ' more' : '')
    : 'No results yet';
  const sumEl = document.getElementById('results-summary-' + raceId);
  if (sumEl) sumEl.textContent = summary;
}

// ---- Notes ----

let _activeNoteType = 'text';

function toggleNotePanel() {
  const panel = document.getElementById('note-panel');
  panel.style.display = panel.style.display === 'none' ? '' : 'none';
  if (panel.style.display !== 'none') {
    document.getElementById('note-body').focus();
  }
}

// Whether the settings-key datalist has been populated this session.
let _settingsKeysFetched = false;

function selectNoteType(type) {
  _activeNoteType = type;
  ['text', 'settings', 'photo'].forEach(t => {
    document.getElementById('note-pane-' + t).style.display = t === type ? '' : 'none';
    document.getElementById('note-tab-' + t).classList.toggle('active', t === type);
  });
  // Lazily populate the key typeahead once per page load when the settings
  // tab is first shown.  Re-fetches after a save so newly added keys appear
  // immediately in the same session.
  if (type === 'settings') _loadSettingsKeys();
}

async function _loadSettingsKeys() {
  try {
    const r = await fetch('/api/notes/settings-keys');
    if (!r.ok) return;
    const {keys} = await r.json();
    const dl = document.getElementById('settings-key-suggestions');
    if (!dl) return;
    dl.innerHTML = keys.map(k => '<option value="' + k.replace(/&/g,'&amp;').replace(/"/g,'&quot;') + '"></option>').join('');
    _settingsKeysFetched = true;
  } catch (_) { /* non-fatal — degrades to plain input */ }
}

function addSettingsRow() {
  const container = document.getElementById('settings-rows');
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:6px;margin-bottom:6px;align-items:center';
  // list="settings-key-suggestions" wires this input to the <datalist> above,
  // giving browser-native typeahead for previously used keys.
  row.innerHTML = '<input class="field" placeholder="Key" list="settings-key-suggestions" style="flex:1;padding:6px 8px;font-size:.85rem"/>'
    + '<input class="field" placeholder="Value" style="flex:1;padding:6px 8px;font-size:.85rem"/>'
    + '<button onclick="this.parentElement.remove()" style="color:#ef4444;background:none;border:none;cursor:pointer;font-size:1.1rem">✕</button>';
  container.appendChild(row);
}

function onPhotoSelected(input) {
  const preview = document.getElementById('photo-preview');
  if (!input.files || !input.files[0]) { preview.innerHTML = ''; return; }
  const url = URL.createObjectURL(input.files[0]);
  preview.innerHTML = '<img src="' + url + '" style="max-width:100%;max-height:150px;border-radius:6px"/>';
}

async function saveNote() {
  if (!state || !state.current_race) return;
  if (_activeNoteType === 'text') await _saveTextNote(state.current_race.id);
  else if (_activeNoteType === 'settings') await _saveSettingsNote(state.current_race.id);
  else if (_activeNoteType === 'photo') await _savePhotoNote(state.current_race.id);
}

async function _saveTextNote(sessionId) {
  const body = document.getElementById('note-body').value.trim();
  if (!body) return;
  await fetch('/api/sessions/' + sessionId + '/notes', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({body, note_type: 'text'})
  });
  document.getElementById('note-body').value = '';
  _closeNotePanel(sessionId);
}

async function _saveSettingsNote(sessionId) {
  const rows = document.querySelectorAll('#settings-rows > div');
  const obj = {};
  rows.forEach(row => {
    const inputs = row.querySelectorAll('input');
    const k = inputs[0].value.trim();
    const v = inputs[1].value.trim();
    if (k) obj[k] = v;
  });
  if (!Object.keys(obj).length) return;
  await fetch('/api/sessions/' + sessionId + '/notes', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({body: JSON.stringify(obj), note_type: 'settings'})
  });
  document.getElementById('settings-rows').innerHTML = '';
  // Refresh the datalist so any newly entered keys appear in the next save.
  _settingsKeysFetched = false;
  _loadSettingsKeys();
  _closeNotePanel(sessionId);
}

async function _savePhotoNote(sessionId) {
  const input = document.getElementById('photo-file');
  if (!input.files || !input.files[0]) return;
  const fd = new FormData();
  fd.append('file', input.files[0]);
  await fetch('/api/sessions/' + sessionId + '/notes/photo', {method: 'POST', body: fd});
  input.value = '';
  document.getElementById('photo-preview').innerHTML = '';
  _closeNotePanel(sessionId);
}

function _closeNotePanel(sessionId) {
  document.getElementById('note-panel').style.display = 'none';
  const listEl = document.getElementById('notes-list-' + sessionId);
  if (listEl && listEl.style.display !== 'none') refreshNotes(sessionId);
}

function renderNote(n, sessionId) {
  const t = new Date(n.ts).toISOString().substring(11, 19) + ' UTC';
  let content = '';
  if (n.note_type === 'photo' && n.photo_path) {
    const src = '/notes/' + n.photo_path;
    content = '<img src="' + src + '" loading="lazy" style="max-width:80px;max-height:60px;border-radius:4px;'
      + 'cursor:pointer;vertical-align:middle;margin-top:2px" onclick="window.open(this.dataset.src)" data-src="' + src + '" />';
  } else if (n.note_type === 'settings' && n.body) {
    try {
      const obj = JSON.parse(n.body);
      content = Object.entries(obj).map(([k, v]) =>
        '<span style="color:#8892a4">' + k.replace(/&/g, '&amp;') + ':</span> ' + String(v).replace(/&/g, '&amp;')
      ).join(' &nbsp;·&nbsp; ');
    } catch { content = n.body; }
  } else {
    content = (n.body || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  const delBtn = sessionId != null
    ? '<button onclick="deleteNote(' + n.id + ',' + sessionId + ')" '
      + 'style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:.8rem;'
      + 'padding:0 4px;float:right" title="Delete">✕</button>'
    : '';
  return '<div style="padding:4px 0;border-bottom:1px solid #0d1a2e;font-size:.82rem;overflow:hidden">'
    + delBtn
    + '<span style="color:#8892a4;margin-right:6px">' + t + '</span>'
    + content + '</div>';
}

async function deleteNote(noteId, sessionId) {
  await fetch('/api/notes/' + noteId, {method: 'DELETE'});
  await refreshNotes(sessionId);
}

async function refreshNotes(sessionId) {
  const el = document.getElementById('notes-list-' + sessionId);
  if (!el) return;
  const r = await fetch('/api/sessions/' + sessionId + '/notes');
  const notes = await r.json();
  el.innerHTML = notes.length
    ? notes.map(n => renderNote(n, sessionId)).join('')
    : '<div style="color:#8892a4;font-size:.8rem">No notes yet</div>';
}

async function toggleNotes(sessionId) {
  const el = document.getElementById('notes-list-' + sessionId);
  if (!el) return;
  const span = el.previousElementSibling;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (span) span.textContent = 'Notes ▶';
    return;
  }
  el.style.display = '';
  if (span) span.textContent = 'Notes ▼';
  await refreshNotes(sessionId);
}

// ---------------------------------------------------------------------------
// Video linking — home page
// ---------------------------------------------------------------------------

async function toggleVideos(sessionId) {
  const el = document.getElementById('videos-list-' + sessionId);
  if (!el) return;
  const span = el.previousElementSibling;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (span) span.textContent = '🎬 Videos ▶';
    return;
  }
  el.style.display = '';
  if (span) span.textContent = '🎬 Videos ▼';
  await _loadVideos(sessionId, el);
}

async function toggleSails(sessionId) {
  const el = document.getElementById('sails-list-' + sessionId);
  if (!el) return;
  const span = el.previousElementSibling;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (span) span.textContent = '⛵ Sails ▶';
    return;
  }
  el.style.display = '';
  if (span) span.textContent = '⛵ Sails ▼';
  await _loadSails(sessionId, el);
}

async function _loadSails(sessionId, el) {
  if (!el) el = document.getElementById('sails-list-' + sessionId);
  if (!el) return;
  const [sailsResp, inventoryResp] = await Promise.all([
    fetch('/api/sessions/' + sessionId + '/sails'),
    fetch('/api/sails'),
  ]);
  const current = await sailsResp.json();
  const inventory = await inventoryResp.json();
  const slots = ['main', 'jib', 'spinnaker'];
  let html = '<div style="font-size:.78rem">';
  slots.forEach(slot => {
    const opts = (inventory[slot] || []).map(s =>
      '<option value="' + s.id + '"' + (current[slot] && current[slot].id === s.id ? ' selected' : '') + '>'
      + s.name.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</option>'
    ).join('');
    html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
      + '<span style="color:#8892a4;width:68px;flex-shrink:0">' + slot.charAt(0).toUpperCase() + slot.slice(1) + '</span>'
      + '<select id="sail-select-' + slot + '-' + sessionId + '" style="flex:1;background:#1a2840;color:#e0e8f0;border:1px solid #2563eb;border-radius:4px;padding:3px 6px;font-size:.78rem">'
      + '<option value="">— none —</option>' + opts
      + '</select></div>';
  });
  html += '<button class="btn btn-primary" style="font-size:.78rem;padding:5px 12px;margin-top:2px" onclick="saveSails(' + sessionId + ')">Save Sails</button>';
  html += '</div>';
  el.innerHTML = html;
}

async function saveSails(sessionId) {
  const slots = ['main', 'jib', 'spinnaker'];
  const body = {};
  slots.forEach(slot => {
    const sel = document.getElementById('sail-select-' + slot + '-' + sessionId);
    body[slot + '_id'] = sel && sel.value ? parseInt(sel.value, 10) : null;
  });
  const r = await fetch('/api/sessions/' + sessionId + '/sails', {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!r.ok) { alert('Failed to save sails'); return; }
  await _loadSails(sessionId, null);
}

async function _loadVideos(sessionId, el) {
  if (!el) el = document.getElementById('videos-list-' + sessionId);
  if (!el) return;
  const r = await fetch('/api/sessions/' + sessionId + '/videos');
  const videos = await r.json();
  let html = '';
  if (videos.length) {
    html += '<div style="margin-bottom:4px">';
    html += videos.map(v => {
      const lbl = v.label ? '<b>' + v.label.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</b> — ' : '';
      const ttl = (v.title || v.youtube_url).replace(/&/g,'&amp;').replace(/</g,'&lt;');
      const yt = '<a href="' + v.youtube_url.replace(/&/g,'&amp;') + '" target="_blank" style="color:#7eb8f7">' + ttl.substring(0,50) + '</a>';
      const del = '<button onclick="deleteVideo(' + v.id + ',' + sessionId + ')" style="color:#ef4444;background:none;border:none;cursor:pointer;font-size:.8rem;margin-left:8px">✕</button>';
      return '<div style="font-size:.78rem;color:#8892a4;margin-bottom:2px">' + lbl + yt + del + '</div>';
    }).join('');
    html += '</div>';
  } else {
    html += '<div style="font-size:.78rem;color:#8892a4;margin-bottom:4px">No videos linked yet</div>';
  }
  html += _videoAddForm(sessionId);
  el.innerHTML = html;
}

function _videoAddForm(sessionId) {
  const container = document.getElementById('videos-list-' + sessionId);
  const startUtc = container ? container.dataset.startUtc : '';
  // Format as datetime-local value (YYYY-MM-DDTHH:mm:ss, no timezone suffix)
  const defaultSyncUtc = startUtc ? new Date(startUtc).toISOString().substring(0, 19) : '';
  return '<div id="video-add-form-' + sessionId + '" style="display:none;margin-top:4px">'
    + '<div style="font-size:.75rem;color:#8892a4;margin-bottom:4px">Link a YouTube video</div>'
    + '<input id="video-url-' + sessionId + '" class="field" placeholder="YouTube URL" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="video-label-' + sessionId + '" class="field" placeholder="Label (e.g. Bow cam)" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<div style="font-size:.72rem;color:#8892a4;margin-bottom:2px">Sync calibration (optional) — UTC time + video position at the same moment:</div>'
    + '<input id="video-sync-utc-' + sessionId + '" class="field" type="datetime-local" step="1" placeholder="UTC time at sync point" value="' + defaultSyncUtc + '" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="video-sync-pos-' + sessionId + '" class="field" placeholder="Video position at that moment (mm:ss, optional)" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<button class="btn btn-primary" style="font-size:.82rem;padding:7px 14px" onclick="submitAddVideo(' + sessionId + ')">Add Video</button>'
    + ' <button onclick="document.getElementById(\\'video-add-form-' + sessionId + '\\').style.display=\\'none\\'" style="background:none;border:none;color:#8892a4;cursor:pointer;font-size:.82rem">Cancel</button>'
    + '</div>'
    + '<button onclick="document.getElementById(\\'video-add-form-' + sessionId + '\\').style.display=\\'\\'" style="font-size:.78rem;color:#7eb8f7;background:none;border:none;cursor:pointer;padding:2px 0">+ Add Video</button>';
}

function _parseVideoPosition(str) {
  // Parse "mm:ss", "hh:mm:ss", or plain seconds string into seconds.
  str = str.trim();
  const parts = str.split(':').map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}

async function submitAddVideo(sessionId) {
  const url = document.getElementById('video-url-' + sessionId).value.trim();
  const label = document.getElementById('video-label-' + sessionId).value.trim();
  const syncUtcVal = document.getElementById('video-sync-utc-' + sessionId).value;
  const syncPosVal = document.getElementById('video-sync-pos-' + sessionId).value.trim();
  if (!url) { alert('YouTube URL is required'); return; }
  // Sync fields are optional — default to now / 0s if not provided.
  const syncUtc = syncUtcVal
    ? (syncUtcVal.includes('Z') || syncUtcVal.includes('+') ? syncUtcVal : syncUtcVal + 'Z')
    : new Date().toISOString();
  const syncOffsetS = syncPosVal ? _parseVideoPosition(syncPosVal) : 0;
  if (syncOffsetS === null) { alert('Video position must be mm:ss or seconds'); return; }
  const btn = document.querySelector('#video-add-form-' + sessionId + ' .btn-primary');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  try {
    const resp = await fetch('/api/sessions/' + sessionId + '/videos', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({youtube_url: url, label, sync_utc: syncUtc, sync_offset_s: syncOffsetS})
    });
    if (!resp.ok) { alert('Failed to add video: ' + resp.status); return; }
    await _loadVideos(sessionId);
  } catch (e) {
    alert('Error saving video: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Add Video'; }
  }
}

async function deleteVideo(videoId, sessionId) {
  if (!confirm('Remove this video link?')) return;
  await fetch('/api/videos/' + videoId, {method: 'DELETE'});
  await _loadVideos(sessionId);
}

let _sailInventoryExpanded = false;

function toggleSailInventory() {
  _sailInventoryExpanded = !_sailInventoryExpanded;
  document.getElementById('sails-inventory-body').style.display = _sailInventoryExpanded ? '' : 'none';
  document.getElementById('sails-inventory-chevron').textContent = _sailInventoryExpanded ? '▼' : '▶';
  if (_sailInventoryExpanded) _loadSailInventory();
}

async function _loadSailInventory() {
  const el = document.getElementById('sail-inventory-list');
  if (!el) return;
  const r = await fetch('/api/sails?include_inactive=1');
  const data = await r.json();
  const allSails = ['main','jib','spinnaker'].flatMap(t => (data[t] || []).map(s => ({...s, type:t})));
  if (!allSails.length) { el.innerHTML = '<div style="font-size:.78rem;color:#8892a4">No sails yet</div>'; return; }
  el.innerHTML = '<table style="width:100%;font-size:.78rem;border-collapse:collapse">'
    + '<tr><th style="text-align:left;color:#8892a4;padding-bottom:4px">Type</th>'
    + '<th style="text-align:left;color:#8892a4;padding-bottom:4px">Name</th>'
    + '<th style="text-align:left;color:#8892a4;padding-bottom:4px">Status</th>'
    + '<th></th></tr>'
    + allSails.map(s => '<tr style="border-top:1px solid #1e3a5f">'
      + '<td style="padding:3px 6px 3px 0;color:#8892a4">' + s.type.charAt(0).toUpperCase() + s.type.slice(1) + '</td>'
      + '<td style="padding:3px 6px 3px 0' + (s.active ? '' : ';color:#8892a4') + '">' + s.name.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</td>'
      + '<td style="padding:3px 6px 3px 0;color:' + (s.active ? '#4ade80' : '#8892a4') + '">' + (s.active ? 'Active' : 'Retired') + '</td>'
      + '<td><button onclick="toggleRetireSail(' + s.id + ',' + (s.active ? 'false' : 'true') + ')" style="font-size:.72rem;color:#8892a4;background:none;border:none;cursor:pointer">'
      + (s.active ? 'Retire' : 'Restore') + '</button></td>'
      + '</tr>'
    ).join('')
    + '</table>';
}

async function addSail() {
  const type = document.getElementById('new-sail-type').value;
  const name = document.getElementById('new-sail-name').value.trim();
  if (!name) { alert('Enter a sail name'); return; }
  const r = await fetch('/api/sails', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({type, name}),
  });
  if (r.status === 409) { alert('A sail with that name already exists'); return; }
  if (!r.ok) { alert('Failed to add sail'); return; }
  document.getElementById('new-sail-name').value = '';
  await _loadSailInventory();
}

async function toggleRetireSail(id, makeActive) {
  await fetch('/api/sails/' + id, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({active: makeActive}),
  });
  await _loadSailInventory();
}

async function checkSystemHealth() {
  try {
    const r = await fetch('/api/system-health');
    if (!r.ok) return;
    const h = await r.json();
    const banner = document.getElementById('health-banner');
    const warnings = [];
    if (h.disk_pct > 85) warnings.push('Disk ' + h.disk_pct.toFixed(0) + '% full');
    if (h.cpu_temp_c != null && h.cpu_temp_c > 75) warnings.push('CPU temp ' + h.cpu_temp_c.toFixed(0) + '°C');
    if (warnings.length) {
      banner.textContent = '⚠ ' + warnings.join(' · ');
      banner.style.display = 'block';
    } else {
      banner.style.display = 'none';
    }
  } catch(e) { /* non-fatal */ }
}

async function loadPolar() {
  try {
    const r = await fetch('/api/polar/current');
    if (!r.ok) return;
    const d = await r.json();
    const set = (id, val, dec) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val != null ? Number(val).toFixed(dec) : '—';
    };
    set('pv-bsp', d.bsp, 1);
    set('pv-baseline', d.baseline_bsp, 1);
    const deltaEl = document.getElementById('pv-delta');
    const noData = document.getElementById('polar-no-data');
    if (d.sufficient_data && d.delta != null) {
      deltaEl.textContent = (d.delta >= 0 ? '+' : '') + d.delta.toFixed(2);
      deltaEl.className = 'inst-value ' + (d.delta >= 0 ? 'polar-delta-pos' : 'polar-delta-neg');
      if (noData) noData.style.display = 'none';
    } else {
      deltaEl.textContent = '—';
      deltaEl.className = 'inst-value';
      if (noData) noData.style.display = d.tws != null ? 'block' : 'none';
    }
  } catch(e) {}
}

loadState();
setInterval(loadState, 10000);
setInterval(tick, 1000);
loadInstruments();
setInterval(loadInstruments, 2000);
loadPolar();
setInterval(loadPolar, 2000);
loadRecentSailors();
checkSystemHealth();
setInterval(checkSystemHealth, 30000);
document.querySelectorAll('.crew-input').forEach(inp => {
  inp.addEventListener('focus', () => { focusedCrewInput = inp; });
});
</script>
__FOOTER__
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# History page HTML
# ---------------------------------------------------------------------------

_HISTORY_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Session History — J105 Logger</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;font-size:clamp(.85rem,2vw,1rem)}
__NAV_CSS__
.page{max-width:600px;margin:0 auto;padding:16px 12px}
@media(min-width:768px){.page{max-width:860px}}
@media(min-width:1200px){.page{max-width:1100px}}
h1{font-size:1.3rem;font-weight:700;color:#7eb8f7}
.card{background:#131f35;border-radius:12px;padding:16px;margin-bottom:12px}
.label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#8892a4;margin-bottom:6px}
.btn{display:inline-block;padding:10px 18px;border:none;border-radius:8px;
font-size:.95rem;font-weight:700;cursor:pointer;letter-spacing:.02em}
.btn-secondary{background:#1e3a5f;color:#7eb8f7;border:1px solid #2563eb}
.btn-export{padding:8px 12px;border:1px solid #2563eb;border-radius:6px;
background:#131f35;color:#7eb8f7;font-size:.8rem;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center}
.btn-grafana{border-color:#b45309;color:#fbbf24}
.event-input{background:#0a1628;border:1px solid #2563eb;border-radius:8px;
padding:10px 12px;color:#e8eaf0;font-size:.95rem;width:100%}
.filter-btn{padding:7px 14px;border:1px solid #2563eb;border-radius:20px;
background:#0a1628;color:#7eb8f7;font-size:.8rem;cursor:pointer}
.filter-btn.active{background:#2563eb;color:#fff}
.badge{font-size:.7rem;padding:1px 6px;border-radius:3px;margin-left:4px;vertical-align:middle}
.badge-race{background:#1e3a5f;color:#7eb8f7}
.badge-practice{background:#14532d;color:#4ade80}
.badge-debrief{background:#2d1b4e;color:#c084fc}
.session-name{font-weight:600;font-size:.95rem;margin-bottom:3px}
.session-meta{font-size:.8rem;color:#8892a4}
.session-exports{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.empty{color:#8892a4;text-align:center;padding:24px 0}
.pager{display:flex;gap:8px;justify-content:center;align-items:center;margin-top:8px}
.pager-info{color:#8892a4;font-size:.85rem}
.session-crew{font-size:.78rem;color:#8892a4;margin-top:3px}
.session-results{font-size:.78rem;color:#8892a4;margin-top:3px}
.results-row{display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid #0d1a2e}
.results-row:last-child{border-bottom:none}
.results-place{min-width:22px;font-size:.82rem;font-weight:700;color:#7eb8f7}
.results-boat{flex:1;font-size:.82rem}
.results-flags{display:flex;gap:3px}
.flag-btn{padding:2px 7px;border:1px solid #374151;border-radius:4px;background:#0a1628;color:#8892a4;font-size:.72rem;cursor:pointer}
.flag-btn.active-dnf{background:#7f1d1d;color:#fca5a5;border-color:#dc2626}
.flag-btn.active-dns{background:#1c1f2e;color:#818cf8;border-color:#4338ca}
.btn-del-result{padding:2px 7px;border:1px solid #374151;border-radius:4px;background:#0a1628;color:#ef4444;font-size:.72rem;cursor:pointer}
.boat-picker-input{width:100%;background:#0a1628;border:1px solid #374151;border-radius:6px;padding:6px 9px;color:#e8eaf0;font-size:.82rem}
.boat-dropdown{position:absolute;top:calc(100% + 2px);left:0;right:0;background:#131f35;border:1px solid #2563eb;border-radius:6px;max-height:190px;overflow-y:auto;z-index:200;box-shadow:0 4px 14px rgba(0,0,0,.6)}
.boat-option{padding:8px 12px;font-size:.82rem;cursor:pointer;border-bottom:1px solid #1e3a5f}
.boat-option:last-child{border-bottom:none}
.boat-option:active{background:#1e3a5f}
.boat-option-new{color:#4ade80}
</style>
</head>
<body>
<div class="page">
__NAV__
<h1 style="margin-bottom:12px">Session History</h1>

<div class="card">
  <input id="q" class="event-input" placeholder="Search by name or event…"
    oninput="scheduleLoad()" style="margin-bottom:10px"/>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
    <button class="filter-btn active" onclick="setType(this,'')">All</button>
    <button class="filter-btn" onclick="setType(this,'race')">Race</button>
    <button class="filter-btn" onclick="setType(this,'practice')">Practice</button>
    <button class="filter-btn" onclick="setType(this,'debrief')">Debrief</button>
  </div>
  <div style="display:flex;gap:8px">
    <div style="flex:1">
      <div class="label">From</div>
      <input id="from-date" type="date" class="event-input" onchange="load()"/>
    </div>
    <div style="flex:1">
      <div class="label">To</div>
      <input id="to-date" type="date" class="event-input" onchange="load()"/>
    </div>
  </div>
</div>

<div id="results"></div>
<div id="pager" class="pager"></div>

<script>
const _GRAFANA_PORT = '__GRAFANA_PORT__';
const _GRAFANA_UID = '__GRAFANA_UID__';
const _isDefaultPort = !location.port || location.port === '443' || location.port === '80';
const GRAFANA_URL = _isDefaultPort
  ? location.origin + '/grafana'
  : location.protocol + '//' + location.hostname + ':' + _GRAFANA_PORT;
const GRAFANA_UID = _GRAFANA_UID;
let currentType = '';
let currentOffset = 0;
const LIMIT = 25;
let loadTimer = null;

function setType(btn, t) {
  currentType = t;
  currentOffset = 0;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  load();
}

function scheduleLoad() {
  clearTimeout(loadTimer);
  loadTimer = setTimeout(load, 300);
}

async function load() {
  const params = new URLSearchParams();
  const q = document.getElementById('q').value.trim();
  if (q) params.set('q', q);
  if (currentType) params.set('type', currentType);
  const from = document.getElementById('from-date').value;
  const to = document.getElementById('to-date').value;
  if (from) params.set('from_date', from);
  if (to) params.set('to_date', to);
  params.set('limit', LIMIT);
  params.set('offset', currentOffset);
  const r = await fetch('/api/sessions?' + params);
  const data = await r.json();
  render(data);
}

let _tz = 'UTC';
function fmtTime(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleTimeString('en-US',{timeZone:_tz,hour:'2-digit',minute:'2-digit',hour12:false});
  } catch(e) { return new Date(iso).toISOString().substring(11,16) + ' UTC'; }
}

function fmtDur(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = Math.floor(s%60);
  if (h) return h + ':' + String(m).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
  return m + ':' + String(ss).padStart(2,'0');
}

function render(data) {
  const el = document.getElementById('results');
  if (!data.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions found</div>';
    document.getElementById('pager').innerHTML = '';
    return;
  }
  el.innerHTML = data.sessions.map(s => {
    const start = fmtTime(s.start_utc);
    const end = s.end_utc ? fmtTime(s.end_utc) : 'in progress';
    const dur = (s.end_utc && s.duration_s != null) ? ' (' + fmtDur(Math.round(s.duration_s)) + ')' : '';
    const typeClass = s.type === 'race' ? 'badge-race' : s.type === 'practice' ? 'badge-practice' : 'badge-debrief';
    const badge = '<span class="badge ' + typeClass + '">' + s.type.toUpperCase() + '</span>';
    const parent = s.parent_race_name ? '<div class="session-meta">Debrief of ' + s.parent_race_name + '</div>' : '';

    const crewLine = (s.crew && s.crew.length)
      ? s.crew.map(c => c.position.charAt(0).toUpperCase() + c.position.slice(1) + ': ' + c.sailor).join(' · ')
      : '';
    const crewSuffix = (s.type === 'debrief' && crewLine) ? ' <span style="color:#8892a4;font-size:.75rem">(from race)</span>' : '';
    const crewHtml = crewLine ? '<div class="session-crew">' + crewLine + crewSuffix + '</div>' : '';

    let exports = '';
    if (s.type !== 'debrief' && s.end_utc) {
      const from = new Date(s.start_utc).getTime();
      const to = new Date(s.end_utc).getTime();
      exports += '<a class="btn-export" href="/api/races/' + s.id + '/export.csv">&#8595; CSV</a>';
      exports += '<a class="btn-export" href="/api/races/' + s.id + '/export.gpx">&#8595; GPX</a>';
      exports += '<a class="btn-export btn-grafana" href="' + GRAFANA_URL + '/d/' + GRAFANA_UID + '/sailing-data?from=' + from + '&to=' + to + '&orgId=1&refresh=" target="_blank">&#128202; Grafana</a>';
    }
    if (s.type !== 'debrief') {
      exports += '<button class="btn-export" id="hist-results-btn-' + s.id + '" onclick="toggleHistoryResults(' + s.id + ')">Results ▶</button>';
      exports += '<button class="btn-export" id="hist-notes-btn-' + s.id + '" onclick="toggleHistoryNotes(' + s.id + ')">Notes ▶</button>';
      exports += '<button class="btn-export" id="hist-videos-btn-' + s.id + '" onclick="toggleHistoryVideos(' + s.id + ')">🎬 Videos ▶</button>';
      exports += '<button class="btn-export" id="hist-sails-btn-' + s.id + '" onclick="toggleHistorySails(' + s.id + ')">⛵ Sails ▶</button>';
    }
    if (s.has_audio && s.audio_session_id) {
      exports += '<audio controls style="width:100%;margin-top:4px">'
        + '<source src="/api/audio/' + s.audio_session_id + '/stream" type="audio/wav">'
        + '</audio>'
        + '<a class="btn-export" href="/api/audio/' + s.audio_session_id + '/download">&#8595; WAV</a>';
      exports += '<button class="btn-export" id="hist-transcript-btn-' + s.id + '" onclick="toggleHistoryTranscript(' + s.id + ',' + s.audio_session_id + ')">📝 Transcript ▶</button>';
    }
    const exportsHtml = exports ? '<div class="session-exports">' + exports + '</div>' : '';
    const resultsPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-results-' + s.id + '" style="display:none"></div>'
      : '';
    const notesPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-notes-' + s.id + '" style="display:none"></div>'
      : '';
    const videosPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-videos-' + s.id + '" data-start-utc="' + s.start_utc + '" style="display:none"></div>'
      : '';
    const sailsPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-sails-' + s.id + '" style="display:none"></div>'
      : '';
    const transcriptPanel = s.has_audio && s.audio_session_id
      ? '<div class="session-results" id="hist-transcript-' + s.id + '" style="display:none"></div>'
      : '';

    return '<div class="card"><div class="session-name">' + s.name + badge + '</div>'
      + '<div class="session-meta">' + s.date + ' &nbsp;·&nbsp; ' + start + ' → ' + end + dur + '</div>'
      + parent + crewHtml + exportsHtml + resultsPanel + notesPanel + videosPanel + sailsPanel + transcriptPanel + '</div>';
  }).join('');

  const total = data.total;
  const page = Math.floor(currentOffset / LIMIT);
  const totalPages = Math.ceil(total / LIMIT);
  const pager = document.getElementById('pager');
  if (totalPages <= 1) {
    pager.innerHTML = '<span class="pager-info">' + total + ' session' + (total !== 1 ? 's' : '') + '</span>';
  } else {
    pager.innerHTML =
      '<button class="btn btn-secondary" style="padding:8px 14px" onclick="go(' + (page-1) + ')"' + (page===0?' disabled':'') + '>&#8592; Prev</button>'
      + '<span class="pager-info">Page ' + (page+1) + ' of ' + totalPages + ' (' + total + ' total)</span>'
      + '<button class="btn btn-secondary" style="padding:8px 14px" onclick="go(' + (page+1) + ')"' + (page>=totalPages-1?' disabled':'') + '>Next &#8594;</button>';
  }
}

function go(page) {
  currentOffset = page * LIMIT;
  load();
  window.scrollTo(0, 0);
}

// ---- History page results (editable) ----
const _histPickerBoats = {};

function _renderHistResultRow(res, raceId) {
  const name = res.sail_number + (res.boat_name ? ' — ' + res.boat_name : '');
  const dnfCls = res.dnf ? ' active-dnf' : '';
  const dnsCls = res.dns ? ' active-dns' : '';
  return '<div class="results-row">'
    + '<span class="results-place">' + res.place + '.</span>'
    + '<span class="results-boat">' + name + '</span>'
    + '<div class="results-flags">'
    + '<button class="flag-btn' + dnfCls + '" onmousedown="event.preventDefault()" onclick="_histToggleFlag(' + raceId + ',' + res.place + ',' + res.boat_id + ',' + (!res.dnf) + ',' + res.dns + ')">DNF</button>'
    + '<button class="flag-btn' + dnsCls + '" onmousedown="event.preventDefault()" onclick="_histToggleFlag(' + raceId + ',' + res.place + ',' + res.boat_id + ',' + res.dnf + ',' + (!res.dns) + ')">DNS</button>'
    + '</div>'
    + '<button class="btn-del-result" onmousedown="event.preventDefault()" onclick="_histDeleteResult(' + raceId + ',' + res.id + ')">✕</button>'
    + '</div>';
}

async function toggleHistoryResults(sessionId) {
  const el = document.getElementById('hist-results-' + sessionId);
  const btn = document.getElementById('hist-results-btn-' + sessionId);
  if (!el) return;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (btn) btn.textContent = 'Results ▶';
    return;
  }
  el.innerHTML = _renderHistResultsPanel(sessionId);
  await _refreshHistResults(sessionId);
  el.style.display = '';
  if (btn) btn.textContent = 'Results ▼';
}

function _renderHistResultsPanel(raceId) {
  return '<div id="results-list-' + raceId + '"></div>'
    + '<div class="results-row" style="border-bottom:none;margin-top:4px">'
    + '<span class="results-place" id="add-place-' + raceId + '">1.</span>'
    + '<div style="position:relative;flex:1">'
    + '<input class="boat-picker-input" id="picker-input-' + raceId + '" placeholder="Search boat…" autocomplete="off"'
    + ' oninput="_histFilterBoats(' + raceId + ',this.value)"'
    + ' onfocus="_histOpenPicker(' + raceId + ')"'
    + ' onblur="_histClosePicker(' + raceId + ')"/>'
    + '<div class="boat-dropdown" id="picker-dropdown-' + raceId + '" style="display:none"></div>'
    + '</div></div>';
}

async function _histOpenPicker(raceId) {
  const r = await fetch('/api/boats?exclude_race=' + raceId);
  _histPickerBoats[raceId] = await r.json();
  const input = document.getElementById('picker-input-' + raceId);
  _histShowBoatDropdown(raceId, input ? input.value : '');
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.style.display = '';
}

function _histClosePicker(raceId) {
  setTimeout(() => {
    const dd = document.getElementById('picker-dropdown-' + raceId);
    if (dd) dd.style.display = 'none';
  }, 200);
}

function _histFilterBoats(raceId, searchText) {
  if (_histPickerBoats[raceId]) {
    _histShowBoatDropdown(raceId, searchText);
    const dd = document.getElementById('picker-dropdown-' + raceId);
    if (dd) dd.style.display = '';
  }
}

function _histShowBoatDropdown(raceId, searchText) {
  const boats = _histPickerBoats[raceId] || [];
  const q = searchText.trim().toLowerCase();
  const filtered = q
    ? boats.filter(b => b.sail_number.toLowerCase().includes(q) || (b.name||'').toLowerCase().includes(q))
    : boats;
  let html = filtered.slice(0,15).map(b => {
    const label = b.name ? b.sail_number + ' — ' + b.name : b.sail_number;
    const esc = label.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return '<div class="boat-option" onmousedown="event.preventDefault()" onclick="_histSelectBoat(' + raceId + ',' + b.id + ')">' + esc + '</div>';
  }).join('');
  const exactMatch = filtered.some(b => b.sail_number.toLowerCase() === searchText.trim().toLowerCase());
  if (searchText.trim() && !exactMatch) {
    const esc = searchText.trim().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const js = searchText.trim().replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'");
    html += '<div class="boat-option boat-option-new" onmousedown="event.preventDefault()" onclick="_histSelectNewBoat(' + raceId + ',\\'' + js + '\\')">+ Add &ldquo;' + esc + '&rdquo;</div>';
  }
  if (!html) html = '<div class="boat-option" style="color:#8892a4;cursor:default">No boats found</div>';
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.innerHTML = html;
}

async function _histSelectBoat(raceId, boatId) {
  const listEl = document.getElementById('results-list-' + raceId);
  const nextPlace = listEl ? listEl.children.length + 1 : 1;
  await fetch('/api/sessions/' + raceId + '/results', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({place: nextPlace, boat_id: boatId})
  });
  const input = document.getElementById('picker-input-' + raceId);
  if (input) input.value = '';
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.style.display = 'none';
  delete _histPickerBoats[raceId];
  await _refreshHistResults(raceId);
  _histOpenPicker(raceId);
}

async function _histSelectNewBoat(raceId, sailNumber) {
  const listEl = document.getElementById('results-list-' + raceId);
  const nextPlace = listEl ? listEl.children.length + 1 : 1;
  await fetch('/api/sessions/' + raceId + '/results', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({place: nextPlace, sail_number: sailNumber})
  });
  const input = document.getElementById('picker-input-' + raceId);
  if (input) input.value = '';
  const dd = document.getElementById('picker-dropdown-' + raceId);
  if (dd) dd.style.display = 'none';
  delete _histPickerBoats[raceId];
  await _refreshHistResults(raceId);
  _histOpenPicker(raceId);
}

async function _histToggleFlag(raceId, place, boatId, dnf, dns) {
  await fetch('/api/sessions/' + raceId + '/results', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({place, boat_id: boatId, dnf, dns})
  });
  await _refreshHistResults(raceId);
}

async function _histDeleteResult(raceId, resultId) {
  await fetch('/api/results/' + resultId, {method:'DELETE'});
  delete _histPickerBoats[raceId];
  await _refreshHistResults(raceId);
}

async function _refreshHistResults(raceId) {
  const r = await fetch('/api/sessions/' + raceId + '/results');
  const results = await r.json();
  const listEl = document.getElementById('results-list-' + raceId);
  if (listEl) listEl.innerHTML = results.map(r => _renderHistResultRow(r, raceId)).join('');
  const addPlace = document.getElementById('add-place-' + raceId);
  if (addPlace) addPlace.textContent = (results.length + 1) + '.';
}

function renderHistoryNote(n, sessionId) {
  const t = new Date(n.ts).toISOString().substring(11,19) + ' UTC';
  let content = '';
  if (n.note_type === 'photo' && n.photo_path) {
    const src = '/notes/' + n.photo_path;
    content = '<img src="' + src + '" loading="lazy" style="max-width:80px;max-height:60px;border-radius:4px;'
      + 'cursor:pointer;vertical-align:middle;margin-top:2px" onclick="window.open(this.dataset.src)" data-src="' + src + '" />';
  } else if (n.note_type === 'settings' && n.body) {
    try {
      const obj = JSON.parse(n.body);
      content = Object.entries(obj).map(([k,v]) =>
        '<span style="color:#8892a4">' + k.replace(/&/g,'&amp;') + ':</span> ' + String(v).replace(/&/g,'&amp;')
      ).join(' &nbsp;·&nbsp; ');
    } catch { content = n.body; }
  } else {
    content = (n.body||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  const delBtn = '<button onclick="deleteHistoryNote(' + n.id + ',' + sessionId + ')" '
    + 'style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:.8rem;'
    + 'padding:0 4px;float:right" title="Delete">✕</button>';
  return '<div style="padding:4px 0;border-bottom:1px solid #0d1a2e;font-size:.82rem;overflow:hidden">'
    + delBtn
    + '<span style="color:#8892a4;margin-right:6px">' + t + '</span>'
    + content + '</div>';
}

async function deleteHistoryNote(noteId, sessionId) {
  await fetch('/api/notes/' + noteId, {method:'DELETE'});
  await _refreshHistoryNotes(sessionId);
}

async function _refreshHistoryNotes(sessionId) {
  const el = document.getElementById('hist-notes-' + sessionId);
  if (!el) return;
  const r = await fetch('/api/sessions/' + sessionId + '/notes');
  const notes = await r.json();
  el.innerHTML = notes.length
    ? notes.map(n => renderHistoryNote(n, sessionId)).join('')
    : '<span style="color:#8892a4;font-size:.8rem">No notes</span>';
}

async function toggleHistoryNotes(sessionId) {
  const el = document.getElementById('hist-notes-' + sessionId);
  const btn = document.getElementById('hist-notes-btn-' + sessionId);
  if (!el) return;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (btn) btn.textContent = 'Notes ▶';
    return;
  }
  await _refreshHistoryNotes(sessionId);
  el.style.display = '';
  if (btn) btn.textContent = 'Notes ▼';
}

async function toggleHistoryVideos(sessionId) {
  const el = document.getElementById('hist-videos-' + sessionId);
  const btn = document.getElementById('hist-videos-btn-' + sessionId);
  if (!el) return;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (btn) btn.textContent = '🎬 Videos ▶';
    return;
  }
  await _loadVideos(sessionId, el);
  el.style.display = '';
  if (btn) btn.textContent = '🎬 Videos ▼';
}

// Shared video helpers (same functions used by home page are available here
// since _loadVideos, submitAddVideo, deleteVideo are defined in the main page
// JS — the history page re-defines them inline for self-containedness).
async function _loadVideos(sessionId, el) {
  if (!el) el = document.getElementById('hist-videos-' + sessionId);
  if (!el) return;
  const r = await fetch('/api/sessions/' + sessionId + '/videos');
  const videos = await r.json();
  let html = '';
  if (videos.length) {
    html += '<div style="margin-bottom:4px">';
    html += videos.map(v => {
      const lbl = v.label ? '<b>' + v.label.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</b> — ' : '';
      const ttl = (v.title || v.youtube_url).replace(/&/g,'&amp;').replace(/</g,'&lt;');
      const yt = '<a href="' + v.youtube_url.replace(/&/g,'&amp;') + '" target="_blank" style="color:#7eb8f7">' + ttl.substring(0,50) + '</a>';
      const del = '<button onclick="deleteHistVideo(' + v.id + ',' + sessionId + ')" style="color:#ef4444;background:none;border:none;cursor:pointer;font-size:.8rem;margin-left:8px">✕</button>';
      return '<div style="font-size:.78rem;color:#8892a4;margin-bottom:2px">' + lbl + yt + del + '</div>';
    }).join('');
    html += '</div>';
  } else {
    html += '<div style="font-size:.78rem;color:#8892a4;margin-bottom:4px">No videos linked yet</div>';
  }
  html += _histVideoAddForm(sessionId);
  el.innerHTML = html;
}

function _histVideoAddForm(sessionId) {
  const container = document.getElementById('hist-videos-' + sessionId);
  const startUtc = container ? container.dataset.startUtc : '';
  const defaultSyncUtc = startUtc ? new Date(startUtc).toISOString().substring(0, 19) : '';
  return '<div id="hist-video-add-form-' + sessionId + '" style="display:none;margin-top:4px">'
    + '<input id="hist-video-url-' + sessionId + '" class="field" placeholder="YouTube URL" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="hist-video-label-' + sessionId + '" class="field" placeholder="Label (e.g. Bow cam)" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<div style="font-size:.72rem;color:#8892a4;margin-bottom:2px">Sync calibration (optional) — UTC time + video position at the same moment:</div>'
    + '<input id="hist-video-sync-utc-' + sessionId + '" class="field" type="datetime-local" step="1" placeholder="UTC time at sync point" value="' + defaultSyncUtc + '" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="hist-video-sync-pos-' + sessionId + '" class="field" placeholder="Video position (mm:ss, optional)" style="margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<button class="btn-export" style="background:#2563eb;color:#fff;border-color:#2563eb" onclick="submitHistAddVideo(' + sessionId + ')">Add Video</button>'
    + ' <button onclick="document.getElementById(\\'hist-video-add-form-' + sessionId + '\\').style.display=\\'none\\'" style="background:none;border:none;color:#8892a4;cursor:pointer;font-size:.82rem">Cancel</button>'
    + '</div>'
    + '<button onclick="document.getElementById(\\'hist-video-add-form-' + sessionId + '\\').style.display=\\'\\'" style="font-size:.78rem;color:#7eb8f7;background:none;border:none;cursor:pointer;padding:2px 0">+ Add Video</button>';
}

function _parseVideoPos(str) {
  str = str.trim();
  const parts = str.split(':').map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}

async function submitHistAddVideo(sessionId) {
  const url = document.getElementById('hist-video-url-' + sessionId).value.trim();
  const label = document.getElementById('hist-video-label-' + sessionId).value.trim();
  const syncUtcVal = document.getElementById('hist-video-sync-utc-' + sessionId).value;
  const syncPosVal = document.getElementById('hist-video-sync-pos-' + sessionId).value.trim();
  if (!url) { alert('YouTube URL is required'); return; }
  // Sync fields are optional — default to now / 0s if not provided.
  const syncUtc = syncUtcVal
    ? (syncUtcVal.includes('Z') || syncUtcVal.includes('+') ? syncUtcVal : syncUtcVal + 'Z')
    : new Date().toISOString();
  const syncOffsetS = syncPosVal ? _parseVideoPos(syncPosVal) : 0;
  if (syncOffsetS === null) { alert('Video position must be mm:ss or seconds'); return; }
  const btn = document.querySelector('#hist-video-add-form-' + sessionId + ' .btn-export');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  try {
    const resp = await fetch('/api/sessions/' + sessionId + '/videos', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({youtube_url: url, label, sync_utc: syncUtc, sync_offset_s: syncOffsetS})
    });
    if (!resp.ok) { alert('Failed to add video: ' + resp.status); return; }
    const el = document.getElementById('hist-videos-' + sessionId);
    await _loadVideos(sessionId, el);
  } catch (e) {
    alert('Error saving video: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Add Video'; }
  }
}

async function deleteHistVideo(videoId, sessionId) {
  if (!confirm('Remove this video link?')) return;
  await fetch('/api/videos/' + videoId, {method: 'DELETE'});
  const el = document.getElementById('hist-videos-' + sessionId);
  await _loadVideos(sessionId, el);
}

async function toggleHistorySails(sessionId) {
  const el = document.getElementById('hist-sails-' + sessionId);
  const btn = document.getElementById('hist-sails-btn-' + sessionId);
  if (!el) return;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (btn) btn.textContent = '⛵ Sails ▶';
    return;
  }
  await _loadSailsForHistory(sessionId, el);
  el.style.display = '';
  if (btn) btn.textContent = '⛵ Sails ▼';
}

async function _loadSailsForHistory(sessionId, el) {
  if (!el) el = document.getElementById('hist-sails-' + sessionId);
  if (!el) return;
  const [sailsResp, inventoryResp] = await Promise.all([
    fetch('/api/sessions/' + sessionId + '/sails'),
    fetch('/api/sails'),
  ]);
  const current = await sailsResp.json();
  const inventory = await inventoryResp.json();
  const slots = ['main', 'jib', 'spinnaker'];
  let html = '<div style="font-size:.78rem">';
  slots.forEach(slot => {
    const opts = (inventory[slot] || []).map(s =>
      '<option value="' + s.id + '"' + (current[slot] && current[slot].id === s.id ? ' selected' : '') + '>'
      + s.name.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</option>'
    ).join('');
    html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
      + '<span style="color:#8892a4;width:68px;flex-shrink:0">' + slot.charAt(0).toUpperCase() + slot.slice(1) + '</span>'
      + '<select id="hist-sail-select-' + slot + '-' + sessionId + '" style="flex:1;background:#1a2840;color:#e0e8f0;border:1px solid #2563eb;border-radius:4px;padding:3px 6px;font-size:.78rem">'
      + '<option value="">— none —</option>' + opts
      + '</select></div>';
  });
  html += '<button class="btn-export" style="background:#2563eb;color:#fff;border-color:#2563eb;font-size:.78rem" onclick="saveHistSails(' + sessionId + ')">Save Sails</button>';
  html += '</div>';
  el.innerHTML = html;
}

async function saveHistSails(sessionId) {
  const slots = ['main', 'jib', 'spinnaker'];
  const body = {};
  slots.forEach(slot => {
    const sel = document.getElementById('hist-sail-select-' + slot + '-' + sessionId);
    body[slot + '_id'] = sel && sel.value ? parseInt(sel.value, 10) : null;
  });
  const r = await fetch('/api/sessions/' + sessionId + '/sails', {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  if (!r.ok) { alert('Failed to save sails'); return; }
  await _loadSailsForHistory(sessionId, null);
}

async function toggleHistoryTranscript(sessionId, audioSessionId) {
  const el = document.getElementById('hist-transcript-' + sessionId);
  const btn = document.getElementById('hist-transcript-btn-' + sessionId);
  if (!el) return;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (btn) btn.textContent = '📝 Transcript ▶';
    return;
  }
  el.style.display = '';
  if (btn) btn.textContent = '📝 Transcript ▼';
  await _loadTranscript(sessionId, audioSessionId, el);
}

async function _loadTranscript(sessionId, audioSessionId, el) {
  if (!el) el = document.getElementById('hist-transcript-' + sessionId);
  if (!el) return;
  el.innerHTML = '<span style="color:#8892a4;font-size:.8rem">Loading…</span>';
  const r = await fetch('/api/audio/' + audioSessionId + '/transcript');
  if (r.status === 404) {
    // No job yet — offer a button to start transcription
    el.innerHTML = '<div style="font-size:.8rem;color:#8892a4">No transcript yet. '
      + '<button class="btn-export" style="font-size:.75rem" onclick="startTranscript(' + sessionId + ',' + audioSessionId + ')">▶ Transcribe</button></div>';
    return;
  }
  const t = await r.json();
  if (t.status === 'pending' || t.status === 'running') {
    el.innerHTML = '<span style="color:#facc15;font-size:.8rem">⏳ Transcription in progress…</span>';
    setTimeout(() => _loadTranscript(sessionId, audioSessionId, el), 3000);
    return;
  }
  if (t.status === 'error') {
    el.innerHTML = '<span style="color:#f87171;font-size:.8rem">⚠ Error: ' + (t.error_msg || 'unknown') + '</span>';
    return;
  }
  // status === 'done'
  if (t.segments && t.segments.length > 0) {
    // merge consecutive same-speaker segments for readability
    const blocks = [];
    for (const seg of t.segments) {
      const last = blocks[blocks.length - 1];
      if (last && last.speaker === seg.speaker) {
        last.text += ' ' + seg.text; last.end = seg.end;
      } else { blocks.push({...seg}); }
    }
    const speakers = [...new Set(blocks.map(b => b.speaker))];
    const palette = ['#7dd3fc','#86efac','#fde68a','#fca5a5','#c4b5fd','#f9a8d4'];
    const color = s => palette[speakers.indexOf(s) % palette.length];
    const fmt = s => { const m=Math.floor(s/60); return m+':'+String(Math.floor(s%60)).padStart(2,'0'); };
    const html = blocks.map(b =>
      `<div style="margin-bottom:8px">
         <span style="color:${color(b.speaker)};font-weight:600;font-size:.75rem">${b.speaker}</span>
         <span style="color:#8892a4;font-size:.7rem;margin-left:4px">[${fmt(b.start)}]</span>
         <div style="color:#c4cdd8;font-size:.8rem;margin-top:2px">${b.text.trim().replace(/</g,'&lt;')}</div>
       </div>`
    ).join('');
    el.innerHTML = '<div style="max-height:300px;overflow-y:auto;background:#0d1929;border-radius:6px;padding:8px">' + html + '</div>';
  } else {
    // legacy: plain text fallback
    const text = t.text ? t.text.replace(/</g,'&lt;') : '(empty)';
    el.innerHTML = '<div style="font-size:.8rem;color:#c4cdd8;white-space:pre-wrap;max-height:200px;overflow-y:auto;background:#0d1929;border-radius:6px;padding:8px">' + text + '</div>';
  }
}

async function startTranscript(sessionId, audioSessionId) {
  const r = await fetch('/api/audio/' + audioSessionId + '/transcribe', {method: 'POST'});
  if (!r.ok) { alert('Failed to start transcription'); return; }
  const el = document.getElementById('hist-transcript-' + sessionId);
  await _loadTranscript(sessionId, audioSessionId, el);
}

// Default: last 30 days
const now = new Date();
const past = new Date(now - 30 * 86400000);
document.getElementById('to-date').value = now.toISOString().substring(0,10);
document.getElementById('from-date').value = past.toISOString().substring(0,10);
fetch('/api/state').then(r=>r.json()).then(s=>{if(s.timezone)_tz=s.timezone;}).catch(()=>{}).finally(()=>load());
</script>
__FOOTER__
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Admin — boat registry page
# ---------------------------------------------------------------------------

_ADMIN_BOATS_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Boat Registry — J105 Logger</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;font-size:clamp(.85rem,2vw,1rem)}
__NAV_CSS__
.page{max-width:640px;margin:0 auto;padding:16px 12px}
@media(min-width:768px){.page{max-width:860px}}
@media(min-width:1200px){.page{max-width:1100px}}
h1{font-size:1.3rem;font-weight:700;color:#7eb8f7}
.card{background:#131f35;border-radius:12px;padding:16px;margin-bottom:12px}
.label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#8892a4;margin-bottom:8px}
.btn-export{padding:8px 12px;border:1px solid #2563eb;border-radius:6px;
background:#131f35;color:#7eb8f7;font-size:.8rem;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center}
.field{background:#0a1628;border:1px solid #2563eb;border-radius:6px;
padding:9px 12px;color:#e8eaf0;font-size:.9rem;width:100%}
.btn-add{padding:9px 16px;border:none;border-radius:6px;background:#2563eb;
color:#fff;font-weight:700;cursor:pointer;font-size:.9rem}
.btn-sm{padding:8px 12px;border:1px solid #374151;border-radius:4px;
background:#0a1628;font-size:.78rem;cursor:pointer;min-height:36px}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.btn-edit{color:#7eb8f7;border-color:#2563eb}
.btn-del{color:#ef4444;border-color:#7f1d1d}
.btn-save{color:#4ade80;border-color:#16a34a}
.btn-cancel{color:#8892a4}
table{width:100%;border-collapse:collapse;font-size:.87rem}
th{text-align:left;color:#8892a4;font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;
padding:6px 8px;border-bottom:1px solid #1e3a5f}
td{padding:7px 8px;border-bottom:1px solid #0d1a2e;vertical-align:middle}
tr:last-child td{border-bottom:none}
.empty{color:#8892a4;text-align:center;padding:20px 0}
</style>
</head>
<body>
<div class="page">
__NAV__
<h1 style="margin-bottom:12px">Boat Registry</h1>

<div class="card">
  <div class="label">Add Boat</div>
  <div style="display:grid;grid-template-columns:1fr;gap:8px;margin-bottom:10px">
    <input id="new-sail" class="field" placeholder="Sail number *" maxlength="30"/>
    <input id="new-name" class="field" placeholder="Boat name" maxlength="40"/>
    <input id="new-class" class="field" placeholder="Class" maxlength="20"/>
  </div>
  <button class="btn-add" onclick="addBoat()">+ Add Boat</button>
</div>

<div class="card">
  <div class="table-wrap" id="boat-table-wrap">Loading…</div>
</div>

<script>
async function loadBoats() {
  const wrap = document.getElementById('boat-table-wrap');
  try {
    const r = await fetch('/api/boats');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const boats = await r.json();
    if (!boats.length) {
      wrap.innerHTML = '<div class="empty">No boats yet</div>';
      return;
    }
    let html = '<table><thead><tr><th>Sail #</th><th>Name</th><th>Class</th><th>Last used</th><th></th></tr></thead><tbody>';
    boats.forEach(b => {
      const lu = b.last_used ? new Date(b.last_used).toLocaleDateString() : '—';
      const safeSail = (b.sail_number||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const safeName = (b.name||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const safeCls  = (b.class||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      // Store values in data attributes — avoids embedding strings in onclick
      // which caused a JS SyntaxError via Python triple-quote escaping (#36).
      html += '<tr id="boat-row-' + b.id + '" data-sail="' + safeSail + '" data-name="' + safeName + '" data-cls="' + safeCls + '">'
        + '<td>' + safeSail + '</td>'
        + '<td>' + (safeName||'<span style="color:#8892a4">—</span>') + '</td>'
        + '<td>' + (safeCls||'<span style="color:#8892a4">—</span>') + '</td>'
        + '<td>' + lu + '</td>'
        + '<td style="white-space:nowrap;display:flex;gap:4px">'
        + '<button class="btn-sm btn-edit" onclick="editBoat(' + b.id + ')">Edit</button>'
        + '<button class="btn-sm btn-del" onclick="deleteBoat(' + b.id + ')">Delete</button>'
        + '</td></tr>';
    });
    html += '</tbody></table>';
    wrap.innerHTML = html;
  } catch(e) {
    wrap.innerHTML = '<div class="empty" style="color:#ef4444">Failed to load boats: ' + e.message + '</div>';
  }
}

async function addBoat() {
  const sail = document.getElementById('new-sail').value.trim();
  if (!sail) { alert('Sail number is required'); return; }
  const name = document.getElementById('new-name').value.trim() || null;
  const cls  = document.getElementById('new-class').value.trim() || null;
  const resp = await fetch('/api/boats', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({sail_number: sail, name, class_name: cls})
  });
  if (!resp.ok) { alert('Failed to add boat'); return; }
  document.getElementById('new-sail').value = '';
  document.getElementById('new-name').value = '';
  document.getElementById('new-class').value = '';
  await loadBoats();
}

function editBoat(id) {
  const row = document.getElementById('boat-row-' + id);
  // Read values from data attributes set during render — safe, no escaping needed.
  const sail = row.dataset.sail || '';
  const name = row.dataset.name || '';
  const cls  = row.dataset.cls  || '';
  const eSail = sail.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  const eName = name.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  const eCls  = cls.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  row.innerHTML = ''
    + '<td><input class="field" id="edit-sail-' + id + '" value="' + eSail + '" style="width:90px"/></td>'
    + '<td><input class="field" id="edit-name-' + id + '" value="' + eName + '" style="width:120px"/></td>'
    + '<td><input class="field" id="edit-class-' + id + '" value="' + eCls + '" style="width:80px"/></td>'
    + '<td></td>'
    + '<td style="white-space:nowrap;display:flex;gap:4px">'
    + '<button class="btn-sm btn-save" onclick="saveBoat(' + id + ')">Save</button>'
    + '<button class="btn-sm btn-cancel" onclick="loadBoats()">Cancel</button>'
    + '</td>';
}

async function saveBoat(id) {
  const sail  = document.getElementById('edit-sail-' + id).value.trim();
  if (!sail) { alert('Sail number is required'); return; }
  const name  = document.getElementById('edit-name-' + id).value.trim() || null;
  const cls   = document.getElementById('edit-class-' + id).value.trim() || null;
  await fetch('/api/boats/' + id, {
    method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({sail_number: sail, name, class_name: cls})
  });
  await loadBoats();
}

async function deleteBoat(id) {
  if (!confirm('Delete this boat?')) return;
  await fetch('/api/boats/' + id, {method:'DELETE'});
  await loadBoats();
}

loadBoats();
</script>
__FOOTER__
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


POSITIONS: tuple[str, ...] = ("helm", "main", "pit", "bow", "tactician", "guest")


class EventRequest(BaseModel):
    event_name: str


class CrewEntry(BaseModel):
    position: str
    sailor: str


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


class NoteCreate(BaseModel):
    body: str | None = None
    note_type: str = "text"
    ts: str | None = None  # UTC ISO 8601; defaults to server time if absent


class VideoCreate(BaseModel):
    youtube_url: str
    label: str = ""
    # Sync point: a known UTC time and the corresponding video player position.
    # offset = logger_utc_s - video_position_s
    # The UI may send either the raw offset or derive it via the calibration
    # helper (sync_utc + sync_video_s).
    sync_utc: str  # UTC ISO 8601
    sync_offset_s: float = 0.0  # seconds; can also be supplied by calibration


class VideoUpdate(BaseModel):
    label: str | None = None
    sync_utc: str | None = None
    sync_offset_s: float | None = None


class SailCreate(BaseModel):
    type: str  # 'main' | 'jib' | 'spinnaker'
    name: str
    notes: str | None = None


class SailUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    active: bool | None = None


class RaceSailsSet(BaseModel):
    main_id: int | None = None
    jib_id: int | None = None
    spinnaker_id: int | None = None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

_LOGIN_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>J105 Logger — Sign In</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;
padding:16px;max-width:480px;margin:0 auto}
h1{font-size:1.3rem;font-weight:700;color:#7eb8f7;margin-bottom:16px}
.card{background:#131f35;border-radius:12px;padding:20px}
label{font-size:.85rem;color:#8892a4;display:block;margin-bottom:6px}
input{width:100%;background:#0a1628;border:1px solid #2563eb;border-radius:8px;
padding:12px;color:#e8eaf0;font-size:1rem;margin-bottom:14px}
.btn{display:block;width:100%;padding:14px;border:none;border-radius:10px;
font-size:1rem;font-weight:700;cursor:pointer;background:#2563eb;color:#fff}
</style>
</head>
<body>
<h1>J105 Logger</h1>
<div class="card">
<form method="post" action="/login">
<input type="hidden" name="next" value="__NEXT__"/>
<label>Invite token</label>
<input type="text" name="token" placeholder="Paste your invite token here" autocomplete="off" value="__TOKEN__"/>
<button class="btn" type="submit">Sign in</button>
</form>
<!--ERROR-->
</div>
</body>
</html>
"""


def _render_admin_users_html(users: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> str:
    """Render a simple admin users management page."""
    from logger.races import configured_tz

    tz = configured_tz()

    def _local_ts(utc_str: str | None) -> str:
        if not utc_str:
            return "—"
        try:
            from datetime import datetime as _dt

            dt = _dt.fromisoformat(utc_str).astimezone(tz)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return utc_str[:19]

    nav = _nav_html("/admin/users")
    footer = _FOOTER_HTML.replace("__GIT_INFO__", _GIT_INFO)
    role_colors = {"admin": "#f59e0b", "crew": "#34d399", "viewer": "#60a5fa"}

    def _badge(role: str) -> str:
        color = role_colors.get(role, "#8892a4")
        return f'<span style="background:{color}22;color:{color};padding:1px 7px;border-radius:4px;font-size:.75rem">{role}</span>'

    rows = "".join(
        f"<tr><td>{u['email']}</td><td>{u['name'] or '—'}</td>"
        f"<td>{_badge(u['role'])}</td>"
        f"<td>{_local_ts(u['last_seen'])}</td>"
        f'<td><button onclick="changeRole({u["id"]})" style="cursor:pointer;background:none;border:1px solid #2563eb;color:#7eb8f7;border-radius:4px;padding:2px 8px;font-size:.8rem">Change role</button></td>'
        f"</tr>"
        for u in users
    )
    sess_rows = "".join(
        f"<tr><td>{s.get('email', '')}</td><td>{s.get('role', '')}</td>"
        f"<td>{s.get('ip', '—')}</td>"
        f"<td>{_local_ts(s['created_at'])}</td>"
        f"<td>{_local_ts(s['expires_at'])}</td>"
        f'<td><button onclick="revokeSession(\'{s["session_id"]}\')" style="cursor:pointer;background:#7f1d1d;border:none;color:#fca5a5;border-radius:4px;padding:2px 8px;font-size:.8rem">Revoke</button></td>'
        f"</tr>"
        for s in sessions
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>J105 Logger — Admin Users</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;padding:16px;max-width:900px;margin:0 auto}}
h1{{font-size:1.3rem;font-weight:700;color:#7eb8f7;margin-bottom:4px}}
h2{{font-size:1rem;font-weight:600;color:#e8eaf0;margin:20px 0 10px}}
{_NAV_CSS}
.card{{background:#131f35;border-radius:12px;padding:16px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th{{text-align:left;padding:6px 8px;color:#8892a4;font-weight:500;border-bottom:1px solid #1e3a5f}}
td{{padding:6px 8px;border-bottom:1px solid #0d1a2e}}
label{{font-size:.85rem;color:#8892a4;display:block;margin-bottom:4px}}
input,select{{background:#0a1628;border:1px solid #2563eb;border-radius:6px;padding:8px 10px;color:#e8eaf0;font-size:.9rem;margin-bottom:10px}}
.btn{{padding:10px 18px;border:none;border-radius:8px;font-size:.9rem;font-weight:700;cursor:pointer;background:#2563eb;color:#fff}}
#invite-result{{margin-top:12px;font-size:.85rem;color:#4ade80;word-break:break-all}}
</style>
</head>
<body>
{nav}
<h1>Admin — Users &amp; Sessions</h1>
<div class="card">
<h2>Users</h2>
<table><thead><tr><th>Email</th><th>Name</th><th>Role</th><th>Last seen</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>
</div>
<div class="card">
<h2>Invite a user</h2>
<form id="invite-form">
<label>Email</label><input type="email" id="inv-email" required/>
<label>Name</label><input type="text" id="inv-name" placeholder="Optional"/>
<label>Role</label>
<select id="inv-role"><option value="viewer">viewer</option><option value="crew">crew</option><option value="admin">admin</option></select><br/>
<button class="btn" type="submit">Generate invite link</button>
</form>
<div id="invite-result"></div>
</div>
<div class="card">
<h2>Active sessions</h2>
<table><thead><tr><th>User</th><th>Role</th><th>IP</th><th>Created</th><th>Expires</th><th></th></tr></thead>
<tbody>{sess_rows}</tbody>
</table>
</div>
<script>
document.getElementById('invite-form').addEventListener('submit', async e => {{
  e.preventDefault();
  const email = document.getElementById('inv-email').value;
  const name = document.getElementById('inv-name').value;
  const role = document.getElementById('inv-role').value;
  const fd = new FormData();
  fd.append('email', email);
  fd.append('name', name);
  fd.append('role', role);
  const r = await fetch('/admin/users/invite', {{method:'POST', body:fd}});
  const d = await r.json();
  document.getElementById('invite-result').textContent = d.invite_url || JSON.stringify(d);
}});
async function changeRole(userId) {{
  const role = prompt('New role (viewer/crew/admin):');
  if (!role) return;
  await fetch('/admin/users/' + userId + '/role', {{method:'PUT', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{role}})}});
  location.reload();
}}
async function revokeSession(sid) {{
  await fetch('/admin/sessions/' + sid, {{method:'DELETE'}});
  location.reload();
}}
</script>
{footer}
</body></html>"""


def _render_profile_html(user: dict[str, Any]) -> str:
    """Render the self-service profile page."""
    import time

    nav = _nav_html("/profile")
    footer = _FOOTER_HTML.replace("__GIT_INFO__", _GIT_INFO)
    user_id = user.get("id") or 0
    name = user.get("name") or user.get("email") or "Unknown"
    email = user.get("email") or ""
    role = user.get("role", "viewer")
    role_colors = {"admin": "#f59e0b", "crew": "#34d399", "viewer": "#60a5fa"}
    rc = role_colors.get(role, "#8892a4")
    cache_bust = int(time.time())
    avatar_url = f"/avatars/{user_id}.jpg?v={cache_bust}" if user_id else ""
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>J105 Logger — Profile</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;padding:16px;max-width:500px;margin:0 auto}}
h1{{font-size:1.3rem;font-weight:700;color:#7eb8f7;margin-bottom:12px}}
.card{{background:#131f35;border-radius:12px;padding:24px;text-align:center}}
.avatar{{width:128px;height:128px;border-radius:50%;object-fit:cover;cursor:pointer;border:3px solid {rc};margin-bottom:16px}}
.name{{font-size:1.2rem;font-weight:600;margin-bottom:4px}}
.email{{color:#8892a4;font-size:.85rem;margin-bottom:8px}}
.role{{background:{rc}22;color:{rc};padding:2px 10px;border-radius:4px;font-size:.8rem;display:inline-block}}
a{{color:#7eb8f7;text-decoration:none}}
{_NAV_CSS}
</style>
</head>
<body>
{nav}
<h1 style="margin-bottom:12px">Profile</h1>
<div class="card">
  <img class="avatar" id="avatar" src="{avatar_url}" alt="avatar" onclick="document.getElementById('file').click()"/>
  <input type="file" id="file" accept="image/*" style="display:none"/>
  <div class="name">{name}</div>
  <div class="email">{email}</div>
  <div class="role">{role}</div>
</div>
<script>
document.getElementById('file').addEventListener('change', async e => {{
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  const r = await fetch('/profile/avatar', {{method:'POST', body:fd}});
  if (r.ok) {{
    window.location.reload();
  }} else {{
    const d = await r.json();
    alert(d.detail || 'Upload failed');
  }}
}});
</script>
{footer}
</body></html>"""


def _render_admin_audit_html(entries: list[dict[str, Any]]) -> str:
    """Render the audit log page."""
    from logger.races import configured_tz

    tz = configured_tz()

    def _local_ts(utc_str: str) -> str:
        try:
            from datetime import datetime as _dt

            dt = _dt.fromisoformat(utc_str).astimezone(tz)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return utc_str[:19]

    nav = _nav_html("/admin/audit")
    footer = _FOOTER_HTML.replace("__GIT_INFO__", _GIT_INFO)
    rows = "".join(
        f"<tr><td>{_local_ts(e['ts'])}</td>"
        f"<td>{e.get('user_name') or e.get('user_email') or '—'}</td>"
        f"<td><code>{e['action']}</code></td>"
        f'<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{e.get("detail") or ""}</td>'  # noqa: E501
        f'<td style="font-size:.7rem">{e.get("ip_address") or ""}</td></tr>'
        for e in entries
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>J105 Logger — Audit Log</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;padding:16px;max-width:1000px;margin:0 auto}}
h1{{font-size:1.3rem;font-weight:700;color:#7eb8f7;margin-bottom:12px}}
.card{{background:#131f35;border-radius:12px;padding:16px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th{{text-align:left;padding:6px 8px;color:#8892a4;font-weight:500;border-bottom:1px solid #1e3a5f}}
td{{padding:6px 8px;border-bottom:1px solid #0d1a2e}}
code{{background:#1e3a5f;padding:1px 5px;border-radius:3px;font-size:.78rem}}
a{{color:#7eb8f7;text-decoration:none}}
{_NAV_CSS}
</style>
</head>
<body>
{nav}
<h1 style="margin-bottom:12px">Audit Log</h1>
<div class="card">
<table><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Detail</th><th>IP</th></tr></thead>
<tbody>{rows}</tbody>
</table>
{('<p style="text-align:center;color:#8892a4;padding:20px">No audit entries yet.</p>' if not entries else "")}
</div>
{footer}
</body></html>"""


_ADMIN_CAMERAS_HTML = """\
<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Cameras — J105 Logger</title>
<style>
*{box-sizing:border-box}
__NAV_CSS__
body{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;margin:0;padding:16px}
.card{background:#131f35;border-radius:12px;padding:16px;margin-bottom:12px}
.label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#8892a4;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:.87rem}
th{text-align:left;color:#8892a4;font-size:.75rem;text-transform:uppercase;padding:6px 8px}
td{padding:7px 8px;border-bottom:1px solid #0d1a2e}
.badge{padding:2px 8px;border-radius:4px;font-size:.78rem;font-weight:600}
.badge-rec{background:#16a34a22;color:#4ade80}
.badge-idle{background:#374151;color:#8892a4}
.badge-err{background:#7f1d1d22;color:#ef4444}
.btn-sm{padding:6px 12px;border:1px solid #374151;border-radius:4px;background:#0a1628;color:#e8eaf0;font-size:.78rem;cursor:pointer}
.btn-start{color:#4ade80;border-color:#16a34a}
.btn-stop{color:#ef4444;border-color:#7f1d1d}
.btn-refresh{color:#7eb8f7;border-color:#2563eb}
</style></head><body>
__NAV__
<h1>Cameras</h1>
<div class="card">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
<div class="label">Configured Cameras</div>
<button class="btn-sm btn-refresh" onclick="loadCameras()">↻ Refresh</button>
</div>
<div id="cam-table">Loading…</div>
</div>
<div class="card">
<div class="label">Recent Camera Sessions</div>
<div id="cam-sessions">Loading…</div>
</div>
<script>
async function loadCameras(){
  document.getElementById('cam-table').textContent='Loading…';
  const r=await fetch('/api/cameras');
  if(!r.ok){document.getElementById('cam-table').textContent='Failed to load';return}
  const cams=await r.json();
  if(!cams.length){document.getElementById('cam-table').innerHTML='<p style="color:#8892a4">No cameras configured. Set CAMERAS env var.</p>';return}
  let h='<table><thead><tr><th>Name</th><th>IP</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
  for(const c of cams){
    const badge=c.error?`<span class="badge badge-err">error</span>`:c.recording?`<span class="badge badge-rec">recording</span>`:`<span class="badge badge-idle">idle</span>`;
    const btn=c.recording?`<button class="btn-sm btn-stop" onclick="camAction('${c.name}','stop')">⏹ Stop</button>`:`<button class="btn-sm btn-start" onclick="camAction('${c.name}','start')">⏺ Start</button>`;
    h+=`<tr><td>${c.name}</td><td>${c.ip}</td><td>${badge}${c.error?' <small style="color:#ef4444">'+c.error.slice(0,60)+'</small>':''}</td><td>${btn}</td></tr>`;
  }
  h+='</tbody></table>';
  document.getElementById('cam-table').innerHTML=h;
}
async function camAction(name,action){
  const r=await fetch('/api/cameras/'+encodeURIComponent(name)+'/'+action,{method:'POST'});
  const d=await r.json();
  if(d.error)alert('Camera '+name+': '+d.error);
  loadCameras();
}
async function loadSessions(){
  const r=await fetch('/api/cameras/sessions');
  if(!r.ok)return;
  const rows=await r.json();
  if(!rows.length){document.getElementById('cam-sessions').innerHTML='<p style="color:#8892a4">No camera sessions recorded yet.</p>';return}
  let h='<table><thead><tr><th>Camera</th><th>Race</th><th>Started</th><th>Stopped</th><th>Latency</th><th>Error</th></tr></thead><tbody>';
  for(const s of rows){
    const started=s.recording_started_utc?new Date(s.recording_started_utc+'Z').toLocaleString():'—';
    const stopped=s.recording_stopped_utc?new Date(s.recording_stopped_utc+'Z').toLocaleString():'—';
    const lat=s.sync_offset_ms!=null?s.sync_offset_ms+'ms':'—';
    const err=s.error?`<small style="color:#ef4444">${s.error.slice(0,40)}</small>`:'—';
    h+=`<tr><td>${s.camera_name}</td><td>${s.race_name||s.session_id}</td><td>${started}</td><td>${stopped}</td><td>${lat}</td><td>${err}</td></tr>`;
  }
  h+='</tbody></table>';
  document.getElementById('cam-sessions').innerHTML=h;
}
loadCameras();loadSessions();
</script>
__FOOTER__
</body></html>"""


def create_app(
    storage: Storage,
    recorder: AudioRecorder | None = None,
    audio_config: AudioConfig | None = None,
    cameras: list[Camera] | None = None,
) -> FastAPI:
    """Create and return the FastAPI application bound to the given Storage.

    If *recorder* and *audio_config* are provided, recording starts when a race
    starts and stops when the race ends.  If *cameras* is provided, cameras
    start/stop recording in sync with races.
    """
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address

    limiter = Limiter(key_func=get_remote_address, config_filename="/dev/null")
    app = FastAPI(title="J105 Logger", docs_url=None, redoc_url=None)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.state.storage = storage
    _audio_session_id: int | None = None
    _debrief_audio_session_id: int | None = None
    _debrief_race_id: int | None = None
    _debrief_race_name: str | None = None
    _debrief_start_utc: datetime | None = None

    from logger.races import RaceConfig

    cfg = RaceConfig()

    def _inject_shared(html: str, current: str) -> str:
        return (
            html.replace("__NAV_CSS__", _NAV_CSS)
            .replace("__NAV__", _nav_html(current))
            .replace("__FOOTER__", _FOOTER_HTML)
            .replace("__GIT_INFO__", _GIT_INFO)
        )

    _page = _inject_shared(
        _HTML.replace("__GRAFANA_PORT__", cfg.grafana_port)
        .replace("__GRAFANA_UID__", cfg.grafana_uid)
        .replace("__SK_PORT__", cfg.sk_port),
        "/",
    )
    _history_page = _inject_shared(
        _HISTORY_HTML.replace("__GRAFANA_PORT__", cfg.grafana_port).replace(
            "__GRAFANA_UID__", cfg.grafana_uid
        ),
        "/history",
    )
    _admin_page = _inject_shared(_ADMIN_BOATS_HTML, "/admin/boats")
    _admin_cameras_page = _inject_shared(_ADMIN_CAMERAS_HTML, "/admin/cameras")

    from logger.auth import (
        _is_auth_disabled,
        _resolve_user,
        generate_token,
        invite_expires_at,
        require_auth,
        session_expires_at,
    )

    _PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/avatars"}

    async def _audit(
        request: Request,
        action: str,
        detail: str | None = None,
        user: dict[str, Any] | None = None,
    ) -> None:
        """Fire-and-forget audit log entry."""
        uid = user.get("id") if user else None
        ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")
        await storage.log_action(action, detail=detail, user_id=uid, ip_address=ip, user_agent=ua)

    @app.middleware("http")
    async def auth_middleware(  # type: ignore[no-untyped-def]
        request: Request,
        call_next,  # noqa: ANN001
    ) -> Response:
        path = request.url.path
        if _is_auth_disabled():
            from logger.auth import _MOCK_ADMIN

            request.state.user = _MOCK_ADMIN
            return await call_next(request)  # type: ignore[no-any-return]
        if path in _PUBLIC_PATHS or path.startswith("/notes/"):
            return await call_next(request)  # type: ignore[no-any-return]
        from http.cookies import SimpleCookie

        raw_cookie = request.headers.get("cookie", "")
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(raw_cookie)
        session_val = cookie["session"].value if "session" in cookie else None
        user = await _resolve_user(request, session=session_val)
        if user is None:
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                from starlette.responses import RedirectResponse as _RR

                return _RR(url=f"/login?next={path}", status_code=307)
            from starlette.responses import JSONResponse as _JR

            return _JR({"detail": "Not authenticated"}, status_code=401)
        request.state.user = user
        return await call_next(request)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Public routes: /healthz, /login, /logout
    # ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/api/me")
    async def api_me(request: Request) -> JSONResponse:
        """Return the current user's identity and role."""
        user: dict[str, Any] | None = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return JSONResponse(
            {
                "id": user.get("id"),
                "email": user.get("email"),
                "name": user.get("name"),
                "role": user.get("role"),
                "avatar_path": user.get("avatar_path"),
            }
        )

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(next: str = "/", token: str = "") -> HTMLResponse:
        html = _LOGIN_HTML.replace("__NEXT__", next).replace("__TOKEN__", token)
        return HTMLResponse(html)

    @app.post("/login", include_in_schema=False)
    @limiter.limit("10/minute")
    async def login_submit(
        request: Request,
        token: str = Form(...),
        next: str = Form(default="/"),
    ) -> Response:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        token = token.strip()
        row = await storage.get_invite_token(token)
        if row is None:
            return HTMLResponse(
                _LOGIN_HTML.replace("__NEXT__", next)
                .replace("__TOKEN__", token)
                .replace(
                    "<!--ERROR-->",
                    '<p style="color:#f87171;margin-top:12px">Invalid or expired token.</p>',
                ),
                status_code=400,
            )
        if row["used_at"] is not None:
            return HTMLResponse(
                _LOGIN_HTML.replace("__NEXT__", next)
                .replace("__TOKEN__", token)
                .replace(
                    "<!--ERROR-->",
                    '<p style="color:#f87171;margin-top:12px">Token already used.</p>',
                ),
                status_code=400,
            )
        expires_dt = _dt.fromisoformat(row["expires_at"])
        if _dt.now(_UTC) > expires_dt:
            return HTMLResponse(
                _LOGIN_HTML.replace("__NEXT__", next)
                .replace("__TOKEN__", token)
                .replace(
                    "<!--ERROR-->",
                    '<p style="color:#f87171;margin-top:12px">Token expired.</p>',
                ),
                status_code=400,
            )

        # Find or create the user
        user = await storage.get_user_by_email(row["email"])
        if user is None:
            user_id = await storage.create_user(row["email"], None, row["role"])
        else:
            user_id = user["id"]

        # Mark token used and create session
        await storage.redeem_invite_token(token)
        session_id = generate_token()
        await storage.create_session(
            session_id,
            user_id,
            session_expires_at(),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(url=next if next.startswith("/") else "/", status_code=303)
        response.set_cookie(
            "session",
            session_id,
            httponly=True,
            samesite="lax",
            max_age=int(os.getenv("AUTH_SESSION_TTL_DAYS", "90")) * 86400,
        )

        # Best-effort new-device alert email
        from logger.email import send_device_alert, smtp_configured

        if smtp_configured():
            asyncio.ensure_future(
                send_device_alert(
                    row["email"],
                    request.client.host if request.client else None,
                    request.headers.get("user-agent"),
                )
            )

        return response

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        from http.cookies import SimpleCookie

        raw_cookie = request.headers.get("cookie", "")
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(raw_cookie)
        if "session" in cookie:
            await storage.delete_session(cookie["session"].value)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("session")
        return response

    # ------------------------------------------------------------------
    # HTML UI
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_page)

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    async def history_page() -> HTMLResponse:
        return HTMLResponse(_history_page)

    @app.get("/admin/boats", response_class=HTMLResponse, include_in_schema=False)
    async def admin_boats_page() -> HTMLResponse:
        return HTMLResponse(_admin_page)

    @app.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
    async def admin_users_page(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> HTMLResponse:
        users = await storage.list_users()
        sessions = await storage.list_auth_sessions()
        await storage.delete_expired_sessions()
        return HTMLResponse(_render_admin_users_html(users, sessions))

    @app.post("/admin/users/invite", status_code=201, include_in_schema=False)
    @limiter.limit("5/minute")
    async def admin_invite_user(
        request: Request,
        email: str = Form(...),
        role: str = Form(...),
        name: str = Form(default=""),
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        if role not in ("admin", "crew", "viewer"):
            raise HTTPException(status_code=422, detail="Invalid role")
        email = email.strip().lower()
        if not email:
            raise HTTPException(status_code=422, detail="email must not be blank")
        clean_name = name.strip() or None
        token = generate_token()
        base = str(request.base_url).rstrip("/")
        await storage.create_invite_token(token, email, role, _user["id"], invite_expires_at())
        invite_url = f"{base}/login?token={token}"
        await _audit(request, "user.invite", detail=f"{email} as {role}", user=_user)

        from logger.email import send_welcome_email, smtp_configured

        email_sent = False
        if smtp_configured() and email:
            email_sent = await send_welcome_email(clean_name, email, role, invite_url)

        return JSONResponse(
            {"invite_url": invite_url, "token": token, "email_sent": email_sent},
            status_code=201,
        )

    @app.put("/admin/users/{user_id}/role", status_code=204, include_in_schema=False)
    async def admin_update_role(
        request: Request,
        user_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        role = (body.get("role") or "").strip()
        if role not in ("admin", "crew", "viewer"):
            raise HTTPException(status_code=422, detail="Invalid role")
        user = await storage.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        await storage.update_user_role(user_id, role)
        await _audit(request, "user.role", detail=f"user={user_id} role={role}", user=_user)

    @app.delete("/admin/sessions/{session_id}", status_code=204, include_in_schema=False)
    async def admin_revoke_session(
        request: Request,
        session_id: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        await storage.delete_session(session_id)
        await _audit(request, "session.revoke", detail=session_id[:16], user=_user)

    # ------------------------------------------------------------------
    # /admin/audit (#93)
    # ------------------------------------------------------------------

    @app.get("/admin/audit", response_class=HTMLResponse, include_in_schema=False)
    async def admin_audit_page(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> HTMLResponse:
        entries = await storage.list_audit_log(limit=200)
        return HTMLResponse(_render_admin_audit_html(entries))

    @app.get("/api/audit")
    async def api_audit_log(
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        entries = await storage.list_audit_log(limit=limit, offset=offset)
        return JSONResponse(entries)

    # ------------------------------------------------------------------
    # /admin/cameras (#98)
    # ------------------------------------------------------------------

    @app.get("/admin/cameras", response_class=HTMLResponse, include_in_schema=False)
    async def admin_cameras_page(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> HTMLResponse:
        return HTMLResponse(_admin_cameras_page)

    @app.get("/api/cameras")
    async def api_list_cameras(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """List configured cameras with live status."""
        if not cameras:
            return JSONResponse([])

        import logger.cameras as cameras_mod

        statuses = await asyncio.gather(
            *(cameras_mod.get_status(cam) for cam in cameras),
            return_exceptions=True,
        )
        result: list[dict[str, Any]] = []
        for cam, st in zip(cameras, statuses, strict=True):
            if isinstance(st, BaseException):
                result.append({
                    "name": cam.name, "ip": cam.ip, "model": cam.model,
                    "recording": False, "error": str(st),
                })
            else:
                result.append({
                    "name": st.name, "ip": st.ip, "model": cam.model,
                    "recording": st.recording, "error": st.error,
                })
        return JSONResponse(result)

    @app.post("/api/cameras/{camera_name}/start")
    async def api_start_camera(
        camera_name: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Manually start recording on a single camera."""
        import logger.cameras as cameras_mod

        cam = next((c for c in (cameras or []) if c.name == camera_name), None)
        if cam is None:
            raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
        status = await cameras_mod.start_camera(cam)
        return JSONResponse({
            "name": status.name, "ip": status.ip,
            "recording": status.recording, "error": status.error,
        })

    @app.post("/api/cameras/{camera_name}/stop")
    async def api_stop_camera(
        camera_name: str,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """Manually stop recording on a single camera."""
        import logger.cameras as cameras_mod

        cam = next((c for c in (cameras or []) if c.name == camera_name), None)
        if cam is None:
            raise HTTPException(404, detail=f"Camera {camera_name!r} not found")
        status = await cameras_mod.stop_camera(cam)
        return JSONResponse({
            "name": status.name, "ip": status.ip,
            "recording": status.recording, "error": status.error,
        })

    @app.get("/api/cameras/sessions")
    async def api_camera_sessions_all(
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        """List recent camera sessions across all cameras."""
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

    @app.get("/api/sessions/{session_id}/cameras")
    async def api_session_cameras(
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """List camera sessions for a specific race."""
        rows = await storage.list_camera_sessions(session_id)
        return JSONResponse(rows)

    # ------------------------------------------------------------------
    # /api/state
    # ------------------------------------------------------------------

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        from logger.races import Race as _Race
        from logger.races import configured_tz, default_event_for_date, local_today, local_weekday

        now = datetime.now(UTC)
        today = local_today()
        date_str = today.isoformat()
        weekday = local_weekday()

        default_event = default_event_for_date(today)
        custom_event = await storage.get_daily_event(date_str)

        if default_event is not None:
            event: str | None = default_event
            event_is_default = True
        elif custom_event is not None:
            event = custom_event
            event_is_default = False
        else:
            event = None
            event_is_default = False

        current = await storage.get_current_race()
        today_races = await storage.list_races_for_date(date_str)

        next_race_num = await storage.count_sessions_for_date(date_str, "race") + 1
        next_practice_num = await storage.count_sessions_for_date(date_str, "practice") + 1

        async def _race_dict(r: _Race) -> dict[str, Any]:
            duration_s: float | None = None
            if r.end_utc is not None:
                duration_s = (r.end_utc - r.start_utc).total_seconds()
            else:
                elapsed = (now - r.start_utc).total_seconds()
                duration_s = elapsed
            crew = await storage.get_race_crew(r.id)
            results = await storage.list_race_results(r.id)
            sails = await storage.get_race_sails(r.id)
            cur = await storage._conn().execute(
                "SELECT id FROM audio_sessions"
                " WHERE race_id = ? AND session_type IN ('race', 'practice') LIMIT 1",
                (r.id,),
            )
            audio_row = await cur.fetchone()
            audio_session_id: int | None = audio_row["id"] if audio_row else None
            return {
                "id": r.id,
                "name": r.name,
                "event": r.event,
                "race_num": r.race_num,
                "date": r.date,
                "start_utc": r.start_utc.isoformat(),
                "end_utc": r.end_utc.isoformat() if r.end_utc else None,
                "duration_s": round(duration_s, 1) if duration_s is not None else None,
                "session_type": r.session_type,
                "crew": crew,
                "results": results,
                "sails": sails,
                "has_audio": audio_session_id is not None,
                "audio_session_id": audio_session_id,
            }

        current_dict = await _race_dict(current) if current else None
        today_race_dicts = [await _race_dict(r) for r in today_races]

        return JSONResponse(
            {
                "date": date_str,
                "weekday": weekday,
                "timezone": str(configured_tz()),
                "event": event,
                "event_is_default": event_is_default,
                "current_race": current_dict,
                "next_race_num": next_race_num,
                "next_practice_num": next_practice_num,
                "today_races": today_race_dicts,
                "has_recorder": recorder is not None,
                "current_debrief": {
                    "race_id": _debrief_race_id,
                    "race_name": _debrief_race_name,
                    "start_utc": _debrief_start_utc.isoformat(),
                }
                if _debrief_race_id is not None
                else None,
            }
        )

    # ------------------------------------------------------------------
    # /api/instruments
    # ------------------------------------------------------------------

    @app.get("/api/instruments")
    async def api_instruments() -> JSONResponse:
        data = await storage.latest_instruments()
        return JSONResponse(data)

    # ------------------------------------------------------------------
    # /api/polar/current
    # ------------------------------------------------------------------

    @app.get("/api/polar/current")
    async def api_polar_current() -> JSONResponse:
        import logger.polar as _polar

        inst = await storage.latest_instruments()
        tws = inst.get("tws_kts")
        twa = inst.get("twa_deg")
        bsp = inst.get("bsp_kts")
        point = None
        if tws is not None and twa is not None:
            point = await _polar.lookup_polar(storage, float(tws), float(twa))
        baseline_bsp = point["mean_bsp"] if point else None
        delta = (
            round(float(bsp) - float(baseline_bsp), 2)
            if (bsp is not None and baseline_bsp is not None)
            else None
        )
        return JSONResponse(
            {
                "bsp": bsp,
                "tws": tws,
                "twa": twa,
                "baseline_bsp": baseline_bsp,
                "baseline_p90": point["p90_bsp"] if point else None,
                "delta": delta,
                "sufficient_data": point is not None,
            }
        )

    # ------------------------------------------------------------------
    # /api/event
    # ------------------------------------------------------------------

    @app.post("/api/event", status_code=204)
    async def api_set_event(
        request: Request,
        body: EventRequest,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        event_name = body.event_name.strip()
        if not event_name:
            raise HTTPException(status_code=422, detail="event_name must not be blank")
        from logger.races import local_today

        date_str = local_today().isoformat()
        await storage.set_daily_event(date_str, event_name)
        await _audit(request, "event.set", detail=event_name, user=_user)

    # ------------------------------------------------------------------
    # /api/races/start
    # ------------------------------------------------------------------

    @app.post("/api/races/start", status_code=201)
    async def api_start_race(
        request: Request,
        session_type: str = Query(default="race"),
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        nonlocal \
            _audio_session_id, \
            _debrief_audio_session_id, \
            _debrief_race_id, \
            _debrief_race_name, \
            _debrief_start_utc
        from logger.races import build_race_name, default_event_for_date

        if session_type not in ("race", "practice"):
            raise HTTPException(
                status_code=422,
                detail="session_type must be 'race' or 'practice'",
            )

        from logger.races import local_today

        now = datetime.now(UTC)
        today = local_today()
        date_str = today.isoformat()

        default_event = default_event_for_date(today)
        custom_event = await storage.get_daily_event(date_str)
        event = custom_event or default_event
        if event is None:
            raise HTTPException(
                status_code=422,
                detail="No event set for today. POST /api/event first.",
            )

        # Auto-stop any active debrief before starting a new session
        if _debrief_audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
            logger.info("Debrief auto-stopped to start new {}", session_type)
            _debrief_audio_session_id = None
            _debrief_race_id = None
            _debrief_race_name = None
            _debrief_start_utc = None

        race_num = await storage.count_sessions_for_date(date_str, session_type) + 1
        name = build_race_name(event, today, race_num, session_type)

        race = await storage.start_race(event, now, date_str, race_num, name, session_type)

        # Copy crew from most recently closed session as defaults
        last_crew = await storage.get_last_session_crew()
        if last_crew:
            await storage.set_race_crew(race.id, last_crew)
            logger.info("Crew carried forward to {}: {} positions", race.name, len(last_crew))

        if recorder is not None and audio_config is not None:
            from logger.audio import AudioDeviceNotFoundError

            try:
                session = await recorder.start(audio_config, name=race.name)
                _audio_session_id = await storage.write_audio_session(
                    session,
                    race_id=race.id,
                    session_type=session_type,
                    name=race.name,
                )
                logger.info("Audio recording started: {}", session.file_path)
            except AudioDeviceNotFoundError as exc:
                logger.warning("Audio unavailable for race {}: {}", race.name, exc)

        if cameras:
            import logger.cameras as cameras_mod

            try:
                statuses = await cameras_mod.start_all(
                    cameras, race.id, storage
                )
                for s in statuses:
                    if s.error:
                        logger.warning("Camera {} failed to start: {}", s.name, s.error)
                    else:
                        logger.info("Camera {} recording started", s.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera start_all failed: {}", exc)

        await _audit(request, "race.start", detail=race.name, user=_user)
        return JSONResponse(
            {
                "id": race.id,
                "name": race.name,
                "event": race.event,
                "race_num": race.race_num,
                "start_utc": race.start_utc.isoformat(),
                "session_type": race.session_type,
            },
            status_code=201,
        )

    # ------------------------------------------------------------------
    # /api/races/{id}/end
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/end", status_code=204)
    async def api_end_race(
        request: Request,
        race_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        nonlocal _audio_session_id
        now = datetime.now(UTC)
        await storage.end_race(race_id, now)
        await _audit(request, "race.end", detail=str(race_id), user=_user)

        if cameras:
            import logger.cameras as cameras_mod

            try:
                statuses = await cameras_mod.stop_all(
                    cameras, race_id, storage
                )
                for s in statuses:
                    if s.error:
                        logger.warning("Camera {} failed to stop: {}", s.name, s.error)
                    else:
                        logger.info("Camera {} recording stopped", s.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera stop_all failed: {}", exc)

        if recorder is not None and _audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_audio_session_id, completed.end_utc)
            logger.info("Audio recording saved: {}", completed.file_path)
            _audio_session_id = None

    # ------------------------------------------------------------------
    # /api/races/{id}/debrief/start
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/debrief/start", status_code=201)
    async def api_start_debrief(
        request: Request,
        race_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        nonlocal \
            _audio_session_id, \
            _debrief_audio_session_id, \
            _debrief_race_id, \
            _debrief_race_name, \
            _debrief_start_utc

        if recorder is None or audio_config is None:
            raise HTTPException(status_code=409, detail="No audio recorder configured")

        cur = await storage._conn().execute(
            "SELECT id, name, end_utc FROM races WHERE id = ?", (race_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Race not found")

        # Defensive: if the race is still in progress, auto-end it first
        if row["end_utc"] is None:
            now_end = datetime.now(UTC)
            await storage.end_race(race_id, now_end)
            if _audio_session_id is not None:
                completed = await recorder.stop()
                assert completed.end_utc is not None
                await storage.update_audio_session_end(_audio_session_id, completed.end_utc)
                _audio_session_id = None
            logger.info("Race {} auto-ended to start debrief", race_id)

        if _debrief_audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
            _debrief_audio_session_id = None

        debrief_name = f"{row['name']}-debrief"
        now = datetime.now(UTC)
        session = await recorder.start(audio_config, name=debrief_name)
        _debrief_audio_session_id = await storage.write_audio_session(
            session,
            race_id=race_id,
            session_type="debrief",
            name=debrief_name,
        )
        _debrief_race_id = race_id
        _debrief_race_name = row["name"]
        _debrief_start_utc = now
        logger.info("Debrief recording started: {}", session.file_path)

        await _audit(request, "debrief.start", detail=row["name"], user=_user)
        return JSONResponse(
            {"race_id": race_id, "race_name": row["name"], "start_utc": now.isoformat()},
            status_code=201,
        )

    # ------------------------------------------------------------------
    # /api/debrief/stop
    # ------------------------------------------------------------------

    @app.post("/api/debrief/stop", status_code=204)
    async def api_stop_debrief(
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        nonlocal _debrief_audio_session_id, _debrief_race_id, _debrief_race_name, _debrief_start_utc

        if _debrief_audio_session_id is None:
            raise HTTPException(status_code=409, detail="No debrief in progress")

        completed = await recorder.stop()
        assert completed.end_utc is not None
        await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
        logger.info("Debrief recording saved: {}", completed.file_path)

        await _audit(request, "debrief.stop", user=_user)
        _debrief_audio_session_id = None
        _debrief_race_id = None
        _debrief_race_name = None
        _debrief_start_utc = None

    # ------------------------------------------------------------------
    # /api/races/{id}/export.{fmt}
    # ------------------------------------------------------------------

    @app.get("/api/races/{race_id}/export.{fmt}")
    async def api_export_race(race_id: int, fmt: str) -> FileResponse:
        if fmt not in ("csv", "gpx", "json"):
            raise HTTPException(status_code=400, detail="fmt must be csv, gpx, or json")

        from logger.races import local_today

        races = await storage.list_races_for_date(local_today().isoformat())
        # Also search across all dates by fetching by id directly
        race = None
        for r in races:
            if r.id == race_id:
                race = r
                break

        if race is None:
            # Fallback: search all races (no date filter)
            cur = await storage._conn().execute(
                "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
                " FROM races WHERE id = ?",
                (race_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Race not found")
            from datetime import datetime as _dt

            from logger.races import Race

            race = Race(
                id=row["id"],
                name=row["name"],
                event=row["event"],
                race_num=row["race_num"],
                date=row["date"],
                start_utc=_dt.fromisoformat(row["start_utc"]),
                end_utc=_dt.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                session_type=row["session_type"],
            )

        if race.end_utc is None:
            raise HTTPException(status_code=409, detail="Race is still in progress")

        from logger.export import export_to_file

        suffix = f".{fmt}"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            out_path = f.name

        await export_to_file(storage, race.start_utc, race.end_utc, out_path)

        filename = f"{race.name}.{fmt}"
        media = {
            "csv": "text/csv",
            "gpx": "application/gpx+xml",
            "json": "application/json",
        }[fmt]
        return FileResponse(
            out_path,
            media_type=media,
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------
    # /api/sessions  (history browser)
    # ------------------------------------------------------------------

    @app.get("/api/sessions")
    async def api_sessions(
        q: str | None = None,
        type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> JSONResponse:
        if type is not None and type not in ("race", "practice", "debrief"):
            raise HTTPException(
                status_code=422,
                detail="type must be 'race', 'practice', or 'debrief'",
            )
        limit = max(1, min(limit, 200))
        total, sessions = await storage.list_sessions(
            q=q or None,
            session_type=type,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"total": total, "sessions": sessions})

    # ------------------------------------------------------------------
    # /api/races
    # ------------------------------------------------------------------

    @app.get("/api/races")
    async def api_list_races(date: str | None = None) -> JSONResponse:
        if date is None:
            from logger.races import local_today

            date = local_today().isoformat()
        races = await storage.list_races_for_date(date)
        result = []
        for r in races:
            duration_s: float | None = None
            if r.end_utc is not None:
                duration_s = (r.end_utc - r.start_utc).total_seconds()
            result.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "event": r.event,
                    "race_num": r.race_num,
                    "date": r.date,
                    "start_utc": r.start_utc.isoformat(),
                    "end_utc": r.end_utc.isoformat() if r.end_utc else None,
                    "duration_s": round(duration_s, 1) if duration_s is not None else None,
                }
            )
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # /api/races/{id}/crew
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/crew", status_code=204)
    async def api_set_crew(
        request: Request,
        race_id: int,
        body: list[CrewEntry],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Race not found")

        invalid = [e.position for e in body if e.position not in POSITIONS]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown position(s): {invalid}. Must be one of {list(POSITIONS)}",
            )

        crew = [{"position": e.position, "sailor": e.sailor} for e in body if e.sailor.strip()]
        await storage.set_race_crew(race_id, crew)
        await _audit(request, "crew.set", detail=str(race_id), user=_user)

    @app.get("/api/races/{race_id}/crew")
    async def api_get_crew(race_id: int) -> JSONResponse:
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Race not found")

        crew = await storage.get_race_crew(race_id)
        recent = await storage.get_recent_sailors()
        return JSONResponse({"crew": crew, "recent_sailors": recent})

    # ------------------------------------------------------------------
    # /api/sailors/recent
    # ------------------------------------------------------------------

    @app.get("/api/sailors/recent")
    async def api_recent_sailors() -> JSONResponse:
        sailors = await storage.get_recent_sailors()
        return JSONResponse({"sailors": sailors})

    # ------------------------------------------------------------------
    # /api/boats
    # ------------------------------------------------------------------

    @app.get("/api/boats")
    async def api_list_boats(
        q: str | None = None,
        exclude_race: int | None = None,
    ) -> JSONResponse:
        boats = await storage.list_boats(exclude_race_id=exclude_race, q=q or None)
        return JSONResponse(boats)

    @app.post("/api/boats", status_code=201)
    async def api_create_boat(
        request: Request,
        body: BoatCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        sail = body.sail_number.strip()
        if not sail:
            raise HTTPException(status_code=422, detail="sail_number must not be blank")
        boat_id = await storage.add_boat(sail, body.name, body.class_name)
        await _audit(request, "boat.add", detail=sail, user=_user)
        return JSONResponse({"id": boat_id}, status_code=201)

    @app.patch("/api/boats/{boat_id}", status_code=204)
    async def api_update_boat(
        request: Request,
        boat_id: int,
        body: BoatUpdate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute(
            "SELECT sail_number, name, class FROM boats WHERE id = ?", (boat_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Boat not found")
        sail = (body.sail_number or "").strip() or row["sail_number"]
        name = body.name if body.name is not None else row["name"]
        class_name = body.class_name if body.class_name is not None else row["class"]
        await storage.update_boat(boat_id, sail, name, class_name)
        await _audit(request, "boat.update", detail=str(boat_id), user=_user)

    @app.delete("/api/boats/{boat_id}", status_code=204)
    async def api_delete_boat(
        request: Request,
        boat_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute("SELECT id FROM boats WHERE id = ?", (boat_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Boat not found")
        await storage.delete_boat(boat_id)
        await _audit(request, "boat.delete", detail=str(boat_id), user=_user)

    # ------------------------------------------------------------------
    # /api/sessions/{race_id}/results
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{race_id}/results")
    async def api_get_results(race_id: int) -> JSONResponse:
        results = await storage.list_race_results(race_id)
        return JSONResponse(results)

    @app.post("/api/sessions/{race_id}/results", status_code=201)
    async def api_upsert_result(
        request: Request,
        race_id: int,
        body: RaceResultEntry,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (race_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Race not found")

        if body.place < 1:
            raise HTTPException(status_code=422, detail="place must be >= 1")

        if body.boat_id is not None:
            boat_id = body.boat_id
            # Verify boat exists
            cur2 = await storage._conn().execute("SELECT id FROM boats WHERE id = ?", (boat_id,))
            if await cur2.fetchone() is None:
                raise HTTPException(status_code=404, detail="Boat not found")
        elif body.sail_number:
            boat_id = await storage.find_or_create_boat(body.sail_number)
        else:
            raise HTTPException(status_code=422, detail="boat_id or sail_number is required")

        result_id = await storage.upsert_race_result(
            race_id,
            body.place,
            boat_id,
            finish_time=body.finish_time,
            dnf=body.dnf,
            dns=body.dns,
            notes=body.notes,
        )
        await _audit(
            request, "result.upsert", detail=f"race={race_id} place={body.place}", user=_user
        )
        return JSONResponse({"id": result_id}, status_code=201)

    # ------------------------------------------------------------------
    # /api/results/{result_id}
    # ------------------------------------------------------------------

    @app.delete("/api/results/{result_id}", status_code=204)
    async def api_delete_result(
        request: Request,
        result_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        cur = await storage._conn().execute(
            "SELECT id FROM race_results WHERE id = ?", (result_id,)
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Result not found")
        await storage.delete_race_result(result_id)
        await _audit(request, "result.delete", detail=str(result_id), user=_user)

    # ------------------------------------------------------------------
    # /api/sessions/{session_id}/notes  &  /api/notes/{note_id}
    # ------------------------------------------------------------------

    async def _resolve_session(session_id: int) -> tuple[int | None, int | None]:
        """Return (race_id, audio_session_id) for the given session_id, or raise 404."""
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is not None:
            return session_id, None
        cur2 = await storage._conn().execute(
            "SELECT id FROM audio_sessions WHERE id = ?", (session_id,)
        )
        if await cur2.fetchone() is not None:
            return None, session_id
        raise HTTPException(status_code=404, detail="Session not found")

    @app.post("/api/sessions/{session_id}/notes", status_code=201)
    async def api_create_note(
        request: Request,
        session_id: int,
        body: NoteCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        if body.note_type not in ("text", "settings"):
            raise HTTPException(status_code=422, detail="note_type must be 'text' or 'settings'")
        if body.note_type == "text" and (not body.body or not body.body.strip()):
            raise HTTPException(status_code=422, detail="body must not be blank for text notes")
        if body.note_type == "settings":
            if not body.body:
                raise HTTPException(
                    status_code=422, detail="body must not be blank for settings notes"
                )
            try:
                parsed = json.loads(body.body)
                if not isinstance(parsed, dict):
                    raise ValueError  # noqa: TRY301
            except (json.JSONDecodeError, ValueError):
                raise HTTPException(  # noqa: B904
                    status_code=422,
                    detail="body must be a JSON object for settings notes",
                )
        race_id, audio_session_id = await _resolve_session(session_id)
        ts = body.ts if body.ts else datetime.now(UTC).isoformat()
        note_id = await storage.create_note(
            ts,
            body.body,
            race_id=race_id,
            audio_session_id=audio_session_id,
            note_type=body.note_type,
            user_id=_user.get("id"),
        )
        from logger import influx

        await asyncio.to_thread(
            influx.write_note,
            ts_iso=ts,
            note_type=body.note_type,
            body=body.body,
            race_id=race_id,
            note_id=note_id,
        )
        await _audit(request, "note.add", detail=body.note_type, user=_user)
        return JSONResponse({"id": note_id, "ts": ts}, status_code=201)

    @app.post("/api/sessions/{session_id}/notes/photo", status_code=201)
    async def api_create_photo_note(
        request: Request,
        session_id: int,
        file: UploadFile,
        ts: str = Form(default=""),
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        race_id, audio_session_id = await _resolve_session(session_id)

        notes_dir = os.environ.get("NOTES_DIR", "data/notes")
        session_dir = Path(notes_dir) / str(session_id)
        await asyncio.to_thread(session_dir.mkdir, parents=True, exist_ok=True)

        now_str = datetime.now(UTC).isoformat()
        actual_ts = ts.strip() if ts.strip() else now_str
        safe_ts = actual_ts.replace(":", "-").replace("+", "")[:19]
        ext = Path(file.filename or "photo.jpg").suffix or ".jpg"
        filename = f"{safe_ts}_{uuid.uuid4().hex[:8]}{ext}"
        dest = session_dir / filename

        data = await file.read()
        await asyncio.to_thread(dest.write_bytes, data)

        photo_path = f"{session_id}/{filename}"
        note_id = await storage.create_note(
            actual_ts,
            None,
            race_id=race_id,
            audio_session_id=audio_session_id,
            note_type="photo",
            photo_path=photo_path,
            user_id=_user.get("id"),
        )
        from logger import influx

        await asyncio.to_thread(
            influx.write_note,
            ts_iso=actual_ts,
            note_type="photo",
            body=f"/notes/{photo_path}",
            race_id=race_id,
            note_id=note_id,
        )
        await _audit(request, "note.photo", detail=photo_path, user=_user)
        return JSONResponse(
            {"id": note_id, "ts": actual_ts, "photo_path": photo_path}, status_code=201
        )

    @app.get("/notes/{path:path}")
    async def serve_note_photo(
        path: str,
        request: Request,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> Response:
        notes_dir = Path(os.environ.get("NOTES_DIR", "data/notes")).resolve()
        full_path = (notes_dir / path).resolve()
        if not str(full_path).startswith(str(notes_dir)):
            raise HTTPException(status_code=403, detail="Forbidden")
        if not full_path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        st = full_path.stat()
        etag = f'"{st.st_mtime_ns}-{st.st_size}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304)
        return FileResponse(
            full_path,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": etag,
            },
        )

    @app.get("/api/sessions/{session_id}/notes")
    async def api_list_notes(session_id: int) -> JSONResponse:
        race_id, audio_session_id = await _resolve_session(session_id)
        notes = await storage.list_notes(race_id=race_id, audio_session_id=audio_session_id)
        return JSONResponse(notes)

    @app.delete("/api/notes/{note_id}", status_code=204)
    async def api_delete_note(
        request: Request,
        note_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        found = await storage.delete_note(note_id)
        if not found:
            raise HTTPException(status_code=404, detail="Note not found")
        await _audit(request, "note.delete", detail=str(note_id), user=_user)

    @app.get("/api/notes/settings-keys")
    async def api_settings_keys() -> JSONResponse:
        """Return all distinct keys used in settings notes, sorted alphabetically.

        Used to populate the typeahead datalist on the settings note entry form.
        Returns: {"keys": ["backstay", "cunningham", ...]}
        """
        keys = await storage.list_settings_keys()
        return JSONResponse({"keys": keys})

    # ------------------------------------------------------------------
    # /api/sessions/{session_id}/videos  &  /api/videos/{video_id}
    # ------------------------------------------------------------------

    def _video_deep_link(row: dict[str, Any], at_utc: datetime | None = None) -> dict[str, Any]:
        """Augment a race_videos row with a computed YouTube deep-link.

        If *at_utc* is supplied the link jumps to that moment in the video.
        Otherwise the link just opens the video from the beginning.
        """
        from logger.video import VideoSession  # local import to avoid circular deps

        sync_utc = datetime.fromisoformat(row["sync_utc"])
        duration_s = row["duration_s"]

        out = dict(row)
        if at_utc is not None and duration_s is not None:
            vs = VideoSession(
                url=row["youtube_url"],
                video_id=row["video_id"],
                title=row["title"],
                duration_s=duration_s,
                sync_utc=sync_utc,
                sync_offset_s=row["sync_offset_s"],
            )
            out["deep_link"] = vs.url_at(at_utc)
        else:
            out["deep_link"] = None
        return out

    @app.get("/api/sessions/{session_id}/videos")
    async def api_list_videos(
        session_id: int,
        at: str | None = None,
    ) -> JSONResponse:
        """List videos linked to a session.

        Optional ``?at=<UTC ISO 8601>`` param computes a deep-link to that
        moment in each video.
        """
        # Videos are only supported on races (not audio sessions).
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Session not found")
        rows = await storage.list_race_videos(session_id)
        at_utc: datetime | None = None
        if at:
            try:
                at_utc = datetime.fromisoformat(at)
                if at_utc.tzinfo is None:
                    at_utc = at_utc.replace(tzinfo=UTC)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
        return JSONResponse([_video_deep_link(r, at_utc) for r in rows])

    @app.get("/api/sessions/{session_id}/videos/redirect")
    async def api_videos_redirect(
        session_id: int,
        at: str | None = None,
    ) -> RedirectResponse:
        """Redirect to the YouTube deep-link for a specific moment in the session's first video.

        Returns ``302 Location`` to the computed YouTube URL (with ``?t=<seconds>``).
        Returns ``404`` if the session doesn't exist or has no linked videos.
        Returns ``422`` if ``at`` is missing or cannot be parsed.
        """
        if not at:
            raise HTTPException(status_code=422, detail="'at' query parameter is required")
        try:
            at_utc = datetime.fromisoformat(at)
            if at_utc.tzinfo is None:
                at_utc = at_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Session not found")
        rows = await storage.list_race_videos(session_id)
        if not rows:
            raise HTTPException(status_code=404, detail="No videos linked to this session")
        # Use the first video by created_at (list_race_videos returns ASC order).
        row = rows[0]
        enriched = _video_deep_link(row, at_utc)
        url = enriched["deep_link"] or row["youtube_url"]
        return RedirectResponse(url=url, status_code=302)

    @app.get("/api/videos/redirect")
    async def api_videos_redirect_by_time(
        at: str | None = None,
    ) -> RedirectResponse:
        """Resolve the race active at ``at`` and redirect to its first video.

        Designed for Grafana Data Links — no session_id required.  Grafana
        passes ``${__value.time:date:iso}`` as the ``at`` parameter and this
        endpoint resolves the correct race automatically.

        Returns ``302 Location`` to the YouTube deep-link with ``?t=<seconds>``.
        Returns ``404`` if no race covers that timestamp or the race has no video.
        Returns ``422`` if ``at`` is missing or cannot be parsed.
        """
        if not at:
            raise HTTPException(status_code=422, detail="'at' query parameter is required")
        try:
            at_utc = datetime.fromisoformat(at)
            if at_utc.tzinfo is None:
                at_utc = at_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid 'at' timestamp")  # noqa: B904

        at_iso = at_utc.isoformat()
        cur = await storage._conn().execute(
            """
            SELECT id FROM races
            WHERE start_utc <= ?
              AND (end_utc >= ? OR end_utc IS NULL)
            ORDER BY start_utc DESC
            LIMIT 1
            """,
            (at_iso, at_iso),
        )
        race_row = await cur.fetchone()
        if race_row is None:
            raise HTTPException(status_code=404, detail="No race found at this timestamp")

        session_id = race_row["id"]
        rows = await storage.list_race_videos(session_id)
        if not rows:
            raise HTTPException(status_code=404, detail="No videos linked to this session")

        row = rows[0]
        enriched = _video_deep_link(row, at_utc)
        url = enriched["deep_link"] or row["youtube_url"]
        return RedirectResponse(url=url, status_code=302)

    @app.post("/api/sessions/{session_id}/videos", status_code=201)
    async def api_add_video(
        request: Request,
        session_id: int,
        body: VideoCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Link a YouTube video to a race session.

        The caller supplies a sync point: a UTC wall-clock time and the
        corresponding video player position (seconds).  This pins the video
        timeline to logger time.
        """
        cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # Parse the sync UTC
        try:
            sync_utc = datetime.fromisoformat(body.sync_utc)
            if sync_utc.tzinfo is None:
                sync_utc = sync_utc.replace(tzinfo=UTC)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904

        # Extract YouTube video ID and fetch metadata via yt-dlp if available
        from logger.video import VideoLinker

        video_id = ""
        title = ""
        duration_s: float | None = None
        try:
            linker = VideoLinker()
            vs = await linker.create_session(body.youtube_url, sync_utc, body.sync_offset_s)
            video_id = vs.video_id
            title = vs.title
            duration_s = vs.duration_s
        except Exception:  # noqa: BLE001
            # yt-dlp unavailable or network error — store the URL as-is.
            # Extract video ID from URL heuristically.
            import re

            m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", body.youtube_url)
            video_id = m.group(1) if m else ""
            title = ""
            duration_s = None

        row_id = await storage.add_race_video(
            race_id=session_id,
            youtube_url=body.youtube_url,
            video_id=video_id,
            title=title,
            label=body.label,
            sync_utc=sync_utc,
            sync_offset_s=body.sync_offset_s,
            duration_s=duration_s,
            user_id=_user.get("id"),
        )
        rows = await storage.list_race_videos(session_id)
        row = next(r for r in rows if r["id"] == row_id)
        await _audit(request, "video.add", detail=body.youtube_url, user=_user)
        return JSONResponse(_video_deep_link(row), status_code=201)

    @app.patch("/api/videos/{video_id}", status_code=200)
    async def api_update_video(
        request: Request,
        video_id: int,
        body: VideoUpdate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Update label or sync calibration on an existing video link."""
        sync_utc: datetime | None = None
        if body.sync_utc is not None:
            try:
                sync_utc = datetime.fromisoformat(body.sync_utc)
                if sync_utc.tzinfo is None:
                    sync_utc = sync_utc.replace(tzinfo=UTC)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid sync_utc timestamp")  # noqa: B904
        found = await storage.update_race_video(
            video_id,
            label=body.label,
            sync_utc=sync_utc,
            sync_offset_s=body.sync_offset_s,
        )
        if not found:
            raise HTTPException(status_code=404, detail="Video not found")
        await _audit(request, "video.update", detail=str(video_id), user=_user)
        return JSONResponse({"id": video_id, "updated": True})

    @app.delete("/api/videos/{video_id}", status_code=204)
    async def api_delete_video(
        request: Request,
        video_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        """Remove a video link."""
        found = await storage.delete_race_video(video_id)
        if not found:
            raise HTTPException(status_code=404, detail="Video not found")
        await _audit(request, "video.delete", detail=str(video_id), user=_user)

    # ------------------------------------------------------------------
    # /api/grafana/annotations
    # ------------------------------------------------------------------

    @app.get("/api/grafana/annotations")
    async def api_grafana_annotations(
        from_: int | None = Query(default=None, alias="from"),
        to: int | None = None,
        sessionId: int | None = None,  # noqa: N803
    ) -> JSONResponse:
        """Grafana SimpleJSON annotation feed.

        Grafana passes epoch milliseconds as ``from`` and ``to``.
        Optional ``sessionId`` scopes results to a single race or practice.
        """
        if from_ is None or to is None:
            return JSONResponse([])
        start = datetime.fromtimestamp(from_ / 1000.0, tz=UTC)
        end = datetime.fromtimestamp(to / 1000.0, tz=UTC)
        race_id: int | None = None
        audio_session_id: int | None = None
        if sessionId is not None:
            race_id, audio_session_id = await _resolve_session(sessionId)
        notes = await storage.list_notes_range(
            start, end, race_id=race_id, audio_session_id=audio_session_id
        )
        result = []
        for n in notes:
            ts_ms = int(datetime.fromisoformat(n["ts"]).timestamp() * 1000)
            text = n["body"] or ""
            if n["note_type"] == "photo" and n.get("photo_path"):
                photo_url = f"/notes/{n['photo_path']}"
                text = f'<img src="{photo_url}" style="max-width:300px"/>'
                if n["body"]:
                    text = n["body"] + "<br/>" + text
            result.append(
                {
                    "time": ts_ms,
                    "timeEnd": ts_ms,
                    "title": n["note_type"].capitalize(),
                    "text": text,
                    "tags": [n["note_type"]],
                }
            )
        return JSONResponse(result, headers={"Access-Control-Allow-Origin": "*"})

    # ------------------------------------------------------------------
    # /api/sails  &  /api/sessions/{id}/sails
    # ------------------------------------------------------------------

    from logger.storage import _SAIL_TYPES  # noqa: PLC0415

    @app.get("/api/sails")
    async def api_list_sails() -> JSONResponse:
        """Return active sails grouped by type."""
        all_sails = await storage.list_sails(include_inactive=False)
        grouped: dict[str, list[dict[str, Any]]] = {t: [] for t in _SAIL_TYPES}
        for s in all_sails:
            if s["type"] in grouped:
                grouped[s["type"]].append(s)
        return JSONResponse(grouped)

    @app.post("/api/sails", status_code=201)
    async def api_add_sail(
        request: Request,
        body: SailCreate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Add a sail to the inventory."""
        if body.type not in _SAIL_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown sail type {body.type!r}. Must be one of {list(_SAIL_TYPES)}",
            )
        if not body.name.strip():
            raise HTTPException(status_code=422, detail="name must not be blank")
        try:
            sail_id = await storage.add_sail(body.type, body.name, body.notes)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Sail already exists: type={body.type!r} name={body.name!r}",
            ) from exc
        await _audit(request, "sail.add", detail=f"{body.type}/{body.name}", user=_user)
        return JSONResponse(
            {"id": sail_id, "type": body.type, "name": body.name.strip()}, status_code=201
        )

    @app.patch("/api/sails/{sail_id}", status_code=200)
    async def api_update_sail(
        request: Request,
        sail_id: int,
        body: SailUpdate,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Update sail name/notes or retire it."""
        found = await storage.update_sail(
            sail_id, name=body.name, notes=body.notes, active=body.active
        )
        if not found:
            raise HTTPException(status_code=404, detail="Sail not found")
        await _audit(request, "sail.update", detail=str(sail_id), user=_user)
        return JSONResponse({"id": sail_id, "updated": True})

    @app.get("/api/sessions/{session_id}/sails")
    async def api_get_session_sails(session_id: int) -> JSONResponse:
        """Return the sail selection for a race/practice session."""
        race = await storage.get_race(session_id)
        if race is None:
            raise HTTPException(status_code=404, detail="Session not found")
        sails = await storage.get_race_sails(session_id)
        return JSONResponse(sails)

    @app.put("/api/sessions/{session_id}/sails", status_code=200)
    async def api_set_session_sails(
        request: Request,
        session_id: int,
        body: RaceSailsSet,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Set the sail selection for a race/practice session."""
        race = await storage.get_race(session_id)
        if race is None:
            raise HTTPException(status_code=404, detail="Session not found")

        # Validate that each supplied sail_id references a sail of the correct type
        slot_map = {"main": body.main_id, "jib": body.jib_id, "spinnaker": body.spinnaker_id}
        for slot_type, sail_id in slot_map.items():
            if sail_id is None:
                continue
            all_sails = await storage.list_sails(include_inactive=True)
            matched = next((s for s in all_sails if s["id"] == sail_id), None)
            if matched is None:
                raise HTTPException(status_code=422, detail=f"Sail id={sail_id} not found")
            if matched["type"] != slot_type:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Sail id={sail_id} has type {matched['type']!r},"
                        f" expected {slot_type!r} for the {slot_type} slot"
                    ),
                )

        await storage.set_race_sails(
            session_id,
            main_id=body.main_id,
            jib_id=body.jib_id,
            spinnaker_id=body.spinnaker_id,
        )
        sails = await storage.get_race_sails(session_id)
        await _audit(request, "sails.set", detail=str(session_id), user=_user)
        return JSONResponse(sails)

    # ------------------------------------------------------------------
    # /api/audio/{session_id}/download  &  /api/audio/{session_id}/stream
    # ------------------------------------------------------------------

    @app.get("/api/audio/{session_id}/download")
    async def download_audio(session_id: int) -> FileResponse:
        """Download a WAV file as an attachment."""
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        path = Path(row["file_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found on disk")
        return FileResponse(
            path,
            media_type="audio/wav",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )

    @app.get("/api/audio/{session_id}/stream")
    async def stream_audio(session_id: int) -> FileResponse:
        """Stream a WAV file; Starlette handles Range headers for seekable playback."""
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        path = Path(row["file_path"])
        if not path.exists():
            raise HTTPException(status_code=404, detail="Audio file not found on disk")
        return FileResponse(path, media_type="audio/wav")

    # ------------------------------------------------------------------
    # /api/system-health
    # ------------------------------------------------------------------

    @app.get("/api/system-health")
    async def api_system_health() -> JSONResponse:
        """Return current CPU, memory, and disk utilisation percentages."""
        import psutil  # type: ignore[import-untyped]

        cpu = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        payload: dict[str, float | None] = {
            "cpu_pct": cpu,
            "mem_pct": mem.percent,
            "disk_pct": disk.percent,
        }
        temp_c: float | None = None
        get_temps = getattr(psutil, "sensors_temperatures", None)
        if get_temps is not None:
            temps: dict[str, list[object]] = get_temps()
            for entries in temps.values():
                if entries:
                    current = getattr(entries[0], "current", None)
                    if current is not None:
                        temp_c = float(current)
                    break
        payload["cpu_temp_c"] = temp_c
        return JSONResponse(payload)

    # ------------------------------------------------------------------
    # /api/audio/{session_id}/transcribe  &  /api/audio/{session_id}/transcript
    # ------------------------------------------------------------------

    @app.post("/api/audio/{session_id}/transcribe", status_code=202)
    async def api_transcribe(
        request: Request,
        session_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        """Trigger a transcription job for an audio session (202 Accepted).

        If a job already exists, returns 409 Conflict.
        """
        row = await storage.get_audio_session_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Audio session not found")
        model = os.environ.get("WHISPER_MODEL", "base")
        try:
            transcript_id = await storage.create_transcript_job(session_id, model)
        except ValueError:
            raise HTTPException(  # noqa: B904
                status_code=409, detail="Transcript job already exists for this session"
            )

        from logger.transcribe import transcribe_session

        diarize = bool(os.environ.get("HF_TOKEN"))
        asyncio.create_task(
            transcribe_session(
                storage, session_id, transcript_id, model_size=model, diarize=diarize
            )
        )
        await _audit(request, "transcribe.start", detail=str(session_id), user=_user)
        return JSONResponse({"status": "accepted", "transcript_id": transcript_id}, status_code=202)

    @app.get("/api/audio/{session_id}/transcript")
    async def api_get_transcript(session_id: int) -> JSONResponse:
        """Poll transcription status and retrieve the transcript text when done."""
        import json as _json

        t = await storage.get_transcript(session_id)
        if t is None:
            raise HTTPException(status_code=404, detail="No transcript job found for this session")
        if t.get("segments_json"):
            t["segments"] = _json.loads(t["segments_json"])
        del t["segments_json"]
        return JSONResponse(t)

    # ------------------------------------------------------------------
    # Tags (#99)
    # ------------------------------------------------------------------

    @app.get("/api/tags")
    async def api_list_tags() -> JSONResponse:
        tags = await storage.list_tags()
        return JSONResponse(tags)

    @app.post("/api/tags", status_code=201)
    async def api_create_tag(
        request: Request,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        name = (body.get("name") or "").strip().lower()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        color = body.get("color")
        tag = await storage.get_tag_by_name(name)
        if tag:
            raise HTTPException(status_code=409, detail="Tag already exists")
        tag_id = await storage.create_tag(name, color)
        await _audit(request, "tag.create", detail=name, user=_user)
        return JSONResponse({"id": tag_id, "name": name, "color": color}, status_code=201)

    @app.patch("/api/tags/{tag_id}", status_code=200)
    async def api_update_tag(
        request: Request,
        tag_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> JSONResponse:
        found = await storage.update_tag(tag_id, name=body.get("name"), color=body.get("color"))
        if not found:
            raise HTTPException(status_code=404, detail="Tag not found")
        await _audit(request, "tag.update", detail=str(tag_id), user=_user)
        return JSONResponse({"id": tag_id, "updated": True})

    @app.delete("/api/tags/{tag_id}", status_code=204)
    async def api_delete_tag(
        request: Request,
        tag_id: int,
        _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
    ) -> None:
        found = await storage.delete_tag(tag_id)
        if not found:
            raise HTTPException(status_code=404, detail="Tag not found")
        await _audit(request, "tag.delete", detail=str(tag_id), user=_user)

    @app.post("/api/sessions/{session_id}/tags", status_code=201)
    async def api_add_session_tag(
        request: Request,
        session_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        tag_id = body.get("tag_id")
        tag_name = body.get("tag_name")
        if tag_id is None and not tag_name:
            raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
        if tag_id is None:
            assert isinstance(tag_name, str)
            tag_id = await storage.get_or_create_tag(tag_name)
        await storage.add_session_tag(session_id, tag_id)
        await _audit(
            request, "session.tag.add", detail=f"session={session_id} tag={tag_id}", user=_user
        )
        return JSONResponse({"session_id": session_id, "tag_id": tag_id}, status_code=201)

    @app.delete("/api/sessions/{session_id}/tags/{tag_id}", status_code=204)
    async def api_remove_session_tag(
        request: Request,
        session_id: int,
        tag_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        await storage.remove_session_tag(session_id, tag_id)
        await _audit(
            request, "session.tag.remove", detail=f"session={session_id} tag={tag_id}", user=_user
        )

    @app.get("/api/sessions/{session_id}/tags")
    async def api_get_session_tags(session_id: int) -> JSONResponse:
        tags = await storage.get_session_tags(session_id)
        return JSONResponse(tags)

    @app.post("/api/notes/{note_id}/tags", status_code=201)
    async def api_add_note_tag(
        request: Request,
        note_id: int,
        body: dict[str, Any],
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> JSONResponse:
        tag_id = body.get("tag_id")
        tag_name = body.get("tag_name")
        if tag_id is None and not tag_name:
            raise HTTPException(status_code=422, detail="tag_id or tag_name is required")
        if tag_id is None:
            assert isinstance(tag_name, str)
            tag_id = await storage.get_or_create_tag(tag_name)
        await storage.add_note_tag(note_id, tag_id)
        await _audit(request, "note.tag.add", detail=f"note={note_id} tag={tag_id}", user=_user)
        return JSONResponse({"note_id": note_id, "tag_id": tag_id}, status_code=201)

    @app.delete("/api/notes/{note_id}/tags/{tag_id}", status_code=204)
    async def api_remove_note_tag(
        request: Request,
        note_id: int,
        tag_id: int,
        _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    ) -> None:
        await storage.remove_note_tag(note_id, tag_id)
        await _audit(request, "note.tag.remove", detail=f"note={note_id} tag={tag_id}", user=_user)

    @app.get("/api/notes/{note_id}/tags")
    async def api_get_note_tags(note_id: int) -> JSONResponse:
        tags = await storage.get_note_tags(note_id)
        return JSONResponse(tags)

    # ------------------------------------------------------------------
    # Profile & Avatars (#100)
    # ------------------------------------------------------------------

    @app.get("/profile", response_class=HTMLResponse, include_in_schema=False)
    async def profile_page(
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> HTMLResponse:
        return HTMLResponse(_render_profile_html(_user))

    @app.post("/profile/avatar", status_code=200, include_in_schema=False)
    async def upload_avatar(
        request: Request,
        file: UploadFile,
        _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
    ) -> JSONResponse:
        user_id = _user.get("id")
        if user_id is None:
            raise HTTPException(status_code=400, detail="Cannot set avatar for mock user")

        # Validate content type
        ct = (file.content_type or "").lower()
        if ct not in ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"):
            raise HTTPException(status_code=422, detail="Unsupported image type")

        data = await file.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="File too large (max 10 MB)")

        avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
        await asyncio.to_thread(avatar_dir.mkdir, parents=True, exist_ok=True)
        dest = avatar_dir / f"{user_id}.jpg"

        try:
            import io

            from PIL import Image  # noqa: PLC0415

            opened = Image.open(io.BytesIO(data))
            rgb = opened.convert("RGB")
            # Centre-crop to square
            w, h = rgb.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            cropped = rgb.crop((left, top, left + side, top + side))
            resized = cropped.resize((256, 256), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=85)
            await asyncio.to_thread(dest.write_bytes, buf.getvalue())
        except ImportError:
            # Pillow not installed — save raw bytes as fallback
            await asyncio.to_thread(dest.write_bytes, data)

        rel_path = f"{user_id}.jpg"
        await storage.set_avatar_path(user_id, rel_path)
        await _audit(request, "avatar.upload", user=_user)
        return JSONResponse({"avatar_path": rel_path})

    @app.get("/avatars/{user_id}.jpg", include_in_schema=False)
    async def serve_avatar(user_id: int) -> Response:
        avatar_dir = Path(os.environ.get("AVATAR_DIR", "data/avatars"))
        path = avatar_dir / f"{user_id}.jpg"
        if path.exists():
            mtime = int(path.stat().st_mtime)
            return FileResponse(
                path,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=60", "ETag": f'"{user_id}-{mtime}"'},
            )
        # Generate initials SVG fallback
        user = await storage.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        name = user.get("name") or user.get("email") or "?"
        parts = name.split()
        initials = (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()
        role = user.get("role", "viewer")
        colors = {"admin": "#2563eb", "crew": "#059669", "viewer": "#6b7280"}
        bg = colors.get(role, "#6b7280")
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">'
            f'<rect width="256" height="256" rx="128" fill="{bg}"/>'
            f'<text x="128" y="128" text-anchor="middle" dy=".35em"'
            f' font-family="system-ui,sans-serif" font-size="96" font-weight="700"'
            f' fill="white">{initials}</text></svg>'
        )
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-cache"},
        )

    return app
