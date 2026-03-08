# How the Co-op Works

> A plain-language guide for sailors joining a Helm Log data co-op.

---

## The short version

Your boat has a small computer (a Raspberry Pi) that records everything
your instruments see: boat speed, wind, heading, GPS position. It also
records audio, video, and race results. All of that data stays on your
boat — you own it completely.

If you join a **co-op**, you agree to share some of that data with other
boats in your fleet. In return, you see theirs. Everyone gets faster.

---

## Why fleets do this

Sharing race data within a fleet is the single fastest way to improve
everyone's sailing. When you can overlay your track against the boats
that beat you, you stop guessing and start seeing exactly where you lost
distance — the bad lane off the start, the extra tack on the beat, the
slow set on the run. Multiply that across a season and the whole fleet
gets faster.

The problem has always been logistics: who collects the data, where does
it live, and who do you trust with it? Helm Log removes all of that.
Each boat records its own data and shares directly with the fleet — no
middleman, no subscriptions, no upload to someone else's server.

---

## How data moves between boats

Each boat's Raspberry Pi talks directly to the other Pis in the co-op
over Tailscale, a lightweight private network that encrypts all traffic
between your boats (free tier, takes 2 minutes to set up).

```
  ┌──────────┐       ┌──────────┐       ┌──────────┐
  │  Boat A   │◄─────►│  Boat B   │◄─────►│  Boat C   │
  │  (Pi)     │       │  (Pi)     │       │  (Pi)     │
  └──────────┘       └──────────┘       └──────────┘
       ▲                                       ▲
       └───────────────────────────────────────┘
            direct, encrypted connections
```

When you tap **Share**, you're authorizing the co-op to see that session.
The actual data transfer happens when your Pi has an internet connection
(usually a phone hotspot at the dock). Other boats can then pull your
track and instrument data directly from your Pi. When their Pis are
offline, cached copies let you still view previously shared sessions.

If a boat's Pi is off or disconnected, nothing breaks — the data syncs
up the next time it comes online.

```
  Instrument → Pi records → You tap "Share" → Pi connects at dock → Peers pull data
  (on water)   (offline)    (authorization)    (phone hotspot)       (over Tailscale)
```

---

## What gets shared and what stays private

When you join a co-op, your **instrument data** from sessions you choose
to share is visible to other co-op members:

| Shared with the co-op | Stays private to you |
|---|---|
| GPS track (where you sailed) | Audio recordings |
| Boat speed, heading, angles | Transcribed conversations |
| Wind speed and direction | Photos and notes |
| Race results and finish order | Crew roster and positions |
| | Sail selection |
| | YouTube video links |

**You choose what to share, session by session.** After each race, you
decide whether to share that session with the co-op or keep it private.
Nothing is ever shared automatically — every share is an explicit action.
You can share one Wednesday night race and skip the next. You can share
all your practices but keep the regattas private. It's entirely up to
you, every time.

---

## What you'll see in the app

When you open Helm Log on your phone, you'll see your sessions — each
race, practice, or debrief you've recorded. Each session has a **Share**
button. Tap it to share with the co-op, or leave it private. The
**Co-op** view shows all shared sessions from fleet members, with track
overlays and fleet benchmarks.

In the early days of a new co-op, the co-op view will be sparse. It
fills up as more boats share more sessions.

---

## How it works day-to-day

### On race day

1. Your Pi records the race automatically (instruments, audio, video) —
   you don't need to be online during the race, everything syncs later
2. After the race, you open the Helm Log web page on your phone
3. You see a prompt: "Share this session with [co-op name]?"
4. If you tap **Share**, other co-op members can see your track and results
5. If you tap **Keep Private**, nobody sees it

### Reviewing other boats

Open the **Co-op** view in Helm Log. You'll see all the races that
other boats shared:

