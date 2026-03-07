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

    async def start(self, config: AudioConfig, name: str | None = None) -> AudioSession:
        """Open the audio stream and start recording to a WAV file.

        If *name* is provided the file is saved as ``{output_dir}/{name}.wav``.
        Otherwise the filename defaults to ``audio_YYYYMMDD_HHMMSS.wav``.

        Returns an AudioSession with start_utc set (end_utc is None until
        stop() is called).

        Raises AudioDeviceNotFoundError if no matching input device is found.
        """
        import sounddevice as sd
        import soundfile as sf

        device_index, device_name = _resolve_device(config.device)

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
            channels=config.channels,
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

        # Open the InputStream — callback enqueues numpy chunks
        self._stream = sd.InputStream(
            device=device_index,
            samplerate=config.sample_rate,
            channels=config.channels,
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
            channels=config.channels,
        )

        logger.debug(
            "Audio stream opened: device={!r} file={} rate={} ch={}",
            device_name,
            file_path,
            config.sample_rate,
            config.channels,
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
# Device resolution helper
# ---------------------------------------------------------------------------


def _resolve_device(spec: str | int | None) -> tuple[int, str]:
    """Resolve a device spec (name substring, index, or None) to (index, name).

    Raises AudioDeviceNotFoundError if no matching input device is found.
    """
    import sounddevice as sd

    devices = sd.query_devices()

    if spec is None:
        # Auto-detect: first device with input channels
        for idx, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                return idx, str(dev["name"])
        raise AudioDeviceNotFoundError("No audio input devices found")

    if isinstance(spec, int):
        if spec < 0 or spec >= len(devices):
            raise AudioDeviceNotFoundError(f"Audio device index {spec} out of range")
        dev = devices[spec]
        if dev["max_input_channels"] == 0:
            raise AudioDeviceNotFoundError(f"Device {spec} ({dev['name']!r}) has no input channels")
        return spec, str(dev["name"])

    # Name substring match (case-insensitive)
    spec_lower = spec.lower()
    for idx, dev in enumerate(devices):
        if spec_lower in str(dev["name"]).lower() and dev["max_input_channels"] > 0:
            return idx, str(dev["name"])
    raise AudioDeviceNotFoundError(
        f"No audio input device matching {spec!r}. "
        "Run `helmlog list-devices` to see available devices."
    )
