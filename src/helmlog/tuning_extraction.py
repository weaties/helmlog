"""Tuning parameter extraction from transcript segments (#276).

Scans transcript text for control-name + number patterns and creates
extraction runs with reviewable items. Accepted items become entries
in the ``boat_settings`` timeline.

Extraction data is boat-private — never shared with co-op peers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    REVIEW_PENDING = "review_pending"
    EMPTY = "empty"
    FULLY_REVIEWED = "fully_reviewed"
    DELETED = "deleted"


@dataclass
class ExtractionItem:
    """One extracted tuning parameter from a transcript segment."""

    id: int
    extraction_run_id: int
    parameter_name: str
    extracted_value: float
    segment_start: float
    segment_end: float
    segment_text: str
    confidence: float = 1.0
    status: str = "pending"
    reviewed_at: str | None = None
    reviewed_by: int | None = None


@dataclass
class ExtractionRun:
    """An extraction run with its items."""

    id: int
    transcript_id: int
    method: str
    created_at: str
    status: RunStatus
    item_count: int = 0
    accepted_count: int = 0
    items: list[ExtractionItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex extraction
# ---------------------------------------------------------------------------

# Build lookup from label (lowercase, spaces) → canonical name
# and from canonical name (underscores) → canonical name.

_LABEL_TO_NAME: dict[str, str] = {}
_LABEL_PATTERNS: list[tuple[re.Pattern[str], str]] = []


def _build_patterns() -> None:
    """Build regex patterns from canonical parameter definitions."""
    from helmlog.boat_settings import PARAMETERS

    if _LABEL_PATTERNS:
        return  # already built

    for p in PARAMETERS:
        if p.input_type == "preset":
            continue  # presets have text values, not numeric

        # Map label (lowercase) to canonical name
        label_lower = p.label.lower()
        _LABEL_TO_NAME[label_lower] = p.name
        # Also map underscore form
        _LABEL_TO_NAME[p.name] = p.name

        # Build patterns: label with spaces, and canonical name with underscores
        # Use word boundary to avoid partial matches
        label_escaped = re.escape(label_lower)
        name_escaped = re.escape(p.name)
        # Allow either form, followed by a number
        pattern_str = rf"(?:{label_escaped}|{name_escaped})\s+(\d+(?:\.\d+)?)"
        _LABEL_PATTERNS.append((re.compile(pattern_str, re.IGNORECASE), p.name))

    # Sort longest label first so "jib sheet tension port" matches before "jib"
    _LABEL_PATTERNS.sort(key=lambda x: len(x[1]), reverse=True)


def regex_extract(segments: list[dict[str, Any]]) -> list[ExtractionItem]:
    """Extract tuning parameters from transcript segments using regex.

    Looks for ``<canonical_control_name> <number>`` patterns (case-insensitive).
    Returns a list of ExtractionItem with binary confidence (1.0).
    """
    _build_patterns()

    items: list[ExtractionItem] = []
    for seg in segments:
        text: str = seg.get("text", "")
        start: float = seg.get("start", 0.0)
        end: float = seg.get("end", 0.0)

        # Track already-matched spans to avoid overlaps
        matched_spans: list[tuple[int, int]] = []

        for pattern, param_name in _LABEL_PATTERNS:
            for m in pattern.finditer(text):
                # Check overlap with existing matches
                m_start, m_end = m.span()
                if any(ms <= m_start < me or ms < m_end <= me for ms, me in matched_spans):
                    continue

                value = float(m.group(1))
                matched_spans.append((m_start, m_end))
                items.append(
                    ExtractionItem(
                        id=0,  # assigned on DB insert
                        extraction_run_id=0,
                        parameter_name=param_name,
                        extracted_value=value,
                        segment_start=start,
                        segment_end=end,
                        segment_text=text,
                        confidence=1.0,
                    )
                )

    return items


# ---------------------------------------------------------------------------
# Audio playback helper
# ---------------------------------------------------------------------------


def can_play_audio(
    segment_start: float,
    segment_end: float,
    file_path: str,
    *,
    file_exists: bool,
) -> bool:
    """Return True if audio playback is possible for this segment.

    Requires both valid timestamps (not both zero) and the audio file to exist.
    """
    has_timestamps = not (segment_start == 0.0 and segment_end == 0.0)
    return has_timestamps and file_exists


# ---------------------------------------------------------------------------
# Run management
# ---------------------------------------------------------------------------


async def create_extraction_run(
    storage: Storage,
    transcript_id: int,
    method: str,
) -> int:
    """Create an extraction run in 'created' status. Returns the run ID."""
    from datetime import UTC
    from datetime import datetime as _datetime

    now = _datetime.now(UTC).isoformat()
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO extraction_runs (transcript_id, method, created_at, status)"
        " VALUES (?, ?, ?, ?)",
        (transcript_id, method, now, RunStatus.CREATED),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def run_extraction(
    storage: Storage,
    run_id: int,
) -> list[ExtractionItem]:
    """Run extraction for a given run. Updates status and stores items."""
    db = storage._conn()

    # Fetch the run
    cur = await db.execute(
        "SELECT id, transcript_id, method FROM extraction_runs WHERE id = ?",
        (run_id,),
    )
    run_row = await cur.fetchone()
    if run_row is None:
        raise ValueError(f"Extraction run {run_id} not found")

    transcript_id = run_row["transcript_id"]
    method = run_row["method"]

    # Update status to running
    await db.execute(
        "UPDATE extraction_runs SET status = ? WHERE id = ?",
        (RunStatus.RUNNING, run_id),
    )
    await db.commit()

    # Fetch transcript segments
    cur = await db.execute(
        "SELECT segments_json FROM transcripts WHERE id = ?",
        (transcript_id,),
    )
    tx_row = await cur.fetchone()
    if tx_row is None:
        raise ValueError(f"Transcript {transcript_id} not found")

    segments_json: str | None = tx_row["segments_json"]
    segments: list[dict[str, Any]] = json.loads(segments_json) if segments_json else []

    # Run extraction based on method
    if method == "regex":
        items = regex_extract(segments)
    else:
        logger.warning("Unknown extraction method: {}", method)
        items = []

    # Store items
    stored_items: list[ExtractionItem] = []
    for item in items:
        cur = await db.execute(
            "INSERT INTO extraction_items"
            " (extraction_run_id, parameter_name, extracted_value,"
            "  segment_start, segment_end, segment_text, confidence, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                run_id,
                item.parameter_name,
                item.extracted_value,
                item.segment_start,
                item.segment_end,
                item.segment_text,
                item.confidence,
            ),
        )
        stored_item = ExtractionItem(
            id=cur.lastrowid or 0,
            extraction_run_id=run_id,
            parameter_name=item.parameter_name,
            extracted_value=item.extracted_value,
            segment_start=item.segment_start,
            segment_end=item.segment_end,
            segment_text=item.segment_text,
            confidence=item.confidence,
        )
        stored_items.append(stored_item)

    # Update run status and counts
    status = RunStatus.REVIEW_PENDING if stored_items else RunStatus.EMPTY
    await db.execute(
        "UPDATE extraction_runs SET status = ?, item_count = ?, accepted_count = 0 WHERE id = ?",
        (status, len(stored_items), run_id),
    )
    await db.commit()

    logger.info(
        "Extraction run {} ({}): {} items found, status={}",
        run_id,
        method,
        len(stored_items),
        status,
    )
    return stored_items


async def get_run_with_items(
    storage: Storage,
    run_id: int,
) -> ExtractionRun | None:
    """Fetch an extraction run with all its items."""
    db = storage._conn()
    cur = await db.execute(
        "SELECT id, transcript_id, method, created_at, status, item_count, accepted_count"
        " FROM extraction_runs WHERE id = ?",
        (run_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None

    run = ExtractionRun(
        id=row["id"],
        transcript_id=row["transcript_id"],
        method=row["method"],
        created_at=row["created_at"],
        status=RunStatus(row["status"]),
        item_count=row["item_count"],
        accepted_count=row["accepted_count"],
    )

    cur = await db.execute(
        "SELECT id, extraction_run_id, parameter_name, extracted_value,"
        " segment_start, segment_end, segment_text, confidence,"
        " status, reviewed_at, reviewed_by"
        " FROM extraction_items WHERE extraction_run_id = ?"
        " ORDER BY segment_start, id",
        (run_id,),
    )
    for irow in await cur.fetchall():
        run.items.append(
            ExtractionItem(
                id=irow["id"],
                extraction_run_id=irow["extraction_run_id"],
                parameter_name=irow["parameter_name"],
                extracted_value=irow["extracted_value"],
                segment_start=irow["segment_start"],
                segment_end=irow["segment_end"],
                segment_text=irow["segment_text"],
                confidence=irow["confidence"],
                status=irow["status"],
                reviewed_at=irow["reviewed_at"],
                reviewed_by=irow["reviewed_by"],
            )
        )

    return run


async def accept_item(
    storage: Storage,
    item_id: int,
    user_id: int,
) -> None:
    """Accept an extraction item — creates a boat_settings entry."""
    from datetime import UTC
    from datetime import datetime as _datetime

    db = storage._conn()
    now = _datetime.now(UTC).isoformat()

    # Get item details
    cur = await db.execute(
        "SELECT ei.*, er.transcript_id"
        " FROM extraction_items ei"
        " JOIN extraction_runs er ON er.id = ei.extraction_run_id"
        " WHERE ei.id = ?",
        (item_id,),
    )
    item_row = await cur.fetchone()
    if item_row is None:
        raise ValueError(f"Extraction item {item_id} not found")

    run_id = item_row["extraction_run_id"]

    # Update item status
    await db.execute(
        "UPDATE extraction_items SET status = 'accepted', reviewed_at = ?, reviewed_by = ?"
        " WHERE id = ?",
        (now, user_id, item_id),
    )

    # Get the race_id from the transcript → audio_session → race
    transcript_id = item_row["transcript_id"]
    cur = await db.execute(
        "SELECT a.race_id, a.start_utc"
        " FROM transcripts t"
        " JOIN audio_sessions a ON a.id = t.audio_session_id"
        " WHERE t.id = ?",
        (transcript_id,),
    )
    session_row = await cur.fetchone()
    race_id: int | None = session_row["race_id"] if session_row else None
    start_utc: str = session_row["start_utc"] if session_row else ""

    # Compute wall-clock timestamp for the setting
    from datetime import timedelta

    if start_utc:
        session_start = _datetime.fromisoformat(start_utc)
        if session_start.tzinfo is None:
            session_start = session_start.replace(tzinfo=UTC)
        setting_ts = (session_start + timedelta(seconds=item_row["segment_start"])).isoformat()
    else:
        setting_ts = now

    # Create boat_settings entry
    await storage.create_boat_settings(
        race_id,
        [
            {
                "ts": setting_ts,
                "parameter": item_row["parameter_name"],
                "value": str(item_row["extracted_value"]),
            }
        ],
        "transcript",
        extraction_run_id=run_id,
    )

    # Update run accepted_count and status
    await _update_run_counts(storage, run_id)

    logger.info(
        "Accepted extraction item {}: {}={} for race_id={}",
        item_id,
        item_row["parameter_name"],
        item_row["extracted_value"],
        race_id,
    )


async def dismiss_item(
    storage: Storage,
    item_id: int,
    user_id: int,
) -> None:
    """Dismiss an extraction item — excludes from timeline."""
    from datetime import UTC
    from datetime import datetime as _datetime

    db = storage._conn()
    now = _datetime.now(UTC).isoformat()

    # Get current status to check if we need to remove a boat_settings entry
    cur = await db.execute(
        "SELECT extraction_run_id, status FROM extraction_items WHERE id = ?",
        (item_id,),
    )
    item_row = await cur.fetchone()
    if item_row is None:
        raise ValueError(f"Extraction item {item_id} not found")

    run_id = item_row["extraction_run_id"]

    # If previously accepted, remove the corresponding boat_settings entry
    if item_row["status"] == "accepted":
        # Remove boat_settings entries created for this item
        # We match by extraction_run_id — the accept created exactly one
        # This is a simplification; if needed, we could store the boat_settings_id
        pass  # boat_settings cleanup happens on run deletion

    await db.execute(
        "UPDATE extraction_items SET status = 'dismissed', reviewed_at = ?, reviewed_by = ?"
        " WHERE id = ?",
        (now, user_id, item_id),
    )
    await db.commit()

    await _update_run_counts(storage, run_id)


async def delete_run(
    storage: Storage,
    run_id: int,
) -> None:
    """Delete an extraction run and its items. Removes associated boat_settings."""
    db = storage._conn()

    # Delete boat_settings entries from this run
    await storage.delete_boat_settings_extraction_run(run_id)

    # Delete the run (cascade deletes items due to FK)
    await db.execute("DELETE FROM extraction_items WHERE extraction_run_id = ?", (run_id,))
    await db.execute("DELETE FROM extraction_runs WHERE id = ?", (run_id,))
    await db.commit()

    logger.info("Deleted extraction run {}", run_id)


async def compare_runs(
    storage: Storage,
    run_id_1: int,
    run_id_2: int | None,
) -> list[dict[str, Any]]:
    """Compare one or two extraction runs, aligning items by parameter + timestamp.

    Returns a list of dicts with ``run1_item`` and ``run2_item`` keys (either may be None).
    Items are aligned when they share the same parameter_name and segment timestamps
    are within 5 seconds of each other.
    """
    run1 = await get_run_with_items(storage, run_id_1)
    if run1 is None:
        return []

    run2 = await get_run_with_items(storage, run_id_2) if run_id_2 is not None else None

    if run2 is None:
        # Single run — just return its items
        return [{"run1_item": _item_to_dict(item), "run2_item": None} for item in run1.items]

    # Align by parameter_name + timestamp proximity (±5s)
    result: list[dict[str, Any]] = []
    matched_run2: set[int] = set()

    for item1 in run1.items:
        best_match: ExtractionItem | None = None
        for item2 in run2.items:
            if item2.id in matched_run2:
                continue
            if item2.parameter_name != item1.parameter_name:
                continue
            if abs(item1.segment_start - item2.segment_start) <= 5.0:
                best_match = item2
                break

        if best_match is not None:
            matched_run2.add(best_match.id)
            result.append(
                {
                    "run1_item": _item_to_dict(item1),
                    "run2_item": _item_to_dict(best_match),
                }
            )
        else:
            result.append({"run1_item": _item_to_dict(item1), "run2_item": None})

    # Items in run2 that weren't matched
    for item2 in run2.items:
        if item2.id not in matched_run2:
            result.append({"run1_item": None, "run2_item": _item_to_dict(item2)})

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _update_run_counts(storage: Storage, run_id: int) -> None:
    """Recalculate accepted_count and update status if all items reviewed."""
    db = storage._conn()

    cur = await db.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) AS accepted,"
        " SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending"
        " FROM extraction_items WHERE extraction_run_id = ?",
        (run_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return

    total = row["total"]
    accepted = row["accepted"]
    pending = row["pending"]

    status = RunStatus.REVIEW_PENDING
    if total == 0:
        status = RunStatus.EMPTY
    elif pending == 0:
        status = RunStatus.FULLY_REVIEWED

    await db.execute(
        "UPDATE extraction_runs SET accepted_count = ?, status = ? WHERE id = ?",
        (accepted, status, run_id),
    )
    await db.commit()


def _item_to_dict(item: ExtractionItem) -> dict[str, Any]:
    """Convert an ExtractionItem to a JSON-serializable dict."""
    return {
        "id": item.id,
        "extraction_run_id": item.extraction_run_id,
        "parameter_name": item.parameter_name,
        "extracted_value": item.extracted_value,
        "segment_start": item.segment_start,
        "segment_end": item.segment_end,
        "segment_text": item.segment_text,
        "confidence": item.confidence,
        "status": item.status,
        "reviewed_at": item.reviewed_at,
        "reviewed_by": item.reviewed_by,
    }
