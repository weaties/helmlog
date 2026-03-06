/* history.js — Session History page logic */

const cfg = document.getElementById('app-config');
initGrafana(cfg.dataset.grafanaPort, cfg.dataset.grafanaUid);
const GRAFANA_URL = GRAFANA_BASE;
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

function render(data) {
  const el = document.getElementById('results');
  if (!data.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions found</div>';
    document.getElementById('pager').innerHTML = '';
    return;
  }
  el.innerHTML = data.sessions.map(s => {
    const start = fmtTimeShort(s.start_utc);
    const end = s.end_utc ? fmtTimeShort(s.end_utc) : 'in progress';
    const dur = (s.end_utc && s.duration_s != null) ? ' (' + fmtDuration(Math.round(s.duration_s)) + ')' : '';
    const typeClass = s.type === 'race' ? 'badge-race' : s.type === 'practice' ? 'badge-practice' : 'badge-debrief';
    const badge = '<span class="badge ' + typeClass + '">' + s.type.toUpperCase() + '</span>';
    const parent = s.parent_race_name ? '<div class="session-meta">Debrief of ' + s.parent_race_name + '</div>' : '';

    // --- Toggle buttons: Results, Crew, Sails, Notes, Videos, Transcript ---
    let toggles = '';
    if (s.type !== 'debrief') {
      toggles += '<button class="btn-export" id="hist-results-btn-' + s.id + '" onclick="toggleHistoryResults(' + s.id + ')">Results ▶</button>';
      toggles += '<button class="btn-export" id="hist-crew-btn-' + s.id + '" onclick="toggleHistoryCrew(' + s.id + ')">Crew ▶</button>';
      toggles += '<button class="btn-export" id="hist-sails-btn-' + s.id + '" onclick="toggleHistorySails(' + s.id + ')">Sails ▶</button>';
      toggles += '<button class="btn-export" id="hist-notes-btn-' + s.id + '" onclick="toggleHistoryNotes(' + s.id + ')">Notes ▶</button>';
      toggles += '<button class="btn-export" id="hist-videos-btn-' + s.id + '" onclick="toggleHistoryVideos(' + s.id + ')">Videos ▶</button>';
    }
    if (s.has_audio && s.audio_session_id) {
      toggles += '<button class="btn-export" id="hist-transcript-btn-' + s.id + '" onclick="toggleHistoryTranscript(' + s.id + ',' + s.audio_session_id + ')">Transcript ▶</button>';
    }
    const togglesHtml = toggles ? '<div class="session-exports">' + toggles + '</div>' : '';

    // --- Download links ---
    let downloads = '';
    if (s.type !== 'debrief' && s.end_utc) {
      const from = new Date(s.start_utc).getTime();
      const to = new Date(s.end_utc).getTime();
      downloads += '<a class="btn-export" href="/api/races/' + s.id + '/export.csv">&#8595; CSV</a>';
      downloads += '<a class="btn-export" href="/api/races/' + s.id + '/export.gpx">&#8595; GPX</a>';
      downloads += '<a class="btn-export btn-grafana" href="' + GRAFANA_URL + '/d/' + GRAFANA_UID + '/sailing-data?from=' + from + '&to=' + to + '&orgId=1&refresh=" target="_blank">&#128202; Grafana</a>';
    }
    if (s.has_audio && s.audio_session_id) {
      downloads += '<a class="btn-export" href="/api/audio/' + s.audio_session_id + '/download">&#8595; WAV</a>';
    }
    const downloadsHtml = downloads ? '<div class="session-exports">' + downloads + '</div>' : '';

    // --- Expandable panels (order matches toggle buttons) ---
    const resultsPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-results-' + s.id + '" style="display:none"></div>'
      : '';
    const crewPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-crew-' + s.id + '" style="display:none"></div>'
      : '';
    const sailsPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-sails-' + s.id + '" style="display:none"></div>'
      : '';
    const notesPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-notes-' + s.id + '" style="display:none"></div>'
      : '';
    const videosPanel = s.type !== 'debrief'
      ? '<div class="session-results" id="hist-videos-' + s.id + '" data-start-utc="' + s.start_utc + '" style="display:none"></div>'
      : '';
    const transcriptPanel = s.has_audio && s.audio_session_id
      ? '<div class="session-results" id="hist-transcript-' + s.id + '" style="display:none"></div>'
      : '';

    // --- Audio playback at the bottom ---
    const audioHtml = (s.has_audio && s.audio_session_id)
      ? '<div style="margin-top:6px"><audio controls style="width:100%">'
        + '<source src="/api/audio/' + s.audio_session_id + '/stream" type="audio/wav">'
        + '</audio></div>'
      : '';

    return '<div class="card"><div class="session-name">' + s.name + badge + '</div>'
      + '<div class="session-meta">' + s.date + ' &nbsp;·&nbsp; ' + start + ' → ' + end + dur + '</div>'
      + parent
      + togglesHtml + resultsPanel + crewPanel + sailsPanel + notesPanel + videosPanel + transcriptPanel
      + downloadsHtml + audioHtml + '</div>';
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

async function toggleHistoryCrew(sessionId) {
  const el = document.getElementById('hist-crew-' + sessionId);
  const btn = document.getElementById('hist-crew-btn-' + sessionId);
  if (!el) return;
  if (el.style.display !== 'none') {
    el.style.display = 'none';
    if (btn) btn.textContent = 'Crew ▶';
    return;
  }
  el.innerHTML = '<span style="color:#8892a4;font-size:.8rem">Loading…</span>';
  const r = await fetch('/api/races/' + sessionId + '/crew');
  const data = await r.json();
  const crew = data.crew || [];
  if (crew.length) {
    el.innerHTML = '<div style="font-size:.82rem">' + crew.map(c =>
      '<span style="color:#8892a4">' + c.position.charAt(0).toUpperCase() + c.position.slice(1) + ':</span> ' + c.sailor
    ).join(' &nbsp;·&nbsp; ') + '</div>';
  } else {
    el.innerHTML = '<span style="color:#8892a4;font-size:.8rem">No crew recorded</span>';
  }
  el.style.display = '';
  if (btn) btn.textContent = 'Crew ▼';
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
    if (btn) btn.textContent = 'Videos ▶';
    return;
  }
  await _loadVideos(sessionId, el);
  el.style.display = '';
  if (btn) btn.textContent = 'Videos ▼';
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
    + ' <button onclick="document.getElementById(\'hist-video-add-form-' + sessionId + '\').style.display=\'none\'" style="background:none;border:none;color:#8892a4;cursor:pointer;font-size:.82rem">Cancel</button>'
    + '</div>'
    + '<button onclick="document.getElementById(\'hist-video-add-form-' + sessionId + '\').style.display=\'\'" style="font-size:.78rem;color:#7eb8f7;background:none;border:none;cursor:pointer;padding:2px 0">+ Add Video</button>';
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
  const syncOffsetS = syncPosVal ? parseVideoPosition(syncPosVal) : 0;
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
    if (btn) btn.textContent = 'Sails ▶';
    return;
  }
  await _loadSailsForHistory(sessionId, el);
  el.style.display = '';
  if (btn) btn.textContent = 'Sails ▼';
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
    if (btn) btn.textContent = 'Transcript ▶';
    return;
  }
  el.style.display = '';
  if (btn) btn.textContent = 'Transcript ▼';
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
    el.innerHTML = '<span style="color:#facc15;font-size:.8rem">Transcription in progress…</span>';
    setTimeout(() => _loadTranscript(sessionId, audioSessionId, el), 3000);
    return;
  }
  if (t.status === 'error') {
    el.innerHTML = '<span style="color:#f87171;font-size:.8rem">Error: ' + (t.error_msg || 'unknown') + '</span>';
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
initTimezone().then(() => load());
