"""Unit tests for logger.transcribe — _merge, diarisation helpers, and remote offload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from logger.transcribe import _merge, _try_remote_transcribe

# ---------------------------------------------------------------------------
# _merge tests (existing)
# ---------------------------------------------------------------------------


def test_merge_assigns_speaker_by_midpoint() -> None:
    """Midpoint of a whisper segment falling in speaker A's interval → SPEAKER_00."""
    whisper_segs = [(0.0, 3.0, "Ready about.")]
    diar_segs = [(0.0, 3.5, "A"), (3.5, 7.0, "B")]
    result = _merge(whisper_segs, diar_segs)
    assert len(result) == 1
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[0]["text"] == "Ready about."
    assert result[0]["start"] == 0.0
    assert result[0]["end"] == 3.0


def test_merge_normalises_labels() -> None:
    """Two unique raw speaker labels map to SPEAKER_00 and SPEAKER_01."""
    whisper_segs = [(0.0, 3.0, "Hello."), (3.5, 6.0, "World.")]
    diar_segs = [(0.0, 3.2, "X"), (3.3, 6.5, "Y")]
    result = _merge(whisper_segs, diar_segs)
    assert result[0]["speaker"] == "SPEAKER_00"
    assert result[1]["speaker"] == "SPEAKER_01"


def test_merge_empty_diarizer() -> None:
    """With no diarisation intervals all segments fall back to SPEAKER_00."""
    whisper_segs = [(0.0, 2.0, "One."), (2.1, 4.0, "Two.")]
    result = _merge(whisper_segs, [])
    assert all(seg["speaker"] == "SPEAKER_00" for seg in result)
    assert len(result) == 2


def test_merge_returns_all_fields() -> None:
    """Each returned dict has start, end, speaker, and text keys."""
    whisper_segs = [(1.0, 2.5, "Tack!")]
    diar_segs = [(0.5, 3.0, "A")]
    result = _merge(whisper_segs, diar_segs)
    assert set(result[0].keys()) == {"start", "end", "speaker", "text"}


def test_merge_nearest_fallback() -> None:
    """Midpoint outside all diarisation intervals uses nearest interval."""
    # whisper segment at 10–12 s; diar covers 0–3 only
    whisper_segs = [(10.0, 12.0, "Late.")]
    diar_segs = [(0.0, 3.0, "A")]
    result = _merge(whisper_segs, diar_segs)
    # Nearest is the only interval; should still be normalised SPEAKER_00
    assert result[0]["speaker"] == "SPEAKER_00"


# ---------------------------------------------------------------------------
# Remote transcription offload tests
# ---------------------------------------------------------------------------

_SEGMENTS = [{"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00", "text": "Ready about."}]


@pytest.mark.asyncio
async def test_try_remote_returns_none_when_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TRANSCRIBE_URL is not set, _try_remote_transcribe returns None."""
    monkeypatch.delenv("TRANSCRIBE_URL", raising=False)
    result = await _try_remote_transcribe("audio.wav", "base", diarize=True)
    assert result is None


@pytest.mark.asyncio
async def test_try_remote_returns_none_when_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TRANSCRIBE_URL is empty string, returns None."""
    monkeypatch.setenv("TRANSCRIBE_URL", "")
    result = await _try_remote_transcribe("audio.wav", "base", diarize=True)
    assert result is None


@pytest.mark.asyncio
async def test_try_remote_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful remote transcription returns (text, segments)."""
    monkeypatch.setenv("TRANSCRIBE_URL", "http://testmac:8321")
    response_data = {"text": "Ready about.", "segments": _SEGMENTS}

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = response_data

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("builtins.open", MagicMock()),
    ):
        result = await _try_remote_transcribe("/data/audio/test.wav", "base", diarize=True)

    assert result is not None
    text, segments = result
    assert text == "Ready about."
    assert len(segments) == 1
    assert segments[0]["speaker"] == "SPEAKER_00"


@pytest.mark.asyncio
async def test_try_remote_fallback_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection error returns None (triggers local fallback)."""
    import httpx

    monkeypatch.setenv("TRANSCRIBE_URL", "http://unreachable:8321")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("builtins.open", MagicMock()),
    ):
        result = await _try_remote_transcribe("/data/audio/test.wav", "base", diarize=True)

    assert result is None


@pytest.mark.asyncio
async def test_try_remote_fallback_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout returns None (triggers local fallback)."""
    import httpx

    monkeypatch.setenv("TRANSCRIBE_URL", "http://slowmac:8321")

    mock_client = AsyncMock()
    mock_client.post.side_effect = httpx.ReadTimeout("Read timed out")
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("builtins.open", MagicMock()),
    ):
        result = await _try_remote_transcribe("/data/audio/test.wav", "base", diarize=True)

    assert result is None


@pytest.mark.asyncio
async def test_try_remote_fallback_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 500 from worker returns None (triggers local fallback)."""
    import httpx

    monkeypatch.setenv("TRANSCRIBE_URL", "http://brokenmac:8321")

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Server Error", request=MagicMock(), response=mock_response
    )

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("builtins.open", MagicMock()),
    ):
        result = await _try_remote_transcribe("/data/audio/test.wav", "base", diarize=True)

    assert result is None
