// Race start UI — #644.
//
// Server returns a snapshot { phase, kind, t0_utc, sync_offset_s, flags, ... }.
// The client computes the live countdown locally from t0_utc and reconciles
// every 30 s. Each user action POSTs to a mutation endpoint and refreshes
// from the response.

(function () {
  "use strict";

  const grid = document.querySelector(".rs-grid");
  const isWriter = grid && grid.dataset.isWriter === "true";
  const errorEl = document.getElementById("rs-error");
  const clockEl = document.getElementById("rs-clock");
  const phaseEl = document.getElementById("rs-phase");
  const syncStatusEl = document.getElementById("rs-sync-status");
  const classFlagEl = document.getElementById("rs-class-flag");
  const prepFlagEl = document.getElementById("rs-prep-flag");
  const specialFlagEl = document.getElementById("rs-special-flag");

  let snapshot = null;

  // Virtual-now matches the server's clock (real + simulator offset).
  // In production sim_offset_s is always 0; in the simulator it's whatever
  // the harness has set, so display + sync stay in sync with the FSM.
  function virtualNowMs() {
    const offset = snapshot && snapshot.sim_offset_s ? snapshot.sim_offset_s : 0;
    return Date.now() + offset * 1000;
  }

  function showError(msg) {
    errorEl.textContent = msg || "";
  }

  function fmtClock(seconds) {
    const sign = seconds < 0 ? "-" : "+";
    const abs = Math.abs(Math.floor(seconds));
    const mm = Math.floor(abs / 60);
    const ss = abs % 60;
    return sign + String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
  }

  function renderClock() {
    if (!snapshot || !snapshot.t0_utc) {
      clockEl.textContent = "--:--";
      clockEl.classList.remove("warn", "go");
      return;
    }
    const t0 = new Date(snapshot.t0_utc).getTime();
    const now = virtualNowMs();
    const remaining = (t0 - now) / 1000;  // seconds; negative after t0

    // Display countdown as a positive number until t0, then count up.
    const displaySec = remaining >= 0 ? remaining : -remaining;
    const sign = remaining >= 0 ? "" : "+";
    const mm = Math.floor(displaySec / 60);
    const ss = Math.floor(displaySec % 60);
    clockEl.textContent = sign + String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");

    clockEl.classList.toggle("warn", remaining > 0 && remaining <= 60);
    clockEl.classList.toggle("go", remaining <= 0);
  }

  function renderFlags() {
    if (!snapshot || !snapshot.flags) return;
    classFlagEl.textContent = snapshot.flags.class_flag_up || "—";
    prepFlagEl.textContent = snapshot.flags.prep_flag_up || "—";
    specialFlagEl.textContent = snapshot.flags.special_flag_up || "—";
  }

  function renderPhase() {
    if (!snapshot) return;
    phaseEl.textContent = snapshot.phase;
    if (snapshot.last_sync_at_utc) {
      const last = new Date(snapshot.last_sync_at_utc);
      const ageS = (Date.now() - last.getTime()) / 1000;
      if (ageS > 300) {
        syncStatusEl.textContent = " · sync stale (" + Math.round(ageS) + "s)";
        syncStatusEl.style.color = "var(--warning)";
      } else {
        syncStatusEl.textContent = " · synced";
        syncStatusEl.style.color = "var(--text-muted)";
      }
    } else {
      syncStatusEl.textContent = " · drift unverified";
      syncStatusEl.style.color = "var(--warning)";
    }
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

  function renderScheduledStart() {
    const schedView = document.getElementById("rs-scheduled-view");
    const liveView = document.getElementById("rs-live-view");
    if (!schedView || !liveView || !snapshot) return;
    const sched = snapshot.scheduled_start;
    // Scheduled-pre-arm: only when a schedule is set AND the FSM is still
    // idle. Hide the live FSM controls entirely — auto-fire at T-15min
    // takes over without any helm action, and cancellation lives on
    // /control to avoid accidental taps near a real start.
    const showScheduled =
      !!sched && (snapshot.phase === "idle" || snapshot.phase === "abandoned");
    if (showScheduled) {
      schedView.style.display = "";
      liveView.style.display = "none";
      const fireMs = new Date(sched.scheduled_start_utc).getTime();
      document.getElementById("rs-sched-utc").textContent =
        new Date(fireMs).toLocaleString();
      const ev = document.getElementById("rs-sched-event");
      ev.textContent = sched.event ? "· " + sched.event : "";
      const remaining = (fireMs - virtualNowMs()) / 1000;
      document.getElementById("rs-sched-countdown").textContent =
        fmtCountdown(remaining);
    } else {
      schedView.style.display = "none";
      liveView.style.display = "";
    }
  }

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
    function set(id, value) { document.getElementById(id).textContent = value; }
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
    if (metrics.line_bias_deg === null || metrics.line_bias_deg === undefined) {
      set("rs-line-bias", "TWD needed");
    } else {
      const sign = metrics.line_bias_deg >= 0 ? "+" : "";
      const fav = metrics.favoured_end ? " " + metrics.favoured_end : "";
      set("rs-line-bias", sign + metrics.line_bias_deg.toFixed(0) + "°" + fav);
    }
    set("rs-line-dist",
      metrics.distance_to_line_m === null ? "—"
        : metrics.distance_to_line_m.toFixed(0) + " m");
    set("rs-line-time",
      metrics.time_to_line_s === null ? "—"
        : metrics.time_to_line_s.toFixed(0) + " s");
  }

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
      renderPhase();
      renderFlags();
      renderClock();
      renderScheduledStart();
      renderLineMetrics(snapshot.line_metrics);
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
    const isJSON = ct.includes("application/json");
    if (!isJSON) {
      const text = await r.text();
      throw new Error(
        "HTTP " + r.status + " (non-JSON): " + text.slice(0, 120)
      );
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
      renderPhase();
      renderFlags();
      renderClock();
      renderScheduledStart();
      renderLineMetrics(snapshot.line_metrics);
    } catch (e) {
      showError(e.message);
    }
  }

  function defaultT0Utc() {
    // Default arm: 5 minutes from virtual-now, rounded up to next 30s.
    // Using virtualNowMs() means the simulator's clock skew is honored
    // so the displayed countdown actually reads ~5:00 after Arm.
    const ms = virtualNowMs() + 5 * 60 * 1000;
    const rounded = Math.ceil(ms / 30000) * 30000;
    return new Date(rounded).toISOString();
  }

  function bind(id, fn) {
    const btn = document.getElementById(id);
    if (btn && !btn.disabled) btn.addEventListener("click", fn);
  }

  bind("rs-arm", () => action("/api/race-start/arm",
        { kind: "5-4-1-0", t0_utc: defaultT0Utc() }));
  bind("rs-sync", () => {
    if (!snapshot || !snapshot.t0_utc) return showError("arm a sequence first");
    // Sync rounds the countdown to the nearest minute. Use case: user
    // hears the prep gun late — countdown reads 4:10 but should be 4:00.
    // Tap sync; we re-anchor so remaining = round(remaining / 60) × 60.
    const remaining = (new Date(snapshot.t0_utc).getTime() - virtualNowMs()) / 1000;
    const rounded = Math.round(remaining / 60) * 60;
    action("/api/race-start/sync", { expected_signal_offset_s: rounded });
  });
  bind("rs-plus-min", () => action("/api/race-start/nudge", { delta_s: 60 }));
  bind("rs-minus-min", () => action("/api/race-start/nudge", { delta_s: -60 }));
  bind("rs-postpone", () => action("/api/race-start/postpone"));
  bind("rs-recall", () => action("/api/race-start/recall"));
  bind("rs-abandon", () => {
    if (confirm("Abandon the race?")) action("/api/race-start/abandon");
  });
  bind("rs-reset", () => {
    if (confirm("Reset the sequence?")) action("/api/race-start/reset");
  });

  // Pings use the boat's GPS feed (server-side latest_position from
  // sk_reader / can_reader) — the phone is never the source of truth.
  // For offline use without a fix, hold Shift to enter manual coords.
  async function pingEnd(end) {
    if (!isWriter) return;
    let body = {};
    if (window.event && window.event.shiftKey) {
      const raw = prompt("Enter lat,lon for " + end + " end:");
      if (!raw) return;
      const parts = raw.split(",").map((s) => parseFloat(s.trim()));
      if (parts.length !== 2 || isNaN(parts[0]) || isNaN(parts[1])) {
        return showError("expected 'lat,lon'");
      }
      body = { latitude_deg: parts[0], longitude_deg: parts[1] };
    }
    return action("/api/race-start/ping/" + end, body);
  }
  bind("rs-ping-boat", () => pingEnd("boat"));
  bind("rs-ping-pin", () => pingEnd("pin"));

  // Local clock tick at 4 Hz; reconcile from server every 2 s so that
  // arm / sync / ping / postpone fired from one device shows up on
  // every other device almost immediately (#644). This is a polling
  // fallback — a WebSocket broadcast would be cheaper at scale, but at
  // 2 s × handful of devices the load is negligible and the flow is
  // robust to disconnect.
  setInterval(renderClock, 250);
  // Tick the scheduled-start countdown alongside the main clock so the
  // helm sees the seconds drop without waiting for the 2 s state poll.
  setInterval(renderScheduledStart, 1000);
  setInterval(refreshState, 2000);

  refreshState();
})();
