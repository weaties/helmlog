/* compare.js — Synced multi-video maneuver comparison (#565)
 *
 * Loads maneuver data from the compare API, creates one YouTube IFrame
 * player per maneuver, and wires them to a common play/pause control.
 * Each player is cued to [maneuver_ts - preroll + globalNudge + perVideoNudge].
 * The grid auto-sizes so all videos fit in the viewport without scrolling.
 */

'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

// SESSION_ID is null in cross-session mode (page loaded at /compare instead
// of /session/{id}/compare). _crossSession switches data fetching between
// the legacy per-session API and the cross-session /api/maneuvers/compare
// endpoint introduced in #584.
const SESSION_ID = document.getElementById('app-config').dataset.sessionId || null;
let _players = [];   // { player, cueSeconds, maneuver, idx, nudge }
let _allManeuvers = [];
let _crossSession = false;
// In single-session mode these are the one-and-only session's data;
// in cross-session mode they stay empty and the *BySession maps are used.
let _videoSync = null;
// Per-session lookup tables — populated whether we're in single- or
// cross-session mode so rendering paths can uniformly read by session_id.
const _videoSyncBySession = Object.create(null);
const _trackBySession = Object.create(null);
const _replayBySession = Object.create(null);
const _raceGunMsBySession = Object.create(null);
// Start-line geometry per session — {pin:[lat,lon], boat:[lat,lon]} or null.
// Populated alongside track/replay when a cell is a "start" maneuver so the
// start overlay can compute live distance-to-line during playback (#584).
const _startLineBySession = Object.create(null);
let _playing = false;
let _prerollS = 10;
let _globalNudge = 0;  // seconds, applied to all videos (#568)
let _ytReady = false;
let _muted = true; // default muted — multiple simultaneous audio is never useful
let _trackOverlayVisible = true;
let _tickInterval = 0; // playback position poll timer
let _gaugeVisible = true;
let _compareFilter = new Set(); // active filter pills on the compare page
const _CMP_TYPE_PILLS = ['tack', 'gybe', 'rounding', 'weather', 'leeward', 'start'];
const _CMP_DIR_PILLS = ['P\u2192S', 'S\u2192P'];
const _CMP_RANK_PILLS = ['good', 'bad'];
// Wind-range pill values look like "tws:8-10" or "tws:15+" so they share
// the single `_compareFilter` set without colliding with other pill names.
const _CMP_TWS_BANDS = [
  { label: '0-6', min: 0, max: 6 },
  { label: '6-8', min: 6, max: 8 },
  { label: '8-10', min: 8, max: 10 },
  { label: '10-12', min: 10, max: 12 },
  { label: '12-15', min: 12, max: 15 },
  { label: '15+', min: 15, max: null },
];
let _filterPanelOpen = false;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async function init() {
  _loadYouTubeAPI();
  const ids = new URLSearchParams(window.location.search).get('ids');
  if (!ids) { _showEmpty(); return; }

  // Decide which endpoint to use: any ":" in ids indicates cross-session
  // <sid>:<mid> pairs (#584); otherwise fall back to the legacy
  // /api/sessions/{sid}/maneuvers/compare path.
  _crossSession = ids.includes(':') || !SESSION_ID;

  let maneuvers = [];
  if (_crossSession) {
    const resp = await fetch(`/api/maneuvers/compare?ids=${encodeURIComponent(ids)}`);
    if (!resp.ok) { _showEmpty(); return; }
    const data = await resp.json();
    maneuvers = data.maneuvers || [];
    const vsb = data.video_sync_by_session || {};
    for (const k of Object.keys(vsb)) _videoSyncBySession[Number(k)] = vsb[k];
  } else {
    const resp = await fetch(`/api/sessions/${SESSION_ID}/maneuvers/compare?ids=${ids}`);
    if (!resp.ok) { _showEmpty(); return; }
    const data = await resp.json();
    maneuvers = data.maneuvers || [];
    _videoSync = data.video_sync;
    if (_videoSync) _videoSyncBySession[Number(SESSION_ID)] = _videoSync;
    // Stamp session_id onto each maneuver so downstream code that
    // looks up by session_id works uniformly across modes.
    maneuvers.forEach(m => { if (m.session_id == null) m.session_id = Number(SESSION_ID); });
  }

  if (!maneuvers.length) { _showEmpty(); return; }

  _allManeuvers = maneuvers.filter(m => m.youtube_url);
  if (!_allManeuvers.length) { _showEmpty(); return; }

  // Fetch track + replay for every distinct session represented in the
  // maneuver set, in parallel. Each session's data is cached separately
  // so cells render from the right source even when sessions are mixed.
  const sessionIds = [...new Set(_allManeuvers.map(m => m.session_id).filter(x => x != null))];
  await Promise.all(sessionIds.map(_loadSessionOverlays));

  _updateHeaderLink();
  _buildGrid();
  document.getElementById('compare-controls').style.display = '';
  window.addEventListener('resize', _onResize);
})();

async function _loadSessionOverlays(sid) {
  // Only fetch course-overlay (start line) when this session contributes a
  // start maneuver — a large majority of compares will be tack/gybe/rounding
  // and don't need it.
  const needStartLine = _allManeuvers.some(
    m => m.session_id === sid && m.type === 'start'
  );
  const tasks = [
    fetch(`/api/sessions/${sid}/track`),
    fetch(`/api/sessions/${sid}/replay`),
  ];
  if (needStartLine) tasks.push(fetch(`/api/sessions/${sid}/course-overlay`));
  const [trackResult, replayResult, overlayResult] = await Promise.allSettled(tasks);
  try {
    if (trackResult.status === 'fulfilled' && trackResult.value.ok) {
      const geo = await trackResult.value.json();
      const feat = (geo.features || [])[0];
      if (feat && feat.geometry && feat.geometry.coordinates) {
        _trackBySession[sid] = {
          coords: feat.geometry.coordinates,
          timestamps: (feat.properties || {}).timestamps || [],
        };
      }
    }
  } catch (_e) { /* track overlay is optional */ }
  try {
    if (replayResult.status === 'fulfilled' && replayResult.value.ok) {
      const rData = await replayResult.value.json();
      _replayBySession[sid] = (rData.samples || []).map(s => ({
        ts: new Date(s.ts),
        hdg: s.hdg, cog: s.cog, stw: s.stw, sog: s.sog,
        tws: s.tws, twa: s.twa, twd: s.twd, aws: s.aws, awa: s.awa,
      }));
      if (rData.race_gun_utc) {
        _raceGunMsBySession[sid] = _parseUtcMs(rData.race_gun_utc);
      }
    }
  } catch (_e) { /* gauge overlay is optional */ }
  try {
    if (overlayResult && overlayResult.status === 'fulfilled' && overlayResult.value.ok) {
      const od = await overlayResult.value.json();
      const sl = od && od.start_line;
      if (sl && sl.pin && sl.boat) {
        _startLineBySession[sid] = { pin: sl.pin, boat: sl.boat };
      }
    }
  } catch (_e) { /* start line is optional */ }
}

