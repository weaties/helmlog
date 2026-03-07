# Remote Transcription Offload

How to offload audio transcription from the Raspberry Pi to a faster machine
(e.g., a Mac) over Tailscale.

---

## Why offload?

Whisper transcription on the Pi is slow — a 30-minute race audio can take
10+ minutes to process on a Raspberry Pi 4.  Offloading to a Mac with an
M-series chip brings that down to under a minute.

The Pi sends the WAV file over HTTP to a lightweight worker process on the
Mac, which runs faster-whisper (and optional speaker diarisation) and returns
the result.  If the Mac is unreachable, the Pi falls back to local
transcription automatically.

```
┌─────────────┐   POST /transcribe   ┌─────────────┐
│  Raspberry   │  ──── Tailscale ───► │  Mac         │
│  Pi (corvopi)│   (WAV upload)       │  (worker)    │
│              │  ◄──── JSON ──────── │              │
│  TRANSCRIBE_ │   (text + segments)  │  port 8321   │
│  URL set     │                      │              │
└─────────────┘                       └─────────────┘
```

---

## Prerequisites

1. **Both machines on the same Tailscale network** — the Pi and Mac must be
   able to reach each other by Tailscale IP or hostname.
2. **The Mac has the project cloned** with dependencies installed:
   ```bash
   git clone https://github.com/weaties/helmlog.git
   cd helmlog
   uv sync
   ```
3. **For speaker diarisation** (optional): a Hugging Face token with accepted
   model licences for `pyannote/speaker-diarization-3.1`.  Set `HF_TOKEN` on
   the Mac.

---

## Setup: Mac (worker)

### 1. Start the worker

From the project root:

```bash
uv run python scripts/transcribe_worker.py
```

Or bind to the Tailscale interface only (more secure):

```bash
uv run python scripts/transcribe_worker.py --host 100.x.x.x --port 8321
```

Replace `100.x.x.x` with the Mac's Tailscale IP (`tailscale ip -4`).

### 2. Verify it's running

```bash
curl http://localhost:8321/healthz
# → {"status":"ok"}
```

### 3. Optional: enable speaker diarisation

```bash
export HF_TOKEN=hf_your_token_here
uv run python scripts/transcribe_worker.py
```

The worker uses the same `_run_with_diarization` and `_run_whisper` functions
from the main project, so diarisation quality is identical to the local path.

### 4. Optional: run as a background service

To keep the worker running across reboots on macOS:

```bash
# Simple approach — run in a tmux session:
tmux new -s transcribe
uv run python scripts/transcribe_worker.py
# Ctrl-B D to detach

# Or use a launchd plist for persistent service (see macOS docs)
```

---

## Setup: Pi

### 1. Set TRANSCRIBE_URL

Add to `.env` on the Pi:

```bash
# Use the Mac's Tailscale hostname or IP
TRANSCRIBE_URL=http://macbook:8321
```

Or by Tailscale IP:

```bash
TRANSCRIBE_URL=http://100.x.x.x:8321
```

### 2. Restart the logger

```bash
sudo systemctl restart helmlog
```

### 3. Verify connectivity

From the Pi, confirm it can reach the worker:

```bash
curl http://macbook:8321/healthz
# → {"status":"ok"}
```

---

## How it works

When a user triggers transcription from the web UI:

1. The Pi checks if `TRANSCRIBE_URL` is set.
2. If set, it POSTs the WAV file to `{TRANSCRIBE_URL}/transcribe` with
   query parameters `model_size` and `diarize`.
3. The worker runs faster-whisper (and optionally pyannote diarisation)
   and returns `{"text": "...", "segments": [...]}`.
4. The Pi stores the result in SQLite just like a local transcription.
5. **If the remote fails** (network error, timeout, worker down), the Pi
   falls back to local transcription automatically with a warning logged.

The timeout is 10 minutes (`_REMOTE_TIMEOUT_S = 600`) to handle long
recordings over slower networks.

---

## Worker API

### `POST /transcribe`

Upload a WAV file for transcription.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file` | form upload | required | WAV audio file |
| `model_size` | query string | `base` | Whisper model: `tiny`, `base`, `small`, `medium`, `large` |
| `diarize` | query string | `true` | Enable speaker diarisation (`true`/`false`) |

**Response** (200):
```json
{
  "text": "Full transcription text...",
  "segments": [
    {"start": 0.0, "end": 4.5, "speaker": "SPEAKER_00", "text": "Tack in three..."},
    {"start": 4.5, "end": 8.2, "speaker": "SPEAKER_01", "text": "Ready on the jib."}
  ]
}
```

Without diarisation, segments contain a single entry with the full text.

### `GET /healthz`

Liveness check.  Returns `{"status": "ok"}`.

---

## Choosing a model

| Model | Size | Speed (Mac M1) | Speed (Pi 4) | Quality |
|-------|------|-----------------|--------------|---------|
| `tiny` | 39 MB | ~5s/min | ~30s/min | Basic |
| `base` | 74 MB | ~10s/min | ~60s/min | Good (default) |
| `small` | 244 MB | ~20s/min | ~3min/min | Better |
| `medium` | 769 MB | ~40s/min | impractical | Great |
| `large` | 1.5 GB | ~60s/min | impractical | Best |

Set the model on the Pi via `WHISPER_MODEL` in `.env`.  The same model name
is sent to the remote worker.  On the Mac, `medium` or `large` are practical
choices.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Transcription still slow (running locally) | Check `TRANSCRIBE_URL` is set and the worker is reachable: `curl $TRANSCRIBE_URL/healthz` |
| "Remote transcribe failed (falling back to local)" in logs | Worker may be down or unreachable. Check Tailscale connectivity and worker logs. |
| Diarisation not working on remote | Ensure `HF_TOKEN` is set on the **Mac** (not the Pi). The worker runs diarisation locally. |
| Timeout errors | Long recordings may exceed the 10-minute timeout. Consider using a smaller model or splitting the audio. |
| Worker crashes on large files | Ensure the Mac has enough RAM. `large` model needs ~4 GB free. |
| "No transcript job found" in web UI | The transcription job may still be in progress. Check worker logs. |
