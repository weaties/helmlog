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
    AudioRecorderGroup,
    _resolve_device,
    capture_start,
)
from helmlog.usb_audio import DetectedDevice

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
        idx, name, max_ch = _resolve_device(0)

    assert idx == 0
    assert name == "Built-in Microphone"
    assert max_ch == 2


def test_resolve_device_by_name_substring() -> None:
    """Case-insensitive substring match selects the correct device."""
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES):
        idx, name, max_ch = _resolve_device("gordik")

    assert idx == 1
    assert "Gordik" in name
    assert max_ch == 1


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


def test_start_with_detected_device_uses_multichannel(tmp_path: Path) -> None:
    """A DetectedDevice overrides config and stamps identity onto the session."""
    import asyncio

    from helmlog.usb_audio import DetectedDevice

    detected = DetectedDevice(
        vendor_id=0x1234,
        product_id=0x5678,
        serial="ABC",
        usb_port_path="1-1.2",
        max_channels=4,
        sounddevice_index=1,
        name="Lavalier 4-ch USB",
    )
    mock_stream = MagicMock()
    mock_sf = MagicMock()

    with (
        patch("sounddevice.InputStream", return_value=mock_stream),
        patch("soundfile.SoundFile", return_value=mock_sf),
    ):
        config = AudioConfig(
            device=None,
            sample_rate=48000,
            channels=1,  # ignored: detected.max_channels wins
            output_dir=str(tmp_path),
        )
        recorder = AudioRecorder()
        session = asyncio.run(recorder.start(config, detected=detected))

    assert session.channels == 4
    assert session.device_name == "Lavalier 4-ch USB"
    assert session.vendor_id == 0x1234
    assert session.product_id == 0x5678
    assert session.serial == "ABC"
    assert session.usb_port_path == "1-1.2"


def test_start_mono_fallback_has_zero_identity(tmp_path: Path) -> None:
    """Without a DetectedDevice, identity fields stay zero/empty."""
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
        session = asyncio.run(recorder.start(config))

    assert session.channels == 1
    assert session.vendor_id == 0
    assert session.product_id == 0
    assert session.serial == ""
    assert session.usb_port_path == ""


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


# ---------------------------------------------------------------------------
# AudioRecorderGroup — sibling-card capture (#509)
# ---------------------------------------------------------------------------


_FAKE_SIBLING_SD_DEVICES = [
    {
        "name": "USB Composite Device: Audio (hw:2,0)",
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48000.0,
    },
    {
        "name": "USB Composite Device: Audio (hw:3,0)",
        "max_input_channels": 1,
        "max_output_channels": 0,
        "default_samplerate": 48000.0,
    },
]


def _detected(idx: int, serial: str) -> DetectedDevice:
    return DetectedDevice(
        vendor_id=0x3634,
        product_id=0x4155,
        serial=serial,
        usb_port_path=f"1-{idx + 1}",
        max_channels=1,
        sounddevice_index=idx,
        name=f"USB Composite Device: Audio (hw:{idx + 2},0)",
    )


def test_audio_recorder_group_start_opens_n_siblings(tmp_path: Path) -> None:
    """Start() with 2 devices opens 2 InputStreams + 2 SoundFiles, one per card."""
    import asyncio

    mock_streams = [MagicMock(), MagicMock()]
    mock_soundfiles = [MagicMock(), MagicMock()]
    stream_iter = iter(mock_streams)
    sf_iter = iter(mock_soundfiles)

    with (
        patch("sounddevice.query_devices", return_value=_FAKE_SIBLING_SD_DEVICES),
        patch("sounddevice.InputStream", side_effect=lambda **kw: next(stream_iter)),
        patch("soundfile.SoundFile", side_effect=lambda *a, **kw: next(sf_iter)),
    ):
        config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
        group = AudioRecorderGroup()
        devices = [_detected(0, "AAA"), _detected(1, "BBB")]
        sessions = asyncio.run(
            group.start(config, devices=devices, name="20260412-multichannel-P1")
        )

    assert len(sessions) == 2
    # Distinct filenames so the two recorders don't collide.
    assert sessions[0].file_path != sessions[1].file_path
    assert sessions[0].file_path.endswith("-sib0.wav")
    assert sessions[1].file_path.endswith("-sib1.wav")

    # Shared capture_group_id, distinct ordinals.
    assert sessions[0].capture_group_id == sessions[1].capture_group_id
    assert sessions[0].capture_group_id is not None
    assert sessions[0].capture_ordinal == 0
    assert sessions[1].capture_ordinal == 1

    # Per-sibling USB identity carried through.
    assert sessions[0].serial == "AAA"
    assert sessions[1].serial == "BBB"
    assert sessions[0].channels == 1
    assert sessions[1].channels == 1

    # Both streams were started.
    assert mock_streams[0].start.called
    assert mock_streams[1].start.called
    assert group.is_recording


