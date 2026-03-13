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
let _maneuvers = []; // loaded maneuver list
let _maneuverMarkers = []; // Leaflet markers for maneuvers

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

async function init() {
  await initTimezone();

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
    loadNotes();
    if (_session.end_utc) loadPolar();
  }
  if (_session.has_audio && _session.audio_session_id) {
    loadTranscript();
    loadAudio();
  }
  loadSharing();
  renderExports();
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
  document.getElementById('session-name').innerHTML = esc(s.name) + badge + peerBadge;

  const start = fmtTime(s.start_utc);
  const end = s.end_utc ? fmtTime(s.end_utc) : 'in progress';
  const dur = (s.end_utc && s.duration_s != null) ? ' (' + fmtDuration(Math.round(s.duration_s)) + ')' : '';
  document.getElementById('session-meta').innerHTML = s.date + ' &middot; ' + start + ' &rarr; ' + end + dur;
}

// ---------------------------------------------------------------------------
// Track map
// ---------------------------------------------------------------------------

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

  const feature = geojson.features[0];
  const coords = feature.geometry.coordinates;
  const rawTimestamps = feature.properties.timestamps || [];
  const latLngs = coords.map(c => [c[1], c[0]]);
  const timestamps = rawTimestamps.map(t => new Date(t.endsWith('Z') || t.includes('+') ? t : t + 'Z'));
  const line = L.polyline(latLngs, {color: '#2563eb', weight: 4}).addTo(_map);

  L.circleMarker(latLngs[0], {radius: 6, color: '#22c55e', fillColor: '#22c55e', fillOpacity: 1})
    .addTo(_map).bindPopup('Start');
  L.circleMarker(latLngs[latLngs.length - 1], {radius: 6, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 1})
    .addTo(_map).bindPopup('Finish');

  const cursor = L.circleMarker([0, 0], {
    radius: 7, color: '#facc15', fillColor: '#facc15', fillOpacity: 1, weight: 2,
  });

  _trackData = {latLngs, timestamps, line, cursor};

  // Click track → seek video + update boat settings
  line.on('click', function(e) {
    const idx = _nearestIndex(e.latlng);
    _moveCursorToIndex(idx);
    _seekVideoToIndex(idx);
    _updateBoatSettingsForUtc(_utcForIndex(idx));
  });

  _map.fitBounds(line.getBounds(), {padding: [20, 20]});
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
  _trackData.cursor.setLatLng(_trackData.latLngs[idx]).addTo(_map);
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

  _videoSync = {
    syncUtc: new Date(vid.sync_utc),
    syncOffsetS: vid.sync_offset_s || 0,
    durationS: vid.duration_s || 0,
    player: null,
    videoId: vid.video_id,
    allVideos: videos,
    activeIdx: videos.indexOf(vid),
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

  // Load YouTube IFrame API
  const tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
}

// YouTube API calls this global function when ready
function onYouTubeIframeAPIReady() {
  _ytReady = true;
  if (_videoSync) _createPlayer(_videoSync.videoId);
}

function _createPlayer(videoId) {
  if (_videoSync.player) {
    _videoSync.player.loadVideoById(videoId);
    return;
  }
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
      onStateChange: _onPlayerStateChange,
    },
  });
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
  // YT.PlayerState.PLAYING = 1
  if (event.data === 1) {
    _startSyncTimer();
  } else {
    _stopSyncTimer();
    // Update cursor on pause too
    _syncMapToVideo();
  }
}

function _startSyncTimer() {
  _stopSyncTimer();
  _syncTimer = setInterval(_syncMapToVideo, 500);
}

function _stopSyncTimer() {
  if (_syncTimer) { clearInterval(_syncTimer); _syncTimer = null; }
}

