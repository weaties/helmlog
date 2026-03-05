# J105 Logger — Crew Operations Guide

_Last reviewed: 2026-03-01 · App version: schema v16_

Quick reference for using the logger on race day.
No technical knowledge required. Print double-sided, laminate, keep in the nav station.

---

## What the system does

The J105 Logger is an always-on Raspberry Pi that collects data from the boat's
B&G instrument system (via NMEA 2000) and gives the crew tools to:

- **Mark races** — one tap to start and stop race sessions; every instrument
  reading is captured automatically for the duration
- **Log notes** — text observations, boat settings (vang, cunningham, etc.), or
  photos, all timestamped and attached to the session
- **Record the debrief** — if the Gordik USB mic is plugged in, record audio
  debriefs and get an automatic text transcript
- **Track results** — record finishing positions with boat names / sail numbers
  directly in the app
- **Track sails** — record which main, jib, and spinnaker were used each race
- **Export data** — download a CSV spreadsheet or GPX track for any race for
  import into Sailmon, Expedition, or a spreadsheet tool
- **Link YouTube videos** — sync a GoPro upload with instrument data by
  marking a common timestamp; the app then generates a deep-link to the exact
  moment in the video for any point in the race
- **View Grafana dashboards** — one-tap link from any race card opens a live
  time-series Grafana dashboard scoped to that race window, showing boatspeed,
  true wind, COG, heading, and more
- **Browse history** — searchable, filterable log of every session with all of
  the above tools available for past races

---

## 1. Connecting to the system

**On the boat (Tailscale network):**

1. Make sure your phone or tablet is on the boat's **Tailscale** network.
   _(One-time setup — ask the navigator if you haven't joined yet.)_
2. Open your browser and go to: **`http://corvopi:3002`**
3. Bookmark this address — you'll open it at the start line.

The page refreshes itself. You do not need to reload it manually.

---

## 2. Before the race

### Set the event name _(non-Monday / non-Wednesday only)_

On **Monday** the event is set to "BallardCup" automatically.
On **Wednesday** it is set to "CYC" automatically.
On any other day an event name field appears at the top — type the name
(e.g. "Swiftsure") and tap **Save**.

### Check instruments are live

Tap **Instruments** to expand the panel. You should see live numbers for BSP,
TWS, TWA, HDG, COG, SOG, AWS, AWA, and TWD updating every 2 seconds.
Grey numbers mean the instrument feed has stalled — tell the navigator.

### Enter the crew _(optional)_

Tap **Crew** to expand the panel. Fill in the names for Helm, Main, Pit, Bow,
Tac, and Guest, then tap **Save Crew**.
Previously used names appear as quick-tap chips below each field.

---

## 3. During the race

### Starting a session

| What you're doing | Button to tap |
|---|---|
| Start a race | **▶ START RACE N** |
| Start a practice | **▶ START PRACTICE** |

The button shows the race name and a running duration timer once the session
is active. The next tap will be **■ END RACE N**.

### Ending a session

Tap **■ END RACE N** (or **■ END PRACTICE**) when the session is over.
The session closes and appears in the "Today's races" list below.

### Adding a note

While a session is in progress the **+ Note** button is visible.

1. Tap **+ Note**.
2. Choose the note type:
   - **Text** — free observation ("lee bow tack at the pin")
   - **Settings** — key/value boat settings ("vang: 5 turns off")
   - **Photo** — take a photo or choose one from your camera roll
3. Tap **Save Note**.

Notes are timestamped automatically and saved permanently.

### Recording race results

Tap **Results ▶** on a completed race card in the "Today's races" list.

- Type a sail number or boat name in the **Search boat…** box.
- Tap the boat to add it at the next finishing position.
- New boats can be added on the fly with **+ Add "…"**.
- Mark **DNF** or **DNS** with the buttons next to each entry.
- Remove an entry with the red **✕** button.

---

## 4. After the race

### Download race data

Every completed race card has:

| Button | Downloads |
|---|---|
| **↓ CSV** | Spreadsheet with every instrument reading |
| **↓ GPX** | GPS track for navigation apps |

### Record a debrief

If the Gordik microphone is plugged in, a **🎙 Debrief** button appears on
completed race cards (visible only when no session is currently active).

1. Tap **🎙 Debrief** — a purple "Debrief in progress" card appears with a timer.
2. Talk through the debrief with the team.
3. Tap **⏹ STOP DEBRIEF** when done.

The audio is saved and linked to that race.

### Record which sails were used

Tap **⛵ Sails ▶** on a race card, select the Main, Jib, and Spinnaker from
the dropdown menus, then tap **Save Sails**.

---

## 5. Viewing past sessions

Open **📋 History** (top-right link on the home page).

Use the controls at the top to find sessions:

| Control | What it does |
|---|---|
| Search box | Filter by session name or event |
| **All / Race / Practice / Debrief** | Filter by session type |
| From / To date pickers | Narrow to a date range |
| **← Prev** / **Next →** | Move between pages (25 sessions per page) |

Each session card has buttons for:

| Button | Opens |
|---|---|
| **↓ CSV** / **↓ GPX** | Download race data |
| **📊 Grafana** | Grafana dashboard scoped to the race window |
| **Results ▶** | Finishing positions |
| **Notes ▶** | Timestamped notes from the race |
| **🎬 Videos ▶** | Linked YouTube videos |
| **⛵ Sails ▶** | Sails used |
| **↓ WAV** | Download debrief audio recording |
| **📝 Transcript ▶** | Start or view a text transcription of the audio |

---

## 6. Troubleshooting

**"The page won't load"**
→ Check you're on the Tailscale network. Try refreshing.
→ If it still won't load, the Pi may have restarted — ask the navigator to check
  `sudo systemctl status j105-logger`.

**"Instrument numbers are grey or frozen"**
→ The Signal K feed has stalled. Ask the navigator:
  `sudo systemctl status signalk j105-logger`

**"The warning banner says Disk XX% full"**
→ Disk is above 85 %. Old WAV files are the biggest user of space.
  Ask the navigator to archive or delete old recordings.

**"The warning banner says CPU temp XX°C"**
→ Normal in direct sunlight. If overheating in shade, check Pi ventilation.

**"↓ CSV is empty or very small"**
→ The logger service may not have been running when the race was active.
  Check `j105-logger status` to confirm rows were written.

**"The START RACE button is missing or greyed out"**
→ A race or debrief is already active. Tap the **■ END** button to close it first.

**Emergency note**
The logger is read-only on the instruments — it cannot affect boat systems.
If anything breaks in the app, simply close the browser. The boat is fine.
