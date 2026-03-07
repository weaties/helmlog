"""Audio transcription via faster-whisper with optional speaker diarisation.

Transcription runs in a thread pool to avoid blocking the event loop.
Results (including errors) are stored in the ``transcripts`` SQLite table.

**Remote offload** — when ``TRANSCRIBE_URL`` is set (e.g.
``http://macbook:8321``), the WAV file is POSTed to a remote worker over
Tailscale.  The worker runs whisper + diarisation on faster hardware and
returns JSON.  If the remote is unreachable the local path runs as fallback.

Speaker diarisation is performed via pyannote.audio when ALL of the following
are true:
  1. ``HF_TOKEN`` is set in the environment (requires accepted model licences
     on huggingface.co for pyannote/speaker-diarization-3.1 and dependencies).
  2. The ``pyannote.audio`` and ``torch`` packages are importable.

Audio is pre-loaded via soundfile and converted to a torch tensor to bypass
pyannote's built-in audio decoder which depends on torchcodec (unavailable
on aarch64).

Without diarisation the plain-text faster-whisper path is used.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Remote transcription offload
# ---------------------------------------------------------------------------

_REMOTE_TIMEOUT_S = 600  # 10 minutes — long recordings on slow networks


async def _try_remote_transcribe(
    file_path: str,
    model_size: str,
    diarize: bool,
    *,
    transcribe_url: str = "",
) -> tuple[str, list[dict[str, object]]] | None:
    """POST the WAV file to a remote worker and return (text, segments).

    *transcribe_url* is the base URL of the worker (e.g. ``http://mac:8321``).
    Falls back to ``TRANSCRIBE_URL`` env var if not provided.

    Returns *None* when no URL is configured or the remote is unreachable,
    signalling the caller to fall back to local processing.
    """
    url = (transcribe_url or os.environ.get("TRANSCRIBE_URL", "")).rstrip("/")
    if not url:
        return None

    import httpx

    endpoint = f"{url}/transcribe"
    logger.info("Remote transcribe: uploading {} to {}", file_path, endpoint)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_REMOTE_TIMEOUT_S)) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    endpoint,
                    files={"file": ("audio.wav", f, "audio/wav")},
                    params={"model_size": model_size, "diarize": str(diarize).lower()},
                )
            resp.raise_for_status()
        data = resp.json()
        text: str = data["text"]
        segments: list[dict[str, object]] = data.get("segments") or []
        logger.info("Remote transcribe succeeded: {} chars, {} segments", len(text), len(segments))
        return text, segments
    except Exception as exc:  # noqa: BLE001
        logger.warning("Remote transcribe failed (falling back to local): {}", exc)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def transcribe_session(
    storage: Storage,
    audio_session_id: int,
    transcript_id: int,
    model_size: str = "base",
    diarize: bool = True,
    *,
    transcribe_url: str = "",
) -> None:
    """Run transcription (and optionally diarisation) and update the transcript row.

    This coroutine is intended to be launched as a background task.
    *transcript_id* must be an existing row (status='pending') created by
    ``storage.create_transcript_job()``.

    When *transcribe_url* (or ``TRANSCRIBE_URL`` env var) is set, the audio
    is POSTed to a remote worker. On failure the local faster-whisper path
    runs as a fallback.

    Diarisation is attempted only when *diarize* is True **and** ``HF_TOKEN``
    is present in the environment. Otherwise the plain faster-whisper path runs.
    """
    row = await storage.get_audio_session_row(audio_session_id)
    if row is None:
        await storage.update_transcript(
            transcript_id,
            status="error",
            error_msg=f"Audio session {audio_session_id} not found",
        )
        return

    await storage.update_transcript(transcript_id, status="running")
    file_path: str = row["file_path"]
    use_diarize = diarize and bool(os.environ.get("HF_TOKEN")) and _pyannote_available()

    try:
        # ----- Remote offload (preferred when TRANSCRIBE_URL is set) -----
        remote = await _try_remote_transcribe(
            file_path, model_size, diarize, transcribe_url=transcribe_url
        )
        if remote is not None:
            text, segments = remote
            segments_json_str = json.dumps(segments) if segments else None
            await storage.update_transcript(
                transcript_id, status="done", text=text, segments_json=segments_json_str
            )
            logger.info(
                "Transcription done (remote): audio_session_id={} chars={}",
                audio_session_id,
                len(text),
            )
            await _run_trigger_scan(storage, audio_session_id, row, segments)
            return

        # ----- Local processing (fallback) -----
        if use_diarize:
            text, segments_json_str = await asyncio.to_thread(
                _run_with_diarization, file_path=file_path, model_size=model_size
            )
            await storage.update_transcript(
                transcript_id, status="done", text=text, segments_json=segments_json_str
            )
            logger.info(
                "Transcription+diarisation done: audio_session_id={} chars={}",
                audio_session_id,
                len(text),
            )
            assert segments_json_str is not None  # _run_with_diarization always returns str
            segments = json.loads(segments_json_str)
        else:
            text = await asyncio.to_thread(_run_whisper, file_path=file_path, model_size=model_size)
            await storage.update_transcript(transcript_id, status="done", text=text)
            logger.info(
                "Transcription done: audio_session_id={} chars={}",
                audio_session_id,
                len(text),
            )
            # Synthesize a single segment so trigger scan can still run
            segments = [{"start": 0.0, "end": 0.0, "text": text}]

        # Auto-scan for trigger keywords and create tagged notes
        await _run_trigger_scan(storage, audio_session_id, row, segments)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed: audio_session_id={} err={}", audio_session_id, exc)
        await storage.update_transcript(transcript_id, status="error", error_msg=str(exc))


# ---------------------------------------------------------------------------
# Trigger scan — auto-create tagged notes from transcript keywords
# ---------------------------------------------------------------------------


async def _run_trigger_scan(
    storage: Storage,
    audio_session_id: int,
    audio_row: dict[str, object],
    segments: list[dict[str, object]],
) -> None:
    """Run trigger keyword scan on transcript segments (best-effort)."""
    from helmlog.triggers import scan_transcript

    start_utc = str(audio_row.get("start_utc") or "")
    if not start_utc:
        return
    try:
        count = await scan_transcript(storage, audio_session_id, start_utc, segments)
        if count:
            logger.info(
                "Trigger scan created {} auto-note(s) for audio_session_id={}",
                count,
                audio_session_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trigger scan failed for audio_session_id={}: {}", audio_session_id, exc)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


def _pyannote_available() -> bool:
    """Return True only if pyannote.audio (and torch) can be imported."""
    try:
        import pyannote.audio  # noqa: F401  # type: ignore[import-untyped]
        import torch  # noqa: F401  # type: ignore[import-untyped]

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Sync workers (run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_whisper(*, file_path: str, model_size: str) -> str:
    """Run faster-whisper synchronously (called from asyncio.to_thread)."""
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    logger.debug("Loading faster-whisper model={} for {}", model_size, file_path)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        file_path, beam_size=5, condition_on_previous_text=False, repetition_penalty=1.2
    )
    parts = [seg.text for seg in segments]
    return " ".join(parts).strip()


def _run_whisper_segments(*, file_path: str, model_size: str) -> list[tuple[float, float, str]]:
    """Return [(start, end, text)] per whisper segment."""
    from faster_whisper import WhisperModel

    logger.debug("Loading faster-whisper model={} for {}", model_size, file_path)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        file_path, beam_size=5, condition_on_previous_text=False, repetition_penalty=1.2
    )
    return [(s.start, s.end, s.text) for s in segments]


def _run_diarizer(file_path: str) -> list[tuple[float, float, str]]:
    """Return [(start, end, speaker_label)] from pyannote diarisation.

    Audio is pre-loaded via soundfile and converted to a torch tensor to bypass
    pyannote's built-in audio decoder which depends on torchcodec (unavailable
    on aarch64).
    """
    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline

    token = os.environ.get("HF_TOKEN") or None
    logger.debug("Loading pyannote diarization pipeline for {}", file_path)
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=token)
    if pipeline is None:
        raise RuntimeError("pyannote Pipeline.from_pretrained returned None — check HF_TOKEN")
    pipeline.to(torch.device("cpu"))

    # Load audio via soundfile → numpy, then convert to torch tensor.
    # soundfile returns (samples,) for mono or (samples, channels) for multi-channel;
    # pyannote expects (channels, samples).
    data, sample_rate = sf.read(file_path, dtype="float32")
    waveform = torch.from_numpy(data).unsqueeze(0) if data.ndim == 1 else torch.from_numpy(data).T
    audio_input: dict[str, object] = {"waveform": waveform, "sample_rate": sample_rate}
    result = pipeline(audio_input)

    # pyannote 4.x returns DiarizeOutput; 3.x returns Annotation directly.
    annotation = getattr(result, "speaker_diarization", result)
    return [(turn.start, turn.end, spk) for turn, _, spk in annotation.itertracks(yield_label=True)]


def _merge(
    whisper_segs: list[tuple[float, float, str]],
    diar_segs: list[tuple[float, float, str]],
) -> list[dict[str, object]]:
    """Assign speaker to each whisper segment by midpoint lookup.

    Raw pyannote speaker labels (e.g. "SPEAKER_00", "A") are normalised to
    SPEAKER_00, SPEAKER_01, … in order of first appearance.
    """
    unique: list[str] = []
    for _, _, label in diar_segs:
        if label not in unique:
            unique.append(label)
    label_map = {lbl: f"SPEAKER_{i:02d}" for i, lbl in enumerate(unique)}

    def speaker_at(mid: float) -> str:
        for s, e, lbl in diar_segs:
            if s <= mid <= e:
                return label_map[lbl]
        # fallback: nearest interval
        if not diar_segs:
            return "SPEAKER_00"
        nearest = min(diar_segs, key=lambda x: min(abs(x[0] - mid), abs(x[1] - mid)))
        return label_map[nearest[2]]

    return [
        {"start": s, "end": e, "speaker": speaker_at((s + e) / 2), "text": t}
        for s, e, t in whisper_segs
    ]


def _run_with_diarization(*, file_path: str, model_size: str) -> tuple[str, str]:
    """Run whisper + diarisation and return (plain_text, segments_json)."""
    whisper_segs = _run_whisper_segments(file_path=file_path, model_size=model_size)
    diar_segs = _run_diarizer(file_path)
    segments = _merge(whisper_segs, diar_segs)
    plain = "\n".join(f"{seg['speaker']}: {str(seg['text']).strip()}" for seg in segments)
    return plain, json.dumps(segments)