def test_audio_recorder_group_stop_stops_all_siblings(tmp_path: Path) -> None:
    """Stop() closes every sibling's stream and SoundFile, preserves group metadata."""
    import asyncio

    mock_streams = [MagicMock(), MagicMock()]
    mock_soundfiles = [MagicMock(), MagicMock()]
    stream_iter = iter(mock_streams)
    sf_iter = iter(mock_soundfiles)

    with (
        patch("sounddevice.query_devices", return_value=_FAKE_SIBLING_SD_DEVICES),
        patch("sounddevice.InputStream", side_effect=lambda **kw: next(stream_iter)),
        patch("soundfile.SoundFile", side_effect=lambda *a, **kw: next(sf_iter)),
    ):
        config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
        group = AudioRecorderGroup()
        asyncio.run(
            group.start(
                config,
                devices=[_detected(0, "AAA"), _detected(1, "BBB")],
                name="20260412-race",
            )
        )
        completed = asyncio.run(group.stop())

    assert len(completed) == 2
    assert not group.is_recording
    # Group metadata preserved through stop().
    assert completed[0].capture_group_id == completed[1].capture_group_id
    assert completed[0].capture_ordinal == 0
    assert completed[1].capture_ordinal == 1
    # end_utc set on every sibling.
    for s in completed:
        assert s.end_utc is not None
    # Both streams + files closed.
    for m in mock_streams:
        assert m.stop.called
        assert m.close.called
    for m in mock_soundfiles:
        assert m.close.called


def test_audio_recorder_group_start_rejects_empty_device_list(tmp_path: Path) -> None:
    import asyncio

    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
    group = AudioRecorderGroup()
    with pytest.raises(AudioDeviceNotFoundError):
        asyncio.run(group.start(config, devices=[], name="x"))


def test_audio_recorder_group_start_cleans_up_on_sibling_failure(tmp_path: Path) -> None:
    """If the second sibling fails to open, the first must be stopped cleanly."""
    import asyncio

    mock_first_stream = MagicMock()
    mock_first_sf = MagicMock()

    def input_stream_side_effect(**_kwargs: object) -> MagicMock:
        if input_stream_side_effect.calls == 0:
            input_stream_side_effect.calls += 1
            return mock_first_stream
        raise RuntimeError("device busy")

    input_stream_side_effect.calls = 0  # type: ignore[attr-defined]

    sf_iter = iter([mock_first_sf, MagicMock()])

    with (
        patch("sounddevice.query_devices", return_value=_FAKE_SIBLING_SD_DEVICES),
        patch("sounddevice.InputStream", side_effect=input_stream_side_effect),
        patch("soundfile.SoundFile", side_effect=lambda *a, **kw: next(sf_iter)),
    ):
        config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))
        group = AudioRecorderGroup()
        with pytest.raises(RuntimeError, match="device busy"):
            asyncio.run(
                group.start(
                    config,
                    devices=[_detected(0, "AAA"), _detected(1, "BBB")],
                    name="x",
                )
            )

    # First sibling was cleaned up even though start() raised.
    assert mock_first_stream.stop.called
    assert mock_first_stream.close.called
    assert not group.is_recording


def test_capture_start_retries_device_detection_after_stop(tmp_path: Path) -> None:
    """#762: when /debrief/start fires right after /end's capture_stop,
    the USB capture devices may briefly enumerate to [] before the
    kernel releases them. capture_start retries detection a few times
    before failing — covers the chained finish-race → debrief flow."""
    import asyncio
    from unittest.mock import AsyncMock

    config = AudioConfig(device=None, sample_rate=48000, channels=1, output_dir=str(tmp_path))

    # First three detection calls return empty (devices still busy), then
    # the fourth returns a single device.
    fake_device = DetectedDevice(
        vendor_id=0x1234,
        product_id=0x5678,
        serial="serialX",
        usb_port_path="usb-0",
        max_channels=2,
        sounddevice_index=0,
        name="usb-pcm-card",
    )
    detect_calls: list[list] = [[], [], [], [fake_device]]
    real_detect = lambda *_a, **_kw: detect_calls.pop(0)  # noqa: E731

    group = MagicMock(spec=AudioRecorderGroup)
    group.start = AsyncMock(
        return_value=[
            # mimic AudioSession enough that write_audio_session is called once
            MagicMock(file_path="x.wav", channel_count=2, ordinal=0)
        ]
    )

    storage = MagicMock()
    storage.write_audio_session = AsyncMock(return_value=42)

    with patch("helmlog.usb_audio.detect_all_capture_devices", side_effect=real_detect):
        primary = asyncio.run(
            capture_start(
                group,
                config,
                storage,
                name="race-1-debrief",
                race_id=1,
                session_type="debrief",
            )
        )

    assert primary == 42
    # Detection retried until devices came back; recorder.start got the device.
    assert group.start.await_count == 1
    _args, kwargs = group.start.await_args
    assert kwargs["devices"] == [fake_device]
    assert len(detect_calls) == 0  # all four detection responses were consumed
