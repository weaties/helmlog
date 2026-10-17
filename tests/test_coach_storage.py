"""Coach grant storage (#848): migration v89 + CRUD."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


async def _race(storage: Storage, name: str, start: datetime) -> int:
    race = await storage.start_race("Evt", start, start.date().isoformat(), 1, name, "race")
    return race.id


async def _grant(storage: Storage, **kw: object) -> int:
    admin = await storage.create_user(f"admin{datetime.now().timestamp()}@x.com", None, "admin")
    args: dict[str, object] = {
        "kind": "account",
        "label": "Coach Pat",
        "coach_email": "Pat@Example.com",
        "token_hash": None,
        "all_sessions": False,
        "allow_video": False,
        "landing_path": None,
        "starts_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
        "created_by": admin,
        "session_ids": [],
    }
    args.update(kw)
    return await storage.create_coach_grant(**args)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_and_get_grant(storage: Storage) -> None:
    r1 = await _race(storage, "R1", NOW)
    r2 = await _race(storage, "R2", NOW + timedelta(hours=2))
    gid = await _grant(storage, session_ids=[r2, r1], allow_video=True)
    g = await storage.get_coach_grant(gid)
    assert g is not None
    assert g["kind"] == "account"
    assert g["coach_email"] == "pat@example.com"  # normalised
    assert g["label"] == "Coach Pat"
    assert g["allow_video"] == 1 and g["all_sessions"] == 0
    assert g["session_ids"] == sorted([r1, r2])
    assert g["revoked_at"] is None
    assert g["created_at"]


@pytest.mark.asyncio
async def test_get_missing_grant(storage: Storage) -> None:
    assert await storage.get_coach_grant(999) is None


@pytest.mark.asyncio
async def test_kind_must_be_account_or_link(storage: Storage) -> None:
    with pytest.raises(ValueError, match="kind"):
        await _grant(storage, kind="other")


@pytest.mark.asyncio
async def test_list_grants_for_email_is_case_insensitive(storage: Storage) -> None:
    g1 = await _grant(storage, coach_email="pat@example.com")
    await _grant(storage, coach_email="someone@else.com")
    await _grant(storage, kind="link", coach_email=None, token_hash="h1")
    rows = await storage.list_coach_grants_for_email("PAT@example.com")
    assert [r["id"] for r in rows] == [g1]


@pytest.mark.asyncio
async def test_update_grant_replaces_session_list(storage: Storage) -> None:
    r1 = await _race(storage, "R1", NOW)
    r2 = await _race(storage, "R2", NOW + timedelta(hours=2))
    gid = await _grant(storage, session_ids=[r1])
    new_exp = (NOW + timedelta(days=60)).isoformat()
    await storage.update_coach_grant(
        gid,
        label="Coach Pat (season)",
        all_sessions=False,
        allow_video=True,
        expires_at=new_exp,
        session_ids=[r2],
    )
    g = await storage.get_coach_grant(gid)
    assert g is not None
    assert g["label"] == "Coach Pat (season)"
    assert g["allow_video"] == 1
    assert g["expires_at"] == new_exp
    assert g["session_ids"] == [r2]


@pytest.mark.asyncio
async def test_revoke_grant(storage: Storage) -> None:
    gid = await _grant(storage)
    await storage.revoke_coach_grant(gid, NOW.isoformat())
    g = await storage.get_coach_grant(gid)
    assert g is not None and g["revoked_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_list_grants_newest_first(storage: Storage) -> None:
    g1 = await _grant(storage, label="first")
    g2 = await _grant(storage, label="second")
    rows = await storage.list_coach_grants()
    assert [r["id"] for r in rows] == [g2, g1]


@pytest.mark.asyncio
async def test_deleting_race_removes_it_from_grants(storage: Storage) -> None:
    r1 = await _race(storage, "R1", NOW)
    r2 = await _race(storage, "R2", NOW + timedelta(hours=2))
    gid = await _grant(storage, session_ids=[r1, r2])
    db = storage._conn()  # noqa: SLF001
    await db.execute("DELETE FROM races WHERE id = ?", (r1,))
    await db.commit()
    g = await storage.get_coach_grant(gid)
    assert g is not None and g["session_ids"] == [r2]


@pytest.mark.asyncio
async def test_list_races_by_ids(storage: Storage) -> None:
    r1 = await _race(storage, "R1", NOW)
    r2 = await _race(storage, "R2", NOW + timedelta(hours=2))
    await _race(storage, "R3", NOW + timedelta(hours=4))
    rows = await storage.list_races_for_coach([r1, r2])
    assert [r["id"] for r in rows] == [r2, r1]  # newest first
    assert set(rows[0]) >= {"id", "name", "start_utc", "end_utc", "session_type", "slug"}


@pytest.mark.asyncio
async def test_list_races_all(storage: Storage) -> None:
    r1 = await _race(storage, "R1", NOW)
    r2 = await _race(storage, "R2", NOW + timedelta(hours=2))
    rows = await storage.list_races_for_coach(None)
    assert [r["id"] for r in rows] == [r2, r1]


@pytest.mark.asyncio
async def test_list_races_excludes_imported_scoring_rows(storage: Storage) -> None:
    r1 = await _race(storage, "R1", NOW)
    r2 = await _race(storage, "Imported", NOW + timedelta(hours=2))
    db = storage._conn()  # noqa: SLF001
    await db.execute("UPDATE races SET source = 'clubspot' WHERE id = ?", (r2,))
    await db.commit()
    assert [r["id"] for r in await storage.list_races_for_coach(None)] == [r1]
    assert [r["id"] for r in await storage.list_races_for_coach([r1, r2])] == [r1]
