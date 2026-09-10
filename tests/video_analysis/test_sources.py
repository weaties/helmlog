"""Tests for source resolution (scripts/analysis/video/sources.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from scripts.analysis.video import sources
from scripts.analysis.video.common import VideoRef

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

SYNC = datetime(2026, 9, 10, 1, 17, 7, tzinfo=UTC)


def test_local_export_path_from_title(tmp_path: Path) -> None:
    v = VideoRef("jygj-NbqFJE", "u", "VID 20260909 181623 00 002", SYNC, 0.0, 1976.0)
    assert sources.local_export_path(v, tmp_path) is None
    f = tmp_path / "VID_20260909_181623_00_002.mp4"
    f.write_bytes(b"")
    assert sources.local_export_path(v, tmp_path) == f
    # Titles that are not the source filename never match a local file.
    other = VideoRef("x", "u", "Race 3", SYNC, 0.0, None)
    assert sources.local_export_path(other, tmp_path) is None


def test_resolve_prefers_local_and_records(
    ledger: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INSTA360_EXPORTS", str(tmp_path))
    monkeypatch.setenv("VIDEO_ANALYSIS_DIR", str(tmp_path / "va"))
    (tmp_path / "VID_20260909_181623_00_002.mp4").write_bytes(b"")
    monkeypatch.setattr(sources, "probe", lambda p: (7680, 3840, 29.97, 1975.9))
    v = VideoRef("jygj-NbqFJE", "u", "VID 20260909 181623 00 002", SYNC, 0.0, 1976.0)
    src = sources.resolve_source(ledger, v)
    assert src.kind == "local" and src.width == 7680
    row = ledger.execute("SELECT kind, path FROM sources WHERE video_id = 'jygj-NbqFJE'").fetchone()
    assert row["kind"] == "local"
    # Second call is served from the ledger without probing.
    monkeypatch.setattr(sources, "probe", lambda p: (_ for _ in ()).throw(AssertionError("probed")))
    assert sources.resolve_source(ledger, v).path == src.path


def test_resolve_downloads_when_no_local(
    ledger: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INSTA360_EXPORTS", str(tmp_path / "none"))
    monkeypatch.setenv("VIDEO_ANALYSIS_DIR", str(tmp_path / "va"))
    calls: list[str] = []

    def fake_download(video_id: str, target: Path) -> None:
        calls.append(video_id)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"")

    monkeypatch.setattr(sources, "download_youtube", fake_download)
    monkeypatch.setattr(sources, "probe", lambda p: (3840, 1920, 30.0, 3000.0))
    v = VideoRef("klXMihwZJS0", "u", "VID 20260408 183010 00 105 106", SYNC, 0.0, 3000.0)
    with pytest.raises(FileNotFoundError):
        sources.resolve_source(ledger, v, download=False)
    src = sources.resolve_source(ledger, v)
    assert src.kind == "youtube" and calls == ["klXMihwZJS0"]
    assert src.path == tmp_path / "va" / "sources" / "klXMihwZJS0" / "klXMihwZJS0.webm"