function _updateHeaderLink() {
  // In cross-session mode the back-link goes to the browser page; in
  // single-session mode it's already set by the server template.
  if (!_crossSession) return;
  const headerEl = document.getElementById('compare-header');
  if (!headerEl) return;
  const back = headerEl.querySelector('a.back-link');
  if (back) {
    back.href = '/maneuvers';
    back.textContent = '\u2190 Maneuvers';
  }
}

// ---------------------------------------------------------------------------
// YouTube IFrame API
// ---------------------------------------------------------------------------

function _loadYouTubeAPI() {
  if (window.YT && window.YT.Player) { _ytReady = true; return; }
  const tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
}

window.onYouTubeIframeAPIReady = function () {
  _ytReady = true;
  _createPendingPlayers();
};

let _pendingPlayers = [];

function _createPendingPlayers() {
  for (const p of _pendingPlayers) {
    _createPlayer(p.divId, p.videoId, p.cueSeconds, p.maneuver, p.idx, p.nudge);
  }
  _pendingPlayers = [];
}

// ---------------------------------------------------------------------------
// Cue point calculation
// ---------------------------------------------------------------------------

function _calcCue(maneuver, nudge) {
  return Math.max(0, (maneuver.video_offset_s || 0) - _prerollS + _globalNudge + (nudge || 0));
}

// ---------------------------------------------------------------------------
// Grid layout — fit all cells in the viewport
// ---------------------------------------------------------------------------

function _gridLayout(n) {
  if (n <= 1) return { cols: 1, rows: 1 };
  if (n <= 2) return { cols: 2, rows: 1 };
  if (n <= 4) return { cols: 2, rows: 2 };
  if (n <= 6) return { cols: 3, rows: 2 };
  if (n <= 9) return { cols: 3, rows: 3 };
  if (n <= 12) return { cols: 4, rows: 3 };
  if (n <= 16) return { cols: 4, rows: 4 };
  const cols = Math.ceil(Math.sqrt(n * 16 / 9));
  return { cols, rows: Math.ceil(n / cols) };
}

function _buildGrid() {
  const grid = document.getElementById('compare-grid');
  // Preserve per-video nudges across rebuilds
  const nudges = {};
  _players.forEach(p => { nudges[p.maneuver.id] = p.nudge || 0; });

  grid.innerHTML = '';
  _players = [];
  _pendingPlayers = [];

  const n = _allManeuvers.length;
  if (!n) { _showEmpty(); return; }

  const visibleManeuvers = _allManeuvers.filter(_matchesCompareFilter);
  const visibleCount = visibleManeuvers.length || n;
  const layout = _gridLayout(visibleCount);
  const headerEl = document.getElementById('compare-header');
  const headerH = headerEl ? headerEl.offsetHeight + 4 : 40;
  const filterEl = document.getElementById('compare-filter-panel');
  const filterH = (filterEl && filterEl.style.display !== 'none') ? filterEl.offsetHeight + 4 : 0;
  const availH = window.innerHeight - headerH - filterH - 12;
  const availW = window.innerWidth - 12;
  const gap = 4;
  const labelH = 20;
  const cellPad = 2;

  const cellH = (availH - gap * (layout.rows - 1)) / layout.rows;
  const videoH = cellH - labelH - cellPad;

  grid.style.gridTemplateColumns = 'repeat(' + layout.cols + ', 1fr)';
  grid.style.height = availH + 'px';

  _updateSubtitle();

  for (let i = 0; i < _allManeuvers.length; i++) {
    const m = _allManeuvers[i];
    const nudge = nudges[m.id] || 0;
    const cueSeconds = _calcCue(m, nudge);
    const divId = 'yt-compare-' + i;

    const cell = document.createElement('div');
    cell.className = 'compare-cell';
    cell.id = 'compare-cell-' + i;
    cell.style.maxHeight = cellH + 'px';

    const typeClass = 'badge-' + (m.type || 'maneuver');
    const dirHint = _directionHint(m);
    const dur = m.duration_sec != null ? m.duration_sec.toFixed(1) + 's' : '';
    const loss = m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt dip' : '';
    const rank = m.rank ? (' <span style="color:' + _rankColor(m.rank) + '">&#9679; ' + m.rank + '</span>') : '';
    const elapsed = _fmtElapsed(m.ts);
    const bsp = (m.entry_bsp != null ? m.entry_bsp.toFixed(1) : '?') + '&#8594;' + (m.exit_bsp != null ? m.exit_bsp.toFixed(1) : '?');
    const turn = m.turn_angle_deg != null ? Math.abs(Math.round(m.turn_angle_deg)) + '&deg;' : '';

    const nudgeDisplay = nudge !== 0 ? (nudge > 0 ? '+' : '') + nudge.toFixed(1) + 's' : '0.0s';

    const trackSvg = _renderTrackOverlay(m, i);
    const courseSvg = _renderCourseOverlay(m, i);
    const gaugeSvg = _renderGaugePlaceholder(m, i);
    const recoveryBar = _renderRecoveryBar(m, i);
    const startSvg = _renderStartOverlay(m, i);
    const wrapId = 'yt-wrap-' + i;
    // In cross-session mode prepend the session label so users can tell
    // which day/race a given cell came from.
    const sessionTag = _crossSession && m.session_name
      ? '<span style="color:var(--text-secondary);font-size:.68rem">'
        + _esc((m.session_start_utc || '').slice(0, 10)) + ' '
        + _esc(m.session_name) + '</span> &middot; '
      : '';
    const vs = _videoSyncBySession[m.session_id];
    if (!vs) continue; // skip cells whose session has no video link
    cell.innerHTML =
      '<button class="cell-dismiss" onclick="dismissCell(' + i + ')" title="Remove from comparison">&#10005;</button>'
      + '<div class="yt-wrap" id="' + wrapId + '" style="height:' + Math.max(60, videoH) + 'px">'
      + '<div id="' + divId + '" style="width:100%;height:100%"></div>'
      + '<div class="yt-pan-layer" id="yt-pan-' + i + '" title="Drag to look around · click to play/pause"></div>'
      + trackSvg
      + courseSvg
      + gaugeSvg
      + recoveryBar
      + startSvg
      + '</div>'
      + '<div class="cell-label">'
      + sessionTag
      + '<b class="' + typeClass + '">' + _esc(m.type || 'maneuver') + '</b>'
      + dirHint + rank + ' ' + elapsed
      + (turn ? ' &middot; ' + turn : '')
      + ' &middot; ' + bsp + ' kt'
      + (dur ? ' &middot; ' + dur : '')
      + (loss ? ' &middot; ' + loss : '')
      + (m.youtube_url ? ' <a href="' + _esc(m.youtube_url) + '" target="_blank" rel="noopener" title="Watch on YouTube" style="color:var(--text-secondary);text-decoration:none;font-size:.72rem" onclick="event.stopPropagation()">&#9654;YT</a>' : '')
      + '<span class="nudge-controls">'
      + '<button onclick="event.stopPropagation();nudgeVideo(' + i + ',-0.5)" title="-0.5s">&#9664;</button>'
      + '<span class="nudge-value" id="nudge-val-' + i + '">' + nudgeDisplay + '</span>'
      + '<button onclick="event.stopPropagation();nudgeVideo(' + i + ',+0.5)" title="+0.5s">&#9654;</button>'
      + '<button onclick="event.stopPropagation();nudgeVideo(' + i + ',0,true)" title="Reset offset" class="nudge-reset">0</button>'
      + '</span>'
      + '</div>';
    grid.appendChild(cell);

    const entry = { divId, videoId: vs.video_id, cueSeconds, maneuver: m, idx: i, nudge };
    if (_ytReady) {
      _createPlayer(divId, vs.video_id, cueSeconds, m, i, nudge);
    } else {
      _pendingPlayers.push(entry);
    }
  }
}

