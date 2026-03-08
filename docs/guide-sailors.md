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

**You choose what to share.** After each race, you decide whether to
share that session with the co-op or keep it private. Nothing is shared
automatically without your action.

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

## Questions?

Talk to your fleet's Helm Log champion — they can explain anything about
the co-op setup or help you get started.

For the full technical details, see the
[Data Licensing Policy](data-licensing.md) and the
[Federation Protocol Design](federation-design.md).
