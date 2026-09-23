// Race start UI.
//
// simrad_timer_state is the single source of truth for the timer,
// whether B&G instruments are connected or not.
//
// Clock path: running → live countdown from t0_utc
//             stopped → frozen stopped_remaining_s
//             idle    → full duration_s or "--:--"
//
// Rolling timer: when running timer reaches 0:00 and rolling_timer_on is
// set, the client calls timer-reset to restart at the full duration.

(function () {
  "use strict";

  const grid = document.querySelector(".rs-grid");
  const isWriter = grid && grid.dataset.isWriter === "true";
  const errorEl = document.getElementById("rs-error");
  const clockEl = document.getElementById("rs-clock");
  const clockInputEl = document.getElementById("rs-clock-input");
  const statusEl = document.getElementById("rs-status");
  const instrToggleEl = document.getElementById("rs-instr-toggle");
  const instrStatusEl = document.getElementById("rs-instr-status");
  const rollingToggleEl = document.getElementById("rs-rolling-toggle");

  let snapshot = null;
  let editingDuration = false;
  let rollingResetInFlight = false;

  // Pi/browser clock skew correction for the simrad timer countdown.
  // The Pi may not have NTP on the water, so its clock can drift from the
  // browser's clock. We track the server's now_utc from each snapshot and
  // use it (+ elapsed client time) instead of bare Date.now() for t0_utc
  // comparisons. This is NOT applied globally via virtualNowMs() because
  // the FSM simulator already has its own sim_offset_s mechanism and a
  // global correction would double-count that offset.
  let _snapshotReceivedAt = Date.now();
  let _snapshotServerNowMs = null;

  function instrumentNowMs() {
    if (_snapshotServerNowMs === null) return Date.now();
    return _snapshotServerNowMs + (Date.now() - _snapshotReceivedAt);
  }

  function _recordSnapshot(s) {
    _snapshotReceivedAt = Date.now();
    if (s && s.now_utc) _snapshotServerNowMs = new Date(s.now_utc).getTime();
  }

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function showError(msg) {
    errorEl.textContent = msg || "";
  }

  function fmtMmSs(seconds) {
    const abs = Math.abs(Math.floor(seconds));
    const mm = Math.floor(abs / 60);
    const ss = abs % 60;
    return String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
  }

  function fmtCountdown(seconds) {
    const sign = seconds < 0 ? "−" : "";
    const abs = Math.abs(Math.floor(seconds));
    const days = Math.floor(abs / 86400);
    const hrs = Math.floor((abs % 86400) / 3600);
    const min = Math.floor((abs % 3600) / 60);
    const sec = abs % 60;
    if (days > 0) return sign + days + "d " + hrs + "h " + min + "m";
    if (hrs > 0) return sign + hrs + "h " + min + "m " + sec + "s";
    if (min > 0) return sign + min + "m " + sec + "s";
    return sign + sec + "s";
  }

  // ---------------------------------------------------------------------------
  // Clock rendering
  // ---------------------------------------------------------------------------

  function remainingSeconds() {
    const instr = snapshot && snapshot.simrad_timer;
    if (!instr) return null;
    if (instr.is_running && instr.t0_utc) {
      return (new Date(instr.t0_utc).getTime() - instrumentNowMs()) / 1000;
    }
    if (!instr.is_running && instr.stopped_remaining_s != null) {
      return instr.stopped_remaining_s;
    }
    if (instr.duration_s != null) {
      return instr.duration_s;
    }
    return null;
  }

  function renderClock() {
    if (editingDuration) return;
    const instr = snapshot && snapshot.simrad_timer;
    clockEl.classList.remove("warn", "go");

    if (!instr) {
      clockEl.textContent = "--:--";
      return;
    }

    if (instr.is_running && instr.t0_utc) {
      const remaining = (new Date(instr.t0_utc).getTime() - instrumentNowMs()) / 1000;
      const abs = Math.abs(remaining);
      const sign = remaining >= 0 ? "" : "+";
      clockEl.textContent = sign + fmtMmSs(abs);
      clockEl.classList.toggle("warn", remaining > 0 && remaining <= 60);
      clockEl.classList.toggle("go", remaining <= 0);

      // Rolling timer: auto-reset when countdown reaches 0:00.
      if (remaining <= 0 && instr.rolling_timer_on && isWriter && !rollingResetInFlight) {
        rollingResetInFlight = true;
        postJSON("/api/race-start/timer-reset", {}).then(data => {
          snapshot = data;
          renderAll();
          rollingResetInFlight = false;
        }).catch(e => {
          showError(e.message);
          rollingResetInFlight = false;
        });
      }
      return;
    }

    if (!instr.is_running && instr.stopped_remaining_s != null) {
      clockEl.textContent = fmtMmSs(instr.stopped_remaining_s);
      return;
    }

    if (instr.duration_s != null) {
      clockEl.textContent = fmtMmSs(instr.duration_s);
      return;
    }

    clockEl.textContent = "--:--";
  }

  // ---------------------------------------------------------------------------
  // Status line
  // ---------------------------------------------------------------------------

  function renderStatus() {
    const instr = snapshot && snapshot.simrad_timer;
    if (!instr) { statusEl.textContent = "—"; return; }
    if (instr.is_running) {
      statusEl.innerHTML = '<span style="color:var(--success,#22c55e)">Running</span>';
    } else if (instr.stopped_remaining_s != null) {
      statusEl.innerHTML = '<span style="color:var(--warning,#f5c518)">Stopped</span>';
    } else {
      statusEl.textContent = "Idle";
    }
  }

  // ---------------------------------------------------------------------------
  // Button states
  // ---------------------------------------------------------------------------

  function renderButtons() {
    if (!isWriter) return;
    const instr = snapshot && snapshot.simrad_timer;
    const running = instr && instr.is_running;

    // Set Start Value disabled when running or editing.
    const setBtn = document.getElementById("rs-btn-setval");
    if (setBtn) {
      setBtn.disabled = !!running;
      setBtn.classList.toggle("editing", editingDuration);
      setBtn.textContent = editingDuration ? "Confirm" : "Set Start Value";
    }

    // Rolling timer switch reflects server state.
    if (rollingToggleEl) {
      rollingToggleEl.checked = !!(instr && instr.rolling_timer_on);
    }
  }

  // ---------------------------------------------------------------------------
  // Instrument timer panel
  // ---------------------------------------------------------------------------

  function renderInstrTimer() {
    const instr = snapshot && snapshot.simrad_timer;
    if (!instrToggleEl || !instrStatusEl) return;

    const on = instr && instr.instrument_timer_on;
    instrToggleEl.checked = !!on;

    if (!instr || (instr.duration_s == null && !instr.t0_utc)) {
      instrStatusEl.innerHTML = "No data received from B&amp;G";
      return;
    }

    if (!on) {
      instrStatusEl.textContent = "Instrument timer disabled";
      return;
    }

    if (instr.is_running && instr.t0_utc) {
      const remaining = (new Date(instr.t0_utc).getTime() - instrumentNowMs()) / 1000;
      const sign = remaining >= 0 ? "" : "+";
      instrStatusEl.innerHTML =
        '<span class="running">Running</span> — ' +
        sign + fmtMmSs(Math.abs(remaining));
      return;
    }

    if (!instr.is_running && instr.stopped_remaining_s != null) {
      instrStatusEl.innerHTML =
        '<span class="stopped">Stopped</span> — ' +
        fmtMmSs(instr.stopped_remaining_s) + " remaining";
      return;
    }

    instrStatusEl.textContent = "Waiting for B&G data";
  }

  // ---------------------------------------------------------------------------
  // Scheduled start view
  // ---------------------------------------------------------------------------

  function renderScheduledStart() {
    const schedView = document.getElementById("rs-scheduled-view");
    const liveView = document.getElementById("rs-live-view");
    if (!schedView || !liveView || !snapshot) return;
    const sched = snapshot.scheduled_start;
    const instr = snapshot.simrad_timer;
    const timerActive = instr && (instr.is_running || instr.stopped_remaining_s != null);
    const showScheduled = !!sched && !timerActive;
    if (showScheduled) {
      schedView.style.display = "";
      liveView.style.display = "none";
      const fireMs = new Date(sched.scheduled_start_utc).getTime();
      document.getElementById("rs-sched-utc").textContent =
        new Date(fireMs).toLocaleString();
      const ev = document.getElementById("rs-sched-event");
      ev.textContent = sched.event ? "· " + sched.event : "";
      const remaining = (fireMs - Date.now()) / 1000;
      document.getElementById("rs-sched-countdown").textContent =
        fmtCountdown(remaining);
    } else {
      schedView.style.display = "none";
      liveView.style.display = "";
    }
  }

  // ---------------------------------------------------------------------------
  // Line metrics
  // ---------------------------------------------------------------------------

  function renderLineCarryover() {
    const el = document.getElementById("rs-line-carryover");
    if (!el || !snapshot || !snapshot.start_line) return;
    const sl = snapshot.start_line;
    const parts = [];
    if (sl.boat_end_carried_over_from_race_id) {
      parts.push("boat end from race " + sl.boat_end_carried_over_from_race_id);
    }
    if (sl.pin_end_carried_over_from_race_id) {
      parts.push("pin end from race " + sl.pin_end_carried_over_from_race_id);
    }
    if (parts.length === 0) {
      el.style.display = "none";
      return;
    }
    el.textContent = "⚠ Line carried over: " + parts.join(", ")
      + ". Re-ping if RC has moved the line.";
    el.style.display = "";
  }

  function renderLineMetrics(metrics) {
    function set(id, value) {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    }
    renderLineCarryover();
    if (!metrics) {
      set("rs-line-bearing", "—");
      set("rs-line-length", "—");
      set("rs-line-bias", "—");
      set("rs-line-dist", "—");
      set("rs-line-time", "—");
      return;
    }
    set("rs-line-bearing", metrics.line_bearing_deg.toFixed(0) + "°");
    set("rs-line-length", metrics.line_length_m.toFixed(0) + " m");
    if (metrics.line_bias_deg == null) {
      set("rs-line-bias", "TWD needed");
    } else {
      const sign = metrics.line_bias_deg >= 0 ? "+" : "";
      const fav = metrics.favoured_end ? " " + metrics.favoured_end : "";
      set("rs-line-bias", sign + metrics.line_bias_deg.toFixed(0) + "°" + fav);
    }
    set("rs-line-dist",
      metrics.distance_to_line_m == null ? "—"
        : metrics.distance_to_line_m.toFixed(0) + " m");
    set("rs-line-time",
      metrics.time_to_line_s == null ? "—"
        : metrics.time_to_line_s.toFixed(0) + " s");
  }

  // ---------------------------------------------------------------------------
  // Render all
  // ---------------------------------------------------------------------------

  function renderAll() {
    renderStatus();
    renderClock();
    renderButtons();
    renderScheduledStart();
    renderLineMetrics(snapshot && snapshot.line_metrics);
    renderInstrTimer();
  }

  // ---------------------------------------------------------------------------
  // Network
  // ---------------------------------------------------------------------------

  async function refreshState() {
    try {
      const r = await fetch("/api/race-start/state");
      const ct = r.headers.get("content-type") || "";
      if (!ct.includes("application/json")) {
        const text = await r.text();
        throw new Error("HTTP " + r.status + " (non-JSON): " + text.slice(0, 120));
      }
      if (!r.ok) {
        const data = await r.json();
        throw new Error(data.detail || "HTTP " + r.status);
      }
      snapshot = await r.json();
      _recordSnapshot(snapshot);
      renderAll();
      showError("");
    } catch (e) {
      showError("could not load state: " + e.message);
    }
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("application/json")) {
      const text = await r.text();
      throw new Error("HTTP " + r.status + " (non-JSON): " + text.slice(0, 120));
    }
    const data = await r.json();
    if (!r.ok) {
      throw new Error(data.detail || ("HTTP " + r.status));
    }
    return data;
  }

  async function action(url, body) {
    if (!isWriter) return;
    showError("");
    try {
      snapshot = await postJSON(url, body);
      _recordSnapshot(snapshot);
      renderAll();
    } catch (e) {
      showError(e.message);
    }
  }

  // ---------------------------------------------------------------------------
  // Set Start Value — inline MM:SS editing
  // ---------------------------------------------------------------------------

  function parseMinutes(raw) {
    const n = parseInt(raw.trim(), 10);
    if (!Number.isInteger(n) || n < 1 || n > 60) return null;
    return n * 60;
  }

  function enterEditMode() {
    if (!isWriter) return;
    const instr = snapshot && snapshot.simrad_timer;
    if (instr && instr.is_running) return;
    editingDuration = true;
    const rem = remainingSeconds();
    const mins = rem != null ? Math.max(1, Math.round(rem / 60)) : 5;
    clockEl.style.display = "none";
    clockInputEl.style.display = "block";
    clockInputEl.value = String(mins);
    clockInputEl.focus();
    clockInputEl.select();
  }

  function exitEditMode(commit) {
    editingDuration = false;
    clockInputEl.style.display = "none";
    clockEl.style.display = "";
    if (commit) {
      const val = parseMinutes(clockInputEl.value);
      if (val == null) {
        showError("enter whole minutes (1–60)");
        return;
      }
      action("/api/race-start/set-duration", { duration_s: val });
    }
  }

  if (clockInputEl) {
    clockInputEl.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); exitEditMode(true); }
      if (e.key === "Escape") { e.preventDefault(); exitEditMode(false); }
    });
    clockInputEl.addEventListener("blur", () => {
      setTimeout(() => { if (editingDuration) exitEditMode(false); }, 150);
    });
    // Strip non-digits as the user types.
    clockInputEl.addEventListener("input", () => {
      clockInputEl.value = clockInputEl.value.replace(/[^0-9]/g, "");
    });
  }

  // ---------------------------------------------------------------------------
  // Button handlers
  // ---------------------------------------------------------------------------

  function bind(id, fn) {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener("click", fn);
  }

  bind("rs-btn-start", () => action("/api/race-start/start"));

  bind("rs-btn-stop", () => action("/api/race-start/stop"));

  bind("rs-btn-reset", () => action("/api/race-start/timer-reset"));

  bind("rs-btn-sync", () => action("/api/race-start/sync"));

  bind("rs-btn-setval", () => {
    if (!isWriter) return;
    if (editingDuration) {
      exitEditMode(true);
    } else {
      enterEditMode();
    }
  });

  // Rolling Timer switch
  if (rollingToggleEl && isWriter) {
    rollingToggleEl.addEventListener("change", async () => {
      const on = rollingToggleEl.checked;
      showError("");
      try {
        await postJSON("/api/race-start/rolling-timer", { on });
        await refreshState();
      } catch (e) {
        showError(e.message);
        rollingToggleEl.checked = !on;
      }
    });
  }

  // Pings use the boat's GPS feed (server-side latest_position from
  // sk_reader / can_reader). Hold Shift to enter manual coords.
  async function pingEnd(end) {
    if (!isWriter) return;
    let body = {};
    if (window.event && window.event.shiftKey) {
      const raw = prompt("Enter lat,lon for " + end + " end:");
      if (!raw) return;
      const parts = raw.split(",").map(s => parseFloat(s.trim()));
      if (parts.length !== 2 || isNaN(parts[0]) || isNaN(parts[1])) {
        return showError("expected 'lat,lon'");
      }
      body = { latitude_deg: parts[0], longitude_deg: parts[1] };
    }
    return action("/api/race-start/ping/" + end, body);
  }
  bind("rs-ping-boat", () => pingEnd("boat"));
  bind("rs-ping-pin", () => pingEnd("pin"));

  // Instrument Timer toggle — revert on failure.
  if (instrToggleEl && isWriter) {
    instrToggleEl.addEventListener("change", async () => {
      const on = instrToggleEl.checked;
      showError("");
      try {
        await postJSON("/api/race-start/instrument-timer", { on });
        await refreshState();
      } catch (e) {
        showError(e.message);
        instrToggleEl.checked = !on;
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Tick loop
  // ---------------------------------------------------------------------------

  // Fast local tick for smooth countdown display; server reconcile every 2 s.
  setInterval(() => { renderClock(); renderInstrTimer(); renderScheduledStart(); }, 250);
  setInterval(refreshState, 2000);

  refreshState();
})();
