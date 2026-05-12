/* maneuvers.js — Cross-session maneuver browser (#584).
 *
 * Pools maneuvers from many sessions, filters by regatta/session/type/
 * direction/wind-range, and hands a set of (session_id, maneuver_id)
 * pairs to the /compare page for synced video playback.
 */

'use strict';

const TYPE_PILLS = ['tack', 'gybe', 'rounding', 'weather', 'leeward', 'start'];
const DIR_PILLS = [
  { label: 'P\u2192S', value: 'PS' },
  { label: 'S\u2192P', value: 'SP' },
];
const TWS_BANDS = [
  { label: '0-6', min: 0, max: 6 },
  { label: '6-8', min: 6, max: 8 },
  { label: '8-10', min: 8, max: 10 },
  { label: '10-12', min: 10, max: 12 },
  { label: '12-15', min: 12, max: 15 },
  { label: '15+', min: 15, max: null },
];

const state = {
  regattaId: '',
  sessionLimit: 20,
  sessionType: null,       // 'race'|'practice'|null — scopes both picker and query
  sessions: [],            // [{id, name, slug, start_utc, maneuver_count, ...}]
  selectedSessionIds: new Set(),
  type: null,              // 'tack'|'gybe'|'rounding'|null
  direction: null,         // 'PS'|'SP'|null
  postStart: false,        // drop pre-gun maneuvers when true
  twsBands: new Set(),     // indices into TWS_BANDS — empty = any
  twsMin: null,            // typed wind range floor; null = unset
  twsMax: null,            // typed wind range ceiling; null = unset
  hasVideo: false,
  tagFilter: new Set(),    // tag ids
  tagMode: 'and',          // 'and'|'or'
  availableTags: [],       // [{id, name, color, count}] pre-tag-filter
  maneuvers: [],
  selected: new Set(),     // composite keys "sid:mid"
  loading: false,
  rankBy: 'distance_loss_m', // metric driving the server-side Rank column
  // Multi-column sort: ordered list of { key, dir }. Click toggles
  // asc → desc → off; shift-click appends as secondary. Empty = natural
  // (server) order, which is chronological ascending.
  sortKeys: [],
};

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async function init() {
  renderPills();
  _wireHeaderClicks();
  await Promise.all([loadRegattas(), loadSessions()]);
  await reload();
})();

async function loadRegattas() {
  try {
    const r = await fetch('/api/maneuvers/regattas');
    if (!r.ok) return;
    const data = await r.json();
    const sel = document.getElementById('mv-regatta');
    for (const reg of data.regattas || []) {
      const opt = document.createElement('option');
      opt.value = String(reg.id);
      const label = reg.name + (reg.session_count ? ' (' + reg.session_count + ')' : '');
      opt.textContent = label;
      sel.appendChild(opt);
    }
  } catch (_e) { /* ok */ }
}

async function loadSessions() {
  const container = document.getElementById('mv-sessions');
  container.textContent = 'Loading...';
  const params = new URLSearchParams();
  if (state.regattaId) params.set('regatta_id', state.regattaId);
  if (state.sessionType) params.set('session_type', state.sessionType);
  params.set('limit', state.regattaId ? '200' : String(state.sessionLimit));
  try {
    const r = await fetch('/api/maneuvers/sessions?' + params.toString());
    if (!r.ok) { container.textContent = 'Failed to load sessions'; return; }
    const data = await r.json();
    state.sessions = data.sessions || [];
    state.selectedSessionIds = new Set(state.sessions.map(s => s.id));
    renderSessions();
  } catch (_e) { container.textContent = 'Failed to load sessions'; }
}

function renderSessions() {
  const container = document.getElementById('mv-sessions');
  if (!state.sessions.length) { container.textContent = 'No sessions found.'; return; }
  const rows = state.sessions.map(s => {
    const checked = state.selectedSessionIds.has(s.id) ? 'checked' : '';
    const date = (s.start_utc || '').slice(0, 10);
    return '<label>'
      + '<input type="checkbox" value="' + s.id + '" ' + checked
      + ' onchange="mvToggleSession(' + s.id + ', this.checked)"/>'
      + '<span>' + date + ' \u00b7 ' + _esc(s.name) + '</span>'
      + '<span class="mv-session-count">' + (s.maneuver_count || 0) + '</span>'
      + '</label>';
  }).join('');
  container.innerHTML = rows;
}

