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
import tempfile
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Remote transcription offload
# ---------------------------------------------------------------------------

_REMOTE_TIMEOUT_S = 3600  # 1 hour — diarization on CPU is slow for long debriefs


async def _try_remote_transcribe(
    file_path: str,
    model_size: str,
    diarize: bool,
    *,
    transcribe_url: str = "",
    num_speakers: int | None = None,
) -> tuple[str, list[dict[str, object]]] | None:
    """POST the WAV file to a remote worker and return (text, segments).

    *transcribe_url* is the base URL of the worker (e.g. ``http://mac:8321``).
    Falls back to ``TRANSCRIBE_URL`` env var if not provided.

    *num_speakers* optionally constrains pyannote to exactly N speakers —
    important for sibling captures that mix 2 people onto one mono WAV.
    Unset = pyannote picks automatically (may over-split on noisy audio).

    Returns *None* when no URL is configured or the remote is unreachable,
    signalling the caller to fall back to local processing.
    """
    url = (transcribe_url or os.environ.get("TRANSCRIBE_URL", "")).rstrip("/")
    if not url:
        return None

    # Warn when sending audio PII over unencrypted transport (#201)
    if url.startswith("http://") and not any(h in url for h in ("localhost", "127.0.0.1", "::1")):
        logger.warning(
            "TRANSCRIBE_URL uses plain HTTP ({}) — crew voice PII is sent unencrypted. "
            "Use HTTPS for production deployments.",
            url,
        )

    import httpx

    endpoint = f"{url}/transcribe"
    logger.info("Remote transcribe: uploading {} to {}", file_path, endpoint)
    params: dict[str, str] = {"model_size": model_size, "diarize": str(diarize).lower()}
    if num_speakers is not None:
        params["num_speakers"] = str(int(num_speakers))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_REMOTE_TIMEOUT_S)) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    endpoint,
                    files={"file": ("audio.wav", f, "audio/wav")},
                    params=params,
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


async def _transcribe_one_channel(
    file_path: str,
    model_size: str,
    diarize: bool,
    *,
    transcribe_url: str = "",
    num_speakers: int | None = None,
) -> tuple[str, list[dict[str, object]]]:
    """Helper: transcribe a single channel (local or remote)."""
    # 1. Try remote
    remote = await _try_remote_transcribe(
        file_path,
        model_size,
        diarize,
        transcribe_url=transcribe_url,
        num_speakers=num_speakers,
    )
    if remote is not None:
        return remote

    # 2. Try local
    if diarize and _pyannote_available() and bool(os.environ.get("HF_TOKEN")):
        text, segments_json_str = await asyncio.to_thread(
            _run_with_diarization,
            file_path=file_path,
            model_size=model_size,
            num_speakers=num_speakers,
        )
        return text, json.loads(segments_json_str)
    else:
        raw_segs = await asyncio.to_thread(
            _run_whisper_segments, file_path=file_path, model_size=model_size
        )
        text = " ".join(t for _, _, t in raw_segs).strip()
        segments = [{"start": s, "end": e, "text": t} for s, e, t in raw_segs]
        return text, segments


