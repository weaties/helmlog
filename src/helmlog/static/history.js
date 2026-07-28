/* history.js — Session History page logic */

let currentType = 'race';
let currentOffset = 0;
const LIMIT = 25;
let loadTimer = null;
const summaryCache = new Map();

// Tag filter state — mirrors the maneuvers panel pattern.
const tagFilter = new Set();
let tagMode = 'and'; // 'and' | 'or'
let availableTags = []; // [{id, name, color, count}] from server, pre-tag-filter

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
  if (tagFilter.size) {
    params.set('tags', [...tagFilter].join(','));
    params.set('tag_mode', tagMode);
  }
  params.set('limit', LIMIT);
  params.set('offset', currentOffset);
  const r = await fetch('/api/sessions?' + params);
  const data = await r.json();
  availableTags = data.available_tags || [];
  render(data);
  renderTagFilterRow(data.sessions);
}

function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function _etypeLabel(et, count) {
  if (et === 'session') return 'session';
  // Pluralize when count > 1 for readability.
  if (count > 1) return et + 's';
  return et;
}

function _entityTypeOrder(a, b) {
  const order = {session: 0, moment: 1, maneuver: 2};
  return (order[a] ?? 99) - (order[b] ?? 99);
}

// Collapse tag_summary into [{id, name, color, parts: [{et, count}]}] so one
// chip renders per tag with a compact entity-type label.
function _groupTagSummary(rows) {
  const byId = new Map();
  for (const r of rows) {
    const existing = byId.get(r.id) || {id: r.id, name: r.name, color: r.color, parts: []};
    existing.parts.push({et: r.entity_type, count: r.count});
    byId.set(r.id, existing);
  }
  // Sort tags by name; inside each tag, order the parts (session first).
  const out = [...byId.values()];
  out.sort((a, b) => a.name.localeCompare(b.name));
  for (const t of out) {
    t.parts.sort((a, b) => _entityTypeOrder(a.et, b.et));
  }
  return out;
}

function renderSessionTagChips(summary) {
  if (!summary || !summary.length) return '';
  const grouped = _groupTagSummary(summary);
  const chips = grouped.map(g => {
    const swatch = g.color
      ? '<span class="swatch" style="background:' + g.color + '"></span>'
      : '';
    const labels = g.parts.map(p => {
      const lbl = _etypeLabel(p.et, p.count);
      return p.count > 1 ? p.count + ' ' + lbl : lbl;
    }).join(', ');
    return '<span class="hist-tag-chip" title="' + esc(labels) + '">'
      + swatch + esc(g.name)
      + ' <span class="etype">(' + esc(labels) + ')</span></span>';
  }).join('');
  return '<div class="hist-tag-row-chips">' + chips + '</div>';
}

