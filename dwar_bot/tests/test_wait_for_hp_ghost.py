"""Tests for wait_for_hp ghost abort / no long dead regen waits."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dwar_bot.core.game_client import CharStats
from dwar_bot.modules.timers_manager import TimersManager

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_wait_for_hp_aborts_on_zero_hp_ghost():
    client = MagicMock()
    # Always dead
    client.get_char_stats = AsyncMock(
        return_value=CharStats(nick="x", level=3, hp=0, hp_max=100)
    )
    tm = TimersManager(client)
    t0 = asyncio.get_event_loop().time()
    ok = await tm.wait_for_hp(target_percent=50.0, max_wait=180)
    elapsed = asyncio.get_event_loop().time() - t0
    assert ok is False
    # Must not sit for the full 180s
    assert elapsed < 30.0


@pytest.mark.asyncio
async def test_wait_for_hp_recovers_when_hp_rises():
    client = MagicMock()
    values = [
        CharStats(nick="x", level=3, hp=20, hp_max=100),
        CharStats(nick="x", level=3, hp=55, hp_max=100),
    ]
    client.get_char_stats = AsyncMock(side_effect=values)
    tm = TimersManager(client)
    orig = asyncio.sleep

    async def _fast(_sec):
        return None

    asyncio.sleep = _fast  # type: ignore
    try:
        ok = await tm.wait_for_hp(target_percent=50.0, max_wait=60)
    finally:
        asyncio.sleep = orig  # type: ignore
    assert ok is True


def test_main_skips_long_regen_on_ghost():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "ghost-locked" in src
    assert "retry resurrect next tick" in src
    assert "WAIT_REGEN but HP=0" in src
    # Must not use 180s wait on the HP≈0 ghost path anymore
    ghost_block = src[src.find("HP≈0: auto-resurrect"):src.find("await self.timers.update_regen")]
    assert "max_wait=180" not in ghost_block


def test_resurrection_skips_regen_at_zero():
    src = (ROOT / "modules" / "resurrection.py").read_text(encoding="utf-8")
    assert "реген бесполезен" in src
