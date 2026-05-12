---
name: ocs-check
description: Detect OCS (over-the-line at gun, returned to clear) races against the live DB and apply the 'ocs' tag to any newly identified ones. TRIGGER when the user asks "check for OCS", "did we OCS", "tag OCS races", "/ocs-check", or after a race day to find any starts that should be marked OCS. Auto-trigger when running /debrief on a race that hasn't been OCS-checked yet.
---

# /ocs-check — auto-detect and tag OCS races

Run the OCS detection logic from `scripts/analysis/ocs_detect.py`
against the live `corvopi-live` database, identify any races where
the boat was over the line at the gun and returned to clear, and
apply the `ocs` tag to those sessions.

## Usage

- `/ocs-check` — scan all completed races (not yet OCS-checked) and
  tag any new OCS-returned ones.
- `/ocs-check <race-id>` — scan one specific race.
- `/ocs-check --since YYYY-MM-DD` — only scan races on or after a date.

## Inputs

- DB: live on `weaties@corvopi-live:/home/weaties/helmlog/data/logger.db`.
  All reads via SSH + sqlite3; tags are inserted via SQL on the same
  DB (the helmlog `tags` API would work too, but direct SQL is
  simpler and doesn't need auth).

## Steps

1. **Resolve the race set.** Default: every race session
   (`session_type='race'`, `end_utc IS NOT NULL`, `start_utc != end_utc`,
   `name NOT LIKE '%RTTS%'`) with a Vakaros gun event AND
   `vakaros_line_positions` endpoints, that has not already been
   tagged `ocs`.

2. **Run the detection.** For each candidate race, follow the algorithm
   in `scripts/analysis/ocs_detect.py`:

   a. Find the gun: latest `vakaros_race_events` row with
      `event_type='race_start'` between `start_utc` and `end_utc`.

   b. Find the most recent line endpoints (boat + pin) at or before
      the gun from `vakaros_line_positions`. If either is missing, skip
      the race with a "no line data" note.

   c. Compute the boat's "side of line" each second across the window
      `[gun-90s, gun+300s]` using a cross-product on lat/lon-projected
      coords (use `latlon_to_xy` and `side_of_line` in
      `scripts/analysis/start_quality.py`).

   d. **pre_side** = majority side over `[gun-60s, gun-10s]` (need ≥5
      samples non-zero).

   e. **at_gun_side** = majority side over `[gun-3s, gun+3s]` (need ≥2
      samples).

   f. **returned** = at any point in `[gun+5s, gun+300s]`, side ==
      pre_side.

   g. Classify:
      - `clean` — pre_side == at_gun_side
      - `OCS — returned` — pre_side != at_gun_side AND returned
      - `OCS? (no return)` — pre_side != at_gun_side AND not returned

   The "OCS? (no return)" cases are usually false positives from
   pre-start positioning noise (boat happens to be on race side at
   gun without an actual OCS) — do **not** auto-tag these. List them
   for manual review.

3. **Apply tags for confirmed OCS races.** For each `OCS — returned`
   race that doesn't already have the tag:

   ```sql
   INSERT INTO entity_tags (tag_id, entity_type, entity_id, created_at, source)
   SELECT (SELECT id FROM tags WHERE name='ocs'), 'session', {race_id},
          '{now_iso}', 'auto'
   WHERE NOT EXISTS (
     SELECT 1 FROM entity_tags
     WHERE tag_id=(SELECT id FROM tags WHERE name='ocs')
       AND entity_type='session' AND entity_id={race_id}
   );

   UPDATE tags SET usage_count = (
     SELECT COUNT(*) FROM entity_tags WHERE tag_id=tags.id
   ), last_used_at='{now_iso}' WHERE name='ocs';
   ```

   The `ocs` tag already exists (id 20, color #dc2626). Don't create it.

4. **Report.** Print a summary table:

   ```
   OCS scan complete. {N} races scanned.

   Newly tagged as OCS:
     - 2026-04-14  20260414-Ballard cup 1-1 (https://corvo105.helmlog.org/session/35/...)
     - 2026-04-19  20260419-PSSR-2          (https://...)

   Already tagged (no change):
     - {date  name}

   No line data (skipped):
     - {date  name}

   OCS? (no return — review manually, NOT tagged):
     - {date  name}
   ```

## DO NOT

- Do not auto-tag the "no return" cases (high false-positive rate;
  they need a human to look at the track).
- Do not delete or modify existing OCS tags.
- Do not bump `usage_count` if no new tags were inserted.
- Do not run against practice sessions or RTTS-style long-distance
  races.

## Pre-existing OCS tags (as of 2026-05-12)

- session 35 (4/14 Ballard Cup 1-1)
- session 55 (4/19 PSSR-2)

If those are missing from the live DB when the skill runs, something
got cleared — confirm with the user before re-tagging.