function _syncMapToVideo() {
  if (!_videoSync || !_videoSync.player || !_trackData) return;
  if (typeof _videoSync.player.getCurrentTime !== 'function') return;

  const videoTime = _videoSync.player.getCurrentTime();
  const utc = _videoOffsetToUtc(videoTime);
  if (!utc) return;

  const idx = _indexForUtc(utc);
  _moveCursorToIndex(idx);
  _updateBoatSettingsForUtc(utc);
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

function toggleSection(name) {
  const body = document.getElementById(name + '-body');
  const toggle = document.getElementById(name + '-toggle');
  if (!body) return;
  _collapsed[name] = !_collapsed[name];
  body.style.display = _collapsed[name] ? 'none' : '';
  if (toggle) toggle.innerHTML = _collapsed[name] ? '&#9654;' : '&#9660;';
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
      const link = '<a href="' + esc(v.youtube_url) + '" target="_blank" style="color:#7eb8f7">' + ttl + '</a>';
      const del = '<button onclick="deleteVideo(' + v.id + ')" style="color:#ef4444;background:none;border:none;cursor:pointer;font-size:.8rem;margin-left:8px">&#10005;</button>';
      return '<div style="margin-bottom:4px">' + lbl + link + del + '</div>';
    }).join('');
  } else {
    body.innerHTML = '<span style="color:#8892a4">No videos linked</span>';
  }
  body.innerHTML += _videoAddForm();
}

function _videoAddForm() {
  const startUtc = _session.start_utc || '';
  const defaultSync = startUtc ? new Date(startUtc).toISOString().substring(0, 19) : '';
  return '<div id="video-add-form" style="display:none;margin-top:8px">'
    + '<input id="video-url" class="field" placeholder="YouTube URL" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="video-label" class="field" placeholder="Label (e.g. Bow cam)" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<div style="font-size:.72rem;color:#8892a4;margin-bottom:2px">Sync calibration (optional):</div>'
    + '<input id="video-sync-utc" class="field" type="datetime-local" step="1" value="' + defaultSync + '" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<input id="video-sync-pos" class="field" placeholder="Video position (mm:ss)" style="width:100%;margin-bottom:4px;padding:6px 8px;font-size:.82rem"/>'
    + '<button class="btn-export" style="background:#2563eb;color:#fff;border-color:#2563eb" onclick="submitAddVideo()">Add Video</button>'
    + ' <button onclick="document.getElementById(\'video-add-form\').style.display=\'none\'" style="background:none;border:none;color:#8892a4;cursor:pointer;font-size:.82rem">Cancel</button>'
    + '</div>'
    + '<button onclick="document.getElementById(\'video-add-form\').style.display=\'\'" style="font-size:.78rem;color:#7eb8f7;background:none;border:none;cursor:pointer;padding:4px 0;margin-top:4px">+ Add Video</button>';
}

async function submitAddVideo() {
  const url = document.getElementById('video-url').value.trim();
  const label = document.getElementById('video-label').value.trim();
  const syncUtcVal = document.getElementById('video-sync-utc').value;
  const syncPosVal = document.getElementById('video-sync-pos').value.trim();
  if (!url) { alert('YouTube URL is required'); return; }
  const syncUtc = syncUtcVal ? (syncUtcVal.includes('Z') ? syncUtcVal : syncUtcVal + 'Z') : new Date().toISOString();
  const syncOffsetS = syncPosVal ? parseVideoPosition(syncPosVal) : 0;
  if (syncOffsetS === null) { alert('Video position must be mm:ss or seconds'); return; }
  const resp = await fetch('/api/sessions/' + SESSION_ID + '/videos', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({youtube_url: url, label, sync_utc: syncUtc, sync_offset_s: syncOffsetS})
  });
  if (!resp.ok) { alert('Failed: ' + resp.status); return; }
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

  let html = '<div id="results-list">';
  html += results.map(res => {
    const name = esc(res.sail_number + (res.boat_name ? ' — ' + res.boat_name : ''));
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

  const nextPlace = results.length + 1;
  html += '<div class="results-row" style="border-bottom:none;margin-top:4px">'
    + '<span class="results-place">' + nextPlace + '.</span>'
    + '<div style="position:relative;flex:1">'
    + '<input class="boat-picker-input" id="picker-input" placeholder="Search boat\u2026" autocomplete="off"'
    + ' oninput="filterBoats(this.value)" onfocus="openPicker()" onblur="closePicker()"/>'
    + '<div class="boat-dropdown" id="picker-dropdown" style="display:none"></div>'
    + '</div></div>';

  body.innerHTML = html;
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
  if (!html) html = '<div class="boat-option" style="color:#8892a4;cursor:default">No boats found</div>';
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

async function loadCrew() {
  const card = document.getElementById('crew-card');
  card.style.display = '';
  const body = document.getElementById('crew-body');
  const r = await fetch('/api/races/' + SESSION_ID + '/crew');
  const data = await r.json();
  const crew = data.crew || [];
  if (crew.length) {
    body.innerHTML = crew.map(c =>
      '<span style="color:#8892a4">' + esc(c.position.charAt(0).toUpperCase() + c.position.slice(1)) + ':</span> ' + esc(c.sailor)
    ).join(' &middot; ');
  } else {
    body.innerHTML = '<span style="color:#8892a4">No crew recorded</span>';
  }
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
      + '<span style="color:#8892a4;width:68px;flex-shrink:0">' + slot.charAt(0).toUpperCase() + slot.slice(1) + '</span>'
      + '<select id="sail-select-' + slot + '" style="flex:1;background:#1a2840;color:#e0e8f0;border:1px solid #2563eb;border-radius:4px;padding:3px 6px;font-size:.78rem">'
      + '<option value="">\u2014 none \u2014</option>' + opts
      + '</select></div>';
  });
  html += '<button class="btn-export" style="background:#2563eb;color:#fff;border-color:#2563eb;font-size:.78rem;margin-top:4px" onclick="saveSails()">Save Sails</button>';
  body.innerHTML = html;
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
  if (!r.ok) alert('Failed to save sails');
}