- Overlay multiple boats on the same race map
- Compare boat speeds on the same leg
- See where you gained or lost distance
- View anonymous fleet benchmarks ("your tacks are faster than 60% of the
  fleet") — these require at least 4 boats sharing in similar conditions

### Coaching

If your fleet has a coach, the co-op admin can grant them temporary access.
Coaches can view shared instrument data but:

- Access has an expiration date
- They can't download or export your data in bulk
- They can't aggregate data across multiple co-ops
- When access expires, it's done automatically

---

## Joining a co-op

1. **Get Helm Log running on your boat** — the fleet champion can help
   with setup
2. **Ask to join** — the co-op admin sends you an invite
3. **Review the charter** — you'll see what agreements the co-op has
   (e.g., coaching access, benchmark sharing) before you join
4. **Accept** — you're in. Start sharing races.

That's it. No accounts to create, no subscriptions.

---

## Leaving a co-op

You can leave anytime. When you leave, two things happen over 30 days:

1. **Your identifiable session data is deleted from other boats' caches.**
   No one can view your tracks, instrument time-series, or race results.
2. **Fleet benchmarks that included your data are preserved but
   anonymized** — "Boat X" replaces your boat name in aggregate
   statistics (like fleet percentile rankings), so the fleet's
   historical benchmarks remain valid without identifying you.

In short: your individual sessions are gone, but the anonymous
statistical contributions remain so the fleet doesn't lose its baseline.

You keep all your own data on your Pi.

If you need a temporary break (injury, boat work, sabbatical), you don't
have to leave the co-op — just stop sharing sessions. Your membership
stays active, and you can resume sharing whenever you're ready.

---

## Current and tide data

If your co-op votes to build a **shared current model**, it requires
**unanimous agreement** from every active member.

What it is: Helm Log can compare each boat's speed-through-water and
heading against its GPS speed and course-over-ground. The difference
reveals how the water is actually moving — current direction and
strength — at each point on the race course. Aggregate this across
multiple boats and races, and you get a picture of the tidal and current
patterns in your sailing area.

Why the higher bar: current knowledge is one of the most competitively
valuable pieces of local expertise a sailor can have. Sharing GPS tracks
reveals where you sailed; sharing current models reveals what the water
was doing, which is harder to observe and more strategically sensitive.
That's why this requires unanimity rather than a simple majority.

You can opt out of current sharing even if the rest of the co-op opts in.

---

## Privacy and trust

- **You control what's shared.** Session-by-session, you choose.
- **Audio and conversations are always private.** They never leave your Pi
  unless you explicitly share them.
- **You can delete anything.** Deleted data is purged from your Pi and
  from any co-op member's cache. Note: if you linked a YouTube video to
  a session, deleting the session removes the link from Helm Log but
  does not delete the video from YouTube — you'd need to do that
  separately on YouTube.
- **The co-op has rules.** Every co-op has a charter that spells out how
  data is used. You see the charter before you join.
- **Prohibited uses.** The charter explicitly prohibits using co-op data
  in protest hearings or for surveillance of other boats.

---

## Common misconceptions

**"If I share, everyone can see everything."**
No. Only instrument data (speed, track, wind) from sessions you
explicitly share. Audio, video, notes, crew, and sail choices are always
private.

**"I have to share every race."**
No. Sharing is per-session and entirely optional. Share the ones you
want, skip the ones you don't.

**"Someone could download my data and keep it forever."**
Co-op data is view-only in the platform. There's no bulk export.

**"If I leave, do other boats keep my old data?"**
Your individual sessions are deleted from other boats' caches within 30
days. Anonymous contributions to fleet benchmarks are preserved as
"Boat X." One exception: if you linked YouTube videos to sessions, the
videos still exist on YouTube — Helm Log can't delete content from a
third-party platform.

**"If I share, can the coach see everything?"**
Only the instrument data you shared, and only during their access
window. Audio, video, notes, and crew info are always private.

---

## Questions?

Talk to your fleet's Helm Log champion — they can explain anything about
the co-op setup or help you get started.

For the full technical details, see the
[Data Licensing Policy](data-licensing.md) and the
[Federation Protocol Design](federation-design.md).