// Build the filter chip row from whatever tags the current result page
// surfaces. This keeps the list focused on tags actually in use rather
// than every tag in the system.
function renderTagFilterRow(sessions) {
  const wrap = document.getElementById('tag-filter-row');
  const chipsHost = document.getElementById('tag-filter-chips');
  const modeWrap = document.getElementById('tag-mode-wrap');
  const clearLink = document.getElementById('tag-clear');
  if (!wrap || !chipsHost) return;

  // Use server-provided available_tags (computed across all sessions
  // matching the non-tag filters) so selecting a tag doesn't collapse
  // the chip row down to the current result.
  const byId = new Map();
  for (const t of availableTags) {
    byId.set(t.id, {id: t.id, name: t.name, color: t.color, count: t.count || 0});
  }
  // Always include currently-selected tags even if not in available_tags,
  // so the user can still deselect them.
  for (const tid of tagFilter) {
    if (!byId.has(tid)) byId.set(tid, {id: tid, name: '#' + tid, color: null, count: 0});
  }

  if (byId.size === 0) {
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = '';
  const sorted = [...byId.values()].sort((a, b) => a.name.localeCompare(b.name));
  chipsHost.innerHTML = sorted.map(t => {
    const active = tagFilter.has(t.id);
    const swatch = t.color
      ? '<span class="swatch" style="background:' + t.color + '"></span>'
      : '';
    const countLbl = t.count ? ' (' + t.count + ')' : '';
    return '<span class="hist-tag-chip' + (active ? ' active' : '') + '"'
      + ' onclick="toggleTagFilter(' + t.id + ')">' + swatch
      + esc(t.name) + '<span class="etype">' + countLbl + '</span></span>';
  }).join('');

  // Mode toggle always visible when the tag row is shown, dimmed when
  // fewer than 2 tags are active so users discover the control up front.
  modeWrap.style.display = '';
  const dimStyle = tagFilter.size < 2 ? 'opacity:.6' : '';
  modeWrap.innerHTML = '<span class="tag-mode-toggle" style="' + dimStyle + '">'
    + '<button class="' + (tagMode === 'and' ? 'active' : '') + '" title="Match sessions that contain every selected tag" onclick="setTagMode(\'and\')">all</button>'
    + '<button class="' + (tagMode === 'or' ? 'active' : '') + '" title="Match sessions that contain any selected tag" onclick="setTagMode(\'or\')">any</button>'
    + '</span>';

  clearLink.style.display = tagFilter.size ? '' : 'none';
}

function toggleTagFilter(tagId) {
  if (tagFilter.has(tagId)) tagFilter.delete(tagId);
  else tagFilter.add(tagId);
  currentOffset = 0;
  load();
}

function setTagMode(mode) {
  if (mode !== 'and' && mode !== 'or') return;
  tagMode = mode;
  currentOffset = 0;
  load();
}

function clearTagFilter() {
  tagFilter.clear();
  currentOffset = 0;
  load();
}

// Thumbnail for a session's first linked YouTube video (#827). Hotlinks
// img.youtube.com, so it must degrade cleanly: on the boat the Pi has no
// uplink and every one of these 404s. The onerror handler swaps the dead
// image for a text badge rather than leaving a broken-image icon — knowing
// a race *has* video is useful even when the thumbnail can't load.
function renderVideoThumb(s) {
  if (!s.first_video_id) return '';
  const vid = String(s.first_video_id);
  const url = s.first_video_url || ('https://www.youtube.com/watch?v=' + encodeURIComponent(vid));
  const extra = (s.video_count || 0) - 1;
  const more = extra > 0
    ? '<span class="hist-video-more">+' + extra + ' more</span>'
    : '';
  const title = extra > 0
    ? 'Watch on YouTube (' + s.video_count + ' videos linked)'
    : 'Watch on YouTube';
  return '<a class="hist-video" href="' + esc(url) + '"'
    + ' target="_blank" rel="noopener noreferrer" title="' + esc(title) + '">'
    + '<span class="hist-video-frame">'
    + '<img src="https://img.youtube.com/vi/' + encodeURIComponent(vid) + '/mqdefault.jpg"'
    + ' alt="" loading="lazy"'
    + ' onerror="this.closest(\'.hist-video\').classList.add(\'hist-video-broken\')">'
    + '<span class="hist-video-play" aria-hidden="true"></span>'
    + '</span>'
    + more
    + '</a>';
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
    const parent = s.parent_race_name ? '<div class="session-meta">Debrief of ' + s.parent_race_name + '</div>' : '';
    const displayName = s.shared_name || s.name;
    const nameLink = '<a href="/session/' + s.id + '" style="color:inherit;text-decoration:none">' + esc(displayName) + '</a>';
    const localNameHint = s.shared_name ? '<div style="font-size:.72rem;color:var(--text-secondary);margin-top:1px">Local: ' + esc(s.name) + '</div>' : '';
    const showSummary = s.type !== 'debrief' && s.end_utc;
    const summaryHtml = showSummary
      ? '<div class="session-summary" id="hist-summary-' + s.id + '"><div class="summary-skeleton"></div></div>'
      : '';
    const tagChips = renderSessionTagChips(s.tag_summary);
    const videoThumb = renderVideoThumb(s);
    // Text column and thumbnail sit side by side; the lazily-loaded summary
    // row stays outside the flex row so it still spans the full card width.
    return '<div class="card"><div class="hist-card-row"><div class="hist-card-main">'
      + '<div class="session-name">' + nameLink + '</div>'
      + '<div class="session-meta">' + s.date + ' &nbsp;·&nbsp; ' + start + ' → ' + end + dur + '</div>'
      + localNameHint
      + parent
      + tagChips
      + '</div>'
      + videoThumb
      + '</div>'
      + summaryHtml
      + '</div>';
  }).join('');

  data.sessions.forEach(s => {
    if (s.type !== 'debrief' && s.end_utc) loadSummary(s.id);
  });

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

async function loadSummary(sessionId) {
  const host = document.getElementById('hist-summary-' + sessionId);
  if (!host) return;
  let data = summaryCache.get(sessionId);
  if (!data) {
    try {
      const r = await fetch('/api/sessions/' + sessionId + '/summary');
      if (!r.ok) { host.innerHTML = ''; return; }
      data = await r.json();
      summaryCache.set(sessionId, data);
    } catch {
      host.innerHTML = '';
      return;
    }
  }
  host.innerHTML = renderSummary(data);
}