let _resizeTimer = 0;
function _onResize() {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(_buildGrid, 200);
}

function _updateSubtitle() {
  document.getElementById('compare-subtitle').textContent =
    _allManeuvers.length + ' maneuver' + (_allManeuvers.length !== 1 ? 's' : '');
}

// ---------------------------------------------------------------------------
// Player creation
// ---------------------------------------------------------------------------

function _createPlayer(divId, videoId, cueSeconds, maneuver, idx, nudge) {
  // Drag-to-pan for 360° videos (#846). YT reports no spherical properties
  // until playback starts, so visibility is re-checked on every state change.
  let pan = null;
  const panLayer = document.getElementById('yt-pan-' + idx);
  const player = new YT.Player(divId, {
    width: '100%',
    height: '100%',
    videoId: videoId,
    playerVars: {
      start: Math.max(0, Math.floor(cueSeconds)),
      controls: 1,
      modestbranding: 1,
      rel: 0,
    },
    events: {
      onReady: function (ev) {
        ev.target.seekTo(cueSeconds, true);
        ev.target.pauseVideo();
        if (_muted) ev.target.mute();
        const speed = parseFloat(document.getElementById('speed-select').value) || 1;
        ev.target.setPlaybackRate(speed);
        // Initial gauge + track dot update for the paused state
        setTimeout(_tickUpdate, 500);
        if (pan) pan.refresh();
      },
      onStateChange: function () {
        if (pan) pan.refresh();
      },
    },
  });
  if (panLayer) pan = attachVideoPan(panLayer, () => player);
  _players.push({ player, cueSeconds, maneuver, idx, nudge: nudge || 0 });
}

// ---------------------------------------------------------------------------
// Per-video nudge (#567)
// ---------------------------------------------------------------------------

function nudgeVideo(idx, delta, reset) {
  const p = _players.find(p => p.idx === idx);
  if (!p) return;
  if (reset) {
    p.nudge = 0;
  } else {
    p.nudge = Math.round((p.nudge + delta) * 10) / 10;
  }
  p.cueSeconds = _calcCue(p.maneuver, p.nudge);
  try {
    p.player.seekTo(p.cueSeconds, true);
    if (!_playing) p.player.pauseVideo();
  } catch (_e) { /* not ready */ }
  setTimeout(_tickUpdate, 300);
  // Update the display
  const el = document.getElementById('nudge-val-' + idx);
  if (el) {
    const v = p.nudge;
    el.textContent = (v > 0 ? '+' : '') + v.toFixed(1) + 's';
  }
}

// ---------------------------------------------------------------------------
// Dismiss (remove a cell)
// ---------------------------------------------------------------------------

function dismissCell(idx) {
  const pi = _players.findIndex(p => p.idx === idx);
  if (pi !== -1) {
    try { _players[pi].player.destroy(); } catch (_e) { /* ok */ }
    _players.splice(pi, 1);
  }
  _allManeuvers.splice(idx, 1);
  if (!_allManeuvers.length) {
    _showEmpty();
    document.getElementById('compare-grid').style.display = 'none';
    return;
  }
  _buildGrid();
}

// ---------------------------------------------------------------------------
// Shared controls
// ---------------------------------------------------------------------------

function _visiblePlayers() {
  return _players.filter(p => _matchesCompareFilter(p.maneuver));
}

function togglePlayAll() {
  const vp = _visiblePlayers();
  if (_playing) {
    _playing = false;
    vp.forEach(p => { try { p.player.pauseVideo(); } catch (_e) { /* not ready */ } });
    document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
    _stopTrackTick();
  } else {
    _playing = true;
    vp.forEach(p => { try { p.player.playVideo(); } catch (_e) { /* not ready */ } });
    document.getElementById('play-all-btn').innerHTML = '&#9646;&#9646; Pause All';
    _startTrackTick();
  }
}

function seekAllToStart() {
  _playing = false;
  _stopTrackTick();
  document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
  _visiblePlayers().forEach(p => {
    p.cueSeconds = _calcCue(p.maneuver, p.nudge);
    try {
      p.player.seekTo(p.cueSeconds, true);
      p.player.pauseVideo();
    } catch (_e) { /* not ready */ }
  });
  setTimeout(_tickUpdate, 300);
}

function setAllSpeed(val) {
  const rate = parseFloat(val) || 1;
  _players.forEach(p => { try { p.player.setPlaybackRate(rate); } catch (_e) { /* not ready */ } });
}

function toggleMuteAll() {
  _muted = !_muted;
  _players.forEach(p => {
    try { _muted ? p.player.mute() : p.player.unMute(); } catch (_e) { /* not ready */ }
  });
  const btn = document.getElementById('mute-btn');
  if (btn) btn.innerHTML = _muted ? '&#128263; Unmute' : '&#128264; Mute';
}

function setPreroll(val) {
  _prerollS = parseInt(val, 10) || 10;
  _seekAllToCue();
}

// Global offset (#568)
function adjustGlobalOffset(delta) {
  _globalNudge = Math.round((_globalNudge + delta) * 10) / 10;
  document.getElementById('global-offset-val').textContent =
    (_globalNudge > 0 ? '+' : '') + _globalNudge.toFixed(1) + 's';
  _seekAllToCue();
}

function resetGlobalOffset() {
  _globalNudge = 0;
  document.getElementById('global-offset-val').textContent = '0.0s';
  _seekAllToCue();
}

function _seekAllToCue() {
  _playing = false;
  _stopTrackTick();
  document.getElementById('play-all-btn').innerHTML = '&#9654; Play All';
  _players.forEach(p => {
    p.cueSeconds = _calcCue(p.maneuver, p.nudge);
    try {
      p.player.seekTo(p.cueSeconds, true);
      p.player.pauseVideo();
    } catch (_e) { /* not ready */ }
  });
  setTimeout(_tickUpdate, 300);
}

// ---------------------------------------------------------------------------
// Track overlay (#570)
// ---------------------------------------------------------------------------

