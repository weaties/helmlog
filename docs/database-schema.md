# J105 Logger — Database Schema (v22)

SQLite database storing time-series sailing instrument data, race sessions,
audio recordings, transcripts, video links, weather/tide data, and user auth.

All timestamps are **UTC ISO 8601 strings** (`TEXT`). The schema is versioned
with simple integer migrations in `src/logger/storage.py`.

---

## Entity-Relationship Overview

```
users ──< auth_sessions
  │
  ├──< invite_tokens (created_by)
  ├──< audit_log
  ├──< session_notes (user_id)
  └──< race_videos (user_id)

races ──< race_crew
  │   ──< race_results ──> boats
  │   ──< race_sails ──> sails
  │   ──< race_videos
  │   ──< session_notes
  │   ──< audio_sessions
  │   ──< session_tags ──> tags
  │   ──< camera_sessions ──> cameras
  │
daily_events (one per date)

session_notes ──< note_tags ──> tags

audio_sessions ──< transcripts

-- Standalone time-series (no FKs, queried by timestamp range):
headings, speeds, depths, positions, cogsog, winds, environmental,
weather, tides, video_sessions, polar_baseline
```

---

## Tables

### Instrument Data (time-series)

These tables are append-only, high-volume. Each has a `ts` index for range queries.
`source_addr` is the NMEA 2000 source address of the transmitting device.

#### `headings`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| source_addr | INTEGER | NOT NULL | NMEA 2000 source address |
| heading_deg | REAL | NOT NULL | Vessel heading in degrees |
| deviation_deg | REAL | | Compass deviation |
| variation_deg | REAL | | Magnetic variation |

Index: `idx_headings_ts` on `(ts)`

#### `speeds`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| source_addr | INTEGER | NOT NULL | NMEA 2000 source address |
| speed_kts | REAL | NOT NULL | Speed through water in knots |

Index: `idx_speeds_ts` on `(ts)`

#### `depths`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| source_addr | INTEGER | NOT NULL | NMEA 2000 source address |
| depth_m | REAL | NOT NULL | Water depth in meters |
| offset_m | REAL | | Transducer offset |

Index: `idx_depths_ts` on `(ts)`

#### `positions`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| source_addr | INTEGER | NOT NULL | NMEA 2000 source address |
| latitude_deg | REAL | NOT NULL | Latitude in decimal degrees |
| longitude_deg | REAL | NOT NULL | Longitude in decimal degrees |

Index: `idx_positions_ts` on `(ts)`

#### `cogsog`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| source_addr | INTEGER | NOT NULL | NMEA 2000 source address |
| cog_deg | REAL | NOT NULL | Course over ground in degrees |
| sog_kts | REAL | NOT NULL | Speed over ground in knots |

Index: `idx_cogsog_ts` on `(ts)`

#### `winds`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| source_addr | INTEGER | NOT NULL | NMEA 2000 source address |
| wind_speed_kts | REAL | NOT NULL | Wind speed in knots |
| wind_angle_deg | REAL | NOT NULL | Wind angle in degrees |
| reference | INTEGER | NOT NULL | NMEA 2000 wind reference (0=true north, 1=magnetic, 2=apparent, 3=true boat, 4=true water) |

Index: `idx_winds_ts` on `(ts)`

#### `environmental`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| source_addr | INTEGER | NOT NULL | NMEA 2000 source address |
| water_temp_c | REAL | NOT NULL | Water temperature in Celsius |

Index: `idx_environmental_ts` on `(ts)`

---

### External Data

#### `weather`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| lat | REAL | NOT NULL | Latitude of observation |
| lon | REAL | NOT NULL | Longitude of observation |
| wind_speed_kts | REAL | NOT NULL | Wind speed in knots |
| wind_dir_deg | REAL | NOT NULL | Wind direction in degrees |
| air_temp_c | REAL | NOT NULL | Air temperature in Celsius |
| pressure_hpa | REAL | NOT NULL | Barometric pressure in hPa |

Index: `idx_weather_ts` on `(ts)`

#### `tides`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| station_id | TEXT | NOT NULL | NOAA CO-OPS station ID |
| station_name | TEXT | NOT NULL | Station display name |
| height_m | REAL | NOT NULL | Tide height in meters |
| type | TEXT | NOT NULL | Tide type (e.g. "H", "L") |

