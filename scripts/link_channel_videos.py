#!/usr/bin/env python3
"""Match YouTube channel videos to Gaia GPS-imported races and link via API.

Usage:
    uv run scripts/link_channel_videos.py --channel-id UC5atMPIm9wXKp393BJj0fdw
    uv run scripts/link_channel_videos.py --channel-id UC5atMPIm9wXKp393BJj0fdw --yes

Requires a valid session cookie (PI_SESSION_COOKIE env var or --cookie).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

PACIFIC = ZoneInfo("America/Los_Angeles")

# Title patterns: "VID YYYYMMDD HHMMSS ...", "YYYYMMDD HHMMSS ...",
#                  "YYYYMMDD ...", "YYMMDD ..."
_DATE_TIME_RE = re.compile(r"(?:VID\s+)?(\d{8})\s+(\d{6})\b", re.IGNORECASE)
_DATE_ONLY_RE = re.compile(r"(?:VID\s+)?(\d{8})\b", re.IGNORECASE)
_SHORT_DATE_RE = re.compile(r"^(\d{6})\b")  # YYMMDD
_ROUND_RE = re.compile(r"\b[rd](\d+)\b", re.IGNORECASE)


def fetch_channel_videos(channel_id: str) -> list[dict[str, object]]:
    """Fetch all videos from a YouTube channel via yt-dlp Python API."""
    import yt_dlp  # type: ignore[import-untyped]

    ydl_opts: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/channel/{channel_id}/videos",
            download=False,
        )

    if not info or "entries" not in info:
        return []

    videos: list[dict[str, object]] = []
    for entry in info["entries"]:
        if entry:
            videos.append(
                {
                    "id": entry.get("id", ""),
                    "title": entry.get("title", ""),
                    "duration": entry.get("duration"),
                }
            )
    return videos


def fetch_sessions(
    base_url: str, cookie: str, from_date: str, to_date: str
) -> list[dict[str, object]]:
    """Fetch all sessions from the Pi API."""
    sessions: list[dict[str, object]] = []
    offset = 0
    while True:
        resp = httpx.get(
            f"{base_url}/api/sessions",
            params={
                "from_date": from_date,
                "to_date": to_date,
                "limit": 200,
                "offset": offset,
            },
            cookies={"session": cookie},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("sessions", [])
        sessions.extend(batch)
        if len(batch) < 200:
            break
        offset += 200
    return sessions


def fetch_existing_videos(base_url: str, cookie: str, session_id: int) -> list[dict[str, object]]:
    """Get videos already linked to a session."""
    resp = httpx.get(
        f"{base_url}/api/sessions/{session_id}/videos",
        cookies={"session": cookie},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def parse_video_date_time(title: str) -> tuple[str | None, datetime | None]:
    """Extract YYYYMMDD date string and optional UTC datetime from a video title."""
    m = _DATE_TIME_RE.search(title)
    if m:
        date_str = m.group(1)
        try:
            local_dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y%m%d %H%M%S").replace(
                tzinfo=PACIFIC
            )
            return date_str, local_dt.astimezone(UTC)
        except ValueError:
            return date_str, None

    m2 = _DATE_ONLY_RE.search(title)
    if m2:
        return m2.group(1), None

    m3 = _SHORT_DATE_RE.search(title)
    if m3:
        return "20" + m3.group(1), None

    return None, None


def _extract_round_tag(text: str) -> str | None:
    """Extract a normalised round tag like 'r1', 'r2', 'd1r3' from text.

    Looks for patterns: r1, r2, d1r3, d2 r1, etc.
    Returns the most specific tag found, or None.
    """
    text = text.lower()
    # "d1r3", "d1 r3", "d2r1", "d2-r1"
    m = re.search(r"\bd(\d+)[\s\-]*r(\d+)\b", text)
    if m:
        return f"d{m.group(1)}r{m.group(2)}"
    # Standalone "r1", "r2", etc.
    m = re.search(r"\br(\d+)\b", text)
    if m:
        return f"r{m.group(1)}"
    return None


def best_race_match(
    video_title: str,
    candidates: list[dict[str, object]],
    vid_utc: datetime | None,
) -> dict[str, object] | None:
    """Pick the best matching race from candidates using keyword overlap + time proximity."""
    title_lower = video_title.lower()
    title_words = set(re.findall(r"[a-z]{3,}", title_lower))
    vid_round = _extract_round_tag(title_lower)

    best: dict[str, object] | None = None
    best_score = -1.0

    for race in candidates:
        event_lower = str(race.get("event", "")).lower()
        event_words = set(re.findall(r"[a-z]{3,}", event_lower))
        race_round = _extract_round_tag(event_lower)

        # Keyword overlap score
        common = title_words & event_words
        score = len(common) * 2.0

        # Round tag matching — reward exact match, penalise mismatch
        if vid_round and race_round:
            if vid_round == race_round:
                score += 10.0
            else:
                # Partial match: same round number but different day, or vice versa
                vid_r = re.search(r"r(\d+)", vid_round)
                race_r = re.search(r"r(\d+)", race_round)
                if vid_r and race_r and vid_r.group(1) == race_r.group(1):
                    score += 3.0
                else:
                    score -= 3.0

        # Time proximity bonus — only when we have a parsed video timestamp
        if vid_utc:
            start_str = str(race.get("start_utc", ""))
            if start_str:
                race_start = datetime.fromisoformat(start_str)
                if race_start.tzinfo is None:
                    race_start = race_start.replace(tzinfo=UTC)
                diff_s = abs((vid_utc - race_start).total_seconds())
                if diff_s < 1800:
                    score += 10.0
                elif diff_s < 7200:
                    score += 5.0
                elif diff_s < 14400:
                    score += 2.0

        if score > best_score:
            best_score = score
            best = race

    return best if best_score >= 2.0 else None


def link_video(
    base_url: str,
    cookie: str,
    session_id: int,
    youtube_url: str,
    sync_utc: str,
    label: str = "youtube",
) -> dict[str, object]:
    """POST a video link to the Pi API."""
    resp = httpx.post(
        f"{base_url}/api/sessions/{session_id}/videos",
        json={
            "youtube_url": youtube_url,
            "sync_utc": sync_utc,
            "sync_offset_s": 0.0,
            "label": label,
        },
        cookies={"session": cookie},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", required=True, help="YouTube channel ID")
    parser.add_argument("--base-url", default="http://corvopi-tst1", help="Pi API base URL")
    parser.add_argument("--cookie", default="", help="Session cookie (or PI_SESSION_COOKIE env)")
    parser.add_argument("--yes", action="store_true", help="Write matches (otherwise dry run)")
    parser.add_argument(
        "--min-duration", type=int, default=120, help="Skip videos shorter than N sec"
    )
    args = parser.parse_args()

    import os

    cookie = args.cookie or os.environ.get("PI_SESSION_COOKIE", "")
    if not cookie:
        print("No session cookie. Use --cookie or set PI_SESSION_COOKIE.")
        sys.exit(1)

    # 1. Fetch all YouTube videos
    logger.info("Fetching videos from channel {}...", args.channel_id)
    yt_videos = fetch_channel_videos(args.channel_id)
    logger.info("Found {} videos on channel.", len(yt_videos))

    # 2. Fetch all sessions from the API (Gaia imports span 2024-2025)
    logger.info("Fetching sessions from {}...", args.base_url)
    sessions = fetch_sessions(args.base_url, cookie, "2024-01-01", "2025-12-31")
    logger.info("Found {} sessions.", len(sessions))

    if not sessions:
        print("No sessions found.")
        return

    # 3. Collect already-linked video IDs
    logger.info("Checking existing video links...")
    already_linked: set[str] = set()
    for sess in sessions:
        sid = int(str(sess["id"]))
        existing = fetch_existing_videos(args.base_url, cookie, sid)
        for v in existing:
            vid_id = str(v.get("video_id", ""))
            if vid_id:
                already_linked.add(vid_id)
    logger.info("{} videos already linked.", len(already_linked))

    # 4. Match videos to sessions
    matches: list[tuple[dict[str, object], dict[str, object], str]] = []
    unmatched: list[dict[str, object]] = []

    for vid in yt_videos:
        vid_id = str(vid.get("id", ""))
        title = str(vid.get("title", ""))
        duration = float(str(vid.get("duration") or 0))

        if vid_id in already_linked:
            continue

        if duration < args.min_duration:
            continue

        vid_date_str, vid_utc = parse_video_date_time(title)
        if not vid_date_str:
            unmatched.append(vid)
            continue

        try:
            vd = datetime.strptime(vid_date_str, "%Y%m%d").date()
        except ValueError:
            unmatched.append(vid)
            continue

        # Find sessions on the same date (accounting for UTC vs Pacific offset)
        candidates = []
        for s in sessions:
            s_date = str(s.get("date", ""))
            if not s_date:
                continue
            try:
                sd = date.fromisoformat(s_date)
            except ValueError:
                continue
            if sd in (vd, vd + timedelta(days=1)):
                candidates.append(s)

        if not candidates:
            unmatched.append(vid)
            continue

        best = best_race_match(title, candidates, vid_utc)
        if best:
            reason = "date+time" if vid_utc else "date+keywords"
            matches.append((vid, best, reason))
        else:
            unmatched.append(vid)

    # 5. Print report
    if matches:
        print(f"\n{'Video Title':<55} {'Race Event':<35} {'Match'}")
        print("-" * 100)
        for vid, race, reason in matches:
            vt = str(vid.get("title", ""))[:55]
            re_ = str(race.get("event", ""))[:35]
            print(f"{vt:<55} {re_:<35} {reason}")

    if unmatched:
        print(f"\n--- {len(unmatched)} unmatched video(s) ---")
        for vid in unmatched[:15]:
            print(f"  {vid.get('title')}")
        if len(unmatched) > 15:
            print(f"  ... and {len(unmatched) - 15} more")

    print(
        f"\n{len(matches)} match(es), {len(unmatched)} unmatched,"
        f" {len(already_linked)} already linked."
    )

    if not args.yes:
        print("\nDry run — use --yes to write these links.")
        return

    if not matches:
        return

    # 6. Link via API
    linked = 0
    for vid, race, _reason in matches:
        vid_id = str(vid.get("id", ""))
        vid_url = f"https://www.youtube.com/watch?v={vid_id}"
        session_id = int(str(race["id"]))
        sync_utc = str(race.get("start_utc", ""))

        try:
            link_video(args.base_url, cookie, session_id, vid_url, sync_utc)
            linked += 1
            logger.info("Linked: {} → session {}", vid.get("title"), session_id)
        except httpx.HTTPStatusError as exc:
            logger.warning("Failed to link {}: {}", vid.get("title"), exc.response.text)

    print(f"\n{linked} video(s) linked to sessions.")


if __name__ == "__main__":
    main()
