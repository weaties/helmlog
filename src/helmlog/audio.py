"""Audio recording module for sailing session voice capture.

Supports USB Audio Class (UAC) devices such as the Gordik 2T1R wireless
lavalier receiver. Hardware isolation: all sounddevice/soundfile access lives
here so the rest of the codebase can be tested without physical hardware.

Recording pattern:
    InputStream callback → queue.Queue → writer thread → SoundFile.write()

Configuration via environment variables (see AudioConfig).
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AudioDeviceNotFoundError(Exception):
    """Raised when no matching audio input device can be found."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioConfig:
    """Configuration for audio recording, read from environment variables."""

    device: str | int | None = field(
        default_factory=lambda: _parse_device(os.environ.get("AUDIO_DEVICE"))
    )
    sample_rate: int = field(
        default_factory=lambda: int(os.environ.get("AUDIO_SAMPLE_RATE", "48000"))
    )
    channels: int = field(default_factory=lambda: int(os.environ.get("AUDIO_CHANNELS", "1")))
    output_dir: str = field(default_factory=lambda: os.environ.get("AUDIO_DIR", "data/audio"))


def _parse_device(val: str | None) -> str | int | None:
    """Parse AUDIO_DEVICE: integer index, name substring, or None."""
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return val


# ---------------------------------------------------------------------------
# AudioSession
# ---------------------------------------------------------------------------


@dataclass
class AudioSession:
    """Metadata for a completed or in-progress audio recording."""

    file_path: str
    device_name: str
    start_utc: datetime
    end_utc: datetime | None
    sample_rate: int
    channels: int
    channel_map: dict[int, str] | None = None
    # USB device identity for multi-channel playback (#494). Zero / empty
    # when the device is not a known USB sound card (built-in mic on dev,
    # mono fallback path).
    vendor_id: int = 0
    product_id: int = 0
    serial: str = ""
    usb_port_path: str = ""
    # Sibling-card capture (#509). When multiple mono USB receivers are
    # recorded in parallel, every sibling shares a ``capture_group_id`` and
    # gets its own ordinal (0..N-1). None / 0 = legacy single-device session.
    capture_group_id: str | None = None
    capture_ordinal: int = 0


# ---------------------------------------------------------------------------
# AudioRecorder
# ---------------------------------------------------------------------------