async def _transcribe_multi_channel(
    file_path: str,
    *,
    channels: int,
    channel_map: dict[int, str],
    model_size: str,
    transcribe_url: str = "",
) -> list[dict[str, object]]:
    """Split a multi-channel WAV and transcribe each channel independently.

    Channel indices are 0-based to match sounddevice / numpy and the v63
    ``channel_map`` table. ``channel_map[i]`` resolves to the configured
    position name (e.g. "helm"); unmapped channels fall back to ``CH{i}``.

    Pyannote diarisation is intentionally **skipped** because hardware
    isolation already ties each channel to a person — running diarisation
    would be redundant and slow per the issue spec.

    Returns a single list of segments sorted by ``start``, each tagged with
    ``channel_index``, ``position_name``, and ``speaker`` (= position).
    """
    import soundfile as sf

    logger.info("Multi-channel isolation: transcribing {} channels for {}", channels, file_path)
    data, samplerate = sf.read(file_path)

    all_segments: list[dict[str, object]] = []
    for i in range(channels):
        position = channel_map.get(i, f"CH{i}")
        logger.debug("Transcribing channel {} (position={})", i, position)

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            ch_data = data[:, i] if data.ndim > 1 else data
            sf.write(tmp_path, ch_data, samplerate)
        try:
            _text, segments = await _transcribe_one_channel(
                tmp_path, model_size, diarize=False, transcribe_url=transcribe_url
            )
            for seg in segments:
                seg["channel_index"] = i
                seg["position_name"] = position
                seg["speaker"] = position
            all_segments.extend(segments)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    all_segments.sort(key=lambda x: float(str(x.get("start", 0))))
    return all_segments


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def _persist_sibling_segments(
    storage: Storage,
    transcript_id: int,
    segments: list[dict[str, object]],
    *,
    ordinal: int,
    position_name: str,
) -> None:
    """Tag mono-sibling segments with channel_index/position and persist relationally.

    Sibling-card captures (#509) skip the multi-channel split because each
    WAV is already a single-channel file — but the downstream merge (chunk 3)
    needs the same ``channel_index``/``position_name``/``speaker`` tags that
    pt.4's multi-channel path provides, so we annotate and bulk-insert here.

    Speaker labels (#648): always keep the pyannote-diarized label when one
    was emitted so cross-mic speakers are distinguishable — the hardware-
    isolation "one mic = one person" shortcut breaks both in debrief (crew
    share mics) and in the race itself (someone talks into a neighbor's mic
    during a maneuver). We prefix each label with ``position_name`` so
    ``sib0:SPEAKER_00`` stays distinct from ``sib1:SPEAKER_00`` (pyannote
    labels are per-WAV, not correlated across siblings). Segments without a
    speaker fall back to the bare ``position_name``.
    """
    if not segments:
        return
    for seg in segments:
        seg["channel_index"] = ordinal
        seg["position_name"] = position_name
        raw_speaker = seg.get("speaker")
        seg["speaker"] = f"{position_name}:{raw_speaker}" if raw_speaker else position_name
    relational = [
        {
            "segment_index": idx,
            "start_time": float(seg.get("start", 0.0)),  # type: ignore[arg-type]
            "end_time": float(seg.get("end", 0.0)),  # type: ignore[arg-type]
            "text": str(seg.get("text", "")),
            "speaker": str(seg.get("speaker") or position_name),
            "channel_index": ordinal,
            "position_name": position_name,
        }
        for idx, seg in enumerate(segments)
    ]
    await storage.insert_transcript_segments(transcript_id, relational)


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
    channels: int = row.get("channels", 1)

    # Sibling-card capture (#509): when the session is one of N parallel
    # mono USB cards, tag its segments with the ordinal + configured
    # position_name so the downstream merge can stitch them back together.
    sibling_tag: tuple[int, str] | None = None
    num_speakers_hint: int | None = None
    if row.get("capture_group_id") and channels == 1:
        cmap = await storage.get_channel_map_for_audio_session(audio_session_id)
        # Fallback label (#648): user-facing "R1" / "R2" etc. when no admin
        # channel_map is configured. Maps to the receiver number from the
        # common 2-mic-per-receiver setup, 1-indexed for humans.
        ordinal = int(row.get("capture_ordinal") or 0)
        position = cmap.get(0, f"R{ordinal + 1}")
        sibling_tag = (ordinal, position)
        # #648: common sibling-mode setup has multiple crew sharing one
        # wireless receiver (2 mics → one mono WAV). Tell pyannote how many
        # voices to expect so it doesn't over-split noisy race audio into
        # 5–9 spurious labels. Configurable via env; default 2.
        try:
            num_speakers_hint = int(os.environ.get("AUDIO_SPEAKERS_PER_CHANNEL", "2"))
        except ValueError:
            num_speakers_hint = 2
        if num_speakers_hint <= 0:
            num_speakers_hint = None

    segments_json_str: str | None = None
    try:
        # ----- Multi-channel Isolation Mode (#462 pt.3) -----
        if channels > 1:
            # Look up the active channel→position map via the v63/v64 chain:
            # session identity → channel_map override → admin default. 0-based.
            channel_map = await storage.get_channel_map_for_audio_session(audio_session_id)
            all_segments = await _transcribe_multi_channel(
                file_path,
                channels=channels,
                channel_map=channel_map,
                model_size=model_size,
                transcribe_url=transcribe_url,
            )
            merged_text = " ".join(str(s.get("text", "")).strip() for s in all_segments).strip()
            segments_json_str = json.dumps(all_segments)

            # Relational persistence (#493) — populate transcript_segments so
            # downstream sub-issues (#496–#499) don't re-parse JSON.
            relational = [
                {
                    "segment_index": idx,
                    "start_time": float(seg.get("start", 0.0)),  # type: ignore[arg-type]
                    "end_time": float(seg.get("end", 0.0)),  # type: ignore[arg-type]
                    "text": str(seg.get("text", "")),
                    "speaker": seg.get("speaker"),
                    "channel_index": seg.get("channel_index"),
                    "position_name": seg.get("position_name"),
                }
                for idx, seg in enumerate(all_segments)
            ]
            await storage.insert_transcript_segments(transcript_id, relational)

            await storage.update_transcript(
                transcript_id, status="done", text=merged_text, segments_json=segments_json_str
            )
            logger.info(
                "Multi-channel transcription done: audio_session_id={} channels={} segments={}",
                audio_session_id,
                channels,
                len(all_segments),
            )
            await _run_trigger_scan(storage, audio_session_id, row, all_segments)
            await _run_llm_callback_detection(storage, audio_session_id)
            return

        # ----- Single-channel mode (Existing logic) -----
        use_diarize = diarize and bool(os.environ.get("HF_TOKEN")) and _pyannote_available()

        # ----- Remote offload (preferred when TRANSCRIBE_URL is set) -----
        remote = await _try_remote_transcribe(
            file_path,
            model_size,
            diarize,
            transcribe_url=transcribe_url,
            num_speakers=num_speakers_hint,
        )
        if remote is not None:
            text, segments = remote
            if sibling_tag is not None:
                await _persist_sibling_segments(
                    storage,
                    transcript_id,
                    segments,
                    ordinal=sibling_tag[0],
                    position_name=sibling_tag[1],
                )
            segments_json_str = json.dumps(segments) if segments else None
            await storage.update_transcript(
                transcript_id, status="done", text=text, segments_json=segments_json_str
            )
            logger.info(
                "Transcription done (remote): audio_session_id={} chars={}",
                audio_session_id,
                len(text),
            )
            # Voice learning: auto-match speakers against stored profiles
            if diarize and segments:
                await _try_auto_match(storage, transcript_id, file_path, segments)
            await _run_trigger_scan(storage, audio_session_id, row, segments)
            await _run_llm_callback_detection(storage, audio_session_id)
            return

        # ----- Local processing (fallback) -----
        if use_diarize:
            text, segments_json_str = await asyncio.to_thread(
                _run_with_diarization,
                file_path=file_path,
                model_size=model_size,
                num_speakers=num_speakers_hint,
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
            # Voice learning: auto-match speakers against stored profiles
            await _try_auto_match(storage, transcript_id, file_path, segments)
        else:
            raw_segs = await asyncio.to_thread(
                _run_whisper_segments, file_path=file_path, model_size=model_size
            )
            text = " ".join(t for _, _, t in raw_segs).strip()
            segments = [{"start": s, "end": e, "text": t} for s, e, t in raw_segs]
            segments_json_str = json.dumps(segments) if segments else None
            await storage.update_transcript(
                transcript_id, status="done", text=text, segments_json=segments_json_str
            )
            logger.info(
                "Transcription done: audio_session_id={} chars={}",
                audio_session_id,
                len(text),
            )

        # Sibling-card annotation (#509): local single-channel paths reach here
        # with a plain segments list; tag and persist before trigger scan.
        if sibling_tag is not None:
            await _persist_sibling_segments(
                storage,
                transcript_id,
                segments,
                ordinal=sibling_tag[0],
                position_name=sibling_tag[1],
            )
            # Rewrite segments_json so GET /api/audio/{id}/transcript sees the tags
            segments_json_str = json.dumps(segments) if segments else None
            await storage.update_transcript(
                transcript_id, status="done", segments_json=segments_json_str
            )

        # Auto-scan for trigger keywords and create tagged notes
        await _run_trigger_scan(storage, audio_session_id, row, segments)
        await _run_llm_callback_detection(storage, audio_session_id)
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


async def _run_llm_callback_detection(
    storage: Storage,
    audio_session_id: int,
) -> None:
    """Auto-trigger the LLM verbal-callback job once per race (#697).

    Best-effort: silently no-op when ``ANTHROPIC_API_KEY`` is unset, the
    audio session is not linked to a race, the consent flag is unset, or
    the cost cap is reached. The admin can always re-run via the route.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return
    try:
        from helmlog.llm_callback_job import maybe_run_after_transcription
        from helmlog.llm_client import LLMClient, LLMConfig

        cfg = LLMConfig(
            api_key=api_key,
            model=os.environ.get("LLM_CALLBACK_MODEL", "claude-haiku-4-5-20251001"),
            endpoint=os.environ.get("LLM_ENDPOINT", "https://api.anthropic.com/v1/messages"),
            input_usd_per_mtok=1.00,
            output_usd_per_mtok=5.00,
            cache_read_usd_per_mtok=0.10,
            cache_write_usd_per_mtok=1.25,
        )
        result = await maybe_run_after_transcription(
            storage,
            audio_session_id=audio_session_id,
            client=LLMClient(cfg),
        )
        if result is not None:
            logger.info(
                "LLM callback detection: audio_session_id={} result={}",
                audio_session_id,
                result,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "LLM callback detection failed for audio_session_id={}: {}",
            audio_session_id,
            exc,
        )


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


def _run_diarizer(
    file_path: str, *, num_speakers: int | None = None
) -> list[tuple[float, float, str]]:
    """Return [(start, end, speaker_label)] from pyannote diarisation.

    Audio is pre-loaded via soundfile and converted to a torch tensor to bypass
    pyannote's built-in audio decoder which depends on torchcodec (unavailable
    on aarch64).

    *num_speakers* (optional) constrains pyannote to exactly N speakers —
    critical for sibling captures that mix 2 crew onto one mono WAV, where
    otherwise pyannote over-splits into 5–9 labels on noisy race audio.
    """
    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline

    token = os.environ.get("HF_TOKEN") or None
    logger.debug("Loading pyannote diarization pipeline for {}", file_path)
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", token=token)
    if pipeline is None:
        raise RuntimeError("pyannote Pipeline.from_pretrained returned None — check HF_TOKEN")
    # Use Apple Silicon GPU when available — ~5x faster than CPU for pyannote
    device = torch.device("mps") if torch.backends.mps.is_available() else torch.device("cpu")
    logger.debug("pyannote device: {}", device)
    pipeline.to(device)

    # Load audio via soundfile → numpy, then convert to torch tensor.
    # soundfile returns (samples,) for mono or (samples, channels) for multi-channel;
    # pyannote expects (channels, samples).
    data, sample_rate = sf.read(file_path, dtype="float32")
    waveform = torch.from_numpy(data).unsqueeze(0) if data.ndim == 1 else torch.from_numpy(data).T
    audio_input: dict[str, object] = {"waveform": waveform, "sample_rate": sample_rate}
    if num_speakers is not None and num_speakers > 0:
        result = pipeline(audio_input, num_speakers=int(num_speakers))
    else:
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


def _run_with_diarization(
    *, file_path: str, model_size: str, num_speakers: int | None = None
) -> tuple[str, str]:
    """Run whisper + diarisation and return (plain_text, segments_json)."""
    whisper_segs = _run_whisper_segments(file_path=file_path, model_size=model_size)
    diar_segs = _run_diarizer(file_path, num_speakers=num_speakers)
    segments = _merge(whisper_segs, diar_segs)
    plain = "\n".join(f"{seg['speaker']}: {str(seg['text']).strip()}" for seg in segments)
    return plain, json.dumps(segments)


# ---------------------------------------------------------------------------
# Voice learning: embedding extraction + auto-matching (#443)
# ---------------------------------------------------------------------------

# Minimum thresholds before building a voice profile
_MIN_SEGMENTS_FOR_PROFILE = 30
_MIN_SESSIONS_FOR_PROFILE = 2

# Confidence thresholds for auto-matching
_AUTO_MATCH_HIGH = 0.7  # auto-assign
_AUTO_MATCH_LOW = 0.4  # suggest (marginal)


async def _try_auto_match(
    storage: Storage,
    transcript_id: int,
    file_path: str,
    segments: list[dict[str, object]],
) -> None:
    """Best-effort auto-matching of speakers against stored voice profiles."""
    try:
        # Build diar_segs from merged segments for embedding extraction
        diar_segs = [
            (float(str(seg["start"])), float(str(seg["end"])), str(seg.get("speaker", "")))
            for seg in segments
            if seg.get("speaker")
        ]
        if not diar_segs:
            return
        speaker_embs = await asyncio.to_thread(_extract_speaker_embeddings, file_path, diar_segs)
        if speaker_embs:
            await auto_match_speakers(storage, transcript_id, speaker_embs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Auto-match failed (non-critical): {}", exc)


def _extract_speaker_embeddings(
    file_path: str,
    diar_segs: list[tuple[float, float, str]],
) -> dict[str, bytes]:
    """Extract per-speaker embedding vectors from diarization segments.

    Uses the pyannote embedding model to compute a centroid embedding for each
    speaker by averaging embeddings across their segments.

    Returns a dict mapping raw speaker labels (whatever the caller passed —
    e.g. ``sib0:SPEAKER_00`` for sibling captures, #648) to serialised
    float32 embedding bytes. The labels are preserved end-to-end so
    cross-session auto-match can key on the same globally-unique label the
    segments store.
    """
    try:
        import numpy as np
        import soundfile as sf
        import torch
        from pyannote.audio import Model
    except ImportError:
        logger.debug("pyannote/torch not available for embedding extraction")
        return {}

    token = os.environ.get("HF_TOKEN") or None
    try:
        loaded = Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM", token=token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load embedding model: {}", exc)
        return {}
    if loaded is None:
        logger.debug("Embedding model returned None")
        return {}

    from pyannote.audio import Inference

    inference = Inference(loaded, window="whole")

    data, sample_rate = sf.read(file_path, dtype="float32")
    if data.ndim == 1:
        data = data.reshape(-1, 1)

    # Group segments by raw speaker label — no renumbering, so the keys
    # match what's stored in transcript_segments.speaker (#648).
    speaker_segs: dict[str, list[tuple[float, float]]] = {}
    for s, e, lbl in diar_segs:
        speaker_segs.setdefault(lbl, []).append((s, e))

    embeddings: dict[str, bytes] = {}
    for speaker, segs in speaker_segs.items():
        seg_embeddings = []
        for start, end in segs:
            start_frame = int(start * sample_rate)
            end_frame = min(int(end * sample_rate), len(data))
            if end_frame - start_frame < sample_rate * 0.5:
                continue  # skip very short segments
            chunk = data[start_frame:end_frame]
            if chunk.ndim > 1:
                waveform = torch.from_numpy(chunk).T
            else:
                waveform = torch.from_numpy(chunk).unsqueeze(0)
            try:
                emb = inference({"waveform": waveform, "sample_rate": sample_rate})
                seg_embeddings.append(emb)
            except Exception:  # noqa: BLE001
                continue
        if seg_embeddings:
            centroid = np.mean(seg_embeddings, axis=0).astype(np.float32)
            embeddings[speaker] = centroid.tobytes()

    return embeddings


def _cosine_similarity(a: bytes, b: bytes) -> float:
    """Compute cosine similarity between two float32 embedding byte vectors."""
    import numpy as np

    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    if len(va) != len(vb) or len(va) == 0:
        return 0.0
    dot = float(np.dot(va, vb))
    norm = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return dot / norm if norm > 0 else 0.0


async def auto_match_speakers(
    storage: Storage,
    transcript_id: int,
    speaker_embeddings: dict[str, bytes],
) -> dict[str, dict[str, object]]:
    """Match diarized speakers against stored voice profiles.

    Returns a dict of speaker_label → {type, user_id, name, confidence} for
    speakers that exceed the marginal confidence threshold.
    """
    if not speaker_embeddings:
        return {}

    # Load all voice profiles with consent
    db = storage._conn()
    cur = await db.execute(
        "SELECT vp.user_id, vp.embedding, u.name"
        " FROM crew_voice_profiles vp"
        " JOIN users u ON u.id = vp.user_id"
        " JOIN crew_consents cc ON cc.user_id = vp.user_id"
        "   AND cc.consent_type = 'voice_profile' AND cc.granted = 1"
    )
    profiles = await cur.fetchall()
    if not profiles:
        return {}

    matches: dict[str, dict[str, object]] = {}
    used_users: set[int] = set()

    # For each speaker, find best matching profile
    scored: list[tuple[str, int, str, float]] = []
    for speaker, emb in speaker_embeddings.items():
        for profile in profiles:
            sim = _cosine_similarity(emb, profile["embedding"])
            scored.append((speaker, profile["user_id"], profile["name"], sim))

    # Sort by confidence descending, assign greedily (no double-assignment)
    scored.sort(key=lambda x: x[3], reverse=True)
    for speaker, uid, name, conf in scored:
        if speaker in matches or uid in used_users:
            continue
        if conf < _AUTO_MATCH_LOW:
            continue
        matches[speaker] = {
            "type": "auto",
            "user_id": uid,
            "name": name,
            "confidence": round(conf, 2),
        }
        used_users.add(uid)

    # Write auto-matches to the transcript's speaker_map
    if matches:
        existing = await storage.get_speaker_map(transcript_id)
        for label, entry in matches.items():
            if label not in existing:  # don't overwrite manual assignments
                existing[label] = entry
        await db.execute(
            "UPDATE transcripts SET speaker_map = ? WHERE id = ?",
            (json.dumps(existing), transcript_id),
        )
        await db.commit()
        logger.info(
            "Auto-matched {} speakers in transcript {}: {}",
            len(matches),
            transcript_id,
            {k: f"{v['name']} ({v['confidence']})" for k, v in matches.items()},
        )

    return matches


async def maybe_build_voice_profile(
    storage: Storage,
    user_id: int,
) -> bool:
    """Check if enough manual assignments exist to build/update a voice profile.

    Requires ≥ _MIN_SEGMENTS_FOR_PROFILE segments across ≥ _MIN_SESSIONS_FOR_PROFILE sessions.
    Returns True if a profile was built or updated.
    """
    # Check consent
    consents = await storage.get_crew_consents(user_id)
    has_consent = any(c["consent_type"] == "voice_profile" and c["granted"] for c in consents)
    if not has_consent:
        return False

    # Count manual crew assignments across transcripts
    db = storage._conn()
    cur = await db.execute(
        "SELECT t.id, t.speaker_map, a.file_path, t.segments_json"
        " FROM transcripts t"
        " JOIN audio_sessions a ON a.id = t.audio_session_id"
        " WHERE t.speaker_map IS NOT NULL AND t.segments_json IS NOT NULL"
    )
    rows = await cur.fetchall()

    total_segments = 0
    sessions_with_assignment = 0

    for row in rows:
        smap = json.loads(row["speaker_map"] or "{}")
        user_labels = [
            label
            for label, entry in smap.items()
            if isinstance(entry, dict)
            and entry.get("type") == "crew"
            and entry.get("user_id") == user_id
        ]
        if not user_labels:
            continue
        sessions_with_assignment += 1
        segments = json.loads(row["segments_json"] or "[]")
        for seg in segments:
            if seg.get("speaker") in user_labels:
                total_segments += 1

    if (
        total_segments < _MIN_SEGMENTS_FOR_PROFILE
        or sessions_with_assignment < _MIN_SESSIONS_FOR_PROFILE
    ):
        logger.debug(
            "Voice profile for user {}: {}/{} segments, {}/{} sessions — not enough yet",
            user_id,
            total_segments,
            _MIN_SEGMENTS_FOR_PROFILE,
            sessions_with_assignment,
            _MIN_SESSIONS_FOR_PROFILE,
        )
        return False

    # Compute aggregate embedding from all assigned segments
    if not _pyannote_available():
        logger.debug("pyannote not available — cannot build voice profile")
        return False

    logger.info(
        "Building voice profile for user {}: {} segments across {} sessions",
        user_id,
        total_segments,
        sessions_with_assignment,
    )

    try:
        import numpy as np
    except ImportError:
        return False

    all_embeddings: list[bytes] = []
    for row in rows:
        smap = json.loads(row["speaker_map"] or "{}")
        user_labels = [
            label
            for label, entry in smap.items()
            if isinstance(entry, dict)
            and entry.get("type") == "crew"
            and entry.get("user_id") == user_id
        ]
        if not user_labels:
            continue
        file_path = row["file_path"]
        # Extract embeddings for the assigned speaker labels
        segments = json.loads(row["segments_json"] or "[]")
        diar_segs = [
            (seg["start"], seg["end"], seg["speaker"])
            for seg in segments
            if seg.get("speaker") in user_labels
        ]
        if not diar_segs:
            continue
        try:
            embs = await asyncio.to_thread(_extract_speaker_embeddings, file_path, diar_segs)
            all_embeddings.extend(embs.values())
        except Exception as exc:  # noqa: BLE001
            logger.debug("Embedding extraction failed for {}: {}", file_path, exc)

    if not all_embeddings:
        return False

    # Compute centroid
    vecs = [np.frombuffer(e, dtype=np.float32) for e in all_embeddings]
    centroid = np.mean(vecs, axis=0).astype(np.float32)
    await storage.upsert_voice_profile(
        user_id,
        centroid.tobytes(),
        segment_count=total_segments,
        session_count=sessions_with_assignment,
    )
    logger.info("Voice profile built for user {}", user_id)
    return True
