"""Tests for tuning parameter extraction from transcripts (#276)."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from helmlog.boat_settings import PARAMETER_NAMES
from helmlog.storage import Storage, StorageConfig
from helmlog.tuning_extraction import (
    RunStatus,
    accept_item,
    compare_runs,
    create_extraction_run,
    delete_run,
    dismiss_item,
    get_run_with_items,
    regex_extract,
    run_extraction,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def transcript_id(storage: Storage) -> int:
    """Create a fake audio session + transcript with segments for testing."""
    db = storage._conn()
    # Create a test user (needed for reviewed_by FK)
    await db.execute(
        "INSERT INTO users (id, email, name, role, created_at)"
        " VALUES (1, 'test@test.com', 'Test User', 'admin', '2025-06-15T00:00:00Z')"
    )
    # Create a race
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc)"
        " VALUES (1, 'Test Race', 'TestEvent', 1, '2025-06-15', '2025-06-15T12:00:00Z')"
    )
    # Create an audio session (device_name, sample_rate, channels required by schema)
    await db.execute(
        "INSERT INTO audio_sessions (id, file_path, device_name, start_utc, end_utc,"
        " sample_rate, channels, race_id)"
        " VALUES (1, '/tmp/test.wav', 'test', '2025-06-15T12:00:00Z',"
        " '2025-06-15T13:00:00Z', 44100, 1, 1)"
    )
    segments = [
        {"start": 10.0, "end": 15.0, "text": "backstay 12"},
        {"start": 20.0, "end": 25.0, "text": "let's ease the vang a bit"},
        {"start": 30.0, "end": 35.0, "text": "vang 8.5"},
        {"start": 40.0, "end": 45.0, "text": "outhaul 3"},
        {"start": 50.0, "end": 55.0, "text": "wind is picking up"},
        {"start": 60.0, "end": 65.0, "text": "cunningham 2.5 looks good"},
    ]
    await db.execute(
        "INSERT INTO transcripts (id, audio_session_id, status, text, segments_json,"
        " model, created_utc, updated_utc)"
        " VALUES (1, 1, 'done', 'backstay 12 vang 8.5 outhaul 3 cunningham 2.5', ?, 'base',"
        " '2025-06-15T13:00:00Z', '2025-06-15T13:00:00Z')",
        (json.dumps(segments),),
    )
    await db.commit()
    return 1


@pytest_asyncio.fixture
async def empty_transcript_id(storage: Storage) -> int:
    """Transcript with no tuning mentions."""
    db = storage._conn()
    await db.execute(
        "INSERT INTO races (id, name, event, race_num, date, start_utc)"
        " VALUES (2, 'Race 2', 'TestEvent', 2, '2025-06-15', '2025-06-15T14:00:00Z')"
    )
    await db.execute(
        "INSERT INTO audio_sessions (id, file_path, device_name, start_utc, end_utc,"
        " sample_rate, channels, race_id)"
        " VALUES (2, '/tmp/test2.wav', 'test', '2025-06-15T14:00:00Z',"
        " '2025-06-15T15:00:00Z', 44100, 1, 2)"
    )
    segments = [
        {"start": 10.0, "end": 15.0, "text": "wind is picking up"},
        {"start": 20.0, "end": 25.0, "text": "tack in five seconds"},
    ]
    await db.execute(
        "INSERT INTO transcripts (id, audio_session_id, status, text, segments_json,"
        " model, created_utc, updated_utc)"
        " VALUES (2, 2, 'done', 'wind is picking up tack in five seconds', ?, 'base',"
        " '2025-06-15T15:00:00Z', '2025-06-15T15:00:00Z')",
        (json.dumps(segments),),
    )
    await db.commit()
    return 2


# ---------------------------------------------------------------------------
# Regex extraction
# ---------------------------------------------------------------------------


class TestRegexExtract:
    def test_extracts_control_name_with_number(self) -> None:
        segments = [{"start": 10.0, "end": 15.0, "text": "backstay 12"}]
        items = regex_extract(segments)
        assert len(items) == 1
        assert items[0].parameter_name == "backstay"
        assert items[0].extracted_value == 12.0
        assert items[0].segment_start == 10.0
        assert items[0].segment_end == 15.0
        assert items[0].confidence == 1.0

    def test_extracts_decimal_value(self) -> None:
        segments = [{"start": 30.0, "end": 35.0, "text": "vang 8.5"}]
        items = regex_extract(segments)
        assert len(items) == 1
        assert items[0].parameter_name == "vang"
        assert items[0].extracted_value == 8.5

    def test_skips_name_without_number(self) -> None:
        segments = [{"start": 20.0, "end": 25.0, "text": "let's ease the vang a bit"}]
        items = regex_extract(segments)
        assert len(items) == 0

    def test_skips_non_parameter_text(self) -> None:
        segments = [{"start": 50.0, "end": 55.0, "text": "wind is picking up"}]
        items = regex_extract(segments)
        assert len(items) == 0

    def test_multiple_matches_in_one_segment(self) -> None:
        segments = [{"start": 10.0, "end": 20.0, "text": "backstay 12 and vang 8"}]
        items = regex_extract(segments)
        assert len(items) == 2
        names = {i.parameter_name for i in items}
        assert names == {"backstay", "vang"}

    def test_multiple_segments(self) -> None:
        segments = [
            {"start": 10.0, "end": 15.0, "text": "backstay 12"},
            {"start": 30.0, "end": 35.0, "text": "vang 8.5"},
            {"start": 40.0, "end": 45.0, "text": "outhaul 3"},
        ]
        items = regex_extract(segments)
        assert len(items) == 3

    def test_case_insensitive(self) -> None:
        segments = [{"start": 10.0, "end": 15.0, "text": "Backstay 12"}]
        items = regex_extract(segments)
        assert len(items) == 1
        assert items[0].parameter_name == "backstay"

    def test_multi_word_parameter_name(self) -> None:
        """Multi-word names like 'main halyard' should be extracted."""
        segments = [{"start": 10.0, "end": 15.0, "text": "main halyard 5"}]
        items = regex_extract(segments)
        assert len(items) == 1
        assert items[0].parameter_name == "main_halyard"
        assert items[0].extracted_value == 5.0

    def test_underscore_parameter_name(self) -> None:
        """Underscore form should also work (e.g. from manual dictation)."""
        segments = [{"start": 10.0, "end": 15.0, "text": "main_halyard 5"}]
        items = regex_extract(segments)
        assert len(items) == 1
        assert items[0].parameter_name == "main_halyard"

    def test_partial_name_skipped(self) -> None:
        """'car' alone should not match 'car_position_port'."""
        segments = [{"start": 10.0, "end": 15.0, "text": "car 5"}]
        items = regex_extract(segments)
        assert len(items) == 0

    def test_all_parameters_extractable(self) -> None:
        """Every canonical parameter should be extractable by regex."""
        from helmlog.boat_settings import PARAMETERS

        for p in PARAMETERS:
            if p.input_type == "preset":
                continue  # presets have text values, not numeric
            label = p.label.lower()
            segments = [{"start": 0.0, "end": 1.0, "text": f"{label} 7.5"}]
            items = regex_extract(segments)
            assert len(items) >= 1, f"Parameter {p.name!r} (label={label!r}) not extracted"
            assert items[0].parameter_name == p.name


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


class TestRunLifecycle:
    @pytest.mark.asyncio
    async def test_create_run(self, storage: Storage, transcript_id: int) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        assert run.status == RunStatus.CREATED
        assert run.method == "regex"
        assert run.transcript_id == transcript_id

    @pytest.mark.asyncio
    async def test_run_extraction_finds_items(self, storage: Storage, transcript_id: int) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        assert len(items) == 4  # backstay, vang, outhaul, cunningham
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        assert run.status == RunStatus.REVIEW_PENDING
        assert run.item_count == 4

    @pytest.mark.asyncio
    async def test_run_extraction_empty(self, storage: Storage, empty_transcript_id: int) -> None:
        run_id = await create_extraction_run(storage, empty_transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        assert len(items) == 0
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        assert run.status == RunStatus.EMPTY

    @pytest.mark.asyncio
    async def test_delete_run_cascades(self, storage: Storage, transcript_id: int) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        await run_extraction(storage, run_id)
        await delete_run(storage, run_id)
        run = await get_run_with_items(storage, run_id)
        assert run is None


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------


class TestReviewActions:
    @pytest.mark.asyncio
    async def test_accept_item(self, storage: Storage, transcript_id: int) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        item = items[0]
        await accept_item(storage, item.id, user_id=1)
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        accepted = [i for i in run.items if i.status == "accepted"]
        assert len(accepted) == 1
        assert run.accepted_count == 1

    @pytest.mark.asyncio
    async def test_dismiss_item(self, storage: Storage, transcript_id: int) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        item = items[0]
        await dismiss_item(storage, item.id, user_id=1)
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        dismissed = [i for i in run.items if i.status == "dismissed"]
        assert len(dismissed) == 1

    @pytest.mark.asyncio
    async def test_accept_then_dismiss_reversible(
        self, storage: Storage, transcript_id: int
    ) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        item = items[0]
        await accept_item(storage, item.id, user_id=1)
        await dismiss_item(storage, item.id, user_id=1)
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        dismissed = [i for i in run.items if i.status == "dismissed"]
        assert len(dismissed) == 1
        assert run.accepted_count == 0

    @pytest.mark.asyncio
    async def test_dismiss_then_accept_reversible(
        self, storage: Storage, transcript_id: int
    ) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        item = items[0]
        await dismiss_item(storage, item.id, user_id=1)
        await accept_item(storage, item.id, user_id=1)
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        accepted = [i for i in run.items if i.status == "accepted"]
        assert len(accepted) == 1

    @pytest.mark.asyncio
    async def test_accept_creates_boat_setting(self, storage: Storage, transcript_id: int) -> None:
        """Accepting an item should create a boat_settings entry."""
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        item = items[0]
        await accept_item(storage, item.id, user_id=1)
        # Check boat_settings has entry
        settings = await storage.list_boat_settings(race_id=1)
        matching = [s for s in settings if s["extraction_run_id"] == run_id]
        assert len(matching) == 1
        assert matching[0]["parameter"] == item.parameter_name
        assert matching[0]["source"] == "transcript"

    @pytest.mark.asyncio
    async def test_all_reviewed_sets_fully_reviewed(
        self, storage: Storage, transcript_id: int
    ) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        items = await run_extraction(storage, run_id)
        for item in items:
            await accept_item(storage, item.id, user_id=1)
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        assert run.status == RunStatus.FULLY_REVIEWED

    @pytest.mark.asyncio
    async def test_delete_run_preserves_manual_entries(
        self, storage: Storage, transcript_id: int
    ) -> None:
        """Deleting an extraction run should not affect manual boat settings."""
        # Create a manual setting
        await storage.create_boat_settings(
            1,
            [{"ts": "2025-06-15T12:05:00Z", "parameter": "backstay", "value": "10"}],
            "manual",
        )
        # Create and run extraction
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        await run_extraction(storage, run_id)
        # Accept one item
        run = await get_run_with_items(storage, run_id)
        assert run is not None
        await accept_item(storage, run.items[0].id, user_id=1)
        # Delete run
        await delete_run(storage, run_id)
        # Manual entry survives
        settings = await storage.list_boat_settings(race_id=1)
        manual = [s for s in settings if s["source"] == "manual"]
        assert len(manual) == 1


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class TestComparison:
    @pytest.mark.asyncio
    async def test_compare_single_run(self, storage: Storage, transcript_id: int) -> None:
        run_id = await create_extraction_run(storage, transcript_id, "regex")
        await run_extraction(storage, run_id)
        result = await compare_runs(storage, run_id, None)
        assert len(result) == 4  # 4 items from run

    @pytest.mark.asyncio
    async def test_compare_two_runs_same_transcript(
        self, storage: Storage, transcript_id: int
    ) -> None:
        run_id_1 = await create_extraction_run(storage, transcript_id, "regex")
        await run_extraction(storage, run_id_1)
        run_id_2 = await create_extraction_run(storage, transcript_id, "regex")
        await run_extraction(storage, run_id_2)
        result = await compare_runs(storage, run_id_1, run_id_2)
        # Same transcript = same items, all should align
        assert len(result) == 4
        for pair in result:
            assert pair["run1_item"] is not None
            assert pair["run2_item"] is not None

    @pytest.mark.asyncio
    async def test_compare_empty_run(
        self, storage: Storage, empty_transcript_id: int, transcript_id: int
    ) -> None:
        run_id_1 = await create_extraction_run(storage, transcript_id, "regex")
        await run_extraction(storage, run_id_1)
        run_id_2 = await create_extraction_run(storage, empty_transcript_id, "regex")
        await run_extraction(storage, run_id_2)
        result = await compare_runs(storage, run_id_1, run_id_2)
        assert len(result) == 4
        for pair in result:
            assert pair["run1_item"] is not None
            assert pair["run2_item"] is None


# ---------------------------------------------------------------------------
# Audio playback logic
# ---------------------------------------------------------------------------


class TestAudioPlayback:
    def test_playback_enabled_with_timestamps_and_file(self) -> None:
        """Audio playback requires valid timestamps AND an existing file."""
        from helmlog.tuning_extraction import can_play_audio

        # Both timestamps and file exist — enabled
        assert can_play_audio(10.0, 15.0, "/tmp/test.wav", file_exists=True) is True

    def test_playback_disabled_no_timestamps(self) -> None:
        from helmlog.tuning_extraction import can_play_audio

        assert can_play_audio(0.0, 0.0, "/tmp/test.wav", file_exists=True) is False

    def test_playback_disabled_no_file(self) -> None:
        from helmlog.tuning_extraction import can_play_audio

        assert can_play_audio(10.0, 15.0, "/tmp/test.wav", file_exists=False) is False

    def test_playback_disabled_no_timestamps_no_file(self) -> None:
        from helmlog.tuning_extraction import can_play_audio

        assert can_play_audio(0.0, 0.0, "/tmp/test.wav", file_exists=False) is False


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


class TestParameterValidation:
    def test_extracted_names_are_canonical(self) -> None:
        """All names returned by regex_extract must be in PARAMETER_NAMES."""
        from helmlog.boat_settings import PARAMETERS

        for p in PARAMETERS:
            if p.input_type == "preset":
                continue
            label = p.label.lower()
            segments = [{"start": 0.0, "end": 1.0, "text": f"{label} 7"}]
            items = regex_extract(segments)
            for item in items:
                assert item.parameter_name in PARAMETER_NAMES, (
                    f"Extracted name {item.parameter_name!r} not in PARAMETER_NAMES"
                )