Unique: `(ts, station_id)`
Index: `idx_tides_ts` on `(ts)`

---

### Race & Session Management

#### `races`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| name | TEXT | NOT NULL, UNIQUE | Auto-generated name (e.g. "YRA-1") |
| event | TEXT | NOT NULL | Event name |
| race_num | INTEGER | NOT NULL | Race number within event |
| date | TEXT | NOT NULL | Date string (YYYY-MM-DD) |
| start_utc | TEXT | NOT NULL | Race start UTC ISO 8601 |
| end_utc | TEXT | | Race end UTC ISO 8601 |
| session_type | TEXT | NOT NULL, DEFAULT 'race' | "race", "practice", etc. |

Indexes: `idx_races_date` on `(date)`, `idx_races_start_utc` on `(start_utc)`

#### `daily_events`
| Column | Type | Constraints | Description |
|---|---|---|---|
| date | TEXT | PK | YYYY-MM-DD |
| event_name | TEXT | NOT NULL | Event name for the day |

#### `race_crew`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| race_id | INTEGER | NOT NULL, FK → races(id) ON DELETE CASCADE | |
| position | TEXT | NOT NULL | Crew position (helm, main, pit, bow, tactician, guest) |
| sailor | TEXT | NOT NULL | Sailor name |

Unique: `(race_id, position)`
Index: `idx_race_crew_race_id` on `(race_id)`

#### `recent_sailors`
| Column | Type | Constraints | Description |
|---|---|---|---|
| sailor | TEXT | PK | Sailor name |
| last_used | TEXT | NOT NULL | Last time this sailor was assigned to a race |

#### `boats`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| sail_number | TEXT | UNIQUE, NOT NULL | Sail/hull number |
| name | TEXT | | Boat name |
| class | TEXT | | Boat class (e.g. "J/105") |
| last_used | TEXT | | Last race appearance |

#### `race_results`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| race_id | INTEGER | NOT NULL, FK → races(id) ON DELETE CASCADE | |
| place | INTEGER | NOT NULL | Finishing position |
| boat_id | INTEGER | NOT NULL, FK → boats(id) | |
| finish_time | TEXT | | Finish time |
| dnf | INTEGER | NOT NULL, DEFAULT 0 | Did not finish flag |
| dns | INTEGER | NOT NULL, DEFAULT 0 | Did not start flag |
| notes | TEXT | | |
| created_at | TEXT | NOT NULL | |

Unique: `(race_id, place)`, `(race_id, boat_id)`
Index: `idx_race_results_race_id` on `(race_id)`

#### `sails`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| type | TEXT | NOT NULL | Sail type: "main", "jib", "spinnaker" |
| name | TEXT | NOT NULL | Sail name/identifier |
| notes | TEXT | | |
| active | INTEGER | NOT NULL, DEFAULT 1 | Soft-delete flag |

Unique: `(type, name)`

#### `race_sails`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| race_id | INTEGER | NOT NULL, FK → races(id) ON DELETE CASCADE | |
| sail_id | INTEGER | NOT NULL, FK → sails(id) | |

Unique: `(race_id, sail_id)`
Index: `idx_race_sails_race_id` on `(race_id)`

---

### Audio & Transcription

#### `audio_sessions`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| file_path | TEXT | NOT NULL | Path to WAV file |
| device_name | TEXT | NOT NULL | Audio input device name |
| start_utc | TEXT | NOT NULL | Recording start UTC |
| end_utc | TEXT | | Recording end UTC |
| sample_rate | INTEGER | NOT NULL | Sample rate in Hz |
| channels | INTEGER | NOT NULL | Channel count |
| race_id | INTEGER | FK → races(id) | Associated race (nullable) |
| session_type | TEXT | NOT NULL, DEFAULT 'race' | |
| name | TEXT | | Optional display name |

Index: `idx_audio_sessions_start_utc` on `(start_utc)`

