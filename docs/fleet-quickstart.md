# Helm Log — Fleet Quickstart

> Print this and hand it out at the dock.

---

## What is it?

A small computer on your boat (Raspberry Pi) that records your
instruments — boat speed, wind, heading, GPS track — and lets you share
race data with your fleet. No cloud server, no subscription.

---

## How it works

```
  Your boat's Pi  ◄──────►  Other boats' Pis
       │                          │
  Records your data       Records their data
  Stays on YOUR boat      Stays on THEIR boat
       │                          │
       └──── You choose to share ─┘
              (race by race)
```

Boats connect over Tailscale, a lightweight private network (free tier,
takes 2 minutes to set up). Data syncs when the Pi has internet — usually
a phone hotspot at the dock.

---

## What gets shared (if you choose to)

| You share               | You keep private        |
|--------------------------|--------------------------|
| GPS track               | Audio / conversations    |
| Boat speed & angles     | Photos & notes           |
| Wind speed & direction  | Crew roster              |
| Race results            | Sail selection           |

You choose what to share, **race by race**.

---

## What you get back

- See where you gained and lost vs other boats
- Fleet benchmarks ("your tacks are faster than 60% of the fleet")
- Coach can run debriefs with real data
- The whole fleet gets faster together

Fleet benchmarks need at least 4 boats sharing in similar conditions to
be meaningful. In the early days, the co-op view will be sparse — it
fills up as more boats contribute.

---

## What it costs

- **Raspberry Pi 5 (4GB)**: ~$60
- **CAN bus HAT or Signal K gateway**: ~$30-80 depending on your
  instrument system
- **SD card (32GB+)**: ~$10
- **Tailscale**: free (personal tier)
- **Helm Log software**: free and open source
- **Ongoing costs**: none

Your fleet champion can help with the specifics for your boat's setup.

---

## Getting started

1. Talk to your fleet's Helm Log champion
2. They'll help set up the Pi on your boat
3. After setup: one tap to share a race, one tap to keep it private

---

## Common questions

**Where does my data go?**
Nowhere unless you send it. It stays on your Pi.

**Do I have to share every race?**
No. You choose, race by race.

**Can I leave?**
Anytime. Your individual session data is deleted from other boats within
30 days. Fleet benchmark statistics that included your data are kept but
anonymized ("Boat X") so the fleet doesn't lose its baseline.

**Do I need internet on the water?**
No. The Pi records everything offline. You only need internet when you
want to share or view other boats' data.

**What if my Pi is offline?**
Nothing breaks. Data syncs automatically when you're back online.

---

*Questions? Talk to your fleet champion or visit the
[full sailor guide](guide-sailors.md).*