function mvToggleSession(id, on) {
  if (on) state.selectedSessionIds.add(id);
  else state.selectedSessionIds.delete(id);
  reload();
}

function mvSelectAllSessions(on) {
  if (on) state.selectedSessionIds = new Set(state.sessions.map(s => s.id));
  else state.selectedSessionIds = new Set();
  renderSessions();
  reload();
}

function mvOnRegattaChange() {
  state.regattaId = document.getElementById('mv-regatta').value;
  loadSessions().then(reload);
}

function mvReloadSessions() {
  const raw = document.getElementById('mv-session-limit').value;
  const n = Number(raw);
  if (Number.isFinite(n) && n > 0) state.sessionLimit = Math.min(100, Math.max(1, Math.floor(n)));
  loadSessions().then(reload);
}

// ---------------------------------------------------------------------------
// Filter pills
// ---------------------------------------------------------------------------

function renderPills() {
  const stEl = document.getElementById('mv-sessiontype-pills');
  if (stEl) {
    const entries = [
      { value: null, label: 'all' },
      { value: 'race', label: 'race' },
      { value: 'practice', label: 'practice' },
    ];
    stEl.innerHTML = entries.map(e => '<button class="mv-pill'
      + (state.sessionType === e.value ? ' active' : '')
      + '" onclick="mvSetSessionType(' + (e.value == null ? 'null' : '\'' + e.value + '\'') + ')">'
      + e.label + '</button>').join('');
  }

  const typeEl = document.getElementById('mv-type-pills');
  typeEl.innerHTML = '<button class="mv-pill' + (state.type == null ? ' active' : '')
    + '" onclick="mvSetType(null)">all</button>'
    + TYPE_PILLS.map(t => '<button class="mv-pill' + (state.type === t ? ' active' : '')
      + '" onclick="mvSetType(\'' + t + '\')">' + t + '</button>').join('');

  const dirEl = document.getElementById('mv-dir-pills');
  dirEl.innerHTML = '<button class="mv-pill' + (state.direction == null ? ' active' : '')
    + '" onclick="mvSetDir(null)">all</button>'
    + DIR_PILLS.map(d => '<button class="mv-pill' + (state.direction === d.value ? ' active' : '')
      + '" onclick="mvSetDir(\'' + d.value + '\')">' + d.label + '</button>').join('');

  const phaseEl = document.getElementById('mv-phase-pills');
  if (phaseEl) {
    phaseEl.innerHTML = '<button class="mv-pill' + (!state.postStart ? ' active' : '')
      + '" onclick="mvSetPhase(false)">all</button>'
      + '<button class="mv-pill' + (state.postStart ? ' active' : '')
      + '" onclick="mvSetPhase(true)" title="Hide pre-gun maneuvers">post-start</button>';
  }

  const twsEl = document.getElementById('mv-tws-pills');
  const anyActive = state.twsBands.size === 0;
  twsEl.innerHTML = '<button class="mv-pill' + (anyActive ? ' active' : '')
    + '" onclick="mvClearTws()">any</button>'
    + TWS_BANDS.map((b, i) => '<button class="mv-pill'
      + (state.twsBands.has(i) ? ' active' : '')
      + '" onclick="mvToggleTws(' + i + ')">' + b.label + '</button>').join('');
}

function mvSetType(t) { state.type = t; renderPills(); reload(); }
function mvSetDir(d) { state.direction = d; renderPills(); reload(); }
function mvToggleTws(i) {
  if (state.twsBands.has(i)) state.twsBands.delete(i);
  else state.twsBands.add(i);
  renderPills();
  reload();
}
function mvClearTws() { state.twsBands.clear(); renderPills(); reload(); }

