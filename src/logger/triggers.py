"""Keyword-triggered auto-notes from transcript segments.

Scans transcript segments for configured trigger keywords (e.g. "protest",
"capsize") and creates timestamped notes at those moments so nothing is missed
during debrief.

Trigger rules are loaded from the ``TRANSCRIPT_TRIGGERS`` env var (JSON list).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from logger.storage import Storage


@dataclass(frozen=True)
class TriggerRule:
    """A single keyword trigger rule."""

    keyword: str
    tag: str
    note_name: str
    case_insensitive: bool = True
    speaker_role: str | None = None


def load_trigger_rules() -> list[TriggerRule]:
    """Load trigger rules from TRANSCRIPT_TRIGGERS env var."""
    raw = os.environ.get("TRANSCRIPT_TRIGGERS", "")
    if not raw.strip():
        return _default_rules()
    try:
        items = json.loads(raw)
        return [
            TriggerRule(
                keyword=item["keyword"],
                tag=item["tag"],
                note_name=item.get("note_name", item["keyword"].title()),
                case_insensitive=item.get("case_insensitive", True),
                speaker_role=item.get("speaker_role"),
            )
            for item in items
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Invalid TRANSCRIPT_TRIGGERS: {}", exc)
        return _default_rules()


def _default_rules() -> list[TriggerRule]:
    """Return built-in trigger rules when no env var is set."""
    return [
        TriggerRule(keyword="protest", tag="protest", note_name="Protest"),
        TriggerRule(keyword="capsize", tag="capsize", note_name="Capsize"),
        TriggerRule(keyword="man overboard", tag="mob", note_name="Man Overboard"),
    ]


@dataclass
class _Match:
    """A keyword match in a transcript segment."""

    rule: TriggerRule
    segment_start: float
    segment_end: float
    text: str


def _scan_segments(
    segments: list[dict[str, Any]],
    rules: list[TriggerRule],
) -> list[_Match]:
    """Scan transcript segments for keyword matches."""
    matches: list[_Match] = []
    for seg in segments:
        text = seg.get("text", "")
        start = seg.get("start", 0.0)
        end = seg.get("end", start)
        speaker = seg.get("speaker")

        for rule in rules:
            # Speaker filter
            if rule.speaker_role and speaker != rule.speaker_role:
                continue

            haystack = text.lower() if rule.case_insensitive else text
            needle = rule.keyword.lower() if rule.case_insensitive else rule.keyword
            if needle in haystack:
                matches.append(_Match(rule=rule, segment_start=start, segment_end=end, text=text))
    return matches


def _dedup_matches(matches: list[_Match], window_s: float = 30.0) -> list[_Match]:
    """Deduplicate matches: one note per keyword per 30-second window."""
    if not matches:
        return []

    matches.sort(key=lambda m: (m.rule.keyword, m.segment_start))
    deduped: list[_Match] = []
    for m in matches:
        # Check if there's a recent match for the same keyword
        existing = next(
            (
                d
                for d in reversed(deduped)
                if d.rule.keyword == m.rule.keyword
                and abs(m.segment_start - d.segment_start) < window_s
            ),
            None,
        )
        if existing is None:
            deduped.append(m)
    return deduped


def _build_context(
    segments: list[dict[str, Any]],
    match_start: float,
    context_segments: int = 1,
) -> str:
    """Gather the matched segment plus surrounding context."""
    texts: list[str] = []
    for i, seg in enumerate(segments):
        if abs(seg.get("start", 0.0) - match_start) < 0.01:
            start = max(0, i - context_segments)
            end = min(len(segments), i + context_segments + 1)
            texts = [segments[j].get("text", "") for j in range(start, end)]
            break
    return " ".join(t.strip() for t in texts if t.strip())


async def scan_transcript(
    storage: Storage,
    audio_session_id: int,
    session_started_at: str,
    segments: list[dict[str, Any]],
    *,
    rules: list[TriggerRule] | None = None,
) -> int:
    """Scan transcript segments and create auto-notes. Returns count of notes created."""
    if rules is None:
        rules = load_trigger_rules()
    if not rules or not segments:
        return 0

    matches = _scan_segments(segments, rules)
    matches = _dedup_matches(matches)

    if not matches:
        return 0

    # Resolve the race_id for this audio session
    row = await storage.get_audio_session_row(audio_session_id)
    race_id: int | None = row["race_id"] if row and row.get("race_id") else None

    session_start = datetime.fromisoformat(session_started_at)
    if session_start.tzinfo is None:
        session_start = session_start.replace(tzinfo=UTC)

    created = 0
    for m in matches:
        # Compute wall-clock timestamp
        note_dt = session_start + timedelta(seconds=m.segment_start)
        note_ts = note_dt.isoformat()

        # Dedup check: existing auto-note within 5 seconds
        existing = await _check_existing_note(storage, race_id, audio_session_id, note_ts)
        if existing:
            continue

        context = _build_context(segments, m.segment_start)
        note_id = await storage.create_note(
            note_ts,
            context or m.text,
            race_id=race_id,
            audio_session_id=audio_session_id,
            note_type="text",
        )

        # Tag the note
        tag_id = await storage.get_or_create_tag(m.rule.tag, _tag_color(m.rule.tag))
        await storage.add_note_tag(note_id, tag_id)
        auto_tag_id = await storage.get_or_create_tag("auto-detected", "#eab308")
        await storage.add_note_tag(note_id, auto_tag_id)

        logger.info(
            "Auto-note created: {} at {} (audio_session={})",
            m.rule.note_name,
            note_ts,
            audio_session_id,
        )
        created += 1

    return created


async def _check_existing_note(
    storage: Storage,
    race_id: int | None,
    audio_session_id: int,
    note_ts: str,
    window_s: float = 5.0,
) -> bool:
    """Check if an auto-detected note already exists within +-window_s of note_ts."""
    dt = datetime.fromisoformat(note_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    lo = (dt - timedelta(seconds=window_s)).isoformat()
    hi = (dt + timedelta(seconds=window_s)).isoformat()
    db = storage._conn()
    cur = await db.execute(
        "SELECT sn.id FROM session_notes sn"
        " JOIN note_tags nt ON sn.id = nt.note_id"
        " JOIN tags t ON nt.tag_id = t.id"
        " WHERE t.name = 'auto-detected'"
        " AND sn.ts BETWEEN ? AND ?"
        " AND (sn.race_id = ? OR sn.audio_session_id = ?)"
        " LIMIT 1",
        (lo, hi, race_id, audio_session_id),
    )
    return await cur.fetchone() is not None


def _tag_color(tag_name: str) -> str:
    """Return a default color for well-known tag names."""
    colors: dict[str, str] = {
        "protest": "#e53e3e",
        "capsize": "#ed8936",
        "mob": "#e53e3e",
        "auto-detected": "#eab308",
    }
    return colors.get(tag_name, "#8892a4")
