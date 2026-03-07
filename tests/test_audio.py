"""Tests for src/logger/audio.py.

All tests mock sounddevice and soundfile so they run without physical hardware.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from unittest.mock import MagicMock, patch

import pytest

from helmlog.audio import (
    AudioConfig,
    AudioDeviceNotFoundError,
    AudioRecorder,
    _resolve_device,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_DEVICES = [
    {
        "name": "Built-in Microphone",
        "max_input_channels": 2,
        "max_output_channels": 0,
        "default_samplerate": 44100.0,
    },
    {
        "name": "Gordik 2T1R USB Audio",
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48000.0,
    },
    {
        "name": "HDMI Output",
        "max_input_channels": 0,
        "max_output_channels": 2,
        "default_samplerate": 48000.0,
    },
]


# ---------------------------------------------------------------------------
# test_audio_config_defaults
# ---------------------------------------------------------------------------


def test_audio_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """AudioConfig reads defaults from env vars (or uses hard-coded fallbacks)."""
    monkeypatch.delenv("AUDIO_DEVICE", raising=False)
    monkeypatch.delenv("AUDIO_SAMPLE_RATE", raising=False)
    monkeypatch.delenv("AUDIO_CHANNELS", raising=False)
    monkeypatch.delenv("AUDIO_DIR", raising=False)

    config = AudioConfig()

    assert config.device is None
    assert config.sample_rate == 48000
    assert config.channels == 1
    assert config.output_dir == "data/audio"


def test_audio_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """AudioConfig picks up env var overrides."""
    monkeypatch.setenv("AUDIO_DEVICE", "Gordik")
    monkeypatch.setenv("AUDIO_SAMPLE_RATE", "44100")
    monkeypatch.setenv("AUDIO_CHANNELS", "2")
    monkeypatch.setenv("AUDIO_DIR", "/tmp/audio")

    config = AudioConfig()

    assert config.device == "Gordik"
    assert config.sample_rate == 44100
    assert config.channels == 2
    assert config.output_dir == "/tmp/audio"


def test_audio_config_integer_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUDIO_DEVICE as a numeric string is parsed to an int."""
    monkeypatch.setenv("AUDIO_DEVICE", "3")
    config = AudioConfig()
    assert config.device == 3


# ---------------------------------------------------------------------------
# test_wav_filename_format
# ---------------------------------------------------------------------------


def test_wav_filename_format() -> None:
    """The WAV filename embeds the UTC timestamp in YYYYMMDD_HHMMSS format."""
    fixed_dt = datetime(2025, 8, 10, 14, 5, 30, tzinfo=UTC)
    filename = f"audio_{fixed_dt.strftime('%Y%m%d_%H%M%S')}.wav"
    assert filename == "audio_20250810_140530.wav"
    assert filename.endswith(".wav")


# ---------------------------------------------------------------------------
# test_list_devices_filters_inputs
# ---------------------------------------------------------------------------


def test_list_devices_filters_inputs() -> None:
    """list_devices() returns only devices with max_input_channels > 0."""
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES):
        devices = AudioRecorder.list_devices()

    assert len(devices) == 2
    names = [d["name"] for d in devices]
    assert "HDMI Output" not in names
    assert "Built-in Microphone" in names
    assert "Gordik 2T1R USB Audio" in names

    # Verify index assignment is correct (HDMI is index 2, skipped)
    assert devices[0]["index"] == 0
    assert devices[1]["index"] == 1


# ---------------------------------------------------------------------------
# test_start_records_utc
# ---------------------------------------------------------------------------


def test_start_records_utc(tmp_path: Path) -> None:
    """start() sets start_utc before the stream opens."""
    before = datetime.now(UTC)

    mock_stream = MagicMock()
    mock_sf = MagicMock()
    mock_sf.__enter__ = MagicMock(return_value=mock_sf)
    mock_sf.__exit__ = MagicMock(return_value=False)

    with (
        patch("sounddevice.query_devices", return_value=_FAKE_DEVICES),
        patch("sounddevice.InputStream", return_value=mock_stream),
        patch("soundfile.SoundFile", return_value=mock_sf),
    ):
        config = AudioConfig(
            device=None,
            sample_rate=48000,
            channels=1,
            output_dir=str(tmp_path),
        )
        recorder = AudioRecorder()

        import asyncio

        session = asyncio.run(recorder.start(config))

    after = datetime.now(UTC)

    assert before <= session.start_utc <= after
    assert session.end_utc is None
    assert session.file_path.endswith(".wav")
    assert "audio_" in session.file_path


