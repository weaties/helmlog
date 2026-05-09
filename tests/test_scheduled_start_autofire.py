"""Tests for the schedule auto-fire path.

When the schedule fires (T-15min before the configured gun), the system
should: (a) create the race row, (b) cancel the schedule row, (c) auto-arm
the race-start FSM with t0 = scheduled gun. The 5-4-1-0 sequence then
plays out hands-off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from helmlog.routes.races import SCHEDULE_LEAD_S, _do_scheduled_start

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest.mark.asyncio
async def test_lead_constant_is_15min() -> None:
    assert SCHEDULE_LEAD_S == 15 * 60


@pytest.mark.asyncio
async def test_do_scheduled_start_arms_fsm_when_gun_supplied(
    storage: Storage,
) -> None:
    """_do_scheduled_start with gun_at: race row created + FSM armed at gun."""
    gun = datetime.now(UTC) + timedelta(minutes=15)

    # Build a stub app with the bits _do_scheduled_start touches.
    app = MagicMock()
    app.state.storage = storage
    app.state.recorder = None
    app.state.audio_config = None
    app.state.session_state = MagicMock()

    await _do_scheduled_start(app, event="R2TS", session_type="race", gun_at=gun)

    # Race row exists.
    current = await storage.get_current_race()
    assert current is not None
    assert current.event == "R2TS"

    # FSM is armed with t0 = gun.
    fsm = await storage.get_race_start_state()
    assert fsm is not None
    assert fsm["phase"] == "armed"
    assert fsm["kind"] == "5-4-1-0"
    assert fsm["t0_utc"] == gun.isoformat()


@pytest.mark.asyncio
async def test_do_scheduled_start_no_gun_does_not_arm(storage: Storage) -> None:
    """Without gun_at (legacy callers), _do_scheduled_start creates the race
    but does not touch the FSM."""
    app = MagicMock()
    app.state.storage = storage
    app.state.recorder = None
    app.state.audio_config = None
    app.state.session_state = MagicMock()

    await _do_scheduled_start(app, event="TestRegatta", session_type="race")

    current = await storage.get_current_race()
    assert current is not None
    fsm = await storage.get_race_start_state()
    assert fsm is None