function _renderTrackOverlay(m, idx) {
  const track = m.track;
  if (!track || track.length < 2) return '';

  const size = 120;
  const pad = 8;

  // Compute bounds
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of track) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return '';

  // Square bounds with padding
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(5, Math.max(maxX - minX, maxY - minY) / 2 + 3);
  const bMinX = cx - half, bMaxX = cx + half, bMinY = cy - half, bMaxY = cy + half;

  const sx = x => pad + (x - bMinX) / (bMaxX - bMinX) * (size - 2 * pad);
  const sy = y => (size - pad) - (y - bMinY) / (bMaxY - bMinY) * (size - 2 * pad);

  // Build polyline
  const pts = track.map(p => sx(p.x).toFixed(1) + ',' + sy(p.y).toFixed(1)).join(' ');

  // Origin crosshair (maneuver start point)
  const ox = sx(0), oy = sy(0);

  // Color by rank
  const rankColors = { good: '#3db86e', bad: '#d64545', avg: '#888' };
  const color = rankColors[m.rank] || '#7eb8f7';

  const display = _trackOverlayVisible ? '' : 'display:none;';

  return '<svg class="track-overlay" id="track-svg-' + idx + '" width="' + size + '" height="' + size + '" style="' + display + '">'
    + '<rect width="' + size + '" height="' + size + '" rx="6" fill="rgba(0,0,0,.45)"/>'
    + '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
    + '<circle cx="' + ox + '" cy="' + oy + '" r="2.5" fill="#fff" stroke="' + color + '" stroke-width="1"/>'
    + '<text x="' + (size - pad) + '" y="' + (pad + 6) + '" text-anchor="end" font-size="7" fill="rgba(255,255,255,.5)">&#8593; wind</text>'
    + '<circle id="track-dot-' + idx + '" cx="' + ox + '" cy="' + oy + '" r="3.5" fill="#fff" stroke="rgba(0,0,0,.6)" stroke-width="1"/>'
    + '</svg>';
}

function _renderCourseOverlay(m, idx) {
  const sessionTrack = _trackBySession[m.session_id];
  if (!sessionTrack || !sessionTrack.coords.length) return '';
  const coords = sessionTrack.coords; // [lng, lat]
  if (coords.length < 2) return '';

  const size = 100;
  const pad = 6;

  // Convert lng/lat to simple x/y (Mercator-ish, fine for local scale)
  const lat0 = coords[0][1], lng0 = coords[0][0];
  const cosLat = Math.cos(lat0 * Math.PI / 180);
  const mPerDeg = 111320;
  const points = coords.map(c => ({
    x: (c[0] - lng0) * mPerDeg * cosLat,
    y: (c[1] - lat0) * mPerDeg,
  }));

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of points) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return '';

  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(20, Math.max(maxX - minX, maxY - minY) / 2 * 1.1);
  const sx = x => pad + (x - (cx - half)) / (2 * half) * (size - 2 * pad);
  const sy = y => (size - pad) - (y - (cy - half)) / (2 * half) * (size - 2 * pad);

  // Downsample for SVG performance (keep every Nth point)
  const step = Math.max(1, Math.floor(points.length / 300));
  const pts = [];
  for (let i = 0; i < points.length; i += step) {
    pts.push(sx(points[i].x).toFixed(1) + ',' + sy(points[i].y).toFixed(1));
  }

  // Maneuver position marker
  let marker = '';
  if (m.lat != null && m.lon != null) {
    const mx = (m.lon - lng0) * mPerDeg * cosLat;
    const my = (m.lat - lat0) * mPerDeg;
    const msx = sx(mx), msy = sy(my);
    const rankColors = { good: '#3db86e', bad: '#d64545', avg: '#888' };
    const mc = rankColors[m.rank] || '#7eb8f7';
    marker = '<circle cx="' + msx.toFixed(1) + '" cy="' + msy.toFixed(1)
      + '" r="4" fill="' + mc + '" stroke="#fff" stroke-width="1.5"/>';
  }

  const display = _trackOverlayVisible ? '' : 'display:none;';

  return '<svg class="track-overlay course-overlay" id="course-svg-' + idx + '" width="' + size + '" height="' + size + '" style="' + display + 'bottom:auto;left:auto;top:6px;right:30px">'
    + '<rect width="' + size + '" height="' + size + '" rx="6" fill="rgba(0,0,0,.4)"/>'
    + '<polyline points="' + pts.join(' ') + '" fill="none" stroke="rgba(255,255,255,.4)" stroke-width="1" stroke-linejoin="round"/>'
    + marker
    + '</svg>';
}

function toggleTrackOverlay() {
  _trackOverlayVisible = !_trackOverlayVisible;
  const btn = document.getElementById('track-toggle-btn');
  if (btn) {
    btn.style.background = _trackOverlayVisible ? 'var(--accent-strong)' : 'var(--bg-input)';
    btn.style.color = _trackOverlayVisible ? 'var(--bg-primary)' : 'var(--text-secondary)';
    btn.style.border = _trackOverlayVisible ? 'none' : '1px solid var(--border)';
  }
  for (let i = 0; i < _allManeuvers.length; i++) {
    const svg = document.getElementById('track-svg-' + i);
    if (svg) svg.style.display = _trackOverlayVisible ? '' : 'none';
    const course = document.getElementById('course-svg-' + i);
    if (course) course.style.display = _trackOverlayVisible ? '' : 'none';
  }
}

// Update overlays based on current playback time
function _startTrackTick() {
  if (_tickInterval) return;
  _tickInterval = setInterval(_tickUpdate, 200);
}

function _stopTrackTick() {
  if (_tickInterval) { clearInterval(_tickInterval); _tickInterval = 0; }
}

function _tickUpdate() {
  for (const p of _players) {
    let videoTime;
    try { videoTime = p.player.getCurrentTime(); } catch (_e) { continue; }

    // --- Track dot update ---
    if (_trackOverlayVisible) {
      const track = p.maneuver.track;
      if (track && track.length >= 2) {
        const dot = document.getElementById('track-dot-' + p.idx);
        if (dot) {
          const currentT = videoTime - (p.maneuver.video_offset_s || 0);
          let best = track[0], bestDt = Math.abs(track[0].t - currentT);
          for (let i = 1; i < track.length; i++) {
            const dt = Math.abs(track[i].t - currentT);
            if (dt < bestDt) { bestDt = dt; best = track[i]; }
          }
          const size = 120, pad = 8;
          let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
          for (const pt of track) {
            if (pt.x < minX) minX = pt.x; if (pt.x > maxX) maxX = pt.x;
            if (pt.y < minY) minY = pt.y; if (pt.y > maxY) maxY = pt.y;
          }
          const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
          const half = Math.max(5, Math.max(maxX - minX, maxY - minY) / 2 + 3);
          const svgX = pad + (best.x - (cx - half)) / (2 * half) * (size - 2 * pad);
          const svgY = (size - pad) - (best.y - (cy - half)) / (2 * half) * (size - 2 * pad);
          dot.setAttribute('cx', svgX.toFixed(1));
          dot.setAttribute('cy', svgY.toFixed(1));
        }
      }
    }

    // --- Gauge update ---
    if (_gaugeVisible) {
      const samples = _replayBySession[p.maneuver.session_id];
      if (samples && samples.length) _updateGauge(p, videoTime, samples);
    }

    // --- Start overlay update (countdown + DTL) ---
    if (p.maneuver.type === 'start') _updateStartOverlay(p, videoTime);
  }
}