class AudioRecorder:
    """Records audio from a USB input device to a WAV file.

    Usage::

        recorder = AudioRecorder()
        session = await recorder.start(config)
        ...
        completed = await recorder.stop()
    """

    def __init__(self) -> None:
        self._stream: Any | None = None
        self._sound_file: Any | None = None
        self._writer_thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()
        self._chunk_queue: queue.Queue[Any] = queue.Queue()
        self._session: AudioSession | None = None

    # ------------------------------------------------------------------
    # Device enumeration
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices() -> list[dict[str, object]]:
        """Return a list of available audio input devices.

        Each entry is a dict with keys: index, name, max_input_channels,
        default_samplerate.
        """
        import sounddevice as sd

        devices = sd.query_devices()
        result: list[dict[str, object]] = []
        for idx, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                result.append(
                    {
                        "index": idx,
                        "name": dev["name"],
                        "max_input_channels": dev["max_input_channels"],
                        "default_samplerate": dev["default_samplerate"],
                    }
                )
        return result

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        """True if a recording is currently in progress."""
        return self._session is not None

    async def start(
        self,
        config: AudioConfig,
        name: str | None = None,
        *,
        detected: Any = None,  # noqa: ANN401 — DetectedDevice; loose to avoid hard import
    ) -> AudioSession:
        """Open the audio stream and start recording to a WAV file.

        If *name* is provided the file is saved as ``{output_dir}/{name}.wav``.
        Otherwise the filename defaults to ``audio_YYYYMMDD_HHMMSS.wav``.

        If *detected* is a ``DetectedDevice`` from ``usb_audio.detect_*`` it
        overrides ``config.device``/``config.channels`` and persists the USB
        identity tuple onto the returned session. Otherwise we fall back to
        the legacy mono path driven by ``config``.

        Returns an AudioSession with start_utc set (end_utc is None until
        stop() is called).

        Raises AudioDeviceNotFoundError if no matching input device is found.
        """
        import sounddevice as sd
        import soundfile as sf

        if detected is not None:
            device_index = int(detected.sounddevice_index)
            device_name = str(detected.name)
            max_ch = int(detected.max_channels)
        else:
            device_index, device_name, max_ch = _resolve_device(config.device)

        requested_ch = max_ch if detected is not None else config.channels

        if requested_ch > max_ch:
            logger.warning(
                "Requested {} channels but device {!r} only supports {}. Falling back to {}.",
                requested_ch,
                device_name,
                max_ch,
                max_ch,
            )
            requested_ch = max_ch

        # Resolve channel map from environment/settings
        channel_map = None
        if requested_ch >= 2:
            channel_map = {}
            for i in range(1, requested_ch + 1):
                pos = os.environ.get(f"AUDIO_CH{i}_POS")
                if pos:
                    channel_map[i] = pos

        # Ensure output directory exists
        Path(config.output_dir).mkdir(parents=True, exist_ok=True)

        # Build filename — use provided name or fall back to UTC timestamp
        start_utc = datetime.now(UTC)
        if name is not None:
            filename = f"{name}.wav"
        else:
            filename = f"audio_{start_utc.strftime('%Y%m%d_%H%M%S')}.wav"
        file_path = str(Path(config.output_dir) / filename)

        # Open the sound file (writer thread will use this)
        self._sound_file = sf.SoundFile(
            file_path,
            mode="w",
            samplerate=config.sample_rate,
            channels=requested_ch,
            format="WAV",
            subtype="PCM_16",
        )

        # Clear any leftover state
        self._stop_event.clear()
        while not self._chunk_queue.empty():
            self._chunk_queue.get_nowait()

        # Start background writer thread
        self._writer_thread = threading.Thread(
            target=self._write_loop,
            name="audio-writer",
            daemon=True,
        )
        self._writer_thread.start()

        # Open the InputStream — callback enqueues numpy chunks.
        # Pass ``(device_index, None)`` rather than a scalar so PortAudio does
        # not try to pair us with its cached default output device — on Linux
        # with USB hot-unplug that cached pointer can go stale between service
        # startup and race-start and trip "Illegal combination of I/O devices".
        self._stream = sd.InputStream(
            device=(device_index, None),
            samplerate=config.sample_rate,
            channels=requested_ch,
            dtype="int16",
            callback=self._audio_callback,
        )
        self._stream.start()

        self._session = AudioSession(
            file_path=file_path,
            device_name=device_name,
            start_utc=start_utc,
            end_utc=None,
            sample_rate=config.sample_rate,
            channels=requested_ch,
            channel_map=channel_map,
            vendor_id=getattr(detected, "vendor_id", 0) if detected else 0,
            product_id=getattr(detected, "product_id", 0) if detected else 0,
            serial=getattr(detected, "serial", "") if detected else "",
            usb_port_path=getattr(detected, "usb_port_path", "") if detected else "",
        )

        logger.debug(
            "Audio stream opened: device={!r} file={} rate={} ch={}",
            device_name,
            file_path,
            config.sample_rate,
            requested_ch,
        )
        return self._session

    async def stop(self) -> AudioSession:
        """Stop recording, flush all buffered audio, and close the file.

        Returns the completed AudioSession with end_utc set.
        """
        if self._session is None:
            raise RuntimeError("AudioRecorder.stop() called before start()")

        # Stop the InputStream (no more callbacks after this)
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        # Signal writer thread and wait for it to drain the queue
        self._stop_event.set()
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=10)
            self._writer_thread = None

        # Close the sound file
        if self._sound_file is not None:
            self._sound_file.close()
            self._sound_file = None

        self._session.end_utc = datetime.now(UTC)
        logger.debug("Audio stream closed: file={}", self._session.file_path)
        completed = self._session
        self._session = None
        return completed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        indata: Any,  # noqa: ANN401
        frames: int,  # noqa: ARG002
        time: Any,  # noqa: ANN401, ARG002
        status: Any,  # noqa: ANN401
    ) -> None:
        """sounddevice callback: enqueue a copy of the incoming audio chunk."""
        if status:
            logger.warning("Audio input status: {}", status)
        self._chunk_queue.put(indata.copy())

    def _write_loop(self) -> None:
        """Writer thread: drain the queue and write chunks to the WAV file."""
        while not self._stop_event.is_set() or not self._chunk_queue.empty():
            try:
                chunk = self._chunk_queue.get(timeout=0.1)
                if self._sound_file is not None:
                    self._sound_file.write(chunk)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("Audio writer thread error: {}", exc)


