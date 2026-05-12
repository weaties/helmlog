---
name: debrief
description: Run a focused per-race debrief and attach the summary as a moment on the session. TRIGGER when the user says "debrief race N", "debrief that race", "/debrief", "post-race analysis", or asks for analysis of a single specific race. DO NOT trigger for season-wide reviews (that's a manual run of scripts/analysis/full_analysis.py), broad questions about boat performance, or non-race sessions.
---

# /debrief — per-race performance debrief

Generate a concise debrief for a single race and persist it on the
session in the helmlog UI as a moment, so the crew can see it next time
they open the session detail page.

## Usage

- `/debrief` — debrief the most recent **completed race** (`session_type='race'`,
  `end_utc IS NOT NULL`, `start_utc != end_utc`).
- `/debrief <race-id>` — debrief a specific race by `races.id`.
- `/debrief latest practice` — same shape, but for the most recent practice.

## Inputs

- DB: live on `weaties@corvopi-live:/home/weaties/helmlog/data/logger.db`.
  Read-only access via SSH + sqlite3. Do **not** copy the DB locally —
  query in place to keep the analysis grounded in the latest data.
- Backup DB (when corvopi-live is unreachable): `~/Backups/helmlog/<latest>/data/logger.db`.

## Steps

1. **Resolve the race.** If no ID was given, find the latest race:
   ```sql
   SELECT id, name, date, start_utc, end_utc, vakaros_session_id
   FROM races
   WHERE session_type='race' AND end_utc IS NOT NULL AND start_utc != end_utc
   ORDER BY start_utc DESC LIMIT 1;
   ```
   Confirm with the user before proceeding (`Debriefing race {id}: {name} on {date}. Continue?`).

2. **Run the analyses for this race only.** Use `scripts/analysis/`
   helpers but constrain to the single race window
   (`start_utc → end_utc`, or post-gun if the Vakaros gun event exists).
   Compute:

   - **Finish:** Corvo's place + points from `race_results` joined via
     `local_session_id`. If no place is recorded yet, say so.
   - **Conditions:** mean TWS, TWS range, mean TWA upwind / downwind.
   - **Heel:** mean abs heel (from `attitudes`) — note if not present.
   - **VMG percentages:** % of seconds at ≥90% best-VMG (vs the
     season's polar baseline).
   - **Shifts:** good/bad/neutral tack count from the maneuvers table
     using the 90–150s pre/post window approach in
     `scripts/analysis/full_analysis.py`.
   - **Start:** dist@gun, SOG@gun, TTL, favored end, what end Corvo
     went to, tack at gun. Use the helpers in
     `scripts/analysis/start_quality.py`.
   - **OCS:** if the start analysis flags this race as OCS-returned,
     call it out and apply the `ocs` tag (see /ocs-check).
   - **Crew weight:** lookup default crew (1,070 lb on Corvo as of May
     2026) — note if a per-race override exists in `crew_defaults`.

3. **Pull and analyze the audio transcripts.** Each race has audio in
   one or more `audio_sessions` rows linked by `race_id`:

   - `session_type='race'` — the on-water mic capture (helm + crew chatter)
   - `session_type='debrief'` — post-race recorded debrief (only present
     for some races)

   Both can have multiple sibling rows when more than one mic was
   running (different `capture_group_id` per session_type, ordered by
   `capture_ordinal`). For each audio_session, check `transcripts.status`:

   ```sql
   SELECT a.id, a.session_type, a.capture_ordinal, t.id AS transcript_id,
          t.status, length(t.text) AS text_len
   FROM audio_sessions a
   LEFT JOIN transcripts t ON t.audio_session_id = a.id
   WHERE a.race_id = {race_id}
   ORDER BY a.session_type, a.capture_ordinal;
   ```

   Status handling:

   - **`done`** with non-empty text → fetch `text` and `segments_json`
     (the latter has speaker labels + timestamps) and feed into the
     analysis below.
   - **`running` or `pending`** → note that transcript is in progress,
     skip the analysis section, tell the user to re-run /debrief once
     it completes (typical wait: 1–5 min for a race, 30s–2min for a
     debrief).
   - **No transcript row exists** for an audio_session → kick off
     transcription via the helmlog API:
     ```
     curl -X POST -b ~/.helmlog-cookie \
          https://corvo105.helmlog.org/api/audio/{audio_session_id}/transcribe
     ```
     This returns 202 Accepted and the worker on the user's Mac
     (com.helmlog.transcribe-worker, Tailscale 100.127.219.117:8321)
     picks up the job. **The auth cookie file must exist; if it
     doesn't, prompt the user to log in once and save it.**
   - **`error`** → show the error_msg field, don't auto-retry. Suggest
     the user retranscribes manually if appropriate.

   **What to look for in the transcript:**

   - **Tactical calls vs reactions.** Sailing language is brief —
     "pressure right!", "two boat lengths", "tack", "hold". A good
     race has crisp anticipatory calls; a bad race has lots of
     reactive "watch out!" or "we got rolled" comments.
   - **Specific moments worth flagging back into the analysis:**
     - Mark roundings (counted)
     - Tacks/jibes (count vs the maneuvers table — should match)
     - OCS recall calls ("come back!", "we're over!") — cross-reference
       with the OCS detection
     - Pressure/wind callouts ("big puff coming", "lift on the right")
     - Boat-on-boat ("Moose to leeward", "got rolled by LiftOff")
     - Sail trim ("more vang", "kite collapse", "we're stalled")
   - **Tone trajectory.** Compare debrief audio (post-race calm
     reflection) to race audio (in-the-moment reactions). The debrief
     usually surfaces what went wrong in the team's own words —
     prioritize quoting the crew's own language over your own
     speculation.
   - **Segment-level speaker tracking.** `segments_json` has
     `speaker` labels (or anonymized IDs); per-position quotes are more
     useful than aggregate. If `position_name` is set on
     `transcript_segments`, group by position (helm/main/pit/bow/tac).

   **Add a TRANSCRIPT section to the debrief synthesized in step 4**
   (not as a transcription dump — as a curated 3–6 bullet list of
   notable callouts with timestamps, drawn from the actual transcript
   text). Always cite a specific quote and timestamp.

4. **Synthesize the debrief.** A short markdown summary, ~250–400
   words, with this structure:

   ```markdown
   ## Result
   {place + points + boat conditions in one sentence}

   ## Start
   - Distance from line at gun: {x} m
   - SOG at gun: {y} kt
   - Time to clear line: {z} s {(OCS — returned)}
   - Favored end: {boat/pin/square}, bias {n°}
   - We went to: {boat/mid/pin} — {got favored ✓ / wrong end ✗}
   - Tack at gun: {stbd/port}

   ## Speed (post-gun)
   - Upwind VMG: {p}% of best-ever in similar conditions
   - Downwind VMG: {q}% of best-ever
   - Mean heel: {h}° (or "not recorded")

   ## Shifts
   - Tacked on header: {g}, on lift: {b}, neutral: {n}

   ## From the transcript
   {3-6 bullets of notable callouts with timestamps and speakers,
   drawn from the actual transcript text. Always quote — don't
   paraphrase. Example:
     - 02:14:55 (helm): "we're getting rolled by Moose"
     - 02:18:30 (tac): "pressure on the right, hold this tack"
   If the transcript is in progress: "Transcript still processing
   ({status}); re-run /debrief in a few minutes for transcript notes."
   If no audio captured for this race: "No race audio recorded."}

   ## What we did well / what to look at
   {1-2 bullets each, drawn from BOTH the data AND the transcript —
   be specific, cite numbers, tie to the season patterns where
   relevant (e.g., "we sailed 135° TWA in 7 kt — the season optimum
   is 145°, the same gap we've seen all spring") and to the crew's
   own words from the transcript ("the helm called out 'we're slow'
   at 02:30 — the data shows we were 0.5 kt below polar at that
   moment")}
   ```

6. **Attach to the session.** Insert the debrief as a moment on the
   session via direct SQL on the live DB (the API requires a session
   cookie that's awkward to wire up from the Mac).

   ```sql
   -- Run as: ssh weaties@corvopi-live "sqlite3 /home/weaties/helmlog/data/logger.db <<EOF ... EOF"
   BEGIN;

   -- Create the moment, anchored at the gun (or start_utc + 5min if no gun)
   INSERT INTO moments (
     session_id, subject, anchor_kind, anchor_t_start,
     resolved, source, created_by, created_at, updated_at
   )
   VALUES (
     {race_id},
     'Post-race debrief — {short summary, e.g. "5th, OCS recovery, 65% UP"}',
     'timestamp',
     '{gun_iso}',
     0, 'auto', 1,  -- created_by user 1 = Dan; adjust if needed
     '{now_iso}', '{now_iso}'
   );

   -- Capture the new ID and write the body as a comment
   INSERT INTO comments (moment_id, author, body, created_at)
   VALUES (last_insert_rowid(), 1, '{markdown body}', '{now_iso}');

   COMMIT;
   ```

   The full markdown debrief goes in the **comment body** (rich text
   supported); the moment **subject** stays a one-line summary so the
   moment list is scannable.

7. **Report the URL.** Tell the user the moment was created and give
   them the deep link:
   `https://corvo105.helmlog.org/session/{race_id}/{slug}?moment={moment_id}`
   (or just `/session/{id}/{slug}` if the `?moment=` deep-link param
   doesn't exist on the session detail page).

## Conventions

- The debrief comment should be **honest and specific**, not flattering.
  Cite numbers. Reference the season patterns where they apply.
- Wind angles always shown as **TWA (AWA ~xxx°)** — the crew sees AWA
  on the gauges. AWA derived from BSP at the cell using the formula in
  `scripts/analysis/awa_table.py`.
- Don't speculate about competitor behavior — the data is from one boat
  only. If you suspect an issue (e.g., "got rolled by Moose"), phrase it
  as a question for the crew, not an assertion.
- If `vakaros_session_id` is null OR `vakaros_line_positions` has no
  endpoints for this race, mark the start section as "no line data —
  skip" rather than computing bogus distances.

## DO NOT

- Do not modify any other tables (e.g., don't auto-update settings).
- Do not create more than one debrief moment per race. Check first:
  ```sql
  SELECT id FROM moments WHERE session_id={race_id} AND source='auto'
   AND subject LIKE 'Post-race debrief%' LIMIT 1;
  ```
  If one exists, ask the user before creating another (offer to UPDATE
  the comment body instead).
- Do not run this against practice sessions unless explicitly asked.
- Do not write to the DB if the user hasn't confirmed in step 1.
- Do not paraphrase transcript content. If you quote a crewmember,
  use their exact words and timestamp. If you can't find a clean
  quote that supports a claim, drop the claim instead of inventing
  one.
- Do not include the full transcript text in the debrief — it'll be
  thousands of words. Curate to 3–6 high-signal callouts.
- Do not auto-trigger transcription for sessions with `error` status —
  the user may have a reason (e.g., bad audio).

## Why this skill (and not a script)

- The exact analysis varies — sometimes we want OCS detail, sometimes
  current matters, sometimes shifts. A skill lets us synthesize the
  prose layer.
- The "attach to session" step needs context to write a useful subject
  line, which a script can't do generically.
- Scripts in `scripts/analysis/` do the underlying number-crunching;
  this skill is the orchestrator + writer.
