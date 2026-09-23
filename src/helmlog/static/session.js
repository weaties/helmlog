/* session.js — Session detail page logic */

const cfg = document.getElementById('app-config');
const SESSION_ID = cfg.dataset.sessionId;
initGrafana(cfg.dataset.grafanaPort, cfg.dataset.grafanaUid);

let _session = null;
let _map = null;
let _trackData = null; // {latLngs, timestamps (as Date), line, cursor}
let _videoSync = null; // {syncUtc (Date), syncOffsetS, durationS, player}
let _ytReady = false;
let _syncTimer = null;

// ---------------------------------------------------------------------------
// Playback clock — single source of truth for the session timeline (#446)
//
// Every surface (map, video, audio, transcript) is both a producer (calls
// setPosition when the user interacts with it) and a consumer (renders the
// current position). Producers go through one entry point so there is exactly
// one clock. Echo events from media elements are debounced via _seekingUntil.
// ---------------------------------------------------------------------------

const _playClock = {
  positionUtc: null, // current position as a Date on session UTC timeline
  state: 'idle', // idle | playing | paused | seeking | ended
  consumers: [], // [{name, render(utc)}]
  seekingUntil: 0, // performance.now() ms; ignore echo events until this time
  tickTimer: null,
  tickAnchorUtc: null, // UTC at last tick anchor
  tickAnchorPerf: 0, // performance.now() at last tick anchor
  speed: 1, // replay speed multiplier (1 / 2 / 4 / 8)
};

function _clockNowMs() { return performance.now(); }

function registerSurface(name, render) {
  _playClock.consumers.push({name, render});
}

// Stop every active playback surface (replay tick, YT video, HTML audio element,
// multi-channel Web Audio) so seeks coming from the scrubber/map/keyboard only
// move the playhead — the PR review caught that they were stepping on each
// other and stuttering when multiple surfaces kept playing mid-seek.
function _pauseAllPlayback() {
  try { _stopPlayTick(); } catch (e) { /* swallow */ }
  _playClock.state = 'paused';
  try {
    if (_videoSync && _videoSync.player && typeof _videoSync.player.getPlayerState === 'function') {
      const st = _videoSync.player.getPlayerState();
      if (st === 1 && typeof _videoSync.player.pauseVideo === 'function') {
        _videoSync.player.pauseVideo();
      }
    }
  } catch (e) { /* swallow */ }
  try {
    const aEl = document.getElementById('session-audio')
      || document.querySelector('#audio-body audio');
    if (aEl && !aEl.paused) aEl.pause();
  } catch (e) { /* swallow */ }
  try { if (typeof _mcIsPlaying !== 'undefined' && _mcIsPlaying) _mcPause(); } catch (e) { /* swallow */ }
  try { _updateReplayControls(); } catch (e) { /* not yet wired */ }
}

// Seek helper used by every "click/drag to move the playhead" entry point —
// scrubber, map click, keyboard arrows, maneuver table, discussion anchors.
// Guarantees playback is paused first so nothing auto-resumes after the seek.
function _seekTo(utc, source) {
  _pauseAllPlayback();
  setPosition(utc, {source: source || 'seek'});
}

function setPosition(utc, opts) {
  if (!utc) return;
  const date = utc instanceof Date ? utc : new Date(utc);
  if (isNaN(date.getTime())) return;
  _playClock.positionUtc = date;
  // Suppress media echo events for a brief window after a programmatic seek
  _playClock.seekingUntil = _clockNowMs() + 200;
  // Re-anchor the playing tick on the new position
  _playClock.tickAnchorUtc = date;
  _playClock.tickAnchorPerf = _clockNowMs();
  const source = (opts && opts.source) || null;
  // Stash source so individual consumers can decide whether this producer
  // should drive them. In particular, playback-bearing consumers (video,
  // audio, mc) use this to avoid seeking one player when a different player
  // is the one producing updates — e.g. WAV play must not start YT.
  _playClock.currentSource = source;
  for (const c of _playClock.consumers) {
    if (c.name === source) continue; // don't echo back to producer
    try { c.render(date); } catch (e) { /* never let one surface break others */ }
  }
  _playClock.currentSource = null;
  // Keep the replay scrubber + time label in sync with producer events
  // (YT scrub, WAV scrub) — otherwise only clock-driven ticks update them.
  try { _updateReplayControls(); } catch (e) { /* not yet wired */ }
}

function _isEchoEvent() {
  return _clockNowMs() < _playClock.seekingUntil;
}

function _startPlayTick() {
  _stopPlayTick();
  _playClock.tickAnchorUtc = _playClock.positionUtc;
  _playClock.tickAnchorPerf = _clockNowMs();
  _playClock.state = 'playing';
  _playClock.tickTimer = setInterval(() => {
    if (!_playClock.tickAnchorUtc) return;
    const elapsedMs = (_clockNowMs() - _playClock.tickAnchorPerf) * (_playClock.speed || 1);
    const utc = new Date(_playClock.tickAnchorUtc.getTime() + elapsedMs);
    _playClock.positionUtc = utc;
    // Auto-stop at end of replay window
    if (_replayEnd && utc.getTime() >= _replayEnd.getTime()) {
      _playClock.positionUtc = _replayEnd;
      _stopPlayTick();
      for (const c of _playClock.consumers) {
        try { c.render(_replayEnd); } catch (e) { /* swallow */ }
      }
      _updateReplayControls();
      return;
    }
    for (const c of _playClock.consumers) {
      try { c.render(utc); } catch (e) { /* swallow */ }
    }
    _updateReplayControls();
  }, 100);
}

function _setPlaybackSpeed(newSpeed) {
  if (!newSpeed) return;
  // Re-anchor so speed change doesn't jump the cursor
  _playClock.tickAnchorUtc = _playClock.positionUtc;
  _playClock.tickAnchorPerf = _clockNowMs();
  _playClock.speed = newSpeed;
}

function _stopPlayTick() {
  if (_playClock.tickTimer) {
    clearInterval(_playClock.tickTimer);
    _playClock.tickTimer = null;
  }
  _playClock.state = 'paused';
}

let _maneuvers = []; // loaded maneuver list
let _maneuverMarkers = []; // Leaflet markers for maneuvers
let _vakarosSyntheticStart = null; // {ts, source} placeholder injected from VKX

// Cross-surface seek behaviour: if a render() call moves the playhead by more
// than this many seconds, treat it as a user-initiated jump and pause any
// currently-playing media. Small deltas (playback ticks) keep the media
// running.
const _LARGE_JUMP_SEC = 2.0;

// Project a (lat, lon) point along a true-north bearing for a given distance
// in meters. Equirectangular approximation — accurate to ~1 m at race scale.
function _offsetPoint(lat, lon, bearingDeg, distM) {
  const br = bearingDeg * Math.PI / 180;
  const dy = distM * Math.cos(br);
  const dx = distM * Math.sin(br);
  const newLat = lat + dy / 111320;
  const newLon = lon + dx / (111320 * Math.cos(lat * Math.PI / 180));
  return [newLat, newLon];
}

// Format milliseconds relative to a reference (positive after, negative
// before) as e.g. "T-5:30" or "T+0:42". Used for line-ping tooltips so the
// crew can see how long before the gun each end was set.
function _fmtRelativeToStart(deltaMs) {
  const sign = deltaMs >= 0 ? '+' : '-';
  const absS = Math.round(Math.abs(deltaMs) / 1000);
  const mm = Math.floor(absS / 60);
  const ss = absS % 60;
  return 'T' + sign + mm + ':' + String(ss).padStart(2, '0');
}

// Build a small SVG-based Leaflet divIcon. `opacity` controls the visual
// saturation/strength so older pings can be drawn dimmer than the active
// (most recent) one without changing the color hue.
function _vakarosFlagIcon(color, opacity) {
  const html = '<div style="opacity:' + opacity + ';transform:translate(-4px,-22px);filter:drop-shadow(0 1px 1px rgba(0,0,0,0.5))">'
    + '<svg viewBox="0 0 24 26" width="24" height="26" xmlns="http://www.w3.org/2000/svg">'
    + '<line x1="4" y1="3" x2="4" y2="24" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>'
    + '<path d="M4 3 L20 8 L4 13 Z" fill="' + color + '" stroke="#fff" stroke-width="1"/>'
    + '</svg></div>';
  return L.divIcon({className: 'vakaros-flag', html: html, iconSize: [24, 26], iconAnchor: [4, 22]});
}

function _vakarosBoatIcon(color, opacity) {
  const html = '<div style="opacity:' + opacity + ';transform:translate(-14px,-12px);filter:drop-shadow(0 1px 1px rgba(0,0,0,0.5))">'
    + '<svg viewBox="0 0 28 22" width="28" height="22" xmlns="http://www.w3.org/2000/svg">'
    + '<line x1="14" y1="12" x2="14" y2="2" stroke="#fff" stroke-width="1.6"/>'
    + '<path d="M14 4 L22 12 L14 12 Z" fill="' + color + '" stroke="#fff" stroke-width="0.8"/>'
    + '<path d="M2 12 L26 12 L22 19 L6 19 Z" fill="' + color + '" stroke="#fff" stroke-width="1"/>'
    + '</svg></div>';
  return L.divIcon({className: 'vakaros-boat', html: html, iconSize: [28, 22], iconAnchor: [14, 12]});
}

// Per-pane transcript state (#648). Both race and debrief transcripts use
// the same rendering pipeline; each pane gets its own slot so follow state,
// active-segment highlight, and block storage stay isolated.
//
//   _transcriptPanes[paneKey] = {
//     containerId, followBtnId, surfaceName,
//     audioSessionId,      // primary audio_session_id for this pane
//     audioStartUtc,       // Date anchor for clock math
//     onPlay(block),       // click-to-play handler
//     onOpenOverride(block), // pencil-click handler
//     speakerMap,          // label → crew entry
//     blocks,              // merged display blocks
//     follow,              // auto-scroll enabled
//     lastActiveIdx,       // last-rendered active block
//     surfaceRegistered,   // registerSurface called once per pane
//     lastProgrammaticScrollAt,  // timestamp of our last scroll — used to
//                                // ignore our own scroll events
//   }
const _transcriptPanes = {};

// Back-compat alias so legacy code keeps working until all call sites move
// off the global (#648).
let _transcriptFollow = true;

function _scrollTranscriptSegmentIntoView(container, el, paneKey) {
  // Uses getBoundingClientRect (#648) instead of offsetTop because offsetTop
  // is relative to the nearest *positioned* ancestor, which is not always
  // the scroll container — when it isn't, assigning the raw offsetTop to
  // container.scrollTop lands at a wildly wrong position (the symptom the
  // user reported as "transcript jumps to god only knows where").
  const pane = paneKey ? _transcriptPanes[paneKey] : null;
  const follow = pane ? pane.follow : _transcriptFollow;
  if (!follow) return false;
  const margin = 4;
  const cRect = container.getBoundingClientRect();
  const eRect = el.getBoundingClientRect();
  const relTop = eRect.top - cRect.top + container.scrollTop;
  const relBottom = relTop + el.offsetHeight;
  const top = relTop - margin;
  const bottom = relBottom + margin;
  let target = null;
  if (top < container.scrollTop) {
    target = top;
  } else if (bottom > container.scrollTop + container.clientHeight) {
    target = bottom - container.clientHeight;
  }
  if (target == null) return false;
  const now = Date.now();
  if (pane) pane.lastProgrammaticScrollAt = now;
  container.scrollTop = Math.max(0, target);
  return true;
}

function _wireTranscriptScrollListener(paneKey) {
  const pane = _transcriptPanes[paneKey];
  if (!pane) return;
  const container = document.getElementById(pane.containerId);
  if (!container || container._scrollWired) return;
  container._scrollWired = true;
  container.addEventListener('scroll', function() {
    // Ignore the scroll events we triggered ourselves (400 ms window).
    if (Date.now() - (pane.lastProgrammaticScrollAt || 0) < 400) return;
    if (pane.follow) {
      pane.follow = false;
      if (paneKey === 'race') _transcriptFollow = false;
      _renderTranscriptFollowBadge(paneKey);
    }
  });
}

function _renderTranscriptFollowBadge(paneKey) {
  const pane = _transcriptPanes[paneKey];
  if (!pane) return;
  const btn = document.getElementById(pane.followBtnId);
  if (!btn) return;
  if (pane.follow) {
    btn.textContent = '\u25C9 Follow';
    btn.style.opacity = '1';
    btn.title = 'Auto-scrolling to active segment. Click to pause.';
  } else {
    btn.textContent = '\u25CB Follow';
    btn.style.opacity = '0.55';
    btn.title = 'Auto-scroll paused (you scrolled manually). Click to resume.';
  }
}

function toggleTranscriptFollow(paneKey) {
  const key = paneKey || 'race';
  const pane = _transcriptPanes[key];
  if (!pane) return;
  pane.follow = !pane.follow;
  if (key === 'race') _transcriptFollow = pane.follow;
  _renderTranscriptFollowBadge(key);
}
let _transcriptId = null; // transcript ID for tuning extraction
let _tuningSegmentAudio = null; // shared <audio> for segment playback
let _tuningSegmentTimer = null; // timeupdate stop timer
let _transcriptAudio = null; // shared <audio> for transcript segment playback
let _transcriptBlocks = []; // merged transcript blocks for highlight tracking
let _speakerMap = {}; // speaker_map from API (crew assignments)

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

async function init() {
  await initTimezone();

  // Live-race strip-down (#664): toggle the body class before any loaders
  // fire so CSS hides the non-essential cards as the page settles, rather
  // than flashing them on screen and yanking them away.
  if (cfg.dataset.live === '1') {
    document.body.classList.add('live-race');
  }

  // If a deep-link query string targets a specific moment, start opening
  // it immediately so the user sees the moment detail without waiting for
  // the rest of the session page (track, video, polar, etc.) to finish
  // loading. The moments card shows a spinner; loadMoments() runs later
  // alongside everything else and seeds _threads / map markers.
  _maybeOpenDeepLinkMoment();

  const r = await fetch('/api/sessions/' + SESSION_ID + '/detail');
  if (!r.ok) {
    document.getElementById('session-name').textContent = 'Session not found';
    return;
  }
  _session = await r.json();

  renderHeader();
  // Load track and videos in parallel, then wire up sync
  await Promise.all([loadTrack(), loadVideoPlayer()]);
  loadManeuvers();
  loadVideos();
  if (_session.has_wind_field) loadWindField();
  if (_session.type !== 'debrief') {
    loadResults();
    loadCrew();
    loadSails();
    loadBoatSettings();
    if (_session.end_utc) loadPolar();
    loadAnalysis();
  }
  if (_session.has_audio && _session.audio_session_id) {
    loadTranscript();
    loadAudio();
  }
  // When a deep-link moment is in flight, skip the list body render so
  // openThread()'s detail view (already rendering in parallel) isn't
  // clobbered. The moment list still gets populated (_threads, anchor
  // index, map markers) so the back-button can repaint instantly.
  await loadMoments({renderList: !_deepLinkMomentInFlight});
  if (!_deepLinkMomentInFlight) _checkThreadHash();
  loadSharing();
  loadMatch();
  renderExports();
  renderDangerZone();
  if (cfg.dataset.live === '1') _startLiveRefresh();
  _applyDeepLink();
}

let _deepLinkMomentInFlight = false;

function _maybeOpenDeepLinkMoment() {
  const params = new URLSearchParams(window.location.search);
  const momentParam = params.get('moment') || params.get('thread');
  if (!momentParam) return;
  const momentId = parseInt(momentParam, 10);
  if (isNaN(momentId)) return;
  const commentParam = params.get('comment');
  const commentId = commentParam ? parseInt(commentParam, 10) : null;
  const card = document.getElementById('moments-card');
  if (card) card.style.display = '';
  _deepLinkMomentInFlight = true;
  // openThread is async — fire it and let it render while init() continues
  // its other work in parallel.
  openThread(momentId, commentId && !isNaN(commentId) ? commentId : null);
}

// ---------------------------------------------------------------------------
// Live refresh — while a race is in progress and this session is served at /
// (see #635). Reload the track + videos periodically so the map extends as
// the boat moves and newly-uploaded clips appear. Connects to /ws/live as
// a presence signal; falls back to polling if the socket is unavailable.
// ---------------------------------------------------------------------------

const _LIVE_REFRESH_MS = 15000;
let _liveInterval = null;
let _liveWs = null;
let _liveLastRefresh = 0;
let _livePhotoUrl = null;
let _liveWsBackoffMs = 1000;
let _liveWsRetryTimer = null;
let _liveGotPositionViaWs = false;

async function _liveRefreshOnce() {
  // Polling fallback. Only invoked if the WS is dead — once a position
  // arrives over the socket, _liveGotPositionViaWs flips and this stops
  // re-fetching the track endpoint (the expensive part on cellular).
  const now = Date.now();
  if (now - _liveLastRefresh < _LIVE_REFRESH_MS - 500) return;
  _liveLastRefresh = now;
  try {
    const tasks = [loadVideos(), _refreshLivePhoto()];
    if (!_liveGotPositionViaWs) tasks.push(loadTrack());
    await Promise.all(tasks);
  } catch (e) { /* non-fatal */ }
}

async function _refreshLivePhoto() {
  const card = document.getElementById('live-photo-card');
  if (!card) return;
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/notes');
    if (!r.ok) { card.style.display = 'none'; return; }
    const notes = await r.json();
    const photos = notes.filter(n => n.note_type === 'photo' && n.photo_path);
    if (!photos.length) {
      card.style.display = 'none';
      _livePhotoUrl = null;
      return;
    }
    photos.sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));
    const latest = photos[0];
    const url = '/notes/' + latest.photo_path;
    card.style.display = '';
    if (url !== _livePhotoUrl) {
      document.getElementById('live-photo-img').src = url;
      document.getElementById('live-photo-link').href = url;
      _livePhotoUrl = url;
    }
    document.getElementById('live-photo-ts').textContent = fmtTime(latest.ts);
  } catch (e) { /* non-fatal */ }
}

// Haversine distance in nautical miles between two [lat, lng] points.
// Used by the live distance gauges and the boat→start straight-line
// computation. 3440.065 nm is the Earth's mean radius in nm.
function _haversineNm(a, b) {
  const toRad = (d) => d * Math.PI / 180;
  const dLat = toRad(b[0] - a[0]);
  const dLon = toRad(b[1] - a[1]);
  const lat1 = toRad(a[0]);
  const lat2 = toRad(b[0]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * 3440.065 * Math.asin(Math.sqrt(h));
}

// Cumulative track length (nm) from start_utc to the latest fix. Iterates
// _trackData.latLngs and sums Haversine legs from the first point at or
// after race start_utc.
let _gunLatLngIdx = -1;
function _recomputeDistanceSailed() {
  if (!_trackData || !_trackData.latLngs.length || !_session || !_session.start_utc) return;
  const startMs = new Date(_session.start_utc).getTime();
  if (_gunLatLngIdx < 0) {
    for (let i = 0; i < _trackData.timestamps.length; i++) {
      if (_trackData.timestamps[i].getTime() >= startMs) { _gunLatLngIdx = i; break; }
    }
    if (_gunLatLngIdx < 0) return; // no post-gun fixes yet
  }
  let total = 0;
  for (let i = _gunLatLngIdx + 1; i < _trackData.latLngs.length; i++) {
    total += _haversineNm(_trackData.latLngs[i - 1], _trackData.latLngs[i]);
  }
  const el = document.getElementById('hud-dist-sailed');
  if (el) el.textContent = total.toFixed(2);
}

// Course distance: start → rounding₁ → … → rounding_n → boat. With no
// roundings yet (the live-race case — detector only runs at race-end),
// degrades to start → boat. _roundings is populated lazily from the
// maneuvers endpoint once it's been run.
let _roundings = []; // [{ts, lat, lon}]
function _recomputeCourseDistance() {
  if (!_trackData || !_trackData.latLngs.length || !_session || !_session.start_utc) return;
  if (_gunLatLngIdx < 0) return;
  const startLatLng = _trackData.latLngs[_gunLatLngIdx];
  const boat = _trackData.latLngs[_trackData.latLngs.length - 1];
  let total = 0;
  let prev = startLatLng;
  for (const r of _roundings) {
    const here = [r.lat, r.lon];
    total += _haversineNm(prev, here);
    prev = here;
  }
  total += _haversineNm(prev, boat);
  const el = document.getElementById('hud-dist-line');
  if (el) el.textContent = total.toFixed(2);
}

// Resolve roundings from the maneuvers list to (lat, lon) by snapping
// each rounding's ts to the nearest position in _trackData. Stored in
// _roundings so the course-distance compute can iterate them. Refreshes
// any time _maneuvers or _trackData changes.
function _populateRoundings() {
  if (typeof _maneuvers === 'undefined' || !_maneuvers || !_trackData) {
    _roundings = [];
    return;
  }
  const tsList = _trackData.timestamps;
  if (!tsList.length) { _roundings = []; return; }
  const out = [];
  for (const m of _maneuvers) {
    if (m.type !== 'rounding') continue;
    const tsMs = new Date(m.ts.endsWith('Z') || m.ts.includes('+') ? m.ts : m.ts + 'Z').getTime();
    let bestIdx = -1, bestDt = Infinity;
    for (let i = 0; i < tsList.length; i++) {
      const dt = Math.abs(tsList[i].getTime() - tsMs);
      if (dt < bestDt) { bestDt = dt; bestIdx = i; }
    }
    if (bestIdx >= 0) {
      const ll = _trackData.latLngs[bestIdx];
      out.push({ts: m.ts, lat: ll[0], lon: ll[1]});
    }
  }
  _roundings = out;
}

// Latest live instrument snapshot from the WS — used to enrich the
// synthetic _replaySamples row we push when a new position arrives, so
// the scrubber HUD has data when pinned at the live tip.
let _lastLiveInstruments = null;

// Push live instrument values straight into the GAUGES card so it stays
// real-time without polling /api/instruments. Mirrors the binding inside
// _renderHud() — but driven by the WS message instead of the scrubber.
// Set / drift now arrive pre-computed from the server (#729) so we just
// bind them here alongside the raw measurements.
function _renderLiveGauges(d) {
  if (!d) return;
  _lastLiveInstruments = d;
  const setNum = (id, val, decimals) => {
    const el = document.getElementById(id);
    if (el) el.textContent = (val != null && !Number.isNaN(val)) ? Number(val).toFixed(decimals) : '—';
  };
  const setDeg = (id, val) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (val == null || Number.isNaN(val)) { el.textContent = '—'; return; }
    const wrapped = ((Math.round(val) % 360) + 360) % 360;
    el.textContent = wrapped + '°';
  };
  const setSigned = (id, val, decimals) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (val == null || Number.isNaN(val)) { el.textContent = '—'; return; }
    const sign = Number(val) >= 0 ? '+' : '';
    el.textContent = sign + Number(val).toFixed(decimals) + '°';
  };
  setNum('hud-stw', d.bsp_kts, 2);
  setNum('hud-sog', d.sog_kts, 2);
  setNum('hud-tws', d.tws_kts, 1);
  setNum('hud-aws', d.aws_kts, 1);
  setDeg('hud-twd', d.twd_deg);
  setDeg('hud-twa', d.twa_deg);
  setDeg('hud-awa', d.awa_deg);
  setNum('hud-hdg', d.heading_deg, 0);
  setNum('hud-cog', d.cog_deg, 0);
  setSigned('hud-heel', d.heel_deg, 1);
  setSigned('hud-trim', d.trim_deg, 1);
  setDeg('hud-set', d.set_deg);
  setNum('hud-drift', d.drift_kts, 2);
}

// Throttle for live overlay rebuilds — wind/current arrow layers iterate
// the entire track on each rebuild, so 1 Hz would be wasteful. The
// existing arrow cadence is multi-second (zoom-dependent) so a 10 s
// rebuild is plenty to extend coverage along the new track segment.
let _lastOverlayRebuildMs = 0;
const _OVERLAY_REBUILD_INTERVAL_MS = 10000;

// Append a position fix to the polyline + casing and refresh distances.
// If the user is pinned at the end of the scrubber, also advance the
// replay clock so the boat icon, HUD, and overlays follow the live tip.
// No-op if the fix is older than the last one we have (out-of-order
// arrival, e.g. on reconnect after a brief disconnect).
function _appendLivePosition(payload) {
  if (!_trackData || !payload || payload.lat == null || payload.lon == null) return;
  const ts = new Date(payload.ts.endsWith('Z') || payload.ts.includes('+') ? payload.ts : payload.ts + 'Z');
  const last = _trackData.timestamps[_trackData.timestamps.length - 1];
  if (last && ts <= last) return;
  _trackData.latLngs.push([payload.lat, payload.lon]);
  _trackData.timestamps.push(ts);
  if (_trackData.line) _trackData.line.setLatLngs(_trackData.latLngs);
  if (_trackData.casing) _trackData.casing.setLatLngs(_trackData.latLngs);
  _liveGotPositionViaWs = true;
  _recomputeDistanceSailed();
  _recomputeCourseDistance();

  // Live-tail follow: extend the scrubber range and, if the user was
  // pinned at the end (within 1 s of _replayEnd), advance the play
  // clock so the boat cursor, HUD, sparklines, and overlay arrows all
  // follow the new fix.
  if (_replayEnd && _replaySamples) {
    const wasAtEnd = _playClock.positionUtc &&
                     _playClock.positionUtc.getTime() >= _replayEnd.getTime() - 1000;
    if (_lastLiveInstruments) {
      const inst = _lastLiveInstruments;
      _replaySamples.push({
        ts: ts,
        stw: inst.bsp_kts,
        sog: inst.sog_kts,
        tws: inst.tws_kts,
        twa: inst.twa_deg,
        twd: inst.twd_deg,
        aws: inst.aws_kts,
        awa: inst.awa_deg,
        hdg: inst.heading_deg,
        cog: inst.cog_deg,
        heel: inst.heel_deg,
        trim: inst.trim_deg,
        set: inst.set_deg,
        drift: inst.drift_kts,
      });
    }
    _replayEnd = ts;
    if (wasAtEnd) {
      // setPosition fans out to all _playClock consumers — the map
      // cursor, HUD, sparklines, etc. — so the live tip stays selected
      // and _updateReplayControls() keeps the scrubber in sync.
      setPosition(ts, {source: 'live-tail'});
    } else if (typeof _updateReplayControls === 'function') {
      // User has scrubbed back; don't move the playhead, but keep the
      // scrubber's max + duration label in sync with the new end.
      _updateReplayControls();
    }

    // Throttled rebuild so wind/current arrows extend along the new
    // segment of track. Skipped if neither overlay is enabled.
    const nowMs = Date.now();
    if (nowMs - _lastOverlayRebuildMs >= _OVERLAY_REBUILD_INTERVAL_MS) {
      _lastOverlayRebuildMs = nowMs;
      if (_currentEnabled && typeof _rebuildCurrentOverlay === 'function') _rebuildCurrentOverlay();
      if (_windEnabled && typeof _rebuildWindOverlay === 'function') _rebuildWindOverlay();
    }
  }
}

function _handleLiveWsMessage(ev) {
  let msg;
  try { msg = JSON.parse(ev.data); } catch (e) { return; }
  if (msg.type === 'position') {
    _appendLivePosition(msg.data);
  } else if (msg.type === 'instruments') {
    _renderLiveGauges(msg.data);
  }
}

function _connectLiveWs() {
  try {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    _liveWs = new WebSocket(`${proto}//${location.host}/ws/live`);
    _liveWs.onmessage = _handleLiveWsMessage;
    _liveWs.onopen = () => { _liveWsBackoffMs = 1000; };
    _liveWs.onclose = () => {
      _liveWs = null;
      // Reconnect with exponential backoff capped at 30 s. The polling
      // fallback in _liveRefreshOnce() takes over (re-fetching the full
      // track) until the socket comes back.
      _liveGotPositionViaWs = false;
      if (_liveWsRetryTimer) clearTimeout(_liveWsRetryTimer);
      _liveWsRetryTimer = setTimeout(_connectLiveWs, _liveWsBackoffMs);
      _liveWsBackoffMs = Math.min(_liveWsBackoffMs * 2, 30000);
    };
    _liveWs.onerror = () => { try { _liveWs.close(); } catch (e) {} };
  } catch (e) { /* fallback to polling */ }
}

function _startLiveRefresh() {
  if (_liveInterval) return;
  _liveInterval = setInterval(_liveRefreshOnce, _LIVE_REFRESH_MS);
  _refreshLivePhoto();
  _connectLiveWs();
}

// ---------------------------------------------------------------------------
// Danger zone — session deletion (#409)
// ---------------------------------------------------------------------------

function renderDangerZone() {
  const role = cfg.dataset.userRole;
  if (role !== 'admin') return;
  if (!_session.end_utc) return; // active session — cannot delete
  document.getElementById('danger-zone').style.display = '';
}

async function deleteSession() {
  const name = _session.name || 'this session';
  if (!confirm('Delete "' + name + '"?\n\nThis will permanently remove all data, audio, and files. This cannot be undone.')) return;
  const btn = document.getElementById('delete-session-btn');
  btn.disabled = true;
  btn.textContent = 'Deleting\u2026';
  const r = await fetch('/api/sessions/' + SESSION_ID, { method: 'DELETE' });
  if (r.ok) {
    window.location.href = '/history';
  } else {
    btn.disabled = false;
    btn.textContent = 'Delete Session';
    const data = await r.json().catch(() => null);
    alert('Delete failed: ' + (data && data.detail ? data.detail : r.statusText));
  }
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function renderHeader() {
  const s = _session;
  const typeClass = s.type === 'race' ? 'badge-race'
    : s.type === 'practice' ? 'badge-practice'
    : s.type === 'synthesized' ? 'badge-synthesized'
    : 'badge-debrief';
  const badge = '<span class="badge ' + typeClass + '">' + s.type.toUpperCase() + '</span>';
  const peerBadge = s.peer_fingerprint
    ? '<span class="badge badge-peer" title="Peer boat">PEER</span>'
    : '';
  const matchBadge = s.match_status === 'confirmed'
    ? '<span class="badge badge-practice" title="Co-op matched">MATCHED</span>'
    : s.match_status === 'candidate'
    ? '<span class="badge badge-debrief" title="Pending match">PENDING</span>'
    : '';
  const displayName = s.shared_name || s.name;
  document.getElementById('session-name').innerHTML = esc(displayName) + badge + peerBadge + matchBadge;

  const start = fmtTime(s.start_utc);
  const end = s.end_utc ? fmtTime(s.end_utc) : 'in progress';
  const dur = (s.end_utc && s.duration_s != null) ? ' (' + fmtDuration(Math.round(s.duration_s)) + ')' : '';
  let meta = s.date + ' &middot; ' + start + ' &rarr; ' + end + dur;
  if (s.shared_name) meta += '<br><span style="font-size:.72rem;color:var(--text-secondary)">Local: ' + esc(s.name) + '</span>';
  document.getElementById('session-meta').innerHTML = meta;
}

// ---------------------------------------------------------------------------
// Track map
// ---------------------------------------------------------------------------

const TRACK_SIZE_KEY = 'helmlog.session.trackMapSize';
const TRACK_SIZES = ['s', '', 'l', 'xl'];

function applyTrackSize(size, map) {
  const el = document.getElementById('track-map');
  if (!el) return;
  TRACK_SIZES.forEach(s => { if (s) el.classList.remove('size-' + s); });
  if (size) el.classList.add('size-' + size);
  document.querySelectorAll('.track-size-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.size === size);
  });
  if (map) setTimeout(() => map.invalidateSize(), 160);
}

function initTrackSizeControls(map) {
  let saved = '';
  try { saved = localStorage.getItem(TRACK_SIZE_KEY) || ''; } catch (e) {}
  if (!TRACK_SIZES.includes(saved)) saved = '';
  applyTrackSize(saved, null);
  document.querySelectorAll('.track-size-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const size = btn.dataset.size;
      applyTrackSize(size, map);
      try { localStorage.setItem(TRACK_SIZE_KEY, size); } catch (e) {}
    });
  });
}

async function loadTrack() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/track');
  const geojson = await r.json();
  if (!geojson.features || !geojson.features.length) return;

  const container = document.getElementById('track-container');
  container.style.display = '';

  _map = L.map('track-map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap', maxZoom: 18,
  }).addTo(_map);
  // Expose the map so the race-start widget can overlay the HelmLog
  // start line + bias on the prestart track (#644). Read-only consumer.
  window._helmlogLeafletMap = _map;
  initTrackSizeControls(_map);
  _restorePersistedSections();

  const feature = geojson.features[0];
  const coords = feature.geometry.coordinates;
  const rawTimestamps = feature.properties.timestamps || [];
  const latLngs = coords.map(c => [c[1], c[0]]);
  const timestamps = rawTimestamps.map(t => new Date(t.endsWith('Z') || t.includes('+') ? t : t + 'Z'));
  // Instrument (SK) track: dashed like the Vakaros overlay but with a
  // distinct color and a longer dash period, so the two patterns don't
  // land on top of each other when both tracks are shown. Vakaros uses
  // '2,4' (dash 2, gap 4, 6px cycle); we use '6,6' (12px cycle). The
  // different cycle lengths prevent consistent overlap and the different
  // dash sizes read clearly even where they briefly coincide. Butt caps
  // keep the dashes sharply rectangular for that "super crisp" look.
  //
  // The polyline is drawn from a moving-average smoothed copy of the
  // GPS samples. Raw 1 Hz fixes are noisy enough that the line looks
  // fuzzy when zoomed in (see the user report of a wavy track). The
  // cursor continues to interpolate against the raw latLngs so the
  // boat position itself isn't lagged by the smoothing.
  const trackColor = cssVar('--warning') || '#fbbf24';
  // Dark casing under the bright dashed line so the track reads against
  // light water tiles — the dashed yellow alone disappeared on OSM blue.
  // The casing is solid + slightly wider, the bright line is dashed on
  // top, preserving the Vakaros-style visual identity.
  const casing = L.polyline(latLngs, {
    color: '#1a1a1a',
    weight: 5,
    opacity: 0.55,
    lineCap: 'round',
    lineJoin: 'round',
  }).addTo(_map);
  const line = L.polyline(latLngs, {
    color: trackColor,
    weight: 3,
    opacity: 1,
    lineCap: 'butt',
    lineJoin: 'miter',
    dashArray: '2, 4',
  }).addTo(_map);

  const successColor = cssVar('--success');
  const dangerColor = cssVar('--danger');
  const warningColor = cssVar('--warning');
  L.circleMarker(latLngs[0], {radius: 6, color: successColor, fillColor: successColor, fillOpacity: 1})
    .addTo(_map).bindPopup('Start');
  L.circleMarker(latLngs[latLngs.length - 1], {radius: 6, color: dangerColor, fillColor: dangerColor, fillOpacity: 1})
    .addTo(_map).bindPopup('Finish');

  // Boat cursor: divIcon with a rotating SVG so we can show heading (boat
  // orientation) and COG (a separate indicator line) in one marker. The DOM
  // is built once and mutated in-place on each tick to avoid re-creating
  // the Leaflet layer at 10 Hz.
  const cursorIcon = L.divIcon({
    className: 'boat-cursor',
    html: _renderBoatCursorSvg(0, 0),
    iconSize: [64, 64],
    iconAnchor: [32, 32],
  });
  const cursor = L.marker([0, 0], {icon: cursorIcon, interactive: false});

  _trackData = {latLngs, timestamps, line, casing, cursor};
  // Reset the gun-index cache so the next compute walks from scratch.
  _gunLatLngIdx = -1;
  _recomputeDistanceSailed();
  _populateRoundings();
  _recomputeCourseDistance();

  // Map is a consumer: render the cursor at the requested UTC. We use a
  // continuous interpolated position (not the nearest sample index) so the
  // boat glides along the polyline instead of stepping between fixes — the
  // stepping was especially visible at 8x replay.
  registerSurface('map', function(utc) {
    if (!_trackData) return;
    _moveCursorToUtc(utc);
    _updateBoatSettingsForUtc(utc);
    _updateBoatInstrument(utc);
  });

  // Click track → seek the playback clock (which then seeks video, audio, etc.)
  line.on('click', function(e) {
    const idx = _nearestIndex(e.latlng);
    const utc = _utcForIndex(idx);
    if (utc) _seekTo(utc, 'map');
    // Map producer still updates its own cursor immediately
    _moveCursorToIndex(idx);
  });

  // Right-click track → start discussion at that point
  line.on('contextmenu', function(e) {
    L.DomEvent.preventDefault(e);
    const idx = _nearestIndex(e.latlng);
    const utc = _utcForIndex(idx);
    if (utc) {
      _moveCursorToIndex(idx);
      showNewMomentForm(utc.toISOString());
    }
  });

  _map.fitBounds(line.getBounds(), {padding: [20, 20]});
  document.getElementById('track-hint').textContent = 'Click track to seek \u00b7 Right-click to start a moment at that point';

  // #458 — if a matched Vakaros session exists, overlay start line, line pings,
  // and race-start marker on top of the SK track.
  try {
    await loadVakarosOverlay();
  } catch (err) {
    console.warn('Vakaros overlay failed to load:', err);
  }

  // HelmLog start-line overlay (#644 + bias-overlay): boat/pin markers and
  // a time-synced bias indicator that updates as the scrubber moves.
  try {
    await loadHelmlogStartLineOverlay();
  } catch (err) {
    console.warn('HelmLog start-line overlay failed to load:', err);
  }
}

async function loadVakarosOverlay() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/vakaros-overlay');
  if (!r.ok) return;
  const data = await r.json();
  if (!data || !data.matched) return;

  const pinColor = cssVar('--warning') || '#f59e0b';
  const boatColor = cssVar('--accent-strong') || '#60a5fa';
  const startColor = cssVar('--success') || '#34d399';
  const vakarosTrackColor = cssVar('--accent') || '#8b5cf6';

  // Line-position markers (pin = flag, committee boat = boat icon).
  // Group by type so we can saturate the most recent of each type at full
  // strength and dim earlier pings.
  const linePings = data.line_positions || [];
  const raceStartCtx = data.race_start_context;
  const raceStartMs = raceStartCtx && raceStartCtx.ts
    ? new Date(raceStartCtx.ts.endsWith('Z') || raceStartCtx.ts.includes('+') ? raceStartCtx.ts : raceStartCtx.ts + 'Z').getTime()
    : null;
  const byType = {pin: [], boat: []};
  for (const lp of linePings) {
    if (byType[lp.line_type]) byType[lp.line_type].push(lp);
  }
  ['pin', 'boat'].forEach(function(type) {
    const pings = byType[type];
    pings.forEach(function(lp, idx) {
      const isLatest = idx === pings.length - 1;
      const opacity = isLatest ? 1.0 : 0.4;
      const color = type === 'pin' ? pinColor : boatColor;
      const label = type === 'pin' ? 'Pin' : 'Committee boat';
      const icon = type === 'pin'
        ? _vakarosFlagIcon(color, opacity)
        : _vakarosBoatIcon(color, opacity);
      // Tooltip + popup: when this ping was set, relative to race start.
      const lpMs = new Date(lp.ts.endsWith('Z') || lp.ts.includes('+') ? lp.ts : lp.ts + 'Z').getTime();
      let tip = 'Vakaros ' + label + ' ping';
      if (raceStartMs != null) {
        tip += ' \u00b7 ' + _fmtRelativeToStart(lpMs - raceStartMs);
      }
      if (!isLatest) tip += ' (earlier)';
      const marker = L.marker([lp.latitude_deg, lp.longitude_deg], {icon: icon})
        .addTo(_map)
        .bindTooltip(tip)
        .bindPopup(tip);
      // Push the active (latest) ping to the top of the z-order.
      if (isLatest && marker.setZIndexOffset) marker.setZIndexOffset(500);
    });
  });

  // Start line (dashed polyline between the most recent pin and boat pings).
  if (data.line) {
    L.polyline([data.line.pin, data.line.boat], {
      color: pinColor, weight: 3, dashArray: '6, 6', opacity: 0.9,
    })
      .addTo(_map)
      .bindTooltip('Vakaros start line', {sticky: true})
      .bindPopup('Vakaros start line');

    // Wind ticks: a short line from each line endpoint pointing UPWIND
    // at the moment of the start gun. Lets you eyeball the bias visually
    // — if both ticks make the same angle with the start line, the line
    // is square; otherwise the more-perpendicular end is favoured.
    const ctx = data.race_start_context;
    if (ctx && ctx.twd_deg != null) {
      const tickLen = Math.max(60, (data.line.length_m || 0) * 0.6);
      const pinUp = _offsetPoint(data.line.pin[0], data.line.pin[1], ctx.twd_deg, tickLen);
      const boatUp = _offsetPoint(data.line.boat[0], data.line.boat[1], ctx.twd_deg, tickLen);
      const twdLabel = Math.round(ctx.twd_deg) + '\u00b0';
      const tws = ctx.tws_kts != null ? ctx.tws_kts.toFixed(1) + ' kt' : '?';
      const popup = 'Wind at gun: ' + twdLabel + ' \u00b7 ' + tws;
      L.polyline([data.line.pin, pinUp], {
        color: vakarosTrackColor, weight: 3, opacity: 0.9,
      })
        .addTo(_map)
        .bindTooltip(popup, {sticky: true})
        .bindPopup(popup);
      L.polyline([data.line.boat, boatUp], {
        color: vakarosTrackColor, weight: 3, opacity: 0.9,
      })
        .addTo(_map)
        .bindTooltip(popup, {sticky: true})
        .bindPopup(popup);
      // Small marker at each upwind tip so the direction reads cleanly,
      // and so there's a generous hover target at the end of each tick.
      L.circleMarker(pinUp, {
        radius: 4, color: vakarosTrackColor, fillColor: vakarosTrackColor, fillOpacity: 1, weight: 1,
      }).addTo(_map).bindTooltip(popup);
      L.circleMarker(boatUp, {
        radius: 4, color: vakarosTrackColor, fillColor: vakarosTrackColor, fillOpacity: 1, weight: 1,
      }).addTo(_map).bindTooltip(popup);

      // Start-line laylines (#473): from each end of the line, extend two
      // tack laylines at the upwind-approach angle relative to the wind
      // when the gun went off. Length scales with the start line itself
      // (1/3 of line length) so the lines stay proportional at every
      // zoom. Color is muted rose so they read clearly without washing
      // out the more important start-line + wind ticks.
      const LAYLINE_LEN_M = Math.max(60, (data.line.length_m || 180) / 3);
      const TACK_HALF = 45;
      const TACK_COLOR = '#f472b6';  // muted pink — less saturated than the rounding lines
      const windTo = (ctx.twd_deg + 180) % 360;
      const stbdBearing = (windTo + TACK_HALF) % 360;
      const portBearing = (windTo - TACK_HALF + 360) % 360;
      for (const end of [data.line.pin, data.line.boat]) {
        const stbd = _offsetPoint(end[0], end[1], stbdBearing, LAYLINE_LEN_M);
        const port = _offsetPoint(end[0], end[1], portBearing, LAYLINE_LEN_M);
        const opts = {color: TACK_COLOR, weight: 2, opacity: 0.7, dashArray: '4, 6', lineCap: 'butt'};
        L.polyline([end, stbd], opts).addTo(_map);
        L.polyline([end, port], opts).addTo(_map);
      }
    }

    // Line info panel below the map.
    const infoEl = document.getElementById('vakaros-line-info');
    if (infoEl) {
      const bearing = data.line.bearing_deg.toFixed(1).padStart(5, '0');
      let txt = 'Start line: ' + data.line.length_m.toFixed(1) + ' m \u00b7 ' +
        bearing + '\u00b0 T (pin \u2192 boat)';
      if (ctx && ctx.twd_deg != null) {
        txt += ' \u00b7 wind ' + Math.round(ctx.twd_deg) + '\u00b0 T';
      }
      if (ctx && ctx.line_bias_deg != null && ctx.favored_end) {
        if (ctx.favored_end === 'square') {
          txt += ' \u00b7 square';
        } else {
          txt += ' \u00b7 ' + Math.abs(ctx.line_bias_deg).toFixed(1) +
            '\u00b0 favoring ' + ctx.favored_end;
        }
      }
      infoEl.textContent = txt;
      infoEl.style.display = '';
    }
  }

  // Vakaros track polyline — drawn but hidden by default.  A checkbox above
  // the map lets the user toggle SK vs Vakaros track independently.
  let vakarosLine = null;
  if (data.track && data.track.geometry && data.track.geometry.coordinates.length) {
    const vakLatLngs = data.track.geometry.coordinates.map(c => [c[1], c[0]]);
    vakarosLine = L.polyline(vakLatLngs, {
      color: vakarosTrackColor,
      weight: 3,
      opacity: 0.9,
      lineCap: 'butt',
      lineJoin: 'miter',
      // Intentionally kept shorter than the SK track's '6,6' so the two
      // dash cycles don't align when both overlays are visible.
      dashArray: '2, 4',
    }).bindPopup('Vakaros track');
    // Reveal the selector now that there's something to toggle.
    const selector = document.getElementById('vakaros-track-toggle');
    if (selector) selector.style.display = '';
    const vkBox = document.getElementById('toggle-vakaros-track');
    const skBox = document.getElementById('toggle-sk-track');
    if (vkBox) {
      vkBox.addEventListener('change', function() {
        if (vkBox.checked) { vakarosLine.addTo(_map); }
        else { _map.removeLayer(vakarosLine); }
      });
    }
    if (skBox && _trackData && _trackData.line) {
      skBox.addEventListener('change', function() {
        if (skBox.checked) {
          if (_trackData.casing) _trackData.casing.addTo(_map);
          _trackData.line.addTo(_map);
        } else {
          _map.removeLayer(_trackData.line);
          if (_trackData.casing) _map.removeLayer(_trackData.casing);
        }
      });
    }
  }

  // Race-start marker on the SK track, positioned at the point closest in time
  // to the RACE_START event. Also inject a synthetic "start" entry into the
  // maneuvers panel so the race start shows up in the event list.
  const raceStart = (data.race_events || []).find(e => e.event_type === 'race_start');
  if (raceStart && _trackData && _trackData.timestamps.length) {
    const startUtc = new Date(raceStart.ts.endsWith('Z') || raceStart.ts.includes('+') ? raceStart.ts : raceStart.ts + 'Z');
    let nearestIdx = 0;
    let minDelta = Infinity;
    for (let i = 0; i < _trackData.timestamps.length; i++) {
      const d = Math.abs(_trackData.timestamps[i] - startUtc);
      if (d < minDelta) { minDelta = d; nearestIdx = i; }
    }
    const latLng = _trackData.latLngs[nearestIdx];
    const startTip = 'Vakaros race start \u00b7 ' + startUtc.toISOString();
    L.circleMarker(latLng, {
      radius: 9, color: startColor, fillColor: startColor, fillOpacity: 1, weight: 2,
    }).addTo(_map).bindTooltip(startTip).bindPopup(startTip);

    // Stash a synthetic "start" row so the maneuvers panel can show it.
    // loadManeuvers() reads this and merges it after fetching the API.
    const ctx = data.race_start_context || {};
    _vakarosSyntheticStart = {
      id: 'vakaros-race-start',
      type: 'start',
      ts: startUtc.toISOString(),
      source: 'vakaros',
      // Surface BSP in the "BSP in→out" column — start has no exit, so the
      // entry_bsp alone is shown.
      entry_bsp: ctx.bsp_kts != null ? ctx.bsp_kts : null,
      // Extras for the detail panel below the table.
      start_sog_kts: ctx.sog_kts != null ? ctx.sog_kts : null,
      start_distance_to_line_m: ctx.distance_to_line_m != null ? ctx.distance_to_line_m : null,
      start_lat: ctx.latitude_deg != null ? ctx.latitude_deg : null,
      start_lon: ctx.longitude_deg != null ? ctx.longitude_deg : null,
      start_tws_kts: ctx.tws_kts != null ? ctx.tws_kts : null,
      start_twd_deg: ctx.twd_deg != null ? ctx.twd_deg : null,
      start_twa_deg: ctx.twa_deg != null ? ctx.twa_deg : null,
      start_polar_pct: ctx.polar_pct != null ? ctx.polar_pct : null,
      start_line_bias_deg: ctx.line_bias_deg != null ? ctx.line_bias_deg : null,
      start_favored_end: ctx.favored_end || null,
    };
    _injectVakarosStartIntoManeuvers();
  }
}

// ---------------------------------------------------------------------------
// HelmLog start-line overlay (#644 + bias overlay)
// ---------------------------------------------------------------------------
//
// Renders boat/pin markers, a dashed orange line, and dynamic upwind ticks
// at each end that follow the replay scrubber. The favoured end is
// highlighted with a halo. All of it is computed from /race-start-overlay
// (the line) + _replaySamples (TWD-at-cursor); no extra fetch on scrub.

let _hlStartLineData = null;  // {boat: [lat,lon], pin: [...], length_m, bearing_deg}
let _hlBoatHalo = null;
let _hlPinHalo = null;
let _hlPinTick = null;
let _hlBoatTick = null;
let _hlBiasLabel = null;

function _angleDiffDeg(a, b) {
  const d = ((a - b + 540) % 360) - 180;
  return d;
}

function _findReplaySampleAt(utcMs) {
  if (!_replaySamples || !_replaySamples.length) return null;
  // Binary search for the latest sample with ts <= utcMs.
  let lo = 0;
  let hi = _replaySamples.length - 1;
  if (_replaySamples[0].ts.getTime() > utcMs) return null;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (_replaySamples[mid].ts.getTime() <= utcMs) lo = mid;
    else hi = mid - 1;
  }
  return _replaySamples[lo];
}

async function loadHelmlogStartLineOverlay() {
  if (!_map) return;
  const r = await fetch('/api/sessions/' + SESSION_ID + '/race-start-overlay');
  if (!r.ok) return;
  const data = await r.json();
  if (!data || !data.line) return;

  _hlStartLineData = data.line;
  const HELMLOG_COLOR = '#f59e0b';

  // Static endpoint markers + line (always on, regardless of scrubber).
  const boat = data.line.boat;
  const pin = data.line.pin;
  const boatTip = data.line.boat_end_carried_over_from_race_id
    ? 'HelmLog boat-end (from race ' + data.line.boat_end_carried_over_from_race_id + ')'
    : 'HelmLog boat-end ping';
  const pinTip = data.line.pin_end_carried_over_from_race_id
    ? 'HelmLog pin-end (from race ' + data.line.pin_end_carried_over_from_race_id + ')'
    : 'HelmLog pin-end ping';
  L.polyline([pin, boat], {
    color: HELMLOG_COLOR, weight: 3, opacity: 0.9, dashArray: '8, 8',
  }).addTo(_map).bindTooltip('HelmLog start line', { sticky: true });
  L.circleMarker(boat, {
    radius: 7, color: HELMLOG_COLOR, fillColor: HELMLOG_COLOR,
    fillOpacity: 0.9, weight: 2,
  }).addTo(_map).bindTooltip(boatTip);
  L.circleMarker(pin, {
    radius: 7, color: HELMLOG_COLOR, fillColor: '#fbbf24',
    fillOpacity: 0.9, weight: 2,
  }).addTo(_map).bindTooltip(pinTip);

  // Halos behind each marker — sized/colored by which end is favoured
  // at the current scrub time. Drawn beneath the static markers (added
  // first so they paint underneath).
  _hlBoatHalo = L.circleMarker(boat, {
    radius: 0, color: HELMLOG_COLOR, fillColor: HELMLOG_COLOR,
    fillOpacity: 0.25, weight: 0, interactive: false,
  }).addTo(_map);
  _hlPinHalo = L.circleMarker(pin, {
    radius: 0, color: HELMLOG_COLOR, fillColor: '#fbbf24',
    fillOpacity: 0.25, weight: 0, interactive: false,
  }).addTo(_map);

  // Wind ticks pointing upwind from each end — redrawn every clock tick.
  _hlBoatTick = L.polyline([boat, boat], {
    color: HELMLOG_COLOR, weight: 2, opacity: 0.85, interactive: false,
  }).addTo(_map);
  _hlPinTick = L.polyline([pin, pin], {
    color: HELMLOG_COLOR, weight: 2, opacity: 0.85, interactive: false,
  }).addTo(_map);

  // Bias label panel below the map (re-uses the existing track-hint area
  // styling; we add our own tiny block right under it).
  _ensureHlBiasLabel();

  // Register as a play-clock surface so it updates on every tick/seek.
  registerSurface('hl-start-bias', function(utc) { _renderHlStartBias(utc); });
  // Initial render at the current cursor (may be _replayStart on first load).
  if (_playClock && _playClock.positionUtc) {
    _renderHlStartBias(_playClock.positionUtc);
  }
}

function _ensureHlBiasLabel() {
  if (_hlBiasLabel) return;
  const container = document.getElementById('track-container');
  if (!container) return;
  _hlBiasLabel = document.createElement('div');
  _hlBiasLabel.id = 'hl-bias-label';
  _hlBiasLabel.style.cssText =
    'font-size:.78rem;color:var(--text-secondary);padding:6px 8px;'
    + 'background:var(--bg-input);border:1px solid var(--border);'
    + 'border-radius:4px;margin-top:6px;font-variant-numeric:tabular-nums';
  _hlBiasLabel.innerHTML = 'Line bias: <span id="hl-bias-text">—</span>';
  container.appendChild(_hlBiasLabel);
}

function _renderHlStartBias(utc) {
  if (!_hlStartLineData || !_hlBoatHalo || !_hlPinHalo) return;
  const utcMs = utc instanceof Date ? utc.getTime() : new Date(utc).getTime();
  const sample = _findReplaySampleAt(utcMs);
  const twd = sample && sample.twd != null ? sample.twd : null;
  const tws = sample && sample.tws != null ? sample.tws : null;
  const lineBearing = _hlStartLineData.bearing_deg;
  const boat = _hlStartLineData.boat;
  const pin = _hlStartLineData.pin;
  const lineLen = _hlStartLineData.length_m || 100;

  const labelEl = document.getElementById('hl-bias-text');

  if (twd == null) {
    if (labelEl) labelEl.textContent = '— (no wind data at this moment)';
    if (_hlBoatTick) _hlBoatTick.setLatLngs([boat, boat]);
    if (_hlPinTick) _hlPinTick.setLatLngs([pin, pin]);
    if (_hlBoatHalo) _hlBoatHalo.setRadius(0);
    if (_hlPinHalo) _hlPinHalo.setRadius(0);
    return;
  }

  // Bias = 90 − |angle_diff(line_bearing, twd)|. Positive = pin favoured.
  const delta = _angleDiffDeg(lineBearing, twd);
  const bias = 90 - Math.abs(delta);
  const favoured = Math.abs(bias) < 1 ? 'neutral' : (bias > 0 ? 'pin' : 'boat');

  // Wind ticks upwind from each end (toward TWD).
  const tickLen = Math.max(40, lineLen * 0.4);
  const boatUp = _offsetPoint(boat[0], boat[1], twd, tickLen);
  const pinUp = _offsetPoint(pin[0], pin[1], twd, tickLen);
  if (_hlBoatTick) _hlBoatTick.setLatLngs([boat, boatUp]);
  if (_hlPinTick) _hlPinTick.setLatLngs([pin, pinUp]);

  // Halo on the favoured end. Size scales with bias magnitude (1°→10px, 90°→25px).
  const haloRadius = Math.min(25, 10 + Math.abs(bias) / 6);
  if (favoured === 'boat') {
    _hlBoatHalo.setRadius(haloRadius);
    _hlPinHalo.setRadius(0);
  } else if (favoured === 'pin') {
    _hlPinHalo.setRadius(haloRadius);
    _hlBoatHalo.setRadius(0);
  } else {
    _hlBoatHalo.setRadius(0);
    _hlPinHalo.setRadius(0);
  }

  if (labelEl) {
    const sign = bias >= 0 ? '+' : '';
    const twsTxt = tws != null ? ' · ' + tws.toFixed(1) + ' kt' : '';
    labelEl.innerHTML =
      '<strong>' + sign + bias.toFixed(0) + '°</strong> '
      + '<span style="text-transform:uppercase;letter-spacing:.05em">'
      + favoured + '</span> favoured · TWD ' + Math.round(twd) + '°' + twsTxt;
  }
}

function _injectVakarosStartIntoManeuvers() {
  if (!_vakarosSyntheticStart) return;
  const existing = _maneuvers.find(m => m.id === 'vakaros-race-start');
  if (existing) return;
  _maneuvers.push(_vakarosSyntheticStart);
  _maneuverSelected.add('vakaros-race-start');
  // Re-sort so the start lands in chronological order.
  if (typeof renderManeuverCard === 'function') renderManeuverCard();
}

function _nearestIndex(latlng) {
  if (!_trackData) return 0;
  let minDist = Infinity, nearIdx = 0;
  for (let i = 0; i < _trackData.latLngs.length; i++) {
    const d = _map.latLngToLayerPoint(_trackData.latLngs[i])
      .distanceTo(_map.latLngToLayerPoint(latlng));
    if (d < minDist) { minDist = d; nearIdx = i; }
  }
  return nearIdx;
}

function _moveCursorToIndex(idx) {
  if (!_trackData) return;
  const ts = _trackData.timestamps[idx];
  if (ts) {
    _moveCursorToUtc(ts);
  } else {
    _trackData.cursor.setLatLng(_trackData.latLngs[idx]).addTo(_map);
  }
}

// Interpolated cursor: finds the two bracketing GPS samples for the
// requested UTC and lerps lat/lng between them, so the boat glides
// continuously along the polyline. Rotation is still circular-meaned
// across a ±5s window to damp HDG/COG noise.
function _moveCursorToUtc(utc) {
  if (!_trackData) return;
  const {latLngs, timestamps} = _trackData;
  if (!latLngs.length) return;
  const tMs = utc.getTime();
  // Outside the GPS track's time span there is no real boat position to show.
  // The replay scrubber extends ~20 min into the prestart (#712) so video /
  // audio / gauges can be scrubbed before the gun, but the GPS track almost
  // always begins at the gun (instruments come online just before racing).
  // Clamping to an endpoint would falsely park the boat on the green "Start"
  // dot for that whole pre-gun window — so hide the cursor instead.
  if (tMs < timestamps[0].getTime() || tMs > timestamps[timestamps.length - 1].getTime()) {
    if (_map && _map.hasLayer(_trackData.cursor)) _map.removeLayer(_trackData.cursor);
    return;
  }
  // Bracket the timestamps
  let hi = _trackLowerBound(tMs);
  if (hi <= 0) { hi = 1; }
  if (hi >= timestamps.length) { hi = timestamps.length - 1; }
  const lo = hi - 1;
  const t0 = timestamps[lo].getTime();
  const t1 = timestamps[hi].getTime();
  let frac = t1 > t0 ? (tMs - t0) / (t1 - t0) : 0;
  if (frac < 0) frac = 0; else if (frac > 1) frac = 1;
  const a = latLngs[lo];
  const b = latLngs[hi];
  const lat = a[0] + (b[0] - a[0]) * frac;
  const lng = a[1] + (b[1] - a[1]) * frac;
  const interp = [lat, lng];
  _trackData.cursor.setLatLng(interp).addTo(_map);

  const windowed = _windowedHeadingCog(tMs, 5000);
  const el = _trackData.cursor.getElement();
  if (el) el.innerHTML = _renderBoatCursorSvg(windowed.hdg, windowed.cog);
  if (_followBoat && _map) _maybeFollowPan(interp);
}

// Binary search for the first timestamp index where ts >= tMs. Mirrors
// the replay-sample helper but operates on the track timestamps array.
function _trackLowerBound(tMs) {
  const ts = _trackData && _trackData.timestamps;
  if (!ts || !ts.length) return 0;
  let lo = 0, hi = ts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid].getTime() < tMs) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

// Circular-mean smoothing of hdg/cog over a window around ts. Returns
// {hdg, cog} in degrees [0, 360). Null fields if not enough samples.
function _windowedHeadingCog(tMs, halfMs) {
  if (!_replaySamples || !_replaySamples.length) return {hdg: null, cog: null};
  const lo = tMs - halfMs;
  const hi = tMs + halfMs;
  let hdgX = 0, hdgY = 0, hdgN = 0;
  let cogX = 0, cogY = 0, cogN = 0;
  // _replaySamples is sorted by ts; linear scan around the cursor is
  // bounded by the window so this is still O(window) not O(n).
  const startIdx = _sampleLowerBound(lo);
  for (let i = startIdx; i < _replaySamples.length; i++) {
    const s = _replaySamples[i];
    const t = s.ts.getTime();
    if (t > hi) break;
    if (s.hdg != null && !isNaN(s.hdg)) {
      const r = (s.hdg * Math.PI) / 180;
      hdgX += Math.cos(r); hdgY += Math.sin(r); hdgN++;
    }
    if (s.cog != null && !isNaN(s.cog)) {
      const r = (s.cog * Math.PI) / 180;
      cogX += Math.cos(r); cogY += Math.sin(r); cogN++;
    }
  }
  const angle = (x, y, n) => {
    if (!n) return null;
    const a = (Math.atan2(y / n, x / n) * 180) / Math.PI;
    return (a + 360) % 360;
  };
  return {hdg: angle(hdgX, hdgY, hdgN), cog: angle(cogX, cogY, cogN)};
}

function _sampleLowerBound(tMs) {
  if (!_replaySamples || !_replaySamples.length) return 0;
  let lo = 0, hi = _replaySamples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (_replaySamples[mid].ts.getTime() < tMs) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

// Soft-viewport follow: only recenter when the boat is within 25% of the
// nearest map edge, otherwise leave the map alone. Prevents the constant
// 10 Hz panning that caused the page-wide jitter.
function _maybeFollowPan(latLng) {
  if (!_map) return;
  // latLngs are stored as [lat, lng] arrays; normalize.
  const lat = Array.isArray(latLng) ? latLng[0] : latLng.lat;
  const lng = Array.isArray(latLng) ? latLng[1] : latLng.lng;
  const bounds = _map.getBounds();
  const ne = bounds.getNorthEast();
  const sw = bounds.getSouthWest();
  const latSpan = ne.lat - sw.lat;
  const lngSpan = ne.lng - sw.lng;
  const margin = 0.25;
  const latMargin = latSpan * margin;
  const lngMargin = lngSpan * margin;
  const nearEdge =
    lat < sw.lat + latMargin ||
    lat > ne.lat - latMargin ||
    lng < sw.lng + lngMargin ||
    lng > ne.lng - lngMargin;
  if (!nearEdge) return;
  // Use Leaflet's panTo with a short animation so the recenter glides
  // rather than snapping; duration short enough not to overshoot tick.
  _map.panTo(latLng, {animate: true, duration: 0.4});
}

// -----------------------------------------------------------------------
// Current overlay (#523). Toggleable layer showing derived water current
// (set/drift) from the boat's own track. Phase 1 renders arrows along the
// sailed track and a live boat-adjacent indicator during replay. The
// extrapolated field beyond the sailed area is deferred to a follow-up.
// -----------------------------------------------------------------------

let _currentLayer = null;         // L.LayerGroup for along-track arrows
let _currentEnabled = false;
let _currentOverlayBuilt = false;
let _currentZoomHandler = null;

// Sample cadence for the current overlay as a function of map zoom. Matches
// _windStepMsForZoom so current arrows render at the same density as the
// wind barbs.
function _currentStepMsForZoom(zoom) {
  if (zoom == null) zoom = 14;
  const step = 20000 * Math.pow(2, Math.max(0, 15 - zoom));
  return Math.min(600000, Math.max(15000, step));
}

function _renderCurrentArrowSvg(setDeg, driftKts, color) {
  // Length in px scales with drift, clamped for readability. 1 kt -> 18 px,
  // capped at ~44 px so heavy current doesn't swamp the map.
  const len = Math.min(44, Math.max(10, 8 + driftKts * 18));
  const tail = Math.max(0, len - 6);
  const c = color || '#2563eb';
  // Compass rotation: set_deg is the direction the current flows *toward*
  // (0=N, 90=E). SVG x-axis points east so rotate by (setDeg - 90).
  const rot = setDeg - 90;
  let parts = '<svg width="64" height="64" viewBox="-32 -32 64 64" style="overflow:visible;pointer-events:auto">';
  // Invisible hit-target halo so hover/touch is easy along the shaft length.
  parts += '<circle cx="0" cy="0" r="' + (len / 2 + 6) + '" fill="#fff" fill-opacity="0.001"/>';
  parts += '<g transform="rotate(' + rot + ')">';
  parts += '<line x1="0" y1="0" x2="' + tail + '" y2="0" stroke="#000" stroke-opacity="0.4" stroke-width="2.5" stroke-linecap="round"/>';
  parts += '<line x1="0" y1="0" x2="' + tail + '" y2="0" stroke="' + c + '" stroke-width="1.4" stroke-linecap="round"/>';
  parts += '<polygon points="' + len + ',0 ' + tail + ',-2.5 ' + tail + ',2.5" fill="' + c + '" stroke="#000" stroke-opacity="0.4" stroke-width="0.4"/>';
  parts += '</g></svg>';
  return parts;
}

function _currentArrowDivIcon(setDeg, driftKts, color) {
  return L.divIcon({
    className: 'current-arrow',
    html: _renderCurrentArrowSvg(setDeg, driftKts, color),
    iconSize: [64, 64],
    iconAnchor: [32, 32],
  });
}

// Locate the track position for a given UTC by bracketing the track
// timestamps (same bracket-and-lerp approach as _moveCursorToUtc).
function _trackLatLngAtUtc(tMs) {
  if (!_trackData) return null;
  const {latLngs, timestamps} = _trackData;
  if (!latLngs.length) return null;
  let hi = _trackLowerBound(tMs);
  if (hi <= 0) hi = 1;
  if (hi >= timestamps.length) hi = timestamps.length - 1;
  const lo = hi - 1;
  const t0 = timestamps[lo].getTime();
  const t1 = timestamps[hi].getTime();
  let frac = t1 > t0 ? (tMs - t0) / (t1 - t0) : 0;
  if (frac < 0) frac = 0; else if (frac > 1) frac = 1;
  const a = latLngs[lo];
  const b = latLngs[hi];
  return [a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac];
}

// Circular-mean of set/drift across a ±halfMs window. Used both for
// along-track thinning (damp noise) and the live boat-adjacent indicator.
function _windowedSetDrift(tMs, halfMs) {
  if (!_replaySamples || !_replaySamples.length) return null;
  const lo = tMs - halfMs;
  const hi = tMs + halfMs;
  let x = 0, y = 0, n = 0;
  const startIdx = _sampleLowerBound(lo);
  for (let i = startIdx; i < _replaySamples.length; i++) {
    const s = _replaySamples[i];
    const t = s.ts.getTime();
    if (t > hi) break;
    if (s.set == null || s.drift == null) continue;
    if (isNaN(s.set) || isNaN(s.drift)) continue;
    // Vector mean: average the (set, drift) vectors in N/E components.
    const r = (s.set * Math.PI) / 180;
    x += s.drift * Math.cos(r);
    y += s.drift * Math.sin(r);
    n++;
  }
  if (!n) return null;
  x /= n; y /= n;
  const drift = Math.hypot(x, y);
  if (drift < 1e-6) return {set: 0, drift: 0};
  const setDeg = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  return {set: setDeg, drift: drift};
}

function _rebuildCurrentOverlay() {
  if (!_map || !_trackData || !_replaySamples || !_replaySamples.length) return;
  if (_currentLayer) { _map.removeLayer(_currentLayer); _currentLayer = null; }
  _currentLayer = L.layerGroup();

  // Cadence scales with zoom (mirrors the wind overlay) so zooming out thins
  // arrows instead of cluttering the map. Half-window scales with step so
  // averaging matches the displayed cadence, clamped to keep 1 Hz noise damped.
  const stepMs = _currentStepMsForZoom(_map.getZoom());
  const halfMs = Math.max(10000, stepMs / 2);
  const t0 = _trackData.timestamps[0].getTime();
  const tEnd = _trackData.timestamps[_trackData.timestamps.length - 1].getTime();
  for (let t = t0; t <= tEnd; t += stepMs) {
    const sd = _windowedSetDrift(t, halfMs);
    if (!sd || sd.drift < 0.08) continue; // noise floor
    const pos = _trackLatLngAtUtc(t);
    if (!pos) continue;
    const marker = L.marker(pos, {
      icon: _currentArrowDivIcon(sd.set, sd.drift, '#2563eb'),
      interactive: true,
      keyboard: false,
      riseOnHover: true,
    });
    const tip = 'Set ' + Math.round(sd.set) + '\u00b0 \u00b7 Drift ' + sd.drift.toFixed(2) + ' kt';
    marker.bindTooltip(tip, {direction: 'top', offset: [0, -6], sticky: true});
    _currentLayer.addLayer(marker);
  }

  if (_currentEnabled) _currentLayer.addTo(_map);
  _currentOverlayBuilt = true;
}

function _setCurrentOverlayEnabled(on) {
  _currentEnabled = !!on;
  if (_currentEnabled) {
    if (!_currentOverlayBuilt) _rebuildCurrentOverlay();
    if (_currentLayer && _map) _currentLayer.addTo(_map);
    if (_map && !_currentZoomHandler) {
      _currentZoomHandler = () => { if (_currentEnabled) _rebuildCurrentOverlay(); };
      _map.on('zoomend', _currentZoomHandler);
    }
  } else {
    if (_currentLayer && _map) _map.removeLayer(_currentLayer);
    if (_map && _currentZoomHandler) {
      _map.off('zoomend', _currentZoomHandler);
      _currentZoomHandler = null;
    }
  }
}

// -----------------------------------------------------------------------
// Boat-centered instrument cluster. A compass-rose dial drawn around the
// boat cursor showing live wind (TWD/TWS) and/or current (set/drift). The
// wind and current displays are toggled independently; if either is on the
// dial frame is rendered, otherwise the marker is removed entirely.
// -----------------------------------------------------------------------

let _boatInstrumentMarker = null;
const _boatInstrument = {wind: false, current: false};


function _renderBoatInstrumentSvg(opts) {
  const hdg = opts.hdg, twd = opts.twd, tws = opts.tws;
  const set = opts.set, drift = opts.drift;
  const showWind = opts.showWind, showCurrent = opts.showCurrent;
  const R = 84;          // outer ring radius
  const RING_W = 14;     // tick band width
  const inner = R - RING_W - 6;
  let s = '<svg width="220" height="220" viewBox="-110 -110 220 220" style="overflow:visible;pointer-events:none">';
  // Compass ring background
  s += '<circle cx="0" cy="0" r="' + R + '" fill="rgba(15,23,42,0.55)" stroke="#0f172a" stroke-width="2"/>';
  s += '<circle cx="0" cy="0" r="' + (R - RING_W) + '" fill="rgba(255,255,255,0.05)" stroke="#1f2937" stroke-width="1"/>';
  // Tick marks every 10°, labels every 30°
  for (let deg = 0; deg < 360; deg += 10) {
    const major = (deg % 30) === 0;
    const len = major ? 8 : 4;
    const r1 = R - 2;
    const r2 = R - 2 - len;
    const a = (deg - 90) * Math.PI / 180;
    const x1 = r1 * Math.cos(a), y1 = r1 * Math.sin(a);
    const x2 = r2 * Math.cos(a), y2 = r2 * Math.sin(a);
    s += '<line x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1) + '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1) + '" stroke="#e5e7eb" stroke-width="' + (major ? 1.5 : 0.8) + '"/>';
    if (major) {
      const lr = R - 2 - len - 6;
      const lx = lr * Math.cos(a), ly = lr * Math.sin(a);
      const lbl = deg === 0 ? 'N' : deg === 90 ? 'E' : deg === 180 ? 'S' : deg === 270 ? 'W' : String(deg).padStart(3, '0');
      s += '<text x="' + lx.toFixed(1) + '" y="' + (ly + 3).toFixed(1) + '" text-anchor="middle" font-size="9" fill="#e5e7eb" font-family="sans-serif">' + lbl + '</text>';
    }
  }
  // Wind arrow (orange). TWD points the direction the wind is *coming from*,
  // so the shaft sits on the upwind side of the dial pointing inward.
  if (showWind && twd != null && tws != null) {
    s += '<g transform="rotate(' + twd + ')">';
    s += '<line x1="0" y1="-8" x2="0" y2="' + (-inner) + '" stroke="#000" stroke-opacity="0.55" stroke-width="6" stroke-linecap="round"/>';
    s += '<line x1="0" y1="-8" x2="0" y2="' + (-inner) + '" stroke="#f59e0b" stroke-width="3.5" stroke-linecap="round"/>';
    s += '<polygon points="0,-' + (inner + 6) + ' -6,' + (-inner + 4) + ' 6,' + (-inner + 4) + '" fill="#f59e0b" stroke="#000" stroke-opacity="0.55" stroke-width="0.6"/>';
    s += '</g>';
    // TWS callout sits just past the arrow tip (along TWD), upright in the
    // un-rotated frame. Place via bearing-to-XY so it follows the wind.
    const twdRad = twd * Math.PI / 180;
    const twsX = Math.sin(twdRad) * (inner + 16);
    const twsY = -Math.cos(twdRad) * (inner + 16);
    s += '<text x="' + twsX.toFixed(1) + '" y="' + (twsY + 4).toFixed(1) + '" text-anchor="middle" font-size="12" font-weight="700" fill="#f59e0b" stroke="#0f172a" stroke-width="2.5" paint-order="stroke" font-family="sans-serif">' + tws.toFixed(1) + '</text>';
  }
  // Boat hull at center, oriented to HDG.
  if (hdg != null) {
    s += '<g transform="rotate(' + hdg + ')">';
    s += '<polygon points="0,-15 9,13 -9,13" fill="#facc15" stroke="#1f2937" stroke-width="1.4" stroke-linejoin="round"/>';
    s += '</g>';
  } else {
    s += '<circle cx="0" cy="0" r="3" fill="#facc15" stroke="#1f2937" stroke-width="1"/>';
  }
  // Current arrow (blue). Set is the direction current flows *toward*, so the
  // arrow points outward from the boat in that direction. Length scales with
  // drift, capped to fit inside the ring.
  if (showCurrent && set != null && drift != null && drift >= 0.05) {
    const len = Math.min(inner - 4, Math.max(18, 18 + drift * 22));
    const tail = Math.max(0, len - 8);
    s += '<g transform="rotate(' + set + ')">';
    s += '<line x1="0" y1="-2" x2="0" y2="' + (-tail) + '" stroke="#000" stroke-opacity="0.55" stroke-width="6" stroke-linecap="round"/>';
    s += '<line x1="0" y1="-2" x2="0" y2="' + (-tail) + '" stroke="#2563eb" stroke-width="3.5" stroke-linecap="round"/>';
    s += '<polygon points="0,' + (-len) + ' -7,' + (-tail) + ' 7,' + (-tail) + '" fill="#2563eb" stroke="#000" stroke-opacity="0.55" stroke-width="0.6"/>';
    s += '</g>';
    // Drift callout sits just past the arrowhead so it never overlaps the
    // boat hull (which extends ~15 px from center in any direction).
    const setRad = set * Math.PI / 180;
    const labelR = len + 10;
    const driftX = Math.sin(setRad) * labelR;
    const driftY = -Math.cos(setRad) * labelR;
    s += '<text x="' + driftX.toFixed(1) + '" y="' + (driftY + 4).toFixed(1) + '" text-anchor="middle" font-size="13" font-weight="700" fill="#fff" stroke="#0f172a" stroke-width="3" paint-order="stroke" font-family="sans-serif">' + drift.toFixed(1) + '</text>';
  }
  // HDG readout sits past the arrow tips in front of the bow, drawn last so
  // it stays on top of the wind/current arrows when they happen to point the
  // same way the boat is heading.
  if (hdg != null) {
    const hdgStr = String(Math.round(hdg) % 360).padStart(3, '0');
    const hdgRad = hdg * Math.PI / 180;
    const labelR = inner + 8;
    const bowX = Math.sin(hdgRad) * labelR;
    const bowY = -Math.cos(hdgRad) * labelR;
    s += '<g transform="translate(' + bowX.toFixed(1) + ',' + bowY.toFixed(1) + ')">';
    s += '<rect x="-17" y="-9" width="34" height="14" rx="2" fill="#0f172a" stroke="#e5e7eb" stroke-width="1"/>';
    s += '<text x="0" y="2" text-anchor="middle" font-size="11" fill="#e5e7eb" font-family="sans-serif">' + hdgStr + '</text>';
    s += '</g>';
  }
  s += '</svg>';
  return s;
}

function _ensureBoatInstrumentMarker() {
  if (_boatInstrumentMarker || !_map) return;
  const icon = L.divIcon({
    className: 'boat-instrument',
    html: '',
    iconSize: [220, 220],
    iconAnchor: [110, 110],
  });
  _boatInstrumentMarker = L.marker([0, 0], {icon: icon, interactive: false, keyboard: false});
  // Keep the dial below the boat cursor and other interactive markers.
  _boatInstrumentMarker.setZIndexOffset(-500);
}

function _updateBoatInstrument(utc) {
  if (!_boatInstrument.wind && !_boatInstrument.current) return;
  if (!_map || !_trackData || !_replaySamples || !_replaySamples.length) return;
  const tMs = utc.getTime();
  const pos = _trackLatLngAtUtc(tMs);
  if (!pos) return;
  _ensureBoatInstrumentMarker();
  _boatInstrumentMarker.setLatLng(pos);
  if (!_map.hasLayer(_boatInstrumentMarker)) _boatInstrumentMarker.addTo(_map);
  const sd = _boatInstrument.current ? _windowedSetDrift(tMs, 15000) : null;
  const w = _boatInstrument.wind ? _windowedTwdTws(tMs, 10000) : null;
  const hc = _windowedHeadingCog(tMs, 5000);
  const el = _boatInstrumentMarker.getElement();
  if (el) {
    el.innerHTML = _renderBoatInstrumentSvg({
      hdg: hc ? hc.hdg : null,
      twd: w ? w.twd : null,
      tws: w ? w.tws : null,
      set: sd ? sd.set : null,
      drift: sd ? sd.drift : null,
      showWind: _boatInstrument.wind,
      showCurrent: _boatInstrument.current,
    });
  }
}

function _setBoatInstrument(kind, on) {
  _boatInstrument[kind] = !!on;
  if (!_boatInstrument.wind && !_boatInstrument.current) {
    if (_boatInstrumentMarker && _map && _map.hasLayer(_boatInstrumentMarker)) {
      _map.removeLayer(_boatInstrumentMarker);
    }
    return;
  }
  if (_playClock && _playClock.positionUtc) _updateBoatInstrument(_playClock.positionUtc);
}

// -----------------------------------------------------------------------
// Wind overlay (#554). Toggleable layer showing the TWD/TWS the boat
// actually experienced, sampled along the track as meteorological wind
// barbs. The shaft points in the direction the wind is coming *from*
// (standard convention), with flags encoding TWS: pennant = 50 kt,
// full barb = 10 kt, half barb = 5 kt. Sample cadence scales with map
// zoom so zooming out doesn't swamp the map. Samples with twd == null
// (e.g. ref=BOAT without heading) are skipped.
// -----------------------------------------------------------------------

let _windLayer = null;
let _windEnabled = false;
let _windZoomHandler = null;

// Render a meteorological wind barb rotated so the shaft points *from*
// the wind source (twd). Flags accumulate from the outer end of the
// shaft inward in 50/10/5 kt increments.
function _renderWindBarbSvg(twdDeg, tws, color) {
  const c = color || '#1f2937';
  const shaftLen = 32;
  // Round to nearest 5 kt for barb counts (standard met convention).
  let knots = Math.round(tws / 5) * 5;
  const pennants = Math.floor(knots / 50); knots -= pennants * 50;
  const fulls = Math.floor(knots / 10); knots -= fulls * 10;
  const halves = Math.floor(knots / 5);
  // Compass rotation: shaft points *toward* the direction the wind is
  // coming from. SVG y-axis points down and 0° rotation leaves -y as
  // "up" (north), which is exactly what we want for TWD=0.
  const rot = twdDeg;
  let parts = '<svg width="64" height="64" viewBox="-32 -32 64 64" style="overflow:visible;pointer-events:auto">';
  // Hit-target halo so hover/touch is easy even on a thin shaft.
  parts += '<circle cx="0" cy="0" r="6" fill="#fff" fill-opacity="0.001"/>';
  parts += '<g transform="rotate(' + rot + ')">';
  // Station dot
  parts += '<circle cx="0" cy="0" r="2" fill="' + c + '"/>';
  // Calm (<3 kt): open circle, no shaft.
  if (tws < 3) {
    parts += '<circle cx="0" cy="0" r="4" fill="none" stroke="' + c + '" stroke-width="1.2"/>';
    parts += '</g></svg>';
    return parts;
  }
  // Shaft (halo + stroke for contrast against map tiles)
  parts += '<line x1="0" y1="0" x2="0" y2="-' + shaftLen + '" stroke="#fff" stroke-opacity="0.85" stroke-width="3.2" stroke-linecap="round"/>';
  parts += '<line x1="0" y1="0" x2="0" y2="-' + shaftLen + '" stroke="' + c + '" stroke-width="1.6" stroke-linecap="round"/>';
  // Barbs hang to the right of the shaft (NH convention). Start at the
  // tip and walk inward. Spacing: 5 px per feature.
  let y = -shaftLen;
  const step = 5;
  const fullW = 10;
  const halfW = 5;
  for (let i = 0; i < pennants; i++) {
    const y2 = y + step;
    parts += '<polygon points="0,' + y + ' ' + fullW + ',' + y + ' 0,' + y2 + '" fill="' + c + '" stroke="' + c + '" stroke-width="0.8" stroke-linejoin="round"/>';
    y = y2;
  }
  if (pennants > 0) y += 1;
  for (let i = 0; i < fulls; i++) {
    parts += '<line x1="0" y1="' + y + '" x2="' + fullW + '" y2="' + (y - 4) + '" stroke="' + c + '" stroke-width="1.6" stroke-linecap="round"/>';
    y += step;
  }
  // A lone half-barb sits one step in from the tip so it's not flush
  // with the end of the shaft.
  if (halves > 0 && fulls === 0 && pennants === 0) y += step;
  for (let i = 0; i < halves; i++) {
    parts += '<line x1="0" y1="' + y + '" x2="' + halfW + '" y2="' + (y - 2) + '" stroke="' + c + '" stroke-width="1.6" stroke-linecap="round"/>';
    y += step;
  }
  parts += '</g></svg>';
  return parts;
}

function _windBarbDivIcon(twdDeg, tws, color) {
  return L.divIcon({
    className: 'wind-barb',
    html: _renderWindBarbSvg(twdDeg, tws, color),
    iconSize: [64, 64],
    iconAnchor: [32, 32],
  });
}

// Sample cadence for the barb overlay as a function of map zoom. Higher
// zoom → more detail; zooming out thins out to keep barbs from colliding.
function _windStepMsForZoom(zoom) {
  if (zoom == null) zoom = 14;
  // At zoom 15 → 20s, 14 → 40s, 13 → 80s, 12 → 160s, ... Clamped so a
  // very long session at low zoom still shows some barbs.
  const step = 20000 * Math.pow(2, Math.max(0, 15 - zoom));
  return Math.min(600000, Math.max(15000, step));
}

// Circular mean of twd and scalar mean of tws across a ±halfMs window.
// Returns null if no valid samples (all twd null or out of range).
function _windowedTwdTws(tMs, halfMs) {
  if (!_replaySamples || !_replaySamples.length) return null;
  const lo = tMs - halfMs;
  const hi = tMs + halfMs;
  let x = 0, y = 0, twsSum = 0, n = 0;
  const startIdx = _sampleLowerBound(lo);
  for (let i = startIdx; i < _replaySamples.length; i++) {
    const s = _replaySamples[i];
    const t = s.ts.getTime();
    if (t > hi) break;
    if (s.twd == null || s.tws == null) continue;
    if (isNaN(s.twd) || isNaN(s.tws)) continue;
    const r = (s.twd * Math.PI) / 180;
    x += Math.cos(r);
    y += Math.sin(r);
    twsSum += s.tws;
    n++;
  }
  if (!n) return null;
  const twd = ((Math.atan2(y / n, x / n) * 180) / Math.PI + 360) % 360;
  return {twd: twd, tws: twsSum / n};
}

function _rebuildWindOverlay() {
  if (!_map || !_trackData || !_replaySamples || !_replaySamples.length) return;
  if (_windLayer) { _map.removeLayer(_windLayer); _windLayer = null; }
  _windLayer = L.layerGroup();

  const stepMs = _windStepMsForZoom(_map.getZoom());
  // Half-window scales with step so averaging matches the displayed cadence,
  // but is clamped so we still damp 1 Hz noise at the densest zoom.
  const halfMs = Math.max(10000, stepMs / 2);
  const t0 = _trackData.timestamps[0].getTime();
  const tEnd = _trackData.timestamps[_trackData.timestamps.length - 1].getTime();
  for (let t = t0; t <= tEnd; t += stepMs) {
    const w = _windowedTwdTws(t, halfMs);
    if (!w) continue;
    const pos = _trackLatLngAtUtc(t);
    if (!pos) continue;
    const marker = L.marker(pos, {
      icon: _windBarbDivIcon(w.twd, w.tws, _twsColor(w.tws)),
      interactive: true,
      keyboard: false,
      riseOnHover: true,
    });
    const tip = 'TWD ' + Math.round(w.twd) + '\u00b0 \u00b7 TWS ' + w.tws.toFixed(1) + ' kt';
    marker.bindTooltip(tip, {direction: 'top', offset: [0, -6], sticky: true});
    _windLayer.addLayer(marker);
  }

  if (_windEnabled) _windLayer.addTo(_map);
}

function _setWindOverlayEnabled(on) {
  _windEnabled = !!on;
  if (_windEnabled) {
    _rebuildWindOverlay();
    if (_windLayer && _map) _windLayer.addTo(_map);
    if (_map && !_windZoomHandler) {
      _windZoomHandler = () => { if (_windEnabled) _rebuildWindOverlay(); };
      _map.on('zoomend', _windZoomHandler);
    }
  } else {
    if (_windLayer && _map) _map.removeLayer(_windLayer);
    if (_map && _windZoomHandler) {
      _map.off('zoomend', _windZoomHandler);
      _windZoomHandler = null;
    }
  }
}

// Render the boat cursor SVG. The hull is a simplified triangle that
// rotates with heading so the bow points the way the boat is pointing.
// A second line extending from the center shows COG, so the offset
// between heading and COG (leeway, current) is visible at a glance.
function _renderBoatCursorSvg(hdg, cog) {
  const hasHdg = hdg != null && !isNaN(hdg);
  const hasCog = cog != null && !isNaN(cog);
  const hdgDeg = hasHdg ? hdg : 0;
  // Larger viewBox so the indicator lines can extend well beyond the hull
  // and read clearly against the track without being clipped.
  let parts = '<svg width="64" height="64" viewBox="-32 -32 64 64" style="overflow:visible;pointer-events:none">';
  // COG line (red) — drawn first so it sits under the hull. Doubled up
  // with a dark halo stroke so it pops against any map background.
  if (hasCog) {
    parts += '<g transform="rotate(' + cog + ')">';
    parts += '<line x1="0" y1="0" x2="0" y2="-30" stroke="#000" stroke-opacity="0.55" stroke-width="6" stroke-linecap="round"/>';
    parts += '<line x1="0" y1="0" x2="0" y2="-30" stroke="#ef4444" stroke-width="3.5" stroke-linecap="round"/>';
    parts += '</g>';
  }
  // Heading line (yellow) — same halo treatment.
  if (hasHdg) {
    parts += '<g transform="rotate(' + hdgDeg + ')">';
    parts += '<line x1="0" y1="0" x2="0" y2="-26" stroke="#000" stroke-opacity="0.55" stroke-width="6" stroke-linecap="round"/>';
    parts += '<line x1="0" y1="0" x2="0" y2="-26" stroke="#facc15" stroke-width="3.5" stroke-linecap="round"/>';
    parts += '</g>';
  }
  // Hull — thin triangle, bow at top, stern at bottom, rotated to heading
  parts += '<polygon points="0,-11 6,8 -6,8" fill="#facc15" stroke="#1f2937" stroke-width="1.25" stroke-linejoin="round" transform="rotate(' + hdgDeg + ')"/>';
  parts += '</svg>';
  return parts;
}

function _indexForUtc(utcDate) {
  if (!_trackData || !_trackData.timestamps.length) return 0;
  const t = utcDate.getTime();
  // Binary-ish search for nearest timestamp
  let best = 0, bestDiff = Math.abs(_trackData.timestamps[0].getTime() - t);
  for (let i = 1; i < _trackData.timestamps.length; i++) {
    const diff = Math.abs(_trackData.timestamps[i].getTime() - t);
    if (diff < bestDiff) { bestDiff = diff; best = i; }
    if (_trackData.timestamps[i].getTime() > t) break; // timestamps are sorted
  }
  return best;
}

function _utcForIndex(idx) {
  if (!_trackData || !_trackData.timestamps.length) return null;
  return _trackData.timestamps[Math.min(idx, _trackData.timestamps.length - 1)];
}

// ---------------------------------------------------------------------------
// YouTube IFrame Player
// ---------------------------------------------------------------------------

async function loadVideoPlayer() {
  const vr = await fetch('/api/sessions/' + SESSION_ID + '/videos');
  const videos = await vr.json();
  if (!videos.length) return;

  // Use first video with a video_id
  const vid = videos.find(v => v.video_id) || videos[0];
  if (!vid || !vid.video_id) return;

  const isLocal = vid.youtube_url && vid.youtube_url.startsWith('/');

  _videoSync = {
    syncUtc: new Date(vid.sync_utc),
    syncOffsetS: vid.sync_offset_s || 0,
    durationS: vid.duration_s || 0,
    player: null,
    videoId: vid.video_id,
    allVideos: videos,
    activeIdx: videos.indexOf(vid),
    isLocal,
  };

  const container = document.getElementById('video-container');
  container.style.display = '';

  // Render video switcher if multiple videos
  if (videos.length > 1) {
    const switcher = document.getElementById('video-switcher');
    switcher.innerHTML = videos.map((v, i) => {
      const label = v.label || v.title || ('Video ' + (i + 1));
      const cls = i === _videoSync.activeIdx ? 'filter-btn active' : 'filter-btn';
      return '<button class="' + cls + '" onclick="switchVideo(' + i + ')">' + esc(label) + '</button>';
    }).join('');
  }

  if (isLocal) {
    _createLocalPlayer(vid.youtube_url);
  } else {
    // Load YouTube IFrame API
    const tag = document.createElement('script');
    tag.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(tag);
  }
}

// YouTube API calls this global function when ready
function onYouTubeIframeAPIReady() {
  _ytReady = true;
  if (_videoSync) _createPlayer(_videoSync.videoId);
}

function _createPlayer(videoId) {
  if (_videoSync.player) {
    _videoSync.player.loadVideoById(videoId);
    _ensureWatchOnYoutubeLink();
    return;
  }
  // Use the standard YT.Player div-based instantiation. The manual iframe
  // approach broke YouTube's embedder identity verification (Error 153).
  // 360 video panning is NOT supported in YouTube iframe embeds for
  // third-party domains regardless of `allow` attributes — it only works on
  // youtube.com itself and in the YouTube app. We expose a "Watch on YouTube"
  // link below the player as the workaround for spherical videos.
  _videoSync.player = new YT.Player('yt-player', {
    height: '100%',
    width: '100%',
    videoId: videoId,
    playerVars: {
      modestbranding: 1,
      rel: 0,
      enablejsapi: 1,
      origin: location.origin,
    },
    events: {
      onReady: _onVideoReady,
      onStateChange: _onPlayerStateChange,
    },
  });
  _ensureWatchOnYoutubeLink();
}

// Create an HTML5 <video> player for a locally-served file and install a
// YT-Player-compatible adapter on _videoSync.player so the rest of the
// session code (seekTo, getCurrentTime, pauseVideo, getPlayerState) works
// unchanged without knowing whether the source is YouTube or a local file.
function _createLocalPlayer(url) {
  const wrap = document.getElementById('yt-player');
  wrap.innerHTML = '';
  const el = document.createElement('video');
  el.src = url;
  el.controls = true;
  el.preload = 'metadata';
  el.style.cssText = 'width:100%;height:100%;display:block;background:#000';
  wrap.appendChild(el);

  const adapter = {
    getCurrentTime() { return el.currentTime; },
    seekTo(t) { el.currentTime = t; },
    pauseVideo() { el.pause(); },
    playVideo() { el.play().catch(() => {}); },
    getPlayerState() { return el.paused ? 2 : 1; },
    loadVideoById() { /* no-op: local single-file */ },
  };
  _videoSync.player = adapter;

  el.addEventListener('loadedmetadata', () => _onVideoReady());
  el.addEventListener('play', () => _onPlayerStateChange({data: 1}));
  el.addEventListener('pause', () => _onPlayerStateChange({data: 2}));
  el.addEventListener('ended', () => _onPlayerStateChange({data: 0}));
}

// Render the "Watch on YouTube" link once. The URL is computed live in the
// click handler so it always reflects the currently selected video and its
// current playhead — the old implementation baked a stale videoId + t=0 into
// the href at player-create time and never refreshed on switchVideo().
function _ensureWatchOnYoutubeLink() {
  if (document.getElementById('yt-watch-on-youtube')) return;
  const linkBar = document.createElement('div');
  linkBar.id = 'yt-watch-on-youtube';
  linkBar.style.cssText = 'margin-top:6px;text-align:right;font-size:.75rem';
  const container = document.getElementById('video-container');
  if (container) container.appendChild(linkBar);
  linkBar.innerHTML = '<a href="#" rel="noopener" style="color:var(--accent);text-decoration:none" title="Open in YouTube for 360° panning controls" onclick="return _openWatchOnYoutube(event)">Watch on YouTube &#8599;</a>';
}

function _openWatchOnYoutube(ev) {
  if (ev && ev.preventDefault) ev.preventDefault();
  if (!_videoSync || !_videoSync.videoId) return false;
  let t = 0;
  try {
    if (_videoSync.player && _videoSync.player.getCurrentTime) {
      t = Math.floor(_videoSync.player.getCurrentTime() || 0);
    }
  } catch (e) { /* ignore */ }
  const url = 'https://www.youtube.com/watch?v=' + encodeURIComponent(_videoSync.videoId) + (t > 0 ? '&t=' + t + 's' : '');
  window.open(url, '_blank', 'noopener');
  return false;
}

// Watchdog for user scrubs on the YT player itself. The IFrame API doesn't
// emit a "seeked" event, so we poll getCurrentTime() while the clock is not
// in playing state and fan out any unexpected jump as a producer event.
// Disabled during clock-driven playback because fast speeds (2×/4×/8×) would
// otherwise read as "seeks" on every poll.
let _ytScrubPollTimer = null;
let _ytScrubLastOffset = null;
function _startYtScrubWatch() {
  if (_ytScrubPollTimer) return;
  _ytScrubPollTimer = setInterval(() => {
    if (!_videoSync || !_videoSync.player) return;
    if (_playClock.state === 'playing') return;
    if (_isEchoEvent()) return;
    let cur;
    try { cur = _videoSync.player.getCurrentTime(); } catch (e) { return; }
    if (cur == null || isNaN(cur)) return;
    if (_ytScrubLastOffset == null) { _ytScrubLastOffset = cur; return; }
    const delta = Math.abs(cur - _ytScrubLastOffset);
    _ytScrubLastOffset = cur;
    // Threshold > max natural drift between polls. YT paused shouldn't move
    // at all, so anything above 0.4s is a user-initiated scrub.
    if (delta < 0.4) return;
    const utc = _videoOffsetToUtc(cur);
    if (!utc) return;
    setPosition(utc, {source: 'video'});
  }, 250);
}

function _onVideoReady() {
  _startYtScrubWatch();
  // Video is a consumer: seek to the requested UTC if it's within range.
  // Large jumps (>2 s) pause the player so audio doesn't keep playing from
  // wherever the user just clicked. Small deltas (playback ticks) don't
  // touch playback state.
  registerSurface('video', function(utc) {
    // Playback independence: don't seek YT just because another player's
    // producer fanout reached us. Let WAV/mc play on their own.
    if (_playClock.currentSource === 'audio' || _playClock.currentSource === 'mc') return;
    if (!_videoSync || !_videoSync.player || !_videoSync.player.seekTo) return;
    const offset = _utcToVideoOffset(utc);
    if (offset === null || offset < 0) return;
    if (_videoSync.durationS && offset > _videoSync.durationS) return;
    let currentOffset = null;
    try {
      if (typeof _videoSync.player.getCurrentTime === 'function') {
        currentOffset = _videoSync.player.getCurrentTime();
      }
    } catch (e) { /* swallow */ }
    const delta = currentOffset != null ? Math.abs(currentOffset - offset) : Infinity;
    // While the clock tick is driving playback, the render callback fires at
    // ~10 Hz. Issuing seekTo() on every tick for small deltas (<0.5s) causes
    // the YouTube embed to stutter — the player is constantly re-buffering
    // instead of playing. Skip small corrections and let YT run naturally.
    if (delta < 0.5) return;
    if (delta > _LARGE_JUMP_SEC) {
      try {
        const state = typeof _videoSync.player.getPlayerState === 'function'
          ? _videoSync.player.getPlayerState() : -1;
        // YT.PlayerState.PLAYING = 1
        if (state === 1 && typeof _videoSync.player.pauseVideo === 'function') {
          _videoSync.player.pauseVideo();
        }
      } catch (e) { /* swallow */ }
    }
    _videoSync.player.seekTo(offset, true);
  });
  // If a deep-link was requested before the YT player was ready, re-apply
  // it now so the video actually seeks. The scrubber / track have already
  // been parked on the target by the earlier _applyDeepLink() call at the
  // tail of init().
  _applyDeepLink();
}

function switchVideo(idx) {
  const videos = _videoSync.allVideos;
  if (idx < 0 || idx >= videos.length) return;
  _videoSync.activeIdx = idx;
  const vid = videos[idx];
  _videoSync.syncUtc = new Date(vid.sync_utc);
  _videoSync.syncOffsetS = vid.sync_offset_s || 0;
  _videoSync.durationS = vid.duration_s || 0;
  _videoSync.videoId = vid.video_id;

  // Update switcher buttons
  document.querySelectorAll('#video-switcher .filter-btn').forEach((btn, i) => {
    btn.classList.toggle('active', i === idx);
  });

  if (_videoSync.player && _videoSync.player.loadVideoById) {
    _videoSync.player.loadVideoById(vid.video_id);
  }
}

function _onPlayerStateChange(event) {
  // YT.PlayerState.PLAYING = 1, PAUSED = 2, ENDED = 0, BUFFERING = 3
  if (event.data === 1) {
    // If we're inside the echo window from a recent programmatic seek,
    // clamp YT back to paused — a scrub just drove seekTo() and the embed
    // is trying to auto-resume out from under us. Outside the echo window
    // this is a user-initiated play and we leave it alone.
    if (_isEchoEvent()) {
      try {
        if (typeof _videoSync.player.pauseVideo === 'function') {
          _videoSync.player.pauseVideo();
        }
      } catch (e) { /* swallow */ }
      return;
    }
    _stopSyncTimer();
    // YT plays independently — drive a 2 Hz fanout so the map cursor, gauges,
    // and track scrubber follow along. Do NOT start the replay clock tick;
    // replay 'playing' state is reserved for the track replay play button.
    _syncTimer = setInterval(_videoTick, 500);
  } else {
    _stopSyncTimer();
    // Deliberately do NOT call _videoTick() here: every YT state change
    // (paused, buffering, cued) would otherwise overwrite _playClock.positionUtc
    // with YT's getCurrentTime(), which during a scrub can still be 0 or an
    // old offset and snaps the progress bar/cursor backward. _videoTick only
    // makes sense while YT is actually playing and driving the clock.
  }
}

function _stopSyncTimer() {
  if (_syncTimer) { clearInterval(_syncTimer); _syncTimer = null; }
}

function _videoTick() {
  if (!_videoSync || !_videoSync.player) return;
  if (typeof _videoSync.player.getCurrentTime !== 'function') return;
  // The replay clock is master when it's playing — let its tick drive the
  // surfaces instead of YT so the rates don't fight each other.
  if (_playClock.state === 'playing') return;
  const utc = _videoOffsetToUtc(_videoSync.player.getCurrentTime());
  if (!utc) return;
  // Fan out through setPosition so the track scrubber and gauges update too.
  // source='video' keeps the video consumer itself out of the loop (no echo).
  setPosition(utc, {source: 'video'});
}

// Convert video playback seconds → UTC
function _videoOffsetToUtc(videoSeconds) {
  if (!_videoSync) return null;
  // videoSeconds = syncOffsetS + (utc - syncUtc) in seconds
  // so utc = syncUtc + (videoSeconds - syncOffsetS) * 1000
  const ms = _videoSync.syncUtc.getTime() + (videoSeconds - _videoSync.syncOffsetS) * 1000;
  return new Date(ms);
}

// Convert UTC → video playback seconds
function _utcToVideoOffset(utcDate) {
  if (!_videoSync) return null;
  return _videoSync.syncOffsetS + (utcDate.getTime() - _videoSync.syncUtc.getTime()) / 1000;
}

// Seek video to the timestamp at track index
function _seekVideoToIndex(idx) {
  const utc = _utcForIndex(idx);
  if (!utc || !_videoSync || !_videoSync.player) return;
  const offset = _utcToVideoOffset(utc);
  if (offset === null || offset < 0) return;
  if (typeof _videoSync.player.seekTo === 'function') {
    _videoSync.player.seekTo(offset, true);
  }
}

// ---------------------------------------------------------------------------
// Section toggle
// ---------------------------------------------------------------------------

const _collapsed = {'boat-settings': true};
const _PERSISTED_SECTIONS = ['track-layers', 'replay-gauges'];
const _SECTION_KEY = 'helmlog.session.collapsed.';

function _applySectionState(name, collapsed) {
  const body = document.getElementById(name + '-body');
  const toggle = document.getElementById(name + '-toggle');
  if (!body) return;
  _collapsed[name] = collapsed;
  body.style.display = collapsed ? 'none' : '';
  if (toggle) toggle.innerHTML = collapsed ? '&#9654;' : '&#9660;';
}

function toggleSection(name) {
  const body = document.getElementById(name + '-body');
  if (!body) return;
  _applySectionState(name, !_collapsed[name]);
  if (_PERSISTED_SECTIONS.includes(name)) {
    try { localStorage.setItem(_SECTION_KEY + name, _collapsed[name] ? '1' : '0'); } catch (e) {}
  }
}

const _LAYER_TOGGLE_KEY = 'helmlog.session.layer.';
const _PERSISTED_LAYER_TOGGLES = [];

function _persistLayerToggle(id, apply) {
  const el = document.getElementById(id);
  if (!el) return;
  _PERSISTED_LAYER_TOGGLES.push({id, apply});
  let saved = null;
  try { saved = localStorage.getItem(_LAYER_TOGGLE_KEY + id); } catch (e) {}
  if (saved === '1' || saved === '0') {
    el.checked = saved === '1';
  }
  el.addEventListener('change', (e) => {
    const checked = !!e.target.checked;
    try { localStorage.setItem(_LAYER_TOGGLE_KEY + id, checked ? '1' : '0'); } catch (err) {}
    apply(checked);
  });
}

// After replay data is loaded, apply the saved state of every persisted layer
// toggle so overlays like Boat wind / Boat current render on session load
// instead of waiting for the user to toggle them on.
function _applyPersistedLayerToggles() {
  for (const {id, apply} of _PERSISTED_LAYER_TOGGLES) {
    const el = document.getElementById(id);
    if (!el) continue;
    try { apply(!!el.checked); } catch (e) {}
  }
}

function _restorePersistedSections() {
  for (const name of _PERSISTED_SECTIONS) {
    let saved = null;
    try { saved = localStorage.getItem(_SECTION_KEY + name); } catch (e) {}
    if (saved === '1') _applySectionState(name, true);
  }
}

// ---------------------------------------------------------------------------
// Videos (list/add/delete — below the player)
// ---------------------------------------------------------------------------

async function loadVideos() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/videos');
  const videos = await r.json();
  const card = document.getElementById('videos-card');
  const body = document.getElementById('videos-body');

  if (!videos.length && _session.type === 'debrief') return;
  card.style.display = '';

  if (videos.length) {
    body.innerHTML = videos.map(v => {
      const lbl = v.label ? '<b>' + esc(v.label) + '</b> — ' : '';
      const ttl = esc(v.title || v.youtube_url).substring(0, 60);
      const link = '<a href="' + esc(v.youtube_url) + '" target="_blank" style="color:var(--accent)">' + ttl + '</a>';
      const editSync = '<button onclick="toggleEditSync(' + v.id + ')" style="color:var(--accent);background:none;border:none;cursor:pointer;font-size:.78rem;margin-left:8px" title="Edit sync">&#8635; sync</button>';
      const del = '<button onclick="deleteVideo(' + v.id + ')" style="color:var(--danger);background:none;border:none;cursor:pointer;font-size:.8rem;margin-left:4px">&#10005;</button>';
      return '<div style="margin-bottom:4px">' + lbl + link + editSync + del + '</div>'
        + _videoEditSyncForm(v);
    }).join('');
  } else {
    body.innerHTML = '<span style="color:var(--text-secondary)">No videos linked</span>';
  }
  body.innerHTML += _videoAddForm();
}

// The track-scrubber readout (the "00:00 / 00:00" label next to the
// scrubber) measures elapsed from _replayStart, which is the prestart
// window cutoff (start_utc − 20 min) — not start_utc itself. Use that
// same anchor here so what the user types matches what they read off
// the page. Falls back to start_utc if the replay payload hasn't
// landed yet (cache miss / pre-load).
function _trackTimeAnchor() {
  if (typeof _replayStart !== 'undefined' && _replayStart) return _replayStart;
  const startUtc = _session && _session.start_utc;
  return startUtc ? new Date(startUtc) : null;
}

// Convert a track-scrubber time (mm:ss) and a video position (mm:ss)
// into the sync_utc + sync_offset_s pair the API expects.
function _resolveSyncTimes(videoPosStr, trackPosStr) {
  const videoPosS = parseVideoPosition(videoPosStr);
  if (videoPosS === null || videoPosS < 0) {
    alert('Video position must be mm:ss or seconds');
    return null;
  }
  const trackPosS = parseVideoPosition(trackPosStr);
  if (trackPosS === null || trackPosS < 0) {
    alert('Track position must be mm:ss or seconds (matches the scrubber readout)');
    return null;
  }
  const anchor = _trackTimeAnchor();
  if (!anchor) {
    alert('Session has no start time — cannot resolve track position');
    return null;
  }
  const syncUtc = new Date(anchor.getTime() + trackPosS * 1000).toISOString();
  return {syncUtc, syncOffsetS: videoPosS};
}

function _videoAddForm() {
  return '<div id="video-add-form" style="display:none;margin-top:8px">'
    + '<input id="video-url" class="field" placeholder="YouTube URL" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="video-label" class="field" placeholder="Label (e.g. Bow cam)" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<div style="font-size:.72rem;color:var(--text-secondary);margin-bottom:2px">Sync calibration: pick the same moment in the video and on the track scrubber.</div>'
    + '<div style="display:flex;gap:4px;margin-bottom:4px">'
    +   '<input id="video-sync-video-pos" class="field" placeholder="Video pos (mm:ss)" style="flex:1;padding:6px 8px;font-size:.82rem"/>'
    +   '<input id="video-sync-track-pos" class="field" placeholder="Track pos (mm:ss)" style="flex:1;padding:6px 8px;font-size:.82rem"/>'
    + '</div>'
    + '<button id="video-add-submit" class="btn-export" style="background:var(--accent-strong);color:var(--bg-primary);border-color:var(--accent-strong)" onclick="submitAddVideo()">Add Video</button>'
    + ' <button onclick="document.getElementById(\'video-add-form\').style.display=\'none\'" style="background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:.82rem">Cancel</button>'
    + '</div>'
    + '<button onclick="document.getElementById(\'video-add-form\').style.display=\'\'" style="font-size:.78rem;color:var(--accent);background:none;border:none;cursor:pointer;padding:4px 0;margin-top:4px">+ Add Video</button>';
}

// Convert a current video row's stored sync into the (videoPos, trackPos)
// pair the editor presents. trackPos is elapsed seconds from the same
// anchor the scrubber readout uses (_replayStart, falling back to
// start_utc); it can be negative if the video starts before the
// replay window, so keep the sign instead of clamping.
function _currentSyncDisplay(v) {
  const offsetS = Number(v.sync_offset_s || 0);
  let trackS = 0;
  const anchor = _trackTimeAnchor();
  if (anchor && v.sync_utc) {
    trackS = (new Date(v.sync_utc).getTime() - anchor.getTime()) / 1000;
  }
  const fmt = (s) => {
    const sign = s < 0 ? '-' : '';
    return sign + fmtDuration(Math.round(Math.abs(s)));
  };
  return {videoPos: fmt(offsetS), trackPos: fmt(trackS)};
}

function _videoEditSyncForm(v) {
  const cur = _currentSyncDisplay(v);
  return '<div id="video-edit-sync-' + v.id + '" style="display:none;margin:4px 0 10px 16px;padding:8px;background:var(--bg-secondary);border:1px solid var(--border);border-radius:4px">'
    + '<div style="font-size:.72rem;color:var(--text-secondary);margin-bottom:4px">Pick the same moment (e.g. the gun) in the video and on the track scrubber:</div>'
    + '<div style="display:flex;gap:4px;margin-bottom:4px">'
    +   '<input id="edit-sync-video-' + v.id + '" class="field" placeholder="Video pos (mm:ss)" value="' + esc(cur.videoPos) + '" style="flex:1;padding:6px 8px;font-size:.82rem"/>'
    +   '<input id="edit-sync-track-' + v.id + '" class="field" placeholder="Track pos (mm:ss)" value="' + esc(cur.trackPos) + '" style="flex:1;padding:6px 8px;font-size:.82rem"/>'
    + '</div>'
    + '<button class="btn-sm" style="background:var(--accent-strong);color:var(--bg-primary);border:none;border-radius:4px;padding:4px 10px;font-size:.78rem;cursor:pointer" onclick="submitEditSync(' + v.id + ')">Save sync</button>'
    + ' <button onclick="toggleEditSync(' + v.id + ')" style="background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:.78rem">Cancel</button>'
    + '</div>';
}

// _replayStart is loaded asynchronously via _loadReplayData and may not
// be set when loadVideos first renders the form HTML. Re-derive the
// prefill at toggle time so the user sees the values measured against
// the same anchor the scrubber uses by the time they're looking.
function toggleEditSync(videoId) {
  const el = document.getElementById('video-edit-sync-' + videoId);
  if (!el) return;
  if (el.style.display === 'none') {
    const v = _videoSync && _videoSync.allVideos
      ? _videoSync.allVideos.find(x => x.id === videoId)
      : null;
    if (v) {
      const cur = _currentSyncDisplay(v);
      const videoEl = document.getElementById('edit-sync-video-' + videoId);
      const trackEl = document.getElementById('edit-sync-track-' + videoId);
      if (videoEl) videoEl.value = cur.videoPos;
      if (trackEl) trackEl.value = cur.trackPos;
    }
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}

async function submitEditSync(videoId) {
  const videoPosVal = document.getElementById('edit-sync-video-' + videoId).value.trim();
  const trackPosVal = document.getElementById('edit-sync-track-' + videoId).value.trim();
  const sync = _resolveSyncTimes(videoPosVal, trackPosVal);
  if (!sync) return;
  const resp = await fetch('/api/videos/' + videoId, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sync_utc: sync.syncUtc, sync_offset_s: sync.syncOffsetS})
  });
  if (!resp.ok) { alert('Failed: ' + resp.status); return; }
  location.reload();
}

async function submitAddVideo() {
  const url = document.getElementById('video-url').value.trim();
  const label = document.getElementById('video-label').value.trim();
  const videoPosVal = document.getElementById('video-sync-video-pos').value.trim();
  const trackPosVal = document.getElementById('video-sync-track-pos').value.trim();
  if (!url) { alert('YouTube URL is required'); return; }
  let syncUtc;
  let syncOffsetS = 0;
  if (videoPosVal || trackPosVal) {
    const sync = _resolveSyncTimes(videoPosVal, trackPosVal);
    if (!sync) return;
    syncUtc = sync.syncUtc;
    syncOffsetS = sync.syncOffsetS;
  } else {
    // No calibration entered: anchor at session start, video offset 0.
    syncUtc = _session.start_utc || new Date().toISOString();
  }
  // Disable the button while the POST is in flight: yt-dlp metadata lookup
  // can take several seconds, and a second click would have created a
  // duplicate row (now also blocked at the DB layer, but the UX should
  // never let it get that far).
  const btn = document.getElementById('video-add-submit');
  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
  try {
    const resp = await fetch('/api/sessions/' + SESSION_ID + '/videos', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({youtube_url: url, label, sync_utc: syncUtc, sync_offset_s: syncOffsetS})
    });
    if (!resp.ok) {
      alert('Failed: ' + resp.status);
      if (btn) { btn.disabled = false; btn.textContent = 'Add Video'; }
      return;
    }
  } catch (e) {
    alert('Error adding video: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = 'Add Video'; }
    return;
  }
  // Reload everything to pick up new video in player
  location.reload();
}

async function deleteVideo(videoId) {
  if (!confirm('Remove this video link?')) return;
  await fetch('/api/videos/' + videoId, {method: 'DELETE'});
  location.reload();
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

let _pickerBoats = null;

async function loadResults() {
  const card = document.getElementById('results-card');
  card.style.display = '';
  const body = document.getElementById('results-body');
  const r = await fetch('/api/sessions/' + SESSION_ID + '/results');
  const results = await r.json();

  const imported = results.length > 0 && results[0].imported;
  let html = '<div id="results-list">';
  if (imported) {
    html += '<div style="display:flex;justify-content:space-between;align-items:center;font-size:.75rem;color:var(--text-secondary);margin-bottom:6px">'
      + '<span>Imported from race results</span>'
      + '<button class="btn-sm" style="font-size:.7rem;padding:2px 8px" onclick="unlinkImported()">Unlink</button>'
      + '</div>';
  } else {
    html += '<div id="link-imported-slot"></div>';
  }
  html += results.map(res => {
    const name = esc(res.sail_number + (res.boat_name ? ' \u2014 ' + res.boat_name : ''));
    if (imported) {
      const pts = res.points != null ? res.points : '';
      const status = res.status_code || '';
      const isSelf = res.sail_number === (document.body.dataset.sailNumber || '');
      const cls = isSelf ? ' style="background:var(--accent-strong);color:#fff;border-radius:4px;padding:2px 4px"' : '';
      return '<div class="results-row"' + cls + '>'
        + '<span class="results-place">' + res.place + '.</span>'
        + '<span class="results-boat">' + name + '</span>'
        + '<span style="margin-left:auto;font-size:.82rem;color:var(--text-secondary)">'
        + (status ? '<span style="color:var(--danger)">' + esc(status) + '</span> ' : '')
        + pts + ' pts</span>'
        + '</div>';
    }
    const dnfCls = res.dnf ? ' active-dnf' : '';
    const dnsCls = res.dns ? ' active-dns' : '';
    return '<div class="results-row">'
      + '<span class="results-place">' + res.place + '.</span>'
      + '<span class="results-boat">' + name + '</span>'
      + '<div class="results-flags">'
      + '<button class="flag-btn' + dnfCls + '" onclick="toggleFlag(' + res.place + ',' + res.boat_id + ',' + (!res.dnf) + ',' + res.dns + ')">DNF</button>'
      + '<button class="flag-btn' + dnsCls + '" onclick="toggleFlag(' + res.place + ',' + res.boat_id + ',' + res.dnf + ',' + (!res.dns) + ')">DNS</button>'
      + '</div>'
      + '<button class="btn-del-result" onclick="deleteResult(' + res.id + ')">&#10005;</button>'
      + '</div>';
  }).join('');
  html += '</div>';

  if (!imported) {
    const nextPlace = results.length + 1;
    html += '<div class="results-row" style="border-bottom:none;margin-top:4px">'
      + '<span class="results-place">' + nextPlace + '.</span>'
      + '<div style="position:relative;flex:1">'
      + '<input class="boat-picker-input" id="picker-input" placeholder="Search boat\u2026" autocomplete="off"'
      + ' oninput="filterBoats(this.value)" onfocus="openPicker()" onblur="closePicker()"/>'
      + '<div class="boat-dropdown" id="picker-dropdown" style="display:none"></div>'
      + '</div></div>';
  }

  body.innerHTML = html;
  if (!imported) {
    renderLinkImportedSlot();
  }
}

async function renderLinkImportedSlot() {
  const slot = document.getElementById('link-imported-slot');
  if (!slot) return;
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/imported-candidates');
    if (!r.ok) return;
    const candidates = await r.json();
    if (!candidates.length) return;
    const opts = candidates.map(c => {
      const label = (c.regatta_name || 'Regatta')
        + ' — ' + (c.class_name || '')
        + ' — Race ' + (c.race_num || '?')
        + ' (' + (c.date || '') + ', ' + c.result_count + ' results)';
      return '<option value="' + c.id + '">' + esc(label) + '</option>';
    }).join('');
    slot.innerHTML = '<div style="font-size:.75rem;color:var(--text-secondary);margin-bottom:6px">'
      + 'Imported race results available near this date:'
      + '</div>'
      + '<div style="display:flex;gap:6px;margin-bottom:10px">'
      + '<select id="imported-picker" style="flex:1;padding:6px 8px;border:1px solid var(--border);border-radius:4px;background:var(--bg-input);color:var(--text-primary);font-size:.85rem">'
      + '<option value="">— pick imported race —</option>'
      + opts
      + '</select>'
      + '<button class="btn-sm btn-add" onclick="linkImported()">Link</button>'
      + '</div>';
  } catch (e) {
    // Silent — the picker is an enhancement; hand entry still works.
  }
}

async function linkImported() {
  const sel = document.getElementById('imported-picker');
  if (!sel || !sel.value) return;
  const fd = new FormData();
  fd.append('imported_race_id', sel.value);
  const r = await fetch('/api/sessions/' + SESSION_ID + '/link-imported', {method: 'POST', body: fd});
  if (r.ok) loadResults();
}

async function unlinkImported() {
  if (!confirm('Unlink imported results from this session? Hand-entered results will show again.')) return;
  const fd = new FormData();
  fd.append('imported_race_id', '0');
  const r = await fetch('/api/sessions/' + SESSION_ID + '/link-imported', {method: 'POST', body: fd});
  if (r.ok) loadResults();
}

async function openPicker() {
  const r = await fetch('/api/boats?exclude_race=' + SESSION_ID);
  _pickerBoats = await r.json();
  showBoatDropdown('');
  document.getElementById('picker-dropdown').style.display = '';
}

function closePicker() {
  setTimeout(() => {
    const dd = document.getElementById('picker-dropdown');
    if (dd) dd.style.display = 'none';
  }, 200);
}

function filterBoats(text) {
  if (_pickerBoats) {
    showBoatDropdown(text);
    document.getElementById('picker-dropdown').style.display = '';
  }
}

function showBoatDropdown(searchText) {
  const q = searchText.trim().toLowerCase();
  const filtered = q
    ? _pickerBoats.filter(b => b.sail_number.toLowerCase().includes(q) || (b.name || '').toLowerCase().includes(q))
    : _pickerBoats;
  let html = filtered.slice(0, 15).map(b => {
    const label = esc(b.name ? b.sail_number + ' — ' + b.name : b.sail_number);
    return '<div class="boat-option" onmousedown="event.preventDefault()" onclick="selectBoat(' + b.id + ')">' + label + '</div>';
  }).join('');
  const exactMatch = filtered.some(b => b.sail_number.toLowerCase() === searchText.trim().toLowerCase());
  if (searchText.trim() && !exactMatch) {
    const js = searchText.trim().replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    html += '<div class="boat-option boat-option-new" onmousedown="event.preventDefault()" onclick="selectNewBoat(\'' + js + '\')">+ Add &ldquo;' + esc(searchText.trim()) + '&rdquo;</div>';
  }
  if (!html) html = '<div class="boat-option" style="color:var(--text-secondary);cursor:default">No boats found</div>';
  document.getElementById('picker-dropdown').innerHTML = html;
}

async function selectBoat(boatId) {
  const list = document.getElementById('results-list');
  const nextPlace = list ? list.children.length + 1 : 1;
  await fetch('/api/sessions/' + SESSION_ID + '/results', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place: nextPlace, boat_id: boatId})
  });
  _pickerBoats = null;
  loadResults();
}

async function selectNewBoat(sailNumber) {
  const list = document.getElementById('results-list');
  const nextPlace = list ? list.children.length + 1 : 1;
  await fetch('/api/sessions/' + SESSION_ID + '/results', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place: nextPlace, sail_number: sailNumber})
  });
  _pickerBoats = null;
  loadResults();
}

async function toggleFlag(place, boatId, dnf, dns) {
  await fetch('/api/sessions/' + SESSION_ID + '/results', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({place, boat_id: boatId, dnf, dns})
  });
  loadResults();
}

async function deleteResult(resultId) {
  await fetch('/api/results/' + resultId, {method: 'DELETE'});
  loadResults();
}

// ---------------------------------------------------------------------------
// Crew
// ---------------------------------------------------------------------------

let _sessionCrew = [];
let _sessionCrewUsers = [];
let _sessionCrewPositions = [];
const _sessionUserRole = cfg.dataset.userRole || 'viewer';

async function loadCrew() {
  const card = document.getElementById('crew-card');
  card.style.display = '';
  const body = document.getElementById('crew-body');
  const r = await fetch('/api/races/' + SESSION_ID + '/crew');
  const data = await r.json();
  _sessionCrew = data.crew || [];
  renderCrewCollapsed();
  // Start collapsed — show summary
  _collapsed['crew'] = true;
  body.style.display = 'none';
  document.getElementById('crew-toggle').innerHTML = '&#9654;';
}

function renderCrewCollapsed() {
  const summary = document.getElementById('crew-summary');
  if (!summary) return;
  if (_sessionCrew.length) {
    let totalBody = 0, totalGear = 0, hasWeight = false;
    const lines = _sessionCrew.map(c => {
      const pos = esc(c.position.charAt(0).toUpperCase() + c.position.slice(1));
      const name = c.attributed ? esc(c.user_name || '\u2014') : '<em>(not attributed)</em>';
      let wt = '';
      if (c.body_weight != null || c.gear_weight != null) {
        hasWeight = true;
        const b = c.body_weight || 0;
        const g = c.gear_weight || 0;
        totalBody += b;
        totalGear += g;
        wt = ' <span style="color:var(--text-muted);font-size:.75rem">('
          + (b ? b.toFixed(0) : '0');
        if (g) wt += '+' + g.toFixed(0) + 'g';
        wt += ' lbs)</span>';
      }
      return '<span style="color:var(--text-secondary)">' + pos + ':</span> ' + name + wt;
    });
    let html = lines.join(' &nbsp;\u00b7&nbsp; ');
    if (hasWeight) {
      const total = totalBody + totalGear;
      html += '<div style="color:var(--text-secondary);font-size:.78rem;margin-top:4px">'
        + 'Total crew weight: ' + total.toFixed(0) + ' lbs'
        + ' (body ' + totalBody.toFixed(0) + ' + gear ' + totalGear.toFixed(0) + ')</div>';
    }
    summary.innerHTML = html;
  } else {
    summary.innerHTML = '<span style="color:var(--text-secondary)">No crew recorded</span>';
  }
}

async function loadCrewEditForm() {
  const body = document.getElementById('crew-body');
  if (!body) return;
  // Lazy-load positions and users
  if (!_sessionCrewPositions.length) {
    const [posR, usrR] = await Promise.all([
      fetch('/api/crew/positions'),
      fetch('/api/crew/users'),
    ]);
    _sessionCrewPositions = (await posR.json()).positions || [];
    _sessionCrewUsers = (await usrR.json()).users || [];
  }
  const canEdit = _sessionUserRole === 'admin' || _sessionUserRole === 'crew';
  let html = '';
  for (const p of _sessionCrewPositions) {
    const label = esc(p.name.charAt(0).toUpperCase() + p.name.slice(1));
    const entry = _sessionCrew.find(c => c.position_id === p.id);
    const curVal = entry && entry.user_id ? String(entry.user_id) : '';
    const bodyWt = entry && entry.body_weight != null ? entry.body_weight : '';
    const gearWt = entry && entry.gear_weight != null ? entry.gear_weight : '';
    html += '<div class="crew-row" data-pos-id="' + p.id + '">';
    html += '<span class="crew-pos">' + label + '</span>';
    html += '<select class="crew-select' + (curVal ? ' has-value' : '') + '" '
      + (canEdit ? 'onchange="onSessionCrewChange(this)"' : 'disabled') + '>';
    html += '<option value="">\u2014</option>';
    // Build filtered options
    const taken = new Set();
    for (const c of _sessionCrew) {
      if (c.user_id && c.position_id !== p.id) taken.add(String(c.user_id));
    }
    for (const u of _sessionCrewUsers) {
      const uid = String(u.id);
      if (uid === curVal || !taken.has(uid)) {
        const n = esc(u.name || u.email);
        const suffix = u.pending ? ' (invited)' : '';
        html += '<option value="' + uid + '"' + (uid === curVal ? ' selected' : '') + '>' + n + suffix + '</option>';
      }
    }
    if (canEdit) html += '<option value="__new__">+ Add new...</option>';
    html += '</select>';
    html += '<input type="number" class="crew-weight" data-field="body" step="0.1" min="0" max="500"'
      + ' placeholder="Body lbs" title="Body weight (lbs)" value="' + bodyWt + '"'
      + (canEdit ? ' onchange="onSessionCrewWeightChange()"' : ' disabled') + '/>';
    html += '<input type="number" class="crew-weight" data-field="gear" step="0.1" min="0" max="100"'
      + ' placeholder="Gear lbs" title="Gear weight (lbs)" value="' + gearWt + '"'
      + (canEdit ? ' onchange="onSessionCrewWeightChange()"' : ' disabled') + '/>';
    html += '</div>';
  }
  html += '<div id="session-crew-total" class="crew-total-weight"></div>';
  body.innerHTML = html;
  _updateSessionCrewTotal();
}

function toggleCrewSection() {
  _collapsed['crew'] = !_collapsed['crew'];
  const body = document.getElementById('crew-body');
  const toggle = document.getElementById('crew-toggle');
  const summary = document.getElementById('crew-summary');
  if (_collapsed['crew']) {
    body.style.display = 'none';
    summary.style.display = '';
    toggle.innerHTML = '&#9654;';
  } else {
    body.style.display = '';
    summary.style.display = 'none';
    toggle.innerHTML = '&#9660;';
    loadCrewEditForm();
  }
}

async function onSessionCrewChange(selectEl) {
  // Handle "Add new..."
  if (selectEl && selectEl.value === '__new__') {
    selectEl.value = '';
    const name = prompt('New crew member name:');
    if (name && name.trim()) {
      try {
        const r = await fetch('/api/crew/placeholder', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: name.trim()})
        });
        if (r.ok) {
          const data = await r.json();
          _sessionCrewUsers.push({id: data.id, name: data.name, email: '', role: 'viewer'});
          const opt = document.createElement('option');
          opt.value = String(data.id);
          selectEl.appendChild(opt);
          selectEl.value = String(data.id);
        }
      } catch (e) { console.error('create placeholder error', e); }
    }
  }
  // Auto-default body weight from user profile when user is selected
  if (selectEl) {
    const row = selectEl.closest('.crew-row');
    const bodyInput = row.querySelector('.crew-weight[data-field="body"]');
    const uid = selectEl.value ? parseInt(selectEl.value) : null;
    if (uid) {
      const user = _sessionCrewUsers.find(u => u.id === uid);
      if (user && user.weight_lbs != null && !bodyInput.value) {
        bodyInput.value = user.weight_lbs;
      }
    } else {
      bodyInput.value = '';
      row.querySelector('.crew-weight[data-field="gear"]').value = '';
    }
  }
  await _saveSessionCrew();
}

function onSessionCrewWeightChange() {
  _updateSessionCrewTotal();
  _saveSessionCrew();
}

function _updateSessionCrewTotal() {
  const el = document.getElementById('session-crew-total');
  if (!el) return;
  let totalBody = 0, totalGear = 0, count = 0;
  document.querySelectorAll('#crew-body .crew-row').forEach(row => {
    const bv = parseFloat(row.querySelector('.crew-weight[data-field="body"]').value);
    const gv = parseFloat(row.querySelector('.crew-weight[data-field="gear"]').value);
    if (!isNaN(bv)) { totalBody += bv; count++; }
    if (!isNaN(gv)) totalGear += gv;
  });
  const total = totalBody + totalGear;
  if (count > 0) {
    el.innerHTML = '<strong>Total weight: ' + total.toFixed(1) + ' lbs</strong>'
      + ' <span style="color:var(--text-secondary)">=&nbsp;crew ' + totalBody.toFixed(1)
      + '&nbsp;+&nbsp;gear ' + totalGear.toFixed(1) + '</span>';
    el.style.display = 'block';
  } else {
    el.style.display = 'none';
  }
}

async function _saveSessionCrew() {
  // Collect crew with weights
  const crew = [];
  document.querySelectorAll('#crew-body .crew-row').forEach(row => {
    const posId = parseInt(row.dataset.posId);
    const sel = row.querySelector('.crew-select');
    const userId = sel.value && sel.value !== '__new__' ? parseInt(sel.value) : null;
    const bodyVal = row.querySelector('.crew-weight[data-field="body"]').value;
    const gearVal = row.querySelector('.crew-weight[data-field="gear"]').value;
    const bodyWeight = bodyVal ? parseFloat(bodyVal) : null;
    const gearWeight = gearVal ? parseFloat(gearVal) : null;
    if (userId || bodyWeight != null || gearWeight != null) {
      crew.push({position_id: posId, user_id: userId, body_weight: bodyWeight, gear_weight: gearWeight});
    }
    sel.classList.toggle('has-value', !!sel.value && sel.value !== '__new__');
  });
  await fetch('/api/races/' + SESSION_ID + '/crew', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(crew)
  });
  // Reload resolved crew and update summary
  const r = await fetch('/api/races/' + SESSION_ID + '/crew');
  const data = await r.json();
  _sessionCrew = data.crew || [];
  renderCrewCollapsed();
  // Refresh dropdowns to filter assigned users
  loadCrewEditForm();
}

// ---------------------------------------------------------------------------
// Sails
// ---------------------------------------------------------------------------

async function loadSails() {
  const card = document.getElementById('sails-card');
  card.style.display = '';
  const body = document.getElementById('sails-body');
  const [sailsResp, inventoryResp] = await Promise.all([
    fetch('/api/sessions/' + SESSION_ID + '/sails'),
    fetch('/api/sails'),
  ]);
  const current = await sailsResp.json();
  const inventory = await inventoryResp.json();
  const slots = ['main', 'jib', 'spinnaker'];
  let html = '';
  slots.forEach(slot => {
    const opts = (inventory[slot] || []).map(s =>
      '<option value="' + s.id + '"' + (current[slot] && current[slot].id === s.id ? ' selected' : '') + '>'
      + esc(s.name) + '</option>'
    ).join('');
    html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'
      + '<span style="color:var(--text-secondary);width:68px;flex-shrink:0">' + slot.charAt(0).toUpperCase() + slot.slice(1) + '</span>'
      + '<select id="sail-select-' + slot + '" style="flex:1;background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--accent-strong);border-radius:4px;padding:3px 6px;font-size:.78rem">'
      + '<option value="">\u2014 none \u2014</option>' + opts
      + '</select></div>';
  });
  html += '<button class="btn-export" style="background:var(--accent-strong);color:var(--bg-primary);border-color:var(--accent-strong);font-size:.78rem;margin-top:4px" onclick="saveSails()">Save Sails</button>';
  html += '<div id="sail-changes-timeline"></div>';
  body.innerHTML = html;

  // If no sails are set for this session, pre-select from boat-level defaults
  const hasAnySail = slots.some(slot => current[slot] && current[slot].id);
  if (!hasAnySail) {
    try {
      const defaultsResp = await fetch('/api/sails/defaults');
      if (defaultsResp.ok) {
        const defaults = await defaultsResp.json();
        slots.forEach(slot => {
          if (defaults[slot] && defaults[slot].id) {
            const sel = document.getElementById('sail-select-' + slot);
            if (sel) sel.value = String(defaults[slot].id);
          }
        });
      }
    } catch (_) { /* ignore — defaults are a convenience, not critical */ }
  }
  await loadSailChangeTimeline();
}

async function saveSails() {
  const slots = ['main', 'jib', 'spinnaker'];
  const payload = {};
  slots.forEach(slot => {
    const sel = document.getElementById('sail-select-' + slot);
    payload[slot + '_id'] = sel && sel.value ? parseInt(sel.value, 10) : null;
  });
  const r = await fetch('/api/sessions/' + SESSION_ID + '/sails', {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
  });
  if (!r.ok) { alert('Failed to save sails'); return; }
  await loadSailChangeTimeline();
}

async function loadSailChangeTimeline() {
  const container = document.getElementById('sail-changes-timeline');
  if (!container) return;
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/sail-changes');
    if (!r.ok) return;
    const data = await r.json();
    const changes = data.changes || [];
    if (changes.length <= 1) {
      container.style.display = 'none';
      return;
    }
    container.style.display = 'block';
    let html = '<div style="font-size:.75rem;color:var(--text-secondary);margin-top:8px;border-top:1px solid ' + cssVar('--border') + ';padding-top:8px">'
      + '<strong>Sail Changes</strong></div>';
    html += '<div style="font-size:.75rem;margin-top:4px">';
    changes.forEach((c, i) => {
      const ts = c.ts ? new Date(c.ts + (c.ts.endsWith('Z') ? '' : 'Z')) : null;
      const timeStr = ts ? ts.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
      const names = ['main', 'jib', 'spinnaker']
        .filter(s => c[s] && c[s].name)
        .map(s => esc(c[s].name));
      const label = names.length ? names.join(' · ') : '(none)';
      const isFirst = i === 0;
      html += '<div style="display:flex;gap:8px;align-items:baseline;margin-bottom:2px">'
        + '<span style="color:var(--text-secondary);min-width:70px">' + timeStr + '</span>'
        + '<span' + (isFirst ? ' style="color:var(--text-muted)"' : '') + '>'
        + (isFirst ? '(start) ' : '') + label + '</span>'
        + '</div>';
    });
    html += '</div>';
    container.innerHTML = html;
  } catch (e) { console.error('sail changes timeline error', e); }
}

// ---------------------------------------------------------------------------
// Transcript
// ---------------------------------------------------------------------------

async function loadTranscript() {
  // Transcript now lives inside #audio-card (consolidated with the race
  // player for parity with the debrief card layout), so visibility is
  // driven by loadAudio() showing the audio card — no separate toggle here.
  const body = document.getElementById('transcript-body');
  body.innerHTML = '<span style="color:var(--text-secondary)">Loading\u2026</span>';

  const r = await fetch('/api/audio/' + _session.audio_session_id + '/transcript');
  if (r.status === 404) {
    body.innerHTML = '<span style="color:var(--text-secondary)">No transcript yet. </span>'
      + '<button class="btn-export" style="font-size:.75rem" onclick="startTranscript()">&#9654; Transcribe</button>';
    return;
  }
  const t = await r.json();
  if (t.status === 'pending' || t.status === 'running') {
    body.innerHTML = '<span style="color:var(--warning)">Transcription in progress\u2026</span>';
    setTimeout(loadTranscript, 3000);
    return;
  }
  if (t.status === 'error') {
    body.innerHTML = '<span style="color:var(--danger)">Error: ' + esc(t.error_msg || 'unknown') + '</span>';
    return;
  }
  // Store transcript ID for tuning extraction
  if (t.id) {
    _transcriptId = t.id;
    loadTuningExtractions();
  }
  _speakerMap = t.speaker_map || {};
  // Check if segments have speaker labels (diarized) vs plain whisper segments
  const hasDiarizedSegments = t.segments && t.segments.length > 0
    && t.segments.some(s => s.speaker);
  if (hasDiarizedSegments) {
    // #648: render the retranscribe button above the shared pane render so
    // the race transcript gets the same affordance the debrief has —
    // there's always a way to re-run diarization from the transcript view.
    body.innerHTML =
      '<div style="display:flex;justify-content:flex-end;margin-bottom:4px">'
      + '<button class="btn-export" style="font-size:.7rem" onclick="retranscribe()" '
      + 'title="Re-run transcription with diarization so crew sharing a mic get separate speaker labels">'
      + '&#8635; Retranscribe with diarization</button>'
      + '</div>'
      + '<div id="race-transcript-shared"></div>';
    const shared = document.getElementById('race-transcript-shared');
    _renderDiarizedTranscript(shared, t);
  } else {
    const text = t.text ? esc(t.text) : '(empty)';
    body.innerHTML = '<div style="font-size:.8rem;color:var(--text-primary);white-space:pre-wrap;max-height:300px;overflow-y:auto;background:var(--bg-secondary);border-radius:6px;padding:8px">' + text + '</div>'
      + '<div style="margin-top:8px"><button class="btn-export" style="font-size:.75rem" onclick="retranscribe()" title="Re-run transcription with speaker diarization">&#8635; Retranscribe with diarization</button></div>';
  }
}

// Race transcript entry point — thin trampoline into the shared pane
// renderer (#648). Both race and debrief go through _renderTranscriptPane
// so layout, follow button, pencils, and auto-scroll behave identically.
function _renderDiarizedTranscript(body, t) {
  _renderTranscriptPane('race', body, t, {
    containerId: 'transcript-segments',
    followBtnId: 'transcript-follow-btn',
    surfaceName: 'transcript',
    segClass: 'transcript-seg',
    speakerClass: 'transcript-speaker',
    audioStartUtcRaw: _session && _session.audio_start_utc,
    onClickSegment: 'playTranscriptSegment',
    onClickSpeaker: (escLabel) => "openSpeakerPicker('" + escLabel + "')",
    onClickOverride: 'openSegmentOverridePicker',
    onToggleFollow: "toggleTranscriptFollow('race')",
    speakerMap: _speakerMap,
  });
}

// Shared renderer driven by pane state (#648). Keeps race + debrief in
// lock-step on layout, click affordances, follow behaviour, and scroll
// math. Pane state lives in _transcriptPanes[paneKey] so multiple panes
// can coexist on the same page without trampling each other.
function _renderTranscriptPane(paneKey, body, t, opts) {
  const prev = _transcriptPanes[paneKey] || {};
  const pane = _transcriptPanes[paneKey] = {
    ...prev,
    containerId: opts.containerId,
    followBtnId: opts.followBtnId,
    surfaceName: opts.surfaceName,
    audioStartUtcRaw: opts.audioStartUtcRaw,
    segClass: opts.segClass,
    speakerMap: opts.speakerMap || {},
    blocks: [],
    follow: prev.follow !== undefined ? prev.follow : true,
    lastActiveIdx: -1,
    surfaceRegistered: prev.surfaceRegistered || false,
    lastProgrammaticScrollAt: prev.lastProgrammaticScrollAt || 0,
  };

  const blocks = [];
  for (const seg of t.segments) {
    const last = blocks[blocks.length - 1];
    // Group by speaker, channel, AND override status so a single segment
    // override (#648) visually breaks out of its block instead of being
    // hidden under the parent speaker's label.
    const sameBlock = last
      && last.speaker === seg.speaker
      && last.channel_index === seg.channel_index
      && (last.override_user_id || null) === (seg.override_user_id || null);
    if (sameBlock) {
      last.text += ' ' + seg.text;
      last.end = seg.end;
      last._segRefs.push({
        audio_session_id: seg.audio_session_id,
        segment_index: seg.segment_index,
      });
    } else {
      const block = {...seg};
      block._segRefs = [{
        audio_session_id: seg.audio_session_id,
        segment_index: seg.segment_index,
      }];
      blocks.push(block);
    }
  }
  pane.blocks = blocks;
  // Keep legacy race-only globals in sync until every call site migrates.
  if (paneKey === 'race') {
    _transcriptBlocks = blocks;
    _speakerMap = pane.speakerMap;
  }

  const rawSpeakers = [...new Set(t.segments.map(s => s.speaker))];
  const speakers = [...new Set(blocks.map(b => b.speaker))];
  const palette = [cssVar('--accent'), cssVar('--success'), cssVar('--warning'), cssVar('--danger'), '#c4b5fd', '#f9a8d4'];
  const color = s => palette[rawSpeakers.indexOf(s) >= 0 ? rawSpeakers.indexOf(s) % palette.length : speakers.indexOf(s) % palette.length];
  const fmt = s => { const m = Math.floor(s / 60); return m + ':' + String(Math.floor(s % 60)).padStart(2, '0'); };

  const displayName = (rawLabel) => {
    const entry = pane.speakerMap[rawLabel];
    if (entry && entry.name) {
      if (entry.type === 'auto' && entry.confidence != null) {
        return entry.name + ' (' + Math.round(entry.confidence * 100) + '%)';
      }
      return entry.name;
    }
    return rawLabel;
  };

  body.innerHTML = ''
    + '<div style="display:flex;justify-content:flex-end;align-items:center;margin-bottom:6px;gap:8px">'
    + '<button id="' + opts.followBtnId + '" type="button" '
    + 'onclick="' + opts.onToggleFollow + '" '
    + 'style="font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer;border-radius:3px" '
    + 'title="Auto-scrolling to active segment. Click to pause.">\u25C9 Follow</button>'
    + '</div>'
    + '<div id="' + opts.containerId + '" style="max-height:400px;overflow-y:auto;background:var(--bg-secondary);border-radius:6px;padding:8px">'
    + blocks.map((b, i) => {
      const speakerHtml = b.override_user_id
        ? '<span class="' + opts.speakerClass + '" data-speaker="' + esc(b.speaker)
          + '" style="color:' + color(b.speaker) + ';font-weight:600;font-size:.75rem;'
          + 'font-style:italic" title="Overridden from ' + esc(displayName(b.speaker))
          + ' — click ✎ to change">'
          + esc(b.override_name || '') + '</span>'
        : '<span class="' + opts.speakerClass + '" data-speaker="' + esc(b.speaker)
          + '" style="color:' + color(b.speaker) + ';font-weight:600;font-size:.75rem;'
          + 'cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px" '
          + 'onclick="event.stopPropagation();' + opts.onClickSpeaker(esc(b.speaker)) + '" '
          + 'title="Click to assign crew">'
          + esc(displayName(b.speaker)) + '</span>';
      return '<div class="' + opts.segClass + '" data-idx="' + i + '" '
        + 'style="margin-bottom:8px;padding:4px 6px;border-radius:4px;cursor:pointer;'
        + 'transition:background .15s" onclick="' + opts.onClickSegment + '(' + i + ')">'
        + speakerHtml
        + '<span style="color:var(--text-secondary);font-size:.7rem;margin-left:4px">['
        + fmt(b.start) + ']</span>'
        + ' <span class="transcript-seg-override" title="Override this segment only" '
        + 'style="font-size:.7rem;cursor:pointer;color:var(--text-secondary);margin-left:4px" '
        + 'onclick="event.stopPropagation();' + opts.onClickOverride + '(' + i + ')">&#9998;</span>'
        + '<div style="color:var(--text-primary);font-size:.8rem;margin-top:2px">'
        + esc(b.text.trim()) + '</div>'
        + '</div>';
    }).join('')
    + '</div>';

  _registerTranscriptSurface(paneKey);
  _wireTranscriptScrollListener(paneKey);
  _renderTranscriptFollowBadge(paneKey);
}

// Legacy back-compat globals. Race pane now lives in _transcriptPanes.race,
// but some older code still reads/writes these. Kept until full migration.
let _transcriptSurfaceRegistered = false;
let _lastActiveTranscriptIdx = -1;

function _registerTranscriptSurface(paneKey) {
  const pane = _transcriptPanes[paneKey];
  if (!pane || !pane.audioStartUtcRaw) return;
  if (pane.surfaceRegistered) return;  // idempotent — transcript polls every 3s
  pane.surfaceRegistered = true;
  if (paneKey === 'race') _transcriptSurfaceRegistered = true;
  const raw = pane.audioStartUtcRaw;
  const audioStart = new Date(
    raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z'
  );
  registerSurface(pane.surfaceName, function(utc) {
    const p = _transcriptPanes[paneKey];
    if (!p || !p.blocks.length) return;
    const local = (utc.getTime() - audioStart.getTime()) / 1000;

    // Pick a single best-match block (#648): with merged sibling transcripts
    // multiple blocks can cover the same instant, and highlighting + scrolling
    // to every match yanked the scroll position to whichever one ran last in
    // the loop. The "most recently started block that contains local" is
    // stable and follows the speaker whose utterance actually started most
    // recently — what a listener would expect to see in the auto-follow view.
    let activeIdx = -1;
    let bestStart = -Infinity;
    for (let i = 0; i < p.blocks.length; i++) {
      const b = p.blocks[i];
      if (local >= b.start && local <= b.end && b.start > bestStart) {
        activeIdx = i;
        bestStart = b.start;
      }
    }

    const container = document.getElementById(p.containerId);
    const segs = container ? container.querySelectorAll('.' + p.segClass) : [];
    for (let i = 0; i < segs.length; i++) {
      const el = segs[i];
      if (!el) continue;
      el.style.background = i === activeIdx
        ? 'var(--bg-hover, rgba(255,255,255,0.08))'
        : '';
    }

    // Only scroll when the active segment actually changes — scrolling every
    // tick while the same block is playing caused the "transcript drifts away
    // from playhead" jitter, because co-active blocks flipped the scroll
    // target back and forth between their DOM positions.
    if (activeIdx !== -1 && activeIdx !== p.lastActiveIdx) {
      p.lastActiveIdx = activeIdx;
      if (paneKey === 'race') _lastActiveTranscriptIdx = activeIdx;
      const el = segs[activeIdx];
      if (container && el) _scrollTranscriptSegmentIntoView(container, el, paneKey);
    } else if (activeIdx === -1) {
      p.lastActiveIdx = -1;
      if (paneKey === 'race') _lastActiveTranscriptIdx = -1;
    }
  });
}

// Click-to-play for a transcript block. Re-enables auto-scroll, seeks the
// playback clock (video/map follow), and dispatches to the right audio
// player based on the pane (#648). Race pane drives the race mc player;
// debrief pane drives the debrief dmc player.
function _playTranscriptBlock(paneKey, idx) {
  const pane = _transcriptPanes[paneKey];
  if (!pane) return;
  const b = pane.blocks[idx];
  if (!b) return;
  // Clicking a segment is a strong "I want to follow this" signal —
  // re-enable auto-scroll if the user had paused it.
  if (!pane.follow) {
    pane.follow = true;
    if (paneKey === 'race') _transcriptFollow = true;
    _renderTranscriptFollowBadge(paneKey);
  }
  if (pane.audioStartUtcRaw) {
    const raw = pane.audioStartUtcRaw;
    const audioStart = new Date(
      raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z'
    );
    setPosition(new Date(audioStart.getTime() + b.start * 1000), {source: pane.surfaceName});
  }
  const ch = (b.channel_index !== undefined && b.channel_index !== null)
    ? b.channel_index : null;
  if (paneKey === 'debrief') {
    _dmcOnSegmentClick(ch === null ? -1 : ch, b.start, b.end);
    return;
  }
  // Race pane — multi-channel via mc player if configured, else fall back
  // to the legacy single-<audio> element.
  if ((_session.audio_channels || 1) > 1) {
    _mcOnSegmentClick(ch, b.start, b.end);
    return;
  }
  const audioEl = document.getElementById('session-audio')
    || document.querySelector('#audio-body audio');
  if (audioEl) {
    _transcriptAudio = audioEl;
    audioEl.currentTime = b.start;
    audioEl.play();
  }
}

// Back-compat thin trampolines — keep old inline onclicks resolving.
function playTranscriptSegment(idx) { _playTranscriptBlock('race', idx); }
function playDebriefTranscriptSegment(idx) { _playTranscriptBlock('debrief', idx); }

// Open a picker to override the speaker for just this one block — covers
// every underlying transcript_segments row in the block (#648). Pane-aware
// so the race pencil and the debrief pencil both use this one picker.
async function _openBlockOverridePicker(paneKey, blockIdx) {
  const pane = _transcriptPanes[paneKey];
  if (!pane) return;
  const block = pane.blocks[blockIdx];
  if (!block || !Array.isArray(block._segRefs) || !block._segRefs.length) return;
  let users;
  try {
    const r = await fetch('/api/crew/users');
    if (!r.ok) return;
    users = (await r.json()).users || [];
  } catch { return; }

  const old = document.getElementById('speaker-picker');
  if (old) old.remove();

  const picker = document.createElement('div');
  picker.id = 'speaker-picker';
  picker.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);'
    + 'background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;'
    + 'padding:16px;z-index:1000;box-shadow:0 4px 20px rgba(0,0,0,0.3);min-width:220px;'
    + 'max-height:320px;overflow-y:auto';
  let html =
    '<div style="font-weight:600;margin-bottom:4px;font-size:.85rem">'
    + 'Override speaker for this segment</div>'
    + '<div style="color:var(--text-secondary);font-size:.72rem;margin-bottom:8px">'
    + 'Only this one utterance — other segments with the same speaker are unchanged.'
    + '</div>';
  if (!users || !users.length) {
    html += '<div style="color:var(--text-secondary);font-size:.8rem">No crew members found</div>';
  } else {
    for (const u of users) {
      html += '<div class="speaker-pick-option" '
        + 'style="padding:6px 8px;cursor:pointer;border-radius:4px;font-size:.8rem" '
        + 'onmouseover="this.style.background=\'var(--bg-hover, rgba(255,255,255,0.08))\'" '
        + 'onmouseout="this.style.background=\'\'" '
        + 'onclick="_applyBlockOverride(\'' + paneKey + '\',' + blockIdx + ',' + u.id + ')">'
        + esc(u.name || u.email) + '</div>';
    }
  }
  html += '<div style="display:flex;gap:6px;justify-content:flex-end;margin-top:8px">'
    + '<button class="btn-export" style="font-size:.72rem" '
    + 'onclick="_applyBlockOverride(\'' + paneKey + '\',' + blockIdx + ',null)">Clear override</button>'
    + '<button class="btn-export" style="font-size:.72rem" '
    + 'onclick="document.getElementById(\'speaker-picker\').remove();'
    + 'document.getElementById(\'speaker-picker-backdrop\').remove()">Cancel</button>'
    + '</div>';
  picker.innerHTML = html;

  const backdrop = document.createElement('div');
  backdrop.id = 'speaker-picker-backdrop';
  backdrop.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;'
    + 'background:rgba(0,0,0,0.4);z-index:999';
  backdrop.onclick = () => { picker.remove(); backdrop.remove(); };
  document.body.appendChild(backdrop);
  document.body.appendChild(picker);
}

// Back-compat trampolines for inline onclick handlers on existing segments.
function openSegmentOverridePicker(idx) { return _openBlockOverridePicker('race', idx); }
function openDebriefBlockOverridePicker(idx) { return _openBlockOverridePicker('debrief', idx); }

async function _applyBlockOverride(paneKey, blockIdx, userId) {
  const pane = _transcriptPanes[paneKey];
  if (!pane) return;
  const block = pane.blocks[blockIdx];
  if (!block || !Array.isArray(block._segRefs)) return;
  const picker = document.getElementById('speaker-picker');
  const backdrop = document.getElementById('speaker-picker-backdrop');
  if (picker) picker.remove();
  if (backdrop) backdrop.remove();

  // Fire one POST per underlying segment. Same response on every call.
  let lastName = null;
  for (const ref of block._segRefs) {
    try {
      const r = await fetch(
        '/api/audio/' + ref.audio_session_id
        + '/transcript/segments/' + ref.segment_index + '/speaker-override',
        {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({user_id: userId}),
        },
      );
      if (r.ok) {
        const data = await r.json();
        lastName = data && data.name;
      }
    } catch (e) { /* keep going */ }
  }
  // Update block state in place so render reflects without a full reload.
  if (userId === null) {
    delete block.override_user_id;
    delete block.override_name;
  } else {
    block.override_user_id = userId;
    block.override_name = lastName;
  }
  // Cheap path — re-run the transcript loader so the block splits update
  // correctly (override status is part of the grouping key).
  if (paneKey === 'race') {
    loadTranscript();
  } else {
    const deb = _session && _session.debrief_audio;
    if (deb) _loadDebriefTranscript(deb.audio_session_id);
  }
}

// Back-compat trampoline for the previous global name.
function applySegmentOverride(blockIdx, userId) {
  return _applyBlockOverride('race', blockIdx, userId);
}

async function openSpeakerPicker(speakerLabel, audioSessionId) {
  // audioSessionId routes the eventual POST to the right transcript when
  // clicking on a debrief segment (#648). Omitted → defaults to the race.
  // Fetch crew list for the picker
  let users;
  try {
    const r = await fetch('/api/crew/users');
    if (!r.ok) return;
    users = (await r.json()).users || [];
  } catch { return; }

  // Remove any existing picker
  const old = document.getElementById('speaker-picker');
  if (old) old.remove();

  // Build a simple dropdown picker
  const picker = document.createElement('div');
  picker.id = 'speaker-picker';
  picker.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;padding:16px;z-index:1000;box-shadow:0 4px 20px rgba(0,0,0,0.3);min-width:200px;max-height:300px;overflow-y:auto';

  const sidArg = audioSessionId ? (',' + audioSessionId) : '';
  let html = '<div style="font-weight:600;margin-bottom:8px;font-size:.85rem">Assign ' + esc(speakerLabel) + ' to:</div>';
  if (!users || !users.length) {
    html += '<div style="color:var(--text-secondary);font-size:.8rem">No crew members found</div>';
  } else {
    for (const u of users) {
      html += '<div class="speaker-pick-option" style="padding:6px 8px;cursor:pointer;border-radius:4px;font-size:.8rem" onmouseover="this.style.background=\'var(--bg-hover, rgba(255,255,255,0.08))\'" onmouseout="this.style.background=\'\'" onclick="assignSpeaker(\'' + esc(speakerLabel) + '\',' + u.id + sidArg + ')">' + esc(u.name || u.email) + '</div>';
    }
  }
  html += '<div style="text-align:right;margin-top:8px"><button class="btn-export" style="font-size:.75rem" onclick="document.getElementById(\'speaker-picker\').remove()">Cancel</button></div>';
  picker.innerHTML = html;

  // Backdrop
  const backdrop = document.createElement('div');
  backdrop.id = 'speaker-picker-backdrop';
  backdrop.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:999';
  backdrop.onclick = () => { picker.remove(); backdrop.remove(); };
  document.body.appendChild(backdrop);
  document.body.appendChild(picker);
}

async function assignSpeaker(speakerLabel, userId, audioSessionId) {
  // audioSessionId defaults to the race primary so existing race-transcript
  // calls keep working; the debrief transcript passes its own id (#648).
  const sid = audioSessionId || _session.audio_session_id;
  const r = await fetch('/api/audio/' + sid + '/transcript/assign-speaker', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({speaker_label: speakerLabel, user_id: userId})
  });
  // Clean up picker
  const picker = document.getElementById('speaker-picker');
  const backdrop = document.getElementById('speaker-picker-backdrop');
  if (picker) picker.remove();
  if (backdrop) backdrop.remove();

  if (!r.ok) { alert('Failed to assign speaker'); return; }
  const data = await r.json();
  // Update speaker_map locally and re-render labels
  _speakerMap[speakerLabel] = {type: 'crew', user_id: data.user_id, name: data.name};
  // Update all speaker labels in the transcript(s). The race transcript uses
  // .transcript-speaker spans; the debrief uses .debrief-transcript-speaker.
  document.querySelectorAll(
    '.transcript-speaker[data-speaker="' + speakerLabel + '"],' +
    ' .debrief-transcript-speaker[data-speaker="' + speakerLabel + '"]'
  ).forEach(el => {
    el.textContent = data.name;
  });
  // Reload the debrief transcript so its own speakerMap updates too.
  const debAudio = _session && _session.debrief_audio;
  if (debAudio && typeof _loadDebriefTranscript === 'function') {
    _loadDebriefTranscript(debAudio.audio_session_id);
  }
}

async function startTranscript() {
  const r = await fetch('/api/audio/' + _session.audio_session_id + '/transcribe', {method: 'POST'});
  if (!r.ok) { alert('Failed to start transcription'); return; }
  loadTranscript();
}

async function retranscribe() {
  if (!confirm('Re-run transcription with diarization? The existing transcript will be replaced.')) return;
  const r = await fetch('/api/audio/' + _session.audio_session_id + '/retranscribe', {method: 'POST'});
  if (!r.ok) { alert('Failed to start retranscription'); return; }
  loadTranscript();
}

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------

// Debrief audio card (#546): when the race has an attached debrief recording,
// render a second native <audio controls> below the primary player. Debriefs
// are sequential recordings, not capture-group siblings, so they're surfaced
// as their own row rather than going through the Web Audio mix path.
function _renderDebriefPlayer() {
  const deb = _session && _session.debrief_audio;
  if (!deb) return;
  // Anchor to #audio-card (not #audio-body) so the debrief subsection lands
  // after the race transcript that now lives inside the same card.
  const card = document.getElementById('audio-card');
  if (!card) return;
  const existing = document.getElementById('debrief-player');
  if (existing) existing.remove();
  const wrap = document.createElement('div');
  wrap.id = 'debrief-player';

  // #648: collapsible "Debrief Audio" header matches the "Race Audio" section
  // above so the two audio blocks read as peer sections at the same visual
  // weight. Player + transcript both live inside the body so they collapse
  // together.
  wrap.innerHTML =
    '<div class="section-title" style="margin-top:14px;cursor:pointer" '
    + 'onclick="toggleSection(\'debrief-audio\')">'
    + 'Debrief Audio <span id="debrief-audio-toggle">&#9660;</span></div>'
    + '<div class="section-body" id="debrief-audio-body">'
    + '<div id="debrief-audio-inner"></div>'
    + '<div class="section-title" style="margin-top:12px;cursor:pointer" '
    + 'onclick="toggleSection(\'debrief-transcript\')">'
    + 'Transcript <span id="debrief-transcript-toggle">&#9660;</span></div>'
    + '<div class="section-body" id="debrief-transcript-body" style="font-size:.78rem">'
    + '<span style="color:var(--text-secondary)">Loading transcript\u2026</span>'
    + '</div>'
    + '</div>';
  card.appendChild(wrap);

  const inner = document.getElementById('debrief-audio-inner');
  const siblings = Array.isArray(deb.siblings) ? deb.siblings : [];
  if (siblings.length > 1) {
    _renderDebriefMultiChannel(inner, deb, siblings);
  } else {
    inner.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px">'
      + '<audio id="debrief-audio" controls preload="metadata" style="flex:1;min-width:0">'
      + '<source src="' + deb.stream_url + '" type="audio/wav"></audio>'
      + '<a class="btn-sm" href="/api/audio/' + deb.audio_session_id + '/download" '
      + 'style="font-size:.72rem;text-decoration:none" title="Download debrief WAV">&#8595;</a>'
      + '</div>';
  }
  _loadDebriefTranscript(deb.audio_session_id);
}

// ---------------------------------------------------------------------------
// Debrief multi-channel player (#648)
//
// Mirrors the race multi-channel player but uses plain <audio> elements
// (one per sibling) + .muted for isolation, instead of the Web Audio
// decodeAudioData path. Plenty for the debrief UX -- sticky isolation +
// channel mixing -- and works for any length without memory ballooning.
// ---------------------------------------------------------------------------

let _dmcEls = [];
let _dmcIsolated = null;
let _dmcSticky = false;
let _dmcIsolationTimer = null;
let _dmcDurationS = 0;
let _dmcSiblings = [];

function _renderDebriefMultiChannel(wrap, deb, siblings) {
  _dmcSiblings = siblings;
  _dmcEls = [];
  _dmcIsolated = null;
  _dmcSticky = false;
  _dmcDurationS = 0;
  const labels = siblings.map(s => s.position_name || `R${(Number(s.ordinal) || 0) + 1}`);
  const downloadHref = '/api/audio/' + deb.audio_session_id + '/download';
  wrap.innerHTML =
    '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
    + '<button id="dmc-playpause" class="btn-sm" onclick="_dmcTogglePlay()" '
    + 'style="font-size:1.1rem;padding:4px 12px">&#9654;</button>'
    + '<input id="dmc-seek" type="range" min="0" max="1000" value="0" '
    + 'style="flex:1;min-width:160px" oninput="_dmcSeekFromSlider(this.value)">'
    + '<span id="dmc-time" style="font-size:.78rem;color:var(--text-secondary);'
    + 'min-width:80px;text-align:right">0:00 / 0:00</span>'
    + '<a class="btn-sm" href="' + downloadHref + '" '
    + 'style="font-size:.72rem;text-decoration:none" title="Download debrief primary WAV">&#8595;</a>'
    + '</div>'
    + '<div style="display:flex;align-items:center;gap:10px;margin-top:6px;'
    + 'font-size:.78rem;color:var(--text-secondary);flex-wrap:wrap">'
    + '<label><input id="dmc-sticky" type="checkbox" onchange="_dmcToggleSticky(this.checked)"> '
    + 'Sticky isolation</label>'
    + '<button class="btn-sm" onclick="_dmcSetIsolation(null)">All channels</button>'
    + siblings.map((s, i) =>
      '<button class="btn-sm" onclick="_dmcSetIsolation(' + i + ')">' + esc(labels[i]) + '</button>'
    ).join('')
    + '<span id="dmc-isolation-indicator">mixed</span>'
    + '</div>'
    // Hidden <audio> elements, one per sibling -- driven in parallel.
    + siblings.map(s =>
      '<audio id="dmc-sib-' + s.ordinal + '" preload="metadata" src="' + esc(s.stream_url)
      + '" style="display:none"></audio>'
    ).join('');
}

function _dmcInit() {
  // Resolve element handles + wire listeners. Safe to re-call; each element
  // is wired at most once via a property flag. Returns true when all
  // siblings have been resolved in the DOM.
  if (!_dmcSiblings.length) return false;
  _dmcEls = _dmcSiblings.map(s => document.getElementById('dmc-sib-' + s.ordinal));
  if (_dmcEls.some(e => !e)) return false;
  _dmcEls.forEach(el => {
    if (el._dmcWired) return;
    el._dmcWired = true;
    el.addEventListener('loadedmetadata', () => {
      if (isFinite(el.duration) && el.duration > _dmcDurationS) {
        _dmcDurationS = el.duration;
        _dmcUpdateProgress();
      }
    });
  });
  const primary = _dmcEls[0];
  if (!primary._dmcPrimaryWired) {
    primary._dmcPrimaryWired = true;
    let dmcFanoutLast = 0;
    primary.addEventListener('timeupdate', () => {
      _dmcUpdateProgress();
      const t = primary.currentTime;
      for (let i = 1; i < _dmcEls.length; i++) {
        if (Math.abs(_dmcEls[i].currentTime - t) > 0.2) {
          try { _dmcEls[i].currentTime = t; } catch (e) { /* swallow */ }
        }
      }
      // Fan out to the playback clock so the debrief transcript's auto-scroll
      // (and any other consumer) keeps up with playback (#648). Throttle to
      // 5 Hz — matches the existing audio fanout cadence. Uses source
      // 'debrief-mc' so the debrief transcript consumer is NOT the one we
      // skip in setPosition's same-source filter.
      const deb = _session && _session.debrief_audio;
      if (deb && deb.start_utc) {
        const now = (typeof _clockNowMs === 'function') ? _clockNowMs() : Date.now();
        if (now - dmcFanoutLast >= 200) {
          dmcFanoutLast = now;
          const raw = deb.start_utc;
          const startUtc = new Date(
            raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z'
          );
          if (!isNaN(startUtc.getTime())) {
            setPosition(
              new Date(startUtc.getTime() + t * 1000),
              {source: 'debrief-mc'},
            );
          }
        }
      }
    });
    primary.addEventListener('ended', () => {
      _dmcEls.forEach(el => { try { el.pause(); } catch (e) { /* swallow */ } });
      const btn = document.getElementById('dmc-playpause');
      if (btn) btn.innerHTML = '&#9654;';
    });
  }
  return true;
}

function _dmcTogglePlay() {
  if (!_dmcInit()) return;
  const btn = document.getElementById('dmc-playpause');
  if (_dmcEls[0].paused) {
    _dmcEls.forEach(el => {
      const p = el.play();
      if (p && typeof p.catch === 'function') p.catch(() => { /* autoplay blocked */ });
    });
    if (btn) btn.innerHTML = '&#9208;';
  } else {
    _dmcEls.forEach(el => { try { el.pause(); } catch (e) { /* swallow */ } });
    if (btn) btn.innerHTML = '&#9654;';
  }
}

function _dmcSeekFromSlider(val) {
  if (!_dmcInit() || !_dmcDurationS) return;
  const t = (Number(val) / 1000) * _dmcDurationS;
  _dmcEls.forEach(el => { try { el.currentTime = t; } catch (e) { /* swallow */ } });
}

function _dmcUpdateProgress() {
  const seek = document.getElementById('dmc-seek');
  const time = document.getElementById('dmc-time');
  if (!seek || !time || !_dmcEls.length) return;
  const t = _dmcEls[0].currentTime || 0;
  if (_dmcDurationS > 0) seek.value = String((t / _dmcDurationS) * 1000);
  const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  time.textContent = `${fmt(t)} / ${fmt(_dmcDurationS)}`;
}

function _dmcSetIsolation(idx) {
  if (!_dmcInit()) return;
  _dmcIsolated = idx;
  _dmcEls.forEach((el, i) => {
    el.muted = idx !== null && i !== idx;
  });
  const ind = document.getElementById('dmc-isolation-indicator');
  if (ind) {
    if (idx === null) {
      ind.textContent = 'mixed';
    } else {
      const label = (_dmcSiblings[idx] && _dmcSiblings[idx].position_name) || `R${idx + 1}`;
      ind.textContent = 'isolated: ' + label;
    }
  }
}

function _dmcToggleSticky(on) {
  _dmcSticky = !!on;
  if (!_dmcSticky) {
    if (_dmcIsolationTimer) {
      clearTimeout(_dmcIsolationTimer);
      _dmcIsolationTimer = null;
    }
    _dmcSetIsolation(null);
  }
}

// Called by debrief transcript segment clicks -- seeks all <audio> els to
// startSec, starts playback, optionally isolates a channel for the segment's
// duration (or permanently when sticky).
function _dmcOnSegmentClick(channelIdx, startSec, endSec) {
  if (!_dmcInit()) return;
  const t = Math.max(0, Number(startSec) || 0);
  _dmcEls.forEach(el => { try { el.currentTime = t; } catch (e) { /* swallow */ } });
  _dmcEls.forEach(el => {
    const p = el.play();
    if (p && typeof p.catch === 'function') p.catch(() => { /* swallow */ });
  });
  const btn = document.getElementById('dmc-playpause');
  if (btn) btn.innerHTML = '&#9208;';
  if (typeof channelIdx === 'number' && channelIdx >= 0) {
    if (_dmcIsolationTimer) {
      clearTimeout(_dmcIsolationTimer);
      _dmcIsolationTimer = null;
    }
    _dmcSetIsolation(channelIdx);
    if (!_dmcSticky) {
      const durMs = Math.max(500, ((Number(endSec) || 0) - t) * 1000);
      _dmcIsolationTimer = setTimeout(() => {
        _dmcSetIsolation(null);
        _dmcIsolationTimer = null;
      }, durMs);
    }
  }
}

// Seek the debrief audio to `t` seconds and start playback. When the debrief
// is multi-sibling (#648), hand off to the sibling coordinator so isolation +
// sticky apply on transcript clicks. Single-stream debriefs drop through to
// the plain <audio> element.
function seekDebriefAudio(t, channelIdx) {
  if (_dmcSiblings && _dmcSiblings.length > 1) {
    _dmcOnSegmentClick(
      typeof channelIdx === 'number' ? channelIdx : -1,
      t,
      t,
    );
    return;
  }
  const el = document.getElementById('debrief-audio');
  if (!el) return;
  try {
    el.currentTime = Math.max(0, Number(t) || 0);
    const p = el.play();
    if (p && typeof p.catch === 'function') p.catch(() => { /* swallow autoplay */ });
  } catch (e) { /* swallow */ }
}

// Debrief transcript (#546): self-contained fetch + render for the debrief
// audio row. Intentionally does not touch the race-transcript globals
// (_transcriptBlocks, _transcriptId, _speakerMap) or wire audio-sync — the
// debrief is off the race timeline and doesn't feed tuning extraction.
async function _loadDebriefTranscript(audioSessionId) {
  const body = document.getElementById('debrief-transcript-body');
  if (!body) return;
  const r = await fetch('/api/audio/' + audioSessionId + '/transcript');
  if (r.status === 404) {
    body.innerHTML =
      '<span style="color:var(--text-secondary)">No transcript yet. </span>'
      + '<button class="btn-export" style="font-size:.72rem" '
      + 'onclick="startDebriefTranscript(' + audioSessionId + ')">'
      + '&#9654; Transcribe</button>';
    return;
  }
  const t = await r.json();
  if (t.status === 'pending' || t.status === 'running') {
    body.innerHTML = '<span style="color:var(--warning)">Transcription in progress\u2026</span>';
    setTimeout(() => _loadDebriefTranscript(audioSessionId), 3000);
    return;
  }
  if (t.status === 'error') {
    body.innerHTML =
      '<span style="color:var(--danger)">Error: ' + esc(t.error_msg || 'unknown') + '</span>';
    return;
  }
  const segs = Array.isArray(t.segments) ? t.segments : [];
  const hasDiarized = segs.length > 0 && segs.some(s => s.speaker);

  // #648: debrief transcript now uses the same shared renderer as the race
  // transcript — block grouping, follow button, per-segment override pencil,
  // speaker-click crew picker, auto-scroll synced to the debrief player.
  // Non-diarized fallback keeps the simple text view + retranscribe button.
  if (hasDiarized) {
    // Retranscribe button sits above the shared render; the shared renderer
    // owns everything from the follow button down.
    body.innerHTML =
      '<div style="display:flex;justify-content:flex-end;margin-bottom:4px">'
      + '<button class="btn-export" style="font-size:.7rem" '
      + 'onclick="retranscribeDebrief(' + audioSessionId + ')" '
      + 'title="Re-run transcription with diarization so crew sharing a mic get separate speaker labels">'
      + '&#8635; Retranscribe with diarization</button>'
      + '</div>'
      + '<div id="debrief-transcript-shared"></div>';
    const shared = document.getElementById('debrief-transcript-shared');
    const debAudio = _session && _session.debrief_audio;
    _renderTranscriptPane('debrief', shared, t, {
      containerId: 'debrief-transcript-segments',
      followBtnId: 'debrief-transcript-follow-btn',
      surfaceName: 'debrief-transcript',
      segClass: 'debrief-transcript-seg',
      speakerClass: 'debrief-transcript-speaker',
      audioStartUtcRaw: debAudio && debAudio.start_utc,
      onClickSegment: 'playDebriefTranscriptSegment',
      onClickSpeaker: (escLabel) => "openSpeakerPicker('" + escLabel + "'," + audioSessionId + ")",
      onClickOverride: 'openDebriefBlockOverridePicker',
      onToggleFollow: "toggleTranscriptFollow('debrief')",
      speakerMap: t.speaker_map || {},
    });
  } else if (segs.length) {
    // Non-diarized fallback — short untimed list + retranscribe CTA.
    const fmt = s => { const m = Math.floor(s / 60); return m + ':' + String(Math.floor(s % 60)).padStart(2, '0'); };
    const segStyle = 'margin-bottom:3px;cursor:pointer;padding:2px 4px;border-radius:3px';
    const html = segs.map(s => {
      const start = Number(s.start) || 0;
      const ch = (s.channel_index !== undefined && s.channel_index !== null)
        ? Number(s.channel_index) : -1;
      return '<div style="' + segStyle + '" onclick="seekDebriefAudio(' + start + ',' + ch + ')" '
        + 'onmouseover="this.style.background=\'var(--bg-primary)\'" '
        + 'onmouseout="this.style.background=\'transparent\'" '
        + 'title="Click to play from here">'
        + '<span style="color:var(--text-secondary);font-family:monospace">['
        + fmt(start) + ']</span> ' + esc(s.text || '') + '</div>';
    }).join('');
    body.innerHTML =
      '<div style="display:flex;justify-content:flex-end;margin-bottom:4px">'
      + '<button class="btn-export" style="font-size:.7rem" '
      + 'onclick="retranscribeDebrief(' + audioSessionId + ')">'
      + '&#8635; Retranscribe with diarization</button>'
      + '</div>'
      + '<div style="max-height:260px;overflow-y:auto;background:var(--bg-secondary);'
      + 'border-radius:6px;padding:8px;color:var(--text-primary)">' + html + '</div>';
  } else if (t.text) {
    body.innerHTML = '<div style="white-space:pre-wrap">' + esc(t.text) + '</div>';
  } else {
    body.innerHTML = '<span style="color:var(--text-secondary)">(empty)</span>';
  }
}

// Debrief-transcript per-segment override picker (#648). Same flow as the
// race-transcript openSegmentOverridePicker but targets one debrief segment
// directly (debrief renders per-segment, no grouping).
async function openDebriefSegmentOverridePicker(audioSessionId, segmentIndex) {
  let users;
  try {
    const r = await fetch('/api/crew/users');
    if (!r.ok) return;
    users = (await r.json()).users || [];
  } catch { return; }

  const old = document.getElementById('speaker-picker');
  if (old) old.remove();

  const picker = document.createElement('div');
  picker.id = 'speaker-picker';
  picker.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);'
    + 'background:var(--bg-primary);border:1px solid var(--border);border-radius:8px;'
    + 'padding:16px;z-index:1000;box-shadow:0 4px 20px rgba(0,0,0,0.3);min-width:220px;'
    + 'max-height:320px;overflow-y:auto';
  let html =
    '<div style="font-weight:600;margin-bottom:4px;font-size:.85rem">'
    + 'Override speaker for this segment</div>'
    + '<div style="color:var(--text-secondary);font-size:.72rem;margin-bottom:8px">'
    + 'Only this one utterance — other segments with the same speaker are unchanged.'
    + '</div>';
  if (!users || !users.length) {
    html += '<div style="color:var(--text-secondary);font-size:.8rem">No crew members found</div>';
  } else {
    for (const u of users) {
      html += '<div class="speaker-pick-option" '
        + 'style="padding:6px 8px;cursor:pointer;border-radius:4px;font-size:.8rem" '
        + 'onmouseover="this.style.background=\'var(--bg-hover, rgba(255,255,255,0.08))\'" '
        + 'onmouseout="this.style.background=\'\'" '
        + 'onclick="applyDebriefSegmentOverride('
        + audioSessionId + ',' + segmentIndex + ',' + u.id + ')">'
        + esc(u.name || u.email) + '</div>';
    }
  }
  html += '<div style="display:flex;gap:6px;justify-content:flex-end;margin-top:8px">'
    + '<button class="btn-export" style="font-size:.72rem" '
    + 'onclick="applyDebriefSegmentOverride('
    + audioSessionId + ',' + segmentIndex + ',null)">Clear override</button>'
    + '<button class="btn-export" style="font-size:.72rem" '
    + 'onclick="document.getElementById(\'speaker-picker\').remove();'
    + 'document.getElementById(\'speaker-picker-backdrop\').remove()">Cancel</button>'
    + '</div>';
  picker.innerHTML = html;

  const backdrop = document.createElement('div');
  backdrop.id = 'speaker-picker-backdrop';
  backdrop.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;'
    + 'background:rgba(0,0,0,0.4);z-index:999';
  backdrop.onclick = () => { picker.remove(); backdrop.remove(); };
  document.body.appendChild(backdrop);
  document.body.appendChild(picker);
}

async function applyDebriefSegmentOverride(audioSessionId, segmentIndex, userId) {
  const picker = document.getElementById('speaker-picker');
  const backdrop = document.getElementById('speaker-picker-backdrop');
  if (picker) picker.remove();
  if (backdrop) backdrop.remove();
  try {
    await fetch(
      '/api/audio/' + audioSessionId
      + '/transcript/segments/' + segmentIndex + '/speaker-override',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user_id: userId}),
      },
    );
  } catch (e) { /* swallow */ }
  const deb = _session && _session.debrief_audio;
  if (deb) _loadDebriefTranscript(deb.audio_session_id);
}

async function retranscribeDebrief(audioSessionId) {
  if (!confirm(
    'Re-run transcription with diarization? The existing debrief transcript will be replaced. '
    + 'This takes a few minutes on the remote worker.'
  )) return;
  const body = document.getElementById('debrief-transcript-body');
  if (body) {
    body.innerHTML = '<span style="color:var(--warning)">Starting retranscription…</span>';
  }
  const r = await fetch('/api/audio/' + audioSessionId + '/retranscribe', { method: 'POST' });
  if (!r.ok) {
    if (body) {
      body.innerHTML = '<span style="color:var(--danger)">Failed to start retranscription</span>';
    }
    return;
  }
  _loadDebriefTranscript(audioSessionId);
}

async function startDebriefTranscript(audioSessionId) {
  const body = document.getElementById('debrief-transcript-body');
  if (body) {
    body.innerHTML = '<span style="color:var(--warning)">Starting transcription\u2026</span>';
  }
  const r = await fetch('/api/audio/' + audioSessionId + '/transcribe', { method: 'POST' });
  if (!r.ok) {
    if (body) {
      body.innerHTML = '<span style="color:var(--danger)">Failed to start transcription</span>';
    }
    return;
  }
  _loadDebriefTranscript(audioSessionId);
}

function loadAudio() {
  const card = document.getElementById('audio-card');
  card.style.display = '';
  // Multi-channel sessions use the Web Audio path so transcript clicks can
  // isolate a single channel without re-fetching audio (#462 pt.6).
  if ((_session.audio_channels || 1) > 1) {
    loadMultiChannelAudio();
    return;
  }
  document.getElementById('audio-body').innerHTML =
    '<audio id="session-audio" controls style="width:100%">'
    + '<source src="/api/audio/' + _session.audio_session_id + '/stream" type="audio/wav">'
    + '</audio>';
  _renderDebriefPlayer();
  const el = document.getElementById('session-audio');
  if (!el) return;
  // Always wire audio→transcript highlighting (works even if audio_start_utc
  // is missing — segments use audio-local seconds, same as el.currentTime).
  el.addEventListener('timeupdate', _highlightTranscriptFromAudio);
  el.addEventListener('seeked', _highlightTranscriptFromAudio);

  if (!_session.audio_start_utc) return;
  const audioStart = new Date(
    _session.audio_start_utc.endsWith('Z') || _session.audio_start_utc.includes('+')
      ? _session.audio_start_utc
      : _session.audio_start_utc + 'Z'
  );

  const audioLocalToUtc = s => new Date(audioStart.getTime() + s * 1000);
  const utcToAudioLocal = utc => (utc.getTime() - audioStart.getTime()) / 1000;

  // Audio is a consumer — seek to the requested UTC if it's within range.
  // A "large jump" (>2 s away) is treated as a user click, so we pause any
  // currently-playing audio: it's distracting to have audio keep going from
  // a new spot just because the user clicked somewhere on the page.
  // Track the most recently requested local position. Setting currentTime
  // on a still-loading <audio> element is silently dropped by the browser,
  // so we re-apply the target on 'loadedmetadata' and 'play' to guarantee
  // the user's scrub lands when playback actually starts.
  let _audioTargetLocal = null;
  const _applyAudioTarget = () => {
    if (_audioTargetLocal == null) return;
    if (el.duration && _audioTargetLocal > el.duration) return;
    if (Math.abs(el.currentTime - _audioTargetLocal) < 0.15) return;
    try { el.currentTime = _audioTargetLocal; } catch (e) { /* not seekable yet */ }
  };
  el.addEventListener('loadedmetadata', _applyAudioTarget);

  registerSurface('audio', function(utc) {
    // NB: setting el.currentTime on a paused audio element is safe — it
    // doesn't start playback — so unlike the video consumer we don't need
    // to skip based on currentSource. Seeking a paused audio keeps its
    // scrubber in lockstep with YT/replay even when the user isn't
    // listening to it.
    const local = utcToAudioLocal(utc);
    if (local < 0 || (el.duration && local > el.duration)) return;
    _audioTargetLocal = local;
    const delta = Math.abs(el.currentTime - local);
    if (delta < 0.15) return; // already there
    if (delta > _LARGE_JUMP_SEC && !el.paused) {
      try { el.pause(); } catch (e) { /* swallow */ }
    }
    try { el.currentTime = local; } catch (e) { /* not seekable yet */ }
  });

  // Audio is a producer — fan out to other surfaces when user scrubs/plays.
  // timeupdate fires ~4 Hz during playback, so this drives the map cursor and
  // any other UTC-based consumers in real time.
  let _audioFanoutLast = 0;
  const _fanout = () => {
    if (_isEchoEvent()) return;
    const now = _clockNowMs();
    if (now - _audioFanoutLast < 150) return; // throttle
    _audioFanoutLast = now;
    setPosition(audioLocalToUtc(el.currentTime), {source: 'audio'});
  };
  el.addEventListener('seeked', _fanout);
  el.addEventListener('timeupdate', _fanout);
  // The WAV player plays independently — it is NOT allowed to drive the
  // replay clock's 'playing' state. The clock is reserved for the track
  // replay play button. Fanout via timeupdate is enough to keep the map
  // cursor and gauges tracking while WAV plays on its own.
  el.addEventListener('play', function() {
    // Re-apply any pending target seek first — if the user scrubbed before
    // metadata loaded, the original currentTime= assignment may have been
    // dropped, and we'd otherwise start playback from 0:00.
    _applyAudioTarget();
    setPosition(audioLocalToUtc(el.currentTime), {source: 'audio'});
  });
}

// ---------------------------------------------------------------------------
// Multi-channel Web Audio playback (#462 pt.6)
//
// Pipeline:
//   fetch WAV → AudioContext.decodeAudioData → AudioBufferSourceNode →
//   ChannelSplitterNode → per-channel GainNodes → ChannelMergerNode → destination
//
// All channels are audible by default. Clicking a transcript segment mutes
// the other channels for the duration of that segment, then resumes the
// mixed playback. The "sticky" toggle locks isolation until released.
// ---------------------------------------------------------------------------

let _mcCtx = null;
let _mcBuffer = null;          // primary duration/time reference
let _mcSource = null;          // single-file multi-channel source
let _mcSplitter = null;        // used only in single-file multi-channel path
let _mcMerger = null;
let _mcGains = [];
let _mcStartTime = 0;        // AudioContext.currentTime when playback started
let _mcStartOffset = 0;       // buffer offset (seconds) when playback started
let _mcIsPlaying = false;
let _mcIsolatedChannel = null;
let _mcIsolationTimer = null;
// Sibling-card capture (#509): N mono buffers played in parallel, one
// BufferSource per receiver. Sync is sample-accurate because all sources
// share the same AudioContext clock via a single start(when, offset).
let _mcSiblings = false;
let _mcBuffers = [];
let _mcSources = [];
let _mcSticky = false;
let _mcRafHandle = null;
// Streaming mode (#648): for sessions over AUDIO_STREAM_THRESHOLD_MINUTES we
// play each sibling through an HTMLAudioElement + MediaElementAudioSourceNode
// instead of decodeAudioData, so memory stays flat regardless of session
// length. Sync is approximate (browser nudges, not sample-accurate), which is
// fine for lavalier mic audio but would be wrong for music.
let _mcStreamMode = false;
let _mcAudioEls = [];
let _mcMediaSources = [];
// Target of a just-issued seek in streaming mode. Reads of
// _mcAudioEls[0].currentTime can be briefly stale during the Chrome seek
// cycle (we've seen reads return the pre-seek time for ~50 ms after a click),
// which shipped the transcript consumer a stale `local` and caused the
// auto-scroll to jump to the wrong place. While a seek is in flight we hand
// out the authoritative target instead.
let _mcPendingSeekTarget = null;
let _mcPendingSeekClearAt = 0;

function _mcCurrentTime() {
  if (_mcStreamMode) {
    // During an in-flight seek, primary.currentTime can briefly report stale
    // values — prefer the pending target so the transcript scroll lands at
    // the clicked segment instead of the pre-click playhead (#648).
    if (_mcPendingSeekTarget !== null) {
      if (_clockNowMs() >= _mcPendingSeekClearAt) {
        _mcPendingSeekTarget = null;
      } else {
        return _mcPendingSeekTarget;
      }
    }
    const el = _mcAudioEls[0];
    return el ? el.currentTime : _mcStartOffset;
  }
  if (!_mcCtx || !_mcBuffer) return 0;
  if (!_mcIsPlaying) return _mcStartOffset;
  return _mcStartOffset + (_mcCtx.currentTime - _mcStartTime);
}

function _mcSetIsolation(channelIndex) {
  // null = mixed (all gains 1); otherwise mute every other channel
  _mcIsolatedChannel = channelIndex;
  if (!_mcGains.length) return;
  _mcGains.forEach((g, i) => {
    g.gain.value = (channelIndex === null || i === channelIndex) ? 1 : 0;
  });
  const ind = document.getElementById('mc-isolation-indicator');
  if (ind) {
    if (channelIndex === null) {
      ind.textContent = 'mixed';
    } else {
      // Prefer the configured position_name (R1 / R2 / "helm pair" / …) —
      // matches the debrief's isolation indicator format (#648).
      const sib = _session
        && Array.isArray(_session.audio_siblings)
        && _session.audio_siblings[channelIndex];
      const label = (sib && sib.position_name) || ('R' + (channelIndex + 1));
      ind.textContent = 'isolated: ' + label;
    }
  }
}

function _mcClearTimer() {
  if (_mcIsolationTimer !== null) {
    clearTimeout(_mcIsolationTimer);
    _mcIsolationTimer = null;
  }
}

function _mcRebuildSource(offsetSeconds) {
  // Streaming mode (#648): seek every <audio> element and start playback
  // on all. Drift correction happens in the progress tick.
  if (_mcStreamMode) {
    _mcPendingSeekTarget = offsetSeconds;
    _mcPendingSeekClearAt = _clockNowMs() + 500;
    _mcAudioEls.forEach(el => {
      try { el.currentTime = offsetSeconds; } catch (e) { /* not seekable yet */ }
    });
    _mcAudioEls.forEach(el => {
      const p = el.play();
      if (p && typeof p.catch === 'function') p.catch(() => { /* autoplay blocked */ });
    });
    _mcStartOffset = offsetSeconds;
    _mcIsPlaying = true;
    _mcUpdateButtons();
    return;
  }

  // Sibling-card mode: rebuild N BufferSources in parallel, started at the
  // same AudioContext time so all receivers stay sample-aligned (#509).
  if (_mcSiblings) {
    _mcSources.forEach(s => {
      try { s.stop(); } catch (e) { /* not started */ }
      try { s.disconnect(); } catch (e) { /* swallow */ }
    });
    _mcSources = [];
    const when = _mcCtx.currentTime + 0.02;  // small lead so start() is atomic
    _mcBuffers.forEach((buf, i) => {
      const src = _mcCtx.createBufferSource();
      src.buffer = buf;
      src.connect(_mcGains[i]);
      if (i === 0) {
        src.onended = () => {
          if (!_mcSources.length) return;
          if (_mcCurrentTime() >= _mcBuffer.duration - 0.05) {
            _mcIsPlaying = false;
            _mcStartOffset = 0;
            _mcUpdateButtons();
          }
        };
      }
      src.start(when, offsetSeconds);
      _mcSources.push(src);
    });
    _mcStartTime = when;
    _mcStartOffset = offsetSeconds;
    _mcIsPlaying = true;
    _mcUpdateButtons();
    return;
  }

  // AudioBufferSourceNode is single-use: every play/seek requires a fresh one.
  if (_mcSource) {
    try { _mcSource.stop(); } catch (e) { /* not started */ }
    try { _mcSource.disconnect(); } catch (e) { /* swallow */ }
  }
  _mcSource = _mcCtx.createBufferSource();
  _mcSource.buffer = _mcBuffer;
  _mcSource.connect(_mcSplitter);
  _mcSource.onended = () => {
    if (!_mcSource) return;
    // Distinguish a natural end-of-buffer from a manual stop() during seek
    if (_mcCurrentTime() >= _mcBuffer.duration - 0.05) {
      _mcIsPlaying = false;
      _mcStartOffset = 0;
      _mcUpdateButtons();
    }
  };
  _mcSource.start(0, offsetSeconds);
  _mcStartTime = _mcCtx.currentTime;
  _mcStartOffset = offsetSeconds;
  _mcIsPlaying = true;
  _mcUpdateButtons();
}

function _mcPlay() {
  if (!_mcCtx || !_mcBuffer) return;
  if (_mcCtx.state === 'suspended') _mcCtx.resume();
  _mcRebuildSource(_mcStartOffset);
  _mcStartProgressTick();
}

function _mcPause() {
  if (!_mcIsPlaying) return;
  if (_mcStreamMode) {
    _mcStartOffset = _mcCurrentTime();
    _mcAudioEls.forEach(el => { try { el.pause(); } catch (e) { /* swallow */ } });
    _mcIsPlaying = false;
    _mcUpdateButtons();
    _mcStopProgressTick();
    return;
  }
  if (_mcSiblings) {
    _mcStartOffset = _mcCurrentTime();
    _mcSources.forEach(s => { try { s.stop(); } catch (e) { /* swallow */ } });
    _mcIsPlaying = false;
    _mcUpdateButtons();
    _mcStopProgressTick();
    return;
  }
  if (!_mcSource) return;
  _mcStartOffset = _mcCurrentTime();
  try { _mcSource.stop(); } catch (e) { /* already stopped */ }
  _mcIsPlaying = false;
  _mcUpdateButtons();
  _mcStopProgressTick();
}

function _mcSeek(toSeconds) {
  if (!_mcCtx || !_mcBuffer) return;
  const clamped = Math.max(0, Math.min(_mcBuffer.duration, toSeconds));
  if (_mcStreamMode) {
    _mcPendingSeekTarget = clamped;
    _mcPendingSeekClearAt = _clockNowMs() + 500;  // clear within one RAF batch
    _mcAudioEls.forEach(el => {
      try { el.currentTime = clamped; } catch (e) { /* not seekable yet */ }
    });
    if (!_mcIsPlaying) _mcStartOffset = clamped;
    _mcUpdateProgress();
    // Force the transcript consumer to re-pick an active block on the next
    // tick, even if we happen to pick the same DOM index — guarantees the
    // scroll lands at the clicked target.
    _lastActiveTranscriptIdx = -1;
    return;
  }
  if (_mcIsPlaying) {
    _mcRebuildSource(clamped);
  } else {
    _mcStartOffset = clamped;
  }
  _mcUpdateProgress();
  _lastActiveTranscriptIdx = -1;
}

function _mcUpdateButtons() {
  const btn = document.getElementById('mc-playpause');
  if (btn) btn.textContent = _mcIsPlaying ? '⏸' : '▶';
}

function _mcUpdateProgress() {
  const seek = document.getElementById('mc-seek');
  const time = document.getElementById('mc-time');
  if (!seek || !time || !_mcBuffer) return;
  const t = _mcCurrentTime();
  seek.value = String((t / _mcBuffer.duration) * 1000);
  const fmt = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  time.textContent = `${fmt(t)} / ${fmt(_mcBuffer.duration)}`;
}

let _mcFanoutLast = 0;
let _mcDriftCheckLast = 0;
function _mcStartProgressTick() {
  _mcStopProgressTick();
  const tick = () => {
    _mcUpdateProgress();
    // Streaming-mode drift correction (#648): HTMLAudioElements don't stay
    // sample-accurate across siblings; nudge non-primary elements back in
    // line with primary every 500 ms if drift exceeds 150 ms.
    const now = _clockNowMs();
    if (_mcStreamMode && _mcIsPlaying && _mcAudioEls.length > 1
        && now - _mcDriftCheckLast >= 500) {
      _mcDriftCheckLast = now;
      const primaryT = _mcAudioEls[0].currentTime;
      for (let i = 1; i < _mcAudioEls.length; i++) {
        const el = _mcAudioEls[i];
        if (Math.abs(el.currentTime - primaryT) > 0.15) {
          try { el.currentTime = primaryT; } catch (e) { /* swallow */ }
        }
      }
    }
    // Throttled producer fanout: while mc is playing, push the current
    // playhead out as a source='mc' update so the map cursor, gauges, and
    // replay scrubber track along. 150 ms matches the existing audio
    // fanout cadence.
    if (_mcIsPlaying && now - _mcFanoutLast >= 150) {
      _mcFanoutLast = now;
      const anchor = _mcSessionStart();
      if (anchor) {
        setPosition(
          new Date(anchor.getTime() + _mcCurrentTime() * 1000),
          {source: 'mc'},
        );
      }
    }
    if (_mcIsPlaying) _mcRafHandle = requestAnimationFrame(tick);
  };
  _mcRafHandle = requestAnimationFrame(tick);
}

function _mcStopProgressTick() {
  if (_mcRafHandle !== null) {
    cancelAnimationFrame(_mcRafHandle);
    _mcRafHandle = null;
  }
}

async function loadMultiChannelAudio() {
  const body = document.getElementById('audio-body');
  body.innerHTML =
    '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
    '<button id="mc-playpause" class="btn-sm" onclick="_mcTogglePlay()" style="font-size:1.1rem;padding:4px 12px">▶</button>' +
    '<input id="mc-seek" type="range" min="0" max="1000" value="0" style="flex:1;min-width:160px" oninput="_mcSeekFromSlider(this.value)">' +
    '<span id="mc-time" style="font-size:.78rem;color:var(--text-secondary);min-width:80px;text-align:right">0:00 / 0:00</span>' +
    '</div>' +
    '<div style="display:flex;align-items:center;gap:10px;margin-top:6px;font-size:.78rem;color:var(--text-secondary)">' +
    '<label><input id="mc-sticky" type="checkbox" onchange="_mcToggleSticky(this.checked)"> Sticky isolation</label>' +
    '<button class="btn-sm" onclick="_mcSetIsolation(null)">All channels</button>' +
    ((_session && Array.isArray(_session.audio_siblings) && _session.audio_siblings.length > 1)
      ? _session.audio_siblings.map((s, i) =>
          '<button class="btn-sm" onclick="_mcSetIsolation(' + i + ')">'
          + esc(s.position_name || ('R' + (i + 1))) + '</button>'
        ).join('')
      : '') +
    '<span id="mc-isolation-indicator">mixed</span>' +
    '</div>' +
    '<div id="mc-status" style="font-size:.78rem;color:var(--text-secondary);margin-top:4px">Loading audio…</div>';

  // Render the debrief player up front so a stuck / failed race-audio decode
  // never hides a perfectly good debrief (#648).
  _renderDebriefPlayer();

  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) {
      document.getElementById('mc-status').textContent =
        'Web Audio API not supported in this browser.';
      return;
    }
    _mcCtx = new Ctx();

    const siblings = _session && _session.audio_siblings;

    // Streaming path (#648): for sessions at/above the server-configured
    // threshold (AUDIO_STREAM_THRESHOLD_MINUTES), play each sibling through
    // an HTMLAudioElement + MediaElementAudioSourceNode instead of
    // decodeAudioData. Memory stays flat; seeking is byte-range-driven.
    if (_session && _session.use_streaming_audio && siblings && siblings.length > 1) {
      _mcStreamMode = true;
      _mcSiblings = true;
      _mcAudioEls = siblings.map(s => {
        const el = new Audio();
        el.preload = 'auto';
        el.src = s.stream_url;
        return el;
      });
      _mcMerger = _mcCtx.createChannelMerger(1);
      _mcGains = _mcAudioEls.map(() => {
        const g = _mcCtx.createGain();
        g.gain.value = 1;
        g.connect(_mcMerger, 0, 0);
        return g;
      });
      _mcMerger.connect(_mcCtx.destination);
      _mcMediaSources = _mcAudioEls.map((el, i) => {
        const src = _mcCtx.createMediaElementSource(el);
        src.connect(_mcGains[i]);
        return src;
      });
      // Wait for metadata so we know the longest duration for the seek bar.
      const durations = await Promise.all(_mcAudioEls.map(el => new Promise((resolve, reject) => {
        if (el.readyState >= 1 && isFinite(el.duration)) return resolve(el.duration);
        el.addEventListener('loadedmetadata', () => resolve(el.duration), {once: true});
        el.addEventListener('error', () => reject(new Error('metadata load failed')), {once: true});
      })));
      const longest = durations.reduce((a, b) => (b > a ? b : a), 0);
      _mcBuffer = {duration: longest};
      // End detection on primary — mirrors the buffer-path ended handler.
      _mcAudioEls[0].addEventListener('ended', () => {
        if (!_mcIsPlaying) return;
        if (_mcCurrentTime() >= _mcBuffer.duration - 0.1) {
          _mcIsPlaying = false;
          _mcStartOffset = 0;
          _mcUpdateButtons();
          _mcStopProgressTick();
        }
      });
      const labels = siblings.map(s => s.position_name || `R${(Number(s.ordinal) || 0) + 1}`).join(', ');
      document.getElementById('mc-status').textContent =
        `${siblings.length} receivers (${labels}) — streaming mode — click a transcript segment to isolate that mic.`;
      _mcUpdateProgress();
      return;
    }

    // Sibling-card capture (#509): N mono WAVs, one BufferSource per
    // receiver, routed through per-source gains to a common mono merger.
    if (siblings && siblings.length > 1) {
      _mcSiblings = true;
      const decoded = await Promise.all(siblings.map(async s => {
        const r = await fetch(s.stream_url);
        if (!r.ok) throw new Error('audio fetch failed: ' + r.status);
        const ab = await r.arrayBuffer();
        return await _mcCtx.decodeAudioData(ab);
      }));
      _mcBuffers = decoded;
      // Primary buffer = the longest sibling, so the seek bar covers the
      // whole recording even if one card's stream ended a few ms early.
      let longest = decoded[0];
      for (const b of decoded) { if (b.duration > longest.duration) longest = b; }
      _mcBuffer = longest;
      _mcMerger = _mcCtx.createChannelMerger(1);
      _mcGains = decoded.map(() => {
        const g = _mcCtx.createGain();
        g.gain.value = 1;
        g.connect(_mcMerger, 0, 0);
        return g;
      });
      _mcMerger.connect(_mcCtx.destination);
      const labels = siblings.map(s => s.position_name || `R${(Number(s.ordinal) || 0) + 1}`).join(', ');
      document.getElementById('mc-status').textContent =
        `${siblings.length} receivers (${labels}) — click a transcript segment to isolate that mic.`;
      _mcUpdateProgress();
      return;
    }

    const r = await fetch('/api/audio/' + _session.audio_session_id + '/stream');
    if (!r.ok) throw new Error('audio fetch failed: ' + r.status);
    const buf = await r.arrayBuffer();
    _mcBuffer = await _mcCtx.decodeAudioData(buf);
    const channels = _mcBuffer.numberOfChannels;
    _mcSplitter = _mcCtx.createChannelSplitter(channels);
    _mcMerger = _mcCtx.createChannelMerger(1);
    _mcGains = [];
    for (let i = 0; i < channels; i++) {
      const g = _mcCtx.createGain();
      g.gain.value = 1;
      _mcSplitter.connect(g, i);
      // Mix every channel down to mono so isolation works regardless of
      // device output count. Per CLAUDE.md the lavalier device exposes one
      // mic per channel — mono playback is what the listener wants.
      g.connect(_mcMerger, 0, 0);
      _mcGains.push(g);
    }
    _mcMerger.connect(_mcCtx.destination);
    document.getElementById('mc-status').textContent =
      `${channels}-channel session — click a transcript segment to isolate that channel.`;
    _mcUpdateProgress();
  } catch (e) {
    console.error('multi-channel audio load failed', e);
    document.getElementById('mc-status').textContent = 'Error: ' + e.message;
  }
  // Register mc as a clock consumer so scrubs/seeks from other surfaces move
  // the mc seek bar and start offset. Skipped while playing (avoid fighting
  // natural playback) and while the user is actively dragging the slider.
  registerSurface('mc', function(utc) {
    // Moving _mcStartOffset on a paused mc source doesn't start playback,
    // so we can safely follow updates from any producer. The _mcIsPlaying
    // guard below still prevents us from interrupting active playback.
    if (!_mcBuffer) return;
    const anchor = _mcSessionStart();
    if (!anchor) return;
    const seconds = (utc.getTime() - anchor.getTime()) / 1000;
    if (seconds < 0 || seconds > _mcBuffer.duration) return;
    if (_mcIsPlaying) return;
    const seek = document.getElementById('mc-seek');
    if (seek && document.activeElement === seek) return;
    const delta = Math.abs((_mcStartOffset || 0) - seconds);
    if (delta < 0.15) return;
    _mcStartOffset = seconds;
    _mcUpdateProgress();
  });
}

function _mcTogglePlay() {
  if (!_mcCtx || !_mcBuffer) return;
  if (_mcIsPlaying) _mcPause();
  else _mcPlay();
}

function _mcSeekFromSlider(val) {
  if (!_mcBuffer) return;
  const seconds = (Number(val) / 1000) * _mcBuffer.duration;
  _mcSeek(seconds);
  // Producer: fan out to every other surface (map cursor, video, gauges)
  // so dragging the Web Audio scrubber keeps everything in sync. Skipped
  // when we can't derive a session-wide UTC anchor.
  const anchor = _mcSessionStart();
  if (anchor) setPosition(new Date(anchor.getTime() + seconds * 1000), {source: 'mc'});
}

// Resolve the session-wide UTC anchor for the multi-channel buffer. The
// buffer is keyed to _session.audio_start_utc (same reference the transcript
// highlighter uses), normalized to UTC regardless of how the backend wrote it.
function _mcSessionStart() {
  if (!_session || !_session.audio_start_utc) return null;
  const raw = _session.audio_start_utc;
  const iso = raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

function _mcToggleSticky(on) {
  _mcSticky = !!on;
  if (!_mcSticky) {
    _mcClearTimer();
    _mcSetIsolation(null);
  }
}

// Called by transcript-segment click handlers (see playTranscriptSegment).
// channelIndex may be undefined for single-channel sessions; in that case
// no isolation is applied.
function _mcOnSegmentClick(channelIndex, startSec, endSec) {
  if (!_mcCtx || !_mcBuffer) return;
  _mcSeek(startSec);
  if (!_mcIsPlaying) _mcPlay();
  if (channelIndex === undefined || channelIndex === null) return;
  _mcClearTimer();
  _mcSetIsolation(channelIndex);
  if (_mcSticky) return;  // sticky mode keeps isolation until released
  const durationMs = Math.max(0, (endSec - startSec) * 1000);
  _mcIsolationTimer = setTimeout(() => {
    _mcSetIsolation(null);
    _mcIsolationTimer = null;
  }, durationMs);
}

// Direct transcript highlighter — follows audio.currentTime regardless of
// playback-clock state. Drives the same active-segment styling and scroll
// behavior as the clock-driven path.
function _highlightTranscriptFromAudio(ev) {
  const el = ev && ev.target ? ev.target : document.getElementById('session-audio');
  if (!el || !_transcriptBlocks.length) return;
  const t = el.currentTime;
  const segs = document.querySelectorAll('.transcript-seg');
  let activeIdx = -1;
  for (let i = 0; i < _transcriptBlocks.length; i++) {
    const b = _transcriptBlocks[i];
    if (t >= b.start && t <= b.end) { activeIdx = i; break; }
  }
  for (let i = 0; i < segs.length; i++) {
    const segEl = segs[i];
    if (!segEl) continue;
    if (i === activeIdx) {
      segEl.style.background = 'var(--bg-hover, rgba(255,255,255,0.08))';
      const container = document.getElementById('transcript-segments');
      if (container) _scrollTranscriptSegmentIntoView(container, segEl);
    } else {
      segEl.style.background = '';
    }
  }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Co-op Sharing
// ---------------------------------------------------------------------------

async function loadSharing() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/sharing');
  if (!r.ok) return;
  const data = await r.json();
  if (!data.co_ops || !data.co_ops.length) return;

  const card = document.getElementById('sharing-card');
  card.style.display = '';
  renderSharing(data);
}

function renderSharing(data) {
  const body = document.getElementById('sharing-body');
  let html = '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">';
  for (const c of data.co_ops) {
    if (c.shared) {
      html += '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--success);color:var(--success)"'
        + ' onclick="unshareSession(\'' + esc(c.co_op_id) + '\')">'
        + esc(c.co_op_name) + ' &#10003;</button>';
    } else {
      html += '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--border);color:var(--text-primary)"'
        + ' onclick="shareSession(\'' + esc(c.co_op_id) + '\')">'
        + 'Share with ' + esc(c.co_op_name) + '</button>';
    }
  }
  html += '</div>';

  // Show sharing details
  if (data.sharing && data.sharing.length) {
    html += '<div style="margin-top:8px;font-size:.78rem;color:var(--text-secondary)">';
    for (const s of data.sharing) {
      html += '<div>Shared with <strong style="color:var(--text-primary)">' + esc(s.co_op_name || s.co_op_id) + '</strong>';
      if (s.embargo_until) html += ' (embargo until ' + esc(s.embargo_until).slice(0, 10) + ')';
      html += ' &mdash; ' + esc(s.shared_at).slice(0, 19) + '</div>';
    }
    html += '</div>';
  }
  body.innerHTML = html;
}

async function shareSession(coopId) {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/share', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({co_op_id: coopId})
  });
  if (r.ok) { loadSharing(); } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Failed to share');
  }
}

async function unshareSession(coopId) {
  if (!confirm('Stop sharing this session with this co-op?')) return;
  const r = await fetch('/api/sessions/' + SESSION_ID + '/share/' + encodeURIComponent(coopId), {
    method: 'DELETE'
  });
  if (r.ok) { loadSharing(); } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Failed to unshare');
  }
}

// ---------------------------------------------------------------------------
// Session Match (#281)
// ---------------------------------------------------------------------------

async function loadMatch() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match');
  if (!r.ok) return;
  const data = await r.json();

  const card = document.getElementById('match-card');
  const body = document.getElementById('match-body');
  const role = document.getElementById('app-config').dataset.userRole;
  const isAdmin = role === 'admin';

  if (data.status === 'unmatched') {
    if (!isAdmin) return; // viewers don't see scan button
    card.style.display = '';
    body.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">No match found.</div>'
      + '<button class="btn-export" style="margin-top:6px" onclick="scanForMatches()">Scan for matches</button>';
    return;
  }

  card.style.display = '';
  let html = '<div style="font-size:.82rem">';

  // Peer info line (shared between candidate and confirmed states)
  const peerLine = data.peer_boat_name
    ? '<div style="color:var(--text-secondary);margin-bottom:6px">Matched boat: <strong style="color:var(--text-primary)">'
      + esc(data.peer_boat_name) + '</strong>'
      + (data.peer_session_name ? ' — ' + esc(data.peer_session_name) : '')
      + '</div>'
    : '';

  if (data.status === 'candidate') {
    html += '<div style="color:var(--warning);margin-bottom:6px">Pending match — awaiting confirmation</div>';
    html += peerLine;
    if (isAdmin) {
      html += '<div style="display:flex;gap:6px">'
        + '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--success);color:var(--success)" onclick="confirmMatch()">Confirm</button>'
        + '<button class="btn-export" style="background:var(--bg-secondary);border:1px solid var(--danger);color:var(--danger)" onclick="rejectMatch()">Reject</button>'
        + '</div>';
    }
  } else if (data.status === 'confirmed') {
    html += '<div style="color:var(--success);margin-bottom:6px">Matched with co-op boats</div>';
    html += peerLine;
    if (data.shared_name) {
      html += '<div style="margin-bottom:4px">Shared name: <strong style="color:var(--text-primary)">' + esc(data.shared_name) + '</strong></div>';
    }
    if (isAdmin) {
      html += '<div style="margin-top:6px">'
        + '<input type="text" id="match-name-input" value="' + esc(data.shared_name || '') + '"'
        + ' placeholder="Set shared name" style="background:var(--bg-input);border:1px solid ' + cssVar('--border') + ';border-radius:4px;color:var(--text-primary);padding:4px 8px;font-size:.8rem;width:60%">'
        + ' <button class="btn-export" onclick="setMatchName()">Save</button>'
        + '</div>';
    }
  }

  html += '</div>';
  body.innerHTML = html;
}

async function scanForMatches() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/scan', {method: 'POST'});
  if (r.ok) {
    const d = await r.json();
    const n = d.proposals ? d.proposals.length : 0;
    alert(n + ' match proposal(s) sent to peers.');
    loadMatch();
    renderHeader();
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Scan failed');
  }
}

async function confirmMatch() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/confirm', {method: 'POST'});
  if (r.ok) {
    loadMatch();
    // Reload detail to update header badges
    const dr = await fetch('/api/sessions/' + SESSION_ID + '/detail');
    if (dr.ok) { _session = await dr.json(); renderHeader(); }
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Confirm failed');
  }
}

async function rejectMatch() {
  if (!confirm('Reject this session match?')) return;
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/reject', {method: 'POST'});
  if (r.ok) {
    loadMatch();
    const dr = await fetch('/api/sessions/' + SESSION_ID + '/detail');
    if (dr.ok) { _session = await dr.json(); renderHeader(); }
  } else {
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Reject failed');
  }
}

async function setMatchName() {
  const input = document.getElementById('match-name-input');
  const btn = document.querySelector('#match-body button[onclick="setMatchName()"]');
  const name = input ? input.value.trim() : '';
  if (!name) { alert('Enter a name'); return; }
  if (btn) { btn.textContent = 'Saving...'; btn.disabled = true; }
  const r = await fetch('/api/sessions/' + SESSION_ID + '/match/name', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: name})
  });
  if (r.ok) {
    if (btn) { btn.textContent = 'Saved ✓'; setTimeout(() => { btn.textContent = 'Save'; btn.disabled = false; }, 1500); }
    loadMatch();
    const dr = await fetch('/api/sessions/' + SESSION_ID + '/detail');
    if (dr.ok) { _session = await dr.json(); renderHeader(); }
  } else {
    if (btn) { btn.textContent = 'Save'; btn.disabled = false; }
    const d = await r.json().catch(() => ({}));
    alert(d.detail || 'Failed to set name');
  }
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

function renderExports() {
  const s = _session;
  if (s.type === 'debrief' || !s.end_utc) return;

  const card = document.getElementById('exports-card');
  card.style.display = '';
  const body = document.getElementById('exports-body');

  const from = new Date(s.start_utc).getTime();
  const to = new Date(s.end_utc).getTime();
  let html = '';
  html += '<a class="btn-export" href="/api/races/' + s.id + '/export.csv">&#8595; CSV</a>';
  html += '<a class="btn-export" href="/api/races/' + s.id + '/export.gpx">&#8595; GPX</a>';
  html += '<a class="btn-export btn-grafana" href="' + GRAFANA_BASE + '/d/' + GRAFANA_UID + '/sailing-data?from=' + from + '&to=' + to + '&orgId=1&refresh=" target="_blank">&#128202; Grafana</a>';
  if (s.has_audio && s.audio_session_id) {
    html += '<a class="btn-export" href="/api/audio/' + s.audio_session_id + '/download">&#8595; WAV</a>';
  }
  body.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Analysis — staleness indicator + A/B comparison (#412)
// ---------------------------------------------------------------------------

let _analysisResult = null;

async function loadAnalysis() {
  try {
    const r = await fetch('/api/analysis/results/' + SESSION_ID);
    if (!r.ok) return;
    _analysisResult = await r.json();

    const card = document.getElementById('analysis-card');
    card.style.display = '';
    renderAnalysisResult(_analysisResult, document.getElementById('analysis-body'));
  } catch (e) { /* non-fatal */ }
}

function renderAnalysisResult(result, container) {
  let html = '';
  // Staleness banner
  if (result.stale_reason) {
    html += '<div class="stale-banner">'
      + '<span>Analysis outdated: ' + esc(result.stale_reason.replace(/_/g, ' ')) + '</span>'
      + '<button onclick="rerunAnalysis()">Re-run analysis</button>'
      + '</div>';
  }
  // Label
  const label = result.plugin_name
    ? esc(result.plugin_name) + (result.plugin_version ? ' v' + esc(result.plugin_version) : '')
    : '';
  if (label) {
    html += '<div style="font-size:.75rem;color:var(--text-secondary);margin-bottom:6px">' + label + '</div>';
  }
  // Metrics
  const metrics = result.metrics || [];
  if (metrics.length) {
    for (const m of metrics) {
      html += '<div class="metric">'
        + '<span>' + esc(m.label || m.name) + '</span>'
        + '<span><strong>' + esc(String(m.value)) + '</strong>'
        + (m.unit ? ' <span style="color:var(--text-secondary)">' + esc(m.unit) + '</span>' : '')
        + '</span></div>';
    }
  }
  // Insights
  const insights = result.insights || [];
  if (insights.length) {
    for (const i of insights) {
      const cls = i.severity === 'critical' ? 'insight-critical'
        : i.severity === 'warning' ? 'insight-warning' : '';
      html += '<div class="insight ' + cls + '">' + esc(i.message) + '</div>';
    }
  }
  if (!metrics.length && !insights.length) {
    html += '<div style="font-size:.82rem;color:var(--text-secondary)">No analysis data available</div>';
  }
  container.innerHTML = html;
}

async function rerunAnalysis() {
  const body = document.getElementById('analysis-body');
  body.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">Running analysis\u2026</div>';
  try {
    const r = await fetch('/api/analysis/run/' + SESSION_ID, {method: 'POST'});
    if (!r.ok) { body.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Re-run failed</div>'; return; }
    _analysisResult = await r.json();
    renderAnalysisResult(_analysisResult, body);
  } catch (e) {
    body.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Re-run failed: ' + esc(e.message) + '</div>';
  }
}

// A/B Comparison
let _abMode = false;

async function showAbCompare() {
  const body = document.getElementById('analysis-body');
  if (_abMode) { _abMode = false; await loadAnalysis(); return; }
  _abMode = true;

  // Fetch available models
  let plugins;
  try {
    const r = await fetch('/api/analysis/models');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    plugins = await r.json();
  } catch (e) { body.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Failed to load models</div>'; return; }

  if (plugins.length < 2) {
    body.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">At least 2 plugins are needed for A/B comparison</div>';
    return;
  }

  let html = '<div class="ab-selector">';
  for (const p of plugins) {
    html += '<label><input type="checkbox" value="' + esc(p.name) + '" checked> ' + esc(p.display_name) + '</label>';
  }
  html += '<button onclick="runAbCompare()" style="background:var(--accent-strong);color:var(--bg-primary);border:none;border-radius:4px;padding:5px 12px;font-size:.78rem;cursor:pointer">Compare</button>';
  html += '</div><div id="ab-panels"></div>';
  body.innerHTML = html;
}

async function runAbCompare() {
  const checks = document.querySelectorAll('#analysis-body .ab-selector input[type=checkbox]:checked');
  const models = Array.from(checks).map(c => c.value);
  if (models.length < 2) { alert('Select at least 2 models'); return; }
  if (models.length > 5) { alert('Select at most 5 models'); return; }

  const panels = document.getElementById('ab-panels');
  panels.innerHTML = '<div style="font-size:.82rem;color:var(--text-secondary)">Running comparison\u2026</div>';

  try {
    const r = await fetch('/api/analysis/ab-compare/' + SESSION_ID, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({models: models})
    });
    if (!r.ok) { const d = await r.json().catch(() => ({})); panels.innerHTML = '<div style="color:var(--danger);font-size:.82rem">' + esc(d.detail || 'Comparison failed') + '</div>'; return; }
    const data = await r.json();

    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">';
    for (const p of data.panels) {
      html += '<div class="ab-panel">';
      if (p.error) {
        html += '<h3>' + esc(p.plugin_name) + '</h3>';
        html += '<div style="color:var(--danger);font-size:.82rem">' + esc(p.error) + '</div>';
      } else {
        html += '<h3>' + esc(p.label || p.plugin_name) + '</h3>';
        if (p.stale_reason) {
          html += '<div style="font-size:.72rem;color:var(--warning);margin-bottom:4px">Stale: ' + esc(p.stale_reason.replace(/_/g, ' ')) + '</div>';
        }
        const metrics = p.metrics || [];
        for (const m of metrics) {
          html += '<div class="metric"><span>' + esc(m.label || m.name) + '</span>'
            + '<span><strong>' + esc(String(m.value)) + '</strong>'
            + (m.unit ? ' ' + esc(m.unit) : '') + '</span></div>';
        }
        const insights = p.insights || [];
        for (const i of insights) {
          const cls = i.severity === 'critical' ? 'insight-critical'
            : i.severity === 'warning' ? 'insight-warning' : '';
          html += '<div class="insight ' + cls + '">' + esc(i.message) + '</div>';
        }
      }
      html += '</div>';
    }
    html += '</div>';
    panels.innerHTML = html;
  } catch (e) {
    panels.innerHTML = '<div style="color:var(--danger);font-size:.82rem">Error: ' + esc(e.message) + '</div>';
  }
}

// ---------------------------------------------------------------------------
// Polar Performance
// ---------------------------------------------------------------------------

let _polarData = null;       // full-session cells from /api/sessions/:id/polar
let _polarDataRaw = null;    // untouched API response (for reset)

async function loadPolar() {
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/polar');
    if (!r.ok) return;
    const data = await r.json();
    if (!data.cells || !data.cells.length) return;

    _polarDataRaw = data;
    _polarData = data;
    document.getElementById('polar-card').style.display = '';
    renderPolarDiagram();
    renderPolarHeatmap();

    // Summary line
    const above = data.cells.filter(c => c.delta != null && c.delta > 0).length;
    const below = data.cells.filter(c => c.delta != null && c.delta < 0).length;
    const noBaseline = data.cells.filter(c => c.delta == null).length;
    const withDelta = data.cells.filter(c => c.delta != null);
    let avgDelta = 0;
    if (withDelta.length) {
      const totalWeight = withDelta.reduce((s, c) => s + c.samples, 0);
      avgDelta = withDelta.reduce((s, c) => s + c.delta * c.samples, 0) / totalWeight;
    }
    const sign = avgDelta >= 0 ? '+' : '';
    const summary = document.getElementById('polar-summary');
    // POST /api/polar/rebuild requires admin — only show the affordance to
    // users who can use it (#597).
    const rebuildBtn = _sessionUserRole === 'admin'
      ? ' &middot; <button onclick="rebuildPolarBaseline()" style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:.78rem;text-decoration:underline;padding:0">Rebuild baseline</button>'
      : '';
    summary.innerHTML =
      sign + avgDelta.toFixed(2) + ' kt weighted avg vs baseline &middot; '
      + above + ' bins above, ' + below + ' below'
      + (noBaseline ? ' &middot; ' + noBaseline + ' bins no baseline' : '')
      + ' &middot; ' + data.session_sample_count + ' samples'
      + rebuildBtn
      + _polarWindowCaption(data);
  } catch (e) { /* non-fatal */ }
}

// Small caption showing the racing window this polar was computed over, so the
// trim (prestart excluded, post-finish auto-detected) is auditable (#812).
function _polarWindowCaption(data) {
  if (!data.window_start || !data.window_end) return '';
  const gun = data.gun_source === 'vakaros' ? 'Vakaros gun' : 'session start';
  const fin = data.finish_source === 'heuristic' ? 'auto-detected finish' : 'session end';
  return '<div style="font-size:.7rem;color:var(--text-secondary);margin-top:2px">'
    + 'Racing window: ' + fmtTime(data.window_start) + '–' + fmtTime(data.window_end)
    + ' &middot; ' + gun + ' → ' + fin
    + '</div>';
}

async function rebuildPolarBaseline() {
  const summary = document.getElementById('polar-summary');
  summary.textContent = 'Rebuilding baseline\u2026';
  try {
    const r = await fetch('/api/polar/rebuild', {method: 'POST'});
    if (!r.ok) { summary.textContent = 'Rebuild failed: ' + r.status; return; }
    const d = await r.json();
    summary.textContent = 'Baseline rebuilt: ' + d.bins + ' bins. Reloading\u2026';
    await loadPolar();
  } catch (e) { summary.textContent = 'Rebuild failed'; }
}

function setPolarView(view) {
  document.getElementById('polar-diagram-view').style.display = view === 'diagram' ? '' : 'none';
  document.getElementById('polar-heatmap-view').style.display = view === 'heatmap' ? '' : 'none';
  document.getElementById('polar-tab-diagram').classList.toggle('active', view === 'diagram');
  document.getElementById('polar-tab-heatmap').classList.toggle('active', view === 'heatmap');
}

// --- Polar diagram (Canvas) ---

let _TWS_COLORS = null;

function _initTwsColors() {
  if (_TWS_COLORS) return;
  _TWS_COLORS = [
    [6, cssVar('--accent')],       [8, cssVar('--accent-strong')], [10, cssVar('--accent-strong')],
    [12, '#7c3aed'],               [14, cssVar('--warning')],      [16, cssVar('--danger')],
    [18, cssVar('--danger')],      [20, '#991b1b'],
  ];
}

function _twsColor(tws) {
  _initTwsColors();
  for (let i = _TWS_COLORS.length - 1; i >= 0; i--) {
    if (tws >= _TWS_COLORS[i][0]) return _TWS_COLORS[i][1];
  }
  return cssVar('--text-muted');
}

function renderPolarDiagram() {
  const canvas = document.getElementById('polar-canvas');
  if (!canvas || !_polarData) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  // Full-circle layout: starboard on the right (positive x), port mirrored
  // on the left (negative x) — lets the diagram show port/starboard asymmetry
  // alongside the heatmap split (#534).
  const cx = W / 2, cy = H / 2;
  const maxRadius = Math.min(cx, cy) - 30;

  ctx.clearRect(0, 0, W, H);

  let maxBsp = 0;
  for (const c of _polarData.cells) {
    if (c.session_mean != null) maxBsp = Math.max(maxBsp, c.session_mean);
    if (c.baseline_mean != null) maxBsp = Math.max(maxBsp, c.baseline_mean);
    if (c.baseline_p90 != null) maxBsp = Math.max(maxBsp, c.baseline_p90);
  }
  maxBsp = Math.ceil(maxBsp) + 1;
  if (maxBsp < 4) maxBsp = 4;
  const scale = maxRadius / maxBsp;

  const polarBorder = cssVar('--border');
  const polarTextSec = cssVar('--text-secondary');
  ctx.strokeStyle = polarBorder;
  ctx.lineWidth = 0.5;
  ctx.setLineDash([3, 3]);
  ctx.font = '11px monospace';
  ctx.fillStyle = polarTextSec;
  for (let bsp = 1; bsp <= maxBsp; bsp++) {
    const r = bsp * scale;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.fillText(bsp + '', cx + r + 3, cy + 4);
  }

  // Radial TWA lines on both sides.
  ctx.strokeStyle = polarBorder;
  for (const side of [1, -1]) {
    for (let deg = 0; deg <= 180; deg += 30) {
      const rad = deg * Math.PI / 180;
      const x2 = cx + side * maxBsp * scale * Math.sin(rad);
      const y2 = cy - maxBsp * scale * Math.cos(rad);
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(x2, y2);
      ctx.stroke();
      const lx = cx + side * (maxBsp * scale + 14) * Math.sin(rad);
      const ly = cy - (maxBsp * scale + 14) * Math.cos(rad);
      ctx.fillText(deg + '\u00b0', lx - 10, ly + 4);
    }
  }
  // Horizontal upwind/downwind divider at TWA = 90°.
  ctx.setLineDash([6, 4]);
  ctx.strokeStyle = polarTextSec;
  ctx.beginPath();
  ctx.moveTo(cx - maxBsp * scale, cy);
  ctx.lineTo(cx + maxBsp * scale, cy);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = polarTextSec;
  ctx.fillText('PORT', cx - maxBsp * scale - 4, cy - maxBsp * scale - 6);
  ctx.fillText('STBD', cx + maxBsp * scale - 26, cy - maxBsp * scale - 6);

  // Baseline curves — symmetric, so draw mirrored on both sides.
  const baselineByTws = {};
  for (const c of _polarData.cells) {
    if (c.baseline_mean == null) continue;
    if (!baselineByTws[c.tws]) baselineByTws[c.tws] = [];
    baselineByTws[c.tws].push(c);
  }
  // Dedup baseline points per TWS so mirrored draws don't double up.
  const drawnTws = [];
  for (const tws of Object.keys(baselineByTws).map(Number).sort((a, b) => a - b)) {
    const seen = {};
    const uniq = [];
    for (const p of baselineByTws[tws]) {
      if (seen[p.twa]) continue;
      seen[p.twa] = true;
      uniq.push(p);
    }
    const pts = uniq.sort((a, b) => a.twa - b.twa);
    if (pts.length < 2) continue;
    const color = _twsColor(tws);
    drawnTws.push({tws, color});

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.globalAlpha = 0.7;
    // Split upwind (<90°) from downwind (≥90°) so the baseline curve
    // doesn't cross the beam line — a boat never sails that shape.
    const upwind = pts.filter(p => p.twa < 90);
    const downwind = pts.filter(p => p.twa >= 90);
    for (const side of [1, -1]) {
      for (const leg of [upwind, downwind]) {
        if (leg.length < 2) continue;
        ctx.beginPath();
        for (let i = 0; i < leg.length; i++) {
          const rad = leg[i].twa * Math.PI / 180;
          const r = leg[i].baseline_mean * scale;
          const x = cx + side * r * Math.sin(rad);
          const y = cy - r * Math.cos(rad);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
  }

  // Session points, placed on the side that matches the cell's tack.
  _polarDotHitboxes = [];
  for (const c of _polarData.cells) {
    if (c.session_mean == null) continue;
    const side = c.tack === 'port' ? -1 : 1;
    const rad = c.twa * Math.PI / 180;
    const r = c.session_mean * scale;
    const x = cx + side * r * Math.sin(rad);
    const y = cy - r * Math.cos(rad);

    const dotColor = c.delta == null ? cssVar('--text-muted')
      : c.delta >= 0 ? cssVar('--success') : cssVar('--danger');
    const dotSize = Math.min(6, Math.max(3, Math.log2(c.samples + 1) * 1.5));

    ctx.beginPath();
    ctx.arc(x, y, dotSize, 0, 2 * Math.PI);
    ctx.fillStyle = dotColor;
    ctx.fill();
    ctx.strokeStyle = cssVar('--bg-primary');
    ctx.lineWidth = 1;
    ctx.stroke();

    _polarDotHitboxes.push({x: x, y: y, r: dotSize, cell: c});

    if (_polarSelectedCells.has(_polarCellKey(c))) {
      ctx.beginPath();
      ctx.arc(x, y, dotSize + 4, 0, 2 * Math.PI);
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2.5;
      ctx.stroke();
    }
  }
  _bindPolarCanvasHandler();

  const legend = document.getElementById('polar-legend');
  if (legend && drawnTws.length) {
    legend.innerHTML = 'Baseline curves: '
      + drawnTws.map(d =>
        '<span style="color:' + d.color + '">\u25cf ' + d.tws + ' kt</span>'
      ).join(' &nbsp; ')
      + ' &nbsp; Session (left = port, right = stbd): '
      + '<span style="color:var(--success)">\u25cf faster</span> '
      + '<span style="color:var(--danger)">\u25cf slower</span> '
      + '<span style="color:var(--text-muted)">\u25cf no baseline</span>';
  }
}

// --- Heatmap ---

function _deltaColor(delta) {
  if (delta == null) return cssVar('--bg-secondary');
  const clamped = Math.max(-1, Math.min(1, delta));
  if (clamped >= 0) {
    const t = clamped;
    return 'rgb(' + Math.round(20 + (1 - t) * 180) + ','
      + Math.round(80 + t * 160) + ','
      + Math.round(20 + (1 - t) * 180) + ')';
  } else {
    const t = -clamped;
    return 'rgb(' + Math.round(80 + t * 160) + ','
      + Math.round(20 + (1 - t) * 180) + ','
      + Math.round(20 + (1 - t) * 180) + ')';
  }
}

// Max TWS bin to always show in the heatmap (even when session has no data
// there) so the wind range up to 20 kt is always visible (#534).
const POLAR_HEATMAP_MAX_TWS = 20;

function _polarSubgridHtml(title, cells, twaBins, twsRange) {
  const cellMap = {};
  for (const c of cells) cellMap[c.tws + ',' + c.twa] = c;

  let html = '<div class="polar-subgrid"><h4 style="margin:.5rem 0 .25rem;font-size:.78rem;'
    + 'color:var(--text-secondary);font-weight:600">' + title + '</h4>';
  html += '<table style="border-collapse:collapse;font-size:.72rem;width:100%">';
  html += '<tr><th style="padding:2px 4px;color:var(--text-secondary);text-align:right;font-weight:normal">TWS\\TWA</th>';
  for (const twa of twaBins) {
    html += '<th style="padding:2px 4px;color:var(--text-secondary);font-weight:normal;min-width:36px">' + twa + '\u00b0</th>';
  }
  html += '</tr>';

  for (const tws of twsRange) {
    html += '<tr><td style="padding:2px 4px;color:var(--text-secondary);text-align:right;white-space:nowrap">' + tws + ' kt</td>';
    for (const twa of twaBins) {
      const c = cellMap[tws + ',' + twa];
      if (!c) {
        html += '<td style="padding:2px 4px;background:var(--bg-secondary);border:1px solid var(--bg-input)"></td>';
        continue;
      }
      const bg = _deltaColor(c.delta);
      const textColor = c.delta == null ? 'var(--text-secondary)' : 'var(--text-primary)';
      const text = c.delta != null
        ? (c.delta >= 0 ? '+' : '') + c.delta.toFixed(2)
        : c.session_mean != null ? c.session_mean.toFixed(1) : '';
      const ttl = 'TWS=' + tws + ' TWA=' + twa + '\u00b0 (' + c.point_of_sail + '/' + c.tack + ')'
        + '\nSession BSP: ' + (c.session_mean != null ? c.session_mean.toFixed(2) : 'n/a')
        + '\nBaseline: ' + (c.baseline_mean != null ? c.baseline_mean.toFixed(2) : 'n/a')
        + '\nP90: ' + (c.baseline_p90 != null ? c.baseline_p90.toFixed(2) : 'n/a')
        + '\nSamples: ' + c.samples
        + (c.delta != null ? '\n\nClick to highlight matching replay segments' : '');
      const onclick = 'highlightPolarCellSegments(' + tws + ",'" + c.point_of_sail + "','" + c.tack + "'," + twa + ')';
      const cursor = c.delta != null ? 'pointer' : 'default';
      html += '<td style="padding:2px 4px;background:' + bg + ';border:1px solid var(--bg-input);'
        + 'color:' + textColor + ';text-align:center;cursor:' + cursor + '" title="' + ttl + '"'
        + ' onclick="' + onclick + '">' + text + '</td>';
    }
    html += '</tr>';
  }
  html += '</table></div>';
  return html;
}

function renderPolarHeatmap() {
  const container = document.getElementById('polar-heatmap');
  if (!container || !_polarData) return;

  const data = _polarData;
  const split = { 'upwind-starboard': [], 'upwind-port': [], 'downwind-starboard': [], 'downwind-port': [] };
  let maxTws = 0;
  for (const c of data.cells) {
    const key = c.point_of_sail + '-' + c.tack;
    if (split[key]) split[key].push(c);
    if (c.tws > maxTws) maxTws = c.tws;
  }

  const topTws = Math.max(POLAR_HEATMAP_MAX_TWS, maxTws);
  const twsRange = [];
  for (let t = 0; t <= topTws; t++) twsRange.push(t);

  const upwindTwa = [];
  for (let a = 0; a < 90; a += 5) upwindTwa.push(a);
  const downwindTwa = [];
  for (let a = 90; a <= 180; a += 5) downwindTwa.push(a);

  let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">';
  html += _polarSubgridHtml('Upwind \u2014 Starboard tack', split['upwind-starboard'], upwindTwa, twsRange);
  html += _polarSubgridHtml('Upwind \u2014 Port tack', split['upwind-port'], upwindTwa, twsRange);
  html += _polarSubgridHtml('Downwind \u2014 Starboard tack', split['downwind-starboard'], downwindTwa, twsRange);
  html += _polarSubgridHtml('Downwind \u2014 Port tack', split['downwind-port'], downwindTwa, twsRange);
  html += '</div>';
  container.innerHTML = html;
}

// Highlight all graded replay segments that match a polar cell (#534).
// twaBin is optional: when omitted, matches all TWA bins in that TWS/pos/tack
// (heatmap row/col click); when provided, restricts to a single cell (diagram
// dot click).
function highlightPolarCellSegments(twsBin, pointOfSail, tack, twaBin) {
  const grades = (typeof _replayGrades !== 'undefined' && _replayGrades) ? _replayGrades : null;
  const st = document.getElementById('polar-highlight-status');
  if (!grades || !grades.length) {
    if (st) st.textContent = 'Replay not loaded \u2014 highlight unavailable';
    return;
  }
  const matching = grades.filter(g => {
    if (g.tws == null || g.tack == null || g.point_of_sail == null) return false;
    if (Math.floor(g.tws) !== twsBin) return false;
    if (g.point_of_sail !== pointOfSail) return false;
    if (g.tack !== tack) return false;
    if (twaBin != null) {
      if (g.twa == null) return false;
      if (Math.floor(g.twa / 5) * 5 !== twaBin) return false;
    }
    return true;
  });
  const label = twsBin + ' kt / ' + pointOfSail + ' / ' + tack
    + (twaBin != null ? ' / TWA ' + twaBin + '\u00b0' : '');
  if (st) {
    st.textContent = matching.length
      ? matching.length + ' segments highlighted (' + label + ')'
      : 'No matching segments for ' + label;
  }
  _setPolarHighlightSegments(matching);
}

// Polar filters (#534). All client-side — no API calls.
function _readPolarFilters() {
  const el = id => document.getElementById(id);
  return {
    pos: el('pf-pos') ? el('pf-pos').value : 'all',
    tack: el('pf-tack') ? el('pf-tack').value : 'all',
    twsMin: el('pf-tws-min') ? parseFloat(el('pf-tws-min').value) : 0,
    twsMax: el('pf-tws-max') ? parseFloat(el('pf-tws-max').value) : 40,
    delta: el('pf-delta') ? el('pf-delta').value : 'all',
    phase: el('pf-phase') ? el('pf-phase').value : 'all',
  };
}

function _rebuildCellsFromGrades(grades) {
  // Re-aggregate segments into (tws, twa, pos, tack) cells. Used when a
  // time-window filter (race phase) restricts which segments count.
  // Target BSP is constant per (tws, twa) since the baseline is symmetric,
  // so we just reuse the first non-null target seen in the bucket.
  const buckets = {};
  for (const g of grades) {
    if (g.tws == null || g.twa == null || g.tack == null || g.point_of_sail == null) continue;
    const tws = Math.floor(g.tws);
    const twa = Math.floor(g.twa / 5) * 5;
    const key = tws + '|' + twa + '|' + g.point_of_sail + '|' + g.tack;
    let b = buckets[key];
    if (!b) {
      b = {
        tws: tws, twa: twa, point_of_sail: g.point_of_sail, tack: g.tack,
        bspSum: 0, bspCount: 0, target: null,
      };
      buckets[key] = b;
    }
    if (g.bsp != null) { b.bspSum += g.bsp; b.bspCount += 1; }
    if (b.target == null && g.target != null) b.target = g.target;
  }
  const cells = [];
  for (const b of Object.values(buckets)) {
    const session_mean = b.bspCount ? b.bspSum / b.bspCount : null;
    const delta = (session_mean != null && b.target != null)
      ? Math.round((session_mean - b.target) * 10000) / 10000 : null;
    cells.push({
      tws: b.tws, twa: b.twa,
      point_of_sail: b.point_of_sail, tack: b.tack,
      baseline_mean: b.target, baseline_p90: null,
      session_mean: session_mean != null ? Math.round(session_mean * 10000) / 10000 : null,
      samples: b.bspCount,
      delta: delta,
    });
  }
  cells.sort((a, b) => a.tws - b.tws || a.twa - b.twa);
  return cells;
}

function _applyPolarFilters() {
  if (!_polarDataRaw) return;
  const f = _readPolarFilters();

  // 1. Decide the cell source: raw API cells or re-aggregate from replay grades
  //    when a race-phase filter is active.
  let cells;
  if (f.phase !== 'all'
      && typeof _replayGrades !== 'undefined' && _replayGrades && _replayGrades.length) {
    const gun = (typeof _raceGun !== 'undefined' && _raceGun) ? _raceGun : _replayStart;
    const finish = _replayEnd;
    const subset = _replayGrades.filter(g => {
      const t = g.t_start.getTime();
      if (f.phase === 'prestart') return t < gun.getTime();
      if (f.phase === 'racing') return t >= gun.getTime() && t <= finish.getTime();
      if (f.phase === 'postfinish') return t > finish.getTime();
      return true;
    });
    cells = _rebuildCellsFromGrades(subset);
  } else {
    cells = _polarDataRaw.cells.slice();
  }

  // 2. Apply client-side hides.
  cells = cells.filter(c => {
    if (f.pos !== 'all' && c.point_of_sail !== f.pos) return false;
    if (f.tack !== 'all' && c.tack !== f.tack) return false;
    if (c.tws < f.twsMin || c.tws > f.twsMax) return false;
    if (f.delta === 'faster' && !(c.delta != null && c.delta > 0)) return false;
    if (f.delta === 'slower' && !(c.delta != null && c.delta < 0)) return false;
    if (f.delta === 'big' && !(c.delta != null && Math.abs(c.delta) >= 0.5)) return false;
    return true;
  });

  _polarData = Object.assign({}, _polarDataRaw, {
    cells: cells,
    tws_bins: Array.from(new Set(cells.map(c => c.tws))).sort((a, b) => a - b),
    twa_bins: Array.from(new Set(cells.map(c => c.twa))).sort((a, b) => a - b),
    session_sample_count: cells.reduce((s, c) => s + (c.samples || 0), 0),
  });
  _polarSelectedCells.clear();
  renderPolarDiagram();
  renderPolarHeatmap();
}

function _onPolarFiltersChanged() { _applyPolarFilters(); }

function _resetPolarFilters() {
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set('pf-pos', 'all');
  set('pf-tack', 'all');
  set('pf-tws-min', '0');
  set('pf-tws-max', '40');
  set('pf-delta', 'all');
  set('pf-phase', 'all');
  _applyPolarFilters();
}

// Dot click-selection state for the polar diagram.
let _polarDotHitboxes = [];  // [{x, y, r, cell}]
let _polarSelectedCells = new Set(); // keys "tws|twa|pos|tack"
let _polarCanvasHandlerBound = false;

function _polarCellKey(c) {
  return c.tws + '|' + c.twa + '|' + c.point_of_sail + '|' + c.tack;
}

function _bindPolarCanvasHandler() {
  if (_polarCanvasHandlerBound) return;
  const canvas = document.getElementById('polar-canvas');
  if (!canvas) return;
  canvas.addEventListener('click', function(e) {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const x = (e.clientX - rect.left) * scaleX;
    const y = (e.clientY - rect.top) * scaleY;
    let best = null;
    let bestDist = Infinity;
    for (const hb of _polarDotHitboxes) {
      const dx = x - hb.x;
      const dy = y - hb.y;
      const d = Math.sqrt(dx * dx + dy * dy);
      if (d <= hb.r + 4 && d < bestDist) { best = hb; bestDist = d; }
    }
    if (!best) {
      if (!e.shiftKey) {
        _polarSelectedCells.clear();
        _applyPolarDotSelection();
      }
      return;
    }
    const key = _polarCellKey(best.cell);
    if (e.shiftKey) {
      if (_polarSelectedCells.has(key)) _polarSelectedCells.delete(key);
      else _polarSelectedCells.add(key);
    } else {
      _polarSelectedCells.clear();
      _polarSelectedCells.add(key);
    }
    _applyPolarDotSelection();
  });
  _polarCanvasHandlerBound = true;
}

function _applyPolarDotSelection() {
  renderPolarDiagram();  // redraws rings around selected dots
  const grades = (typeof _replayGrades !== 'undefined' && _replayGrades) ? _replayGrades : null;
  const st = document.getElementById('polar-highlight-status');
  if (!grades) return;
  if (!_polarSelectedCells.size) {
    if (st) st.textContent = '';
    _setPolarHighlightSegments([]);
    return;
  }
  const matching = grades.filter(g => {
    if (g.tws == null || g.tack == null || g.point_of_sail == null || g.twa == null) return false;
    const tws = Math.floor(g.tws);
    const twa = Math.floor(g.twa / 5) * 5;
    const key = tws + '|' + twa + '|' + g.point_of_sail + '|' + g.tack;
    return _polarSelectedCells.has(key);
  });
  if (st) {
    st.textContent = _polarSelectedCells.size + ' cell(s) selected \u2014 '
      + matching.length + ' segments highlighted';
  }
  _setPolarHighlightSegments(matching);
}

// Bright overlay polylines for segments matching a clicked polar cell.
// Drawn on top of the base track; cleared on next call.
let _polarHighlightLayers = [];

function _clearPolarHighlight() {
  for (const l of _polarHighlightLayers) {
    try { _map && _map.removeLayer(l); } catch (e) { /* ignore */ }
  }
  _polarHighlightLayers = [];
}

function _setPolarHighlightSegments(grades) {
  _clearPolarHighlight();
  if (!_map || !_trackData || !_trackData.timestamps || !_trackData.latLngs) return;
  if (!grades || !grades.length) return;
  const timestamps = _trackData.timestamps;
  const latLngs = _trackData.latLngs;
  for (const g of grades) {
    const tStart = g.t_start.getTime();
    const tEnd = g.t_end.getTime();
    const slice = [];
    for (let i = 0; i < timestamps.length; i++) {
      const t = timestamps[i].getTime();
      if (t >= tStart && t <= tEnd) slice.push(latLngs[i]);
    }
    if (slice.length >= 2) {
      const line = L.polyline(slice, {
        color: '#facc15', weight: 8, opacity: 0.95,
      }).addTo(_map);
      _polarHighlightLayers.push(line);
    }
  }
}

// ---------------------------------------------------------------------------
// Maneuvers
// ---------------------------------------------------------------------------

const _MANEUVER_COLORS = { tack: cssVar('--accent-strong'), gybe: cssVar('--warning'), rounding: cssVar('--success'), start: cssVar('--success') };
const _RANK_COLORS = {
  good: cssVar('--success'),
  bad: cssVar('--error'),
  avg: cssVar('--text-secondary'),
  consistent: cssVar('--text-secondary'),
};
let _maneuverSort = { key: 'ts', dir: 1 };  // ts | type | duration_sec | distance_loss_m | loss_kts | turn_angle_deg
// Active filter pills. Multi-select: combined with AND across dimensions
// (type, rank, time) and OR within a dimension. Empty set == "all".
let _maneuverFilter = new Set();
// Tag filter state: a Set of tag ids + an AND/OR mode for multi-select.
let _maneuverTagFilter = new Set();
let _maneuverTagMode = 'and'; // 'and' | 'or'
const _MANEUVER_TYPE_PILLS = ['tack', 'gybe', 'rounding'];
const _MANEUVER_RANK_PILLS = ['good', 'bad'];
const _MANEUVER_DIR_PILLS = ['P\u2192S', 'S\u2192P'];
const _MANEUVER_TIME_PILLS = ['post-start'];
let _maneuverOverlay = false; // toggle for all-tacks-overlaid diagram
let _maneuverShowSK = true; // toggle SK-derived tracks in the overlay
let _maneuverShowVakaros = false; // toggle Vakaros tracks in the overlay
let _maneuverSelected = new Set(); // ids of maneuvers selected for overlay
let _maneuverTickInterval = 0; // seconds between track tick marks; 0 = off
let _maneuverLineChartsOn = false; // toggle for inline BSP/heading-rate/TWA charts (#619)
let _maneuverLineChartsCtrl = null; // controller returned by mvInitLineCharts
let _maneuverLineChartsFetchKey = ''; // signature of ids last fetched

function _parseUtc(iso) {
  if (!iso) return null;
  const s = (iso.endsWith('Z') || iso.includes('+')) ? iso : iso + 'Z';
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function _fmtElapsed(iso) {
  const t = _parseUtc(iso);
  if (!t || !_session || !_session.start_utc) return '\u2014';
  const start = _parseUtc(_session.start_utc);
  if (!start) return '\u2014';
  const secs = Math.max(0, Math.round((t.getTime() - start.getTime()) / 1000));
  const mm = Math.floor(secs / 60);
  const ss = secs % 60;
  return '+' + String(mm).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
}

async function loadManeuvers() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/maneuvers');
  if (!r.ok) return;
  _maneuvers = await r.json();
  // Default: select all maneuvers for overlay when a new list arrives.
  _maneuverSelected = new Set(_maneuvers.map((m, i) => m.id != null ? m.id : i));
  // Re-inject the Vakaros race start if the overlay already stashed one.
  _injectVakarosStartIntoManeuvers();
  renderManeuverCard();
  if (_map && _maneuvers.length) _addManeuverMarkers();
  // Roundings are now loaded — refresh laylines so they anchor on the
  // mark positions from this fetch (handles re-detection too).
  if (typeof _drawAllLaylines === 'function') _drawAllLaylines();
  // Refresh the course-distance gauge using the new rounding set.
  _populateRoundings();
  _recomputeCourseDistance();
}

function _manKey(m, idx) {
  return m.id != null ? m.id : idx;
}

function toggleManeuverSelected(keyStr) {
  // keyStr may be a number-as-string; normalise
  const key = isNaN(Number(keyStr)) ? keyStr : Number(keyStr);
  if (_maneuverSelected.has(key)) _maneuverSelected.delete(key);
  else _maneuverSelected.add(key);
  if (_maneuverOverlay) renderManeuverCard();
}

function setManeuverSelectAll(mode) {
  if (mode === 'all') {
    _maneuverSelected = new Set(_maneuvers.map((m, i) => _manKey(m, i)));
  } else if (mode === 'none') {
    _maneuverSelected = new Set();
  } else if (mode === 'filtered') {
    _maneuverSelected = new Set(_maneuverRows().map((m) => _manKey(m, _maneuvers.indexOf(m))));
  }
  renderManeuverCard();
}

function openManeuverCompare() {
  // Use the filtered+sorted rows (respects active filter pills) intersected
  // with the overlay selection, so the user sees exactly the subset they
  // expect from the current filter state.
  const ids = _maneuverRows()
    .filter(m => _maneuverSelected.has(_manKey(m, _maneuvers.indexOf(m))) && typeof m.id === 'number')
    .map(m => m.id);
  if (!ids.length) { alert('Select maneuvers to compare.'); return; }
  window.open('/session/' + SESSION_ID + '/compare?ids=' + ids.join(','), '_blank');
}

function openManeuverOverlay() {
  // Overlay (#619) — defaults to every tack/gybe in the session the first
  // time it opens, then narrows via the same filter+selection state that
  // drives Compare. Roundings have no head-to-wind alignment, so they're
  // excluded from the URL regardless of selection.
  const selectedIds = _maneuverRows()
    .filter(m => _maneuverSelected.has(_manKey(m, _maneuvers.indexOf(m)))
                  && typeof m.id === 'number'
                  && (m.type === 'tack' || m.type === 'gybe'))
    .map(m => SESSION_ID + ':' + m.id);
  const ids = selectedIds.length
    ? selectedIds
    // Nothing selected (or only roundings selected) → fall back to every
    // alignable maneuver in the session. Matches the issue's "default
    // selection = every tack/gybe in that session" AC.
    : _maneuvers
        .filter(m => typeof m.id === 'number' && (m.type === 'tack' || m.type === 'gybe'))
        .map(m => SESSION_ID + ':' + m.id);
  if (!ids.length) {
    alert('No tacks or gybes in this session — overlay requires a head-to-wind alignment point.');
    return;
  }
  window.open(
    '/maneuvers/overlay?session=' + SESSION_ID + '&ids=' + ids.join(','),
    '_blank'
  );
}

function _maneuverRows() {
  const items = _maneuvers.filter(_matchesManeuverFilter);
  const key = _maneuverSort.key, dir = _maneuverSort.dir;
  items.sort((a, b) => {
    let av = a[key], bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (key === 'ts') { av = new Date(av).getTime(); bv = new Date(bv).getTime(); }
    if (key === 'type') return dir * String(av).localeCompare(String(bv));
    return dir * (av - bv);
  });
  return items;
}

function setManeuverSort(key) {
  if (_maneuverSort.key === key) _maneuverSort.dir *= -1;
  else { _maneuverSort.key = key; _maneuverSort.dir = key === 'ts' ? 1 : -1; }
  renderManeuverCard();
}

function setManeuverFilter(f) {
  if (f === 'all') {
    _maneuverFilter.clear();
  } else if (_maneuverFilter.has(f)) {
    _maneuverFilter.delete(f);
  } else {
    // Direction pills are mutually exclusive
    if (_MANEUVER_DIR_PILLS.includes(f)) {
      _MANEUVER_DIR_PILLS.forEach(d => _maneuverFilter.delete(d));
    }
    _maneuverFilter.add(f);
  }
  renderManeuverCard();
}

function toggleManeuverOverlay() {
  _maneuverOverlay = !_maneuverOverlay;
  renderManeuverCard();
}

function toggleManeuverLineCharts() {
  _maneuverLineChartsOn = !_maneuverLineChartsOn;
  if (!_maneuverLineChartsOn && _maneuverLineChartsCtrl) {
    _maneuverLineChartsCtrl.destroy();
    _maneuverLineChartsCtrl = null;
    _maneuverLineChartsFetchKey = '';
  }
  renderManeuverCard();
}

async function _paintSessionLineCharts() {
  // Uses the same /api/maneuvers/overlay endpoint as /maneuvers/overlay
  // so single-session and cross-session line charts share one data path.
  const host = document.getElementById('mv-line-charts-host');
  if (!host) return;
  const ids = _maneuvers
    .filter(m => typeof m.id === 'number' && (m.type === 'tack' || m.type === 'gybe'))
    .map(m => SESSION_ID + ':' + m.id);
  if (!ids.length) {
    host.innerHTML = '<div style="color:var(--text-secondary);font-size:.72rem;padding:6px">'
      + 'No tacks or gybes in this session — line charts require a head-to-wind alignment point.'
      + '</div>';
    return;
  }
  const key = ids.join(',');
  if (key === _maneuverLineChartsFetchKey && _maneuverLineChartsCtrl) {
    // Same selection, same controller — no-op. Hover sync still works
    // because the controller reads from its own state.
    return;
  }
  try {
    const r = await fetch('/api/maneuvers/overlay?ids=' + encodeURIComponent(key));
    if (!r.ok) {
      host.innerHTML = '<div style="color:var(--text-secondary);font-size:.72rem;padding:6px">'
        + 'Failed to load line charts (HTTP ' + r.status + ')</div>';
      return;
    }
    const payload = await r.json();
    if (!payload.maneuvers || !payload.maneuvers.length) {
      host.innerHTML = '<div style="color:var(--text-secondary);font-size:.72rem;padding:6px">'
        + 'No maneuvers with a head-to-wind timestamp yet — try again after backfill completes.'
        + '</div>';
      return;
    }
    if (_maneuverLineChartsCtrl) _maneuverLineChartsCtrl.destroy();
    _maneuverLineChartsCtrl = window.mvInitLineCharts(host, payload, {
      mode: 'auto',
      panelHeight: 130,
    });
    _maneuverLineChartsFetchKey = key;
  } catch (e) {
    host.innerHTML = '<div style="color:var(--text-secondary);font-size:.72rem;padding:6px">'
      + 'Failed to load line charts: ' + e + '</div>';
  }
}

function toggleManeuverShowVakaros() {
  _maneuverShowVakaros = !_maneuverShowVakaros;
  if (!_maneuverShowSK && !_maneuverShowVakaros) _maneuverShowSK = true;
  renderManeuverCard();
}

function toggleManeuverShowSK() {
  _maneuverShowSK = !_maneuverShowSK;
  if (!_maneuverShowSK && !_maneuverShowVakaros) _maneuverShowVakaros = true;
  renderManeuverCard();
}

function setManeuverTickInterval(sec) {
  _maneuverTickInterval = Number(sec) || 0;
  renderManeuverCard();
}

// ---------- Tack diagram rendering (SVG) ----------

function _trackBounds(tracks) {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  tracks.forEach(tr => tr.forEach(p => {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }));
  if (!isFinite(minX)) return null;
  // Square-ish bounds with a bit of padding.
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(10, Math.max(maxX - minX, maxY - minY) / 2 + 5);
  return { minX: cx - half, maxX: cx + half, minY: cy - half, maxY: cy + half };
}

function _renderTrackSvg(tracks, opts) {
  // tracks: array of { points, color, label, highlight?, maneuverIdx?, ghost? }
  opts = opts || {};
  const w = opts.width || 260;
  const h = opts.height || 200;
  const pad = 12;
  const interactive = !!opts.interactive;
  const pointSets = tracks.map(t => t.points).filter(p => p && p.length);
  if (!pointSets.length) return '';
  // Extend bounds so the ghost reference line (running along y) is visible
  // even when it extends beyond the actual track.
  const ghostYs = tracks.map(t => t.ghost).filter(v => v != null && !isNaN(v));
  const extras = ghostYs.map(y => ({ x: 0, y }));
  const allPoints = pointSets.concat([extras]);
  const b = _trackBounds(allPoints);
  if (!b) return '';
  const sx = x => pad + (x - b.minX) / (b.maxX - b.minX) * (w - 2 * pad);
  const sy = y => (h - pad) - (y - b.minY) / (b.maxY - b.minY) * (h - 2 * pad);

  const scaleM = (b.maxX - b.minX);
  const gridStep = scaleM > 200 ? 50 : scaleM > 80 ? 20 : 10;
  const gridLines = [];
  for (let gx = Math.ceil(b.minX / gridStep) * gridStep; gx <= b.maxX; gx += gridStep) {
    gridLines.push('<line x1="' + sx(gx) + '" y1="' + pad + '" x2="' + sx(gx) + '" y2="' + (h - pad) + '" stroke="var(--border)" stroke-width="0.5"/>');
  }
  for (let gy = Math.ceil(b.minY / gridStep) * gridStep; gy <= b.maxY; gy += gridStep) {
    gridLines.push('<line x1="' + pad + '" y1="' + sy(gy) + '" x2="' + (w - pad) + '" y2="' + sy(gy) + '" stroke="var(--border)" stroke-width="0.5"/>');
  }

  const originX = sx(0), originY = sy(0);
  const crosshair = '<circle cx="' + originX + '" cy="' + originY + '" r="3" fill="var(--accent)"/>'
    + '<line x1="' + originX + '" y1="' + (originY - 8) + '" x2="' + originX + '" y2="' + (originY + 8) + '" stroke="var(--accent)" stroke-width="0.6"/>'
    + '<line x1="' + (originX - 8) + '" y1="' + originY + '" x2="' + (originX + 8) + '" y2="' + originY + '" stroke="var(--accent)" stroke-width="0.6"/>';

  // Wind-up frame: +y = upwind. Label it so the orientation is unambiguous.
  const windLabels = '<text x="' + (w / 2) + '" y="10" text-anchor="middle" font-size="9" fill="var(--text-secondary)">↑ upwind</text>'
    + '<text x="' + (w / 2) + '" y="' + (h - 12) + '" text-anchor="middle" font-size="9" fill="var(--text-secondary)">↓ downwind</text>';

  // For each trace, find the actual boat position at the same moment as
  // the ghost endpoint (t = duration_sec) — that's where the boat was
  // when a zero-loss tack would have put it at (0, ghost_m).
  const actualAtDuration = tracks.map(t => {
    if (!t.points || !t.points.length || t.durationSec == null) return null;
    let best = null;
    let bestDt = Infinity;
    for (const p of t.points) {
      const dt = Math.abs(p.t - t.durationSec);
      if (dt < bestDt) { bestDt = dt; best = p; }
    }
    return best;
  });

  // "Climb the ladder" ghost references. In the wind-up frame the ghost
  // is a dashed vertical segment from the origin to (0, ghost_m) — that
  // is where a zero-loss instant-turn boat would be sitting at t=duration.
  //
  // To show the upwind gap against the actual track we:
  //   1. Mark the actual boat position at t=duration (filled circle).
  //   2. Drop a horizontal ("perpendicular to the wind") dashed line
  //      from that point onto the wind axis — this is the projection
  //      of the actual position onto the ladder.
  //   3. Draw a vertical gap segment from the projection up to the
  //      ghost endpoint. Its length is the upwind loss vs the ghost.
  //   4. Label only the vertical gap so the number matches the shape.
  const ghostLines = tracks.map((t, i) => {
    if (t.ghost == null || isNaN(t.ghost)) return '';
    const gy1 = sy(0), gy2 = sy(t.ghost);
    let out = '<line x1="' + originX + '" y1="' + gy1 + '" x2="' + originX + '" y2="' + gy2
      + '" stroke="' + t.color + '" stroke-width="1" stroke-dasharray="3,3" opacity="0.7"/>'
      + '<circle cx="' + originX + '" cy="' + gy2 + '" r="2.5" fill="none" stroke="' + t.color
      + '" stroke-width="1.2" opacity="0.8"/>';

    const actual = actualAtDuration[i];
    if (actual) {
      const ax = sx(actual.x), ay = sy(actual.y);
      const projY = ay;  // projection onto x=0 keeps the same y (wind component).
      // Marker on the actual track at t = duration.
      out += '<circle cx="' + ax + '" cy="' + ay + '" r="3" fill="' + t.color
        + '" stroke="var(--bg-secondary)" stroke-width="1"/>';
      // Horizontal ("perpendicular to wind") projection from actual onto wind axis.
      out += '<line x1="' + ax + '" y1="' + ay + '" x2="' + originX + '" y2="' + projY
        + '" stroke="' + t.color + '" stroke-width="1" stroke-dasharray="1,2" opacity="0.55"/>';
      // Vertical gap segment from projection up to ghost endpoint.
      out += '<line x1="' + originX + '" y1="' + projY + '" x2="' + originX + '" y2="' + gy2
        + '" stroke="' + t.color + '" stroke-width="2.5" opacity="0.9"/>';
      // Gap label — only in single-track / highlighted mode to avoid
      // clutter in the overlay.
      if (t.highlight) {
        const deltaM = t.ghost - actual.y;
        const midY = (projY + gy2) / 2;
        const label = (deltaM >= 0 ? '−' : '+') + Math.abs(deltaM).toFixed(1) + ' m';
        out += '<text x="' + (originX + 6) + '" y="' + (midY + 3) + '" font-size="10" fill="' + t.color
          + '" style="paint-order:stroke;stroke:var(--bg-secondary);stroke-width:3px;stroke-linejoin:round">'
          + label + '</text>';
      }
    }
    return out;
  }).join('');

  // Time ticks + speed-recovery markers. Both ride on top of the path so
  // they're always visible regardless of track ordering.
  const tickInterval = _maneuverTickInterval;
  const decorations = tracks.map(t => {
    if (!t.points || !t.points.length) return '';
    let out = '';
    if (tickInterval > 0) {
      const ts = t.points.map(p => p.t).filter(v => v != null);
      if (ts.length) {
        const tMin = Math.min(...ts), tMax = Math.max(...ts);
        const kStart = Math.ceil(tMin / tickInterval);
        const kEnd = Math.floor(tMax / tickInterval);
        for (let k = kStart; k <= kEnd; k++) {
          const target = k * tickInterval;
          let best = null, bestDt = Infinity;
          for (const p of t.points) {
            const dt = Math.abs(p.t - target);
            if (dt < bestDt) { bestDt = dt; best = p; }
          }
          if (!best || bestDt > tickInterval / 2) continue;
          const cx = sx(best.x), cy = sy(best.y);
          out += '<circle cx="' + cx + '" cy="' + cy + '" r="1.8" fill="' + t.color
            + '" stroke="var(--bg-secondary)" stroke-width="0.5" opacity="0.9"/>';
          if (k !== 0 && t.highlight) {
            out += '<text x="' + (cx + 3) + '" y="' + (cy - 3) + '" font-size="8" fill="' + t.color
              + '" style="paint-order:stroke;stroke:var(--bg-secondary);stroke-width:2px;stroke-linejoin:round">'
              + target + 's</text>';
          }
        }
      }
    }
    if (t.entryBsp != null && t.durationSec != null) {
      const targets = [
        { frac: 0.8, shape: 'square', label: '80%' },
        { frac: 1.0, shape: 'diamond', label: '100%' },
      ];
      for (const tgt of targets) {
        const threshold = t.entryBsp * tgt.frac;
        let hit = null;
        for (const p of t.points) {
          if (p.t < t.durationSec) continue;
          if (p.bsp != null && p.bsp >= threshold) { hit = p; break; }
        }
        if (!hit) continue;
        const cx = sx(hit.x), cy = sy(hit.y);
        const r = 4;
        if (tgt.shape === 'square') {
          out += '<rect x="' + (cx - r) + '" y="' + (cy - r) + '" width="' + (2 * r)
            + '" height="' + (2 * r) + '" fill="none" stroke="' + t.color
            + '" stroke-width="1.4"/>';
        } else {
          out += '<polygon points="' + cx + ',' + (cy - r) + ' ' + (cx + r) + ',' + cy
            + ' ' + cx + ',' + (cy + r) + ' ' + (cx - r) + ',' + cy
            + '" fill="none" stroke="' + t.color + '" stroke-width="1.4"/>';
        }
        if (t.highlight) {
          out += '<text x="' + (cx + r + 2) + '" y="' + (cy + 3) + '" font-size="8" fill="' + t.color
            + '" style="paint-order:stroke;stroke:var(--bg-secondary);stroke-width:2px;stroke-linejoin:round">'
            + tgt.label + '</text>';
        }
      }
    }
    return out;
  }).join('');

  const paths = tracks.map(t => {
    if (!t.points || !t.points.length) return '';
    const d = t.points.map((p, i) => (i === 0 ? 'M' : 'L') + sx(p.x).toFixed(1) + ' ' + sy(p.y).toFixed(1)).join(' ');
    const width = t.highlight ? 2.5 : 1.4;
    const opacity = t.highlight ? 1 : 0.7;
    let attrs = 'fill="none" stroke="' + t.color + '" stroke-width="' + width + '" opacity="' + opacity + '" stroke-linecap="round"';
    if (t.dashArray) attrs += ' stroke-dasharray="' + t.dashArray + '"';
    if (interactive && t.maneuverIdx != null) {
      attrs += ' data-man-idx="' + t.maneuverIdx + '"'
        + ' style="pointer-events:stroke;cursor:pointer"'
        + ' onmouseenter="showOverlayTip(event,' + t.maneuverIdx + ')"'
        + ' onmouseleave="scheduleOverlayTipHide()"'
        + ' onclick="highlightManeuver(' + t.maneuverIdx + ')"';
    }
    return '<path d="' + d + '" ' + attrs + '/>';
  }).join('');

  // Invisible fat underlay to widen hover hit-target.
  const hoverUnderlay = interactive ? tracks.map(t => {
    if (!t.points || !t.points.length || t.maneuverIdx == null) return '';
    const d = t.points.map((p, i) => (i === 0 ? 'M' : 'L') + sx(p.x).toFixed(1) + ' ' + sy(p.y).toFixed(1)).join(' ');
    return '<path d="' + d + '" fill="none" stroke="rgba(0,0,0,0)" stroke-width="14"'
      + ' style="pointer-events:stroke;cursor:pointer"'
      + ' onmouseenter="showOverlayTip(event,' + t.maneuverIdx + ')"'
      + ' onmouseleave="scheduleOverlayTipHide()"'
      + ' onclick="highlightManeuver(' + t.maneuverIdx + ')"/>';
  }).join('') : '';

  const scaleLabel = '<text x="' + (w - pad) + '" y="' + (h - 2) + '" text-anchor="end" font-size="9" fill="var(--text-secondary)">grid ' + gridStep + ' m</text>';

  return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:3px">'
    + gridLines.join('') + ghostLines + hoverUnderlay + paths + decorations + crosshair + windLabels + scaleLabel + '</svg>';
}

// Overlay tooltip — anchored on mouseenter, stays reachable by the mouse.
// Hiding is delayed so the cursor can traverse from the trace to the tip;
// entering the tip itself cancels the hide, so links inside are clickable.
let _overlayTipHideTimer = null;
let _overlayTipIdx = null;

function _ensureOverlayTip() {
  let tip = document.getElementById('overlay-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'overlay-tip';
    tip.style.cssText = 'position:fixed;z-index:9999;background:var(--bg-primary);'
      + 'border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:.72rem;'
      + 'box-shadow:0 4px 12px rgba(0,0,0,0.3);max-width:240px;display:none';
    tip.onmouseenter = cancelOverlayTipHide;
    tip.onmouseleave = scheduleOverlayTipHide;
    document.body.appendChild(tip);
  }
  return tip;
}

function _highlightManeuverRow(idx, on) {
  // Mirror the overlay hover on the table row so the two views stay linked.
  document.querySelectorAll('.maneuver-table tr').forEach(r => r.classList.remove('hover-row'));
  if (on && idx != null) {
    const row = document.getElementById('mrow-' + idx);
    if (row) {
      row.classList.add('hover-row');
      row.scrollIntoView({block: 'nearest'});
    }
  }
}

function _highlightOverlayTrack(idx, on) {
  // Bump the matching overlay paths when a table row is hovered, so the
  // link from row → track is as obvious as track → row. Uses inline
  // styles so the override wins over the attribute-level stroke-width.
  const paths = document.querySelectorAll(
    '#maneuvers-body svg path[data-man-idx="' + idx + '"]'
  );
  paths.forEach(p => {
    if (p.getAttribute('stroke') === 'rgba(0,0,0,0)') return;  // skip hover underlay
    if (on) {
      p.style.strokeWidth = '3.5';
      p.style.opacity = '1';
    } else {
      p.style.strokeWidth = '';
      p.style.opacity = '';
    }
  });
}

function showOverlayTip(ev, idx) {
  cancelOverlayTipHide();
  const m = _maneuvers[idx];
  if (!m) return;
  _overlayTipIdx = idx;
  _highlightManeuverRow(idx, true);
  const tip = _ensureOverlayTip();
  const color = _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
  const rankColor = m.rank ? _RANK_COLORS[m.rank] : 'var(--text-secondary)';
  const twsVal = m.entry_tws != null ? m.entry_tws : (m.tws_bin != null ? m.tws_bin : null);
  const twsStr = twsVal != null ? ((twsVal.toFixed ? twsVal.toFixed(1) : twsVal) + ' kt') : '—';
  // Actual upwind progress at t = duration, from the track points.
  let ghostDelta = null;
  if (m.track && m.track.length && m.duration_sec != null && m.ghost_m != null) {
    let best = null, bestDt = Infinity;
    for (const p of m.track) {
      const dt = Math.abs(p.t - m.duration_sec);
      if (dt < bestDt) { bestDt = dt; best = p; }
    }
    if (best) ghostDelta = m.ghost_m - best.y;
  }
  const ghostDeltaStr = ghostDelta != null
    ? (ghostDelta >= 0 ? '−' : '+') + Math.abs(ghostDelta).toFixed(1) + ' m vs ghost'
    : '—';
  const rows = [
    ['Elapsed', _fmtElapsed(m.ts)],
    ['Time', fmtTime(m.ts)],
    ['Duration', m.duration_sec != null ? m.duration_sec.toFixed(1) + ' s' : '—'],
    ['Turn phase', m.time_to_head_to_wind_s != null ? m.time_to_head_to_wind_s.toFixed(1) + ' s' : '—'],
    ['Recovery', m.time_to_recover_s != null ? m.time_to_recover_s.toFixed(1) + ' s' : '—'],
    ['Turn angle', m.turn_angle_deg != null ? Math.round(Math.abs(m.turn_angle_deg)) + '°' : '—'],
    ['BSP in→out', (m.entry_bsp != null ? m.entry_bsp.toFixed(1) : '—') + '→' + (m.exit_bsp != null ? m.exit_bsp.toFixed(1) : '—')],
    ['BSP dip', m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—'],
    ['Min BSP', m.min_bsp != null ? m.min_bsp.toFixed(1) + ' kt' : '—'],
    ['Dist loss', m.distance_loss_m != null ? m.distance_loss_m.toFixed(1) + ' m' : '—'],
    ['Ladder ideal', m.ghost_m != null ? m.ghost_m.toFixed(1) + ' m' : '—'],
    ['Ladder Δ', ghostDeltaStr],
    ['TWS', twsStr],
    ['TWD', m.twd_deg != null ? Math.round(m.twd_deg) + '°' : '—'],
  ];
  const rankPctStr = m.loss_percentile != null ? ' (p' + m.loss_percentile + ')' : '';
  const header = '<div style="margin-bottom:4px;display:flex;justify-content:space-between;align-items:center;gap:6px">'
    + '<span><span style="color:' + color + ';font-weight:600">' + esc(m.type) + '</span>'
    + (m.rank ? ' <span style="color:' + rankColor + '" title="loss percentile (lower = less loss)">●' + esc(m.rank) + rankPctStr + '</span>' : '') + '</span>'
    + '<span style="color:var(--text-secondary);cursor:pointer;font-size:.8rem" onclick="hideOverlayTip()" title="Close">✕</span>'
    + '</div>';
  const grid = '<div style="display:grid;grid-template-columns:auto 1fr;gap:2px 8px">'
    + rows.map(([k, v]) => '<span style="color:var(--text-secondary)">' + k + '</span><b>' + esc(v) + '</b>').join('')
    + '</div>';
  const yt = m.youtube_url
    ? '<div style="margin-top:6px"><a href="' + esc(m.youtube_url) + '" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">&#9654; Watch on YouTube &#8599;</a></div>'
    : '';
  tip.innerHTML = header + grid + yt;
  tip.style.display = 'block';

  // Anchor the tooltip to the overlay SVG (top-right of the container) so
  // it does not follow the cursor. This keeps the YouTube link reachable
  // and prevents the "tooltip runs away" problem.
  const svgContainer = document.querySelector('#maneuvers-body svg');
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14;
  let y = ev.clientY + 14;
  if (svgContainer) {
    const box = svgContainer.getBoundingClientRect();
    x = box.right + 8;
    y = box.top;
  }
  if (x + r.width > window.innerWidth) x = Math.max(2, window.innerWidth - r.width - 4);
  if (y + r.height > window.innerHeight) y = Math.max(2, window.innerHeight - r.height - 4);
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

function scheduleOverlayTipHide() {
  cancelOverlayTipHide();
  _overlayTipHideTimer = setTimeout(hideOverlayTip, 180);
}

function cancelOverlayTipHide() {
  if (_overlayTipHideTimer) {
    clearTimeout(_overlayTipHideTimer);
    _overlayTipHideTimer = null;
  }
}

function hideOverlayTip() {
  cancelOverlayTipHide();
  const tip = document.getElementById('overlay-tip');
  if (tip) tip.style.display = 'none';
  _highlightManeuverRow(_overlayTipIdx, false);
  _overlayTipIdx = null;
}

function _raceStartMs() {
  if (_vakarosSyntheticStart && _vakarosSyntheticStart.ts) {
    const d = _parseUtc(_vakarosSyntheticStart.ts);
    if (d) return d.getTime();
  }
  return null;
}

function _matchesManeuverFilter(m) {
  if (_maneuverFilter.size) {
    const activeTypes = _MANEUVER_TYPE_PILLS.filter(p => _maneuverFilter.has(p));
    if (activeTypes.length && !activeTypes.includes(m.type)) return false;
    const activeRanks = _MANEUVER_RANK_PILLS.filter(p => _maneuverFilter.has(p));
    if (activeRanks.length && !activeRanks.includes(m.rank)) return false;
    // Direction filter: P→S = negative turn_angle_deg, S→P = positive
    const activeDir = _MANEUVER_DIR_PILLS.filter(p => _maneuverFilter.has(p));
    if (activeDir.length) {
      if (m.turn_angle_deg == null) return false;
      const isPS = m.turn_angle_deg < 0;  // P→S = negative
      if (activeDir.includes('P\u2192S') && !isPS) return false;
      if (activeDir.includes('S\u2192P') && isPS) return false;
    }
    if (_maneuverFilter.has('post-start')) {
      const startMs = _raceStartMs();
      if (startMs != null) {
        const t = _parseUtc(m.ts);
        if (t == null || t.getTime() < startMs) return false;
      }
    }
  }
  // Tag filter — AND or OR semantics across selected tag ids.
  if (_maneuverTagFilter.size) {
    const have = new Set((m.tags || []).map(t => t.id));
    if (_maneuverTagMode === 'or') {
      let matched = false;
      for (const tid of _maneuverTagFilter) {
        if (have.has(tid)) { matched = true; break; }
      }
      if (!matched) return false;
    } else {
      for (const tid of _maneuverTagFilter) {
        if (!have.has(tid)) return false;
      }
    }
  }
  return true;
}

function setManeuverTagFilter(tagId) {
  if (_maneuverTagFilter.has(tagId)) _maneuverTagFilter.delete(tagId);
  else _maneuverTagFilter.add(tagId);
  renderManeuverCard();
}

function setManeuverTagMode(mode) {
  if (mode !== 'and' && mode !== 'or') return;
  _maneuverTagMode = mode;
  renderManeuverCard();
}

function clearManeuverTagFilter() {
  _maneuverTagFilter.clear();
  renderManeuverCard();
}

function _tagModeBtn(mode, label) {
  const active = _maneuverTagMode === mode;
  const title = mode === 'and'
    ? 'Match maneuvers with all selected tags'
    : 'Match maneuvers with any selected tag';
  const style = 'font-size:.68rem;padding:2px 8px;border:none;background:'
    + (active ? 'var(--accent)' : 'transparent') + ';color:'
    + (active ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer';
  return '<button style="' + style + '" title="' + title
    + '" onclick="setManeuverTagMode(\'' + mode + '\')">' + label + '</button>';
}

function _renderOverlaySvg() {
  const items = _maneuvers
    .filter((m, i) => _maneuverSelected.has(_manKey(m, i)))
    .filter(_matchesManeuverFilter)
    .filter(m => m.track && m.track.length);
  if (!items.length) {
    return '<div style="color:var(--text-secondary);font-size:.75rem">No maneuvers match the current filter. Clear the filter or tick more rows below.</div>';
  }
  const tracks = [];
  items.forEach(m => {
    const idx = _maneuvers.indexOf(m);
    const baseColor = _RANK_COLORS[m.rank] || _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
    if (_maneuverShowSK && m.track && m.track.length) {
      tracks.push({
        points: m.track,
        color: baseColor,
        label: m.type,
        highlight: false,
        maneuverIdx: idx,
        ghost: m.ghost_m,
        durationSec: m.duration_sec,
        entryBsp: m.entry_bsp,
      });
    }
    if (_maneuverShowVakaros && m.track_vakaros && m.track_vakaros.length) {
      tracks.push({
        points: m.track_vakaros,
        color: '#8b5cf6',
        label: m.type + ' (vakaros)',
        highlight: false,
        maneuverIdx: idx,
        ghost: _maneuverShowSK ? null : m.ghost_m,
        durationSec: _maneuverShowSK ? null : m.duration_sec,
        entryBsp: _maneuverShowSK ? null : m.entry_bsp,
        dashArray: '2,3',
      });
    }
  });
  if (!tracks.length) {
    return '<div style="color:var(--text-secondary);font-size:.75rem">No tracks to show — enable SK or Vakaros.</div>';
  }
  const svg = _renderTrackSvg(tracks, { width: 420, height: 340, interactive: true });
  const totalLabel = _maneuverFilter.size === 0
    ? _maneuvers.length + ''
    : _maneuvers.filter(_matchesManeuverFilter).length + ' '
        + Array.from(_maneuverFilter).join('+');
  const legend = '<div style="font-size:.7rem;color:var(--text-secondary);margin-top:4px">'
    + items.length + ' of ' + totalLabel + ' overlaid. Colours = rank '
    + '<span style="color:' + _RANK_COLORS.good + '">●good</span> '
    + '<span style="color:' + _RANK_COLORS.avg + '">●avg</span> '
    + '<span style="color:' + _RANK_COLORS.bad + '">●bad</span>. '
    + 'Entry at origin (+), entry direction ↑. Hover a trace for stats &amp; video.'
    + '</div>';
  return svg + legend;
}

function _manHeader(label, key) {
  const arrow = _maneuverSort.key === key ? (_maneuverSort.dir > 0 ? ' ▲' : ' ▼') : '';
  return '<th style="cursor:pointer" onclick="setManeuverSort(\'' + key + '\')">' + label + arrow + '</th>';
}

function renderManeuverCard() {
  const card = document.getElementById('maneuvers-card');
  const body = document.getElementById('maneuvers-body');
  card.style.display = '';

  if (!_maneuvers.length) {
    body.innerHTML = '<span style="color:var(--text-secondary)">No maneuvers detected. Click &#8635; Detect to analyse this session.</span>';
    return;
  }

  const tacks = _maneuvers.filter(m => m.type === 'tack').length;
  const gybes = _maneuvers.filter(m => m.type === 'gybe').length;
  const roundings = _maneuvers.filter(m => m.type === 'rounding').length;
  const good = _maneuvers.filter(m => m.rank === 'good').length;
  const bad = _maneuvers.filter(m => m.rank === 'bad').length;

  const overlayBtnStyle = 'font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:'
    + (_maneuverOverlay ? 'var(--accent)' : 'transparent') + ';color:'
    + (_maneuverOverlay ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px';
  const hasVakarosTracks = _maneuvers.some(m => m.track_vakaros && m.track_vakaros.length);
  const skBtnStyle = 'font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:'
    + (_maneuverShowSK ? 'var(--accent)' : 'transparent') + ';color:'
    + (_maneuverShowSK ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px';
  const vakarosBtnStyle = 'font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:'
    + (_maneuverShowVakaros ? '#8b5cf6' : 'transparent') + ';color:'
    + (_maneuverShowVakaros ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px';
  const sourceBtns = hasVakarosTracks
    ? '<button style="' + skBtnStyle + '" onclick="toggleManeuverShowSK()" title="Show SK-derived tracks">sk</button>'
      + '<button style="' + vakarosBtnStyle + '" onclick="toggleManeuverShowVakaros()" title="Show Vakaros GPS tracks">vakaros</button>'
    : '';
  const summary = '<div style="color:var(--text-secondary);font-size:.75rem;margin-bottom:6px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">'
    + '<span>' + tacks + 'T · ' + gybes + 'G · ' + roundings + 'R</span>'
    + '<span style="color:' + _RANK_COLORS.good + '">' + good + ' good</span>'
    + '<span style="color:' + _RANK_COLORS.bad + '">' + bad + ' bad</span>'
    + '<span style="flex:1"></span>'
    + sourceBtns
    + '<label style="font-size:.7rem;color:var(--text-secondary)">ticks '
    + '<select onchange="setManeuverTickInterval(this.value)" style="font-size:.7rem;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border);border-radius:3px;padding:1px 3px">'
    + ['0', '2', '5', '10'].map(s =>
        '<option value="' + s + '"' + (String(_maneuverTickInterval) === s ? ' selected' : '') + '>'
        + (s === '0' ? 'off' : s + 's') + '</option>').join('')
    + '</select></label>'
    + '<button style="' + overlayBtnStyle + '" onclick="toggleManeuverOverlay()" title="Overlay all filtered tacks on one diagram">overlay</button>'
    + '<button style="font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:'
      + (_maneuverLineChartsOn ? 'var(--accent)' : 'transparent') + ';color:'
      + (_maneuverLineChartsOn ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px"'
      + ' onclick="toggleManeuverLineCharts()"'
      + ' title="BSP / heading rate / TWA stacked line charts aligned at head-to-wind">line charts</button>'
    + '<a href="/api/sessions/' + SESSION_ID + '/maneuvers.csv" download style="color:var(--accent);text-decoration:none">CSV &#8595;</a>'
    + '</div>';

  const filters = ['all', 'tack', 'gybe', 'rounding', 'P\u2192S', 'S\u2192P', 'good', 'bad'];
  if (_raceStartMs() != null) filters.push('post-start');
  const filterBar = '<div style="display:flex;gap:4px;margin-bottom:6px;flex-wrap:wrap">'
    + filters.map(f => {
        const active = f === 'all' ? _maneuverFilter.size === 0 : _maneuverFilter.has(f);
        const style = 'font-size:.7rem;padding:2px 8px;border:1px solid var(--border);background:'
          + (active ? 'var(--accent)' : 'transparent') + ';color:'
          + (active ? 'var(--bg-primary)' : 'var(--text-secondary)') + ';cursor:pointer;border-radius:3px';
        return '<button style="' + style + '" onclick="setManeuverFilter(\'' + f + '\')">' + f + '</button>';
      }).join('')
    + '</div>';

  // Tag filter bar — only show tags that are actually attached to at least
  // one maneuver in this session, so unused tags don't clutter the UI.
  const tagUsage = new Map(); // id → {name, color, count}
  for (const m of _maneuvers) {
    for (const t of (m.tags || [])) {
      const row = tagUsage.get(t.id) || {name: t.name, color: t.color, count: 0};
      row.count++;
      tagUsage.set(t.id, row);
    }
  }
  let tagFilterBar = '';
  if (tagUsage.size > 0) {
    const sorted = [...tagUsage.entries()].sort((a, b) => a[1].name.localeCompare(b[1].name));
    const chips = sorted.map(([id, row]) => {
      const active = _maneuverTagFilter.has(id);
      const borderColor = row.color || 'var(--border)';
      const style = 'font-size:.7rem;padding:2px 8px;border:1px solid ' + borderColor
        + ';background:' + (active ? 'var(--accent)' : 'transparent') + ';color:'
        + (active ? 'var(--bg-primary)' : 'var(--text-primary)')
        + ';cursor:pointer;border-radius:10px';
      const swatch = row.color
        ? '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + row.color + ';margin-right:4px;vertical-align:middle"></span>'
        : '';
      return '<button style="' + style + '" onclick="setManeuverTagFilter(' + id + ')">' + swatch + esc(row.name) + ' (' + row.count + ')</button>';
    }).join('');
    const clearBtn = _maneuverTagFilter.size
      ? '<button style="font-size:.68rem;padding:2px 6px;border:none;background:none;color:var(--text-secondary);cursor:pointer;text-decoration:underline" onclick="clearManeuverTagFilter()">clear</button>'
      : '';
    // Mode toggle always visible when the tag row is shown, dimmed when
    // fewer than 2 tags are active so users discover the control.
    const modeDim = _maneuverTagFilter.size < 2 ? ';opacity:.6' : '';
    const modeToggle = '<span style="display:inline-flex;border:1px solid var(--border);border-radius:3px;overflow:hidden;margin-left:4px' + modeDim + '">'
      +   _tagModeBtn('and', 'all')
      +   _tagModeBtn('or', 'any')
      + '</span>';
    tagFilterBar = '<div style="display:flex;gap:4px;margin-bottom:6px;flex-wrap:wrap;align-items:center">'
      + '<span style="font-size:.68rem;color:var(--text-secondary);margin-right:2px">Tags:</span>'
      + chips + modeToggle + clearBtn
      + '</div>';
  }

  const items = _maneuverRows();
  let rows = items.map((m) => {
    const idx = _maneuvers.indexOf(m);
    const key = _manKey(m, idx);
    const color = _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
    const rankColor = m.rank ? _RANK_COLORS[m.rank] : 'transparent';
    const rankDot = m.rank
      ? '<span title="' + m.rank + '" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + rankColor + ';margin-right:4px"></span>'
      : '';
    // Direction hint from the signed turn angle.
    let dirHint = '';
    if ((m.type === 'tack' || m.type === 'gybe') && m.turn_angle_deg != null) {
      dirHint = m.turn_angle_deg > 0
        ? '<span title="Starboard → Port" style="color:var(--text-secondary);margin-left:3px">S→P</span>'
        : '<span title="Port → Starboard" style="color:var(--text-secondary);margin-left:3px">P→S</span>';
    }
    const typeBadge = rankDot + '<span style="color:' + color + ';font-weight:600">'
      + esc(m.type) + '</span>' + dirHint;
    const selected = _maneuverSelected.has(key);
    const cbox = '<input type="checkbox" ' + (selected ? 'checked ' : '') + 'onclick="event.stopPropagation();toggleManeuverSelected(\'' + key + '\')" title="Include in overlay">';
    const elapsed = _fmtElapsed(m.ts);
    const t = fmtTime(m.ts);
    const dur = m.duration_sec != null ? m.duration_sec.toFixed(1) + 's' : '—';
    const turn = m.turn_angle_deg != null ? Math.round(Math.abs(m.turn_angle_deg)) + '°' : '—';
    const bspDip = m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—';
    const distLoss = m.distance_loss_m != null ? m.distance_loss_m.toFixed(1) + ' m' : '—';
    const entry = (m.entry_bsp != null ? m.entry_bsp.toFixed(1) : '—') + '→' + (m.exit_bsp != null ? m.exit_bsp.toFixed(1) : '—');
    // Fall back to the detector's stored tws_bin (integer kt) when the
    // averaged entry window didn't hit any wind samples.
    const twsVal = m.entry_tws != null ? m.entry_tws : (m.tws_bin != null ? m.tws_bin : null);
    const cond = twsVal != null ? (twsVal.toFixed ? twsVal.toFixed(0) : twsVal) + ' kt' : '—';
    const yt = m.youtube_url
      ? '<a href="' + esc(m.youtube_url) + '" target="_blank" rel="noopener" title="Watch on YouTube" style="color:var(--accent);text-decoration:none" onclick="event.stopPropagation()">&#9654;</a>'
      : '';
    // "+ Moment" action — open the Moments panel's new-moment form anchored
    // to this maneuver. Inline in the actions column alongside the YouTube
    // link so there's no new column churn.
    const momentBtn = m.id != null
      ? '<a href="#" title="Create a moment anchored to this maneuver" '
        + 'onclick="event.preventDefault();event.stopPropagation();createMomentFromManeuver(' + idx + ')" '
        + 'style="color:var(--accent);text-decoration:none;margin-left:6px;font-size:.9em" aria-label="Create moment here">+</a>'
      : '';
    return '<tr id="mrow-' + idx + '" style="cursor:pointer"'
      + ' onclick="highlightManeuver(' + idx + ')"'
      + ' onmouseenter="_highlightOverlayTrack(' + idx + ',true)"'
      + ' onmouseleave="_highlightOverlayTrack(' + idx + ',false)">'
      + '<td>' + cbox + '</td>'
      + '<td>' + typeBadge + '</td>'
      + '<td style="font-variant-numeric:tabular-nums">' + elapsed + '</td>'
      + '<td>' + t + '</td>'
      + '<td>' + dur + '</td>'
      + '<td>' + turn + '</td>'
      + '<td>' + entry + '</td>'
      + '<td title="BSP dip from pre-maneuver baseline to minimum BSP during the turn. Not exit−entry.">' + bspDip + '</td>'
      + '<td>' + distLoss + '</td>'
      + '<td>' + esc(cond) + '</td>'
      + '<td style="white-space:nowrap">' + yt + momentBtn + '</td>'
      + '</tr>';
  }).join('');

  const overlayBlock = _maneuverOverlay
    ? '<div style="margin-bottom:8px">' + _renderOverlaySvg() + '</div>'
    : '';

  // Inline line charts (#619 parity) — three stacked panels aligned at
  // head-to-wind. Host div is always present; the fetch happens on a
  // separate tick so the table below renders immediately. Controlled
  // by _maneuverLineChartsOn so the user can hide it when not needed.
  const lineChartsBlock = _maneuverLineChartsOn
    ? '<div id="mv-line-charts-host" style="margin-bottom:8px"></div>'
    : '';

  const selCount = _maneuverSelected.size;
  const hasVideoInSelected = _maneuvers.some(m => _maneuverSelected.has(_manKey(m, _maneuvers.indexOf(m))) && m.youtube_url);
  const compareBtnStyle = 'font-size:.7rem;padding:3px 10px;border:1px solid var(--accent);background:none;color:var(--accent);cursor:pointer;border-radius:4px;margin-left:auto;font-weight:600' + (hasVideoInSelected ? '' : ';opacity:.4;pointer-events:none');
  const selectBar = '<div style="font-size:.7rem;color:var(--text-secondary);margin:4px 0;display:flex;gap:6px;align-items:center">'
    + '<span>Overlay: ' + selCount + ' selected</span>'
    + '<button style="font-size:.68rem;padding:1px 6px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer;border-radius:3px" onclick="setManeuverSelectAll(\'all\')">all</button>'
    + '<button style="font-size:.68rem;padding:1px 6px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer;border-radius:3px" onclick="setManeuverSelectAll(\'none\')">none</button>'
    + '<button style="font-size:.68rem;padding:1px 6px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer;border-radius:3px" onclick="setManeuverSelectAll(\'filtered\')">match filter</button>'
    + '<button style="font-size:.7rem;padding:3px 10px;border:1px solid var(--accent);background:none;color:var(--accent);cursor:pointer;border-radius:4px;font-weight:600" onclick="openManeuverOverlay()" title="Time-aligned multi-maneuver chart at head-to-wind">Overlay Chart</button>'
    + '<button style="' + compareBtnStyle + '" onclick="openManeuverCompare()" title="Open synced video comparison for selected maneuvers">Compare Videos</button>'
    + '</div>';

  body.innerHTML = summary + filterBar + tagFilterBar + overlayBlock + lineChartsBlock + selectBar
    + '<table class="maneuver-table"><thead><tr>'
    + '<th title="Include in overlay"></th>'
    + _manHeader('Type', 'type')
    + '<th>Elapsed</th>'
    + _manHeader('Time', 'ts')
    + _manHeader('Dur', 'duration_sec')
    + _manHeader('Turn', 'turn_angle_deg')
    + '<th>BSP in→out</th>'
    + '<th title="BSP dip: baseline − min BSP during the turn. Not exit−entry." onclick="setManeuverSort(\'loss_kts\')" style="cursor:pointer">BSP dip' + (_maneuverSort.key === 'loss_kts' ? (_maneuverSort.dir > 0 ? ' ▲' : ' ▼') : '') + '</th>'
    + _manHeader('Dist loss', 'distance_loss_m')
    + '<th>TWS</th><th></th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>'
    + '<div id="maneuver-detail" style="margin-top:8px"></div>';

  // Paint the line charts after the host div lands in the DOM. Cheap —
  // cache-hit on the overlay endpoint when the id-list is unchanged.
  if (_maneuverLineChartsOn) _paintSessionLineCharts();
}

function _renderManeuverDetail(m) {
  const el = document.getElementById('maneuver-detail');
  if (!el) return;
  if (!m) { el.innerHTML = ''; return; }

  // Special case: Vakaros-sourced race start has its own metric set.
  if (m.type === 'start' && m.source === 'vakaros') {
    let biasLabel = '—';
    if (m.start_line_bias_deg != null) {
      const mag = Math.abs(m.start_line_bias_deg).toFixed(1);
      const end = m.start_favored_end || 'square';
      biasLabel = end === 'square' ? 'square' : (mag + '° favoring ' + end);
    }
    const rows = [
      ['BSP at gun', m.entry_bsp != null ? m.entry_bsp.toFixed(2) + ' kt' : '—'],
      ['SOG at gun', m.start_sog_kts != null ? m.start_sog_kts.toFixed(2) + ' kt' : '—'],
      ['Distance to line', m.start_distance_to_line_m != null ? m.start_distance_to_line_m.toFixed(1) + ' m' : '—'],
      ['Polar %', m.start_polar_pct != null ? m.start_polar_pct.toFixed(0) + '%' : '—'],
      ['TWS', m.start_tws_kts != null ? m.start_tws_kts.toFixed(1) + ' kt' : '—'],
      ['TWD', m.start_twd_deg != null ? Math.round(m.start_twd_deg) + '°' : '—'],
      ['TWA', m.start_twa_deg != null ? Math.round(m.start_twa_deg) + '°' : '—'],
      ['Line bias', biasLabel],
    ];
    el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px 12px;font-size:.72rem;background:var(--bg-secondary);padding:8px;border-radius:3px">'
      + rows.map(([k, v]) => '<div><span style="color:var(--text-secondary)">' + k + '</span> <b>' + esc(v) + '</b></div>').join('')
      + '</div>'
      + _renderManeuverTagRow(m);
    return;
  }

  const bspDipLabel = m.loss_kts != null && m.entry_bsp != null && m.min_bsp != null
    ? m.loss_kts.toFixed(2) + ' kt (' + m.entry_bsp.toFixed(1) + '→' + m.min_bsp.toFixed(1) + ')'
    : (m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—');
  const rows = [
    ['Entry HDG', m.entry_hdg != null ? m.entry_hdg.toFixed(0) + '°' : '—'],
    ['Exit HDG', m.exit_hdg != null ? m.exit_hdg.toFixed(0) + '°' : '—'],
    ['Turn rate', m.turn_rate_deg_s != null ? m.turn_rate_deg_s.toFixed(1) + '°/s' : '—'],
    ['Min BSP', m.min_bsp != null ? m.min_bsp.toFixed(1) + ' kt' : '—'],
    ['BSP dip', bspDipLabel],
    ['Entry TWA', m.entry_twa != null ? m.entry_twa.toFixed(0) + '°' : '—'],
    ['Exit TWA', m.exit_twa != null ? m.exit_twa.toFixed(0) + '°' : '—'],
    ['TWD', m.twd_deg != null ? Math.round(m.twd_deg) + '°' : '—'],
    ['Time to recover', m.time_to_recover_s != null ? m.time_to_recover_s.toFixed(1) + ' s' : '—'],
    ['Distance loss', m.distance_loss_m != null ? m.distance_loss_m.toFixed(1) + ' m' : '—'],
    ['Ladder ideal', m.ghost_m != null ? m.ghost_m.toFixed(1) + ' m' : '—'],
    ['Ladder Δ', (() => {
      if (!m.track || !m.track.length || m.duration_sec == null || m.ghost_m == null) return '—';
      let best = null, bestDt = Infinity;
      for (const p of m.track) {
        const dt = Math.abs(p.t - m.duration_sec);
        if (dt < bestDt) { bestDt = dt; best = p; }
      }
      if (!best) return '—';
      const d = m.ghost_m - best.y;
      return (d >= 0 ? '−' : '+') + Math.abs(d).toFixed(1) + ' m';
    })()],
  ];
  const metricsGrid = '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px 12px;font-size:.72rem;background:var(--bg-secondary);padding:8px;border-radius:3px">'
    + rows.map(([k, v]) => '<div><span style="color:var(--text-secondary)">' + k + '</span> <b>' + esc(v) + '</b></div>').join('')
    + '</div>';
  const detailTracks = [];
  if (_maneuverShowSK && m.track && m.track.length) {
    detailTracks.push({
      points: m.track,
      color: _RANK_COLORS[m.rank] || _MANEUVER_COLORS[m.type] || 'var(--accent)',
      label: m.type,
      highlight: true,
      ghost: m.ghost_m,
      durationSec: m.duration_sec,
      entryBsp: m.entry_bsp,
    });
  }
  if (_maneuverShowVakaros && m.track_vakaros && m.track_vakaros.length) {
    detailTracks.push({
      points: m.track_vakaros,
      color: '#8b5cf6',
      label: 'vakaros',
      highlight: !_maneuverShowSK,
      ghost: _maneuverShowSK ? null : m.ghost_m,
      durationSec: _maneuverShowSK ? null : m.duration_sec,
      entryBsp: _maneuverShowSK ? null : m.entry_bsp,
      dashArray: '2,3',
    });
  }
  const diagram = detailTracks.length
    ? _renderTrackSvg(detailTracks, { width: 300, height: 240 })
    : '';
  el.innerHTML = '<div style="display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">'
    + '<div style="flex:1;min-width:260px">' + metricsGrid + '</div>'
    + (diagram ? '<div>' + diagram + '</div>' : '')
    + '</div>'
    + _renderManeuverTagRow(m);
}

// Small tag-picker strip rendered beneath the maneuver detail metrics
// whenever a maneuver is selected in the session page table.
function _renderManeuverTagRow(m) {
  if (!m || m.id == null) return '';
  return '<div style="margin-top:8px;padding:8px;background:var(--bg-secondary);border-radius:3px">'
    + '<div style="font-size:.7rem;color:var(--text-secondary);margin-bottom:4px">Tags</div>'
    + '<tag-picker entity-type="maneuver" entity-id="' + esc(String(m.id)) + '"></tag-picker>'
    + '</div>';
}

function _addManeuverMarkers() {
  // Remove old markers
  _maneuverMarkers.forEach(m => m.remove());
  _maneuverMarkers = [];

  _maneuvers.forEach((m, idx) => {
    if (m.lat == null || m.lon == null) return;
    // Outer ring colored by maneuver type so the icon still tells you what
    // it is at a glance; inner fill colored by rank (good/avg/bad) so the
    // boat's track tells a debrief story without opening every popup.
    const ringColor = _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
    const fillColor = m.rank ? (_RANK_COLORS[m.rank] || ringColor) : ringColor;
    const marker = L.circleMarker([m.lat, m.lon], {
      radius: 7,
      color: ringColor,
      fillColor: fillColor,
      fillOpacity: 0.9,
      weight: 2,
    });
    marker.bindPopup(_renderManeuverPopup(m, idx));
    // Map marker clicks keep the focus on the track — they highlight the
    // maneuver and seek the replay, but do NOT scroll the page down to the
    // maneuvers card (which yanks the user away from the map).
    marker.on('click', function() { highlightManeuver(idx, {scroll: false}); });
    if (_showManeuverMarkers) marker.addTo(_map);
    _maneuverMarkers.push(marker);
  });
}

function _renderManeuverPopup(m, idx) {
  const ringColor = _MANEUVER_COLORS[m.type] || 'var(--text-secondary)';
  const pctSuffix = m.loss_percentile != null ? ' (p' + m.loss_percentile + ')' : '';
  const rankBadge = m.rank
    ? '<span style="color:' + (_RANK_COLORS[m.rank] || ringColor) + '" title="loss percentile (lower = less loss)">● ' + m.rank + pctSuffix + '</span>'
    : '';
  const lines = [
    '<b style="color:' + ringColor + ';text-transform:capitalize">' + (m.type || 'event') + '</b> ' + rankBadge,
    fmtTime(m.ts),
  ];
  if (m.duration_sec != null) lines.push(m.duration_sec.toFixed(1) + ' s total');
  if (m.time_to_head_to_wind_s != null) {
    lines.push('Turn ' + m.time_to_head_to_wind_s.toFixed(1) + ' s · Recovery '
      + (m.time_to_recover_s != null ? m.time_to_recover_s.toFixed(1) + ' s' : '—'));
  }
  if (m.turn_angle_deg != null) lines.push(Math.round(m.turn_angle_deg) + '° turn');
  if (m.entry_bsp != null && m.exit_bsp != null) {
    lines.push('BSP ' + m.entry_bsp.toFixed(1) + ' → ' + m.exit_bsp.toFixed(1) + ' kt');
  }
  if (m.loss_kts != null) lines.push(m.loss_kts.toFixed(2) + ' kt loss');
  if (m.distance_loss_m != null) lines.push(Math.round(m.distance_loss_m) + ' m loss');
  const body = lines.join('<br>');
  // "+ Create moment" action — opens the Moments panel's new-moment form
  // anchored to this maneuver. Only rendered when the maneuver has a stable
  // id (all detected maneuvers do; defensive against any unusual rows).
  const createMoment = (idx != null && m.id != null)
    ? '<div style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px">'
      + '<a href="#" onclick="event.preventDefault();createMomentFromManeuver(' + idx + ')" '
      + 'style="color:var(--accent);font-size:.78rem;text-decoration:none">+ Create moment here &rarr;</a>'
      + '</div>'
    : '';
  return body + createMoment;
}

let _showManeuverMarkers = true;
function _setManeuverMarkersVisible(visible) {
  _showManeuverMarkers = !!visible;
  _maneuverMarkers.forEach(m => {
    if (_showManeuverMarkers) m.addTo(_map);
    else m.remove();
  });
}

function highlightManeuver(idx, opts) {
  // opts.scroll === false suppresses the scroll-to-row behavior — used by
  // map-marker clicks so the user isn't yanked down to the maneuvers card
  // while they're looking at the track.
  const shouldScroll = !(opts && opts.scroll === false);
  // Highlight table row
  document.querySelectorAll('.maneuver-table tr').forEach(r => r.classList.remove('active-row'));
  const row = document.getElementById('mrow-' + idx);
  if (row) {
    row.classList.add('active-row');
    if (shouldScroll) row.scrollIntoView({block: 'nearest'});
  }
  const m = _maneuvers[idx];
  _renderManeuverDetail(m);
  // Move map cursor to maneuver position
  if (m && _trackData) {
    const ts = new Date(m.ts.endsWith('Z') || m.ts.includes('+') ? m.ts : m.ts + 'Z');
    _seekTo(ts, 'maneuver');
  }
  // Seek the embedded player to the maneuver moment if a video is loaded.
  if (m && _videoSync && _videoSync.player) {
    const ts = new Date(m.ts.endsWith('Z') || m.ts.includes('+') ? m.ts : m.ts + 'Z');
    const offset = _utcToVideoOffset(ts);
    if (offset != null && offset >= 0 && _videoSync.player.seekTo) {
      try { _videoSync.player.seekTo(offset, true); } catch (e) { /* ignore */ }
    }
  }
  // Open the marker popup if available
  if (_maneuverMarkers[idx]) _maneuverMarkers[idx].openPopup();
}

async function detectManeuvers() {
  const btn = document.getElementById('detect-maneuvers-btn');
  if (btn) { btn.textContent = '⏳'; btn.disabled = true; }
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/detect-maneuvers', {method: 'POST'});
    if (!r.ok) { alert('Detection failed: ' + r.status); return; }
    await loadManeuvers();
  } finally {
    if (btn) { btn.textContent = '↺ Detect'; btn.disabled = false; }
  }
}

// ---------------------------------------------------------------------------
// Wind Field visualization
// ---------------------------------------------------------------------------

let _wfMap = null;
let _wfCanvas = null;   // Leaflet canvas overlay
let _wfGrid = null;     // last fetched grid response
let _wfTimeseries = null;
let _wfPlaying = false;
let _wfPlayTimer = null;
let _wfTrackLine = null;
let _wfCursor = null;
let _wfMarkMarkers = [];
let _wfDuration = 0;
let _wfDebounce = null;

async function loadWindField() {
  const card = document.getElementById('wind-field-card');
  card.style.display = '';

  // Initialize the wind field Leaflet map
  _wfMap = L.map('wf-map');
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap', maxZoom: 18,
  }).addTo(_wfMap);

  // Overlay the boat track
  if (_trackData) {
    _wfTrackLine = L.polyline(_trackData.latLngs, {
      color: cssVar('--accent-strong'), weight: 3, opacity: 0.7,
    }).addTo(_wfMap);
    const wfCursorColor = cssVar('--warning');
    _wfCursor = L.circleMarker([0, 0], {
      radius: 6, color: wfCursorColor, fillColor: wfCursorColor, fillOpacity: 1, weight: 2,
    });
  }

  // Fetch initial grid (t=0) and timeseries in parallel
  const [gridR, tsR] = await Promise.all([
    fetch('/api/sessions/' + SESSION_ID + '/wind-field?elapsed_s=0&grid_size=25'),
    fetch('/api/sessions/' + SESSION_ID + '/wind-timeseries?step_s=10'),
  ]);
  if (!gridR.ok) { card.style.display = 'none'; return; }

  _wfGrid = await gridR.json();
  _wfTimeseries = tsR.ok ? await tsR.json() : null;
  _wfDuration = _wfGrid.duration_s;

  // Set slider range
  const slider = document.getElementById('wf-slider');
  slider.max = Math.floor(_wfDuration);
  slider.value = 0;
  slider.addEventListener('input', () => _onWfSlider(+slider.value));

  // Play button
  document.getElementById('wf-play-btn').addEventListener('click', _toggleWfPlay);

  // Draw marks
  _drawWfMarks(_wfGrid.marks);

  // Fit map bounds to grid
  _wfMap.fitBounds([
    [_wfGrid.grid.lat_min, _wfGrid.grid.lon_min],
    [_wfGrid.grid.lat_max, _wfGrid.grid.lon_max],
  ], {padding: [20, 20]});

  // Create canvas overlay
  _wfCanvas = _createWfCanvasOverlay();
  _wfCanvas.addTo(_wfMap);

  // Render initial state
  _renderWfGrid();
  if (_wfTimeseries) _renderWfChart(0);
}

function _drawWfMarks(marks) {
  for (const mm of _wfMarkMarkers) _wfMap.removeLayer(mm);
  _wfMarkMarkers = [];
  for (const m of marks) {
    const wfMarkColor = cssVar('--warning');
    const marker = L.circleMarker([m.lat, m.lon], {
      radius: 5, color: wfMarkColor, fillColor: wfMarkColor, fillOpacity: 0.9, weight: 1,
    }).addTo(_wfMap).bindTooltip(m.mark_name, {permanent: true, direction: 'right',
      className: 'wf-mark-label', offset: [8, 0]});
    _wfMarkMarkers.push(marker);
  }
}

// Custom Leaflet canvas overlay for wind field rendering
function _createWfCanvasOverlay() {
  const Overlay = L.Layer.extend({
    onAdd(map) {
      this._map = map;
      const pane = map.getPane('overlayPane');
      this._el = L.DomUtil.create('canvas', 'wf-overlay');
      this._el.style.position = 'absolute';
      this._el.style.pointerEvents = 'none';
      pane.appendChild(this._el);
      map.on('moveend zoomend resize', this._reset, this);
      this._reset();
    },
    onRemove(map) {
      L.DomUtil.remove(this._el);
      map.off('moveend zoomend resize', this._reset, this);
    },
    _reset() {
      const size = this._map.getSize();
      const topLeft = this._map.containerPointToLayerPoint([0, 0]);
      this._el.width = size.x;
      this._el.height = size.y;
      L.DomUtil.setPosition(this._el, topLeft);
      _renderWfGrid();
    },
    getCanvas() { return this._el; },
  });
  return new Overlay();
}

function _renderWfGrid() {
  if (!_wfCanvas || !_wfGrid) return;
  const canvas = _wfCanvas.getCanvas();
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const grid = _wfGrid.grid;
  const cells = grid.cells;
  const twsLow = _wfGrid.tws_low;
  const twsHigh = _wfGrid.tws_high;
  const rows = grid.rows;
  const cols = grid.cols;

  for (let i = 0; i < cells.length; i++) {
    const cell = cells[i];
    const pt = _wfMap.latLngToContainerPoint([cell.lat, cell.lon]);

    // Heatmap cell — color by TWS
    const norm = Math.max(0, Math.min(1, (cell.tws - twsLow) / (twsHigh - twsLow + 0.01)));
    const hue = 240 - norm * 240; // blue(low) -> red(high)
    ctx.fillStyle = 'hsla(' + hue + ', 80%, 50%, 0.35)';

    // Cell size in pixels
    const halfLat = (grid.lat_max - grid.lat_min) / (rows - 1) / 2;
    const halfLon = (grid.lon_max - grid.lon_min) / (cols - 1) / 2;
    const tl = _wfMap.latLngToContainerPoint([cell.lat + halfLat, cell.lon - halfLon]);
    const br = _wfMap.latLngToContainerPoint([cell.lat - halfLat, cell.lon + halfLon]);
    ctx.fillRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);

    // Wind arrow
    const arrowLen = 12;
    const twd_rad = cell.twd * Math.PI / 180;
    const dx = -Math.sin(twd_rad) * arrowLen;
    const dy = Math.cos(twd_rad) * arrowLen;
    ctx.strokeStyle = 'rgba(255,255,255,0.6)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(pt.x - dx * 0.5, pt.y - dy * 0.5);
    ctx.lineTo(pt.x + dx * 0.5, pt.y + dy * 0.5);
    ctx.stroke();
    // Arrowhead
    const ax = pt.x + dx * 0.5;
    const ay = pt.y + dy * 0.5;
    const headLen = 4;
    const angle = Math.atan2(dy, dx);
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(ax - headLen * Math.cos(angle - 0.5), ay - headLen * Math.sin(angle - 0.5));
    ctx.moveTo(ax, ay);
    ctx.lineTo(ax - headLen * Math.cos(angle + 0.5), ay - headLen * Math.sin(angle + 0.5));
    ctx.stroke();
  }

  // Draw cursor on track at current elapsed_s
  if (_wfCursor && _trackData && _wfGrid.elapsed_s != null && _wfGrid.duration_s > 0) {
    const frac = _wfGrid.elapsed_s / _wfGrid.duration_s;
    const idx = Math.min(Math.floor(frac * _trackData.latLngs.length), _trackData.latLngs.length - 1);
    _wfCursor.setLatLng(_trackData.latLngs[idx]).addTo(_wfMap);
  }
}

function _onWfSlider(val) {
  document.getElementById('wf-time-label').textContent = _fmtMmSs(val);
  if (_wfTimeseries) _renderWfChart(val);

  // Debounce API call for grid fetch
  if (_wfDebounce) clearTimeout(_wfDebounce);
  _wfDebounce = setTimeout(async () => {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/wind-field?elapsed_s=' + val + '&grid_size=25');
    if (r.ok) {
      _wfGrid = await r.json();
      _renderWfGrid();
    }
  }, 150);
}

function _toggleWfPlay() {
  const btn = document.getElementById('wf-play-btn');
  const slider = document.getElementById('wf-slider');
  if (_wfPlaying) {
    _wfPlaying = false;
    btn.innerHTML = '&#9654;';
    if (_wfPlayTimer) { clearInterval(_wfPlayTimer); _wfPlayTimer = null; }
  } else {
    _wfPlaying = true;
    btn.innerHTML = '&#9646;&#9646;';
    _wfPlayTimer = setInterval(() => {
      let v = +slider.value + 10;
      if (v > +slider.max) v = 0;
      slider.value = v;
      _onWfSlider(v);
    }, 200);
  }
}

function _fmtMmSs(totalS) {
  const m = Math.floor(totalS / 60);
  const s = Math.floor(totalS % 60);
  return m + ':' + (s < 10 ? '0' : '') + s;
}

// Comparative wind time series chart (canvas-drawn)
function _renderWfChart(currentS) {
  const canvas = document.getElementById('wf-chart');
  if (!canvas || !_wfTimeseries) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  const pad = {l: 50, r: 20, t: 20, b: 30, mid: 20};
  const chartH = (H - pad.t - pad.b - pad.mid) / 2;
  const chartW = W - pad.l - pad.r;
  const series = _wfTimeseries.series;
  if (!series.length) return;

  const baseTwd = _wfTimeseries.base_twd;
  const dur = _wfTimeseries.duration_s;
  const colors = [cssVar('--danger'), cssVar('--text-primary'), cssVar('--success')]; // port, center, starboard

  // Compute TWD and TWS ranges
  let twdMin = Infinity, twdMax = -Infinity;
  let twsMin = Infinity, twsMax = -Infinity;
  for (const s of series) {
    for (const v of s.twd) { twdMin = Math.min(twdMin, v); twdMax = Math.max(twdMax, v); }
    for (const v of s.tws) { twsMin = Math.min(twsMin, v); twsMax = Math.max(twsMax, v); }
  }
  twdMin = Math.floor(twdMin - 2); twdMax = Math.ceil(twdMax + 2);
  twsMin = Math.floor(twsMin - 1); twsMax = Math.ceil(twsMax + 1);

  function xForT(t) { return pad.l + (t / dur) * chartW; }

  // --- TWD chart (top) ---
  const twdY0 = pad.t;
  function yForTwd(v) { return twdY0 + chartH - (v - twdMin) / (twdMax - twdMin) * chartH; }

  // Grid
  const wfBorder = cssVar('--border');
  const wfTextSec = cssVar('--text-secondary');
  ctx.strokeStyle = wfBorder; ctx.lineWidth = 0.5; ctx.setLineDash([3, 3]);
  for (let v = twdMin; v <= twdMax; v += 2) {
    const y = yForTwd(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + chartW, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Axis labels
  ctx.fillStyle = wfTextSec; ctx.font = '11px monospace';
  ctx.fillText('TWD', pad.l - 40, twdY0 + chartH / 2 + 4);
  ctx.fillText(twdMin + '°', pad.l - 40, twdY0 + chartH - 2);
  ctx.fillText(twdMax + '°', pad.l - 40, twdY0 + 12);

  // Lines
  for (let p = 0; p < 3; p++) {
    ctx.strokeStyle = colors[p]; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.85;
    ctx.beginPath();
    for (let i = 0; i < series.length; i++) {
      const x = xForT(series[i].t);
      const y = yForTwd(series[i].twd[p]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // --- TWS chart (bottom) ---
  const twsY0 = pad.t + chartH + pad.mid;
  function yForTws(v) { return twsY0 + chartH - (v - twsMin) / (twsMax - twsMin) * chartH; }

  ctx.strokeStyle = wfBorder; ctx.lineWidth = 0.5; ctx.setLineDash([3, 3]);
  for (let v = twsMin; v <= twsMax; v += 2) {
    const y = yForTws(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + chartW, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  ctx.fillStyle = wfTextSec;
  ctx.fillText('TWS', pad.l - 40, twsY0 + chartH / 2 + 4);
  ctx.fillText(twsMin + '', pad.l - 40, twsY0 + chartH - 2);
  ctx.fillText(twsMax + '', pad.l - 40, twsY0 + 12);

  for (let p = 0; p < 3; p++) {
    ctx.strokeStyle = colors[p]; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.85;
    ctx.beginPath();
    for (let i = 0; i < series.length; i++) {
      const x = xForT(series[i].t);
      const y = yForTws(series[i].tws[p]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // Time axis labels
  ctx.fillStyle = wfTextSec;
  const stepMin = Math.max(1, Math.floor(dur / 60 / 8));
  for (let m = 0; m <= dur / 60; m += stepMin) {
    const x = xForT(m * 60);
    ctx.fillText(m + 'm', x - 6, twsY0 + chartH + 14);
  }

  // Vertical hairline at current time
  if (currentS >= 0) {
    const x = xForT(currentS);
    ctx.strokeStyle = cssVar('--warning'); ctx.lineWidth = 1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, twsY0 + chartH); ctx.stroke();
  }

  // Legend
  const labels = ['Port', 'Center', 'Stbd'];
  let lx = pad.l + 10;
  for (let i = 0; i < 3; i++) {
    ctx.fillStyle = colors[i];
    ctx.fillRect(lx, pad.t - 14, 16, 8);
    ctx.fillStyle = wfTextSec;
    ctx.fillText(labels[i], lx + 20, pad.t - 6);
    lx += 70;
  }
}

// ---------------------------------------------------------------------------
// Boat Settings (read-only, time-synced)
// ---------------------------------------------------------------------------

let _bsParams = null;       // parameter definitions from /api/boat-settings/parameters
let _bsResolved = null;     // resolved settings at current playback time
let _bsLastAsOf = null;     // debounce: last as_of value we fetched
let _bsHistory = null;      // all race-specific setting entries (full timeline)

async function loadBoatSettings() {
  const card = document.getElementById('boat-settings-card');
  if (_session.type === 'debrief') return;
  card.style.display = '';

  try {
    const r = await fetch('/api/boat-settings/parameters');
    _bsParams = await r.json();
  } catch (e) { console.error('boat settings params error', e); return; }

  // For completed sessions use end time; for active sessions use now so values
  // entered during the session are visible rather than being filtered out.
  const asOf = _session.end_utc || new Date().toISOString();
  await _fetchAndRenderBoatSettings(asOf);
}

async function _fetchAndRenderBoatSettings(asOf) {
  if (!asOf || !_bsParams) return;
  _bsLastAsOf = asOf;
  try {
    const [resolveRes, historyRes] = await Promise.all([
      fetch('/api/boat-settings/resolve?race_id=' + SESSION_ID
        + '&as_of=' + encodeURIComponent(asOf)),
      fetch('/api/boat-settings?race_id=' + SESSION_ID),
    ]);
    if (resolveRes.ok) _bsResolved = await resolveRes.json();
    if (historyRes.ok) _bsHistory = await historyRes.json();
  } catch (e) { console.error('boat settings resolve error', e); return; }
  _renderBoatSettingsPanel();
}

function _renderBoatSettingsPanel() {
  const body = document.getElementById('boat-settings-body');
  if (!_bsParams || !_bsResolved) return;

  // Build lookup: parameter name → resolved entry (current value)
  const byParam = {};
  for (const entry of _bsResolved) byParam[entry.parameter] = entry;

  // Build lookup: parameter name → all race-specific history entries
  const histByParam = {};
  if (_bsHistory) {
    for (const entry of _bsHistory) {
      if (!histByParam[entry.parameter]) histByParam[entry.parameter] = [];
      histByParam[entry.parameter].push(entry);
    }
  }

  const fmtTs = (ts) => {
    if (!ts) return '';
    const d = new Date(ts);
    return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  };

  const srcBadge = (entry) => {
    if (entry.race_id !== null) {
      const src = entry.source.startsWith('transcript') ? 'transcript' : entry.source;
      return '<span class="bs-source-badge ' + (entry.source.startsWith('transcript') ? 'transcript' : 'race') + '">' + esc(src) + '</span>';
    }
    return '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem">default</span>';
  };

  let html = '';

  for (const cat of _bsParams.categories) {
    // Crew weight row
    let crewWeightHtml = '';
    if (cat.category === 'crew' && _sessionCrew && _sessionCrew.length) {
      let totalBody = 0, totalGear = 0, hasW = false;
      for (const c of _sessionCrew) {
        if (c.body_weight != null) { totalBody += c.body_weight; hasW = true; }
        if (c.gear_weight != null) { totalGear += c.gear_weight; hasW = true; }
      }
      if (hasW) {
        const total = totalBody + totalGear;
        crewWeightHtml = '<div class="bs-row">'
          + '<span class="bs-label">Crew weight</span>'
          + '<span class="bs-value">' + total.toFixed(1) + '</span>'
          + '<span class="bs-unit">lbs</span>'
          + '<span style="color:var(--text-muted);font-size:.75rem;margin-left:6px">'
          + '(body ' + totalBody.toFixed(1) + ' + gear ' + totalGear.toFixed(1) + ')</span>'
          + '</div>';
      }
    }

    html += '<div class="setup-cat-header" onclick="toggleSetupCatSession(\'' + cat.category + '\')">';
    html += '<span class="setup-cat-label">' + esc(cat.label) + '</span>';
    html += '<span class="setup-cat-chevron" id="bs-cat-chev-' + cat.category + '">\u25BC</span>';
    html += '</div>';
    html += '<div class="setup-cat-body" id="bs-cat-' + cat.category + '">';
    html += crewWeightHtml;

    for (const p of cat.parameters) {
      const entry = byParam[p.name];
      const hist = histByParam[p.name] || [];
      const hasHistory = hist.length > 1 || (entry && entry.supersedes_value);
      const paramId = 'bs-hist-' + p.name;

      // Play button for transcript-sourced entries: compute audio offset from ts
      const playBtn = (e) => {
        if (!e.source || !e.source.startsWith('transcript') || !_session.audio_session_id || !e.ts || !_session.start_utc) return '';
        const offsetS = (Date.parse(e.ts) - Date.parse(_session.start_utc)) / 1000;
        if (offsetS < 0) return '';
        return '<button onclick="playSegmentAudio(' + offsetS.toFixed(1) + ',' + (offsetS + 8).toFixed(1) + ')" class="te-play-btn" title="Play transcript segment" style="margin-left:4px">&#9654;</button>';
      };

      // Current value row
      html += '<div class="bs-row" style="cursor:' + (hasHistory ? 'pointer' : 'default') + '"'
        + (hasHistory ? ' onclick="toggleBsHist(\'' + p.name + '\')"' : '') + '>';
      if (hasHistory) {
        html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-right:4px" id="bs-hist-chev-' + p.name + '">\u25B6</span>';
      }
      html += '<span class="bs-label">' + esc(p.label) + '</span>';
      if (entry) {
        html += '<span class="bs-value">' + esc(entry.value) + '</span>';
        if (p.unit) html += '<span class="bs-unit">' + esc(p.unit) + '</span>';
        html += srcBadge(entry);
        if (entry.ts) html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-left:6px" title="' + esc(entry.ts) + '">@ ' + fmtTs(entry.ts) + '</span>';
        html += playBtn(entry);
        if (hasHistory) html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-left:6px">(' + (hist.length + (entry.supersedes_value ? 1 : 0)) + ' entries)</span>';
      } else {
        html += '<span style="color:' + cssVar('--text-muted') + ';font-style:italic">not set</span>';
      }
      html += '</div>';

      // Collapsible history (hidden by default)
      if (hasHistory) {
        html += '<div id="' + paramId + '" style="display:none">';
        // Previous race-specific values, newest to oldest
        if (hist.length > 1) {
          for (let i = hist.length - 2; i >= 0; i--) {
            const h = hist[i];
            html += '<div class="bs-row" style="padding-left:24px;opacity:0.6">';
            html += '<span class="bs-label" style="font-size:.75rem">\u2514 previous</span>';
            html += '<span class="bs-value" style="font-size:.78rem">' + esc(h.value) + '</span>';
            if (p.unit) html += '<span class="bs-unit">' + esc(p.unit) + '</span>';
            html += srcBadge(h);
            if (h.ts) html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem;margin-left:6px" title="' + esc(h.ts) + '">@ ' + fmtTs(h.ts) + '</span>';
            html += playBtn(h);
            html += '</div>';
          }
        }
        // Superseded default at the bottom
        if (entry && entry.supersedes_value) {
          html += '<div class="bs-row" style="padding-left:24px;opacity:0.5">';
          html += '<span class="bs-label" style="font-size:.75rem">\u2514 default</span>';
          html += '<span class="bs-value" style="font-size:.78rem">' + esc(entry.supersedes_value) + '</span>';
          if (p.unit) html += '<span class="bs-unit">' + esc(p.unit) + '</span>';
          html += '<span style="color:' + cssVar('--text-muted') + ';font-size:.7rem">default</span>';
          html += '</div>';
        }
        html += '</div>';
      }
    }
    html += '</div>';
  }

  body.innerHTML = html;
}

function toggleBsHist(paramName) {
  const body = document.getElementById('bs-hist-' + paramName);
  const chev = document.getElementById('bs-hist-chev-' + paramName);
  if (!body) return;
  const hidden = body.style.display === 'none';
  body.style.display = hidden ? '' : 'none';
  if (chev) chev.textContent = hidden ? '\u25BC' : '\u25B6';
}

function toggleSetupCatSession(cat) {
  const body = document.getElementById('bs-cat-' + cat);
  const chev = document.getElementById('bs-cat-chev-' + cat);
  if (!body) return;
  const hidden = body.style.display === 'none';
  body.style.display = hidden ? '' : 'none';
  if (chev) chev.textContent = hidden ? '\u25BC' : '\u25B6';
}

// Called when the playback position changes (track click or video sync)
function _updateBoatSettingsForUtc(utcDate) {
  if (!_bsParams || !utcDate) return;
  const asOf = utcDate.toISOString();
  // Debounce: skip if same second
  if (_bsLastAsOf && _bsLastAsOf.slice(0, 19) === asOf.slice(0, 19)) return;
  _fetchAndRenderBoatSettings(asOf);
}

// ---------------------------------------------------------------------------
// Discussion threads (#282)
// ---------------------------------------------------------------------------

let _threads = [];
let _discussionMarkers = [];
// Currently-focused moment (open in the detail view). Renders as a larger,
// pulsing marker on the map so the user can see at a glance where on the
// track the moment they're reading is anchored. Null when the panel is
// showing the list view.
let _focusedMomentId = null;
function _setFocusedMoment(id) {
  if (_focusedMomentId === id) return;
  _focusedMomentId = id;
  if (_threads && _threads.length) _addDiscussionMarkers();
}

function _threadTitle(t) {
  if (t.title) return esc(t.title);
  const body = t.first_comment_body || (t.comments && t.comments.length ? t.comments[0].body : null);
  if (body) {
    // Titles/previews are plain text \u2014 strip markdown, don't render it (#809).
    const plain = stripMarkdown(body);
    return esc(plain.length > 60 ? plain.slice(0, 60) + '\u2026' : plain);
  }
  return 'Thread #' + t.id;
}

// Tag filter state for the Discussion card.
const _threadTagFilter = new Set();
let _threadTagMode = 'and';
let _threadAvailableTags = [];

async function loadMoments(opts) {
  const renderList = !opts || opts.renderList !== false;
  const card = document.getElementById('moments-card');
  card.style.display = '';
  const body = document.getElementById('moments-body');
  // Fetch anchor index in parallel so entity-ref chips can resolve labels
  _anchorIndex = null;
  const params = new URLSearchParams();
  if (_threadTagFilter.size) {
    params.set('tags', [..._threadTagFilter].join(','));
    params.set('tag_mode', _threadTagMode);
  }
  const qs = params.toString();
  const threadsUrl = '/api/sessions/' + SESSION_ID + '/moments' + (qs ? '?' + qs : '');
  const [threadsResp] = await Promise.all([
    fetch(threadsUrl),
    _ensureAnchorIndex(),
  ]);
  if (!threadsResp.ok) {
    if (renderList) body.innerHTML = '<span style="color:var(--text-secondary)">Failed to load</span>';
    return;
  }
  const data = await threadsResp.json();
  // The list endpoint now returns `moments`. Map each moment to the
  // thread-ish shape the existing discussion UI expects (title = subject,
  // unread_count not yet computed server-side for moments — treat as 0).
  _threads = (data.moments || []).map(m => ({
    id: m.id,
    session_id: m.session_id,
    anchor: m.anchor,
    title: m.subject,
    counterparty: m.counterparty || null,
    created_by: m.created_by,
    created_at: m.created_at,
    updated_at: m.updated_at,
    resolved: m.resolved,
    resolved_at: m.resolved_at,
    resolved_by: m.resolved_by,
    resolution_summary: m.resolution_summary,
    author_name: m.author_name,
    author_email: m.author_email,
    comment_count: m.comment_count || 0,
    unread_count: 0,
    first_comment_body: m.first_comment_body,
    tags: m.tags || [],
  }));
  _threadAvailableTags = data.available_tags || [];
  const totalUnread = _threads.reduce((s, t) => s + (t.unread_count || 0), 0);
  const badge = document.getElementById('moments-badge');
  badge.textContent = totalUnread > 0 ? '(' + totalUnread + ' unread)' : '';
  // List render → user is back to overview; drop the focus highlight.
  // (renderList:false is the deep-link init path, where openThread is in
  // flight and has set the focus — leave it alone.)
  if (renderList) _focusedMomentId = null;
  _addDiscussionMarkers();
  // When called with renderList:false (deep-link init), we've populated
  // _threads, _anchorIndex, the badge, and the map markers — but skip
  // body.innerHTML so the openThread() detail render isn't clobbered.
  if (!renderList) return;

  const filterBar = _renderThreadTagFilterRow();
  if (!_threads.length) {
    const emptyMsg = _threadTagFilter.size
      ? '<span style="color:var(--text-secondary)">No moments match the current tag filter.</span>'
      : '<span style="color:var(--text-secondary)">No moments on this session yet. Use + New Moment or the &#128278; Bookmark button while replaying.</span>';
    body.innerHTML = filterBar + emptyMsg;
    return;
  }
  const threadItems = _threads.map(t => {
    const anchor = _renderAnchorChip(t.anchor);
    const unread = t.unread_count > 0
      ? '<span class="thread-unread">' + t.unread_count + '</span>'
      : '';
    const resolved = t.resolved ? ' resolved' : '';
    const resolvedTag = t.resolved ? '<span style="color:var(--success);font-size:.7rem;margin-left:6px">&#10003; Resolved</span>' : '';
    const title = _threadTitle(t);
    const author = t.author_name || t.author_email || 'Crew Member';
    const count = t.comment_count === 1 ? '1 comment' : t.comment_count + ' comments';
    const resolutionHtml = t.resolved && t.resolution_summary
      ? '<div style="background:var(--bg-secondary);border:1px solid var(--success);border-radius:4px;padding:4px 8px;margin-top:4px;font-size:.72rem;color:var(--success)">'
        + '<strong>Resolution:</strong> ' + esc(t.resolution_summary) + '</div>'
      : '';
    const tagChips = _renderRowTagChipsInline(t.tags);
    const cpChip = _renderCounterpartyChip(t.counterparty, null);
    return '<div class="thread-item' + resolved + '" onclick="openThread(' + t.id + ')">'
      + '<div><strong style="color:var(--text-primary)">' + title + '</strong>' + cpChip + anchor + unread + resolvedTag + '</div>'
      + '<div style="font-size:.72rem;color:var(--text-secondary);margin-top:2px">' + esc(author) + ' &middot; ' + count + ' &middot; ' + fmtTime(t.created_at) + '</div>'
      + resolutionHtml
      + tagChips
      + '</div>';
  }).join('');
  body.innerHTML = filterBar + threadItems;
}

function _renderThreadTagFilterRow() {
  const byId = new Map();
  for (const t of _threadAvailableTags) {
    byId.set(t.id, {id: t.id, name: t.name, color: t.color, count: t.count || 0});
  }
  for (const tid of _threadTagFilter) {
    if (!byId.has(tid)) byId.set(tid, {id: tid, name: '#' + tid, color: null, count: 0});
  }
  if (byId.size === 0) return '';
  const sorted = [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  const chips = sorted.map(t => {
    const active = _threadTagFilter.has(t.id);
    const swatch = t.color ? `<span class="hist-tag-chip-swatch" style="background:${t.color}"></span>` : '';
    return `<span class="session-tag-chip${active ? ' active' : ''}" onclick="event.stopPropagation();_toggleThreadTagFilter(${t.id})">${swatch}${esc(t.name)} <span class="session-tag-count">(${t.count})</span></span>`;
  }).join('');
  const dim = _threadTagFilter.size < 2 ? ';opacity:.6' : '';
  const modeToggle = `<span class="session-tag-mode" style="margin-left:6px${dim}">`
    + `<button class="${_threadTagMode === 'and' ? 'active' : ''}" onclick="event.stopPropagation();_setThreadTagMode('and')" title="Require every selected tag">all</button>`
    + `<button class="${_threadTagMode === 'or' ? 'active' : ''}" onclick="event.stopPropagation();_setThreadTagMode('or')" title="Match any selected tag">any</button>`
    + '</span>';
  const clear = _threadTagFilter.size
    ? '<a href="#" onclick="event.preventDefault();event.stopPropagation();_clearThreadTagFilter()" style="font-size:.7rem;color:var(--text-secondary);margin-left:6px">clear</a>'
    : '';
  return '<div class="session-tag-filter-row">'
    + '<span class="session-tag-label">Tags</span>' + chips + modeToggle + clear
    + '</div>';
}

function _toggleThreadTagFilter(id) {
  if (_threadTagFilter.has(id)) _threadTagFilter.delete(id);
  else _threadTagFilter.add(id);
  loadMoments();
}
function _setThreadTagMode(m) { _threadTagMode = m; loadMoments(); }
function _clearThreadTagFilter() { _threadTagFilter.clear(); loadMoments(); }

function _renderRowTagChipsInline(tags) {
  if (!tags || !tags.length) return '';
  const chips = tags.map(t => {
    const swatch = t.color ? `<span class="hist-tag-chip-swatch" style="background:${t.color}"></span>` : '';
    return `<span class="session-tag-chip" style="cursor:default;font-size:.66rem">${swatch}${esc(t.name)}</span>`;
  }).join(' ');
  return '<div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:3px">' + chips + '</div>';
}

function seekToThreadAnchor(ts) {
  if (!ts) return;
  const utc = new Date(ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z');
  if (isNaN(utc.getTime())) return;
  _seekTo(utc, 'thread');
}

// Resolve an entity-ref anchor (maneuver / bookmark / race / start) to a
// clickable seek target. Uses the session-wide anchor index fetched when
// the discussion panel loads, avoiding one /anchors fetch per thread.
let _anchorIndex = null; // {kind: {entity_id: {t_start, label}}}

async function _ensureAnchorIndex() {
  if (_anchorIndex) return _anchorIndex;
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/anchors');
    if (!r.ok) { _anchorIndex = {}; return _anchorIndex; }
    const rows = await r.json();
    _anchorIndex = {};
    for (const a of rows) {
      if (!_anchorIndex[a.kind]) _anchorIndex[a.kind] = {};
      _anchorIndex[a.kind][a.entity_id] = {t_start: a.t_start, label: a.label};
    }
  } catch { _anchorIndex = {}; }
  return _anchorIndex;
}

function _anchorSeekTime(anchor) {
  if (!anchor) return null;
  if (anchor.kind === 'timestamp' || anchor.kind === 'segment') return anchor.t_start || null;
  if (!_anchorIndex) return null;
  const resolved = _anchorIndex[anchor.kind] && _anchorIndex[anchor.kind][anchor.entity_id];
  return resolved ? resolved.t_start : null;
}

function _anchorDisplayLabel(anchor) {
  if (!anchor) return '';
  if (anchor.kind === 'timestamp') return fmtTime(anchor.t_start);
  if (anchor.kind === 'segment') return fmtTime(anchor.t_start) + '\u2013' + fmtTime(anchor.t_end);
  if (_anchorIndex) {
    const resolved = _anchorIndex[anchor.kind] && _anchorIndex[anchor.kind][anchor.entity_id];
    if (resolved) return resolved.label;
  }
  return anchor.kind;
}

function _renderAnchorChip(anchor) {
  if (!anchor) return '';
  const seekTo = _anchorSeekTime(anchor);
  const label = esc(_anchorDisplayLabel(anchor));
  if (seekTo) {
    return '<span class="thread-anchor" style="cursor:pointer;text-decoration:underline" '
      + 'onclick="event.stopPropagation();seekToThreadAnchor(\'' + esc(seekTo) + '\')" '
      + 'title="Seek playback to this moment">' + label + '</span>';
  }
  return '<span class="thread-anchor">' + label + '</span>';
}

// Cursor-vs-anchor match predicate — mirrors anchor_match.py for the
// client-side highlight pass. Returns true if `cursor` (Date) is within
// the anchor's active window. Maneuver/bookmark/start lookups come from
// _anchorIndex (populated by _ensureAnchorIndex).
const _ANCHOR_MATCH_WINDOW_S = 15;
const _ANCHOR_MATCH_START_WINDOW_S = 60;

function _parseUtc(s) {
  if (!s) return null;
  const d = new Date(s.endsWith('Z') || s.includes('+') ? s : s + 'Z');
  return isNaN(d.getTime()) ? null : d;
}

function anchorMatchesCursor(anchor, cursor) {
  if (!anchor || !cursor) return false;
  const k = anchor.kind;
  if (k === 'timestamp') {
    const t = _parseUtc(anchor.t_start);
    return !!t && Math.abs((cursor - t) / 1000) <= _ANCHOR_MATCH_WINDOW_S;
  }
  if (k === 'segment') {
    const s = _parseUtc(anchor.t_start), e = _parseUtc(anchor.t_end);
    return !!s && !!e && cursor >= s && cursor < e;
  }
  if (k === 'race') return true;
  if (!_anchorIndex) return false;
  const entry = _anchorIndex[k] && _anchorIndex[k][anchor.entity_id];
  if (!entry) return false;
  const base = _parseUtc(entry.t_start);
  if (!base) return false;
  if (k === 'start') {
    return Math.abs((cursor - base) / 1000) <= _ANCHOR_MATCH_START_WINDOW_S;
  }
  // maneuver / bookmark — point anchors with a ±15s window
  return Math.abs((cursor - base) / 1000) <= _ANCHOR_MATCH_WINDOW_S;
}

function _refreshThreadHighlights(utc) {
  if (!utc || !_threads || !_threads.length) return;
  const active = new Set();
  for (const t of _threads) {
    if (anchorMatchesCursor(t.anchor, utc)) active.add(t.id);
  }
  const items = document.querySelectorAll('.thread-item');
  items.forEach(el => {
    const m = el.getAttribute('onclick') && el.getAttribute('onclick').match(/openThread\((\d+)\)/);
    if (!m) return;
    const id = parseInt(m[1], 10);
    el.classList.toggle('thread-active', active.has(id));
  });
}

registerSurface('threads', function(utc) { _refreshThreadHighlights(utc); });

function _checkThreadHash() {
  // Prefer query params (?moment=<id>&comment=<id>) — survive Slack unfurls.
  // ?thread= is accepted as a legacy alias from links generated before #663.
  // Fallback to #thread-<id> fragment for backwards compat.
  const params = new URLSearchParams(window.location.search);
  const momentParam = params.get('moment') || params.get('thread');
  const commentParam = params.get('comment');
  if (momentParam) {
    const momentId = parseInt(momentParam, 10);
    if (!isNaN(momentId)) {
      const commentId = commentParam ? parseInt(commentParam, 10) : null;
      openThread(momentId, commentId && !isNaN(commentId) ? commentId : null);
      return;
    }
  }
  const m = window.location.hash.match(/^#thread-(\d+)(?:-comment-(\d+))?$/);
  if (m) {
    const threadId = parseInt(m[1], 10);
    const commentId = m[2] ? parseInt(m[2], 10) : null;
    openThread(threadId, commentId);
  }
}

function _threadShareUrl(threadId, commentId) {
  const url = new URL(window.location.href);
  url.hash = '';
  url.searchParams.delete('thread');
  url.searchParams.delete('moment');
  url.searchParams.delete('comment');
  url.searchParams.set('moment', String(threadId));
  if (commentId) url.searchParams.set('comment', String(commentId));
  return url.toString();
}

async function copyThreadLink(threadId, commentId, btn) {
  const link = _threadShareUrl(threadId, commentId);
  try {
    await navigator.clipboard.writeText(link);
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }
  } catch (e) {
    prompt('Copy this link:', link);
  }
}

function _flashHighlight(el) {
  if (!el) return;
  el.classList.add('flash-highlight');
  setTimeout(() => el.classList.remove('flash-highlight'), 2200);
}

function _addDiscussionMarkers() {
  _discussionMarkers.forEach(m => m.remove());
  _discussionMarkers = [];
  if (!_map || !_trackData) return;

  _threads.forEach(t => {
    const seekTo = _anchorSeekTime(t.anchor);
    if (!seekTo) return;
    const ts = new Date(seekTo.endsWith('Z') || seekTo.includes('+') ? seekTo : seekTo + 'Z');
    const idx = _indexForUtc(ts);
    const latLng = _trackData.latLngs[idx];
    if (!latLng) return;

    const title = _threadTitle(t);
    const unread = t.unread_count > 0 ? ' <span class="thread-unread">' + t.unread_count + '</span>' : '';
    const resolvedHtml = t.resolved
      ? '<div style="color:var(--success);font-size:.7rem;margin-top:2px">&#10003; Resolved</div>'
        + (t.resolution_summary
          ? '<div style="background:var(--bg-secondary);border:1px solid var(--success);border-radius:4px;padding:4px 6px;margin-top:3px;font-size:.7rem;color:var(--success)">'
            + esc(t.resolution_summary.length > 120 ? t.resolution_summary.slice(0, 120) + '\u2026' : t.resolution_summary) + '</div>'
          : '')
      : '';
    const author = t.author_name || t.author_email || 'Crew Member';
    const count = t.comment_count === 1 ? '1 comment' : t.comment_count + ' comments';

    const popup = '<div style="max-width:260px">'
      + '<div style="font-weight:600;color:var(--text-primary);font-size:.82rem">' + title + unread + '</div>'
      + '<div style="font-size:.7rem;color:var(--text-secondary)">' + esc(author) + ' &middot; ' + count + ' &middot; ' + esc(_anchorDisplayLabel(t.anchor)) + '</div>'
      + resolvedHtml
      + '<div id="discussion-marker-preview-' + t.id + '">'
      + '<div style="font-size:.7rem;color:var(--text-secondary);margin-top:4px">Loading\u2026</div></div>'
      + '<div style="margin-top:6px"><a href="#" data-open-thread="' + t.id + '" '
      + 'style="color:var(--accent);font-size:.78rem;text-decoration:none">Open moment &rarr;</a></div>'
      + '</div>';

    const hasUnread = t.unread_count > 0;
    const markerColor = t.resolved ? cssVar('--success') : hasUnread ? cssVar('--accent') : cssVar('--text-secondary');
    const bgPrimary = cssVar('--bg-primary');
    const markerStyle = t.resolved
      ? 'width:14px;height:14px;background:transparent;border:2px solid ' + cssVar('--success') + ';border-radius:50%'
      : 'width:14px;height:14px;background:' + markerColor + ';border:2px solid ' + bgPrimary + ';border-radius:50%;box-shadow:0 0 4px ' + markerColor;
    const isFocused = (t.id === _focusedMomentId);
    const icon = L.divIcon({
      className: isFocused ? 'discussion-marker focused' : 'discussion-marker',
      html: isFocused
        ? '<div class="moment-marker-focused-dot"></div>'
        : '<div style="' + markerStyle + '"></div>',
      iconSize: isFocused ? [22, 22] : [14, 14],
      iconAnchor: isFocused ? [11, 11] : [7, 7],
    });
    const threadId = t.id;
    const marker = L.marker(latLng, {icon: icon})
      .addTo(_map)
      .bindPopup(popup, {maxWidth: 280, minWidth: 200});
    marker.on('popupopen', function() {
      _loadMarkerPreview(threadId);
      // Wire up the "Open thread" link after Leaflet renders the popup DOM
      const popupEl = marker.getPopup().getElement();
      if (popupEl) {
        const link = popupEl.querySelector('[data-open-thread]');
        if (link) {
          link.addEventListener('click', function(ev) {
            ev.preventDefault();
            marker.closePopup();
            openThread(threadId);
            document.getElementById('moments-card').scrollIntoView({behavior: 'smooth', block: 'start'});
          });
        }
      }
    });
    _discussionMarkers.push(marker);
  });
}

async function _loadMarkerPreview(threadId) {
  const el = document.getElementById('discussion-marker-preview-' + threadId);
  if (!el) return;
  const r = await fetch('/api/moments/' + threadId);
  if (!r.ok) { el.innerHTML = ''; return; }
  const t = await r.json();
  const comments = (t.comments || []).slice(-3);
  if (!comments.length) {
    el.innerHTML = '<div style="font-size:.72rem;color:var(--text-secondary);margin-top:4px">No comments yet</div>';
    return;
  }
  el.innerHTML = comments.map(c => {
    const a = c.author_name || c.author_email || 'Crew Member';
    const plain = stripMarkdown(c.body);
    const body = plain.length > 100 ? plain.slice(0, 100) + '\u2026' : plain;
    return '<div style="margin-top:4px;font-size:.72rem;border-left:2px solid ' + cssVar('--border') + ';padding-left:6px">'
      + '<span style="color:' + cssVar('--accent') + ';font-weight:600">' + esc(a) + '</span> '
      + '<span style="color:var(--text-primary)">' + esc(body) + '</span></div>';
  }).join('');
}

// Open the New Moment form in the Moments panel. `prefill` may be:
//   - a string ISO timestamp \u2192 preselect as a timestamp anchor
//   - an anchor object {kind, entity_id?, t_start?, label?} \u2192 preselect verbatim
//   - falsy \u2192 no preselection (user picks in the picker)
function showNewMomentForm(prefill) {
  // Make sure the Moments card is visible and the page scrolls to it so the
  // user always sees the form they just opened (important when creation was
  // triggered from the map popup or the maneuvers table far below).
  const card = document.getElementById('moments-card');
  if (card) {
    card.style.display = '';
    card.scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  const body = document.getElementById('moments-body');
  const form = document.createElement('div');
  form.className = 'thread-form';
  form.style.marginBottom = '10px';
  const cursor = _playClock.positionUtc ? _playClock.positionUtc.toISOString() : null;
  form.innerHTML = ''
    + '<div style="display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap">'
    + '<input id="new-thread-title" placeholder="Subject (e.g. windshift, close call)" style="flex:2;min-width:180px"/>'
    + '<input id="new-thread-counterparty" list="new-thread-cp-list" placeholder="Counterparty (sail #, e.g. USA 123)" style="flex:1;min-width:140px"/>'
    + '<datalist id="new-thread-cp-list"></datalist>'
    + '</div>'
    + '<div style="margin-bottom:6px;font-size:.72rem;color:var(--text-secondary)">'
    + 'Anchor (optional):'
    + '</div>'
    + '<anchor-picker id="new-thread-anchor-picker" session-id="' + esc(SESSION_ID) + '"></anchor-picker>'
    + '<textarea id="new-thread-body" placeholder="First comment\u2026" style="margin-top:8px"></textarea>'
    + '<div style="margin-top:6px;display:flex;gap:6px">'
    + '<button class="btn-thread" onclick="submitNewThread()">Create Moment</button>'
    + '<button class="btn-thread" style="background:none;color:var(--text-secondary)" onclick="loadMoments()">Cancel</button>'
    + '</div>';
  body.prepend(form);
  _populateCounterpartyDatalist('new-thread-cp-list');
  const picker = document.getElementById('new-thread-anchor-picker');
  if (picker) {
    picker.fallbackCursor = cursor;
    if (prefill) {
      const anchor = typeof prefill === 'string'
        ? {kind: 'timestamp', t_start: prefill, label: fmtTime(prefill)}
        : prefill;
      setTimeout(() => picker._pickAnchor(anchor), 0);
    }
  }
}

// Open the New Moment form prefilled with a maneuver anchor. Used by the
// maneuver map-popup "+ Create moment" button and the maneuver-table action.
function createMomentFromManeuver(idx) {
  const m = _maneuvers && _maneuvers[idx];
  if (!m || m.id == null) return;
  // Close any open map popup so the maneuver's popup doesn't hang around
  // over the track while the user fills out the form below.
  if (_map && typeof _map.closePopup === 'function') _map.closePopup();
  const label = (m.type || 'maneuver') + (m.ts ? ' at ' + fmtTime(m.ts) : '');
  showNewMomentForm({kind: 'maneuver', entity_id: m.id, label: label});
}

async function submitNewThread() {
  const title = document.getElementById('new-thread-title').value.trim();
  const cpEl = document.getElementById('new-thread-counterparty');
  const counterparty = cpEl ? cpEl.value.trim() : '';
  const picker = document.getElementById('new-thread-anchor-picker');
  let anchor = picker ? picker.value : null;
  // Fallback: if the user didn't pick anything and the replay has a
  // cursor, anchor at that timestamp. Matches the picker's Enter-on-empty
  // behaviour for users who click Create Thread without touching the picker.
  if (!anchor && _playClock.positionUtc) {
    anchor = {kind: 'timestamp', t_start: _playClock.positionUtc.toISOString()};
  }
  const firstComment = document.getElementById('new-thread-body').value.trim();
  // Flatten the anchor object into the moment-create body. Default to
  // session-scope if the user hasn't picked an anchor or set a cursor.
  const payload = {anchor_kind: 'session'};
  if (title) payload.subject = title;
  if (counterparty) payload.counterparty = counterparty;
  if (firstComment) payload.first_comment = firstComment;
  if (anchor && anchor.kind) {
    payload.anchor_kind = anchor.kind;
    if (anchor.entity_id !== undefined && anchor.entity_id !== null) {
      payload.anchor_entity_id = anchor.entity_id;
    }
    if (anchor.t_start) payload.t_start = anchor.t_start;
    if (anchor.t_end) payload.t_end = anchor.t_end;
  }
  const r = await fetch('/api/sessions/' + SESSION_ID + '/moments', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    alert('Failed to create moment: ' + (detail.detail || r.status));
    return;
  }
  if (counterparty) _counterpartyCache = null;
  const {id} = await r.json();
  openThread(id);
}

async function openThread(threadId, scrollToCommentId) {
  const body = document.getElementById('moments-body');
  body.innerHTML = '<span style="color:var(--text-secondary)">Loading\u2026</span>';
  // Promote this moment's map marker to the focused (large + pulsing) style
  // so the user can see at a glance where on the track the open moment is
  // anchored. Cleared when loadMoments() repaints the list view.
  _setFocusedMoment(threadId);
  // Mark as read
  fetch('/api/moments/' + threadId + '/read', {method: 'POST'});
  const r = await fetch('/api/moments/' + threadId);
  if (!r.ok) { loadMoments(); return; }
  const t = await r.json();
  await _ensureAnchorIndex();
  const anchor = _renderAnchorChip(t.anchor);
  let resolveBtn = '';
  if (t.resolved) {
    resolveBtn = '<button class="btn-unresolve" onclick="unresolveThread(' + t.id + ')">Unresolve</button>';
  } else {
    resolveBtn = '<button class="btn-resolve" onclick="resolveThread(' + t.id + ')">Resolve</button>';
  }
  const resolutionHtml = t.resolved && t.resolution_summary
    ? '<div style="background:var(--bg-secondary);border:1px solid var(--success);border-radius:4px;padding:6px 8px;margin-top:6px;font-size:.78rem;color:var(--success)">'
      + '<strong>Resolution:</strong> ' + esc(t.resolution_summary) + '</div>'
    : '';
  const commentsHtml = (t.comments || []).map(c => {
    const author = c.author_name || c.author_email || 'Crew Member';
    const edited = c.edited_at ? ' <span class="comment-edited">(edited)</span>' : '';
    // Show the edit control only to the comment's author or an admin \u2014 the
    // backend enforces the same rule (routes/comments.py). A moment's body is
    // its first comment, so this uniformly covers the original post and replies.
    const canEdit = (c.author != null && c.author === _currentUserId) || _userRole === 'admin';
    const editBtn = canEdit
      ? ' <button class="btn-edit-comment" title="Edit comment" '
        + 'onclick="editComment(' + t.id + ',' + c.id + ')">\u270e</button>'
      : '';
    return '<div class="comment-item" id="comment-' + c.id + '" data-raw="' + esc(c.body) + '">'
      + '<span class="comment-author">' + esc(author) + '</span>'
      + '<span class="comment-time">' + fmtTime(c.created_at) + '</span>' + edited
      + ' <button class="btn-copy-link" title="Copy link to this comment" '
      + 'onclick="copyThreadLink(' + t.id + ',' + c.id + ',this)">\ud83d\udd17</button>'
      + editBtn
      + '<div class="comment-body">' + renderMarkdown(c.body) + '</div>'
      + '</div>';
  }).join('');
  const copyThreadBtn = '<button class="btn-copy-link" title="Copy link to this moment" '
    + 'onclick="copyThreadLink(' + t.id + ',null,this)">\ud83d\udd17 Copy link</button>';
  const subjectText = t.subject || '';
  const subjectDisplay = subjectText ? esc(subjectText) : '<em style="color:var(--text-secondary);font-weight:400">Untitled</em>';
  const subjectSpan = '<span class="moment-subject-editable" id="moment-subject-' + t.id + '"'
    + ' data-raw="' + esc(subjectText) + '"'
    + ' onclick="_editMomentSubject(' + t.id + ')"'
    + ' title="Click to edit subject"'
    + ' style="color:var(--text-primary);font-size:.9rem;font-weight:600">'
    + subjectDisplay + '</span>';
  const cpChip = _renderCounterpartyChip(t.counterparty, {momentId: t.id, editable: true});
  const galleryHtml = _renderMomentAttachmentsSection(t);
  body.innerHTML = '<div style="margin-bottom:8px">'
    + '<button style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:.78rem;padding:0" onclick="loadMoments()">&larr; All moments</button>'
    + '</div>'
    + '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
    + '<div style="flex:1;min-width:0">' + subjectSpan + cpChip + anchor + '</div>'
    + '<div style="flex-shrink:0;display:flex;gap:6px">' + copyThreadBtn + resolveBtn + '</div>'
    + '</div>'
    + resolutionHtml
    + galleryHtml
    + '<div style="margin-top:4px;margin-bottom:6px">'
    + '<div style="font-size:.7rem;color:var(--text-secondary);margin-bottom:3px">Tags</div>'
    + '<tag-picker entity-type="moment" entity-id="' + t.id + '"></tag-picker>'
    + '</div>'
    + '<div id="thread-comments">' + (commentsHtml || '<span style="color:var(--text-secondary)">No comments yet</span>') + '</div>'
    + '<div class="thread-form" style="margin-top:8px">'
    + '<textarea id="reply-body" placeholder="Reply\u2026"></textarea>'
    + '<div style="margin-top:4px"><button class="btn-thread" onclick="submitReply(' + t.id + ')">Reply</button></div>'
    + '</div>';
  const card = document.getElementById('moments-card');
  _scrollDeepLinkTarget(card, scrollToCommentId);
}

// Scroll behavior for deep-linked threads:
// - Default: pin the thread header (first message) to the top of the viewport.
// - If a specific comment was requested, still prefer top-of-thread — but only
//   if the target comment would actually be visible in the viewport at that
//   scroll position. Otherwise scroll the comment fully into view.
//
// The #track-container card (and other async-loading content above the
// moments card) keeps growing for up to ~2s after init() fires. A single
// RAF-scheduled scroll lands above the final card position. Re-measure and
// correct the scroll up to a 2.5s deadline or until the target is stable.
function _scrollDeepLinkTarget(card, scrollToCommentId) {
  if (!card) return;
  const computeTarget = () => {
    const cardTop = card.getBoundingClientRect().top + window.scrollY;
    const viewportH = window.innerHeight;
    let highlight = card;
    let scrollY = cardTop;
    if (scrollToCommentId) {
      const t = document.getElementById('comment-' + scrollToCommentId);
      if (t) {
        highlight = t;
        const tRect = t.getBoundingClientRect();
        const tTop = tRect.top + window.scrollY;
        const tBottom = tTop + tRect.height;
        if (tBottom > cardTop + viewportH) {
          scrollY = tTop - Math.max(0, (viewportH - tRect.height) / 2);
        }
      }
    }
    return {scrollY: Math.max(0, scrollY), highlight};
  };

  // Kill ongoing correction if the user scrolls or we navigate away.
  let cancelled = false;
  const cancel = () => { cancelled = true; };
  window.addEventListener('wheel', cancel, {once: true, passive: true});
  window.addEventListener('touchstart', cancel, {once: true, passive: true});
  window.addEventListener('keydown', cancel, {once: true});

  requestAnimationFrame(() => {
    const {scrollY, highlight} = computeTarget();
    window.scrollTo({top: scrollY, behavior: 'smooth'});
    _flashHighlight(highlight);

    // Correction loop — re-measure for up to 2.5s; if the target has
    // shifted by more than a few px, snap to it (instant, not smooth —
    // repeated smooth-scrolls stack and feel jumpy). Stop early once the
    // position has been stable for ~400ms.
    const deadline = performance.now() + 2500;
    let lastY = scrollY;
    let stableSince = null;
    const tick = () => {
      if (cancelled) return;
      const now = performance.now();
      if (now > deadline) return;
      const {scrollY: newY} = computeTarget();
      if (Math.abs(newY - lastY) > 3) {
        window.scrollTo({top: newY, behavior: 'auto'});
        lastY = newY;
        stableSince = null;
      } else if (stableSince == null) {
        stableSince = now;
      } else if (now - stableSince > 400) {
        return;
      }
      setTimeout(tick, 120);
    };
    // Start after the initial smooth-scroll has had a chance to start.
    setTimeout(tick, 250);
  });
}

// ---------------------------------------------------------------------------
// Counterparty chip + typeahead (#677)
// ---------------------------------------------------------------------------
//
// Typeahead source is the boats registry (populated by results imports).
// Previously-used free-text counterparty values are merged in as a fallback
// so early users / non-competitor values (e.g. "mark boat") still surface.
// The counterparty column itself stays free-text — issue #656 tracks the
// future FK promotion once real usage patterns are in.

let _counterpartyCache = null;

async function _loadCounterparties() {
  if (_counterpartyCache) return _counterpartyCache;
  const entries = [];
  const seen = new Set();
  try {
    const r = await fetch('/api/boats');
    if (r.ok) {
      const boats = await r.json();
      for (const b of boats || []) {
        const sail = (b.sail_number || '').trim();
        if (!sail || seen.has(sail)) continue;
        seen.add(sail);
        entries.push({value: sail, label: b.name ? sail + ' — ' + b.name : sail});
      }
    }
  } catch (e) { /* fall through to counterparty strings */ }
  try {
    const r = await fetch('/api/moments/counterparties');
    if (r.ok) {
      const {counterparties} = await r.json();
      for (const c of counterparties || []) {
        const v = (c || '').trim();
        if (!v || seen.has(v)) continue;
        seen.add(v);
        entries.push({value: v, label: v});
      }
    }
  } catch (e) { /* ignore */ }
  _counterpartyCache = entries;
  return entries;
}

async function _populateCounterpartyDatalist(datalistId) {
  const list = await _loadCounterparties();
  const dl = document.getElementById(datalistId);
  if (!dl) return;
  // Put sail number in `value` (what gets saved) and the richer
  // "sail — name" string in the option body so users see both while
  // typing. Most browsers show the option body; Safari shows value only.
  dl.innerHTML = list
    .map(e => '<option value="' + esc(e.value) + '">' + esc(e.label) + '</option>')
    .join('');
}

// Render a counterparty chip. `opts.editable` makes it clickable in the
// detail view (or renders a "+ counterparty" stub when value is empty).
function _renderCounterpartyChip(value, opts) {
  const editable = !!(opts && opts.editable);
  const momentId = opts && opts.momentId;
  if (!value) {
    if (!editable) return '';
    return '<button type="button" class="moment-counterparty-add"'
      + ' id="moment-cp-' + momentId + '"'
      + ' onclick="event.stopPropagation();_editMomentCounterparty(' + momentId + ')">'
      + '+ counterparty</button>';
  }
  const cls = editable ? 'moment-counterparty-chip editable' : 'moment-counterparty-chip';
  const attr = editable
    ? ' id="moment-cp-' + momentId + '" onclick="event.stopPropagation();_editMomentCounterparty(' + momentId + ')" title="Click to edit"'
    : '';
  return '<span class="' + cls + '" data-raw="' + esc(value) + '"' + attr + '>'
    + esc(value) + '</span>';
}

// ---------------------------------------------------------------------------
// Inline subject + counterparty edit (#677)
// ---------------------------------------------------------------------------

function _editMomentSubject(momentId) {
  const el = document.getElementById('moment-subject-' + momentId);
  if (!el || el.tagName === 'INPUT') return;
  const current = el.getAttribute('data-raw') || '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'moment-inline-edit';
  input.value = current;
  input.placeholder = 'Subject (optional)';
  input.id = 'moment-subject-' + momentId;
  input.style.fontSize = '.9rem';
  input.style.fontWeight = '600';
  input.style.width = 'min(100%, 360px)';
  el.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newVal = input.value.trim();
    if (commit && newVal !== current) {
      const r = await fetch('/api/moments/' + momentId, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({subject: newVal || null}),
      });
      if (!r.ok) {
        alert(r.status === 403
          ? 'Only the author (or an admin) can edit this moment.'
          : 'Failed to update subject (HTTP ' + r.status + ')');
      }
    }
    openThread(momentId);
  };
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
}

function _editMomentCounterparty(momentId) {
  const el = document.getElementById('moment-cp-' + momentId);
  if (!el || el.tagName === 'INPUT') return;
  const current = el.getAttribute('data-raw') || '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'moment-inline-edit';
  input.value = current;
  input.placeholder = 'Counterparty';
  input.setAttribute('list', 'moment-cp-datalist-' + momentId);
  input.id = 'moment-cp-' + momentId;
  input.style.fontSize = '.78rem';
  input.style.width = '160px';
  input.style.marginLeft = '6px';
  const dl = document.createElement('datalist');
  dl.id = 'moment-cp-datalist-' + momentId;
  el.replaceWith(input);
  input.after(dl);
  _populateCounterpartyDatalist(dl.id);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newVal = input.value.trim();
    if (commit && newVal !== current) {
      const r = await fetch('/api/moments/' + momentId, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({counterparty: newVal || null}),
      });
      if (!r.ok) {
        alert(r.status === 403
          ? 'Only the author (or an admin) can edit this moment.'
          : 'Failed to update counterparty (HTTP ' + r.status + ')');
      } else {
        _counterpartyCache = null;
      }
    }
    openThread(momentId);
  };
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
}

// ---------------------------------------------------------------------------
// Attachment gallery (#677)
// ---------------------------------------------------------------------------

function _renderMomentAttachmentsSection(t) {
  const photos = (t.attachments || []).filter(a => a.kind === 'photo');
  const uploadInputId = 'moment-photo-file-' + t.id;
  const uploadBtn = ''
    + '<input type="file" id="' + uploadInputId + '" accept="image/*" style="display:none"'
    + ' onchange="_uploadMomentPhoto(' + t.id + ',this)"/>'
    + '<button type="button" class="btn-moment-upload"'
    + ' onclick="document.getElementById(\'' + uploadInputId + '\').click()">'
    + '+ Photo</button>';
  if (!photos.length) {
    return '<div style="margin-top:6px">' + uploadBtn + '</div>';
  }
  const thumbs = photos.map(a => {
    const url = '/attachments/' + a.path.split('/').map(encodeURIComponent).join('/');
    return '<div class="moment-photo-thumb">'
      + '<img src="' + esc(url) + '" alt=""'
      + ' onclick="_enlargeMomentPhoto(\'' + esc(url) + '\')"/>'
      + '<button type="button" class="btn-photo-delete" title="Delete photo"'
      + ' onclick="_deleteMomentAttachment(' + t.id + ',' + a.id + ')">×</button>'
      + '</div>';
  }).join('');
  return '<div class="moment-gallery">' + thumbs + '</div>'
    + '<div style="margin-top:6px">' + uploadBtn + '</div>';
}

function _enlargeMomentPhoto(url) {
  const overlay = document.createElement('div');
  overlay.className = 'moment-photo-overlay';
  overlay.innerHTML = '<img src="' + esc(url) + '" alt=""/>';
  const close = () => overlay.remove();
  overlay.addEventListener('click', close);
  const onKey = (e) => {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }
  };
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);
}

async function _uploadMomentPhoto(momentId, input) {
  if (!input.files || !input.files[0]) return;
  const file = input.files[0];
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch('/api/moments/' + momentId + '/attachments', {
    method: 'POST', body: fd,
  });
  if (!r.ok) {
    const mb = (file.size / 1048576).toFixed(1);
    alert(r.status === 413
      ? 'Photo too large (' + mb + ' MB). Maximum upload size is 20 MB.'
      : 'Upload failed (HTTP ' + r.status + '). Please try again.');
    return;
  }
  input.value = '';
  openThread(momentId);
}

async function _deleteMomentAttachment(momentId, attachmentId) {
  if (!confirm('Delete this photo?')) return;
  const r = await fetch('/api/attachments/' + attachmentId, {method: 'DELETE'});
  if (!r.ok && r.status !== 404) {
    alert('Delete failed (HTTP ' + r.status + ').');
    return;
  }
  openThread(momentId);
}

async function submitReply(threadId) {
  const el = document.getElementById('reply-body');
  const text = el.value.trim();
  if (!text) return;
  const r = await fetch('/api/moments/' + threadId + '/comments', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({body: text})
  });
  if (!r.ok) { alert('Failed to post reply'); return; }
  openThread(threadId);
}

// ---------------------------------------------------------------------------
// Comment editing (#809) — author-or-admin, in-place textarea over the body.
// The raw markdown is stashed on the comment element's data-raw attribute at
// render time so edit/cancel need no extra fetch.
// ---------------------------------------------------------------------------

function editComment(momentId, commentId) {
  const item = document.getElementById('comment-' + commentId);
  const bodyEl = item && item.querySelector('.comment-body');
  if (!bodyEl) return;
  if (item.querySelector('.comment-edit-textarea')) return;  // already editing
  const raw = item.dataset.raw || '';
  const editBtn = item.querySelector('.btn-edit-comment');
  if (editBtn) editBtn.style.display = 'none';
  bodyEl.innerHTML = '';
  const ta = document.createElement('textarea');
  ta.className = 'comment-edit-textarea';
  ta.value = raw;
  bodyEl.appendChild(ta);
  const actions = document.createElement('div');
  actions.className = 'comment-edit-actions';
  actions.innerHTML =
    '<button class="btn-thread" onclick="_saveCommentEdit(' + momentId + ',' + commentId + ')">Save</button>'
    + '<button class="btn-thread comment-edit-cancel" '
    + 'onclick="_cancelCommentEdit(' + commentId + ')">Cancel</button>';
  bodyEl.appendChild(actions);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

async function _saveCommentEdit(momentId, commentId) {
  const item = document.getElementById('comment-' + commentId);
  const ta = item && item.querySelector('.comment-edit-textarea');
  if (!ta) return;
  const text = ta.value.trim();
  if (!text) { alert('Comment cannot be empty'); return; }
  const r = await fetch('/api/comments/' + commentId, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({body: text}),
  });
  if (!r.ok) { alert('Failed to save edit'); return; }
  // Reload the thread so the fresh (edited) marker + re-render come from the
  // server (matches submitReply / resolve).
  openThread(momentId);
}

function _cancelCommentEdit(commentId) {
  const item = document.getElementById('comment-' + commentId);
  const bodyEl = item && item.querySelector('.comment-body');
  if (!bodyEl) return;
  bodyEl.innerHTML = renderMarkdown(item.dataset.raw || '');
  const editBtn = item.querySelector('.btn-edit-comment');
  if (editBtn) editBtn.style.display = '';
}

function resolveThread(threadId) {
  const container = document.getElementById('thread-comments');
  if (!container) return;
  // Show inline form instead of prompt() — mobile browsers handle prompt() inconsistently
  const existing = document.getElementById('resolve-form');
  if (existing) { existing.remove(); return; }
  const form = document.createElement('div');
  form.id = 'resolve-form';
  form.className = 'thread-form';
  form.style.marginTop = '8px';
  form.innerHTML = '<textarea id="resolve-summary" placeholder="Resolution summary (optional)"></textarea>'
    + '<div style="margin-top:4px;display:flex;gap:6px">'
    + '<button class="btn-resolve" onclick="_submitResolve(' + threadId + ')">Confirm Resolve</button>'
    + '<button class="btn-thread" style="background:none;color:var(--text-secondary)" onclick="document.getElementById(\'resolve-form\').remove()">Cancel</button>'
    + '</div>';
  container.after(form);
  document.getElementById('resolve-summary').focus();
}

async function _submitResolve(threadId) {
  const el = document.getElementById('resolve-summary');
  const summary = el ? el.value.trim() || null : null;
  await fetch('/api/moments/' + threadId + '/resolve', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({resolution_summary: summary})
  });
  openThread(threadId);
}

async function unresolveThread(threadId) {
  await fetch('/api/moments/' + threadId + '/unresolve', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
  });
  openThread(threadId);
}

// ---------------------------------------------------------------------------
// @mention autocomplete (#284)
// ---------------------------------------------------------------------------

let _mentionUsers = null; // [{id, name}, ...]

// ---------------------------------------------------------------------------
// Markdown rendering for comment bodies (#809)
//
// Comment bodies are authored as GitHub-Flavored Markdown and rendered richly.
// Pipeline: markdown-it (html:false so raw HTML in source is escaped, not
// parsed) -> DOMPurify.sanitize (strict tag/attr allowlist) -> walk text nodes
// outside <code>/<pre>/<a> to inject @mention highlight spans (preserving the
// prior behaviour without mangling code). Subjects/counterparties stay plain
// text. If the vendored libs fail to load we degrade to the old escaped-text +
// mentions rendering.
// ---------------------------------------------------------------------------

// Tags/attributes DOMPurify is allowed to keep. Everything markdown-it can emit
// for GFM is here; anything else (script, style, iframe, event handlers, raw
// HTML from source) is stripped. `input` is only for disabled task-list
// checkboxes.
const _MD_ALLOWED_TAGS = [
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr',
  'strong', 'em', 'b', 'i', 'del', 's', 'ins', 'sub', 'sup', 'kbd',
  'blockquote', 'ul', 'ol', 'li', 'a', 'code', 'pre', 'span',
  'table', 'thead', 'tbody', 'tr', 'th', 'td', 'input',
];
const _MD_ALLOWED_ATTR = [
  'href', 'title', 'class', 'start', 'type', 'checked', 'disabled', 'rel', 'target',
];

let _md = null;             // memoised markdown-it instance
let _mdHooksInstalled = false;

function _getMarkdownRenderer() {
  if (_md) return _md;
  if (typeof markdownit === 'undefined' || typeof DOMPurify === 'undefined') return null;
  const md = markdownit({
    html: false,        // never parse raw HTML from comment source
    linkify: true,      // autolink bare URLs
    breaks: true,       // GitHub-comment parity: soft newline -> <br>
    typographer: false,
    highlight: function (str, lang) {
      if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
        try {
          return '<pre class="hljs"><code>'
            + hljs.highlight(str, { language: lang, ignoreIllegals: true }).value
            + '</code></pre>';
        } catch (e) { /* fall through to escaped */ }
      }
      return '<pre class="hljs"><code>' + md.utils.escapeHtml(str) + '</code></pre>';
    },
  });
  if (typeof markdownitTaskLists !== 'undefined') {
    md.use(markdownitTaskLists);
  }
  if (!_mdHooksInstalled) {
    // Open rendered links in a new tab and neutralise tab-nabbing / referrer.
    DOMPurify.addHook('afterSanitizeAttributes', function (node) {
      if (node.tagName === 'A' && node.getAttribute('href')) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer nofollow');
      }
    });
    _mdHooksInstalled = true;
  }
  _md = md;
  return md;
}

// Render raw markdown to a sanitised HTML string with @mentions highlighted.
function renderMarkdown(raw) {
  const md = _getMarkdownRenderer();
  if (!md) {
    // Libs unavailable — preserve legacy behaviour (escaped text + mentions).
    return _renderMentions(esc(raw || ''));
  }
  const clean = DOMPurify.sanitize(md.render(raw || ''), {
    ALLOWED_TAGS: _MD_ALLOWED_TAGS,
    ALLOWED_ATTR: _MD_ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
  });
  const container = document.createElement('div');
  container.innerHTML = clean;
  _injectMentionsIntoDom(container);
  return container.innerHTML;
}

// Reduce markdown to a single line of plain text for list titles / previews
// (issue: snippet = stripped, not full render).
function stripMarkdown(raw) {
  const md = _getMarkdownRenderer();
  if (!md) return String(raw || '');
  const clean = DOMPurify.sanitize(md.render(raw || ''), {
    ALLOWED_TAGS: _MD_ALLOWED_TAGS,
    ALLOWED_ATTR: _MD_ALLOWED_ATTR,
  });
  const tmp = document.createElement('div');
  tmp.innerHTML = clean;
  return (tmp.textContent || '').replace(/\s+/g, ' ').trim();
}

// Walk text nodes and wrap @mentions in highlight spans, skipping code/pre/a
// subtrees so code samples and link text are left untouched.
function _injectMentionsIntoDom(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      let p = node.parentNode;
      while (p && p !== root) {
        const tag = p.nodeName;
        if (tag === 'CODE' || tag === 'PRE' || tag === 'A') return NodeFilter.FILTER_REJECT;
        p = p.parentNode;
      }
      return /@[\w.\-]/.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const targets = [];
  let n;
  while ((n = walker.nextNode())) targets.push(n);
  for (const node of targets) {
    const escaped = esc(node.nodeValue);
    const html = _renderMentions(escaped);
    if (html === escaped) continue;  // nothing matched
    const span = document.createElement('span');
    span.innerHTML = html;
    node.parentNode.replaceChild(span, node);
  }
}

function _renderMentions(escapedText) {
  if (!_mentionUsers || !_mentionUsers.length) {
    // Fallback: highlight single-word @mentions
    return escapedText.replace(/@([\w.\-]+)/g, '<span style="color:var(--accent);font-weight:600">@$1</span>');
  }
  // Sort names longest-first so "dan weatbrook" matches before "dan"
  const names = _mentionUsers.map(u => u.name).filter(Boolean).sort((a, b) => b.length - a.length);
  let result = escapedText;
  for (const name of names) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    result = result.replace(
      new RegExp('@' + escaped, 'g'),
      '<span style="color:var(--accent);font-weight:600">@' + name + '</span>'
    );
  }
  return result;
}

async function _loadMentionUsers() {
  if (_mentionUsers) return _mentionUsers;
  try {
    const r = await fetch('/api/users/names');
    if (r.ok) _mentionUsers = await r.json();
    else _mentionUsers = [];
  } catch (e) { _mentionUsers = []; }
  return _mentionUsers;
}

function _getMentionContext(el) {
  const val = el.value;
  const pos = el.selectionStart;
  // Walk backward from cursor to find @ — allow spaces for multi-word names
  let i = pos - 1;
  while (i >= 0 && /[\w.\- ]/.test(val[i])) i--;
  if (i < 0 || val[i] !== '@') return null;
  // Don't trigger if @ is preceded by a word char (e.g. email)
  if (i > 0 && /\w/.test(val[i - 1])) return null;
  const query = val.substring(i + 1, pos);
  // Don't match if query is only whitespace after @
  if (!query.replace(/\s/g, '')) return { start: i, end: pos, query: '' };
  return { start: i, end: pos, query };
}

function _insertMention(el, ctx, name) {
  const before = el.value.substring(0, ctx.start);
  const after = el.value.substring(ctx.end);
  el.value = before + '@' + name + ' ' + after;
  const newPos = ctx.start + name.length + 2;
  el.setSelectionRange(newPos, newPos);
  el.focus();
  _removeMentionDropdown();
}

function _removeMentionDropdown() {
  const existing = document.getElementById('mention-dropdown');
  if (existing) existing.remove();
}

function _showMentionDropdown(el, matches, ctx) {
  _removeMentionDropdown();
  if (!matches.length) return;

  const dd = document.createElement('div');
  dd.id = 'mention-dropdown';
  dd.style.cssText = 'position:absolute;z-index:9999;background:var(--bg-secondary);border:1px solid var(--accent-strong);'
    + 'border-radius:6px;max-height:150px;overflow-y:auto;min-width:160px;box-shadow:0 4px 12px rgba(0,0,0,.5)';

  const capped = matches.slice(0, 8);
  capped.forEach((u, idx) => {
    const item = document.createElement('div');
    item.textContent = u.name;
    item.setAttribute('data-mention-item', '');
    item.style.cssText = 'padding:6px 10px;cursor:pointer;font-size:.82rem;color:var(--text-primary)';
    if (idx === 0) item.style.background = cssVar('--border');
    item.addEventListener('mouseenter', () => { _highlightMentionItem(idx); });
    item.addEventListener('mousedown', (e) => {
      e.preventDefault();
      _insertMention(el, ctx, u.name);
    });
    dd.appendChild(item);
  });

  // Position below the textarea
  const rect = el.getBoundingClientRect();
  dd.style.left = rect.left + 'px';
  dd.style.top = (rect.bottom + 2) + 'px';
  dd.style.position = 'fixed';
  document.body.appendChild(dd);
}

let _mentionActiveEl = null;
let _mentionIdx = 0;

function _highlightMentionItem(idx) {
  const dd = document.getElementById('mention-dropdown');
  if (!dd) return;
  const items = dd.querySelectorAll('[data-mention-item]');
  items.forEach((el, i) => {
    el.style.background = i === idx ? cssVar('--border') : 'none';
  });
  _mentionIdx = idx;
  if (items[idx]) items[idx].scrollIntoView({block: 'nearest'});
}

function _handleMentionInput(e) {
  const el = e.target;
  if (el.tagName !== 'TEXTAREA') return;
  _mentionActiveEl = el;
  const ctx = _getMentionContext(el);
  if (!ctx) { _removeMentionDropdown(); return; }
  _mentionIdx = 0;
  _loadMentionUsers().then(users => {
    const q = ctx.query.toLowerCase();
    const matches = users.filter(u => u.name && u.name.toLowerCase().includes(q));
    _showMentionDropdown(el, matches, ctx);
  });
}

function _handleMentionKeydown(e) {
  const dd = document.getElementById('mention-dropdown');
  if (!dd) return;
  const items = dd.querySelectorAll('[data-mention-item]');
  if (!items.length) return;
  if (e.key === 'Escape') {
    e.preventDefault();
    _removeMentionDropdown();
    return;
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    _highlightMentionItem(Math.min(_mentionIdx + 1, items.length - 1));
    return;
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    _highlightMentionItem(Math.max(_mentionIdx - 1, 0));
    return;
  }
  if (e.key === 'Tab' || e.key === 'Enter') {
    const active = items[_mentionIdx];
    if (active && _mentionActiveEl) {
      e.preventDefault();
      const ctx = _getMentionContext(_mentionActiveEl);
      if (ctx) _insertMention(_mentionActiveEl, ctx, active.textContent);
    }
  }
}

// Use event delegation on the discussion card
document.addEventListener('input', _handleMentionInput);
document.addEventListener('keydown', _handleMentionKeydown);
document.addEventListener('click', (e) => {
  if (!e.target.closest('#mention-dropdown')) _removeMentionDropdown();
});

// ---------------------------------------------------------------------------
// Tuning Extraction
// ---------------------------------------------------------------------------

async function loadTuningExtractions() {
  if (!_transcriptId) return;
  const r = await fetch('/api/tuning/runs?transcript_id=' + _transcriptId);
  if (!r.ok) return;
  const runs = await r.json();
  renderTuningExtractions(runs);
}

async function renderTuningExtractions(runs) {
  const card = document.getElementById('tuning-extraction-card');
  const body = document.getElementById('tuning-extraction-body');
  const badge = document.getElementById('tuning-extraction-badge');
  card.style.display = '';

  if (!runs.length) {
    badge.textContent = '';
    body.innerHTML = '<span style="color:' + cssVar('--text-secondary') + '">No tuning changes extracted yet. Click &#8635; Extract to analyse the transcript.</span>';
    return;
  }

  // Fetch full details for each run (includes items)
  const detailed = [];
  for (const run of runs) {
    const dr = await fetch('/api/tuning/runs/' + run.id);
    if (dr.ok) detailed.push(await dr.json());
  }

  const totalItems = detailed.reduce((n, r) => n + (r.items ? r.items.length : 0), 0);
  const totalAccepted = detailed.reduce((n, r) => n + (r.accepted_count || 0), 0);
  badge.textContent = totalItems ? '(' + totalItems + ' items, ' + totalAccepted + ' accepted)' : '';

  const fmtSec = s => {
    const m = Math.floor(s / 60);
    return m + ':' + String(Math.floor(s % 60)).padStart(2, '0');
  };

  let html = '';
  for (const run of detailed) {
    const items = run.items || [];
    const created = run.created_at ? new Date(run.created_at).toLocaleString() : '';
    html += '<div style="border:1px solid ' + cssVar('--border') + ';border-radius:6px;padding:8px;margin-bottom:8px">';
    html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">';
    html += '<div style="font-size:.78rem;color:' + cssVar('--accent') + ';font-weight:600">'
      + esc(run.method) + ' &middot; ' + items.length + ' items'
      + '<span style="color:' + cssVar('--text-secondary') + ';font-weight:400;margin-left:6px">' + esc(created) + '</span>'
      + '</div>';
    html += '<button onclick="deleteTuningRun(' + run.id + ')" style="background:none;border:none;color:' + cssVar('--danger') + ';cursor:pointer;font-size:.72rem" title="Delete run">&#10005;</button>';
    html += '</div>';

    if (!items.length) {
      html += '<span style="color:' + cssVar('--text-secondary') + ';font-size:.78rem">No items extracted</span>';
    } else {
      html += '<table class="maneuver-table"><thead><tr>';
      html += '<th>Parameter</th><th>Value</th><th>Segment</th><th>Conf</th><th>Status</th><th></th>';
      html += '</tr></thead><tbody>';
      for (const item of items) {
        const statusCls = 'te-status-' + item.status;
        const statusLabel = item.status.charAt(0).toUpperCase() + item.status.slice(1);
        html += '<tr>';
        html += '<td style="font-weight:600;color:' + cssVar('--text-primary') + '">' + esc(item.parameter_name) + '</td>';
        html += '<td style="color:' + cssVar('--accent') + ';font-variant-numeric:tabular-nums">' + item.extracted_value + '</td>';
        html += '<td><span class="te-segment-text" title="' + esc(item.segment_text) + '">'
          + esc(item.segment_text.length > 60 ? item.segment_text.slice(0, 60) + '\u2026' : item.segment_text)
          + '</span>'
          + '<span style="color:' + cssVar('--text-secondary') + ';font-size:.68rem">[' + fmtSec(item.segment_start) + ' \u2013 ' + fmtSec(item.segment_end) + ']</span>'
          + '</td>';
        html += '<td style="color:' + cssVar('--text-secondary') + '">' + (item.confidence * 100).toFixed(0) + '%</td>';
        html += '<td><span class="' + statusCls + '">' + statusLabel + '</span></td>';
        html += '<td style="white-space:nowrap">';
        if (item.status === 'pending') {
          html += '<button onclick="acceptTuningItem(' + item.id + ')" class="te-play-btn" title="Accept" style="color:' + cssVar('--success') + '">&#10003;</button>';
          html += '<button onclick="dismissTuningItem(' + item.id + ')" class="te-play-btn" title="Dismiss" style="color:' + cssVar('--text-muted') + '">&#10007;</button>';
        }
        if (_session.audio_session_id && !(item.segment_start === 0 && item.segment_end === 0)) {
          html += '<button onclick="playSegmentAudio(' + item.segment_start + ',' + item.segment_end + ')" class="te-play-btn" title="Play segment">&#9654;</button>';
        }
        html += '</td>';
        html += '</tr>';
      }
      html += '</tbody></table>';
    }
    html += '</div>';
  }
  body.innerHTML = html;
}

async function extractTuning() {
  if (!_transcriptId) { alert('No transcript available for extraction'); return; }
  const btn = document.getElementById('extract-tuning-btn');
  if (btn) { btn.textContent = '\u23F3'; btn.disabled = true; }
  try {
    const r = await fetch('/api/tuning/extract/' + _transcriptId, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({method: 'regex'}),
    });
    if (!r.ok) { alert('Extraction failed: ' + r.status); return; }
    await loadTuningExtractions();
  } finally {
    if (btn) { btn.innerHTML = '&#8635; Extract'; btn.disabled = false; }
  }
}

async function acceptTuningItem(itemId) {
  const r = await fetch('/api/tuning/items/' + itemId + '/accept', {method: 'POST'});
  if (!r.ok) { alert('Failed to accept item'); return; }
  await loadTuningExtractions();
  await loadBoatSettings();
}

async function dismissTuningItem(itemId) {
  const r = await fetch('/api/tuning/items/' + itemId + '/dismiss', {method: 'POST'});
  if (!r.ok) { alert('Failed to dismiss item'); return; }
  await loadTuningExtractions();
  await loadBoatSettings();
}

async function deleteTuningRun(runId) {
  if (!confirm('Delete this extraction run and all its items?')) return;
  const r = await fetch('/api/tuning/runs/' + runId, {method: 'DELETE'});
  if (!r.ok) { alert('Failed to delete run'); return; }
  await loadTuningExtractions();
}

function playSegmentAudio(start, end) {
  if (!_session.audio_session_id) return;
  if (!_tuningSegmentAudio) {
    _tuningSegmentAudio = document.createElement('audio');
    _tuningSegmentAudio.src = '/api/audio/' + _session.audio_session_id + '/stream';
    _tuningSegmentAudio.preload = 'auto';
  }
  const audio = _tuningSegmentAudio;
  // Clear any previous stop timer
  if (_tuningSegmentTimer) {
    audio.removeEventListener('timeupdate', _tuningSegmentTimer);
    _tuningSegmentTimer = null;
  }
  audio.currentTime = start;
  audio.play();
  _tuningSegmentTimer = function() {
    if (audio.currentTime >= end) {
      audio.pause();
      audio.removeEventListener('timeupdate', _tuningSegmentTimer);
      _tuningSegmentTimer = null;
    }
  };
  audio.addEventListener('timeupdate', _tuningSegmentTimer);
}

// ---------------------------------------------------------------------------
// Isolation toggle stub — kept so the diarized transcript header can call
// _renderIsolationToggle() without an undefined-function crash. The real
// per-segment isolation now lives in the _mc* Web Audio path above (#462
// pt.6); the toggle just exposes the sticky-isolation control for the
// multi-channel session.
// ---------------------------------------------------------------------------

function _renderIsolationToggle() {
  // The multi-channel audio card already renders its own sticky-isolation
  // checkbox; nothing to add to the transcript header.
  return '';
}

// ---------------------------------------------------------------------------
// Replay controls, HUD, and polar-graded track overlay (#464, #465, #468, #470)
// ---------------------------------------------------------------------------
//
// Extends the existing _playClock (which already drives map/video/audio
// surfaces) with: a visible scrubber, play/pause, speed selector, keyboard
// shortcuts, a live instrument HUD, and a polar-grade color overlay that
// paints the track by % of target. The underlying data comes from
// /api/sessions/{id}/replay in one fetch.

let _replayStart = null; // Date — session start (for scrubber 0)
let _replayEnd = null;   // Date — session end (for scrubber max)
let _raceGun = null;     // Date — effective race gun (may be later than
                         // _replayStart for races with a general recall)
let _replaySamples = null; // [{ts: Date, stw, sog, tws, twa, aws, awa, hdg, cog}]
let _replayGrades = null;  // [{t_start, t_end, ..., grade}]
let _gradeSegments = []; // [L.polyline] overlays when polar view is active
let _gradeViewActive = false;
let _followBoat = false;  // when true, map re-centers on the boat each tick

// Course overlay state (#473): marks, start line, finish line, and laylines
// anchored to each rounding mark. All Leaflet layers, owned by _courseOverlay
// so a single toggle shows/hides them together.
const _courseOverlay = {
  visible: true,
  marks: [],          // raw [{key,name,lat,lon}] from /course-overlay
  markLayers: [],     // [L.layer]
  finishLine: null,   // L.polyline (not yet captured)
  laylines: null,     // L.layerGroup — all rounding-mark laylines, static
};

const _GRADE_COLORS = {
  red: '#d64545',
  yellow: '#d6a745',
  green: '#3db86e',
  suspicious: '#a855f7',
  unknown: '#888888',
};

function _pad2(n) { return String(n).padStart(2, '0'); }

function _fmtClock(deltaMs) {
  if (deltaMs == null || isNaN(deltaMs)) return '--:--';
  const totalS = Math.max(0, Math.floor(deltaMs / 1000));
  const mm = Math.floor(totalS / 60);
  const ss = totalS % 60;
  if (mm >= 60) {
    const hh = Math.floor(mm / 60);
    return hh + ':' + _pad2(mm % 60) + ':' + _pad2(ss);
  }
  return _pad2(mm) + ':' + _pad2(ss);
}

function _binarySearchSample(tMs) {
  if (!_replaySamples || !_replaySamples.length) return null;
  let lo = 0, hi = _replaySamples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (_replaySamples[mid].ts.getTime() <= tMs) lo = mid;
    else hi = mid - 1;
  }
  return _replaySamples[lo];
}

function _findGradeAt(tMs) {
  if (!_replayGrades || !_replayGrades.length) return null;
  // Grades are segment_index-ordered and time-contiguous; linear scan is fine
  // at ~360 segments for a 1h session but use binary for safety.
  let lo = 0, hi = _replayGrades.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (_replayGrades[mid].t_end.getTime() <= tMs) lo = mid + 1;
    else hi = mid;
  }
  const g = _replayGrades[lo];
  if (!g) return null;
  if (tMs < g.t_start.getTime()) return null;
  return g;
}

function _fmtNum(v, digits) {
  if (v == null || isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}

function _fmtPct(v) {
  if (v == null || isNaN(v)) return '—';
  return Math.round(v * 100) + '%';
}

// Lookback window for each gauge's sparkline — enough context to see trend
// without dominating the card.
const _SPARK_LOOKBACK_MS = 5 * 60 * 1000;

// Draw a single-channel sparkline into a canvas. `samples` is the full
// per-second series; we slice it to the lookback window ending at cursorMs
// and scale y to the slice's own min/max so small movements are still
// visible. Returns without touching the canvas when the field is absent
// so missing sensors fall back to a blank strip rather than a garbage line.
//
// opts.zeroCentered: force the y-axis to be symmetric around 0 so signed
// series (heel, trim) read as rail-angle gauges rather than auto-ranged
// blobs. A faint baseline at y=0 is drawn in this mode.
function _drawSparkline(canvasId, field, cursorMs, color, opts) {
  const c = document.getElementById(canvasId);
  if (!c || !_replaySamples || !_replaySamples.length) return;
  const ctx = c.getContext('2d');
  // Match backing store to displayed size on first draw / on resize so the
  // line stays crisp without depending on CSS pixels matching width attrs.
  const cssW = c.clientWidth || c.width;
  const cssH = c.clientHeight || c.height;
  if (c.width !== cssW) c.width = cssW;
  if (c.height !== cssH) c.height = cssH;
  const w = c.width;
  const h = c.height;
  ctx.clearRect(0, 0, w, h);

  const t1 = cursorMs;
  const t0 = cursorMs - _SPARK_LOOKBACK_MS;
  // Collect points within [t0, t1] that have a value for this field
  const pts = [];
  for (let i = 0; i < _replaySamples.length; i++) {
    const s = _replaySamples[i];
    const t = s.ts.getTime();
    if (t < t0) continue;
    if (t > t1) break;
    const v = s[field];
    if (v == null || isNaN(v)) continue;
    pts.push([t, v]);
  }
  if (pts.length < 2) return;

  let vmin = Infinity, vmax = -Infinity;
  for (const [, v] of pts) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
  if (opts && opts.zeroCentered) {
    // Symmetric range around 0, so +12° and -12° mirror each other and
    // the zero line sits in the middle of the strip.
    const r = Math.max(Math.abs(vmin), Math.abs(vmax), 1);
    vmin = -r;
    vmax = r;
  } else {
    // Pad the range a touch so flat series don't sit on the edges
    if (vmax - vmin < 1e-6) { vmax = vmin + 1; }
    const pad = (vmax - vmin) * 0.1;
    vmin -= pad; vmax += pad;
  }

  const xFor = t => ((t - t0) / (t1 - t0)) * (w - 2) + 1;
  const yFor = v => h - 1 - ((v - vmin) / (vmax - vmin)) * (h - 2);

  if (opts && opts.zeroCentered) {
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    const y0 = yFor(0);
    ctx.moveTo(1, y0);
    ctx.lineTo(w - 1, y0);
    ctx.stroke();
  }

  ctx.strokeStyle = color || 'rgba(120,180,255,0.9)';
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(xFor(pts[0][0]), yFor(pts[0][1]));
  for (let i = 1; i < pts.length; i++) {
    ctx.lineTo(xFor(pts[i][0]), yFor(pts[i][1]));
  }
  ctx.stroke();

  // Cursor dot on the most-recent point so it reads as "live"
  const last = pts[pts.length - 1];
  ctx.fillStyle = color || 'rgba(120,180,255,0.9)';
  ctx.beginPath();
  ctx.arc(xFor(last[0]), yFor(last[1]), 1.8, 0, Math.PI * 2);
  ctx.fill();
}

function _renderHud(utc) {
  if (!_replaySamples) return;
  const s = _binarySearchSample(utc.getTime());
  const g = _findGradeAt(utc.getTime());
  const setEl = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  setEl('hud-sog', _fmtNum(s && s.sog, 2));
  setEl('hud-stw', _fmtNum(s && s.stw, 2));
  const _fmtDeg = (v) => {
    if (v == null || Number.isNaN(v)) return '—';
    const wrapped = ((Math.round(v) % 360) + 360) % 360;
    return wrapped + '\u00b0';
  };
  // Signed-degree format: always shows +/- so heel and trim read as
  // rail angle (+ = starboard down / bow up). Bare _fmtNum drops the sign
  // for positives which would look ambiguous on a symmetric gauge.
  const _fmtSignedDeg = (v, digits) => {
    if (v == null || Number.isNaN(v)) return '—';
    const rounded = Number(v).toFixed(digits);
    const sign = Number(v) >= 0 ? '+' : '';
    return sign + rounded + '°';
  };
  setEl('hud-tws', _fmtNum(s && s.tws, 1));
  setEl('hud-twd', _fmtDeg(s && s.twd));
  setEl('hud-twa', _fmtDeg(s && s.twa));
  setEl('hud-aws', _fmtNum(s && s.aws, 1));
  setEl('hud-awa', _fmtDeg(s && s.awa));
  setEl('hud-hdg', _fmtNum(s && s.hdg, 0));
  setEl('hud-cog', _fmtNum(s && s.cog, 0));
  setEl('hud-heel', _fmtSignedDeg(s && s.heel, 1));
  setEl('hud-trim', _fmtSignedDeg(s && s.trim, 1));
  setEl('hud-pct', g && g.pct != null ? _fmtPct(g.pct) : '—');
  setEl('hud-delta', g && g.delta != null ? (g.delta >= 0 ? '+' : '') + _fmtNum(g.delta, 2) : '—');
  setEl('hud-set', s && s.set != null && !Number.isNaN(s.set) ? _fmtDeg(s.set) : '—');
  setEl('hud-drift', s && s.drift != null && !Number.isNaN(s.drift) ? _fmtNum(s.drift, 2) : '—');

  const cursorMs = utc.getTime();
  _drawSparkline('spark-stw', 'stw', cursorMs, 'rgba(120,180,255,0.9)');
  _drawSparkline('spark-sog', 'sog', cursorMs, 'rgba(120,180,255,0.6)');
  _drawSparkline('spark-tws', 'tws', cursorMs, 'rgba(100,220,150,0.9)');
  _drawSparkline('spark-twa', 'twa', cursorMs, 'rgba(100,220,150,0.6)');
  _drawSparkline('spark-aws', 'aws', cursorMs, 'rgba(220,180,100,0.9)');
  _drawSparkline('spark-awa', 'awa', cursorMs, 'rgba(220,180,100,0.6)');
  _drawSparkline('spark-hdg', 'hdg', cursorMs, 'rgba(255,140,140,0.9)');
  _drawSparkline('spark-cog', 'cog', cursorMs, 'rgba(255,140,140,0.6)');
  _drawSparkline('spark-heel', 'heel', cursorMs, 'rgba(180,140,220,0.9)', {zeroCentered: true});
  _drawSparkline('spark-trim', 'trim', cursorMs, 'rgba(180,140,220,0.6)', {zeroCentered: true});
}

function _updateReplayControls() {
  if (!_replayStart || !_replayEnd) return;
  const pos = _playClock.positionUtc || _replayStart;
  const elapsedMs = pos.getTime() - _replayStart.getTime();
  const durMs = _replayEnd.getTime() - _replayStart.getTime();
  const timeEl = document.getElementById('replay-time');
  if (timeEl) timeEl.textContent = _fmtClock(elapsedMs) + ' / ' + _fmtClock(durMs);
  const scrubber = document.getElementById('replay-scrubber');
  if (scrubber && document.activeElement !== scrubber) {
    const frac = durMs > 0 ? Math.min(1, Math.max(0, elapsedMs / durMs)) : 0;
    scrubber.value = String(Math.round(frac * 1000));
  }
  const btn = document.getElementById('replay-play-btn');
  if (btn) btn.innerHTML = _playClock.state === 'playing' ? '&#10074;&#10074;' : '&#9654;';
}

function _togglePlayPause() {
  if (!_replayStart) return;
  if (_playClock.state === 'playing') {
    _stopPlayTick();
  } else {
    // Pause any WAV / multi-channel audio before the clock starts — the
    // user wants the track replay play button to drive the map/video
    // preview, not audio. YT is intentionally left running so it keeps
    // providing a visual preview while the replay scrubs it.
    try {
      const aEl = document.getElementById('session-audio')
        || document.querySelector('#audio-body audio');
      if (aEl && !aEl.paused) aEl.pause();
    } catch (e) { /* swallow */ }
    try { if (typeof _mcIsPlaying !== 'undefined' && _mcIsPlaying) _mcPause(); } catch (e) { /* swallow */ }
    if (!_playClock.positionUtc) _playClock.positionUtc = _replayStart;
    // If at (or past) the end, restart from the beginning
    if (_replayEnd && _playClock.positionUtc.getTime() >= _replayEnd.getTime()) {
      setPosition(_replayStart, {source: 'replay'});
    }
    _startPlayTick();
  }
  _updateReplayControls();
}

function _clearGradeSegments() {
  for (const seg of _gradeSegments) {
    try { _map.removeLayer(seg); } catch (e) { /* ignore */ }
  }
  _gradeSegments = [];
}

function _drawGradeSegments() {
  _clearGradeSegments();
  if (!_map || !_replayGrades || !_trackData) return;
  // Paint contiguous runs of same-grade segments as individual polylines on
  // top of the base track. We walk the sample positions and mark each one
  // with the grade that covers it, then coalesce consecutive same-grade runs.
  const timestamps = _trackData.timestamps;
  const latLngs = _trackData.latLngs;
  if (!timestamps || timestamps.length < 2) return;
  const gradesAtIdx = new Array(timestamps.length);
  for (let i = 0; i < timestamps.length; i++) {
    const g = _findGradeAt(timestamps[i].getTime());
    gradesAtIdx[i] = g ? g.grade : 'unknown';
  }
  let runStart = 0;
  for (let i = 1; i <= timestamps.length; i++) {
    if (i === timestamps.length || gradesAtIdx[i] !== gradesAtIdx[runStart]) {
      const color = _GRADE_COLORS[gradesAtIdx[runStart]] || _GRADE_COLORS.unknown;
      const slice = latLngs.slice(runStart, i + 1); // include boundary point
      if (slice.length >= 2) {
        const line = L.polyline(slice, {color: color, weight: 6, opacity: 0.85}).addTo(_map);
        _gradeSegments.push(line);
      }
      runStart = i;
    }
  }
  // Hide the underlying single-color track + its casing while grade overlay is active
  if (_trackData.line) _trackData.line.setStyle({opacity: 0});
  if (_trackData.casing) _trackData.casing.setStyle({opacity: 0});
}

function _setGradeViewActive(active) {
  _gradeViewActive = !!active;
  const legend = document.getElementById('polar-legend');
  if (legend) legend.style.display = _gradeViewActive ? '' : 'none';
  if (_gradeViewActive) {
    _drawGradeSegments();
  } else {
    _clearGradeSegments();
    if (_trackData && _trackData.line) _trackData.line.setStyle({opacity: 1});
    if (_trackData && _trackData.casing) _trackData.casing.setStyle({opacity: 0.55});
  }
}

async function _loadCourseOverlay() {
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/course-overlay');
    if (!r.ok) return;
    const data = await r.json();
    _courseOverlay.marks = (data.marks || []).filter(m => m.lat != null && m.lon != null);
    _drawCourseMarks();
    // Start-line geometry (and its laylines) are rendered by
    // loadVakarosOverlay, which has the canonical race_start_context with
    // the wind-from bearing at gun time. We intentionally don't draw it
    // here to avoid duplicate layers on top of that one.
    // Laylines depend on _maneuvers (rounding positions) and _replaySamples
    // (TWD at each rounding). Both load asynchronously, so try once now and
    // retry shortly if either is still missing — they'll usually be ready
    // within a few hundred ms after _loadReplayData/loadManeuvers settle.
    _drawAllLaylines();
    setTimeout(_drawAllLaylines, 1500);
    _setCourseOverlayVisible(_courseOverlay.visible);
  } catch (e) {
    // Non-fatal — replay still works without the course overlay.
  }
}

function _drawCourseMarks() {
  for (const layer of _courseOverlay.markLayers) {
    try { _map.removeLayer(layer); } catch (e) { /* swallow */ }
  }
  _courseOverlay.markLayers = [];
  if (!_map) return;
  for (const m of _courseOverlay.marks) {
    // Filled circle marker with the mark name as a tooltip — distinct from
    // the start/finish dots already on the track polyline.
    const layer = L.circleMarker([m.lat, m.lon], {
      radius: 8,
      color: '#1f2937',
      weight: 2,
      fillColor: '#facc15',
      fillOpacity: 0.95,
    }).bindTooltip(m.name || m.key || 'mark', {
      permanent: true,
      direction: 'right',
      offset: [10, 0],
      className: 'wf-mark-label',
    });
    if (_courseOverlay.visible) layer.addTo(_map);
    _courseOverlay.markLayers.push(layer);
  }
}

// Start-line rendering (dashed line, wind ticks, and tack laylines) lives
// in loadVakarosOverlay() at the top of this file — it has the canonical
// race_start_context.twd_deg, which is the wind-from bearing at gun time.
// This file used to have a second implementation here, but it was fed by
// replay-endpoint TWA that's folded to [0,180] and lost the sign needed
// for correct wind-frame math. Removed to avoid duplicate layers.

// Draw laylines anchored to each rounding mark (#473). Mark type (windward
// vs leeward) is inferred from the boat's TWA in the ~20s leading up to the
// rounding: |TWA| < 90 = sailing upwind = windward mark = tack laylines;
// |TWA| >= 90 = sailing downwind = leeward mark = gybe laylines. Each mark
// gets only its own pair, not all four — windward marks don't get gybe
// laylines and vice versa.
//
// Length scales with the leg the boat just sailed: distance from the prior
// rounding (or the boat's position at the race gun for the first rounding)
// × 1.3. That keeps the laylines proportional to the course — short legs
// get short laylines, long legs get long ones — without stretching over
// the whole map.
//
// TWD comes from that same approach sample, so the laylines reflect the
// wind state when the boat was actually approaching the mark. Bearings are
// derived from HDG + TWA without any extra 180° flip (the v1 math bug).
const _LAYLINE_LEG_SCALE = 1.3;
const _LAYLINE_FALLBACK_M = 400;
const _UPWIND_HALF_ANGLE = 45;   // tacking angle / 2 for typical masthead boat
const _DOWNWIND_HALF_ANGLE = 30; // gybing angle / 2 for typical kite boat
// High-contrast colors so they don't get lost on a pale-blue water tile.
const _LAYLINE_TACK_COLOR = '#e11d48';   // saturated rose for upwind/tack
const _LAYLINE_GYBE_COLOR = '#84cc16';   // saturated lime for downwind/gybe
const _LAYLINE_WEIGHT = 3;
const _APPROACH_LOOKBACK_MS = 20_000;

function _drawAllLaylines() {
  if (!_map) return;
  if (_courseOverlay.laylines) {
    try { _map.removeLayer(_courseOverlay.laylines); } catch (e) { /* swallow */ }
    _courseOverlay.laylines = null;
  }
  if (!_maneuvers || !_maneuvers.length) return;
  if (typeof _binarySearchSample !== 'function') return;

  // Only show laylines for roundings that happen after the real race
  // gun, plus a grace window. _raceGun may be later than _replayStart
  // for races with a general recall — the stored races.start_utc points
  // at the original attempt, and the actual gun is the latest Vakaros
  // race_start event inside the race window. 120s of grace after the
  // gun covers the start-hardening maneuver (reach → close-hauled)
  // which the detector classifies as a rounding because the pre/post
  // TWA mode differs; a real windward mark is always more than two
  // minutes of sailing from the line.
  const gun = _raceGun || _replayStart;
  const raceStartMs = (gun && gun.getTime()) || 0;
  const LAYLINE_START_GRACE_MS = 120_000;

  // Build a list of every maneuver (any type) with position + timestamp so
  // we can find "the last straight segment the boat sailed" before each
  // rounding — that's the leg the layline is parallel to.
  const allEvents = [];
  for (const m of _maneuvers) {
    if (!m || m.lat == null || m.lon == null || !m.ts) continue;
    const tsStr = (m.ts.endsWith && (m.ts.endsWith('Z') || m.ts.includes('+'))) ? m.ts : m.ts + 'Z';
    const tMs = new Date(tsStr).getTime();
    if (!isNaN(tMs)) allEvents.push({m, tMs});
  }
  allEvents.sort((a, b) => a.tMs - b.tMs);

  // Boat position at race gun, used as the fallback "leg start" when a
  // rounding has no prior maneuver to measure against.
  let gunPos = null;
  if (_trackData && _trackData.latLngs.length) {
    const gunIdx = raceStartMs ? _indexForUtc(new Date(raceStartMs)) : 0;
    gunPos = _trackData.latLngs[gunIdx] || _trackData.latLngs[0];
  }

  const layers = [];
  for (let i = 0; i < allEvents.length; i++) {
    const {m, tMs} = allEvents[i];
    if (m.type !== 'rounding') continue;
    if (raceStartMs && tMs < raceStartMs + LAYLINE_START_GRACE_MS) continue;
    // Sample from ~20s before the rounding gives the boat's approach state,
    // before the heading-change transient distorts TWA. Falls back to the
    // sample at the rounding ts if the lookback is out of range.
    const approach = _binarySearchSample(tMs - _APPROACH_LOOKBACK_MS) || _binarySearchSample(tMs);
    if (!approach || approach.twa == null || approach.hdg == null) continue;

    const isWindward = Math.abs(approach.twa) < 90;
    // Wind reference frame (north-relative, degrees clockwise from north).
    // HDG + TWA is the bearing FROM which the wind is blowing.
    const windFromBearing = ((approach.hdg + approach.twa) % 360 + 360) % 360;
    const windToBearing = (windFromBearing + 180) % 360;
    const at = [m.lat, m.lon];

    // Layline length scales with the boat's actual approach pattern.
    // For a windward mark we look back at the last two TACKS and use the
    // FURTHER of them × 1.3 — that ensures each tack layline extends past
    // the tack the boat used to get onto that tack, which is what the
    // user actually wants to see ("extend beyond where we tacked onto
    // port last before the rounding"). For a leeward mark we use the
    // distance to the previous GYBE × 1.3 but cap at 300m so long
    // downwind legs don't stretch laylines across the whole map.
    const MAX_LEEWARD_LAYLINE_M = 300;

    let laylineLen;
    if (isWindward) {
      const tackDists = [];
      for (let j = i - 1; j >= 0 && tackDists.length < 2; j--) {
        const prev = allEvents[j];
        if (prev.m.type === 'tack' && prev.m.lat != null && prev.m.lon != null) {
          tackDists.push(_haversineMeters([prev.m.lat, prev.m.lon], at));
        }
      }
      if (tackDists.length === 0) {
        const legM = gunPos ? _haversineMeters(gunPos, at) : 0;
        laylineLen = legM > 0 ? legM * _LAYLINE_LEG_SCALE : _LAYLINE_FALLBACK_M;
      } else {
        laylineLen = Math.max(...tackDists) * _LAYLINE_LEG_SCALE;
      }
    } else {
      let prevGybeDist = 0;
      for (let j = i - 1; j >= 0; j--) {
        const prev = allEvents[j];
        if (prev.m.type === 'gybe' && prev.m.lat != null && prev.m.lon != null) {
          prevGybeDist = _haversineMeters([prev.m.lat, prev.m.lon], at);
          break;
        }
      }
      const scaled = prevGybeDist > 0 ? prevGybeDist * _LAYLINE_LEG_SCALE : _LAYLINE_FALLBACK_M;
      laylineLen = Math.min(scaled, MAX_LEEWARD_LAYLINE_M);
    }

    if (isWindward) {
      // Tack laylines extend from the mark downwind (where the boat was
      // coming from on the upwind leg), spread by ±tacking-half-angle.
      const stbd = _projectLatLng(at, (windToBearing + _UPWIND_HALF_ANGLE) % 360, laylineLen);
      const port = _projectLatLng(at, (windToBearing - _UPWIND_HALF_ANGLE + 360) % 360, laylineLen);
      const opts = {color: _LAYLINE_TACK_COLOR, weight: _LAYLINE_WEIGHT, opacity: 0.85, dashArray: '6, 6', lineCap: 'butt'};
      layers.push(L.polyline([at, stbd], opts));
      layers.push(L.polyline([at, port], opts));
    } else {
      // Gybe laylines extend from the mark upwind (where the boat was coming
      // from on the downwind leg), spread by ±gybing-half-angle.
      const stbd = _projectLatLng(at, (windFromBearing + _DOWNWIND_HALF_ANGLE) % 360, laylineLen);
      const port = _projectLatLng(at, (windFromBearing - _DOWNWIND_HALF_ANGLE + 360) % 360, laylineLen);
      const opts = {color: _LAYLINE_GYBE_COLOR, weight: _LAYLINE_WEIGHT, opacity: 0.85, dashArray: '6, 6', lineCap: 'butt'};
      layers.push(L.polyline([at, stbd], opts));
      layers.push(L.polyline([at, port], opts));
    }
  }

  if (!layers.length) return;
  const group = L.layerGroup(layers);
  if (_courseOverlay.visible) group.addTo(_map);
  _courseOverlay.laylines = group;
}

// Haversine distance in meters between two [lat, lng] (or {lat,lng}) points.
// Used to scale layline length to the leg the boat just sailed.
function _haversineMeters(a, b) {
  const aLat = Array.isArray(a) ? a[0] : a.lat;
  const aLng = Array.isArray(a) ? a[1] : a.lng;
  const bLat = Array.isArray(b) ? b[0] : b.lat;
  const bLng = Array.isArray(b) ? b[1] : b.lng;
  const R = 6371000;
  const lat1 = (aLat * Math.PI) / 180;
  const lat2 = (bLat * Math.PI) / 180;
  const dLat = ((bLat - aLat) * Math.PI) / 180;
  const dLng = ((bLng - aLng) * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Project a lat/lng forward by `distM` meters along true bearing `bearingDeg`.
// Equirectangular approximation — fine at race-course scale (sub-km).
function _projectLatLng(latLng, bearingDeg, distM) {
  const lat = Array.isArray(latLng) ? latLng[0] : latLng.lat;
  const lng = Array.isArray(latLng) ? latLng[1] : latLng.lng;
  const R = 6371000;
  const br = (bearingDeg * Math.PI) / 180;
  const dLat = (distM * Math.cos(br)) / R;
  const dLng = (distM * Math.sin(br)) / (R * Math.cos((lat * Math.PI) / 180));
  return [lat + (dLat * 180) / Math.PI, lng + (dLng * 180) / Math.PI];
}

function _setCourseOverlayVisible(visible) {
  _courseOverlay.visible = !!visible;
  for (const layer of _courseOverlay.markLayers) {
    if (_courseOverlay.visible) layer.addTo(_map);
    else { try { _map.removeLayer(layer); } catch (e) { /* swallow */ } }
  }
  if (_courseOverlay.laylines) {
    if (_courseOverlay.visible) _courseOverlay.laylines.addTo(_map);
    else { try { _map.removeLayer(_courseOverlay.laylines); } catch (e) { /* swallow */ } }
  } else if (_courseOverlay.visible) {
    // First time we're being made visible after maneuvers loaded
    _drawAllLaylines();
  }
}

async function _loadReplayData() {
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/replay');
    if (!r.ok) return;
    const data = await r.json();
    // Scrubber 0 is the prestart-window cutoff (start_utc − 20 min) so the
    // helm can scrub through pre-gun maneuvers and have video / gauges /
    // track follow. Falls back to start_utc for older payloads (cache).
    _replayStart = new Date(data.prestart_start_utc || data.start_utc);
    _replayEnd = new Date(data.end_utc);
    _raceGun = data.race_gun_utc ? new Date(data.race_gun_utc) : new Date(data.start_utc);
    _replaySamples = (data.samples || []).map(s => ({
      ts: new Date(s.ts),
      stw: s.stw,
      sog: s.sog,
      tws: s.tws,
      twa: s.twa,
      twd: s.twd,
      aws: s.aws,
      awa: s.awa,
      hdg: s.hdg,
      cog: s.cog,
      heel: s.heel,
      trim: s.trim,
      set: s.set,
      drift: s.drift,
    }));
    if (typeof _rebuildCurrentOverlay === 'function') _rebuildCurrentOverlay();
    if (_windEnabled && typeof _rebuildWindOverlay === 'function') _rebuildWindOverlay();
    _replayGrades = (data.grades || []).map(g => ({
      i: g.i,
      t_start: new Date(g.t_start),
      t_end: new Date(g.t_end),
      grade: g.grade,
      pct: g.pct,
      delta: g.delta,
      tws: g.tws,
      twa: g.twa,
      bsp: g.bsp,
      target: g.target,
      tack: g.tack,
      point_of_sail: g.point_of_sail,
    }));
    // Register HUD as a clock consumer so it updates on every tick/seek
    registerSurface('hud', function(utc) { _renderHud(utc); });
    // Show replay UI
    const controls = document.getElementById('replay-controls');
    if (controls) controls.style.display = '';
    const gaugesWrap = document.getElementById('replay-gauges-wrap');
    if (gaugesWrap) gaugesWrap.style.display = '';
    const toggleRow = document.getElementById('replay-toggle-row');
    if (toggleRow) toggleRow.style.display = '';
    // Initial HUD render at session start
    if (!_playClock.positionUtc) _playClock.positionUtc = _replayStart;
    _renderHud(_playClock.positionUtc);
    _updateReplayControls();
    // Now that samples and the map are in place, re-apply any persisted
    // layer toggles (boat wind/current, overlays, polar colors, etc.) so
    // user preferences carry across sessions.
    _applyPersistedLayerToggles();
    // Samples + replay window are now loaded — redraw the rounding
    // laylines so they respect the race-start filter.
    if (typeof _drawAllLaylines === 'function') _drawAllLaylines();
  } catch (e) {
    // Non-fatal: replay is best-effort, the rest of the page still works
  }
}

function _wireReplayControls() {
  const btn = document.getElementById('replay-play-btn');
  if (btn) btn.addEventListener('click', _togglePlayPause);
  const speedSel = document.getElementById('replay-speed');
  if (speedSel) speedSel.addEventListener('change', (e) => {
    _setPlaybackSpeed(parseFloat(e.target.value) || 1);
  });
  const scrubber = document.getElementById('replay-scrubber');
  if (scrubber) {
    scrubber.addEventListener('input', (e) => {
      if (!_replayStart || !_replayEnd) return;
      const frac = Number(e.target.value) / 1000;
      const t = _replayStart.getTime() + frac * (_replayEnd.getTime() - _replayStart.getTime());
      _seekTo(new Date(t), 'replay');
    });
  }
  const _followBoatApply = (checked) => {
    _followBoat = !!checked;
    if (_followBoat && _trackData && _playClock.positionUtc) {
      const idx = _indexForUtc(_playClock.positionUtc);
      const latLng = _trackData.latLngs[idx];
      if (latLng && _map) _map.panTo(latLng, {animate: true});
    }
  };
  _persistLayerToggle('toggle-polar-grades', _setGradeViewActive);
  _persistLayerToggle('toggle-follow-boat', _followBoatApply);
  _persistLayerToggle('toggle-maneuver-markers', _setManeuverMarkersVisible);
  _persistLayerToggle('toggle-course-overlay', _setCourseOverlayVisible);
  _persistLayerToggle('toggle-current-overlay', _setCurrentOverlayEnabled);
  _persistLayerToggle('toggle-wind-overlay', _setWindOverlayEnabled);
  _persistLayerToggle('toggle-boat-current', (checked) => _setBoatInstrument('current', checked));
  _persistLayerToggle('toggle-boat-wind', (checked) => _setBoatInstrument('wind', checked));

  const prevBtn = document.getElementById('replay-prev-event-btn');
  if (prevBtn) prevBtn.addEventListener('click', () => _stepEvent(-1));
  const nextBtn = document.getElementById('replay-next-event-btn');
  if (nextBtn) nextBtn.addEventListener('click', () => _stepEvent(+1));

  // Keyboard shortcuts: space toggles play, arrows seek ±5s, [/] step events
  document.addEventListener('keydown', (e) => {
    if (!_replayStart) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (e.code === 'Space') {
      e.preventDefault();
      _togglePlayPause();
    } else if (e.code === 'ArrowRight' || e.code === 'ArrowLeft') {
      const delta = (e.code === 'ArrowRight' ? 5000 : -5000) * (e.shiftKey ? 6 : 1);
      const base = _playClock.positionUtc || _replayStart;
      const next = new Date(Math.min(
        _replayEnd.getTime(),
        Math.max(_replayStart.getTime(), base.getTime() + delta)
      ));
      _seekTo(next, 'replay');
      _updateReplayControls();
    } else if (e.code === 'BracketLeft') {
      e.preventDefault();
      _stepEvent(-1);
    } else if (e.code === 'BracketRight') {
      e.preventDefault();
      _stepEvent(+1);
    }
  });
}

// Build a sorted list of replay-stepable events from the loaded maneuvers.
// Filtered to {tack, gybe, rounding, start} and de-duplicated by timestamp
// so the synthetic Vakaros race-start (already injected into _maneuvers)
// doesn't double up if a real maneuver lands at the same instant.
function _replayEventList() {
  if (!_maneuvers || !_maneuvers.length) return [];
  const out = [];
  const seen = new Set();
  for (const m of _maneuvers) {
    if (!m || !m.ts) continue;
    if (m.type && !['tack', 'gybe', 'rounding', 'start'].includes(m.type)) continue;
    const tsStr = (m.ts.endsWith('Z') || m.ts.includes('+')) ? m.ts : m.ts + 'Z';
    const tMs = new Date(tsStr).getTime();
    if (isNaN(tMs)) continue;
    if (seen.has(tMs)) continue;
    seen.add(tMs);
    out.push({ts: tMs, type: m.type});
  }
  out.sort((a, b) => a.ts - b.ts);
  return out;
}

function _stepEvent(direction) {
  const events = _replayEventList();
  if (!events.length) return;
  const cursor = (_playClock.positionUtc && _playClock.positionUtc.getTime()) || (_replayStart && _replayStart.getTime()) || events[0].ts;
  // 250 ms tolerance so "next" doesn't land on the event we're already on.
  const tol = 250;
  let target = null;
  if (direction > 0) {
    for (const e of events) {
      if (e.ts > cursor + tol) { target = e.ts; break; }
    }
    if (target == null) target = events[events.length - 1].ts;
  } else {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].ts < cursor - tol) { target = events[i].ts; break; }
    }
    if (target == null) target = events[0].ts;
  }
  _seekTo(new Date(target), 'replay');
  _updateReplayControls();
}

// ---------------------------------------------------------------------------
// Video overlays (#639) — painted on top of the player to match the
// compare page visuals.
//
// Gauges toggle:
//   • #video-gauge-overlay     top-left   — always on while toggled
//   • #video-recovery-overlay  top-mid    — only during a maneuver window
// Track toggle:
//   • #video-course-overlay    top-right  — always on while toggled
//                                           (full session lat/lng track)
//   • #video-maneuver-overlay  bottom-left — only during a maneuver window
//                                            (wind-up zoom of the turn)
//
// Both buttons default off; state persists in localStorage. SVGs are
// ports of compare.js (#570 / #572 / #574) so the visual language
// matches the compare page exactly.
// ---------------------------------------------------------------------------

const _VIDEO_OVERLAY_PAD_S = 10; // seconds before/after each maneuver window
const _VIDEO_OVERLAY_LS_GAUGES = 'helmlog.videoOverlay.gauges';
const _VIDEO_OVERLAY_LS_TRACK = 'helmlog.videoOverlay.track';
let _videoGaugesOn = false;
let _videoTrackOn = false;
// Cached projection of _trackData.latLngs → mini-map coords. Rebuilt when
// _trackData becomes available after first load.
let _videoCourseProjection = null;
// ID of the maneuver currently mounted for the per-maneuver overlays —
// when it changes we rebuild those SVGs (their geometry is baked per
// maneuver). null means no maneuver is currently mounted.
let _videoMountedManId = null;

function _readOverlayFlag(key) {
  try { return localStorage.getItem(key) === '1'; } catch (e) { return false; }
}
function _writeOverlayFlag(key, on) {
  try { localStorage.setItem(key, on ? '1' : '0'); } catch (e) { /* ignore */ }
}
function _setOverlayBtnStyle(btn, on) {
  if (!btn) return;
  btn.style.background = on ? 'var(--accent-strong)' : 'var(--bg-input)';
  btn.style.color = on ? 'var(--bg-primary)' : 'var(--text-secondary)';
  btn.style.border = on ? 'none' : '1px solid var(--border)';
}
function _removeVideoOverlay(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function toggleVideoGauges() {
  _videoGaugesOn = !_videoGaugesOn;
  _writeOverlayFlag(_VIDEO_OVERLAY_LS_GAUGES, _videoGaugesOn);
  _setOverlayBtnStyle(document.getElementById('video-gauges-btn'), _videoGaugesOn);
  _restartOverlayTick();
}

function toggleVideoTrack() {
  _videoTrackOn = !_videoTrackOn;
  _writeOverlayFlag(_VIDEO_OVERLAY_LS_TRACK, _videoTrackOn);
  _setOverlayBtnStyle(document.getElementById('video-track-btn'), _videoTrackOn);
  _restartOverlayTick();
}

function _parseManUtcMs(iso) {
  if (!iso) return null;
  let s = String(iso).replace(' ', 'T');
  if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d.getTime();
}

function _findActiveManeuver(utc) {
  if (!_maneuvers || !_maneuvers.length || !utc) return null;
  const t = utc.getTime ? utc.getTime() : +utc;
  for (const m of _maneuvers) {
    const startMs = _parseManUtcMs(m.ts);
    if (startMs == null) continue;
    const dur = typeof m.duration_sec === 'number' ? m.duration_sec : 0;
    const winStart = startMs - _VIDEO_OVERLAY_PAD_S * 1000;
    const winEnd = startMs + dur * 1000 + _VIDEO_OVERLAY_PAD_S * 1000;
    if (t >= winStart && t <= winEnd) return m;
  }
  return null;
}

// ---- Gauge (always-on) -----------------------------------------------------

function _renderVideoGaugeSvg() {
  const s = 150, r = 62, cx = s / 2, cy = s / 2;
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
  const cardinals = [{l:'N',d:0},{l:'E',d:90},{l:'S',d:180},{l:'W',d:270}];
  let labels = '';
  for (const c of cardinals) {
    const rad = (c.d - 90) * Math.PI / 180;
    const lx = cx + (r + 9) * Math.cos(rad), ly = cy + (r + 9) * Math.sin(rad);
    labels += '<text x="' + lx.toFixed(1) + '" y="' + ly.toFixed(1)
      + '" text-anchor="middle" dominant-baseline="central" font-size="9" font-weight="600" fill="rgba(255,255,255,.55)">' + c.l + '</text>';
  }
  return '<svg id="video-gauge-overlay" class="video-overlay" width="' + s + '" height="' + s + '">'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + (r + 10) + '" fill="rgba(0,0,0,.5)"/>'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="rgba(255,255,255,.25)" stroke-width="1"/>'
    + ticks + labels
    + '<g id="video-gauge-twd" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + (cy + r - 10) + '" x2="' + cx + '" y2="' + (cy - r + 12) + '" stroke="#f59e0b" stroke-width="2" stroke-linecap="round"/>'
    + '<polygon points="' + cx + ',' + (cy - r + 8) + ' ' + (cx - 4) + ',' + (cy - r + 16) + ' ' + (cx + 4) + ',' + (cy - r + 16) + '" fill="#f59e0b"/>'
    + '</g>'
    + '<g id="video-gauge-awa" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + cy + '" x2="' + cx + '" y2="' + (cy - r + 18) + '" stroke="#60a5fa" stroke-width="1.8" stroke-linecap="round"/>'
    + '<polygon points="' + cx + ',' + (cy - r + 14) + ' ' + (cx - 3) + ',' + (cy - r + 21) + ' ' + (cx + 3) + ',' + (cy - r + 21) + '" fill="#60a5fa"/>'
    + '</g>'
    + '<g id="video-gauge-cog" transform="rotate(0,' + cx + ',' + cy + ')">'
    + '<line x1="' + cx + '" y1="' + cy + '" x2="' + cx + '" y2="' + (cy - r + 6) + '" stroke="rgba(255,255,255,.6)" stroke-width="1" stroke-dasharray="3,2"/>'
    + '</g>'
    + '<polygon points="' + cx + ',' + (cy - 6) + ' ' + (cx - 4) + ',' + (cy + 5) + ' ' + (cx + 4) + ',' + (cy + 5) + '" fill="#fff" stroke="rgba(0,0,0,.4)" stroke-width="0.5"/>'
    + '<rect x="' + (cx - 18) + '" y="1" width="36" height="15" rx="3" fill="rgba(0,0,0,.8)"/>'
    + '<text id="video-gauge-hdg" x="' + cx + '" y="12" text-anchor="middle" font-size="11" font-weight="700" font-family="monospace" fill="#fff">---</text>'
    + '<rect x="2" y="' + (cy - 9) + '" width="36" height="24" rx="3" fill="rgba(0,0,0,.7)"/>'
    + '<text x="20" y="' + (cy - 1) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">BSP</text>'
    + '<text id="video-gauge-bsp" x="20" y="' + (cy + 11) + '" text-anchor="middle" font-size="12" font-weight="700" font-family="monospace" fill="#3db86e">-.-</text>'
    + '<rect x="' + (s - 38) + '" y="' + (cy - 9) + '" width="36" height="24" rx="3" fill="rgba(0,0,0,.7)"/>'
    + '<text x="' + (s - 20) + '" y="' + (cy - 1) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">SOG</text>'
    + '<text id="video-gauge-sog" x="' + (s - 20) + '" y="' + (cy + 11) + '" text-anchor="middle" font-size="12" font-weight="700" font-family="monospace" fill="#fff">-.-</text>'
    + '<rect x="6" y="' + (s - 28) + '" width="40" height="24" rx="3" fill="rgba(0,0,0,.75)"/>'
    + '<text x="26" y="' + (s - 17) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">TWS</text>'
    + '<text id="video-gauge-tws" x="26" y="' + (s - 6) + '" text-anchor="middle" font-size="13" font-weight="700" font-family="monospace" fill="#f59e0b">--</text>'
    + '<rect x="' + (s - 46) + '" y="' + (s - 28) + '" width="40" height="24" rx="3" fill="rgba(0,0,0,.75)"/>'
    + '<text x="' + (s - 26) + '" y="' + (s - 17) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.6)">AWS</text>'
    + '<text id="video-gauge-aws" x="' + (s - 26) + '" y="' + (s - 6) + '" text-anchor="middle" font-size="13" font-weight="700" font-family="monospace" fill="#60a5fa">--</text>'
    + '</svg>';
}

function _videoSampleAt(utcMs) {
  if (!_replaySamples || !_replaySamples.length) return null;
  let lo = 0, hi = _replaySamples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (_replaySamples[mid].ts.getTime() <= utcMs) lo = mid;
    else hi = mid - 1;
  }
  return _replaySamples[lo];
}

function _updateVideoGauge(utc) {
  const sample = _videoSampleAt(utc.getTime());
  if (!sample) return;
  const cx = 75, cy = 75;
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set('video-gauge-hdg', sample.hdg != null ? Math.round(sample.hdg) : '---');
  set('video-gauge-bsp', sample.stw != null ? sample.stw.toFixed(1) : '-.-');
  set('video-gauge-sog', sample.sog != null ? sample.sog.toFixed(1) : '-.-');
  set('video-gauge-tws', sample.tws != null ? sample.tws.toFixed(0) : '--');
  set('video-gauge-aws', sample.aws != null ? sample.aws.toFixed(0) : '--');
  const rot = (id, deg) => {
    const g = document.getElementById(id);
    if (g && deg != null) g.setAttribute('transform', 'rotate(' + deg.toFixed(1) + ',' + cx + ',' + cy + ')');
  };
  if (sample.twd != null && sample.hdg != null) rot('video-gauge-twd', ((sample.twd - sample.hdg) + 360) % 360);
  if (sample.awa != null) rot('video-gauge-awa', sample.awa);
  if (sample.cog != null && sample.hdg != null) rot('video-gauge-cog', ((sample.cog - sample.hdg) + 360) % 360);
}

// ---- Course overlay (always-on, full session lat/lng track) ---------------

function _buildVideoCourseProjection() {
  if (!_trackData || !_trackData.latLngs || _trackData.latLngs.length < 2) return null;
  const size = 140, pad = 8;
  const latLngs = _trackData.latLngs;
  const lat0 = latLngs[0][0], lng0 = latLngs[0][1];
  const cosLat = Math.cos(lat0 * Math.PI / 180);
  const mPerDeg = 111320;
  const pts = latLngs.map(ll => ({
    x: (ll[1] - lng0) * mPerDeg * cosLat,
    y: (ll[0] - lat0) * mPerDeg,
  }));
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of pts) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return null;
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(20, Math.max(maxX - minX, maxY - minY) / 2 * 1.05);
  const sx = x => pad + (x - (cx - half)) / (2 * half) * (size - 2 * pad);
  const sy = y => (size - pad) - (y - (cy - half)) / (2 * half) * (size - 2 * pad);
  const step = Math.max(1, Math.floor(pts.length / 400));
  const poly = [];
  for (let i = 0; i < pts.length; i += step) {
    poly.push(sx(pts[i].x).toFixed(1) + ',' + sy(pts[i].y).toFixed(1));
  }
  return {size, pad, lat0, lng0, cosLat, mPerDeg, sx, sy, polyStr: poly.join(' ')};
}

function _renderVideoCourseSvg() {
  const proj = _videoCourseProjection || _buildVideoCourseProjection();
  if (!proj) return '';
  _videoCourseProjection = proj;
  const s = proj.size;
  return '<svg id="video-course-overlay" class="video-overlay" width="' + s + '" height="' + s + '">'
    + '<rect width="' + s + '" height="' + s + '" rx="6" fill="rgba(0,0,0,.45)"/>'
    + '<polyline points="' + proj.polyStr + '" fill="none" stroke="#7eb8f7" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/>'
    + '<circle id="video-course-dot" cx="-10" cy="-10" r="3.5" fill="#fff" stroke="rgba(0,0,0,.6)" stroke-width="1"/>'
    + '</svg>';
}

function _updateVideoCourseDot(utc) {
  if (!_trackData || !_trackData.timestamps || !_trackData.timestamps.length) return;
  const proj = _videoCourseProjection;
  if (!proj) return;
  const tMs = utc.getTime();
  const ts = _trackData.timestamps;
  let lo = 0, hi = ts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ts[mid].getTime() <= tMs) lo = mid;
    else hi = mid - 1;
  }
  const ll = _trackData.latLngs[lo];
  if (!ll) return;
  const x = (ll[1] - proj.lng0) * proj.mPerDeg * proj.cosLat;
  const y = (ll[0] - proj.lat0) * proj.mPerDeg;
  const dot = document.getElementById('video-course-dot');
  if (!dot) return;
  dot.setAttribute('cx', proj.sx(x).toFixed(1));
  dot.setAttribute('cy', proj.sy(y).toFixed(1));
}

// ---- Maneuver-zoom (per-maneuver, wind-up X/Y projection) ------------------

function _renderVideoManeuverSvg(m) {
  const track = m && m.track;
  if (!track || track.length < 2) return '';
  const size = 120, pad = 8;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of track) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return '';
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(5, Math.max(maxX - minX, maxY - minY) / 2 + 3);
  const bMinX = cx - half, bMaxX = cx + half, bMinY = cy - half, bMaxY = cy + half;
  const sx = x => pad + (x - bMinX) / (bMaxX - bMinX) * (size - 2 * pad);
  const sy = y => (size - pad) - (y - bMinY) / (bMaxY - bMinY) * (size - 2 * pad);
  const pts = track.map(p => sx(p.x).toFixed(1) + ',' + sy(p.y).toFixed(1)).join(' ');
  const ox = sx(0), oy = sy(0);
  const rankColors = { good: '#3db86e', bad: '#d64545', avg: '#888' };
  const color = rankColors[m.rank] || '#7eb8f7';
  return '<svg id="video-maneuver-overlay" class="video-overlay" width="' + size + '" height="' + size + '">'
    + '<rect width="' + size + '" height="' + size + '" rx="6" fill="rgba(0,0,0,.45)"/>'
    + '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
    + '<circle cx="' + ox + '" cy="' + oy + '" r="2.5" fill="#fff" stroke="' + color + '" stroke-width="1"/>'
    + '<text x="' + (size - pad) + '" y="' + (pad + 6) + '" text-anchor="end" font-size="7" fill="rgba(255,255,255,.5)">&#8593; wind</text>'
    + '<circle id="video-maneuver-dot" cx="' + ox + '" cy="' + oy + '" r="3.5" fill="#fff" stroke="rgba(0,0,0,.6)" stroke-width="1"/>'
    + '</svg>';
}

function _updateVideoManeuverDot(utc, m) {
  const track = m && m.track;
  if (!track || track.length < 2) return;
  const mStart = _parseManUtcMs(m.ts);
  if (mStart == null) return;
  const currentT = (utc.getTime() - mStart) / 1000;
  let best = track[0], bestDt = Math.abs(track[0].t - currentT);
  for (let i = 1; i < track.length; i++) {
    const dt = Math.abs(track[i].t - currentT);
    if (dt < bestDt) { bestDt = dt; best = track[i]; }
  }
  const size = 120, pad = 8;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of track) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const half = Math.max(5, Math.max(maxX - minX, maxY - minY) / 2 + 3);
  const svgX = pad + (best.x - (cx - half)) / (2 * half) * (size - 2 * pad);
  const svgY = (size - pad) - (best.y - (cy - half)) / (2 * half) * (size - 2 * pad);
  const dot = document.getElementById('video-maneuver-dot');
  if (dot) { dot.setAttribute('cx', svgX.toFixed(1)); dot.setAttribute('cy', svgY.toFixed(1)); }
}

// ---- Recovery bar (per-maneuver, BSP% vs entry) ---------------------------

function _renderVideoRecoverySvg(m) {
  if (!m || m.entry_bsp == null || m.entry_bsp <= 0) return '';
  const w = 28, h = 150, pad = 20, barX = 6, barW = 16;
  const barTop = pad, barBot = h - pad, barH = barBot - barTop;
  const maxPct = 120;
  const pct100Y = barBot - (100 / maxPct) * barH;
  let minMarker = '';
  if (m.min_bsp != null) {
    const minPct = Math.max(0, Math.min(maxPct, (m.min_bsp / m.entry_bsp) * 100));
    const minY = barBot - (minPct / maxPct) * barH;
    minMarker = '<line x1="' + barX + '" y1="' + minY.toFixed(1) + '" x2="' + (barX + barW) + '" y2="' + minY.toFixed(1)
      + '" stroke="#d64545" stroke-width="1.5" stroke-dasharray="2,1"/>';
  }
  return '<svg id="video-recovery-overlay" class="video-overlay" width="' + w + '" height="' + h + '">'
    + '<rect x="' + barX + '" y="' + barTop + '" width="' + barW + '" height="' + barH + '" rx="3" fill="rgba(0,0,0,.5)" stroke="rgba(255,255,255,.2)" stroke-width="0.5"/>'
    + '<rect id="video-recovery-fill" x="' + barX + '" y="' + barBot + '" width="' + barW + '" height="0" rx="3" fill="#3db86e"/>'
    + '<line x1="' + (barX - 2) + '" y1="' + pct100Y.toFixed(1) + '" x2="' + (barX + barW + 2) + '" y2="' + pct100Y.toFixed(1)
    + '" stroke="#fff" stroke-width="1.5"/>'
    + '<text x="' + (barX + barW / 2) + '" y="' + (pct100Y - 3).toFixed(1) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.7)">'
    + m.entry_bsp.toFixed(1) + '</text>'
    + minMarker
    + '<text id="video-recovery-pct" x="' + (barX + barW / 2) + '" y="' + (barTop - 5) + '" text-anchor="middle" font-size="11" font-weight="700" font-family="monospace" fill="#fff">--%</text>'
    + '<text x="' + (barX + barW / 2) + '" y="' + (barBot + 12) + '" text-anchor="middle" font-size="6" fill="rgba(255,255,255,.5)">BSP%</text>'
    + '</svg>';
}

function _updateVideoRecoveryBar(utc, m) {
  if (!m || m.entry_bsp == null || m.entry_bsp <= 0) return;
  const sample = _videoSampleAt(utc.getTime());
  if (!sample || sample.stw == null) return;
  const pct = (sample.stw / m.entry_bsp) * 100;
  const maxPct = 120;
  const clampPct = Math.max(0, Math.min(maxPct, pct));
  const pad = 20, barTop = pad, barBot = 150 - pad, barH = barBot - barTop, barX = 6;
  const fillH = (clampPct / maxPct) * barH;
  const fillY = barBot - fillH;
  let color;
  if (pct >= 100) color = '#3db86e';
  else if (pct >= 80) color = '#f59e0b';
  else if (pct >= 60) color = '#e87c1e';
  else color = '#d64545';
  const fill = document.getElementById('video-recovery-fill');
  if (fill) {
    fill.setAttribute('y', fillY.toFixed(1));
    fill.setAttribute('height', fillH.toFixed(1));
    fill.setAttribute('fill', color);
  }
  const pctEl = document.getElementById('video-recovery-pct');
  if (pctEl) pctEl.textContent = Math.round(pct) + '%';
}

// ---- Mount / unmount + tick -----------------------------------------------

// The YT IFrame API replaces #yt-player with an <iframe>, so we mount our
// SVGs as siblings of that iframe inside #yt-player-wrap (which stays put
// and carries the position:relative). Children of #yt-player itself would
// be wiped on player load.
function _overlayMount() {
  return document.getElementById('yt-player-wrap');
}

function _ensureAlwaysOnMounted() {
  const mount = _overlayMount();
  if (!mount) return;
  if (_videoGaugesOn && !document.getElementById('video-gauge-overlay')) {
    mount.insertAdjacentHTML('beforeend', _renderVideoGaugeSvg());
  }
  if (_videoTrackOn && !document.getElementById('video-course-overlay')) {
    const svg = _renderVideoCourseSvg();
    if (svg) mount.insertAdjacentHTML('beforeend', svg);
  }
}

function _ensureManeuverOverlaysMounted(m) {
  const mount = _overlayMount();
  if (!mount) return;
  const mid = m.id != null ? String(m.id) : String(m.ts);
  // Cancel any pending fade-out unmount — we're back inside a window.
  if (_pendingUnmountTimer) { clearTimeout(_pendingUnmountTimer); _pendingUnmountTimer = null; _pendingUnmountId = null; }
  if (mid !== _videoMountedManId) {
    // Active maneuver changed — drop the old per-maneuver SVGs so the new
    // ones render with their own geometry.
    _removeVideoOverlay('video-maneuver-overlay');
    _removeVideoOverlay('video-recovery-overlay');
    _videoMountedManId = mid;
  }
  if (_videoTrackOn && !document.getElementById('video-maneuver-overlay')) {
    const svg = _renderVideoManeuverSvg(m);
    if (svg) mount.insertAdjacentHTML('beforeend', svg);
  }
  if (_videoGaugesOn && !document.getElementById('video-recovery-overlay')) {
    const svg = _renderVideoRecoverySvg(m);
    if (svg) mount.insertAdjacentHTML('beforeend', svg);
  }
}

// Fade-out the per-maneuver overlays, then remove the DOM nodes after the
// CSS transition completes. A snapshot of the currently-mounted maneuver id
// is captured so that if the playhead re-enters a window before the timer
// fires, the scheduled removal can detect the swap and skip — we only tear
// down overlays that still belong to the original maneuver.
const _VIDEO_OVERLAY_FADE_MS = 1000;
let _pendingUnmountId = null;
let _pendingUnmountTimer = null;

function _fadeOutAndUnmountManeuverOverlays() {
  _hideClass('video-maneuver-overlay');
  _hideClass('video-recovery-overlay');
  if (_pendingUnmountTimer) clearTimeout(_pendingUnmountTimer);
  _pendingUnmountId = _videoMountedManId;
  _pendingUnmountTimer = setTimeout(() => {
    _pendingUnmountTimer = null;
    // If a new maneuver was mounted in the meantime, leave those alone.
    if (_videoMountedManId !== _pendingUnmountId) return;
    _removeVideoOverlay('video-maneuver-overlay');
    _removeVideoOverlay('video-recovery-overlay');
    _videoMountedManId = null;
  }, _VIDEO_OVERLAY_FADE_MS);
}

function _hideClass(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('show');
}

function _unmountManeuverOverlaysImmediate() {
  if (_pendingUnmountTimer) { clearTimeout(_pendingUnmountTimer); _pendingUnmountTimer = null; }
  _removeVideoOverlay('video-maneuver-overlay');
  _removeVideoOverlay('video-recovery-overlay');
  _videoMountedManId = null;
}

function _initVideoOverlayButtons() {
  _videoGaugesOn = _readOverlayFlag(_VIDEO_OVERLAY_LS_GAUGES);
  _videoTrackOn = _readOverlayFlag(_VIDEO_OVERLAY_LS_TRACK);
  _setOverlayBtnStyle(document.getElementById('video-gauges-btn'), _videoGaugesOn);
  _setOverlayBtnStyle(document.getElementById('video-track-btn'), _videoTrackOn);
  _restartOverlayTick();
}

function _currentVideoUtc() {
  // Prefer the live YT player position — it keeps ticking during natural
  // playback, unlike _playClock which only advances on scrubs + replay play.
  // Falls back to the shared clock when no video is loaded.
  try {
    if (_videoSync && _videoSync.player && typeof _videoSync.player.getCurrentTime === 'function') {
      const cur = _videoSync.player.getCurrentTime();
      if (cur != null && !isNaN(cur)) return _videoOffsetToUtc(cur);
    }
  } catch (e) { /* ignore */ }
  return _playClock.positionUtc;
}

function _videoOverlayTick() {
  if (!_videoGaugesOn && !_videoTrackOn) return;
  _ensureAlwaysOnMounted();
  const utc = _currentVideoUtc();

  // Always-on surfaces
  const gauge = document.getElementById('video-gauge-overlay');
  if (gauge) {
    if (_videoGaugesOn) { gauge.classList.add('show'); if (utc) _updateVideoGauge(utc); }
    else gauge.classList.remove('show');
  }
  const course = document.getElementById('video-course-overlay');
  if (course) {
    if (_videoTrackOn) { course.classList.add('show'); if (utc) _updateVideoCourseDot(utc); }
    else course.classList.remove('show');
  }

  // Per-maneuver surfaces
  const m = utc ? _findActiveManeuver(utc) : null;
  if (m) {
    _ensureManeuverOverlaysMounted(m);
    const man = document.getElementById('video-maneuver-overlay');
    if (man) {
      if (_videoTrackOn) { man.classList.add('show'); _updateVideoManeuverDot(utc, m); }
      else man.classList.remove('show');
    }
    const rec = document.getElementById('video-recovery-overlay');
    if (rec) {
      if (_videoGaugesOn) { rec.classList.add('show'); _updateVideoRecoveryBar(utc, m); }
      else rec.classList.remove('show');
    }
  } else if (_videoMountedManId !== null && _pendingUnmountTimer === null) {
    _fadeOutAndUnmountManeuverOverlays();
  }
}

let _videoOverlayTimer = null;
function _restartOverlayTick() {
  if (_videoOverlayTimer) { clearInterval(_videoOverlayTimer); _videoOverlayTimer = null; }
  if (!_videoGaugesOn) _removeVideoOverlay('video-gauge-overlay');
  if (!_videoTrackOn) _removeVideoOverlay('video-course-overlay');
  if (!_videoGaugesOn && !_videoTrackOn) { _unmountManeuverOverlaysImmediate(); return; }
  _videoOverlayTick();
  _videoOverlayTimer = setInterval(_videoOverlayTick, 200);
}

// Fire immediately on any producer event too, so scrubs + replay play feel
// snappy instead of waiting up to 200ms for the poll.
registerSurface('video-overlay', function(_utc) { _videoOverlayTick(); });

// ---------------------------------------------------------------------------
// Deep-link to a specific moment in the session via ?t= (#642).
//
// Accepts three formats to match YouTube share ergonomics:
//   • seconds            ?t=125          ?t=125.5
//   • readable           ?t=1m5s         ?t=2h10m         ?t=45s
//   • clock              ?t=1:05         ?t=01:23:45
//
// Offsets are measured from the session's start_utc, not from any video's
// own offset, since a session can have multiple linked videos. Out-of-range
// values are clamped to the session window.
// ---------------------------------------------------------------------------

let _deepLinkTargetUtc = null;

function _parseDeepLinkTime(raw) {
  if (raw == null) return null;
  const v = String(raw).trim();
  if (!v) return null;
  // Pure number → seconds
  if (/^\d+(?:\.\d+)?$/.test(v)) return parseFloat(v);
  // XhYmZs (any subset, at least one)
  const readable = v.match(/^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$/);
  if (readable && (readable[1] || readable[2] || readable[3])) {
    const h = parseFloat(readable[1] || 0);
    const mm = parseFloat(readable[2] || 0);
    const ss = parseFloat(readable[3] || 0);
    return h * 3600 + mm * 60 + ss;
  }
  // HH:MM:SS or MM:SS
  const clock = v.match(/^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$/);
  if (clock) {
    const h = parseFloat(clock[1] || 0);
    const mm = parseFloat(clock[2]);
    const ss = parseFloat(clock[3]);
    return h * 3600 + mm * 60 + ss;
  }
  return null;
}

function _fmtDeepLinkTime(totalSec) {
  const s = Math.max(0, Math.floor(totalSec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  let out = '';
  if (h) out += h + 'h';
  if (m || h) out += m + 'm';
  out += ss + 's';
  return out;
}

function _applyDeepLink() {
  if (!_session || !_session.start_utc) return;
  const params = new URLSearchParams(window.location.search);
  const raw = params.get('t');
  if (!raw) return;
  const seconds = _parseDeepLinkTime(raw);
  if (seconds == null) return;
  const sessionStart = new Date(_session.start_utc).getTime();
  const sessionEnd = _session.end_utc
    ? new Date(_session.end_utc).getTime()
    : Date.now();
  let targetMs = sessionStart + seconds * 1000;
  if (targetMs < sessionStart) targetMs = sessionStart;
  if (targetMs > sessionEnd) targetMs = sessionEnd;
  _deepLinkTargetUtc = new Date(targetMs);
  _seekTo(_deepLinkTargetUtc, 'deeplink');
}

async function shareVideoLink() {
  if (!_session || !_session.start_utc) {
    window.alert('Session data not loaded yet — try again in a moment.');
    return;
  }
  // Prefer the YT player's position while it's playing; fall back to the
  // shared clock so a user who only scrubbed the replay scrubber still
  // gets a useful share link.
  let utc = null;
  try {
    if (_videoSync && _videoSync.player && typeof _videoSync.player.getCurrentTime === 'function') {
      const cur = _videoSync.player.getCurrentTime();
      if (cur != null && !isNaN(cur) && typeof _videoOffsetToUtc === 'function') {
        utc = _videoOffsetToUtc(cur);
      }
    }
  } catch (e) { /* fall through */ }
  if (!utc) utc = _playClock.positionUtc;
  if (!utc) {
    window.alert('Play or scrub first so there is a timestamp to anchor the share link to.');
    return;
  }
  const sessionStart = new Date(_session.start_utc).getTime();
  const offsetSec = (utc.getTime() - sessionStart) / 1000;
  const t = _fmtDeepLinkTime(offsetSec);
  const url = location.origin + location.pathname + '?t=' + t;
  const btn = document.getElementById('video-share-btn');
  try {
    await navigator.clipboard.writeText(url);
    _flashShareBtn(btn, 'Copied!');
  } catch (e) {
    // Browsers without clipboard perms → fall back to a prompt the user
    // can copy from manually.
    window.prompt('Copy share link', url);
  }
}

function _flashShareBtn(btn, msg) {
  if (!btn) return;
  const orig = btn.textContent;
  const origBg = btn.style.background;
  const origColor = btn.style.color;
  btn.textContent = msg;
  btn.style.background = 'var(--success)';
  btn.style.color = 'var(--bg-primary)';
  setTimeout(() => {
    btn.textContent = orig;
    btn.style.background = origBg;
    btn.style.color = origColor;
  }, 1500);
}

// Hook into init() path without rewriting it: kick off replay load once the
// DOM is ready, and wire controls immediately. _loadReplayData() waits on
// the fetch, and the track map is loaded in parallel, so the polar overlay
// can only be drawn once both are ready — _setGradeViewActive is a no-op
// until _trackData is populated.
document.addEventListener('DOMContentLoaded', function() {
  _wireReplayControls();
  _loadReplayData();
  _loadCourseOverlay();
  _initVideoOverlayButtons();
});

// ---------------------------------------------------------------------------
// Hooks for the LLM panel (#697)
//
// llm.js needs to drive the same playback surfaces (track cursor, video, YT,
// audio player) when a citation is clicked. Rather than duplicate seek
// logic, expose two helpers that wrap the existing setPosition + play tick
// pipeline. helmlogReloadMoments lets the LLM panel refresh the moments
// list after Save-as-moment so the new row appears without a page reload.
// ---------------------------------------------------------------------------

window.helmlogSeekTo = function(offsetSeconds) {
  // offsetSeconds is relative to the race transcript pane's audio session
  // (i.e., the same time-base the LLM transcript builder used).
  const pane = _transcriptPanes['race'];
  if (!pane || !pane.audioStartUtcRaw) return;
  const raw = pane.audioStartUtcRaw;
  const audioStart = new Date(raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z');
  if (isNaN(audioStart.getTime())) return;
  const targetUtc = new Date(audioStart.getTime() + offsetSeconds * 1000);
  setPosition(targetUtc, {source: 'llm'});
  // Drive the audio surface — multi-channel via mc player, otherwise the
  // legacy single <audio>. Pause first so any currently-playing surface
  // doesn't fight the new seek.
  try { _pauseAllPlayback(); } catch (e) { /* swallow */ }
  if ((_session && _session.audio_channels || 1) > 1) {
    try { _mcOnSegmentClick(null, offsetSeconds, offsetSeconds + 30); } catch (e) { /* fall through */ }
  } else {
    const audioEl = document.getElementById('session-audio')
      || document.querySelector('#audio-body audio');
    if (audioEl) {
      audioEl.currentTime = offsetSeconds;
      audioEl.play().catch(() => {});
    }
  }
  // Start the playback clock so the play button shows as paused (i.e. is
  // active) and the track cursor advances.
  try { _startPlayTick(); _updateReplayControls(); } catch (e) { /* swallow */ }
};

window.helmlogReloadMoments = async function() {
  try { await loadMoments({renderList: true}); } catch (e) { /* swallow */ }
};

// ---------------------------------------------------------------------------
// Go
// ---------------------------------------------------------------------------

init();
