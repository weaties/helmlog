// Read-only race-start widget (#644).
//
// Polls /api/race-start/state every 2 s and renders a compact status bar
// near the top of the page: countdown, class flag, prep flag, special
// flag, and line bias. Self-hides when phase=idle.
//
// Optional: if window._helmlogLeafletMap is set (session.html exposes it),
// the widget also draws the start line + bias tick on the map. Pure
// observer view — no buttons, no mutation.

(function () {
  "use strict";

  let snapshot = null;
  let panel = null;
  let mapLayers = []; // leaflet layers we own; cleared/redrawn each refresh

  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = "race-start-widget";
    panel.style.cssText = [
      "display:none",
      "padding:.5rem 1rem",
      "background:var(--bg-elev,#1a1d23)",
      "border:1px solid var(--border,#333)",
      "border-radius:.5rem",
      "margin:.5rem auto",
      "max-width:960px",
      "font-family:inherit",
      "color:var(--text-primary,#fff)",
    ].join(";");
    panel.innerHTML = `
      <div style="display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap;
                  font-variant-numeric:tabular-nums">
        <div>
          <span style="font-size:.7rem;color:var(--text-muted);
                       text-transform:uppercase;letter-spacing:.1em">Phase</span>
          <div id="rsw-phase" style="font-weight:600;text-transform:uppercase">—</div>
        </div>
        <div>
          <span style="font-size:.7rem;color:var(--text-muted);
                       text-transform:uppercase;letter-spacing:.1em">Countdown</span>
          <div id="rsw-clock" style="font-size:1.5rem;font-weight:700">--:--</div>
        </div>
        <div>
          <span style="font-size:.7rem;color:var(--text-muted);
                       text-transform:uppercase;letter-spacing:.1em">Class</span>
          <div id="rsw-class">—</div>
        </div>
        <div>
          <span style="font-size:.7rem;color:var(--text-muted);
                       text-transform:uppercase;letter-spacing:.1em">Prep</span>
          <div id="rsw-prep">—</div>
        </div>
        <div>
          <span style="font-size:.7rem;color:var(--text-muted);
                       text-transform:uppercase;letter-spacing:.1em">Special</span>
          <div id="rsw-special">—</div>
        </div>
        <div>
          <span style="font-size:.7rem;color:var(--text-muted);
                       text-transform:uppercase;letter-spacing:.1em">Bias</span>
          <div id="rsw-bias">—</div>
        </div>
        <div>
          <span style="font-size:.7rem;color:var(--text-muted);
                       text-transform:uppercase;letter-spacing:.1em">Dist to line</span>
          <div id="rsw-dist">—</div>
        </div>
        <div style="margin-left:auto">
          <a href="/race-start" style="font-size:.8rem;color:var(--accent)">
            Open /race-start ›
          </a>
        </div>
      </div>
    `;

    // Insert below the nav. If <nav> isn't found, fall back to body top.
    const nav = document.querySelector("nav.site-nav");
    if (nav && nav.parentNode) {
      nav.parentNode.insertBefore(panel, nav.nextSibling);
    } else {
      document.body.insertBefore(panel, document.body.firstChild);
    }
    return panel;
  }

  function virtualNowMs(s) {
    const offset = s && s.sim_offset_s ? s.sim_offset_s : 0;
    return Date.now() + offset * 1000;
  }

  function fmtClock(remainingS) {
    const sign = remainingS >= 0 ? "" : "+";
    const abs = Math.abs(Math.floor(remainingS));
    const mm = Math.floor(abs / 60);
    const ss = abs % 60;
    return sign + String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
  }

  function renderClock() {
    if (!snapshot || !snapshot.t0_utc) return;
    const t0 = new Date(snapshot.t0_utc).getTime();
    const remaining = (t0 - virtualNowMs(snapshot)) / 1000;
    const el = document.getElementById("rsw-clock");
    if (el) el.textContent = fmtClock(remaining);
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function renderSnapshot() {
    if (!snapshot) return;
    const p = ensurePanel();
    if (snapshot.phase === "idle") {
      p.style.display = "none";
      clearMapLayers();
      return;
    }
    p.style.display = "block";

    setText("rsw-phase", snapshot.phase.replace(/_/g, " "));
    renderClock();

    const f = snapshot.flags || {};
    setText("rsw-class", f.class_flag_up || "—");
    setText("rsw-prep", f.prep_flag_up || "—");
    setText("rsw-special", f.special_flag_up || "—");

    const m = snapshot.line_metrics;
    if (m && m.line_bias_deg != null) {
      const sign = m.line_bias_deg >= 0 ? "+" : "";
      const fav = m.favoured_end ? " " + m.favoured_end : "";
      setText("rsw-bias", sign + m.line_bias_deg.toFixed(0) + "°" + fav);
    } else {
      setText("rsw-bias", "—");
    }
    if (m && m.distance_to_line_m != null) {
      setText("rsw-dist", m.distance_to_line_m.toFixed(0) + " m");
    } else {
      setText("rsw-dist", "—");
    }

    drawOnMap();
  }

  function clearMapLayers() {
    const map = window._helmlogLeafletMap;
    if (!map) return;
    mapLayers.forEach((layer) => {
      try { map.removeLayer(layer); } catch (e) { /* ignore */ }
    });
    mapLayers = [];
  }

  function drawOnMap() {
    const map = window._helmlogLeafletMap;
    if (!map || !window.L) return;
    // On /session/* pages, session.js owns a richer time-synced overlay
    // (loadHelmlogStartLineOverlay). Skip the widget's static draw here so
    // we don't paint duplicate lines/markers on top of it.
    if (window.location.pathname.startsWith('/session/')) return;
    clearMapLayers();
    if (!snapshot.start_line || !snapshot.start_line.is_complete) return;

    const sl = snapshot.start_line;
    const boat = [sl.boat_end_lat, sl.boat_end_lon];
    const pin = [sl.pin_end_lat, sl.pin_end_lon];
    const boatCarry = sl.boat_end_carried_over_from_race_id;
    const pinCarry = sl.pin_end_carried_over_from_race_id;
    const anyCarry = boatCarry || pinCarry;

    // HelmLog start line: solid orange dashed polyline (distinct from the
    // dashed-rose Vakaros line). When either end is carried over from a
    // prior race (#702), drop opacity + use a sparser dash so the helm
    // sees at-a-glance that it should re-ping if RC moved the line.
    const m = snapshot.line_metrics;
    let tip = anyCarry ? "HelmLog start line (carried over)" : "HelmLog start line";
    if (m) {
      tip += " · " + m.line_length_m.toFixed(0) + " m · "
        + m.line_bearing_deg.toFixed(0) + "°";
      if (m.line_bias_deg != null) {
        const sign = m.line_bias_deg >= 0 ? "+" : "";
        tip += " · bias " + sign + m.line_bias_deg.toFixed(0) + "°"
          + (m.favoured_end ? " " + m.favoured_end : "");
      }
    }
    if (boatCarry) tip += "\nboat end from race " + boatCarry;
    if (pinCarry) tip += "\npin end from race " + pinCarry;
    const line = L.polyline([pin, boat], {
      color: "#f59e0b",
      weight: anyCarry ? 3 : 4,
      opacity: anyCarry ? 0.55 : 0.95,
      dashArray: anyCarry ? "2, 10" : "8, 8",
    }).addTo(map).bindTooltip(tip, { sticky: true }).bindPopup(tip);
    mapLayers.push(line);

    const boatTip = boatCarry
      ? "HelmLog boat-end ping (from race " + boatCarry + ")"
      : "HelmLog boat-end ping";
    const pinTip = pinCarry
      ? "HelmLog pin-end ping (from race " + pinCarry + ")"
      : "HelmLog pin-end ping";
    const boatMarker = L.circleMarker(boat, {
      radius: 6, color: "#f59e0b", fillColor: "#f59e0b",
      fillOpacity: boatCarry ? 0.4 : 1, weight: 2,
    }).addTo(map).bindTooltip(boatTip);
    const pinMarker = L.circleMarker(pin, {
      radius: 6, color: "#f59e0b", fillColor: "#fbbf24",
      fillOpacity: pinCarry ? 0.4 : 1, weight: 2,
    }).addTo(map).bindTooltip(pinTip);
    mapLayers.push(boatMarker, pinMarker);
  }

  async function refresh() {
    try {
      const r = await fetch("/api/race-start/state");
      const ct = r.headers.get("content-type") || "";
      if (!ct.includes("application/json")) return;
      if (!r.ok) return;
      snapshot = await r.json();
      renderSnapshot();
    } catch (e) {
      // best-effort widget; never throw on the host page
    }
  }

  function init() {
    // Skip the widget on /race-start* — the page itself already shows
    // the same data more prominently.
    if (window.location.pathname.startsWith("/race-start")) return;
    refresh();
    setInterval(refresh, 2000);
    setInterval(renderClock, 250);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