# ---------------------------------------------------------------------------
# AudioRecorderGroup — sibling-card parallel capture (#509)
# ---------------------------------------------------------------------------


class AudioRecorderGroup:
    """Record from N USB capture devices in parallel, one WAV per card.

    Stopgap for hardware that presents each wireless receiver as a
    mono-only USB device (#509). Each sibling recorder writes its own
    WAV file; all siblings from one start/stop cycle share a single
    ``capture_group_id`` UUID and an ordinal within the group.

    Usage::

        from helmlog.usb_audio import detect_all_capture_devices

        group = AudioRecorderGroup()
        sessions = await group.start(
            config,
            devices=detect_all_capture_devices(min_channels=1),
            name=race.name,
        )
        # … write every session to storage; all share capture_group_id …
        completed = await group.stop()

    On failure during ``start()``, any siblings that already opened are
    best-effort stopped before the exception propagates, so we never
    leak USB streams.
    """

    def __init__(self) -> None:
        self._recorders: list[AudioRecorder] = []
        self._sessions: list[AudioSession] = []

    @property
    def is_recording(self) -> bool:
        return bool(self._recorders)

    async def start(
        self,
        config: AudioConfig,
        *,
        devices: list[Any],  # list[DetectedDevice]; loose to avoid import
        name: str | None = None,
    ) -> list[AudioSession]:
        """Open one sibling recorder per device and return their sessions."""
        import uuid

        if not devices:
            raise AudioDeviceNotFoundError(
                "AudioRecorderGroup.start(): no capture devices provided"
            )

        group_id = uuid.uuid4().hex
        sessions: list[AudioSession] = []
        recorders: list[AudioRecorder] = []
        try:
            for ordinal, device in enumerate(devices):
                sibling_name: str | None = None
                if name is not None:
                    sibling_name = f"{name}-sib{ordinal}"
                recorder = AudioRecorder()
                session = await recorder.start(config, name=sibling_name, detected=device)
                session.capture_group_id = group_id
                session.capture_ordinal = ordinal
                recorders.append(recorder)
                sessions.append(session)
        except Exception:
            for r in recorders:
                try:
                    await r.stop()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("sibling cleanup on start failure: {}", exc)
            raise

        self._recorders = recorders
        self._sessions = sessions
        logger.debug(
            "AudioRecorderGroup started: group={} siblings={}",
            group_id,
            len(sessions),
        )
        return sessions

    async def stop(self) -> list[AudioSession]:
        """Stop every sibling and return the completed sessions in ordinal order."""
        if not self._recorders:
            raise RuntimeError("AudioRecorderGroup.stop() called before start()")
        completed: list[AudioSession] = []
        errors: list[BaseException] = []
        for r, staged in zip(self._recorders, self._sessions, strict=True):
            try:
                done = await r.stop()
                done.capture_group_id = staged.capture_group_id
                done.capture_ordinal = staged.capture_ordinal
                completed.append(done)
            except Exception as exc:  # noqa: BLE001
                logger.error("sibling stop failed ordinal={}: {}", staged.capture_ordinal, exc)
                errors.append(exc)
        self._recorders = []
        self._sessions = []
        if errors:
            raise RuntimeError(
                f"AudioRecorderGroup.stop(): {len(errors)} sibling(s) failed — first: {errors[0]!r}"
            )
        return completed


