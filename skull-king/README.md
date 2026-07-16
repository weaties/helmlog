# Skull King Scorer

A self-contained scoring app for the **Skull King** card game (Grandpa Beck's
Games). Automates all the round math — bids, zero-bids, misses, and bonus
points — so you never have to add it up by hand.

## Use it

Open `index.html` in any browser (phone, tablet, laptop). No install, no
network, no build step. Everything is in the one file and your game is saved
in the browser's local storage, so a refresh or accidental close won't lose
the scores.

## Scoring rules implemented

Each round you're dealt cards equal to the round number (round 1 → 1 card,
round 10 → 10 cards). You bid how many tricks you'll win.

| Situation | Score |
|---|---|
| Bid ≥ 1 and hit it exactly | 20 × bid |
| Bid ≥ 1 and missed | −10 × (how far off) |
| Bid 0 and won 0 tricks | +10 × round number |
| Bid 0 and won any trick | −10 × round number |

**Bonus points** (added in the *Bonus* box, and only counted when you hit your
bid exactly):

| Bonus | Points |
|---|---|
| Each 14 captured (yellow / purple / green) | +10 |
| The black 14 (Jolly Roger) captured | +20 |
| Each Pirate captured by the Skull King | +30 |
| Skull King captured by a Mermaid | +50 |

## Features

- 2–8 players
- Per-round entry with +/− steppers and live round scores
- Running scoreboard with leader highlight
- Full 10-round score sheet (mirrors the paper sheet)
- Winner / tie banner at the end
- Auto-saves in the browser; **New** starts a fresh game