#### `transcripts`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| audio_session_id | INTEGER | NOT NULL, FK → audio_sessions(id) ON DELETE CASCADE, UNIQUE | One transcript per audio session |
| status | TEXT | NOT NULL, DEFAULT 'pending' | "pending", "processing", "done", "error" |
| text | TEXT | | Full transcript text |
| error_msg | TEXT | | Error message if failed |
| model | TEXT | | Whisper model used |
| created_utc | TEXT | NOT NULL | |
| updated_utc | TEXT | NOT NULL | |
| segments_json | TEXT | | JSON array of timed segments |

Index: `idx_transcripts_audio_session_id` on `(audio_session_id)`

---

### Notes, Videos & Tags

#### `session_notes`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| race_id | INTEGER | FK → races(id) ON DELETE CASCADE | |
| audio_session_id | INTEGER | FK → audio_sessions(id) ON DELETE CASCADE | |
| ts | TEXT | NOT NULL | Note timestamp UTC |
| note_type | TEXT | NOT NULL, DEFAULT 'text' | "text" or "photo" |
| body | TEXT | | Note text content |
| photo_path | TEXT | | Path to photo file |
| created_at | TEXT | NOT NULL | |
| user_id | INTEGER | FK → users(id) | Author (added in v17) |

Indexes: `idx_session_notes_race_id` on `(race_id)`, `idx_session_notes_ts` on `(ts)`

#### `race_videos`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| race_id | INTEGER | NOT NULL, FK → races(id) ON DELETE CASCADE | |
| youtube_url | TEXT | NOT NULL | Full YouTube URL |
| video_id | TEXT | NOT NULL | YouTube video ID |
| label | TEXT | NOT NULL, DEFAULT '' | User label |
| sync_utc | TEXT | NOT NULL | UTC time to sync video to race data |
| sync_offset_s | REAL | NOT NULL, DEFAULT 0 | Video offset in seconds at sync point |
| duration_s | REAL | | Video duration |
| title | TEXT | NOT NULL, DEFAULT '' | YouTube title |
| created_at | TEXT | NOT NULL | |
| user_id | INTEGER | FK → users(id) | Who linked it (added in v17) |

Index: `idx_race_videos_race_id` on `(race_id)`

#### `video_sessions` (legacy, pre-race-video linking)
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| url | TEXT | NOT NULL | |
| video_id | TEXT | NOT NULL | |
| title | TEXT | NOT NULL | |
| duration_s | REAL | NOT NULL | |
| sync_utc | TEXT | NOT NULL | |
| sync_offset_s | REAL | NOT NULL | |
| created_at | TEXT | NOT NULL | |

Index: `idx_video_sessions_sync_utc` on `(sync_utc)`

#### `tags`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| name | TEXT | NOT NULL, UNIQUE | Tag label |
| color | TEXT | | CSS color string |
| created_at | TEXT | NOT NULL | |

#### `session_tags` (junction: races ↔ tags)
| Column | Type | Constraints | Description |
|---|---|---|---|
| session_id | INTEGER | NOT NULL, FK → races(id) ON DELETE CASCADE | |
| tag_id | INTEGER | NOT NULL, FK → tags(id) ON DELETE CASCADE | |

PK: `(session_id, tag_id)`

#### `note_tags` (junction: session_notes ↔ tags)
| Column | Type | Constraints | Description |
|---|---|---|---|
| note_id | INTEGER | NOT NULL, FK → session_notes(id) ON DELETE CASCADE | |
| tag_id | INTEGER | NOT NULL, FK → tags(id) ON DELETE CASCADE | |

PK: `(note_id, tag_id)`

---

### Performance Analysis

#### `polar_baseline`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| tws_bin | INTEGER | NOT NULL | True wind speed bin (knots) |
| twa_bin | INTEGER | NOT NULL | True wind angle bin (degrees) |
| mean_bsp | REAL | NOT NULL | Mean boat speed for this bin |
| p90_bsp | REAL | NOT NULL | 90th percentile boat speed |
| session_count | INTEGER | NOT NULL | Number of sessions contributing |
| sample_count | INTEGER | NOT NULL | Number of data points |
| built_at | TEXT | NOT NULL | When baseline was computed |

Unique: `(tws_bin, twa_bin)`
Index: `idx_polar_tws_twa` on `(tws_bin, twa_bin)`

---

### Auth & Audit