# ---------------------------------------------------------------------------
# Unified start/stop helpers that handle both AudioRecorder and
# AudioRecorderGroup so route handlers can stay oblivious (#509).
# ---------------------------------------------------------------------------


async def capture_start(
    recorder: AudioRecorder | AudioRecorderGroup,
    config: AudioConfig,
    storage: Any,  # noqa: ANN401 — Storage; loose to avoid import cycle
    *,
    name: str | None,
    race_id: int | None,
    session_type: str,
    prev_capture_group_id: str | None = None,
) -> int:
    """Start an audio capture and persist every resulting session.

    Works transparently whether the recorder is a single-device
    ``AudioRecorder`` or a sibling-card ``AudioRecorderGroup``. Returns
    the *primary* session id (ordinal 0) so the caller can keep tracking
    the capture with a single scalar in ``session_state``.

    ``prev_capture_group_id`` is the race's sibling capture group, passed at
    debrief-start time. If set and the USB identity set we detect now differs
    from the one the race saw, log a WARNING (#648 C5) and continue with the
    current topology — debrief is not blocked by the change.
    """
    if isinstance(recorder, AudioRecorderGroup):
        import asyncio as _asyncio

        from helmlog.usb_audio import DetectedDevice, detect_all_capture_devices

        # Chained race-end → debrief-start (#762) immediately re-acquires
        # the USB capture devices that capture_stop just released. On
        # Linux the kernel holds the handles for a fraction of a second,
        # so detect_all_capture_devices can briefly return [] right after
        # the stop. Retry a few times with short backoff before failing.
        devices: list[DetectedDevice] = []
        for _attempt in range(6):
            devices = detect_all_capture_devices(min_channels=1)
            if devices:
                break
            await _asyncio.sleep(0.25)
        else:
            logger.warning("capture_start: no devices detected after 6 retries (1.5s)")

        if prev_capture_group_id is not None:
            await _warn_if_device_set_changed(
                storage,
                prev_capture_group_id=prev_capture_group_id,
                current_devices=devices,
            )
        sessions = await recorder.start(config, devices=devices, name=name)
    else:
        sessions = [await recorder.start(config, name=name)]

    primary_id: int | None = None
    for s in sessions:
        sid = await storage.write_audio_session(
            s, race_id=race_id, session_type=session_type, name=name
        )
        if primary_id is None:
            primary_id = sid
    assert primary_id is not None
    return primary_id


async def capture_stop(
    recorder: AudioRecorder | AudioRecorderGroup,
    storage: Any,  # noqa: ANN401 — Storage
    *,
    primary_session_id: int,
) -> AudioSession:
    """Stop the active capture and persist end_utc for every sibling.

    In single-device mode only ``primary_session_id`` is updated. In
    sibling mode the recorder's completed sessions carry their
    ``capture_group_id`` and ordinals; we resolve the corresponding
    audio_sessions row ids by querying ``list_capture_group_siblings``.
    Returns the primary completed ``AudioSession``.
    """
    if isinstance(recorder, AudioRecorderGroup):
        completed = await recorder.stop()
        if not completed:
            raise RuntimeError("capture_stop(): group returned no sessions")
        primary = completed[0]
        assert primary.end_utc is not None
        group_id = primary.capture_group_id
        if group_id is None:
            # Defensive: sibling recorder somehow forgot the group id.
            await storage.update_audio_session_end(primary_session_id, primary.end_utc)
            return primary
        rows = await storage.list_capture_group_siblings(group_id)
        by_ordinal = {r["capture_ordinal"]: r["id"] for r in rows}
        for s in completed:
            sid = by_ordinal.get(s.capture_ordinal)
            if sid is None:
                logger.warning(
                    "capture_stop(): no row for group={} ordinal={}",
                    group_id,
                    s.capture_ordinal,
                )
                continue
            assert s.end_utc is not None
            await storage.update_audio_session_end(sid, s.end_utc)
        return primary

    completed_single = await recorder.stop()
    assert completed_single.end_utc is not None
    await storage.update_audio_session_end(primary_session_id, completed_single.end_utc)
    return completed_single


