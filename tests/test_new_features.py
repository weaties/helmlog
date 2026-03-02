"""Tests for issues #93 (audit log), #99 (tags + triggers), #100 (avatars), #123 (admin nav)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from logger.storage import Storage, StorageConfig


@pytest_asyncio.fixture
async def storage() -> Storage:  # type: ignore[misc]
    s = Storage(StorageConfig(db_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Audit log (#93)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_action_and_list(storage: Storage) -> None:
    uid = await storage.create_user("test@example.com", "Test", "crew")
    await storage.log_action("race.start", detail="Race 1", user_id=uid, ip_address="1.2.3.4")
    await storage.log_action("race.end", detail="Race 1", user_id=uid)
    entries = await storage.list_audit_log()
    assert len(entries) == 2
    assert entries[0]["action"] == "race.end"  # newest first
    assert entries[1]["action"] == "race.start"
    assert entries[1]["ip_address"] == "1.2.3.4"
    assert entries[1]["user_name"] == "Test"


@pytest.mark.asyncio
async def test_audit_log_limit_offset(storage: Storage) -> None:
    for i in range(5):
        await storage.log_action(f"action.{i}")
    entries = await storage.list_audit_log(limit=2, offset=0)
    assert len(entries) == 2
    entries2 = await storage.list_audit_log(limit=2, offset=2)
    assert len(entries2) == 2
    assert entries[0]["action"] != entries2[0]["action"]


# ---------------------------------------------------------------------------
# Tags (#99)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_tags(storage: Storage) -> None:
    tag_id = await storage.create_tag("protest", "#e53e3e")
    tags = await storage.list_tags()
    assert len(tags) == 1
    assert tags[0]["name"] == "protest"
    assert tags[0]["color"] == "#e53e3e"
    assert tags[0]["id"] == tag_id


@pytest.mark.asyncio
async def test_tag_name_normalization(storage: Storage) -> None:
    await storage.create_tag("  Protest  ", "#e53e3e")
    tag = await storage.get_tag_by_name("protest")
    assert tag is not None
    assert tag["name"] == "protest"


@pytest.mark.asyncio
async def test_get_or_create_tag(storage: Storage) -> None:
    tag_id1 = await storage.get_or_create_tag("new-tag", "#aabbcc")
    tag_id2 = await storage.get_or_create_tag("new-tag")
    assert tag_id1 == tag_id2


@pytest.mark.asyncio
async def test_session_tags(storage: Storage) -> None:
    now = datetime.now(UTC)
    race = await storage.start_race("Test", now, now.date().isoformat(), 1, "Test Race 1", "race")
    tag_id = await storage.create_tag("windy")
    await storage.add_session_tag(race.id, tag_id)
    tags = await storage.get_session_tags(race.id)
    assert len(tags) == 1
    assert tags[0]["name"] == "windy"

    # Idempotent
    await storage.add_session_tag(race.id, tag_id)
    assert len(await storage.get_session_tags(race.id)) == 1

    await storage.remove_session_tag(race.id, tag_id)
    assert len(await storage.get_session_tags(race.id)) == 0


@pytest.mark.asyncio
async def test_note_tags(storage: Storage) -> None:
    now = datetime.now(UTC)
    race = await storage.start_race("Test", now, now.date().isoformat(), 1, "Test Race 1", "race")
    note_id = await storage.create_note(now.isoformat(), "test note", race_id=race.id)
    tag_id = await storage.create_tag("protest", "#e53e3e")
    await storage.add_note_tag(note_id, tag_id)
    tags = await storage.get_note_tags(note_id)
    assert len(tags) == 1
    assert tags[0]["name"] == "protest"

    await storage.remove_note_tag(note_id, tag_id)
    assert len(await storage.get_note_tags(note_id)) == 0


@pytest.mark.asyncio
async def test_update_tag(storage: Storage) -> None:
    tag_id = await storage.create_tag("old-name", "#000")
    found = await storage.update_tag(tag_id, name="new-name", color="#fff")
    assert found is True
    tag = await storage.get_tag_by_name("new-name")
    assert tag is not None
    assert tag["color"] == "#fff"


@pytest.mark.asyncio
async def test_delete_tag(storage: Storage) -> None:
    tag_id = await storage.create_tag("temp")
    now = datetime.now(UTC)
    race = await storage.start_race("T", now, now.date().isoformat(), 1, "T R1", "race")
    await storage.add_session_tag(race.id, tag_id)
    found = await storage.delete_tag(tag_id)
    assert found is True
    assert len(await storage.get_session_tags(race.id)) == 0
    assert await storage.get_tag_by_name("temp") is None


@pytest.mark.asyncio
async def test_tag_usage_counts(storage: Storage) -> None:
    tag_id = await storage.create_tag("counted")
    now = datetime.now(UTC)
    race = await storage.start_race("T", now, now.date().isoformat(), 1, "T R1", "race")
    note_id = await storage.create_note(now.isoformat(), "test", race_id=race.id)
    await storage.add_session_tag(race.id, tag_id)
    await storage.add_note_tag(note_id, tag_id)
    tags = await storage.list_tags()
    t = next(t for t in tags if t["id"] == tag_id)
    assert t["session_count"] == 1
    assert t["note_count"] == 1


# ---------------------------------------------------------------------------
# Triggers (#99)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_scan_creates_notes(storage: Storage) -> None:
    from logger.triggers import TriggerRule, scan_transcript

    now = datetime.now(UTC)
    race = await storage.start_race("T", now, now.date().isoformat(), 1, "T R1", "race")
    # Create a fake audio session
    db = storage._conn()
    await db.execute(
        "INSERT INTO audio_sessions"
        " (file_path, device_name, start_utc, sample_rate, channels, race_id, session_type, name)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("/tmp/test.wav", "test", now.isoformat(), 48000, 1, race.id, "race", "T R1"),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    audio_id = (await cur.fetchone())[0]

    segments = [
        {"start": 0.0, "end": 3.0, "text": "nice wind today"},
        {"start": 10.0, "end": 13.0, "text": "PROTEST! starboard!"},
        {"start": 20.0, "end": 23.0, "text": "good tack"},
    ]
    rules = [TriggerRule(keyword="protest", tag="protest", note_name="Protest")]
    count = await scan_transcript(storage, audio_id, now.isoformat(), segments, rules=rules)
    assert count == 1

    # Verify note was created
    notes = await storage.list_notes(race_id=race.id)
    auto_notes = [n for n in notes if n.get("body") and "PROTEST" in n["body"]]
    assert len(auto_notes) == 1

    # Verify tags
    note_tags = await storage.get_note_tags(auto_notes[0]["id"])
    tag_names = {t["name"] for t in note_tags}
    assert "protest" in tag_names
    assert "auto-detected" in tag_names


@pytest.mark.asyncio
async def test_trigger_dedup(storage: Storage) -> None:
    from logger.triggers import TriggerRule, scan_transcript

    now = datetime.now(UTC)
    race = await storage.start_race("T", now, now.date().isoformat(), 1, "T R1", "race")
    db = storage._conn()
    await db.execute(
        "INSERT INTO audio_sessions"
        " (file_path, device_name, start_utc, sample_rate, channels, race_id, session_type, name)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("/tmp/test.wav", "test", now.isoformat(), 48000, 1, race.id, "race", "T R1"),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    audio_id = (await cur.fetchone())[0]

    # Two protest mentions within 30s should be deduped
    segments = [
        {"start": 5.0, "end": 8.0, "text": "protest!"},
        {"start": 15.0, "end": 18.0, "text": "I said protest"},
    ]
    rules = [TriggerRule(keyword="protest", tag="protest", note_name="Protest")]
    count = await scan_transcript(storage, audio_id, now.isoformat(), segments, rules=rules)
    assert count == 1


@pytest.mark.asyncio
async def test_trigger_idempotent_rescan(storage: Storage) -> None:
    from logger.triggers import TriggerRule, scan_transcript

    now = datetime.now(UTC)
    race = await storage.start_race("T", now, now.date().isoformat(), 1, "T R1", "race")
    db = storage._conn()
    await db.execute(
        "INSERT INTO audio_sessions"
        " (file_path, device_name, start_utc, sample_rate, channels, race_id, session_type, name)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("/tmp/test.wav", "test", now.isoformat(), 48000, 1, race.id, "race", "T R1"),
    )
    await db.commit()
    cur = await db.execute("SELECT last_insert_rowid()")
    audio_id = (await cur.fetchone())[0]

    segments = [{"start": 10.0, "end": 13.0, "text": "protest!"}]
    rules = [TriggerRule(keyword="protest", tag="protest", note_name="Protest")]

    count1 = await scan_transcript(storage, audio_id, now.isoformat(), segments, rules=rules)
    assert count1 == 1

    # Re-scan should not create duplicates
    count2 = await scan_transcript(storage, audio_id, now.isoformat(), segments, rules=rules)
    assert count2 == 0


def test_load_trigger_rules_defaults() -> None:
    from logger.triggers import load_trigger_rules

    rules = load_trigger_rules()
    assert len(rules) >= 3
    assert any(r.keyword == "protest" for r in rules)


# ---------------------------------------------------------------------------
# Avatars (#100)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_avatar_path(storage: Storage) -> None:
    uid = await storage.create_user("test@example.com", "Test", "crew")
    assert await storage.get_avatar_path(uid) is None
    await storage.set_avatar_path(uid, "42.jpg")
    assert await storage.get_avatar_path(uid) == "42.jpg"


# ---------------------------------------------------------------------------
# Web API tests (#93, #99, #100, #123)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:  # type: ignore[misc]
    """Authenticated async client for the web app."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    from logger.web import create_app

    app = create_app(storage)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_api_me(client: AsyncClient) -> None:
    r = await client.get("/api/me")
    assert r.status_code == 200
    data = r.json()
    assert data["role"] == "admin"  # mock admin from AUTH_DISABLED