// ---------------------------------------------------------------------------
// Notes
// ---------------------------------------------------------------------------

async function loadNotes() {
  const card = document.getElementById('notes-card');
  card.style.display = '';
  const body = document.getElementById('notes-body');
  const r = await fetch('/api/sessions/' + SESSION_ID + '/notes');
  const notes = await r.json();
  if (notes.length) {
    body.innerHTML = notes.map(n => {
      const t = fmtTime(n.ts);
      let content = '';
      if (n.note_type === 'photo' && n.photo_path) {
        const src = '/notes/' + n.photo_path;
        content = '<img src="' + src + '" loading="lazy" style="max-width:100px;max-height:80px;border-radius:4px;cursor:pointer;margin-top:2px" onclick="window.open(this.dataset.src)" data-src="' + src + '"/>';
      } else if (n.note_type === 'settings' && n.body) {
        try {
          const obj = JSON.parse(n.body);
          content = Object.entries(obj).map(([k, v]) =>
            '<span style="color:#8892a4">' + esc(k) + ':</span> ' + esc(v)
          ).join(' &middot; ');
        } catch { content = esc(n.body); }
      } else {
        content = esc(n.body);
      }
      const del = '<button onclick="deleteNote(' + n.id + ')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:.8rem;padding:0 4px;float:right">&#10005;</button>';
      return '<div style="padding:4px 0;border-bottom:1px solid #0d1a2e;overflow:hidden">'
        + del + '<span style="color:#8892a4;margin-right:6px">' + t + '</span>' + content + '</div>';
    }).join('');
  } else {
    body.innerHTML = '<span style="color:#8892a4">No notes</span>';
  }
}

async function deleteNote(noteId) {
  await fetch('/api/notes/' + noteId, {method: 'DELETE'});
  loadNotes();
}

// ---------------------------------------------------------------------------
// Transcript
// ---------------------------------------------------------------------------