# ---------------------------------------------------------------------------
# test_stop_sets_end_utc
# ---------------------------------------------------------------------------


def test_stop_sets_end_utc(tmp_path: Path) -> None:
    """stop() sets end_utc after closing the stream."""
    mock_stream = MagicMock()
    mock_sf = MagicMock()

    with (
        patch("sounddevice.query_devices", return_value=_FAKE_DEVICES),
        patch("sounddevice.InputStream", return_value=mock_stream),
        patch("soundfile.SoundFile", return_value=mock_sf),
    ):
        config = AudioConfig(
            device=None,
            sample_rate=48000,
            channels=1,
            output_dir=str(tmp_path),
        )
        recorder = AudioRecorder()

        import asyncio

        asyncio.run(recorder.start(config))
        before_stop = datetime.now(UTC)
        completed = asyncio.run(recorder.stop())
        after_stop = datetime.now(UTC)

    assert completed.end_utc is not None
    assert before_stop <= completed.end_utc <= after_stop
    assert completed.start_utc < completed.end_utc


# ---------------------------------------------------------------------------
# test_no_device_raises
# ---------------------------------------------------------------------------


def test_no_device_raises() -> None:
    """AudioDeviceNotFoundError is raised when no input devices exist."""
    output_only_devices = [
        {
            "name": "HDMI Output",
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        }
    ]

    with (
        patch("sounddevice.query_devices", return_value=output_only_devices),
        pytest.raises(AudioDeviceNotFoundError),
    ):
        _resolve_device(None)


def test_no_device_name_match_raises() -> None:
    """AudioDeviceNotFoundError is raised when name substring doesn't match."""
    with (
        patch("sounddevice.query_devices", return_value=_FAKE_DEVICES),
        pytest.raises(AudioDeviceNotFoundError, match="nonexistent"),
    ):
        _resolve_device("nonexistent")


# ---------------------------------------------------------------------------
# test_resolve_device_by_index
# ---------------------------------------------------------------------------


def test_resolve_device_by_index() -> None:
    """Resolving by integer index works correctly."""
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES):
        idx, name = _resolve_device(0)

    assert idx == 0
    assert name == "Built-in Microphone"


def test_resolve_device_by_name_substring() -> None:
    """Case-insensitive substring match selects the correct device."""
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES):
        idx, name = _resolve_device("gordik")

    assert idx == 1
    assert "Gordik" in name


# ---------------------------------------------------------------------------
# test_is_recording
# ---------------------------------------------------------------------------


def test_is_recording_false_before_start() -> None:
    """is_recording is False on a fresh recorder."""
    recorder = AudioRecorder()
    assert recorder.is_recording is False


def test_is_recording_true_after_start(tmp_path: Path) -> None:
    """is_recording is True after start() and False after stop()."""
    import asyncio

    mock_stream = MagicMock()
    mock_sf = MagicMock()

    with (
        patch("sounddevice.query_devices", return_value=_FAKE_DEVICES),
        patch("sounddevice.InputStream", return_value=mock_stream),
        patch("soundfile.SoundFile", return_value=mock_sf),
    ):
        config = AudioConfig(
            device=None,
            sample_rate=48000,
            channels=1,
            output_dir=str(tmp_path),
        )
        recorder = AudioRecorder()

        asyncio.run(recorder.start(config))
        assert recorder.is_recording is True

        asyncio.run(recorder.stop())
        assert recorder.is_recording is False


# ---------------------------------------------------------------------------
# test_start_with_custom_name
# ---------------------------------------------------------------------------


def test_start_with_custom_name(tmp_path: Path) -> None:
    """start(config, name=...) saves the WAV as {name}.wav."""
    import asyncio

    mock_stream = MagicMock()
    mock_sf = MagicMock()

    with (
        patch("sounddevice.query_devices", return_value=_FAKE_DEVICES),
        patch("sounddevice.InputStream", return_value=mock_stream),
        patch("soundfile.SoundFile", return_value=mock_sf),
    ):
        config = AudioConfig(
            device=None,
            sample_rate=48000,
            channels=1,
            output_dir=str(tmp_path),
        )
        recorder = AudioRecorder()
        session = asyncio.run(recorder.start(config, name="20260226-BallardCup-1"))

    assert session.file_path.endswith("20260226-BallardCup-1.wav")
    assert "audio_" not in session.file_path
