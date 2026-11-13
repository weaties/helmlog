---
name: domain
description: Sailing instrument tribal knowledge that is NOT directly grep-recoverable from the code — common-mistake guards, B&G/instrument-vendor quirks, J/105 polar reference targets, and calibration miscalibration symptoms. Auto-triggers when working on sk_reader.py, can_reader.py, nmea2000.py, polar.py, boat_settings.py, synthesize.py, maneuver_detector.py, export.py, or calibration-related code. Skill content is intentionally narrow — for paths, PGN byte layouts, conversion constants, and table schemas already encoded in the source, read sk_reader.py / nmea2000.py and search storage.py.
---

# Sailing Instrument Domain — Tribal Knowledge

This skill encodes only what the codebase does NOT make obvious on its own:
common mistakes the model has made before, vendor-specific quirks, polar
target speeds for our hull, and miscalibration symptoms. For Signal K paths,
PGN byte layouts, conversion constants, table schemas, and unit conversions,
read `sk_reader.py` and `nmea2000.py`, and search `storage.py` (it is over
12,000 lines — grep for the table or method, don't read it whole). Those files
are the source of truth and are always more current than this skill.

---

## 1. Common mistakes (priming guards)

These are errors the model has historically made. Read these before writing
instrument-handling code.

- **Polar calculations always use BSP (`navigation.speedThroughWater`), never
  SOG (`navigation.speedOverGround`).** SOG includes current — a 1-knot
  favorable current makes the boat look 1 knot faster at every TWA/TWS bin
  and contaminates the polar.
- **TWA is derived FROM apparent wind, not the reverse.** AWA + AWS + BSP →
  TWA + TWS. The masthead wand measures AWA directly; TWA is computed upstream by
  the B&G instrument system. Code that "derives AWA from TWA" is
  upside-down.
- **TWA is 0–180 in HelmLog**, not 0–360. Port/starboard symmetry is assumed
  for polars. If you find yourself wrapping a TWA value through 360, you are
  probably handling TWD (true wind direction), not TWA.
- **Wind reference field matters.** PGN 130306's reference field decides
  what `wind_angle_deg` actually means. Mishandling this is a recurring bug.
  Codes (also in `nmea2000.py`):
  - `0` — true, boat-referenced — value IS TWA
  - `2` — apparent — filter out for polars
  - `4` — true, north-referenced — value is TWD;
    **TWA = `(TWD - HDG + 360) % 360`** then fold to 0–180
- **B&G systems typically emit reference 4 (TWD), not reference 0 (TWA).**
  So `_compute_twa()` in `polar.py` is on the hot path; do not bypass it.
- **Reference=4 needs a contemporaneous heading.** If `HeadingRecord` is
  missing or stale, drop the wind sample — do not guess.
- **AIS PGNs (129038–129810) are never ingested.** Data licensing
  requirement (#208), not a technical limitation. If you find yourself
  adding an AIS decoder, stop and check `docs/data-licensing.md`.

---

## 2. J/105 reference polars (used for synthetic test data)

These are the optimal upwind/downwind targets for our hull, used by
`synthesize.py` to generate fixture data. They are not in the SQL schema; if
you need numeric ground truth for a test, this is it:

| TWS (kts) | Upwind TWA | Upwind BSP | Downwind TWA | Downwind BSP |
|---|---|---|---|---|
| 6  | 44° | 5.2 kts | 150° | 4.8 kts |
| 8  | 43° | 6.0 kts | 145° | 5.8 kts |
| 10 | 42° | 6.5 kts | 140° | 6.5 kts |
| 12 | 41° | 6.8 kts | 135° | 7.0 kts |
| 16 | 39° | 7.3 kts | 130° | 7.6 kts |

If observed BSP is consistently >10% above or below these for the same
(TWS, TWA), suspect BSP calibration drift before assuming the model is fast
or slow.

---

## 3. Polar binning conventions

Not obvious from code at a glance:

- **TWS bins:** floor of TWS in knots (0, 1, 2, … 30+).
- **TWA bins:** floor to nearest 5° (0, 5, 10, … 175, 180).
- **Per bin:** mean BSP, P90 BSP, session count, sample count.
- **Minimum sessions:** 3 races before the baseline is published.

VMG bands (for VMG-per-sail analysis): 0–6, 6–10, 10–15, 15–20, 20+ kts.

---

## 4. Calibration miscalibration symptoms

Crew-entered tuning parameters live in `boat_settings.py`. They are stored
for debrief correlation; most do not yet feed calculations. The non-obvious
part is what miscalibration *looks like* in the data:

| Parameter | What it controls | Miscalibration symptom |
|---|---|---|
| **BSP calibration** | Paddlewheel scale factor | Polars consistently above/below J/105 targets; VMG unreliable |
| **AWA offset** | Wind vane zero point | Upwind performance differs port vs. starboard tack |
| **Compass deviation** | Heading correction table | TWA wrong when derived from TWD (ref=4); tack/gybe misclassified |
| **Depth offset** | Transducer-to-keel distance | `offset_m` in `DepthRecord`; shallow-water alarms fire at wrong depth |
| **GPS antenna position** | SOG/COG | Usually accurate; offset matters only for match-racing precision |

If a debrief shows asymmetric upwind speed across tacks, the first
hypothesis should be AWA offset, not crew technique.

**The boat's instrument system is B&G Triton² displays, not an H5000
processor.** Triton² has a single global STW calibration and no
processor-grade features: no per-tack STW cal, no leeway compensation, no
heel-compensated deviation table. Anything of that kind has to be corrected
in HelmLog software, so do not recommend
"set it in the processor" as a fix.

---

## 5. Maneuver classification rule

Heading change alone does NOT classify a tack vs. gybe — TWA before and
after determines it. Source of truth is `maneuver_detector.py`, but the
rule in plain English:

| TWA before | TWA after | Heading change | Classification |
|---|---|---|---|
| <90° (upwind) | <90° (upwind) | ≥60° | **Tack** |
| >90° (downwind) | >90° (downwind) | ≥60° | **Gybe** |
| crosses 90° boundary | — | ≥60° | **Mark rounding** |

Without true wind data the detector falls back to a generic `"maneuver"`
type — heading change with no TWA context is unclassified, not assumed.
