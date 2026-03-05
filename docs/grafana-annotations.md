# Grafana Annotations — Race & Practice Notes

Notes added during a race or practice session (text, settings, photo) can be
overlaid as **annotations** on any Grafana time-series panel.  When you hover
near a note's timestamp you see the text (and a photo thumbnail for photo
notes) without leaving the dashboard.

---

## How it works

The logger exposes a SimpleJSON-compatible annotation endpoint:

```
GET /api/grafana/annotations?from=<unix_ms>&to=<unix_ms>[&sessionId=<id>]
```

| Parameter   | Type        | Description |
|-------------|-------------|-------------|
| `from`      | integer     | Window start — Unix epoch **milliseconds** (Grafana's default format) |
| `to`        | integer     | Window end — Unix epoch milliseconds |
| `sessionId` | integer     | *(optional)* Scope to a single race or practice session |

### Response format

```json
[
  {
    "time":    1709123456000,
    "timeEnd": 1709123456000,
    "title":   "Text",
    "text":    "Tack at layline — timing was off",
    "tags":    ["text"]
  },
  {
    "time":    1709123890000,
    "timeEnd": 1709123890000,
    "title":   "Photo",
    "text":    "Boat handling drill<br/><img src=\"/notes/42/photo.jpg\" style=\"max-width:300px\"/>",
    "tags":    ["photo"]
  }
]
```

`title` is the note type capitalised (`Text`, `Settings`, `Photo`).  
Photo notes embed an `<img>` tag so Grafana's tooltip renders the thumbnail.

---

## Grafana setup

### 1 — Add a JSON datasource

1. **Connections → Add new connection → JSON API** (install the [Grafana JSON API plugin](https://grafana.com/grafana/plugins/marcusolsson-json-datasource/) if not already present, or use **SimpleJSON**).
2. URL: `http://<pi-host>:3002`
3. Save & test.

### 2 — Add an annotation query to your dashboard

1. Open your dashboard → **Dashboard settings** (⚙) → **Annotations** → **Add annotation query**.
2. Fill in:
   | Field | Value |
   |-------|-------|
   | Name | `Race Notes` |
   | Data source | *(your JSON datasource from step 1)* |
   | Query type | `Annotations` |
   | URL path | `/api/grafana/annotations` |
   | Time field | `time` |
   | Text field | `text` |
   | Title field | `title` |
   | Tags field | `tags` |
3. *(Optional)* To scope to a single session add a **Query param**: key `sessionId`, value `<race-id>`.

Annotations now appear as vertical markers on every time-series panel in the
dashboard.  Hovering a marker shows the note text (and photo thumbnail).

### 3 — Filter by session (optional)

Use a Grafana **variable** to make the `sessionId` dynamic:

```
Variable name:  sessionId
Variable type:  Text box
Default value:  (leave blank for all sessions)
```

Then set the annotation query param to `${sessionId}` — when blank, all notes
in the visible time range are shown; when filled with a race ID only that
race's notes appear.

---

## Curl examples

```bash
# All notes for the current week
FROM=$(date -d "last monday" +%s)000
TO=$(date +%s)000
curl "http://<pi-hostname>:3002/api/grafana/annotations?from=$FROM&to=$TO"

# Notes for session 42 only
curl "http://<pi-hostname>:3002/api/grafana/annotations?from=$FROM&to=$TO&sessionId=42"
```
