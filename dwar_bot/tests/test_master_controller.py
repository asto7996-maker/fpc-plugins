"""Юнит-тесты MasterController FSM (resolve_next_state, PrimaryMode)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dwar_bot.core.master_controller import (
    BotState,
    MasterController,
    PrimaryMode,
    primary_mode_from_env,
)
from dwar_bot.modules.stats_parser import PlayerStats


@pytest.fixture()
def controller(monkeypatch: pytest.MonkeyPatch) -> MasterController:
    # Не тянем реальный Playwright/Telegram на импорте конфигов — оставляем как есть
    ctrl = MasterController.__new__(MasterController)
    ctrl.primary_mode = PrimaryMode.FARMING
    ctrl.hp_heal_pct = 55.0
    ctrl.hp_critical_pct = 35.0
    ctrl._quest_queue = []
    ctrl._stop_event = __import__("asyncio").Event()
    ctrl.remote_state = MagicMock()
    ctrl.remote_state.should_stop = False
    ctrl.remote_state.is_paused = False
    ctrl.ctx = MagicMock()
    ctrl.ctx.anti_bot.is_paused = False
    ctrl.ctx.anti_bot.captcha.is_paused = False
    ctrl.ctx.stats = PlayerStats(hp_current=100, hp_max=100, in_combat=False)
    ctrl.scheduler = MagicMock()
    ctrl.scheduler._tasks = {}
    ctrl.scheduler._handlers = {}
    return ctrl


def test_resolve_priority_captcha(controller: MasterController) -> None:
    controller.ctx.anti_bot.is_paused = True
    assert controller.resolve_next_state() == BotState.HANDLING_CAPTCHA


def test_resolve_priority_combat(controller: MasterController) -> None:
    controller.ctx.stats = PlayerStats(hp_current=80, hp_max=100, in_combat=True)
    assert controller.resolve_next_state() == BotState.IN_COMBAT


def test_resolve_priority_healing(controller: MasterController) -> None:
    controller.ctx.stats = PlayerStats(hp_current=20, hp_max=100, in_combat=False)
    assert controller.resolve_next_state() == BotState.HEALING


def test_resolve_primary_farming(controller: MasterController) -> None:
    controller.primary_mode = PrimaryMode.FARMING
    controller.ctx.stats = PlayerStats(hp_current=90, hp_max=100, in_combat=False)
    assert controller.resolve_next_state() == BotState.FARMING


def test_resolve_primary_trading(controller: MasterController) -> None:
    controller.primary_mode = PrimaryMode.TRADING
    assert controller.resolve_next_state() == BotState.TRADING


def test_resolve_quests_queue(controller: MasterController) -> None:
    controller.primary_mode = PrimaryMode.QUESTS
    controller._quest_queue = [{"talk": "npc"}]
    assert controller.resolve_next_state() == BotState.EXECUTING_QUEST


def test_resolve_pause_and_stop(controller: MasterController) -> None:
    controller.remote_state.is_paused = True
    assert controller.resolve_next_state() == BotState.PAUSED
    controller.remote_state.is_paused = False
    controller.remote_state.should_stop = True
    assert controller.resolve_next_state() == BotState.STOPPING


def test_primary_mode_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWAR_PRIMARY_MODE", "trading")
    assert primary_mode_from_env() == PrimaryMode.TRADING
    monkeypatch.setenv("DWAR_PRIMARY_MODE", "quests")
    assert primary_mode_from_env() == PrimaryMode.QUESTS
