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
  loadVideos();
  if (_session.type !== 'debrief') {
    loadResults();
    loadCrew();
    loadSails();
    loadNotes();
  }
  if (_session.has_audio && _session.audio_session_id) {
    loadTranscript();
    loadAudio();
  }
  renderExports();
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function renderHeader() {
  const s = _session;
  const typeClass = s.type === 'race' ? 'badge-race' : s.type === 'practice' ? 'badge-practice' : 'badge-debrief';
  const badge = '<span class="badge ' + typeClass + '">' + s.type.toUpperCase() + '</span>';
  document.getElementById('session-name').innerHTML = esc(s.name) + badge;

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

  // Click track → seek video
  line.on('click', function(e) {
    const idx = _nearestIndex(e.latlng);
    _moveCursorToIndex(idx);
    _seekVideoToIndex(idx);
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

const _collapsed = {};

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
// Go
// ---------------------------------------------------------------------------

init();