// ---------------------------------------------------------------------------
// Instrument gauge overlay (#572)
// ---------------------------------------------------------------------------

function _renderGaugePlaceholder(m, idx) {
  const samples = _replayBySession[m.session_id];
  if (!samples || !samples.length) return '';
  const s = 150; // gauge size
  const r = 62;  // compass radius
  const cx = s / 2, cy = s / 2;
  const display = _gaugeVisible ? '' : 'display:none;';

  // Compass ticks
  let ticks = '';
  for (let d = 0; d < 360; d += 10) {
    const rad = (d - 90) * Math.PI / 180;
    const inner = d % 30 === 0 ? r - 10 : r - 5;
    const x1 = cx + inner * Math.cos(rad), y1 = cy + inner * Math.sin(rad);
    const x2 = cx + r * Math.cos(rad), y2 = cy + r * Math.sin(rad);
    ticks += '<line x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1)
      + '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1)
      + '" stroke="rgba(255,255,255,.35)" stroke-width="' + (d % 30 === 0 ? '1.2' : '0.6') + '"/>';
  }
  // Cardinal labels
  const cardinals = [{l:'N',d:0},{l:'E',d:90},{l:'S',d:180},{l:'W',d:270}];
  let labels = '';
  for (const c of cardinals) {
    const rad = (c.d - 90) * Math.PI / 180;
    const lx = cx + (r + 9) * Math.cos(rad), ly = cy + (r + 9) * Math.sin(rad);
    labels += '<text x="' + lx.toFixed(1) + '" y="' + ly.toFixed(1)
      + '" text-anchor="middle" dominant-baseline="central" font-size="9" font-weight="600" fill="rgba(255,255,255,.55)">' + c.l + '</text>';
  }

  return '<svg class="gauge-overlay" id="gauge-svg-' + idx + '" width="' + s + '" height="' + s + '" style="' + display + '">'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + (r + 10) + '" fill="rgba(0,0,0,.5)"/>'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="rgba(255,255,255,.25)" stroke-width="1"/>'
    + ticks + labels
    // TWD arrow (orange) — rotated by JS
    + '<g id="gauge-twd-' + idx + '" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + (cy + r - 10) + '" x2="' + cx + '" y2="' + (cy - r + 12) + '" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"/>'
    + '<polygon points="' + cx + ',' + (cy - r + 8) + ' ' + (cx - 4) + ',' + (cy - r + 16) + ' ' + (cx + 4) + ',' + (cy - r + 16) + '" fill="#f59e0b"/>'
    + '</g>'
    // AWA arrow (blue) — rotated by JS
    + '<g id="gauge-awa-' + idx + '" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + cy + '" x2="' + cx + '" y2="' + (cy - r + 18) + '" stroke="#60a5fa" stroke-width="1.8" stroke-linecap="round"/>'
    + '<polygon points="' + cx + ',' + (cy - r + 14) + ' ' + (cx - 3) + ',' + (cy - r + 21) + ' ' + (cx + 3) + ',' + (cy - r + 21) + '" fill="#60a5fa"/>'
    + '</g>'
    // COG line (white dashed)
    + '<g id="gauge-cog-' + idx + '" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + cy + '" x2="' + cx + '" y2="' + (cy - r + 6) + '" stroke="rgba(255,255,255,.6)" stroke-width="1" stroke-dasharray="3,2"/>'
    + '</g>'
    // Boat icon (center)
    + '<polygon points="' + cx + ',' + (cy - 6) + ' ' + (cx - 4) + ',' + (cy + 5) + ' ' + (cx + 4) + ',' + (cy + 5) + '" fill="#fff" stroke="rgba(0,0,0,.4)" stroke-width="0.5"/>'
    // HDG readout (top)
    + '<rect x="' + (cx - 18) + '" y="1" width="36" height="15" rx="3" fill="rgba(0,0,0,.8)"/>'
    + '<text id="gauge-hdg-' + idx + '" x="' + cx + '" y="12" text-anchor="middle" font-size="11" font-weight="700" font-family="monospace" fill="#fff">---</text>'
    // BSP readout (left)
    + '<rect x="2" y="' + (cy - 9) + '" width="36" height="24" rx="3" fill="rgba(0,0,0,.7)"/>'
    + '<text x="20" y="' + (cy - 1) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">BSP</text>'
    + '<text id="gauge-bsp-' + idx + '" x="20" y="' + (cy + 11) + '" text-anchor="middle" font-size="12" font-weight="700" font-family="monospace" fill="#3db86e">-.-</text>'
    // SOG readout (right)
    + '<rect x="' + (s - 38) + '" y="' + (cy - 9) + '" width="36" height="24" rx="3" fill="rgba(0,0,0,.7)"/>'
    + '<text x="' + (s - 20) + '" y="' + (cy - 1) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">SOG</text>'
    + '<text id="gauge-sog-' + idx + '" x="' + (s - 20) + '" y="' + (cy + 11) + '" text-anchor="middle" font-size="12" font-weight="700" font-family="monospace" fill="#fff">-.-</text>'
    // TWS readout (bottom-left, orange)
    + '<rect x="6" y="' + (s - 28) + '" width="40" height="24" rx="3" fill="rgba(0,0,0,.75)"/>'
    + '<text x="26" y="' + (s - 17) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">TWS</text>'
    + '<text id="gauge-tws-' + idx + '" x="26" y="' + (s - 6) + '" text-anchor="middle" font-size="13" font-weight="700" font-family="monospace" fill="#f59e0b">--</text>'
    // AWS readout (bottom-right, blue)
    + '<rect x="' + (s - 46) + '" y="' + (s - 28) + '" width="40" height="24" rx="3" fill="rgba(0,0,0,.75)"/>'
    + '<text x="' + (s - 26) + '" y="' + (s - 17) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">AWS</text>'
    + '<text id="gauge-aws-' + idx + '" x="' + (s - 26) + '" y="' + (s - 6) + '" text-anchor="middle" font-size="13" font-weight="700" font-family="monospace" fill="#60a5fa">--</text>'
    + '</svg>';
}

