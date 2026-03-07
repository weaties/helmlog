# Audio Recording Setup — Gordik 2T1R Wireless Lavalier

## Overview

The helmlog can record voice commentary (crew calls, tactical notes) in sync with instrument data. Audio is captured from a USB audio input device and saved as a WAV file per session, anchored to UTC time for later correlation with instrument logs and video.

---

## Hardware

**Gordik 2T1R** (or any USB Audio Class receiver):

1. Plug the USB receiver into one of the Pi's USB ports.
2. The receiver appears as a standard UAC device — no drivers needed on Linux.

---

## System Dependencies

Install PortAudio and libsndfile if not already present:

```bash
sudo apt install libportaudio2 libsndfile1
```

---

## Finding the Device Name / Index

Run:

```bash
helmlog list-devices
```

Example output:

```
Idx  Name                                      Ch    Default rate
-----------------------------------------------------------------
  0  Built-in Microphone                        2           44100
  1  Gordik 2T1R USB Audio                      1           48000
```

Note the **Name** or **Idx** for the next step.

---

## Configuration

Add to your `.env` file (or export as environment variables):

```env
# Name substring (case-insensitive) — matches any device whose name contains "Gordik"
AUDIO_DEVICE=Gordik

# Or use the integer index from list-devices
# AUDIO_DEVICE=1

# Where WAV files are saved (default: data/audio)
AUDIO_DIR=data/audio

# Sample rate in Hz (default: 48000)
AUDIO_SAMPLE_RATE=48000

# Number of channels: 1=mono, 2=stereo (default: 1)
AUDIO_CHANNELS=1
```

If `AUDIO_DEVICE` is not set, the first available input device is used automatically.

---

## Running

Audio recording starts automatically with the logger:

```bash
helmlog run
```

Look for this log line on startup:

```
Audio recording started: data/audio/audio_20250810_140530.wav
```

On Ctrl-C or SIGTERM:

```
Audio recording saved: data/audio/audio_20250810_140530.wav
```

---

## Listing Recorded Sessions

```bash
helmlog list-audio
```

Example output:

```
File                                          Duration  Start UTC
--------------------------------------------------------------------------------
data/audio/audio_20250810_140530.wav             1:23:45  2025-08-10T14:05:30+00:00
```

---

## WAV File Naming

Files are named using the UTC timestamp at the moment recording began:

```
audio/audio_YYYYMMDD_HHMMSS.wav
```

Example: `audio_20250810_140530.wav` → recording started 2025-08-10 at 14:05:30 UTC.

---

## Verifying Playback

```bash
aplay data/audio/audio_*.wav
```

Or open the file in any audio editor (Audacity, etc.).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No audio input device found` in logs | Check `helmlog list-devices`; verify USB receiver is plugged in |
| Wrong device selected | Set `AUDIO_DEVICE=<name or index>` in `.env` |
| Distorted or noisy audio | Check `AUDIO_SAMPLE_RATE` matches receiver's default rate |
| libportaudio errors on startup | `sudo apt install libportaudio2` |