#### `users`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| email | TEXT | UNIQUE, NOT NULL | Login email |
| name | TEXT | | Display name |
| role | TEXT | NOT NULL, DEFAULT 'viewer' | "admin", "crew", or "viewer" |
| created_at | TEXT | NOT NULL | |
| last_seen | TEXT | | Last activity timestamp |
| avatar_path | TEXT | | Path to avatar image (added in v19) |

#### `invite_tokens`
| Column | Type | Constraints | Description |
|---|---|---|---|
| token | TEXT | PK | Random token string |
| email | TEXT | NOT NULL | Invited email |
| role | TEXT | NOT NULL | Role to assign on acceptance |
| created_by | INTEGER | FK → users(id) | Admin who created invite |
| expires_at | TEXT | NOT NULL | Expiry UTC |
| used_at | TEXT | | When accepted |

#### `auth_sessions`
| Column | Type | Constraints | Description |
|---|---|---|---|
| session_id | TEXT | PK | Random session token |
| user_id | INTEGER | NOT NULL, FK → users(id) ON DELETE CASCADE | |
| created_at | TEXT | NOT NULL | |
| expires_at | TEXT | NOT NULL | |
| ip | TEXT | | Client IP |
| user_agent | TEXT | | Client user-agent |

Index: `idx_auth_sessions_user_id` on `(user_id)`

#### `audit_log`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| ts | TEXT | NOT NULL | UTC ISO 8601 |
| user_id | INTEGER | FK → users(id) | Actor (nullable for system events) |
| action | TEXT | NOT NULL | Action name |
| detail | TEXT | | JSON or free-text detail |
| ip_address | TEXT | | Client IP |
| user_agent | TEXT | | Client user-agent |

Indexes: `idx_audit_log_ts` on `(ts)`, `idx_audit_log_action` on `(action)`

---

### Camera Control

#### `cameras`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| name | TEXT | NOT NULL, UNIQUE | Human-readable camera name |
| ip | TEXT | NOT NULL | Camera IP address |
| model | TEXT | NOT NULL, DEFAULT 'insta360-x4' | Camera model |
| wifi_ssid | TEXT | | Camera WiFi AP SSID |
| wifi_password | TEXT | | Camera WiFi AP password |

#### `camera_sessions`
| Column | Type | Constraints | Description |
|---|---|---|---|
| id | INTEGER | PK AUTOINCREMENT | |
| session_id | INTEGER | NOT NULL, FK → races(id) | Associated race |
| camera_name | TEXT | NOT NULL | Camera name at time of recording |
| camera_ip | TEXT | NOT NULL | Camera IP at time of recording |
| recording_started_utc | TEXT | | When recording started |
| recording_stopped_utc | TEXT | | When recording stopped |
| sync_offset_ms | INTEGER | | Round-trip latency of start command |
| error | TEXT | | Error message if start/stop failed |

Index: `idx_camera_sessions_session` on `(session_id)`

---

### Internal

#### `schema_version`
| Column | Type | Constraints | Description |
|---|---|---|---|
| version | INTEGER | PK | Current schema version (22) |

---

## Notes for Review

- **No WAL mode pragma** is set explicitly; SQLite default journal mode is used.
- **Foreign keys**: SQLite requires `PRAGMA foreign_keys = ON` per connection for FK enforcement — verify this is enabled in the application code.
- **Timestamps as TEXT**: All timestamps are ISO 8601 strings rather than INTEGER (Unix epoch). This is idiomatic for SQLite but means range queries rely on lexicographic ordering of the ISO format.
- **`dnf`/`dns` as INTEGER**: Used as boolean flags (0/1) since SQLite has no native boolean type.
- **Soft-delete pattern**: `sails.active` is a soft-delete flag; no other tables use this pattern.
- **`video_sessions` vs `race_videos`**: `video_sessions` is the older standalone table (v3); `race_videos` (v13) links videos to specific races. Both exist in the schema.
- **Write pattern**: The application buffers instrument rows in memory and flushes to SQLite every 1 second or 200 records (whichever comes first), inside a single transaction.
- **No explicit `ON DELETE` on some FKs**: `race_results.boat_id`, `race_sails.sail_id`, `invite_tokens.created_by`, and `session_notes`/`race_videos` `.user_id` lack `ON DELETE` clauses — deleting referenced rows would fail with FK errors (if FKs are enforced).