function _updateGauge(p, videoTime, samples) {
  // Convert video time to UTC
  const mTs = _parseUtcMs(p.maneuver.ts);
  if (!mTs) return;
  const offsetS = p.maneuver.video_offset_s || 0;
  const utcMs = mTs + (videoTime - offsetS) * 1000;

  // Binary search for nearest sample in this maneuver's session replay
  const sample = _sampleAtTime(utcMs, samples);
  if (!sample) return;

  const idx = p.idx;
  const cx = 75, cy = 75; // gauge center (s/2 where s=150)

  // Update HDG
  const hdgEl = document.getElementById('gauge-hdg-' + idx);
  if (hdgEl) hdgEl.textContent = sample.hdg != null ? Math.round(sample.hdg) : '---';

  // Update BSP
  const bspEl = document.getElementById('gauge-bsp-' + idx);
  if (bspEl) bspEl.textContent = sample.stw != null ? sample.stw.toFixed(1) : '-.-';

  // Update SOG
  const sogEl = document.getElementById('gauge-sog-' + idx);
  if (sogEl) sogEl.textContent = sample.sog != null ? sample.sog.toFixed(1) : '-.-';

  // Update TWS
  const twsEl = document.getElementById('gauge-tws-' + idx);
  if (twsEl) twsEl.textContent = sample.tws != null ? sample.tws.toFixed(0) : '--';

  // Update AWS
  const awsEl = document.getElementById('gauge-aws-' + idx);
  if (awsEl) awsEl.textContent = sample.aws != null ? sample.aws.toFixed(0) : '--';

  // Rotate TWD arrow: show wind direction relative to heading
  // TWD is compass direction wind comes FROM; on the gauge, HDG is up (north=up, rotated)
  // We want the arrow to point in the direction wind blows FROM, relative to the boat heading
  const twdG = document.getElementById('gauge-twd-' + idx);
  if (twdG && sample.twd != null && sample.hdg != null) {
    const relTwd = ((sample.twd - sample.hdg) + 360) % 360;
    twdG.setAttribute('transform', 'rotate(' + relTwd.toFixed(1) + ',' + cx + ',' + cy + ')');
  }

  // Rotate AWA arrow: boat-relative, so direct rotation from top
  const awaG = document.getElementById('gauge-awa-' + idx);
  if (awaG && sample.awa != null) {
    awaG.setAttribute('transform', 'rotate(' + sample.awa.toFixed(1) + ',' + cx + ',' + cy + ')');
  }

  // Rotate COG line relative to heading
  const cogG = document.getElementById('gauge-cog-' + idx);
  if (cogG && sample.cog != null && sample.hdg != null) {
    const relCog = ((sample.cog - sample.hdg) + 360) % 360;
    cogG.setAttribute('transform', 'rotate(' + relCog.toFixed(1) + ',' + cx + ',' + cy + ')');
  }

  // Update BSP recovery bar
  _updateRecoveryBar(p, sample);
}

function _parseUtcMs(iso) {
  if (!iso) return null;
  let s = iso.replace(' ', 'T');
  if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d.getTime();
}

function _sampleAtTime(utcMs, samples) {
  if (!samples || !samples.length) return null;
  let lo = 0, hi = samples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (samples[mid].ts.getTime() <= utcMs) lo = mid;
    else hi = mid - 1;
  }
  return samples[lo];
}

function toggleGaugeOverlay() {
  _gaugeVisible = !_gaugeVisible;
  const btn = document.getElementById('gauge-toggle-btn');
  if (btn) {
    btn.style.background = _gaugeVisible ? 'var(--accent-strong)' : 'var(--bg-input)';
    btn.style.color = _gaugeVisible ? 'var(--bg-primary)' : 'var(--text-secondary)';
    btn.style.border = _gaugeVisible ? 'none' : '1px solid var(--border)';
  }
  for (let i = 0; i < _allManeuvers.length; i++) {
    const svg = document.getElementById('gauge-svg-' + i);
    if (svg) svg.style.display = _gaugeVisible ? '' : 'none';
    const bar = document.getElementById('recovery-svg-' + i);
    if (bar) bar.style.display = _gaugeVisible ? '' : 'none';
  }
}

// ---------------------------------------------------------------------------
// BSP recovery bar (#574)
// ---------------------------------------------------------------------------

function _renderRecoveryBar(m, idx) {
  const samples = _replayBySession[m.session_id];
  if (!samples || !samples.length) return '';
  if (m.entry_bsp == null || m.entry_bsp <= 0) return '';

  const w = 28, h = 150;
  const pad = 20; // top/bottom padding for labels
  const barX = 6, barW = 16;
  const barTop = pad, barBot = h - pad;
  const barH = barBot - barTop;
  // 100% line position (entry speed reference)
  const maxPct = 120;
  const pct100Y = barBot - (100 / maxPct) * barH;
  const display = _gaugeVisible ? '' : 'display:none;';

  // Min BSP marker
  let minMarker = '';
  if (m.min_bsp != null) {
    const minPct = Math.max(0, Math.min(maxPct, (m.min_bsp / m.entry_bsp) * 100));
    const minY = barBot - (minPct / maxPct) * barH;
    minMarker = '<line x1="' + barX + '" y1="' + minY.toFixed(1) + '" x2="' + (barX + barW) + '" y2="' + minY.toFixed(1)
      + '" stroke="#d64545" stroke-width="1.5" stroke-dasharray="2,1"/>';
  }

  return '<svg class="recovery-overlay" id="recovery-svg-' + idx + '" width="' + w + '" height="' + h + '" style="' + display + '">'
    // Background
    + '<rect x="' + barX + '" y="' + barTop + '" width="' + barW + '" height="' + barH + '" rx="3" fill="rgba(0,0,0,.5)" stroke="rgba(255,255,255,.2)" stroke-width="0.5"/>'
    // Fill bar (updated by JS)
    + '<rect id="recovery-fill-' + idx + '" x="' + barX + '" y="' + barBot + '" width="' + barW + '" height="0" rx="3" fill="#3db86e"/>'
    // Clip the fill to bar bounds
    // 100% reference line
    + '<line x1="' + (barX - 2) + '" y1="' + pct100Y.toFixed(1) + '" x2="' + (barX + barW + 2) + '" y2="' + pct100Y.toFixed(1)
    + '" stroke="#fff" stroke-width="1.5"/>'
    // Entry BSP label at 100% line
    + '<text x="' + (barX + barW / 2) + '" y="' + (pct100Y - 3).toFixed(1) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.7)">'
    + m.entry_bsp.toFixed(1) + '</text>'
    // Min BSP marker
    + minMarker
    // Percentage readout (top)
    + '<text id="recovery-pct-' + idx + '" x="' + (barX + barW / 2) + '" y="' + (barTop - 5) + '" text-anchor="middle" font-size="11" font-weight="700" font-family="monospace" fill="#fff">--%</text>'
    // "%" label at bottom
    + '<text x="' + (barX + barW / 2) + '" y="' + (barBot + 12) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.5)">BSP%</text>'
    + '</svg>';
}

