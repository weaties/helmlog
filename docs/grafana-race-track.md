# Grafana — Race Track Panel

The **Race Track** panel in the `Sailing Data` dashboard shows the GPS track for
the current time window as a coloured route on a map.

---

## What it shows

| Element | Source |
|---|---|
| Track line | `navigation.position.latitude/longitude` — aggregated to 5 s |
| Line colour | Boatspeed through water (`navigation.speedThroughWater`) |
| Tooltip | BSP (kts), TWS (kts), TWA (°), TWD (°) at each point |
| Click action | Opens the linked YouTube video at that exact timestamp |

### Speed colour scale

| Colour | Speed |
|---|---|
| Blue | < 4 kts |
| Green | 4–6 kts |
| Yellow | 6–8 kts |
| Red | > 8 kts |

---

## Data source

All data comes from InfluxDB (`signalk` bucket) via the Signal K →
`signalk-to-influxdb2` plugin. No additional j105-logger endpoints are used.

The Flux query joins six Signal K paths using `pivot`:

| Signal K path | Field | Conversion |
|---|---|---|
| `navigation.position.latitude` | `latitude` | — |
| `navigation.position.longitude` | `longitude` | — |
| `navigation.speedThroughWater` | `BSP (kts)` | ×1.94384 (m/s → kts) |
| `environment.wind.speedTrue` | `TWS (kts)` | ×1.94384 (m/s → kts) |
| `environment.wind.angleTrueWater` | `TWA (°)` | ×57.29578 (rad → °) |
| `environment.wind.directionTrue` | `TWD (°)` | ×57.29578 (rad → °) |

Data is aggregated to 5-second windows (`fn: last`) before pivoting.
Rows without a GPS position are filtered out.

---

## YouTube deep-link

Clicking a track point opens the linked video at that moment using the existing
`/api/videos/redirect?at=<ISO 8601>` endpoint (same as the time-series panels).
The link is only functional when a video has been linked to the race via the
History page or `j105-logger link-video`.

---

## Grafana requirements

- **Panel type**: Geomap (built-in since Grafana 9 — no plugin required)
- **Datasource**: InfluxDB 2.x with Flux query language
- **Grafana version**: 12.4.0+ (tested); should work on 10+

---

## Deploying the updated dashboard

The dashboard JSON is provisioned automatically by `scripts/provision-grafana.sh`.
After a deploy, re-run the provision script on the Pi:

```bash
ssh <pi-user>@<pi-host>
cd ~/j105-logger
./scripts/provision-grafana.sh
```

Or restart Grafana to pick up the new JSON from the provisioning directory:

```bash
sudo systemctl restart grafana-server
```

---

## Troubleshooting

**Track is empty / no data**
- Check the time range includes a session where GPS was active.
- Verify `navigation.position.latitude` exists in InfluxDB:
  ```
  influx query 'from(bucket:"signalk") |> range(start:-1h) |> filter(fn:(r) => r._measurement == "navigation.position.latitude") |> limit(n:5)'
  ```
- If the query returns nothing, the GPS source (Signal K path `navigation.position`)
  may not be enabled in the `signalk-to-influxdb2` plugin — check its filter
  settings in the Signal K admin panel.

**Track shows but has no colour**
- Speed data may not be present in the selected time range.
- The route will render in the default blue if `BSP (kts)` is null for all points.

**Map tiles don't load**
- The Pi needs internet access to fetch OpenStreetMap tiles.
- Grafana caches tiles; the map still renders the route offline after initial load.