async function loadTranscript() {
  const card = document.getElementById('transcript-card');
  card.style.display = '';
  const body = document.getElementById('transcript-body');
  body.innerHTML = '<span style="color:#8892a4">Loading\u2026</span>';

  const r = await fetch('/api/audio/' + _session.audio_session_id + '/transcript');
  if (r.status === 404) {
    body.innerHTML = '<span style="color:#8892a4">No transcript yet. </span>'
      + '<button class="btn-export" style="font-size:.75rem" onclick="startTranscript()">&#9654; Transcribe</button>';
    return;
  }
  const t = await r.json();
  if (t.status === 'pending' || t.status === 'running') {
    body.innerHTML = '<span style="color:#facc15">Transcription in progress\u2026</span>';
    setTimeout(loadTranscript, 3000);
    return;
  }
  if (t.status === 'error') {
    body.innerHTML = '<span style="color:#f87171">Error: ' + esc(t.error_msg || 'unknown') + '</span>';
    return;
  }
  if (t.segments && t.segments.length > 0) {
    const blocks = [];
    for (const seg of t.segments) {
      const last = blocks[blocks.length - 1];
      if (last && last.speaker === seg.speaker) {
        last.text += ' ' + seg.text; last.end = seg.end;
      } else { blocks.push({...seg}); }
    }
    const speakers = [...new Set(blocks.map(b => b.speaker))];
    const palette = ['#7dd3fc', '#86efac', '#fde68a', '#fca5a5', '#c4b5fd', '#f9a8d4'];
    const color = s => palette[speakers.indexOf(s) % palette.length];
    const fmt = s => { const m = Math.floor(s / 60); return m + ':' + String(Math.floor(s % 60)).padStart(2, '0'); };
    body.innerHTML = '<div style="max-height:400px;overflow-y:auto;background:#0d1929;border-radius:6px;padding:8px">'
      + blocks.map(b =>
        '<div style="margin-bottom:8px">'
        + '<span style="color:' + color(b.speaker) + ';font-weight:600;font-size:.75rem">' + esc(b.speaker) + '</span>'
        + '<span style="color:#8892a4;font-size:.7rem;margin-left:4px">[' + fmt(b.start) + ']</span>'
        + '<div style="color:#c4cdd8;font-size:.8rem;margin-top:2px">' + esc(b.text.trim()) + '</div>'
        + '</div>'
      ).join('')
      + '</div>';
  } else {
    const text = t.text ? esc(t.text) : '(empty)';
    body.innerHTML = '<div style="font-size:.8rem;color:#c4cdd8;white-space:pre-wrap;max-height:300px;overflow-y:auto;background:#0d1929;border-radius:6px;padding:8px">' + text + '</div>';
  }
}

async function startTranscript() {
  const r = await fetch('/api/audio/' + _session.audio_session_id + '/transcribe', {method: 'POST'});
  if (!r.ok) { alert('Failed to start transcription'); return; }
  loadTranscript();
}

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------