function _updateRecoveryBar(p, sample) {
  const m = p.maneuver;
  if (m.entry_bsp == null || m.entry_bsp <= 0) return;
  if (sample.stw == null) return;

  const idx = p.idx;
  const pct = (sample.stw / m.entry_bsp) * 100;
  const maxPct = 120;
  const clampPct = Math.max(0, Math.min(maxPct, pct));

  const pad = 20;
  const barTop = pad, barBot = 150 - pad;
  const barH = barBot - barTop;
  const barX = 6;

  const fillH = (clampPct / maxPct) * barH;
  const fillY = barBot - fillH;

  // Color by recovery %
  let color;
  if (pct >= 100) color = '#3db86e';       // green: fully recovered
  else if (pct >= 80) color = '#f59e0b';   // amber: almost there
  else if (pct >= 60) color = '#e87c1e';   // orange: mid-recovery
  else color = '#d64545';                   // red: deep dip

  const fill = document.getElementById('recovery-fill-' + idx);
  if (fill) {
    fill.setAttribute('y', fillY.toFixed(1));
    fill.setAttribute('height', fillH.toFixed(1));
    fill.setAttribute('fill', color);
  }

  const pctEl = document.getElementById('recovery-pct-' + idx);
  if (pctEl) {
    pctEl.textContent = Math.round(pct) + '%';
    pctEl.setAttribute('fill', color);
  }
}

// ---------------------------------------------------------------------------
// Start overlay — countdown to gun + distance to line (#584 follow-up)
// ---------------------------------------------------------------------------

function _renderStartOverlay(m, idx) {
  if (m.type !== 'start') return '';
  const w = 150, h = 56;
  return '<svg class="start-overlay" id="start-svg-' + idx + '" width="' + w + '" height="' + h + '">'
    // Background pill
    + '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="8" fill="rgba(0,0,0,.72)"/>'
    // Countdown label (small, above)
    + '<text x="' + (w / 2) + '" y="12" text-anchor="middle" font-size="8" fill="rgba(255,255,255,.55)" letter-spacing="1">COUNTDOWN</text>'
    // Countdown value (large, centered)
    + '<text id="start-countdown-' + idx + '" x="' + (w / 2) + '" y="32" text-anchor="middle" font-size="20" font-weight="700" font-family="monospace" fill="#f59e0b">T-0:00</text>'
    // DTL label + value (right-aligned on row below)
    + '<text x="6" y="50" font-size="8" fill="rgba(255,255,255,.55)" letter-spacing="1">DTL</text>'
    + '<text id="start-dtl-' + idx + '" x="' + (w - 6) + '" y="50" text-anchor="end" font-size="12" font-weight="700" font-family="monospace" fill="#60a5fa">\u2014</text>'
    + '</svg>';
}

function _updateStartOverlay(p, videoTime) {
  const m = p.maneuver;
  if (m.type !== 'start') return;
  const idx = p.idx;

  // Countdown: delta relative to the gun in seconds.
  const delta = videoTime - (m.video_offset_s || 0);
  const countdownEl = document.getElementById('start-countdown-' + idx);
  if (countdownEl) {
    const absS = Math.abs(delta);
    const mm = Math.floor(absS / 60);
    const ss = Math.floor(absS % 60);
    const mmss = String(mm).padStart(1, '0') + ':' + String(ss).padStart(2, '0');
    let label;
    let color;
    if (Math.abs(delta) < 0.5) {
      label = 'GUN';
      color = '#3db86e';
    } else if (delta < 0) {
      label = 'T-' + mmss;
      color = '#f59e0b'; // amber pre-gun
    } else {
      label = 'T+' + mmss;
      color = '#60a5fa'; // blue post-gun
    }
    countdownEl.textContent = label;
    countdownEl.setAttribute('fill', color);
  }

  // Distance to line: look up boat position at the current UTC time and
  // drop a perpendicular onto the pin\u2194boat segment. Falls back to an em
  // dash when any piece of data is unavailable.
  const dtlEl = document.getElementById('start-dtl-' + idx);
  if (!dtlEl) return;
  const sid = m.session_id;
  const line = _startLineBySession[sid];
  const track = _trackBySession[sid];
  if (!line || !track || !track.coords || !track.timestamps || !track.timestamps.length) {
    dtlEl.textContent = '\u2014';
    return;
  }
  const gunMs = _parseUtcMs(m.ts);
  if (gunMs == null) { dtlEl.textContent = '\u2014'; return; }
  const utcMs = gunMs + delta * 1000;
  const pos = _positionAtTime(track, utcMs);
  if (!pos) { dtlEl.textContent = '\u2014'; return; }
  const dtl = _distanceToLine(line.pin, line.boat, pos);
  if (dtl == null) { dtlEl.textContent = '\u2014'; return; }
  dtlEl.textContent = Math.round(dtl) + 'm';
  // Red when on/over the line after the gun, amber when close pre-gun.
  if (delta >= 0 && dtl < 5) dtlEl.setAttribute('fill', '#3db86e');
  else if (delta < 0 && dtl < 10) dtlEl.setAttribute('fill', '#f59e0b');
  else dtlEl.setAttribute('fill', '#60a5fa');
}

function _positionAtTime(track, utcMs) {
  const timestamps = track.timestamps;
  const coords = track.coords;
  if (!timestamps.length || timestamps.length !== coords.length) return null;
  // Binary search for the largest timestamp <= utcMs.
  let lo = 0, hi = timestamps.length - 1;
  const tsMs = (i) => {
    const iso = timestamps[i];
    if (!iso) return NaN;
    let s = iso.replace(' ', 'T');
    if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
    return new Date(s).getTime();
  };
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (tsMs(mid) <= utcMs) lo = mid;
    else hi = mid - 1;
  }
  const c = coords[lo];
  if (!c || c.length < 2) return null;
  return { lat: c[1], lon: c[0] };
}

function _distanceToLine(pin, boat, pos) {
  const pinLat = pin[0], pinLon = pin[1];
  const boatLat = boat[0], boatLon = boat[1];
  const mPerDegLat = 111320;
  const mPerDegLon = 111320 * Math.cos(pinLat * Math.PI / 180);
  const vx = (boatLon - pinLon) * mPerDegLon;
  const vy = (boatLat - pinLat) * mPerDegLat;
  const px = (pos.lon - pinLon) * mPerDegLon;
  const py = (pos.lat - pinLat) * mPerDegLat;
  const segLen = Math.hypot(vx, vy);
  if (segLen === 0) return null;
  return Math.abs(vx * py - vy * px) / segLen;
}

// ---------------------------------------------------------------------------
// Filter panel (#580)
// ---------------------------------------------------------------------------

