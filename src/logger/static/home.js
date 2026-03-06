const cfg = document.getElementById('app-config');
initGrafana(cfg.dataset.grafanaPort, cfg.dataset.grafanaUid, cfg.dataset.skPort);
const g = document.getElementById('grafana-nav');
if (g) g.href = GRAFANA_BASE + '/d/' + GRAFANA_UID + '/sailing-data?refresh=10s';
const s = document.getElementById('signalk-nav');
if (s) { s.href = SK_BASE; s.style.display = ''; }
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

function render(s) {
  if(s.timezone) _tz = s.timezone;
  document.getElementById('header-sub').textContent =
    `${s.weekday} · ${s.event || '(no event)'}`;

  const evSec = document.getElementById('event-section');
  if(!s.event_is_default && !s.current_debrief) {
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

  const instCard = document.getElementById('instruments-card');
  const crewCard = document.getElementById('crew-card');
  const btnDebriefLast = document.getElementById('btn-debrief-last');
  const todaySummary = document.getElementById('today-summary');
  const controlsDiv = document.getElementById('controls');

  const isIdle = !cur && !s.current_debrief;
  const isRacing = !!cur;
  const isDebrief = !!s.current_debrief;

  // --- Instruments & crew: visible only during a race ---
  instCard.classList.toggle('hidden', !isRacing);
  crewCard.classList.toggle('hidden', !isRacing);
  // --- Controls: hidden during debrief ---
  controlsDiv.classList.toggle('hidden', isDebrief);

  if(cur) {
    curCard.classList.remove('hidden');
    btnEnd.classList.remove('hidden');
    btnStartRace.classList.add('hidden');
    btnStartPractice.classList.add('hidden');
    btnDebriefLast.classList.add('hidden');
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
    // Hide start buttons during debrief
    btnStartRace.classList.add('hidden');
    btnStartPractice.classList.add('hidden');
    btnDebriefLast.classList.add('hidden');
  } else {
    debriefCard.classList.add('hidden');
    debriefStartMs = null;
  }

  btnStartRace.textContent = `▶ START RACE ${s.next_race_num}`;

  // --- Debrief last race button: show when idle, has recorder, and finished races exist ---
  const lastFinished = (s.today_races || []).filter(r => r.end_utc).slice(-1)[0];
  if (isIdle && s.has_recorder && lastFinished) {
    btnDebriefLast.classList.remove('hidden');
    btnDebriefLast.textContent = '🎙 DEBRIEF ' + lastFinished.name;
    btnDebriefLast.dataset.raceId = lastFinished.id;
  } else {
    btnDebriefLast.classList.add('hidden');
  }

  // --- Compact today's summary (idle only) ---
  if (isIdle && s.today_races && s.today_races.length) {
    const finished = s.today_races.filter(r => r.end_utc);
    const last = finished.length ? finished[finished.length - 1] : null;
    const parts = [];
    if (finished.length) parts.push(finished.length + ' race' + (finished.length > 1 ? 's' : '') + ' today');
    if (last) parts.push('last: ' + last.name);
    todaySummary.classList.remove('hidden');
    document.getElementById('today-summary-text').textContent = parts.join(' · ');
  } else {
    todaySummary.classList.add('hidden');
  }
}

function tick() {
  const now = new Date();
  document.getElementById('inst-time').textContent =
    now.toISOString().substring(11,19) + ' UTC';
  if(curRaceStartMs) {
    const elapsed = Math.floor((Date.now() - curRaceStartMs) / 1000);
    document.getElementById('cur-duration').textContent = fmtDuration(elapsed);
  }
  if(debriefStartMs) {
    const elapsed = Math.floor((Date.now() - debriefStartMs) / 1000);
    document.getElementById('debrief-duration').textContent = fmtDuration(elapsed);
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
  } else {
    const err = await resp.json().catch(()=>null);
    const msg = err && err.detail ? err.detail : 'Failed to start session';
    alert(msg);
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

async function startDebriefLast() {
  const btn = document.getElementById('btn-debrief-last');
  const raceId = btn && btn.dataset.raceId;
  if (raceId) await startDebrief(parseInt(raceId, 10));
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
  const syncOffsetS = syncPosVal ? parseVideoPosition(syncPosVal) : 0;
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
loadRecentSailors();
checkSystemHealth();
setInterval(checkSystemHealth, 30000);
document.querySelectorAll('.crew-input').forEach(inp => {
  inp.addEventListener('focus', () => { focusedCrewInput = inp; });
});
