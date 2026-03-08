# Fleet Champion's Playbook

> A guide for the tech-forward sailor bringing Helm Log to their fleet.

---

## Your role

You're the person who sees the potential: a fleet where every boat shares
race data, everyone gets faster, and nobody has to trust a commercial
platform with their competitive secrets. You're going to make that happen.

This guide covers how to pitch Helm Log to your fleet, how to get boats
set up, and how to run a co-op without it becoming a second job.

---

## The mental model

When you're explaining Helm Log to someone, the simplest way to think
about it: **each boat is its own server.** Your Pi records your data,
stores it, and serves it to other boats you choose to share with.

The co-op is just an agreement between boats to share instrument data
with each other. The technology enforces the agreement — who's in, what's
shared, when access expires — so you don't have to police it yourself.

If someone asks "where does my data go?", the answer is: "nowhere you
didn't send it."

```
  ┌──────────┐       ┌──────────┐       ┌──────────┐
  │  Boat A   │◄─────►│  Boat B   │◄─────►│  Boat C   │
  │  (Pi)     │       │  (Pi)     │       │  (Pi)     │
  └──────────┘       └──────────┘       └──────────┘
       ▲                                       ▲
       └───────────────────────────────────────┘
            direct, encrypted connections
```

---

## Pitching it to the fleet

### The one-liner

> "We all share our race tracks, we all see where we're fast and slow,
> and nobody's data goes anywhere we don't control."

### The three things sailors care about

1. **"Will it make me faster?"**
   Yes. You'll see your boat speed, angles, and maneuvers compared to the
   fleet average. You'll know exactly where you're losing and gaining.

2. **"Is my data safe?"**
   Your data stays on your boat. You choose what to share, race by race.
   You can leave anytime and your data comes with you.

3. **"Is it complicated?"**
   For you, no. After setup, it's one tap to share a race.

4. **"What does it cost?"**
   About $100 per boat for the hardware (Pi, CAN gateway, SD card).
   The software is free and open source. Tailscale is free. No ongoing
   costs or subscriptions.

### Common objections and how to answer them

**"I don't want people seeing my mistakes."**
They already see your mistakes — they're on the race course. The
difference is that now you can see theirs too, and everyone learns from
it. The boats that debrief together improve the fastest.

**"We're already fast enough."**
Without data, you're guessing. The boats at the top of every competitive
fleet use data. This makes it available to everyone, not just the boats
with the biggest budgets.

**"This sounds complicated."**
For you, it's not. After setup (which I'll help with), it's one tap to
share a race. If you can use a weather app, you can use this.

**"What if someone uses this against me in a protest?"**
They can't. Co-op data cannot be used in protests — it's explicitly
prohibited in the charter.

**"What if someone shares selectively — only their good races?"**
That's their right. Sharing is reciprocal: the more you share, the
more useful the fleet benchmarks are for everyone, including you.

### What not to lead with

- Don't lead with "peer-to-peer" or "cryptographic identity" or
  "Ed25519 signatures."
- Don't lead with the governance model. Lead with the value: faster
  sailing, better debriefs, fleet improvement.
- Don't lead with privacy protections. Lead with the positive: what
  they'll learn. Mention privacy when they ask "what's the catch?"

---

## Getting the first boats set up

### What each boat needs

| Item | Approx. cost |
|---|---|
| Raspberry Pi 5 (4GB) | ~$60 |
| CAN bus HAT or Signal K gateway | ~$30-80 |
| SD card (32GB+) | ~$10 |
| Tailscale account | Free |
| Helm Log software | Free |

The boat also needs an instrument system (B&G, Garmin, etc.) connected
via Signal K, and internet access (phone hotspot is fine).

Tailscale is a lightweight private network that encrypts all traffic
between your boats' Pis. It's what lets the boats talk to each other
without a central server. The free tier supports up to 100 devices.

### The setup sequence

If a boat doesn't have instruments yet, they can still join later — you
don't need 100% coverage to start.

1. **Start with your own boat.** Get Helm Log running, record a few
   races, make sure it works. You need to be confident before you
   evangelize.

2. **Recruit 2-3 early adopters.** Pick the boats most likely to be
   interested — usually the boats that already have instrument systems
   and care about data. Help them set up in person.

3. **Create the co-op.** Once you have 3 boats, create the co-op from
   your Pi. You'll be the initial admin. Pick one other tech-capable
   sailor as a backup admin.

4. **Run it for a few weeks.** Share races, compare tracks, show people
   the fleet benchmark view. Let the value speak for itself.

5. **Invite the rest of the fleet.** Once the early adopters are hooked,
   the remaining boats will want in. Help them set up one at a time.

### Common setup issues

| Problem | Solution |
|---|---|
| Pi won't connect to instruments | Check Signal K is running and the NMEA 2000 gateway is configured |
| No GPS data | Make sure the instrument system is outputting position (PGN 129025) |
| Pi loses time after power-off | Normal — it syncs via NTP when internet is available. The protocol handles clock drift. |
| Tailscale won't connect | Check that the phone hotspot is active and the Pi has internet |
| "My boat's WiFi is terrible" | Hardwire the Pi to a small router, tether the router to a phone |