function _matchesCompareFilter(m) {
  if (!_compareFilter.size) return true;
  const activeTypes = _CMP_TYPE_PILLS.filter(p => _compareFilter.has(p));
  if (activeTypes.length) {
    const hitType = activeTypes.includes(m.type);
    // weather/leeward pills match roundings with that mark (#584 follow-up).
    const hitMark = m.type === 'rounding'
      && ((activeTypes.includes('weather') && m.mark === 'weather')
       || (activeTypes.includes('leeward') && m.mark === 'leeward'));
    if (!hitType && !hitMark) return false;
  }
  const activeRanks = _CMP_RANK_PILLS.filter(p => _compareFilter.has(p));
  if (activeRanks.length && !activeRanks.includes(m.rank)) return false;
  const activeDir = _CMP_DIR_PILLS.filter(p => _compareFilter.has(p));
  if (activeDir.length) {
    if (m.turn_angle_deg == null) return false;
    const isPS = m.turn_angle_deg < 0;
    if (activeDir.includes('P\u2192S') && !isPS) return false;
    if (activeDir.includes('S\u2192P') && isPS) return false;
  }
  if (_compareFilter.has('post-start')) {
    const gunMs = _raceGunMsBySession[m.session_id];
    if (gunMs) {
      const mTs = _parseUtcMs(m.ts);
      if (!mTs || mTs < gunMs) return false;
    }
  }
  // Wind-range bands (#584). Pill values are "tws:<label>"; a single band
  // filter applies but if two are on we union them (logical OR).
  const activeTws = _CMP_TWS_BANDS.filter(b => _compareFilter.has('tws:' + b.label));
  if (activeTws.length) {
    const t = m.entry_tws;
    if (t == null) return false;
    const inAny = activeTws.some(b => t >= b.min && (b.max == null || t <= b.max));
    if (!inAny) return false;
  }
  return true;
}

function setCompareFilter(f) {
  if (f === 'all') {
    _compareFilter.clear();
  } else if (_compareFilter.has(f)) {
    _compareFilter.delete(f);
  } else {
    if (_CMP_DIR_PILLS.includes(f)) {
      _CMP_DIR_PILLS.forEach(d => _compareFilter.delete(d));
    }
    _compareFilter.add(f);
  }
  _applyFilter();
}

function toggleFilterPanel() {
  _filterPanelOpen = !_filterPanelOpen;
  const panel = document.getElementById('compare-filter-panel');
  if (panel) panel.style.display = _filterPanelOpen ? 'flex' : 'none';
  if (_filterPanelOpen) _renderFilterPills();
  const btn = document.getElementById('filter-toggle-btn');
  if (btn) {
    btn.style.background = _filterPanelOpen ? 'var(--accent-strong)' : 'var(--bg-input)';
    btn.style.color = _filterPanelOpen ? 'var(--bg-primary)' : 'var(--text-secondary)';
    btn.style.border = _filterPanelOpen ? 'none' : '1px solid var(--border)';
  }
  // Re-layout grid height without destroying cells
  _applyFilter();
}

function _applyFilter() {
  const grid = document.getElementById('compare-grid');
  let visibleCount = 0;

  for (let i = 0; i < _allManeuvers.length; i++) {
    const m = _allManeuvers[i];
    const cell = document.getElementById('compare-cell-' + i);
    if (!cell) continue;
    const visible = _matchesCompareFilter(m);
    cell.style.display = visible ? '' : 'none';
    if (visible) visibleCount++;

    // Pause hidden players
    if (!visible) {
      const p = _players.find(p => p.idx === i);
      if (p) { try { p.player.pauseVideo(); } catch (_e) { /* ok */ } }
    }
  }

  // Re-layout grid for visible count
  if (visibleCount > 0) {
    const layout = _gridLayout(visibleCount);
    grid.style.gridTemplateColumns = 'repeat(' + layout.cols + ', 1fr)';
    const headerEl = document.getElementById('compare-header');
    const headerH = headerEl ? headerEl.offsetHeight + 4 : 40;
    const filterEl = document.getElementById('compare-filter-panel');
    const filterH = (filterEl && filterEl.style.display !== 'none') ? filterEl.offsetHeight + 4 : 0;
    const availH = window.innerHeight - headerH - filterH - 12;
    grid.style.height = availH + 'px';

    const gap = 4;
    const cellH = (availH - gap * (layout.rows - 1)) / layout.rows;
    // Update visible cell max-heights
    for (let i = 0; i < _allManeuvers.length; i++) {
      const cell = document.getElementById('compare-cell-' + i);
      if (cell && cell.style.display !== 'none') {
        cell.style.maxHeight = cellH + 'px';
      }
    }
    grid.style.display = '';
    document.getElementById('compare-empty').style.display = 'none';
  } else {
    grid.style.display = 'none';
    document.getElementById('compare-empty').style.display = '';
  }

  _renderFilterPills();
  _updateSubtitle();
}

function _renderFilterPills() {
  const container = document.getElementById('filter-pills');
  if (!container) return;

  const pills = ['all', 'tack', 'gybe', 'rounding', 'weather', 'leeward', 'start', 'P\u2192S', 'S\u2192P', 'good', 'bad'];
  // Only show post-start when at least one session has a gun time recorded.
  const anyGun = Object.values(_raceGunMsBySession).some(Boolean);
  if (anyGun) pills.push('post-start');
  // Wind-range pills (#584).
  for (const b of _CMP_TWS_BANDS) pills.push('tws:' + b.label);

  container.innerHTML = pills.map(f => {
    const active = f === 'all' ? _compareFilter.size === 0 : _compareFilter.has(f);
    const style = 'font-size:.72rem;padding:2px 8px;border:1px solid var(--border);background:'
      + (active ? 'var(--accent)' : 'transparent') + ';color:'
      + (active ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px';
    const label = f.startsWith('tws:') ? f.slice(4) + ' kt' : f;
    return '<button style="' + style + '" onclick="setCompareFilter(\'' + f + '\')" title="' + f + '">' + label + '</button>';
  }).join('');
}

// Override _updateSubtitle to show filtered count
function _updateSubtitle() {
  const total = _allManeuvers.length;
  const visible = _allManeuvers.filter(_matchesCompareFilter).length;
  const el = document.getElementById('compare-subtitle');
  if (!el) return;
  if (visible === total) {
    el.textContent = total + ' maneuver' + (total !== 1 ? 's' : '');
  } else {
    el.textContent = visible + ' of ' + total + ' maneuvers';
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _showEmpty() {
  document.getElementById('compare-grid').style.display = 'none';
  document.getElementById('compare-empty').style.display = '';
}

function _directionHint(m) {
  if ((m.type === 'tack' || m.type === 'gybe') && m.turn_angle_deg != null) {
    return m.turn_angle_deg > 0
      ? ' <span style="color:var(--text-secondary);font-size:.68rem">S&#8594;P</span>'
      : ' <span style="color:var(--text-secondary);font-size:.68rem">P&#8594;S</span>';
  }
  return '';
}

function _rankColor(rank) {
  const m = { good: 'var(--success)', bad: 'var(--error)', avg: 'var(--text-secondary)' };
  return m[rank] || 'var(--text-secondary)';
}

function _fmtElapsed(iso) {
  if (!iso) return '';
  try {
    let s = iso.replace(' ', 'T');
    if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (_e) { return ''; }
}

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
