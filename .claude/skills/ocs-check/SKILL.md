---
name: ocs-check
description: Detect OCS (over-the-line at gun, returned to clear) races against the live DB and apply the 'ocs' tag to any newly identified ones. TRIGGER when the user asks "check for OCS", "did we OCS", "tag OCS races", "/ocs-check", or after a race day to find any starts that should be marked OCS. Auto-trigger when running /debrief on a race that hasn't been OCS-checked yet.
---

# /ocs-check — auto-detect and tag OCS races

Run `scripts/analysis/ocs_detect.py` against the live `corvopi-live`
database, identify any races where the boat was over the line at the gun
and returned to clear, and apply the `ocs` tag to those sessions.

The script is the source of truth for the detection algorithm (windows,
sample minimums, classification). Run it — do not re-derive the algorithm
from memory or from this skill. This skill covers what the script does
not: which results get tagged, the confirmation step, and the report.

## Usage

- `/ocs-check` — scan all completed races and tag any new OCS-returned ones.
- `/ocs-check <race-id>` — report and tag one specific race.
- `/ocs-check --since YYYY-MM-DD` — only races on or after a date.

## Inputs

- DB: live on `weaties@corvopi-live:/home/weaties/helmlog/data/logger.db`.
  Query it in place over SSH; do not copy it locally.

## Steps

1. **Run the detector.** It is stdlib-only and only reads:

   ```bash
   ssh weaties@corvopi-live \
     "cd ~/helmlog && python3 scripts/analysis/ocs_detect.py data/logger.db"
   ```

   It scans every completed race (it already excludes practice sessions
   and RTTS-style distance races) and has no race or date filter, so for
   `<race-id>` or `--since` filter its output. If the script is missing
   from the Pi's checkout, read its `main()` and run the same queries over
   `ssh … sqlite3` rather than improvising a different method.

   Each race gets one of three verdicts: `clean`, `OCS — RETURNED`, or
   `OCS? (no return)`. Races with no Vakaros session, gun event or line
   endpoints are skipped silently by the script — list those yourself as
   "no line data" by comparing against the full completed-race list.

2. **Check existing tags.**

   ```sql
   SELECT entity_id FROM entity_tags
   WHERE entity_type='session'
     AND tag_id=(SELECT id FROM tags WHERE name='ocs');
   ```

   If the `ocs` tag does not exist, or no session carries it at all, stop
   and ask — sessions 35 and 55 were tagged before this skill existed, so
   an empty result means something was cleared. Never create the tag
   yourself.

3. **Confirm before writing.** Show the `OCS — RETURNED` races that are
   not yet tagged and wait for a yes. This writes to the live boat
   database. (When `/debrief` invokes this for the one race the user
   already confirmed, that confirmation covers this race's tag.)

4. **Apply the tags** for the confirmed races:

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

5. **Report.** Print a summary table:

   ```
   OCS scan complete. {N} races scanned.

   Newly tagged as OCS:
     - 2026-04-14  20260414-Ballard cup 1-1 (https://corvo105.helmlog.org/session/35/...)

   Already tagged (no change):
     - {date  name}

   No line data (skipped):
     - {date  name}

   OCS? (no return — review manually, NOT tagged):
     - {date  name}
   ```

## DO NOT

- Do not auto-tag the `OCS? (no return)` cases. They are usually
  pre-start positioning noise (the boat happens to be on the race side at
  the gun without an actual OCS) and need a human to look at the track.
- Do not delete or modify existing OCS tags.
- Do not run the `UPDATE tags` statement if no new tags were inserted.
- Do not run against practice sessions or RTTS-style long-distance races.