### Backing up the Pi

Each Pi has two things worth backing up:

1. **The identity key** (`~/.helmlog/identity/boat.key` and `boat.json`)
   — this is how the co-op recognizes the boat. If the SD card dies and
   there's no backup, the boat needs to re-join the co-op as a new
   identity. Back this up to a USB stick or the boat owner's phone.

2. **The database** (`data/helmlog.db`) — all session data, race
   results, transcripts, and notes. Without a backup, historical data on
   a dead Pi is gone.

Encourage every boat to copy these two items to a USB stick at least
once a season. The setup script can configure automatic backups to an
external SSD if one is connected.

---

## Running the co-op

### Day-to-day (almost nothing)

Once the co-op is running, your job is minimal:

- **Approve new boats** when they request to join
- **Check the audit log** occasionally (is anyone accessing data weirdly?)
- **Answer questions** from fleet members

### Seasonal tasks

- **Start of season**: renew coach access if applicable, check all Pis
  are online and up to date
- **End of season**: review co-op membership, remove boats that sold or
  left the fleet
- **If a vote is needed**: proposals (like enabling current model sharing)
  are created from your admin page and sent to all active members

### What to do when things go wrong

| Situation | What to do |
|---|---|
| A boat's Pi dies | Help them set up a new one. If they backed up their identity key, they can rejoin as the same boat. If not, they rejoin as a new identity — the co-op still has their old shared data cached. Historical data on the dead Pi is lost unless they backed up the database (see "Backing up the Pi" above). |
| Someone wants to leave | They can leave from their own Pi. Session data is deleted from peers within 30 days; benchmark contributions are anonymized. |
| Dispute about data sharing | Point to the charter. If the charter doesn't cover it, propose an amendment and vote. |
| Coach wants more access than allowed | The platform enforces the rules — it's not personal, it's policy. |
| "I shared a session but nobody can see it" | The other boats need to be online to pull the data. If their Pis are off, it syncs next time they connect. |
| "The clock on my Pi is wrong" | Normal after a power cycle — syncs via NTP when internet is available. The protocol tolerates up to 5 minutes of drift. |
| "I only want to share with some boats" | Sharing is all-or-nothing within the co-op. To share selectively, keep the session private and share files directly outside the platform. |
| "A new boat joined but can't see old races" | Correct — new members see sessions shared after they join. No backfill. |

---

## Light mode vs full mode

### Light mode (3-5 boats)

For small fleets, keep it simple:

- **Single admin** with a designated backup
- **No formal voting** — decisions by consensus on the dock
- **No multi-admin signatures** — the admin manages join/leave directly
- **Charter is optional** — a handshake agreement is fine at this size

### Full mode (6+ boats)

Once a fleet is large enough that not everyone knows each other well,
formalize:

- **2-3 admins** with multi-admin signing
- **Written charter** (use the template)
- **Formal voting** on proposals (2/3 supermajority)
- **Embargo support** if the fleet wants delayed sharing
- **Coach access** with time-limited grants

The threshold isn't magic — some tight-knit fleets of 8 stay in light
mode, and some fleets of 5 with mixed trust levels go to full mode early.
The point is: don't add governance overhead until you need it.

---

## How decisions get made

### In light mode

Decisions happen on the dock. You're the admin, you have a backup, and
everyone knows each other. If the fleet agrees, you make the change.

### In full mode

Decisions go through the charter:

- **Routine changes** (adding a boat, renewing coach access) — the admin
  handles it directly
- **Policy changes** (enabling current model sharing, granting cross-co-op
  access, adding a new admin) — require a proposal and a 2/3 supermajority
  vote from active members
- **Current model sharing** — requires unanimous consent (higher bar because
  current knowledge is competitively valuable)

The platform enforces these rules. The signed records are on every Pi.

---

## How to run a dock talk

Budget 20-30 minutes if you expect questions:

1. **5 minutes: what it is** — use the mental model ("each boat is its
   own server"), show the diagram, explain shared vs private
2. **5 minutes: show a shared race** — pull up a real session on your
   phone, overlay two boats, point out where one gained on a beat
3. **10-20 minutes: Q&A** — let people ask questions, handle objections
   using the language above

Bring a printout of the [Fleet Quickstart](fleet-quickstart.md) for
anyone who wants to take something home.

---

## Resources

- [Fleet Quickstart](fleet-quickstart.md) — one-page handout, print and
  give to every boat at the dock
- [How the Co-op Works](guide-sailors.md) — give this to sailors who
  want to understand the co-op before joining
- [Coach Access Guide](guide-coaches.md) — give this to coaches
- [Data Licensing Policy](data-licensing.md) — the full policy (technical)
- [Federation Protocol](federation-design.md) — how the protocol works
  (technical)
- [Co-op Charter Template](co-op-charter-template.md) — fillable charter
  for your co-op