function renderSummary(data) {
  const thumb = renderThumbnail(data.track || [], data.events || []);
  const wind = renderWind(data.wind);
  const results = renderResults(data.results || []);
  const parts = [thumb, wind, results].filter(Boolean);
  if (!parts.length) return '';
  return '<div class="summary-row">' + parts.join('') + '</div>';
}

function renderThumbnail(track, events) {
  if (!track.length) return '';
  const W = 140, H = 90, PAD = 6;
  let minLon = Infinity, maxLon = -Infinity, minLat = Infinity, maxLat = -Infinity;
  for (const [lon, lat] of track) {
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
  }
  const rangeLon = Math.max(maxLon - minLon, 1e-9);
  const rangeLat = Math.max(maxLat - minLat, 1e-9);
  const latCorr = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
  const rangeLonCorr = rangeLon * latCorr;
  const scale = Math.min((W - 2 * PAD) / rangeLonCorr, (H - 2 * PAD) / rangeLat);
  const drawnW = rangeLonCorr * scale;
  const drawnH = rangeLat * scale;
  const offX = (W - drawnW) / 2;
  const offY = (H - drawnH) / 2;
  const project = ([lon, lat]) => {
    const x = offX + (lon - minLon) * latCorr * scale;
    const y = H - (offY + (lat - minLat) * scale);
    return [x, y];
  };
  const pts = track.map(project);
  const path = pts.map(([x, y], i) => (i === 0 ? 'M' : 'L') + x.toFixed(1) + ' ' + y.toFixed(1)).join(' ');

  const markerFor = {
    start: { r: 3, fill: '#2aa14f', stroke: '#fff' },
    finish: { r: 3, fill: '#c23030', stroke: '#fff' },
    tack: { r: 1.8, fill: '#1e88e5', stroke: null },
    gybe: { r: 1.8, fill: '#ff9800', stroke: null },
    rounding: { r: 2.4, fill: '#8e44ad', stroke: '#fff' },
  };
  const order = ['tack', 'gybe', 'rounding', 'start', 'finish'];
  const evBy = { tack: [], gybe: [], rounding: [], start: [], finish: [] };
  for (const e of events) {
    if (evBy[e.type] && e.idx >= 0 && e.idx < pts.length) evBy[e.type].push(pts[e.idx]);
  }
  let markers = '';
  for (const type of order) {
    const m = markerFor[type];
    for (const [x, y] of evBy[type]) {
      markers += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + m.r + '" fill="' + m.fill + '"'
        + (m.stroke ? ' stroke="' + m.stroke + '" stroke-width="0.8"' : '') + '/>';
    }
  }
  return '<svg class="summary-thumb" viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '">'
    + '<path d="' + path + '" fill="none" stroke="var(--text-primary)" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/>'
    + markers
    + '</svg>';
}

function renderWind(wind) {
  if (!wind || wind.avg_tws_kts == null) return '';
  const dir = wind.avg_twd_deg;
  const arrow = '<svg width="22" height="22" viewBox="0 0 22 22" style="flex:none">'
    + '<g transform="rotate(' + (dir + 180) + ' 11 11)">'
    + '<path d="M11 3 L11 17 M11 3 L8 7 M11 3 L14 7" stroke="var(--text-primary)" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    + '</g></svg>';
  return '<div class="summary-wind">' + arrow
    + '<div class="summary-wind-text">' + wind.avg_tws_kts.toFixed(1) + ' kt<br><span>' + Math.round(dir) + '°</span></div>'
    + '</div>';
}

(function init() {
  const now = new Date();
  const past = new Date(now - 365 * 86400000);
  const toEl = document.getElementById('to-date');
  const fromEl = document.getElementById('from-date');
  if (toEl) toEl.value = now.toISOString().substring(0, 10);
  if (fromEl) fromEl.value = past.toISOString().substring(0, 10);
  (typeof initTimezone === 'function' ? initTimezone() : Promise.resolve()).then(() => load());
})();

function renderResults(results) {
  if (!results.length) return '';
  const medals = ['🥇', '🥈', '🥉'];
  const lines = results.map((r, i) => {
    const isOwnRow = i >= 3;
    const parts = [];
    if (r.sail_number) parts.push(String(r.sail_number));
    if (r.boat_name) parts.push(r.boat_name);
    const label = parts.join(' — ') || '—';
    let prefix;
    if (isOwnRow) {
      prefix = r.dnf ? 'DNF' : r.dns ? 'DNS' : (r.place + '.');
    } else {
      prefix = medals[i] || (r.place + '.');
    }
    const cls = 'summary-result' + (isOwnRow ? ' summary-result-own' : '');
    return '<div class="' + cls + '">' + prefix + ' ' + label + '</div>';
  }).join('');
  return '<div class="summary-results">' + lines + '</div>';
}