function loadAudio() {
  const card = document.getElementById('audio-card');
  card.style.display = '';
  document.getElementById('audio-body').innerHTML =
    '<audio controls style="width:100%"><source src="/api/audio/' + _session.audio_session_id + '/stream" type="audio/wav"></audio>';
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
      html += '<button class="btn-export" style="background:#0d2818;border:1px solid #16a34a;color:#4ade80"'
        + ' onclick="unshareSession(\'' + esc(c.co_op_id) + '\')">'
        + esc(c.co_op_name) + ' &#10003;</button>';
    } else {
      html += '<button class="btn-export" style="background:#1e293b;border:1px solid #374151;color:#e8eaf0"'
        + ' onclick="shareSession(\'' + esc(c.co_op_id) + '\')">'
        + 'Share with ' + esc(c.co_op_name) + '</button>';
    }
  }
  html += '</div>';

  // Show sharing details
  if (data.sharing && data.sharing.length) {
    html += '<div style="margin-top:8px;font-size:.78rem;color:#8892a4">';
    for (const s of data.sharing) {
      html += '<div>Shared with <strong style="color:#e8eaf0">' + esc(s.co_op_name || s.co_op_id) + '</strong>';
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
// Polar Performance
// ---------------------------------------------------------------------------

let _polarData = null;

async function loadPolar() {
  try {
    const r = await fetch('/api/sessions/' + SESSION_ID + '/polar');
    if (!r.ok) return;
    const data = await r.json();
    if (!data.cells || !data.cells.length) return;

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
    summary.innerHTML =
      sign + avgDelta.toFixed(2) + ' kt weighted avg vs baseline &middot; '
      + above + ' bins above, ' + below + ' below'
      + (noBaseline ? ' &middot; ' + noBaseline + ' bins no baseline' : '')
      + ' &middot; ' + data.session_sample_count + ' samples'
      + ' &middot; <button onclick="rebuildPolarBaseline()" style="background:none;border:none;color:#7eb8f7;cursor:pointer;font-size:.78rem;text-decoration:underline;padding:0">Rebuild baseline</button>';
  } catch (e) { /* non-fatal */ }
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

const _TWS_COLORS = [
  [6, '#7dd3fc'],  [8, '#38bdf8'],  [10, '#2563eb'],
  [12, '#7c3aed'], [14, '#f97316'], [16, '#ef4444'],
  [18, '#dc2626'], [20, '#991b1b'],
];

function _twsColor(tws) {
  for (let i = _TWS_COLORS.length - 1; i >= 0; i--) {
    if (tws >= _TWS_COLORS[i][0]) return _TWS_COLORS[i][1];
  }
  return '#94a3b8';
}

function renderPolarDiagram() {
  const canvas = document.getElementById('polar-canvas');
  if (!canvas || !_polarData) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = 30;
  const maxRadius = H - 50;

  ctx.clearRect(0, 0, W, H);

  // Determine BSP range for scaling
  let maxBsp = 0;
  for (const c of _polarData.cells) {
    if (c.session_mean != null) maxBsp = Math.max(maxBsp, c.session_mean);
    if (c.baseline_mean != null) maxBsp = Math.max(maxBsp, c.baseline_mean);
    if (c.baseline_p90 != null) maxBsp = Math.max(maxBsp, c.baseline_p90);
  }
  maxBsp = Math.ceil(maxBsp) + 1;
  if (maxBsp < 4) maxBsp = 4;
  const scale = maxRadius / maxBsp;

  // Draw concentric BSP circles
  ctx.strokeStyle = '#1e3a5f';
  ctx.lineWidth = 0.5;
  ctx.setLineDash([3, 3]);
  ctx.font = '11px monospace';
  ctx.fillStyle = '#8892a4';
  for (let bsp = 1; bsp <= maxBsp; bsp++) {
    const r = bsp * scale;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI);
    ctx.stroke();
    ctx.fillText(bsp + '', cx + r + 3, cy + 4);
  }

  // Draw radial TWA lines
  ctx.strokeStyle = '#1e3a5f';
  for (let deg = 0; deg <= 180; deg += 30) {
    const rad = deg * Math.PI / 180;
    const x2 = cx + maxBsp * scale * Math.sin(rad);
    const y2 = cy + maxBsp * scale * Math.cos(rad);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    // Label
    const lx = cx + (maxBsp * scale + 14) * Math.sin(rad);
    const ly = cy + (maxBsp * scale + 14) * Math.cos(rad);
    ctx.fillText(deg + '\u00b0', lx - 10, ly + 4);
  }
  ctx.setLineDash([]);

  // Group baseline cells by TWS
  const baselineByTws = {};
  for (const c of _polarData.cells) {
    if (c.baseline_mean == null) continue;
    if (!baselineByTws[c.tws]) baselineByTws[c.tws] = [];
    baselineByTws[c.tws].push(c);
  }

  // Draw baseline curves
  const drawnTws = [];
  for (const tws of Object.keys(baselineByTws).map(Number).sort((a, b) => a - b)) {
    const pts = baselineByTws[tws].sort((a, b) => a.twa - b.twa);
    if (pts.length < 2) continue;
    const color = _twsColor(tws);
    drawnTws.push({tws, color});

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.globalAlpha = 0.7;
    ctx.beginPath();
    for (let i = 0; i < pts.length; i++) {
      const rad = pts[i].twa * Math.PI / 180;
      const r = pts[i].baseline_mean * scale;
      const x = cx + r * Math.sin(rad);
      const y = cy + r * Math.cos(rad);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // Draw session points
  for (const c of _polarData.cells) {
    if (c.session_mean == null) continue;
    const rad = c.twa * Math.PI / 180;
    const r = c.session_mean * scale;
    const x = cx + r * Math.sin(rad);
    const y = cy + r * Math.cos(rad);

    const dotColor = c.delta == null ? '#94a3b8'
      : c.delta >= 0 ? '#22c55e' : '#ef4444';
    const dotSize = Math.min(6, Math.max(3, Math.log2(c.samples + 1) * 1.5));

    ctx.beginPath();
    ctx.arc(x, y, dotSize, 0, 2 * Math.PI);
    ctx.fillStyle = dotColor;
    ctx.fill();
    ctx.strokeStyle = '#0a1628';
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  // Legend
  const legend = document.getElementById('polar-legend');
  if (legend && drawnTws.length) {
    legend.innerHTML = 'Baseline curves: '
      + drawnTws.map(d =>
        '<span style="color:' + d.color + '">\u25cf ' + d.tws + ' kt</span>'
      ).join(' &nbsp; ')
      + ' &nbsp; Session: '
      + '<span style="color:#22c55e">\u25cf faster</span> '
      + '<span style="color:#ef4444">\u25cf slower</span> '
      + '<span style="color:#94a3b8">\u25cf no baseline</span>';
  }
}

// --- Heatmap ---

function _deltaColor(delta) {
  if (delta == null) return '#1e293b';
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

function renderPolarHeatmap() {
  const container = document.getElementById('polar-heatmap');
  if (!container || !_polarData) return;

  const data = _polarData;
  const cellMap = {};
  for (const c of data.cells) {
    cellMap[c.tws + ',' + c.twa] = c;
  }

  let html = '<table style="border-collapse:collapse;font-size:.72rem;width:100%">';

  // Header row: TWA labels
  html += '<tr><th style="padding:2px 4px;color:#8892a4;text-align:right;font-weight:normal">TWS\\TWA</th>';
  for (const twa of data.twa_bins) {
    html += '<th style="padding:2px 4px;color:#8892a4;font-weight:normal;min-width:36px">' + twa + '\u00b0</th>';
  }
  html += '</tr>';

  // One row per TWS
  for (const tws of data.tws_bins) {
    html += '<tr><td style="padding:2px 4px;color:#8892a4;text-align:right;white-space:nowrap">' + tws + ' kt</td>';
    for (const twa of data.twa_bins) {
      const c = cellMap[tws + ',' + twa];
      if (!c) {
        html += '<td style="padding:2px 4px;background:#0d1929;border:1px solid #0a1628"></td>';
        continue;
      }
      const bg = _deltaColor(c.delta);
      const textColor = c.delta == null ? '#8892a4' : '#e8eaf0';
      const text = c.delta != null
        ? (c.delta >= 0 ? '+' : '') + c.delta.toFixed(2)
        : c.session_mean != null ? c.session_mean.toFixed(1) : '';
      const title = 'TWS=' + tws + ' TWA=' + twa + '\u00b0'
        + '\nSession BSP: ' + (c.session_mean != null ? c.session_mean.toFixed(2) : 'n/a')
        + '\nBaseline: ' + (c.baseline_mean != null ? c.baseline_mean.toFixed(2) : 'n/a')
        + '\nP90: ' + (c.baseline_p90 != null ? c.baseline_p90.toFixed(2) : 'n/a')
        + '\nSamples: ' + c.samples;
      html += '<td style="padding:2px 4px;background:' + bg + ';border:1px solid #0a1628;'
        + 'color:' + textColor + ';text-align:center;cursor:default" title="' + title + '">'
        + text + '</td>';
    }
    html += '</tr>';
  }
  html += '</table>';
  container.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Maneuvers
// ---------------------------------------------------------------------------

const _MANEUVER_COLORS = { tack: '#3b82f6', gybe: '#f97316', rounding: '#22c55e' };

async function loadManeuvers() {
  const r = await fetch('/api/sessions/' + SESSION_ID + '/maneuvers');
  if (!r.ok) return;
  _maneuvers = await r.json();
  renderManeuverCard();
  if (_map && _maneuvers.length) _addManeuverMarkers();
}

function renderManeuverCard() {
  const card = document.getElementById('maneuvers-card');
  const body = document.getElementById('maneuvers-body');
  card.style.display = '';

  if (!_maneuvers.length) {
    body.innerHTML = '<span style="color:#8892a4">No maneuvers detected. Click &#8635; Detect to analyse this session.</span>';
    return;
  }

  const tacks = _maneuvers.filter(m => m.type === 'tack').length;
  const gybes = _maneuvers.filter(m => m.type === 'gybe').length;
  const roundings = _maneuvers.filter(m => m.type === 'rounding').length;
  const summary = '<div style="color:#8892a4;font-size:.75rem;margin-bottom:6px">'
    + tacks + ' tack' + (tacks !== 1 ? 's' : '')
    + ' &middot; ' + gybes + ' gybe' + (gybes !== 1 ? 's' : '')
    + ' &middot; ' + roundings + ' rounding' + (roundings !== 1 ? 's' : '')
    + '</div>';

  let rows = _maneuvers.map((m, idx) => {
    const color = _MANEUVER_COLORS[m.type] || '#8892a4';
    const typeBadge = '<span style="color:' + color + ';font-weight:600">' + esc(m.type) + '</span>';
    const t = fmtTime(m.ts);
    const dur = m.duration_sec != null ? m.duration_sec.toFixed(1) + ' s' : '—';
    const loss = m.loss_kts != null ? m.loss_kts.toFixed(2) + ' kt' : '—';
    const cond = (m.twa_bin != null ? m.twa_bin + '° TWA' : '') + (m.tws_bin != null ? (m.twa_bin != null ? ', ' : '') + m.tws_bin + ' kt TWS' : '');
    return '<tr id="mrow-' + idx + '" style="cursor:pointer" onclick="highlightManeuver(' + idx + ')">'
      + '<td>' + typeBadge + '</td>'
      + '<td>' + t + '</td>'
      + '<td>' + dur + '</td>'
      + '<td>' + loss + '</td>'
      + '<td>' + esc(cond || '—') + '</td>'
      + '</tr>';
  }).join('');

  body.innerHTML = summary
    + '<table class="maneuver-table"><thead><tr>'
    + '<th>Type</th><th>Time</th><th>Duration</th><th>BSP Loss</th><th>Conditions</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

function _addManeuverMarkers() {
  // Remove old markers
  _maneuverMarkers.forEach(m => m.remove());
  _maneuverMarkers = [];

  _maneuvers.forEach((m, idx) => {
    if (m.lat == null || m.lon == null) return;
    const color = _MANEUVER_COLORS[m.type] || '#8892a4';
    const marker = L.circleMarker([m.lat, m.lon], {
      radius: 7,
      color: color,
      fillColor: color,
      fillOpacity: 0.85,
      weight: 2,
    })
      .addTo(_map)
      .bindPopup(
        '<b style="color:' + color + '">' + m.type + '</b><br>'
        + fmtTime(m.ts)
        + (m.duration_sec != null ? '<br>' + m.duration_sec.toFixed(1) + ' s' : '')
        + (m.loss_kts != null ? '<br>' + m.loss_kts.toFixed(2) + ' kt loss' : '')
      );
    marker.on('click', function() { highlightManeuver(idx); });
    _maneuverMarkers.push(marker);
  });
}

function highlightManeuver(idx) {
  // Highlight table row
  document.querySelectorAll('.maneuver-table tr').forEach(r => r.classList.remove('active-row'));
  const row = document.getElementById('mrow-' + idx);
  if (row) {
    row.classList.add('active-row');
    row.scrollIntoView({block: 'nearest'});
  }
  // Move map cursor to maneuver position
  const m = _maneuvers[idx];
  if (m && _trackData) {
    const ts = new Date(m.ts.endsWith('Z') || m.ts.includes('+') ? m.ts : m.ts + 'Z');
    const trackIdx = _indexForUtc(ts);
    _moveCursorToIndex(trackIdx);
    _seekVideoToIndex(trackIdx);
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
      color: '#2563eb', weight: 3, opacity: 0.7,
    }).addTo(_wfMap);
    _wfCursor = L.circleMarker([0, 0], {
      radius: 6, color: '#facc15', fillColor: '#facc15', fillOpacity: 1, weight: 2,
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
    const marker = L.circleMarker([m.lat, m.lon], {
      radius: 5, color: '#f97316', fillColor: '#f97316', fillOpacity: 0.9, weight: 1,
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
  const colors = ['#ef4444', '#e8eaf0', '#22c55e']; // port, center, starboard

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
  ctx.strokeStyle = '#1e3a5f'; ctx.lineWidth = 0.5; ctx.setLineDash([3, 3]);
  for (let v = twdMin; v <= twdMax; v += 2) {
    const y = yForTwd(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + chartW, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Axis labels
  ctx.fillStyle = '#8892a4'; ctx.font = '11px monospace';
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

  ctx.strokeStyle = '#1e3a5f'; ctx.lineWidth = 0.5; ctx.setLineDash([3, 3]);
  for (let v = twsMin; v <= twsMax; v += 2) {
    const y = yForTws(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + chartW, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  ctx.fillStyle = '#8892a4';
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
  ctx.fillStyle = '#8892a4';
  const stepMin = Math.max(1, Math.floor(dur / 60 / 8));
  for (let m = 0; m <= dur / 60; m += stepMin) {
    const x = xForT(m * 60);
    ctx.fillText(m + 'm', x - 6, twsY0 + chartH + 14);
  }

  // Vertical hairline at current time
  if (currentS >= 0) {
    const x = xForT(currentS);
    ctx.strokeStyle = '#facc15'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, twsY0 + chartH); ctx.stroke();
  }

  // Legend
  const labels = ['Port', 'Center', 'Stbd'];
  let lx = pad.l + 10;
  for (let i = 0; i < 3; i++) {
    ctx.fillStyle = colors[i];
    ctx.fillRect(lx, pad.t - 14, 16, 8);
    ctx.fillStyle = '#8892a4';
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

async function loadBoatSettings() {
  const card = document.getElementById('boat-settings-card');
  if (_session.type === 'debrief') return;
  card.style.display = '';

  try {
    const r = await fetch('/api/boat-settings/parameters');
    _bsParams = await r.json();
  } catch (e) { console.error('boat settings params error', e); return; }

  // Resolve settings at race start time initially
  const asOf = _session.end_utc || _session.start_utc;
  await _fetchAndRenderBoatSettings(asOf);
}

async function _fetchAndRenderBoatSettings(asOf) {
  if (!asOf || !_bsParams) return;
  _bsLastAsOf = asOf;
  try {
    const r = await fetch('/api/boat-settings/resolve?race_id=' + SESSION_ID
      + '&as_of=' + encodeURIComponent(asOf));
    _bsResolved = await r.json();
  } catch (e) { console.error('boat settings resolve error', e); return; }
  _renderBoatSettingsPanel();
}

function _renderBoatSettingsPanel() {
  const body = document.getElementById('boat-settings-body');
  if (!_bsParams || !_bsResolved) return;

  // Build lookup: parameter name → resolved entry
  const byParam = {};
  for (const entry of _bsResolved) byParam[entry.parameter] = entry;

  let html = '';
  let anyValue = false;

  for (const cat of _bsParams.categories) {
    // Check if any param in this category has a value
    const catHasValues = cat.parameters.some(p => byParam[p.name]);
    if (!catHasValues) continue;

    html += '<div class="setup-cat-header" onclick="toggleSetupCatSession(\'' + cat.category + '\')">';
    html += '<span class="setup-cat-label">' + esc(cat.label) + '</span>';
    html += '<span class="setup-cat-chevron" id="bs-cat-chev-' + cat.category + '">\u25BC</span>';
    html += '</div>';
    html += '<div class="setup-cat-body" id="bs-cat-' + cat.category + '">';

    for (const p of cat.parameters) {
      const entry = byParam[p.name];
      if (!entry) continue;
      anyValue = true;

      html += '<div class="bs-row">';
      html += '<span class="bs-label">' + esc(p.label) + '</span>';
      html += '<span class="bs-value">' + esc(entry.value) + '</span>';
      if (p.unit) html += '<span class="bs-unit">' + esc(p.unit) + '</span>';

      // Source badge for race-specific or transcript settings
      if (entry.race_id !== null) {
        const src = entry.source.startsWith('transcript') ? 'transcript' : 'race';
        const srcLabel = entry.source.startsWith('transcript') ? 'transcript' : 'race';
        html += '<span class="bs-source-badge ' + src + '">' + srcLabel + '</span>';
      }

      // Show superseded boat-level value
      if (entry.supersedes_value) {
        html += '<span class="bs-superseded">' + esc(entry.supersedes_value);
        if (p.unit) html += ' ' + esc(p.unit);
        html += '</span>';
      }

      html += '</div>';
    }
    html += '</div>';
  }

  if (!anyValue) {
    html = '<span class="bs-empty">No boat settings recorded</span>';
  }

  body.innerHTML = html;
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
// Go
// ---------------------------------------------------------------------------

init();
