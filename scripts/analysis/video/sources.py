"""Resolve a race video to a local file: the 8K stitched export when it exists,
otherwise a full ≤4K VP9 download from YouTube kept under the sidecar.

Full downloads rather than section downloads: at YouTube's 4K the whole race
is 1.5–3.5 GB and section downloads re-fetch keyframes on every new instant.

    uv run python -m scripts.analysis.video fetch --race 254 [--race 255 ...]
    uv run python -m scripts.analysis.video fetch --season
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.analysis.video import common

if TYPE_CHECKING:
    import sqlite3

YTDLP_FORMAT = "bestvideo[height<=2160][vcodec^=vp9]+bestaudio[ext=webm]/best[height<=2160]"


@dataclass(frozen=True)
class Source:
    video_id: str
    kind: str  # local | youtube
    path: Path
    width: int
    height: int
    fps: float
    duration_s: float


def local_export_path(video: common.VideoRef, exports: Path | None = None) -> Path | None:
    """The stitched 8K file matching the video title (``VID 20260909 181623 00 002``)."""
    base = exports if exports is not None else common.exports_dir()
    title = video.title.strip()
    if not title.startswith("VID "):
        return None
    candidate = base / (title.replace(" ", "_") + ".mp4")
    return candidate if candidate.exists() else None


def youtube_target(video_id: str) -> Path:
    return common.va_dir() / "sources" / video_id / f"{video_id}.webm"


def probe(path: Path) -> tuple[int, int, float, float]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,duration",
            "-show_entries", "format=duration", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    info = json.loads(out)
    st = info["streams"][0]
    num, _, den = str(st.get("r_frame_rate", "30/1")).partition("/")
    fps = float(num) / float(den or 1)
    dur = st.get("duration") or info.get("format", {}).get("duration") or 0.0
    return int(st["width"]), int(st["height"]), fps, float(dur)


def download_youtube(video_id: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "yt-dlp", "--socket-timeout", "30", "--retries", "10", "--fragment-retries", "10",
            "--no-warnings", "-q", "--progress", "--newline",
            "-f", YTDLP_FORMAT, "--merge-output-format", "webm",
            "-o", str(target.parent / f"{video_id}.%(ext)s"), f"https://youtu.be/{video_id}",
        ],
        check=True,
    )  # fmt: skip
    if not target.exists():
        raise FileNotFoundError(f"yt-dlp finished but {target} is missing")


def _record(ledger: sqlite3.Connection, src: Source) -> None:
    ledger.execute(
        "INSERT OR REPLACE INTO sources (video_id, kind, path, width, height, fps, duration_s,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        (src.video_id, src.kind, str(src.path), src.width, src.height, src.fps, src.duration_s,
         common.utcnow_iso()),
    )  # fmt: skip
    ledger.commit()


def resolve_source(
    ledger: sqlite3.Connection, video: common.VideoRef, download: bool = True
) -> Source:
    """Return the on-disk source for a video, downloading from YouTube if allowed."""
    row = ledger.execute("SELECT * FROM sources WHERE video_id = ?", (video.video_id,)).fetchone()
    if row is not None and Path(row["path"]).exists():
        return Source(
            row["video_id"], row["kind"], Path(row["path"]),
            int(row["width"]), int(row["height"]), float(row["fps"]), float(row["duration_s"]),
        )  # fmt: skip
    local = local_export_path(video)
    if local is not None:
        kind, path = "local", local
    else:
        path = youtube_target(video.video_id)
        if not path.exists():
            if not download:
                raise FileNotFoundError(f"no source for {video.video_id}; run fetch first")
            download_youtube(video.video_id, path)
        kind = "youtube"
    w, h, fps, dur = probe(path)
    src = Source(video.video_id, kind, path, w, h, fps, dur)
    _record(ledger, src)
    return src


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--race", type=int, action="append", default=[], help="race id (repeatable)")
    ap.add_argument("--season", action="store_true", help="every CYC Wednesday race with video")
    ap.add_argument("--no-download", action="store_true", help="only resolve, never download")
    args = ap.parse_args(argv)
    ledger = common.open_ledger()
    meta = common.open_ro(common.meta_db_path())
    races = [common.load_race(meta, r) for r in args.race]
    if args.season:
        races += common.list_cyc_wednesday_races(meta)
    failures = 0
    for race in races:
        if race.video is None:
            print(f"race {race.id}: no video")
            continue
        try:
            src = resolve_source(ledger, race.video, download=not args.no_download)
            print(
                f"race {race.id}: {src.kind} {src.width}x{src.height} {src.duration_s:.0f}s {src.path}"
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            failures += 1
            print(f"race {race.id}: FAILED — {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