@pytest.mark.asyncio
async def test_admin_audit_page(client: AsyncClient) -> None:
    r = await client.get("/admin/audit")
    assert r.status_code == 200
    assert "Audit Log" in r.text


@pytest.mark.asyncio
async def test_api_audit_log(client: AsyncClient, storage: Storage) -> None:
    await storage.log_action("test.action", detail="hello")
    r = await client.get("/api/audit")
    assert r.status_code == 200
    entries = r.json()
    assert len(entries) >= 1
    assert entries[0]["action"] == "test.action"


@pytest.mark.asyncio
async def test_tag_crud_api(client: AsyncClient) -> None:
    # Create
    r = await client.post("/api/tags", json={"name": "Protest", "color": "#e53e3e"})
    assert r.status_code == 201
    tag_id = r.json()["id"]
    assert r.json()["name"] == "protest"  # normalized

    # List
    r = await client.get("/api/tags")
    assert r.status_code == 200
    assert any(t["id"] == tag_id for t in r.json())

    # Update
    r = await client.patch(f"/api/tags/{tag_id}", json={"color": "#ff0000"})
    assert r.status_code == 200

    # Duplicate
    r = await client.post("/api/tags", json={"name": "protest"})
    assert r.status_code == 409

    # Delete
    r = await client.delete(f"/api/tags/{tag_id}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_session_tag_api(client: AsyncClient, storage: Storage) -> None:
    now = datetime.now(UTC)
    race = await storage.start_race("T", now, now.date().isoformat(), 1, "T R1", "race")
    r = await client.post(f"/api/sessions/{race.id}/tags", json={"tag_name": "windy"})
    assert r.status_code == 201
    tag_id = r.json()["tag_id"]

    r = await client.get(f"/api/sessions/{race.id}/tags")
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = await client.delete(f"/api/sessions/{race.id}/tags/{tag_id}")
    assert r.status_code == 204

    r = await client.get(f"/api/sessions/{race.id}/tags")
    assert len(r.json()) == 0


@pytest.mark.asyncio
async def test_note_tag_api(client: AsyncClient, storage: Storage) -> None:
    now = datetime.now(UTC)
    race = await storage.start_race("T", now, now.date().isoformat(), 1, "T R1", "race")
    note_id = await storage.create_note(now.isoformat(), "test", race_id=race.id)
    r = await client.post(f"/api/notes/{note_id}/tags", json={"tag_name": "protest"})
    assert r.status_code == 201

    r = await client.get(f"/api/notes/{note_id}/tags")
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_profile_page(client: AsyncClient) -> None:
    r = await client.get("/profile")
    assert r.status_code == 200
    assert "Profile" in r.text


@pytest.mark.asyncio
async def test_avatar_fallback_svg(client: AsyncClient, storage: Storage) -> None:
    uid = await storage.create_user("test@example.com", "Test User", "crew")
    r = await client.get(f"/avatars/{uid}.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert "TU" in r.text  # initials


@pytest.mark.asyncio
async def test_nav_has_users_link(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert 'id="nav-users"' in r.text
    assert "/admin/users" in r.text


# ---------------------------------------------------------------------------
# Email (#94)
# ---------------------------------------------------------------------------


def test_smtp_configured_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from logger.email import smtp_configured

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    assert smtp_configured() is False


def test_smtp_configured_true(monkeypatch: pytest.MonkeyPatch) -> None:
    from logger.email import smtp_configured

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "test@example.com")
    assert smtp_configured() is True


@pytest.mark.asyncio
async def test_send_email_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock, patch

    from logger.email import send_email

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")

    mock_smtp = MagicMock()
    with patch("logger.email.smtplib.SMTP", return_value=mock_smtp):
        mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp.__exit__ = MagicMock(return_value=False)
        result = await send_email("user@example.com", "Test", "Hello")
    assert result is True
    mock_smtp.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_send_email_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    from logger.email import send_email

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")

    with patch("logger.email.smtplib.SMTP", side_effect=ConnectionRefusedError("nope")):
        result = await send_email("user@example.com", "Test", "Hello")
    assert result is False


@pytest.mark.asyncio
async def test_send_welcome_email(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, patch

    from logger.email import send_welcome_email

    with patch("logger.email.send_email", new_callable=AsyncMock, return_value=True) as mock:
        result = await send_welcome_email(
            "Alice", "alice@example.com", "crew", "http://x/login?token=abc"
        )
    assert result is True
    mock.assert_called_once()
    _to, _subject, _body = mock.call_args[0]
    assert _to == "alice@example.com"
    assert "crew" in _body
    assert "http://x/login?token=abc" in _body


@pytest.mark.asyncio
async def test_send_device_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, patch

    from logger.email import send_device_alert

    with patch("logger.email.send_email", new_callable=AsyncMock, return_value=True) as mock:
        result = await send_device_alert("alice@example.com", "1.2.3.4", "Mozilla/5.0")
    assert result is True
    mock.assert_called_once()
    _to, _subject, _body = mock.call_args[0]
    assert _to == "alice@example.com"
    assert "1.2.3.4" in _body


@pytest.mark.asyncio
async def test_audit_logged_on_race_start(client: AsyncClient, storage: Storage) -> None:
    # Set event first
    await client.post("/api/event", json={"event_name": "Test Event"})
    r = await client.post("/api/races/start?session_type=race")
    assert r.status_code == 201
    entries = await storage.list_audit_log()
    actions = [e["action"] for e in entries]
    assert "race.start" in actions
    assert "event.set" in actions
