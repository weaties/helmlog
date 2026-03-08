# How the Co-op Works

> A plain-language guide for sailors joining a Helm Log data co-op.

---

## The short version

Your boat has a small computer (a Raspberry Pi) that records everything
your instruments see: boat speed, wind, heading, GPS position. It also
records audio, video, and race results.

**All of that data stays on your boat.** It never goes to a central
server. You own it completely.

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

There's no central server. Each boat's Raspberry Pi talks directly to
the other Pis in the co-op over a private network (Tailscale).

```
  ┌──────────┐       ┌──────────┐       ┌──────────┐
  │  Boat A   │◄─────►│  Boat B   │◄─────►│  Boat C   │
  │  (Pi)     │       │  (Pi)     │       │  (Pi)     │
  └──────────┘       └──────────┘       └──────────┘
       ▲                                       ▲
       └───────────────────────────────────────┘
              direct, encrypted connections
                  (no central server)
```

When you share a session, the other boats in the co-op can pull your
track and instrument data directly from your Pi. When their Pis are
offline, cached copies let you still view previously shared sessions.

**If a boat's Pi is off or disconnected**, nothing breaks. The data
syncs up the next time it comes online — there's no deadline.

---

## What gets shared and what stays private

When you join a co-op, your **instrument data** from races and practices
is visible to other co-op members:

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
Nothing is ever shared automatically — every share is an explicit action
you take. You can share one Wednesday night race and skip the next. You
can share all your practices but keep the regattas private. It's entirely
up to you, every time.

---

## What you'll see in the app

When you open Helm Log on your phone, you'll see your sessions — each
race, practice, or debrief you've recorded. Each session has a **Share**
button. Tap it to share with the co-op, or leave it private. The
**Co-op** view shows all shared sessions from fleet members, with track
overlays and fleet benchmarks.

---

## How it works day-to-day

### On race day

1. Your Pi records the race automatically (instruments, audio, video)
2. After the race, you open the Helm Log web page on your phone
3. You see a prompt: "Share this session with [co-op name]?"
4. If you tap **Share**, other co-op members can see your track and results
5. If you tap **Keep Private**, nobody sees it

### Reviewing other boats

Open the **Co-op** view in Helm Log. You'll see all the races that other
boats shared:

- Overlay multiple boats on the same race map
- Compare boat speeds on the same leg
- See where you gained or lost distance
- View anonymous fleet benchmarks ("your tacks are faster than 60% of the
  fleet")

### Coaching

If your fleet has a coach, the co-op admin can grant them temporary access.
Coaches can view shared sessions but:

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

That's it. No accounts to create, no subscriptions, no cloud service.

---

## Leaving a co-op

You can leave anytime. When you leave:

- Your data is no longer visible to the co-op within 30 days
- Any cached copies of your data on other boats are deleted
- Your historical contributions are anonymized ("Boat X" replaces your
  boat name in fleet benchmarks)
- You keep all your own data on your Pi

---

## Current and tide data

If your co-op votes to build a shared current model (how the water
actually moves in your racing area), it requires **unanimous agreement**
from every active member. This is a higher bar because current knowledge
is competitively valuable.

You can opt out of current sharing even if the rest of the co-op opts in.

---

## Privacy and trust

- **Your Pi is the only place your data lives.** There is no cloud server.
- **You control what's shared.** Session-by-session, you choose.
- **Audio and conversations are always private.** They never leave your Pi
  unless you explicitly share them.
- **You can delete anything.** Deleted data is purged from your Pi and
  from any co-op member's cache.
- **The co-op has rules.** Every co-op has a charter that spells out how
  data is used. You see the charter before you join.
- **No one can use your data for gambling, protests, or surveillance.**
  These uses are explicitly prohibited.

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
No. Co-op data is view-only in the platform. There's no bulk export.
If you leave the co-op, cached copies are deleted and your contributions
are anonymized within 30 days.

**"If I leave, do other boats keep my old data?"**
No. Cached copies are deleted and your contributions are anonymized
within 30 days.

**"This is like uploading to Strava or RacingRules."**
Not at all. There is no central server. Your data lives on your Pi and
is shared directly to other boats in your co-op over an encrypted
connection. No company has access to it.

---

## Questions?

Talk to your fleet's Helm Log champion — they can explain anything about
the co-op setup or help you get started.

For the full technical details, see the
[Data Licensing Policy](data-licensing.md) and the
[Federation Protocol Design](federation-design.md).