async def _warn_if_device_set_changed(
    storage: Any,  # noqa: ANN401 — Storage
    *,
    prev_capture_group_id: str,
    current_devices: list[Any],  # list[DetectedDevice]
) -> None:
    """Compare the USB identity set of a previous capture group to the set of
    devices detected now. Log a WARNING when they differ (#648 C5).

    Used at debrief-start to surface hot-unplug / re-enumeration between the
    race and the debrief. Never raises — debrief must not be blocked.
    """
    try:
        prev_siblings = await storage.list_capture_group_siblings(prev_capture_group_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("capture_start: sibling lookup for {} failed: {}", prev_capture_group_id, exc)
        return
    if not prev_siblings:
        return

    prev_identities: set[tuple[int, int, str, str]] = {
        (
            int(s.get("vendor_id") or 0),
            int(s.get("product_id") or 0),
            s.get("serial") or "",
            s.get("usb_port_path") or "",
        )
        for s in prev_siblings
    }
    current_identities: set[tuple[int, int, str, str]] = {
        (
            int(getattr(d, "vendor_id", 0) or 0),
            int(getattr(d, "product_id", 0) or 0),
            getattr(d, "serial", "") or "",
            getattr(d, "usb_port_path", "") or "",
        )
        for d in current_devices
    }
    if prev_identities != current_identities:
        dropped = prev_identities - current_identities
        added = current_identities - prev_identities
        logger.warning(
            "Debrief USB device set changed since race (#648 C5): "
            "race_siblings={} detected_now={} dropped={} added={} — "
            "recording with current topology",
            len(prev_identities),
            len(current_identities),
            sorted(dropped),
            sorted(added),
        )


# ---------------------------------------------------------------------------
# Device resolution helper
# ---------------------------------------------------------------------------


def _resolve_device(spec: str | int | None) -> tuple[int, str, int]:
    """Resolve a device spec to ``(index, name, max_input_channels)``.

    Raises AudioDeviceNotFoundError if no matching input device is found.
    """
    import sounddevice as sd

    devices = sd.query_devices()

    if spec is None:
        # Auto-detect: first device with input channels
        for idx, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                return idx, str(dev["name"]), int(dev["max_input_channels"])
        raise AudioDeviceNotFoundError("No audio input devices found")

    if isinstance(spec, int):
        if spec < 0 or spec >= len(devices):
            raise AudioDeviceNotFoundError(f"Audio device index {spec} out of range")
        dev = devices[spec]
        if dev["max_input_channels"] == 0:
            raise AudioDeviceNotFoundError(f"Device {spec} ({dev['name']!r}) has no input channels")
        return spec, str(dev["name"]), int(dev["max_input_channels"])

    # Name substring match (case-insensitive)
    spec_lower = spec.lower()
    for idx, dev in enumerate(devices):
        if spec_lower in str(dev["name"]).lower() and dev["max_input_channels"] > 0:
            return idx, str(dev["name"]), int(dev["max_input_channels"])
    raise AudioDeviceNotFoundError(
        f"No audio input device matching {spec!r}. "
        "Run `helmlog list-devices` to see available devices."
    )
