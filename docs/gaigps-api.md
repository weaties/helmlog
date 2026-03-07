# Gaia GPS API — Reverse-Engineered Reference

Captured 2026-03-06 via Chrome DevTools HAR export. Gaia GPS (gaiagps.com)
is owned by Outside Inc. Auth uses their OIDC provider.

## Authentication

Login uses OAuth2 / OIDC via `accounts.outsideonline.com`:

1. Browser redirects to `https://accounts.outsideonline.com/oidc/o/authorize/?client_id=oRQVq3GBFm3SHnDPfXbSQ4sL1Yst81gPlw5rBWF6&response_type=code&...`
2. After login, callback to `https://www.gaiagps.com/login/callback/?code=...&state=...`
3. Sets two cookies on `.gaiagps.com`:
   - `sessionid` — Django session cookie (14-day expiry)
   - `csrftoken` — CSRF token (1-year expiry)

All API requests require the `sessionid` cookie. Mutating requests require
`X-CSRFToken` header matching the `csrftoken` cookie.

## Endpoints

### List tracks

```
GET /api/objects/track/?sort_direction=desc&sort_field=create_date&show_archived=false&show_filed=true&page=1&count=1000
Cookie: sessionid=...; csrftoken=...
```

Returns a JSON array of track summaries:

```json
{
  "id": "95041af8fe3308f2750870217e7dd1ac",
  "updated_date": "2025-10-05T18:29:34Z",
  "time_created": "2025-10-05T15:22:40Z",
  "title": "Tiger mountain with Greg",
  "distance": 9216.99,
  "total_ascent": 595.58,
  "total_time": 10852.0,
  "activities": [],
  "source": "Gaia GPS for iPhone",
  "folder": "cf190f880ddfe617d7ec7877311ce2f2",
  "folder_name": "parks visited"
}
```

- `distance` is in **metres**
- `total_time` is in **seconds**
- Pagination: `page=1&count=1000`. Response is a flat array (no wrapper).
  Keep fetching pages until response length < count.

### Track detail

```
GET /api/v3/tracks/{track_id}/
Cookie: sessionid=...; csrftoken=...
```

Returns full track with geometry and stats:

```json
{
  "id": "7d1bd70eeb99ade0641acba897d32f8f",
  "name": "Wednesday Evening Boating",
  "create_date": "2025-09-04T01:17:32",
  "source": "iPhone17,2",
  "geometry": {
    "type": "MultiLineString",
    "coordinates": [
      [
        [-122.417148, 47.688097, 0.5, 1756948653.0],
        [-122.417361, 47.688074, 0.2, 1756948659.0]
      ]
    ]
  },
  "stats": {
    "ascent": 0.0,
    "average_speed": 2.478,
    "descent": 0.0,
    "distance": 6540.8,
    "max_speed": 3.263,
    "moving_speed": 2.478,
    "moving_time": 2639,
    "stopped_time": 0,
    "total_time": 2639
  }
}
```

**Geometry format:** `MultiLineString` with coordinates `[lon, lat, elevation_m, unix_timestamp]`

- `coordinates[0]` = first (usually only) line segment
- Each point: `[longitude, latitude, elevation_metres, unix_epoch_seconds]`
- Speeds in stats are **m/s**
- Distances in stats are **metres**
- Times in stats are **seconds**

## Conversion notes

- SOG: not directly in the track, but can be computed from consecutive
  point timestamps and positions (haversine)
- COG: computed from consecutive lat/lon pairs
- `create_date` appears to be the track start time (matches first point timestamp)
- Elevation values can be noisy (includes -19999.0 sentinel for invalid readings)

## Rate limiting

No rate limiting was observed during testing. Be polite: 1-second delay
between track detail requests.
