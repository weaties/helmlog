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
    const r = await fetch('/api/state?_t=' + Date.now());
    if (!r.ok) { console.error('state fetch failed:', r.status); return; }
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
  const btnDebriefLast = document.getElementById('btn-debrief-last');
  const todaySummary = document.getElementById('today-summary');
  const controlsDiv = document.getElementById('controls');

  const btnSynth = document.getElementById('btn-synthesize');
  const isIdle = !cur && !s.current_debrief;
  const isRacing = !!cur;
  const isDebrief = !!s.current_debrief;

  // --- Instruments: visible only during a race ---
  instCard.classList.toggle('hidden', !isRacing);
  // --- Controls: hidden during debrief ---
  controlsDiv.classList.toggle('hidden', isDebrief);

  // --- Synthesize button: visible when idle and user has developer flag ---
  btnSynth.classList.toggle('hidden', !isIdle || !_isDeveloper);

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
      _crewLoadedForRaceId = cur.id;
      if (_crewMetaLoaded) loadCrewCurrentValues();
    }
    document.getElementById('btn-note').style.display = '';
  } else {
    curCard.classList.add('hidden');
    btnEnd.classList.add('hidden');
    btnStartRace.classList.remove('hidden');
    btnStartPractice.classList.remove('hidden');
    curRaceStartMs = null;
    if (_crewLoadedForRaceId !== null && _crewMetaLoaded) loadCrewCurrentValues();
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

  // --- Refresh boat setup values when race changes ---
  refreshSetupForRace();
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

let crewExpanded = false;
let _crewMetaLoaded = false;
let _crewLoadedForRaceId = null;
let _crewPositions = [];   // [{id, name, display_order}]
let _crewUsers = [];       // [{id, name, email, role}]
let _crewSaveTimer = null;

let instExpanded = false;

function toggleInstruments() {
  instExpanded = !instExpanded;
  document.getElementById('inst-body').style.display = instExpanded ? '' : 'none';
  document.getElementById('inst-chevron').textContent = instExpanded ? '▼' : '▶';
}

function toggleCrew() {
  crewExpanded = !crewExpanded;
  document.getElementById('crew-body').style.display = crewExpanded ? '' : 'none';
  document.getElementById('crew-chevron').textContent = crewExpanded ? '\u25BC' : '\u25B6';
  if (crewExpanded && !_crewMetaLoaded) loadCrewMeta();
}

async function loadCrewMeta() {
  try {
    const [posResp, userResp] = await Promise.all([
      fetch('/api/crew/positions'),
      fetch('/api/crew/users'),
    ]);
    _crewPositions = (await posResp.json()).positions || [];
    _crewUsers = (await userResp.json()).users || [];
    _crewMetaLoaded = true;
    renderCrewRows();
    await loadCrewCurrentValues();
  } catch(e) { console.error('crew meta error', e); }
}

function renderCrewRows() {
  const container = document.getElementById('crew-rows');
  if (!container) return;
  const role = typeof _userRole !== 'undefined' ? _userRole : 'viewer';
  const canEdit = role === 'admin' || role === 'crew';
  let html = '';
  for (const p of _crewPositions) {
    const label = p.name.charAt(0).toUpperCase() + p.name.slice(1);
    html += '<div class="crew-row" data-pos-id="' + p.id + '">';
    html += '<span class="crew-pos">' + escHtml(label) + '</span>';
    html += '<select class="crew-select" '
      + (canEdit ? 'onchange="onCrewChange()"' : 'disabled') + '>';
    html += '<option value="">\u2014</option>';
    for (const u of _crewUsers) {
      const n = escAttr(u.name || u.email);
      html += '<option value="' + u.id + '">' + n + '</option>';
    }
    html += '</select>';
    html += '</div>';
  }
  container.innerHTML = html;
}

async function loadCrewCurrentValues() {
  try {
    // Load race-level crew if a race is active, otherwise boat-level defaults
    let url = '/api/crew/defaults';
    if (state && state.current_race) {
      url = '/api/races/' + state.current_race.id + '/crew';
    }
    const r = await fetch(url);
    const data = await r.json();
    const crew = data.crew || [];
    setCrewInputs(crew);
    updateCrewSummary(crew);
  } catch (e) { console.error('crew current error', e); }
}

function setCrewInputs(crew) {
  const rows = document.querySelectorAll('#crew-rows .crew-row');
  rows.forEach(row => {
    const posId = parseInt(row.dataset.posId);
    const sel = row.querySelector('.crew-select');
    sel.value = '';
    sel.classList.remove('has-value');
    if (crew) {
      const entry = crew.find(c => c.position_id === posId);
      if (entry && entry.user_id) {
        sel.value = String(entry.user_id);
        sel.classList.add('has-value');
      }
    }
  });
}

function getCrewFromInputs() {
  const rows = document.querySelectorAll('#crew-rows .crew-row');
  const crew = [];
  rows.forEach(row => {
    const posId = parseInt(row.dataset.posId);
    const sel = row.querySelector('.crew-select');
    const userId = sel.value ? parseInt(sel.value) : null;
    if (userId) crew.push({position_id: posId, user_id: userId});
  });
  return crew;
}

function updateCrewSummary(crew) {
  const filled = crew ? crew.filter(c => c.user_id || c.user_name).length : 0;
  const el = document.getElementById('crew-summary');
  if (el) el.textContent = filled > 0 ? filled + ' assigned' : '';
}

function onCrewChange() {
  // Debounced auto-save (like boat setup)
  if (_crewSaveTimer) clearTimeout(_crewSaveTimer);
  const statusEl = document.getElementById('crew-status');
  if (statusEl) { statusEl.style.display = ''; statusEl.textContent = 'Saving...'; }
  _crewSaveTimer = setTimeout(() => saveCrew(), 600);
  // Update select border color
  document.querySelectorAll('#crew-rows .crew-select').forEach(sel => {
    sel.classList.toggle('has-value', !!sel.value);
  });
}

async function saveCrew() {
  const crew = getCrewFromInputs();
  const statusEl = document.getElementById('crew-status');
  try {
    let url, method;
    if (state && state.current_race) {
      url = '/api/races/' + state.current_race.id + '/crew';
      method = 'POST';
    } else {
      url = '/api/crew/defaults';
      method = 'POST';
    }
    await fetch(url, {
      method: method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(crew)
    });
    updateCrewSummary(crew);
    if (statusEl) { statusEl.textContent = 'Saved'; setTimeout(() => { statusEl.style.display = 'none'; }, 1500); }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Save failed'; }
    console.error('crew save error', e);
  }
}

// ---------------------------------------------------------------------------
// Boat Setup Panel
// ---------------------------------------------------------------------------

let setupExpanded = false;
let setupCatExpanded = { sail_controls: true };
let setupParams = null;
let setupCurrentValues = {};
let _setupSaveTimers = {};

function toggleSetup() {
  setupExpanded = !setupExpanded;
  document.getElementById('setup-body').style.display = setupExpanded ? '' : 'none';
  document.getElementById('setup-chevron').textContent = setupExpanded ? '\u25BC' : '\u25B6';
  if (setupExpanded && !setupParams) loadSetupParams();
}

function toggleSetupCat(cat) {
  setupCatExpanded[cat] = !setupCatExpanded[cat];
  const body = document.getElementById('setup-cat-' + cat);
  const chev = document.getElementById('setup-cat-chev-' + cat);
  if (body) body.style.display = setupCatExpanded[cat] ? '' : 'none';
  if (chev) chev.textContent = setupCatExpanded[cat] ? '\u25BC' : '\u25B6';
}

async function loadSetupParams() {
  try {
    const r = await fetch('/api/boat-settings/parameters');
    const data = await r.json();
    setupParams = data;
    renderSetupPanel(data);
    await loadSetupCurrentValues();
  } catch (e) { console.error('setup params error', e); }
}

function renderSetupPanel(data) {
  const container = document.getElementById('setup-categories');
  const role = typeof _userRole !== 'undefined' ? _userRole : 'viewer';
  const canEdit = role === 'admin' || role === 'crew';
  let html = '';
  for (const cat of data.categories) {
    const isOpen = setupCatExpanded[cat.category] || false;
    html += '<div class="setup-cat-header" onclick="toggleSetupCat(\'' + cat.category + '\')">';
    html += '<span class="setup-cat-label">' + cat.label + '</span>';
    html += '<span class="setup-cat-chevron" id="setup-cat-chev-' + cat.category + '">'
      + (isOpen ? '\u25BC' : '\u25B6') + '</span>';
    html += '</div>';
    html += '<div class="setup-cat-body" id="setup-cat-' + cat.category + '" style="display:'
      + (isOpen ? '' : 'none') + '">';
    for (const p of cat.parameters) {
      const curVal = setupCurrentValues[p.name] || '';
      html += '<div class="setup-row">';
      html += '<span class="setup-label">' + escHtml(p.label) + '</span>';
      if (p.name === 'weight_distribution') {
        html += '<select class="setup-select' + (curVal ? ' has-value' : '') + '" '
          + 'id="setup-' + p.name + '" '
          + (canEdit ? 'onchange="onSetupChange(\'' + p.name + '\')"' : 'disabled')
          + '>';
        html += '<option value="">--</option>';
        for (const preset of data.weight_distribution_presets) {
          const sel = curVal === preset ? ' selected' : '';
          html += '<option value="' + escAttr(preset) + '"' + sel + '>'
            + escHtml(preset) + '</option>';
        }
        html += '</select>';
      } else {
        html += '<input class="setup-input' + (curVal ? ' has-value' : '') + '" '
          + 'type="number" step="any" id="setup-' + p.name + '" '
          + 'value="' + escAttr(curVal) + '" '
          + 'inputmode="decimal" '
          + (canEdit ? 'onchange="onSetupChange(\'' + p.name + '\')"' : 'readonly')
          + ' placeholder="\u2014"/>';
      }
      if (p.unit) html += '<span class="setup-unit">' + escHtml(p.unit) + '</span>';
      html += '</div>';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escAttr(s) {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

async function loadSetupCurrentValues() {
  // Always load boat-level settings (race_id=null) for the manual UI.
  try {
    const r = await fetch('/api/boat-settings/current');
    const rows = await r.json();
    setupCurrentValues = {};
    for (const row of rows) {
      setupCurrentValues[row.parameter] = row.value;
      const el = document.getElementById('setup-' + row.parameter);
      if (el) {
        el.value = row.value;
        el.classList.toggle('has-value', !!row.value);
      }
    }
    updateSetupSummary();
  } catch (e) { console.error('setup current error', e); }
}

function updateSetupSummary() {
  const count = Object.keys(setupCurrentValues).length;
  const el = document.getElementById('setup-summary');
  if (el) el.textContent = count > 0 ? count + ' set' : '';
}

function onSetupChange(paramName) {
  const el = document.getElementById('setup-' + paramName);
  if (!el) return;
  const val = el.value.trim();
  el.classList.toggle('has-value', !!val);
  if (!val) return;
  setupCurrentValues[paramName] = val;
  updateSetupSummary();
  // Debounce save — 500ms after last change
  if (_setupSaveTimers[paramName]) clearTimeout(_setupSaveTimers[paramName]);
  _setupSaveTimers[paramName] = setTimeout(() => saveSetupValue(paramName, val), 500);
}

async function saveSetupValue(paramName, value) {
  // Manual UI settings are boat-level (race_id=null), not per-race.
  // Per-race settings come from transcript extraction with a race_id.
  const ts = new Date().toISOString();
  try {
    const resp = await fetch('/api/boat-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        race_id: null,
        source: 'manual',
        entries: [{ ts: ts, parameter: paramName, value: value }]
      })
    });
    if (resp.ok) {
      showSetupStatus('Saved ' + paramName.replace(/_/g, ' '));
    } else {
      showSetupStatus('Error saving ' + paramName.replace(/_/g, ' '), true);
    }
  } catch (e) {
    console.error('setup save error', e);
    showSetupStatus('Error saving', true);
  }
}

function showSetupStatus(msg, isError) {
  const el = document.getElementById('setup-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = isError ? '#fca5a5' : '#4ade80';
  el.style.display = '';
  setTimeout(() => { el.style.display = 'none'; }, 2000);
}

// Reload setup values when race state changes
function refreshSetupForRace() {
  if (setupParams) loadSetupCurrentValues();
}

async function startSession(type) {
  try {
    const resp = await fetch(`/api/races/start?session_type=${type}`, {method:'POST'});
    if(resp.ok) {
      // Boat-level crew defaults auto-apply via resolve_crew (#305)
    } else {
      const err = await resp.json().catch(()=>null);
      const msg = err && err.detail ? err.detail : 'Failed to start session';
      alert(msg);
    }
    await loadState();
  } catch(e) {
    console.error('startSession error', e);
    alert('Error: ' + e.message);
  }
}

let _endConfirmTimer = null;
let _endCountdownInterval = null;
const END_CONFIRM_SECONDS = 4;

function confirmEndRace() {
  const btn = document.getElementById('btn-end');
  if (btn.dataset.confirming === 'true') {
    // Second tap — actually end the race
    _clearEndConfirm(btn);
    endRace();
    return;
  }
  // First tap — start countdown
  btn.dataset.confirming = 'true';
  btn.classList.add('btn-end-confirm');
  let remaining = END_CONFIRM_SECONDS;
  btn.textContent = 'TAP TO CONFIRM (' + remaining + ')';
  _endCountdownInterval = setInterval(() => {
    remaining--;
    if (remaining <= 0) {
      _clearEndConfirm(btn);
      return;
    }
    btn.textContent = 'TAP TO CONFIRM (' + remaining + ')';
  }, 1000);
  _endConfirmTimer = setTimeout(() => _clearEndConfirm(btn), END_CONFIRM_SECONDS * 1000);
}

function _clearEndConfirm(btn) {
  clearTimeout(_endConfirmTimer);
  clearInterval(_endCountdownInterval);
  _endConfirmTimer = null;
  _endCountdownInterval = null;
  btn.dataset.confirming = '';
  btn.classList.remove('btn-end-confirm');
  if (state && state.current_race) {
    btn.textContent = '\u25A0 END ' + state.current_race.name;
  }
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
    const js = searchText.trim().replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    html += '<div class="boat-option boat-option-new" onmousedown="event.preventDefault()" onclick="selectNewBoat(' + raceId + ',\'' + js + '\')">+ Add &ldquo;' + esc + '&rdquo;</div>';
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
    + ' <button onclick="document.getElementById(\'video-add-form-' + sessionId + '\').style.display=\'none\'" style="background:none;border:none;color:#8892a4;cursor:pointer;font-size:.82rem">Cancel</button>'
    + '</div>'
    + '<button onclick="document.getElementById(\'video-add-form-' + sessionId + '\').style.display=\'\'" style="font-size:.78rem;color:#7eb8f7;background:none;border:none;cursor:pointer;padding:2px 0">+ Add Video</button>';
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

// ---- Synthesize race ----

let _synthMap = null;
let _synthRcMarker = null;
let _synthBuoyMarkers = [];
let _synthCycMarkers = [];
let _synthCourseLine = null;
let _synthWindArrow = null;
let _synthCustomSequence = [];
let _synthMarkOverrides = {};

function _synthMapReady() {
  return typeof L !== 'undefined' && _synthMap !== null;
}

async function toggleSynthPanel() {
  const panel = document.getElementById('synth-panel');
  const hidden = panel.classList.toggle('hidden');
  if (!hidden) {
    await loadCoopPeers();
    // Init map on first open
    if (!_synthMap) {
      setTimeout(initSynthMap, 100);
    }
  }
}

function initSynthMap() {
  if (typeof L === 'undefined') return;
  const el = document.getElementById('synth-map');
  if (!el || _synthMap) return;

  _synthMap = L.map('synth-map').setView([47.63, -122.40], 12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap', maxZoom: 18,
  }).addTo(_synthMap);

  // Click to place RC
  _synthMap.on('click', function(e) {
    const lat = e.latlng.lat;
    const lon = e.latlng.lng;
    document.getElementById('synth-lat').value = lat.toFixed(5);
    document.getElementById('synth-lon').value = lon.toFixed(5);
    document.getElementById('synth-rc-display').textContent =
      lat.toFixed(4) + ', ' + lon.toFixed(4);
    document.getElementById('synth-lat-field').classList.remove('hidden');
    placeRcMarker(lat, lon);
    updateSynthMarks();
  });

  // Show CYC marks on map
  loadCycMarksOnMap();
}

function placeRcMarker(lat, lon) {
  if (!_synthMapReady()) return;
  if (_synthRcMarker) {
    _synthRcMarker.setLatLng([lat, lon]);
  } else {
    _synthRcMarker = L.marker([lat, lon], {
      draggable: true,
      title: 'RC (Start/Finish)',
      icon: L.divIcon({
        className: '',
        html: '<div style="background:#ef4444;color:#fff;border-radius:50%;width:24px;height:24px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;border:2px solid #fff">RC</div>',
        iconSize: [24, 24],
        iconAnchor: [12, 12],
      }),
    }).addTo(_synthMap);
    _synthRcMarker.on('dragend', function() {
      const pos = _synthRcMarker.getLatLng();
      document.getElementById('synth-lat').value = pos.lat.toFixed(5);
      document.getElementById('synth-lon').value = pos.lng.toFixed(5);
      document.getElementById('synth-rc-display').textContent =
        pos.lat.toFixed(4) + ', ' + pos.lng.toFixed(4);
      updateSynthMarks();
    });
  }
  updateWindArrow(lat, lon);
}

function updateWindArrow(lat, lon) {
  if (!_synthMapReady()) return;
  const windDir = parseFloat(document.getElementById('synth-wind-dir').value) || 0;
  const arrowLen = 0.012;
  const rad = (windDir * Math.PI) / 180;
  const endLat = lat + arrowLen * Math.cos(rad);
  const endLon = lon + arrowLen * Math.sin(rad) / Math.cos(lat * Math.PI / 180);
  if (_synthWindArrow) _synthMap.removeLayer(_synthWindArrow);
  _synthWindArrow = L.polyline(
    [[lat, lon], [endLat, endLon]],
    {color: '#fbbf24', weight: 3, dashArray: '6,4', opacity: 0.8}
  ).addTo(_synthMap);
  // Arrowhead
  const headLen = 0.003;
  const a1 = rad + 2.6;
  const a2 = rad - 2.6;
  const h1 = [endLat + headLen * Math.cos(a1), endLon + headLen * Math.sin(a1) / Math.cos(lat * Math.PI / 180)];
  const h2 = [endLat + headLen * Math.cos(a2), endLon + headLen * Math.sin(a2) / Math.cos(lat * Math.PI / 180)];
  L.polyline([[endLat, endLon], h1], {color: '#fbbf24', weight: 3}).addTo(_synthMap);
  L.polyline([[endLat, endLon], h2], {color: '#fbbf24', weight: 3}).addTo(_synthMap);
}

function onSynthWindChange() {
  updateSynthMarks();
}

async function updateSynthMarks() {
  if (!_synthMapReady()) return;
  const lat = parseFloat(document.getElementById('synth-lat').value) || 47.63;
  const lon = parseFloat(document.getElementById('synth-lon').value) || -122.40;
  const windDir = parseFloat(document.getElementById('synth-wind-dir').value) || 0;
  const courseType = document.getElementById('synth-course').value;

  updateWindArrow(lat, lon);

  // Clear old buoy markers and drag overrides
  _synthBuoyMarkers.forEach(m => _synthMap.removeLayer(m));
  _synthBuoyMarkers = [];
  _synthMarkOverrides = {};
  if (_synthCourseLine) {
    _synthMap.removeLayer(_synthCourseLine);
    _synthCourseLine = null;
  }

  if (courseType === 'custom') return;

  try {
    const resp = await fetch('/api/courses/marks?wind_dir=' + windDir +
      '&start_lat=' + lat + '&start_lon=' + lon);
    if (!resp.ok) return;
    const data = await resp.json();
    const buoy = data.buoy_marks;

    // Determine which marks to show based on course type
    let markKeys;
    if (courseType === 'windward_leeward') {
      markKeys = ['S', 'A', 'X', 'F'];
    } else {
      markKeys = ['S', 'A', 'G', 'X', 'F'];
    }

    const lineCoords = [];
    for (const key of markKeys) {
      const m = buoy[key];
      if (!m) continue;
      const marker = L.marker([m.lat, m.lon], {
        draggable: true,
        title: key + ': ' + m.name,
        icon: L.divIcon({
          className: '',
          html: '<div style="background:#2563eb;color:#fff;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;border:2px solid #fff">' + key + '</div>',
          iconSize: [22, 22],
          iconAnchor: [11, 11],
        }),
      }).addTo(_synthMap);
      marker.bindTooltip(key + ': ' + m.name, {direction: 'top', offset: [0, -12]});
      marker._markKey = key;
      marker.on('dragend', function() {
        const pos = marker.getLatLng();
        _synthMarkOverrides[key] = {lat: pos.lat, lon: pos.lng};
        _updateCourseLine();
      });
      _synthBuoyMarkers.push(marker);
      lineCoords.push([m.lat, m.lon]);
    }

    if (lineCoords.length > 1) {
      _synthCourseLine = L.polyline(lineCoords, {
        color: '#7eb8f7', weight: 2, opacity: 0.7, dashArray: '4,6',
      }).addTo(_synthMap);
    }
  } catch (_) {}
}

async function loadCycMarksOnMap() {
  if (!_synthMapReady()) return;
  try {
    const resp = await fetch('/api/courses/marks');
    if (!resp.ok) return;
    const data = await resp.json();
    const cyc = data.cyc_marks;

    for (const [key, m] of Object.entries(cyc)) {
      const marker = L.marker([m.lat, m.lon], {
        title: key + ': ' + m.name,
        icon: L.divIcon({
          className: '',
          html: '<div style="background:#16a34a;color:#fff;border-radius:3px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;border:1px solid #fff;opacity:0.85">' + key + '</div>',
          iconSize: [20, 20],
          iconAnchor: [10, 10],
        }),
      }).addTo(_synthMap);
      marker.bindTooltip(key + ': ' + m.name, {direction: 'top', offset: [0, -10]});
      marker.on('click', function() { onCycMarkClick(key); });
      _synthCycMarkers.push(marker);
    }
  } catch (_) {}
}

function onCycMarkClick(markKey) {
  const courseType = document.getElementById('synth-course').value;
  if (courseType !== 'custom') return;

  const seq = _synthCustomSequence;
  // If last mark is same, undo
  if (seq.length && seq[seq.length - 1] === markKey) {
    seq.pop();
  } else {
    seq.push(markKey);
  }
  const seqStr = seq.join('-');
  document.getElementById('synth-marks').value = seqStr;
  drawCustomCourseLine();
}

async function drawCustomCourseLine() {
  if (!_synthMapReady()) return;
  if (_synthCourseLine) {
    _synthMap.removeLayer(_synthCourseLine);
    _synthCourseLine = null;
  }
  if (_synthCustomSequence.length < 2) return;

  try {
    const resp = await fetch('/api/courses/marks');
    if (!resp.ok) return;
    const data = await resp.json();
    const all = {...data.buoy_marks, ...data.cyc_marks};
    const coords = [];
    for (const key of _synthCustomSequence) {
      const m = all[key.toUpperCase()];
      if (m) coords.push([m.lat, m.lon]);
    }
    if (coords.length > 1) {
      _synthCourseLine = L.polyline(coords, {
        color: '#fbbf24', weight: 3, opacity: 0.8,
      }).addTo(_synthMap);
    }
  } catch (_) {}
}

function _updateCourseLine() {
  if (!_synthMapReady()) return;
  if (_synthCourseLine) {
    _synthMap.removeLayer(_synthCourseLine);
    _synthCourseLine = null;
  }
  const coords = _synthBuoyMarkers.map(m => {
    const pos = m.getLatLng();
    return [pos.lat, pos.lng];
  });
  if (coords.length > 1) {
    _synthCourseLine = L.polyline(coords, {
      color: '#7eb8f7', weight: 2, opacity: 0.7, dashArray: '4,6',
    }).addTo(_synthMap);
  }
}

function onSynthCourseChange() {
  const v = document.getElementById('synth-course').value;
  document.getElementById('synth-marks-field').classList.toggle('hidden', v !== 'custom');
  _synthCustomSequence = [];
  document.getElementById('synth-marks').value = '';
  updateSynthMarks();
}

// Track data imported from peer for collision avoidance (#246)
let _importedPeerTracks = null;
let _importedPeerInfo = null;
let _importedWindSeed = null;
let _importedWindParams = null;  // full wind params for shift magnitude, leg distance, etc.
let _importedStartUtc = null;    // source session start time for co-op synthesis

async function loadCoopPeers() {
  try {
    const resp = await fetch('/api/federation/co-ops');
    if (!resp.ok) return;
    const data = await resp.json();
    // Flatten peers from all co-ops, tagging each with co_op_id
    const allPeers = [];
    for (const coop of (data.co_ops || [])) {
      for (const p of (coop.peers || [])) {
        allPeers.push({...p, co_op_id: coop.co_op_id});
      }
    }
    const sel = document.getElementById('synth-peer');
    const cur = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    for (const p of allPeers) {
      const label = (p.boat_name || p.sail_number || p.fingerprint) +
        (p.sail_number && p.boat_name ? ' (' + p.sail_number + ')' : '');
      const opt = new Option(label, JSON.stringify({fp: p.fingerprint, coop: p.co_op_id}));
      sel.add(opt);
    }
    if (cur) sel.value = cur;

    // Also populate peer session picker for wind model import
    await loadPeerSessions(allPeers);
  } catch (_) {}
}

async function loadPeerSessions(peers) {
  const sel = document.getElementById('synth-peer-session');
  while (sel.options.length > 1) sel.remove(1);
  const btn = document.getElementById('synth-import-wind');
  btn.disabled = true;

  // Query each co-op for peer sessions
  const coopIds = [...new Set(peers.map(p => p.co_op_id).filter(Boolean))];
  for (const coopId of coopIds) {
    try {
      const resp = await fetch('/api/federation/co-ops/' + coopId + '/peer-sessions');
      if (!resp.ok) continue;
      const data = await resp.json();
      for (const peer of (data.peers || [])) {
        for (const s of (peer.sessions || [])) {
          if (s.status !== 'available') continue;
          // Only show synthesized sessions (they have wind models)
          if (s.session_type !== 'synthesized') continue;
          const label = (peer.boat_name || peer.sail_number || peer.fingerprint.slice(0, 8)) +
            ' \u2014 ' + (s.name || 'Session ' + s.session_id);
          const val = JSON.stringify({
            coop: coopId,
            fp: peer.fingerprint,
            sid: s.session_id,
            boat: peer.boat_name || peer.sail_number,
          });
          sel.add(new Option(label, val));
        }
      }
    } catch (_) {}
  }
  sel.onchange = function() { btn.disabled = !sel.value; };
}

async function importPeerWindModel() {
  const sel = document.getElementById('synth-peer-session');
  if (!sel.value) return;

  const btn = document.getElementById('synth-import-wind');
  const status = document.getElementById('synth-import-status');
  btn.disabled = true;
  btn.textContent = 'Loading...';
  status.style.display = '';
  status.textContent = 'Fetching wind model from peer...';

  try {
    const info = JSON.parse(sel.value);

    // Fetch wind model + start time
    const wfResp = await fetch(
      '/api/federation/co-ops/' + info.coop + '/peers/' + info.fp +
      '/sessions/' + info.sid + '/wind-field'
    );
    if (!wfResp.ok) {
      const err = await wfResp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to fetch wind model (' + wfResp.status + ')');
    }
    const wfData = await wfResp.json();
    const wp = wfData.wind_params;

    // Pre-fill synthesis form from imported wind model
    // The seed is critical — it determines the entire wind field (shifts, puffs, gradients)
    _importedWindSeed = wp.seed;
    _importedWindParams = wp;
    _importedStartUtc = wfData.start_utc || null;
    document.getElementById('synth-wind-dir').value = Math.round(wp.base_twd);
    document.getElementById('synth-tws-lo').value = wp.tws_low;
    document.getElementById('synth-tws-hi').value = wp.tws_high;
    if (wp.laps) document.getElementById('synth-laps').value = wp.laps;
    if (wp.ref_lat) document.getElementById('synth-lat').value = wp.ref_lat;
    if (wp.ref_lon) document.getElementById('synth-lon').value = wp.ref_lon;

    // Set course type
    if (wp.course_type) {
      document.getElementById('synth-course').value = wp.course_type;
      onSynthCourseChange();
    }
    if (wp.mark_sequence && wp.course_type === 'custom') {
      document.getElementById('synth-marks').value = wp.mark_sequence;
    }

    // Place RC marker on map
    if (wp.ref_lat && wp.ref_lon) {
      const lat = parseFloat(wp.ref_lat);
      const lon = parseFloat(wp.ref_lon);
      document.getElementById('synth-rc-display').textContent =
        lat.toFixed(4) + ', ' + lon.toFixed(4);
      document.getElementById('synth-lat-field').classList.remove('hidden');
      placeRcMarker(lat, lon);
      if (_synthMap) _synthMap.setView([lat, lon], 13);
    }

    // Update marks on map
    updateSynthMarks();

    // Set peer attribution
    const peerSel = document.getElementById('synth-peer');
    for (let i = 0; i < peerSel.options.length; i++) {
      try {
        const pv = JSON.parse(peerSel.options[i].value);
        if (pv.fp === info.fp && pv.coop === info.coop) {
          peerSel.selectedIndex = i;
          break;
        }
      } catch (_) {}
    }

    // Fetch peer's track for collision avoidance
    status.textContent = 'Fetching peer track for collision avoidance...';
    const trackResp = await fetch(
      '/api/federation/co-ops/' + info.coop + '/peers/' + info.fp +
      '/sessions/' + info.sid + '/track'
    );
    if (trackResp.ok) {
      const trackData = await trackResp.json();
      _importedPeerTracks = [trackData.track || []];
      _importedPeerInfo = info;
      const pts = (trackData.track || []).length;
      status.textContent = 'Imported wind model from ' + (info.boat || 'peer') +
        ' + ' + pts + ' track points for collision avoidance';
      status.style.color = '#16a34a';
    } else {
      _importedPeerTracks = null;
      _importedPeerInfo = null;
      status.textContent = 'Wind model imported (track not available for collision avoidance)';
      status.style.color = '#fbbf24';
    }

    // Enable collision avoidance checkbox
    document.getElementById('synth-collision').checked = !!_importedPeerTracks;
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
    status.style.color = '#ef4444';
    _importedPeerTracks = null;
    _importedPeerInfo = null;
    _importedStartUtc = null;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Import Wind';
  }
}

async function runSynthesize() {
  const btn = document.getElementById('synth-go');
  btn.disabled = true;
  btn.textContent = 'Generating...';
  const result = document.getElementById('synth-result');
  result.style.display = 'none';
  try {
    const body = {
      course_type: document.getElementById('synth-course').value,
      wind_direction: parseFloat(document.getElementById('synth-wind-dir').value) || 0,
      wind_speed_low: parseFloat(document.getElementById('synth-tws-lo').value) || 8,
      wind_speed_high: parseFloat(document.getElementById('synth-tws-hi').value) || 14,
      laps: parseInt(document.getElementById('synth-laps').value) || 2,
      start_lat: parseFloat(document.getElementById('synth-lat').value) || 47.63,
      start_lon: parseFloat(document.getElementById('synth-lon').value) || -122.40,
      seed: Math.floor(Math.random() * 100000),
      wind_seed: _importedWindSeed != null ? _importedWindSeed : undefined,
      start_utc: _importedStartUtc || undefined,
    };
    // Pass imported wind params that aren't in the form (shift magnitude, leg distance)
    if (_importedWindParams) {
      if (_importedWindParams.shift_magnitude_lo != null)
        body.shift_magnitude_low = _importedWindParams.shift_magnitude_lo;
      if (_importedWindParams.shift_magnitude_hi != null)
        body.shift_magnitude_high = _importedWindParams.shift_magnitude_hi;
      if (_importedWindParams.leg_distance_nm != null)
        body.leg_distance_nm = _importedWindParams.leg_distance_nm;
    }
    if (Object.keys(_synthMarkOverrides).length > 0) {
      body.mark_overrides = _synthMarkOverrides;
    }
    const marks = document.getElementById('synth-marks').value.trim();
    if (marks) body.mark_sequence = marks;
    const peerVal = document.getElementById('synth-peer').value;
    if (peerVal) {
      try {
        const peer = JSON.parse(peerVal);
        if (peer.fp) body.peer_fingerprint = peer.fp;
        if (peer.coop) body.peer_co_op_id = peer.coop;
      } catch (_) {}
    }
    // Collision avoidance (#246)
    const caEnabled = document.getElementById('synth-collision').checked;
    if (caEnabled && _importedPeerTracks && _importedPeerTracks.length > 0) {
      body.other_tracks = _importedPeerTracks;
      body.min_separation_m = parseFloat(document.getElementById('synth-min-sep').value) || 30;
    }
    const resp = await fetch('/api/sessions/synthesize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (resp.ok) {
      const data = await resp.json();
      const dur = Math.round(data.duration_s / 60);
      let msg = data.name + ' \u2014 ' + data.points + ' points, ' + dur + ' min';
      if (data.mark_warnings && data.mark_warnings.length > 0) {
        msg += '\n\u26a0\ufe0f ' + data.mark_warnings.join('\n\u26a0\ufe0f ');
      }
      result.textContent = msg;
      result.style.display = '';
      if (data.mark_warnings && data.mark_warnings.length > 0) {
        result.style.whiteSpace = 'pre-line';
        result.classList.add('synth-warning');
      } else {
        result.style.whiteSpace = '';
        result.classList.remove('synth-warning');
      }
      await loadState();
    } else {
      const err = await resp.json().catch(() => null);
      alert(err && err.detail ? err.detail : 'Synthesize failed');
    }
  } catch (e) {
    alert('Error: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate';
    // Clear imported state so next local synthesis gets a fresh seed
    _importedWindSeed = null;
    _importedWindParams = null;
    _importedStartUtc = null;
    _importedPeerTracks = null;
    _importedPeerInfo = null;
  }
}

loadState();
setInterval(loadState, 10000);
setInterval(tick, 1000);
loadInstruments();
setInterval(loadInstruments, 2000);
checkSystemHealth();
setInterval(checkSystemHealth, 30000);
