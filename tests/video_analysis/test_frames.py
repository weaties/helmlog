"""Tests for frame extraction and strip rendering (scripts/analysis/video/frames.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image
from scripts.analysis.video import common, frames

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    import pytest


def make_frame(path: Path, w: int = 1440, h: int = 720) -> None:
    """Grey sky/sea with a red hull marker at relative bearing +90 on the horizon."""
    im = Image.new("RGB", (w, h), (120, 160, 200))
    x = common.rel_bearing_to_x(90.0, w)
    y = h // 2
    for dx in range(-4, 5):
        for dy in range(-4, 5):
            im.putpixel((x + dx, y + dy), (255, 0, 0))
    im.save(path, quality=95)


def test_horizon_rows_scale_with_height() -> None:
    assert frames.horizon_rows(3840) == (1612, 2227)  # matches the pilot's 1620–2220 band
    assert frames.horizon_rows(2160) == (907, 1252)


def test_ensure_frame_is_idempotent(
    ledger: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_ANALYSIS_DIR", str(tmp_path / "va"))
    calls: list[float] = []

    def fake_extract(source: Path, video_t: float, out: Path) -> None:
        calls.append(video_t)
        out.parent.mkdir(parents=True, exist_ok=True)
        make_frame(out)

    monkeypatch.setattr(frames, "extract_frame", fake_extract)
    p1 = frames.ensure_frame(ledger, "vid", tmp_path / "src.mp4", 468.04)
    p2 = frames.ensure_frame(ledger, "vid", tmp_path / "src.mp4", 468.0)
    assert p1 == p2 and calls == [468.0]
    row = ledger.execute("SELECT width, height FROM frames WHERE video_id = 'vid'").fetchone()
    assert (row["width"], row["height"]) == (1440, 720)
    assert p1 == tmp_path / "va" / "frames" / "vid" / "468.0.jpg"


def test_render_strips_geometry_and_marker_quadrant(tmp_path: Path) -> None:
    frame = tmp_path / "f.jpg"
    make_frame(frame)
    st = common.Stamp(hdg=337.0, sog=4.7, bsp=4.5, twa=55.0, tws=8.6, twd=32.0, lat=None, lon=None)
    header = frames.stamp_text("race 254 gun", "01:25:01", 468.0, st)
    assert "HDG 337" in header and "TWD 032" in header and "TWA +55" in header
    out = frames.render_strips(frame, st, header, tmp_path / "strips", "254_gun")
    assert out.composite.exists() and out.thumb.exists()
    assert all(q.exists() for q in out.quadrants)
    with Image.open(out.composite) as im:
        w, h = im.size
    assert w == frames.STRIP_W
    band = int((frames.horizon_rows(720)[1] - frames.horizon_rows(720)[0]) * (frames.STRIP_W / 360))
    assert h == frames.HEADER_H + 4 * (band + frames.LABEL_H)
    # The +90 marker sits at the left edge of the STBD QUARTER strip (rel +90..+180).
    with Image.open(out.quadrants[3]) as q3:
        px = q3.load()
        y = frames.LABEL_H + band // 2
        reds = [x for x in range(0, 60) if px[x, y][0] > 200 and px[x, y][1] < 80]
    assert reds, "marker missing from the starboard-quarter strip"
    with Image.open(out.quadrants[1]) as q1:
        px = q1.load()
        assert not any(px[x, y][0] > 200 and px[x, y][1] < 80 for x in range(frames.STRIP_W))


def test_stamp_text_handles_missing_telemetry() -> None:
    st = common.Stamp(None, None, None, None, None, None, None, None)
    assert "HDG ?" in frames.stamp_text("x", "00:00:00", 1.0, st)


def test_view_command_points_v360_at_relative_bearing(tmp_path: Path) -> None:
    cmd = frames.view_command(tmp_path / "src.mp4", 468.0, 110.0, tmp_path / "v.jpg")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("v360=e:flat:yaw=110.0:pitch=0:h_fov=90:v_fov=60")
    assert cmd[cmd.index("-ss") + 1] == "468.00"