function _parseTwsField(id) {
  const raw = (document.getElementById(id).value || '').trim();
  if (raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

let _twsRangeDebounce = null;
function mvOnTwsRangeChange() {
  // Debounce typing so each keystroke doesn't fire a fetch.
  state.twsMin = _parseTwsField('mv-tws-min');
  state.twsMax = _parseTwsField('mv-tws-max');
  clearTimeout(_twsRangeDebounce);
  _twsRangeDebounce = setTimeout(reload, 250);
}

function mvClearTwsRange() {
  document.getElementById('mv-tws-min').value = '';
  document.getElementById('mv-tws-max').value = '';
  state.twsMin = null;
  state.twsMax = null;
  reload();
}

function mvOnRankByChange() {
  const sel = document.getElementById('mv-rank-by');
  state.rankBy = sel ? sel.value : 'distance_loss_m';
  reload();
}

function mvSetPhase(on) { state.postStart = !!on; renderPills(); reload(); }
function mvSetSessionType(t) {
  state.sessionType = t;
  renderPills();
  loadSessions().then(reload);
}

// ---------------------------------------------------------------------------
// Fetch + render results
// ---------------------------------------------------------------------------

async function reload() {
  if (state.loading) return;
  state.loading = true;
  try {
    const params = new URLSearchParams();
    if (state.regattaId && state.selectedSessionIds.size === state.sessions.length) {
      params.set('regatta_id', state.regattaId);
    } else if (state.selectedSessionIds.size) {
      params.set('session_ids', [...state.selectedSessionIds].join(','));
    } else {
      // no sessions selected — show nothing
      state.maneuvers = [];
      renderResults();
      return;
    }
    if (state.type) params.set('type', state.type);
    if (state.direction) params.set('direction', state.direction);
    if (state.sessionType) params.set('session_type', state.sessionType);
    if (state.twsBands.size) {
      // Send selected bands as a comma-separated "min-max" list; the server
      // treats them as a logical OR so 8-10 + 10-12 captures the 8-12 range
      // and non-adjacent bands like 6-8 + 12-15 union correctly too.
      const bands = [...state.twsBands].sort((a, b) => a - b).map(i => {
        const b = TWS_BANDS[i];
        return b.max == null ? b.min + '-' : b.min + '-' + b.max;
      });
      params.set('tws_bands', bands.join(','));
    }
    if (state.twsMin != null) params.set('tws_min', String(state.twsMin));
    if (state.twsMax != null) params.set('tws_max', String(state.twsMax));
    if (state.hasVideo) params.set('has_video', '1');
    if (state.postStart) params.set('post_start', '1');
    if (state.rankBy && state.rankBy !== 'distance_loss_m') {
      params.set('rank_by', state.rankBy);
    }
    if (state.tagFilter.size) {
      params.set('tags', [...state.tagFilter].join(','));
      params.set('tag_mode', state.tagMode);
    }

    const r = await fetch('/api/maneuvers/browse?' + params.toString());
    if (!r.ok) { state.maneuvers = []; renderResults(); return; }
    const data = await r.json();
    state.maneuvers = data.maneuvers || [];
    state.availableTags = data.available_tags || [];
    // Purge selection entries that no longer appear in the filtered list
    const ids = new Set(state.maneuvers.map(m => m.session_id + ':' + m.id));
    for (const k of [...state.selected]) if (!ids.has(k)) state.selected.delete(k);
    renderTagFilterRow();
    renderResults();
  } finally {
    state.loading = false;
  }
}

function mvReload() {
  state.hasVideo = document.getElementById('mv-has-video').checked;
  reload();
}

// ---------------------------------------------------------------------------
// Multi-column sort
// ---------------------------------------------------------------------------

function _sortValue(m, key) {
  // Synthesized keys we expose as sort targets that don't map 1:1 to a
  // field on the maneuver dict.
  if (key === 'turn_angle_abs') {
    return m.turn_angle_deg == null ? null : Math.abs(Number(m.turn_angle_deg));
  }
  if (key === 'direction') {
    // Map turn-angle sign to its label, then sort lex (PS < SP).
    return m.turn_angle_deg == null ? null
      : (m.turn_angle_deg < 0 ? 'PS' : 'SP');
  }
  if (key === 'session_start_utc') {
    return m.session_start_utc || '';
  }
  const v = m[key];
  return v === undefined ? null : v;
}

function _compareForSort(a, b, key, dir, numeric) {
  const va = _sortValue(a, key);
  const vb = _sortValue(b, key);
  // Nulls always sort to the end, regardless of direction — keeps "no
  // data" rows from polluting the top when you sort ascending.
  if (va == null && vb == null) return 0;
  if (va == null) return 1;
  if (vb == null) return -1;
  let cmp;
  if (numeric) {
    cmp = Number(va) - Number(vb);
  } else {
    cmp = String(va).localeCompare(String(vb));
  }
  return dir === 'desc' ? -cmp : cmp;
}

function _sortedManeuvers() {
  if (!state.sortKeys.length) return state.maneuvers;
  // Stable sort: walk the keys in priority order, breaking ties downstream.
  const out = state.maneuvers.slice();
  // We need to know numeric vs lex for each key — pull from the header DOM
  // so the truth lives next to the column it describes.
  const numericByKey = {};
  document.querySelectorAll('th.mv-sortable').forEach(th => {
    numericByKey[th.dataset.sort] = th.dataset.numeric === '1';
  });
  out.sort((a, b) => {
    for (const sk of state.sortKeys) {
      const c = _compareForSort(a, b, sk.key, sk.dir, !!numericByKey[sk.key]);
      if (c !== 0) return c;
    }
    return 0;
  });
  return out;
}

function mvOnHeaderClick(ev, key) {
  const existing = state.sortKeys.findIndex(k => k.key === key);
  if (ev.shiftKey) {
    // Add or cycle a secondary key without clearing others.
    if (existing === -1) {
      state.sortKeys.push({ key, dir: 'asc' });
    } else if (state.sortKeys[existing].dir === 'asc') {
      state.sortKeys[existing].dir = 'desc';
    } else {
      state.sortKeys.splice(existing, 1);
    }
  } else {
    // Plain click — replace the whole sort with this key, cycling dir
    // if it was already the only key.
    if (state.sortKeys.length === 1 && state.sortKeys[0].key === key) {
      if (state.sortKeys[0].dir === 'asc') state.sortKeys[0].dir = 'desc';
      else state.sortKeys = [];
    } else {
      state.sortKeys = [{ key, dir: 'asc' }];
    }
  }
  _renderHeaderIndicators();
  renderResults();
}

function _renderHeaderIndicators() {
  document.querySelectorAll('th.mv-sortable').forEach(th => {
    const key = th.dataset.sort;
    const idx = state.sortKeys.findIndex(k => k.key === key);
    // Strip any existing indicator span.
    const existing = th.querySelector('.mv-sort-indicator');
    if (existing) existing.remove();
    if (idx === -1) return;
    const sk = state.sortKeys[idx];
    const arrow = sk.dir === 'asc' ? '↑' : '↓';
    const label = state.sortKeys.length > 1
      ? arrow + (idx + 1)  // show priority when there are multiple keys
      : arrow;
    const span = document.createElement('span');
    span.className = 'mv-sort-indicator';
    span.textContent = label;
    th.appendChild(span);
  });
}

function _wireHeaderClicks() {
  document.querySelectorAll('th.mv-sortable').forEach(th => {
    th.addEventListener('click', (ev) => mvOnHeaderClick(ev, th.dataset.sort));
  });
}

function renderResults() {
  const tbody = document.getElementById('mv-tbody');
  const empty = document.getElementById('mv-empty');
  document.getElementById('mv-count').textContent =
    state.maneuvers.length + ' maneuver' + (state.maneuvers.length !== 1 ? 's' : '');

  if (!state.maneuvers.length) {
    tbody.innerHTML = '';
    empty.style.display = '';
    updateSelectedCount();
    return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = _sortedManeuvers().map(m => {
    const k = m.session_id + ':' + m.id;
    const sel = state.selected.has(k);
    const date = (m.session_start_utc || '').slice(0, 10);
    const time = _fmtTime(m.ts);
    const twsTxt = m.entry_tws != null ? m.entry_tws.toFixed(1) : '—';
    const turnTxt = m.turn_angle_deg != null ? Math.abs(m.turn_angle_deg).toFixed(0) + '\u00b0' : '—';
    const durTxt = m.duration_sec != null ? m.duration_sec.toFixed(1) + 's' : '—';
    const turnPhaseTxt = m.time_to_head_to_wind_s != null
      ? m.time_to_head_to_wind_s.toFixed(1) + 's' : '—';
    const recoverPhaseTxt = m.time_to_recover_s != null
      ? m.time_to_recover_s.toFixed(1) + 's' : '—';
    const typeCls = 'mv-badge-' + (m.type || '');
    // Annotate roundings with the mark type so users can tell weather
    // from leeward at a glance.
    const typeLabel = (m.type === 'rounding' && m.mark)
      ? 'rounding (' + (m.mark === 'weather' ? 'W' : 'L') + ')'
      : (m.type || '');
    const dir = m.turn_angle_deg != null
      ? (m.turn_angle_deg < 0 ? 'P\u2192S' : 'S\u2192P')
      : '';
    const video = m.youtube_url
      ? '<span class="mv-video">&#9654;</span>'
      : '<span class="mv-no-video">\u2014</span>';
    const rank = m.rank ? m.rank : '';
    const rankTitle = m.loss_percentile != null
      ? 'loss percentile: ' + m.loss_percentile + ' (lower = less loss)'
      : '';
    const tagCell = _renderRowTagChips(m.tags);
    return '<tr class="' + (sel ? 'selected' : '') + '" data-k="' + k + '" onclick="mvToggleRow(\'' + k + '\')">'
      + '<td><input type="checkbox" ' + (sel ? 'checked' : '') + ' onclick="event.stopPropagation();mvToggleRow(\'' + k + '\')"/></td>'
      + '<td>' + _esc(date) + ' \u00b7 ' + _esc(m.session_name || '') + '</td>'
      + '<td>' + _esc(time) + '</td>'
      + '<td class="' + typeCls + '">' + _esc(typeLabel) + '</td>'
      + '<td>' + dir + '</td>'
      + '<td class="mv-num">' + twsTxt + '</td>'
      + '<td class="mv-num">' + turnTxt + '</td>'
      + '<td class="mv-num">' + durTxt + '</td>'
      + '<td class="mv-num">' + turnPhaseTxt + '</td>'
      + '<td class="mv-num">' + recoverPhaseTxt + '</td>'
      + '<td' + (rankTitle ? ' title="' + _esc(rankTitle) + '"' : '') + '>' + _esc(rank) + '</td>'
      + '<td>' + video + '</td>'
      + '<td>' + tagCell + '</td>'
      + '</tr>';
  }).join('');

  updateSelectedCount();
}

function mvToggleRow(k) {
  if (state.selected.has(k)) state.selected.delete(k);
  else state.selected.add(k);
  const tr = document.querySelector('tr[data-k="' + k + '"]');
  if (tr) {
    tr.classList.toggle('selected', state.selected.has(k));
    const cb = tr.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = state.selected.has(k);
  }
  updateSelectedCount();
}

function mvToggleAll(on) {
  if (on) state.selected = new Set(state.maneuvers.map(m => m.session_id + ':' + m.id));
  else state.selected = new Set();
  renderResults();
}

function updateSelectedCount() {
  const n = state.selected.size;
  const el = document.getElementById('mv-selected-count');
  el.textContent = n ? '(' + n + ' selected)' : '';
  const cmp = document.getElementById('mv-compare-btn');
  if (cmp) cmp.disabled = n === 0;
  const ov = document.getElementById('mv-overlay-btn');
  if (ov) ov.disabled = n === 0;
  const all = document.getElementById('mv-check-all');
  if (all) all.checked = n > 0 && n === state.maneuvers.length;
}

function mvOpenCompare() {
  if (!state.selected.size) return;
  // Preserve the current table order so cells appear in the same sequence.
  const orderedKeys = state.maneuvers
    .map(m => m.session_id + ':' + m.id)
    .filter(k => state.selected.has(k));
  if (!orderedKeys.length) return;
  window.open('/compare?ids=' + orderedKeys.join(','), '_blank');
}

function mvOpenOverlay() {
  if (!state.selected.size) return;
  // Overlay (#619) only makes sense for maneuvers with a head-to-wind
  // alignment point — roundings have none, so pre-filter them client
  // side to avoid the backend returning them all in excluded_ids.
  const orderedKeys = state.maneuvers
    .filter(m => m.type === 'tack' || m.type === 'gybe')
    .map(m => m.session_id + ':' + m.id)
    .filter(k => state.selected.has(k));
  if (!orderedKeys.length) {
    alert('Overlay requires tack or gybe maneuvers — roundings have no head-to-wind alignment.');
    return;
  }
  window.open('/maneuvers/overlay?ids=' + orderedKeys.join(','), '_blank');
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _fmtTime(iso) {
  if (!iso) return '';
  try {
    let s = String(iso).replace(' ', 'T');
    if (!s.endsWith('Z') && !s.includes('+')) s += 'Z';
    const d = new Date(s);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch (_e) { return ''; }
}

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Tag filter chip row (#587)
// ---------------------------------------------------------------------------

function renderTagFilterRow() {
  const wrap = document.getElementById('mv-tag-filter');
  if (!wrap) return;
  // available_tags comes from the server computed against the pre-tag-filter
  // set, so every tag that could narrow the result is always offered even
  // when a chip is already active.
  const byId = new Map();
  for (const t of (state.availableTags || [])) {
    byId.set(t.id, {id: t.id, name: t.name, color: t.color, count: t.count || 0});
  }
  // Keep currently-selected tags visible even if the result set no longer
  // contains them so the user can still deselect.
  for (const tid of state.tagFilter) {
    if (!byId.has(tid)) byId.set(tid, {id: tid, name: '#' + tid, color: null, count: 0});
  }
  if (byId.size === 0) {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = '';
  const sorted = [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  const chips = sorted.map(t => {
    const active = state.tagFilter.has(t.id);
    const swatch = t.color
      ? '<span class="mv-tag-swatch" style="background:' + t.color + '"></span>'
      : '';
    return '<span class="mv-tag-chip' + (active ? ' active' : '') + '"'
      + ' onclick="mvToggleTagFilter(' + t.id + ')">'
      + swatch + _esc(t.name) + ' <span class="mv-tag-count">(' + t.count + ')</span></span>';
  }).join('');
  // Always show the mode toggle when the tag row is visible, so users
  // discover the control before they've selected tags. Dimmed until a
  // filter is active to signal it's a preference, not an active setting.
  const dim = state.tagFilter.size < 2 ? ';opacity:.6' : '';
  const modeToggle = '<span class="mv-tag-mode" style="margin-left:6px' + dim + '">'
    + '<button class="' + (state.tagMode === 'and' ? 'active' : '') + '" title="Match maneuvers with every selected tag" onclick="mvSetTagMode(\'and\')">all</button>'
    + '<button class="' + (state.tagMode === 'or' ? 'active' : '') + '" title="Match maneuvers with any selected tag" onclick="mvSetTagMode(\'or\')">any</button>'
    + '</span>';
  const clearBtn = state.tagFilter.size
    ? '<a href="#" onclick="event.preventDefault();mvClearTagFilter()" style="font-size:.7rem;color:var(--text-secondary);margin-left:6px">clear</a>'
    : '';
  wrap.innerHTML = '<span class="mv-label">Tags</span>' + chips + modeToggle + clearBtn;
}

function mvToggleTagFilter(tagId) {
  if (state.tagFilter.has(tagId)) state.tagFilter.delete(tagId);
  else state.tagFilter.add(tagId);
  reload();
}

function mvSetTagMode(mode) {
  if (mode !== 'and' && mode !== 'or') return;
  state.tagMode = mode;
  reload();
}

function mvClearTagFilter() {
  state.tagFilter.clear();
  reload();
}

function _renderRowTagChips(tags) {
  if (!tags || !tags.length) return '';
  return tags.map(t => {
    const swatch = t.color
      ? '<span class="mv-tag-swatch" style="background:' + t.color + '"></span>'
      : '';
    return '<span class="mv-tag-chip mv-tag-chip-row">' + swatch + _esc(t.name) + '</span>';
  }).join(' ');
}
