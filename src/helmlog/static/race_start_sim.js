// Race-start simulator UI (#690).

(function () {
  "use strict";

  const logEl = document.getElementById("sim-log");
  const clockEl = document.getElementById("sim-clock-display");
  const clockStatusEl = document.getElementById("sim-clock-status");
  const stepStatusEl = document.getElementById("sim-step-status");
  const scenariosEl = document.getElementById("sim-scenarios");

  let currentScenario = null;
  let currentStep = 0;

  function log(msg) {
    const row = document.createElement("div");
    row.className = "row";
    const ts = new Date().toLocaleTimeString();
    row.textContent = "[" + ts + "] " + msg;
    logEl.insertBefore(row, logEl.firstChild);
    while (logEl.children.length > 50) logEl.removeChild(logEl.lastChild);
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
    return data;
  }

  function formatOffset(s) {
    if (s === 0) return "+0s";
    const sign = s < 0 ? "−" : "+";
    const abs = Math.abs(s);
    if (abs >= 60) return sign + Math.floor(abs / 60) + "m" + Math.floor(abs % 60) + "s";
    return sign + abs.toFixed(0) + "s";
  }

  async function refreshClock() {
    try {
      const r = await fetch("/api/race-start/sim/clock");
      const data = await r.json();
      clockEl.textContent = formatOffset(data.offset_s);
      clockStatusEl.textContent = data.offset_s === 0
        ? "at real time"
        : "FSM is " + formatOffset(data.offset_s) + " from real time";
    } catch (e) {
      clockStatusEl.textContent = "clock fetch failed: " + e.message;
    }
  }

  const sim = {
    async setOffset(offset_s) {
      try {
        await postJSON("/api/race-start/sim/clock", { offset_s });
        log("clock → " + formatOffset(offset_s));
        await refreshClock();
      } catch (e) {
        log("error: " + e.message);
      }
    },

    async writeBoat() {
      const body = {
        latitude_deg: parseFloat(document.getElementById("sim-lat").value),
        longitude_deg: parseFloat(document.getElementById("sim-lon").value),
        sog_kn: parseFloat(document.getElementById("sim-sog").value),
        cog_deg: parseFloat(document.getElementById("sim-cog").value),
        twd_deg: parseFloat(document.getElementById("sim-twd").value),
        tws_kn: parseFloat(document.getElementById("sim-tws").value),
      };
      try {
        const r = await postJSON("/api/race-start/sim/boat", body);
        log("boat: wrote " + r.written.join(", "));
      } catch (e) {
        log("boat error: " + e.message);
      }
    },

    async loadScenarios() {
      try {
        const r = await fetch("/api/race-start/sim/scenarios");
        const data = await r.json();
        scenariosEl.innerHTML = "";
        for (const sc of data.scenarios) {
          const btn = document.createElement("button");
          btn.className = "preset";
          btn.textContent = sc.name + " (" + sc.steps + " steps)";
          btn.onclick = () => sim.startScenario(sc.name);
          scenariosEl.appendChild(btn);
        }
        const next = document.createElement("button");
        next.textContent = "Next step ›";
        next.onclick = () => sim.nextStep();
        scenariosEl.appendChild(next);
      } catch (e) {
        scenariosEl.textContent = "scenario fetch failed: " + e.message;
      }
    },

    async startScenario(name) {
      currentScenario = name;
      currentStep = 0;
      log("scenario: " + name + " — step 0");
      await this.applyStep();
    },

    async nextStep() {
      if (!currentScenario) return log("pick a scenario first");
      currentStep += 1;
      await this.applyStep();
    },

    async applyStep() {
      try {
        const r = await postJSON("/api/race-start/sim/step", {
          scenario: currentScenario,
          step_index: currentStep,
        });
        stepStatusEl.textContent = "step " + r.step_index + ": " + r.label;
        log("step " + r.step_index + ": " + r.label);
        await refreshClock();
        if (r.is_last) {
          stepStatusEl.textContent += " (last step)";
          currentScenario = null;
        }
      } catch (e) {
        log("step error: " + e.message);
      }
    },

    async runDrill() {
      const body = {
        center_lat: parseFloat(document.getElementById("sim-drill-lat").value),
        center_lon: parseFloat(document.getElementById("sim-drill-lon").value),
        line_bearing_deg: parseFloat(document.getElementById("sim-drill-bearing").value),
        line_length_m: parseFloat(document.getElementById("sim-drill-len").value),
        twd_deg: parseFloat(document.getElementById("sim-drill-twd").value),
        sog_kn: parseFloat(document.getElementById("sim-drill-sog").value),
        duration_s: parseFloat(document.getElementById("sim-drill-dur").value),
      };
      try {
        const r = await postJSON("/api/race-start/sim/drill", body);
        log("drill: " + r.duration_s + "s, race_id=" + (r.race_id ?? "(none)"));
      } catch (e) {
        log("drill error: " + e.message);
      }
    },

    async reset() {
      try {
        await postJSON("/api/race-start/sim/reset", {});
        currentScenario = null;
        currentStep = 0;
        stepStatusEl.textContent = "";
        log("reset");
        await refreshClock();
      } catch (e) {
        log("reset error: " + e.message);
      }
    },
  };

  window.sim = sim;
  refreshClock();
  sim.loadScenarios();
  setInterval(refreshClock, 5000);
})();
