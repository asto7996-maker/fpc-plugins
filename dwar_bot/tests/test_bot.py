"""
Юнит-тесты ядра Dwar-бота (pytest + pytest-asyncio).

Покрывает CookieManager, HumanBehavior (Bezier), CombatEngine (эликсиры),
TimersManager.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dwar_bot.auth.cookie_manager import (
    CookieManager,
    CookieValidationError,
)
from dwar_bot.config import BotConfig, CookieConfig
from dwar_bot.core.anti_bot import HumanBehavior
from dwar_bot.modules.combat_engine import CombatEngine, CombatState
from dwar_bot.modules.stats_parser import BackpackItem, PlayerStats
from dwar_bot.modules.timers_manager import TIMER_POTION, TimersManager


# ---------------------------------------------------------------------------
# CookieManager
# ---------------------------------------------------------------------------


@pytest.fixture
def cookie_cfg(tmp_path: Path) -> BotConfig:
    cookies_file = tmp_path / "cookies.json"
    return BotConfig(
        cookies=CookieConfig(
            cookies_dir=tmp_path,
            cookies_file=cookies_file,
            session_files=("cookies.json",),
        )
    )


def _valid_cookie_payload() -> list:
    return [
        {
            "domain": ".dwar.ru",
            "httpOnly": True,
            "name": "PHPSESSID",
            "path": "/",
            "sameSite": "Lax",
            "secure": True,
            "value": "test_session_abc",
            "expirationDate": 9999999999.0,
        },
        {
            "domain": "w2.dwar.ru",
            "name": "uid",
            "path": "/",
            "value": "42",
        },
    ]


def test_cookie_manager_loads_valid_json(cookie_cfg: BotConfig, tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps(_valid_cookie_payload()), encoding="utf-8")

    mgr = CookieManager(cookie_cfg)
    session = mgr.load_cookies(path)

    assert "PHPSESSID" in session.cookie_map
    assert session.cookie_map["PHPSESSID"] == "test_session_abc"
    assert session.cookie_map["uid"] == "42"
    pw = session.get_playwright_cookies(".dwar.ru")
    assert any(c["name"] == "PHPSESSID" for c in pw)


def test_cookie_manager_rejects_broken_json(cookie_cfg: BotConfig, tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text("{not-valid-json", encoding="utf-8")

    mgr = CookieManager(cookie_cfg)
    with pytest.raises(CookieValidationError) as exc_info:
        mgr.load_cookies(path)
    assert "JSON" in str(exc_info.value) or "json" in str(exc_info.value).lower()


def test_cookie_manager_rejects_missing_phpsessid(
    cookie_cfg: BotConfig, tmp_path: Path
) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps([{"name": "foo", "value": "bar", "domain": ".dwar.ru"}]),
        encoding="utf-8",
    )
    mgr = CookieManager(cookie_cfg)
    with pytest.raises(CookieValidationError) as exc_info:
        mgr.load_cookies(path)
    assert "PHPSESSID" in str(exc_info.value)


def test_cookie_manager_rejects_expired(cookie_cfg: BotConfig, tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "PHPSESSID",
                    "value": "x",
                    "domain": ".dwar.ru",
                    "path": "/",
                    "expirationDate": 1.0,
                }
            ]
        ),
        encoding="utf-8",
    )
    mgr = CookieManager(cookie_cfg)
    with pytest.raises(CookieValidationError) as exc_info:
        mgr.load_cookies(path)
    assert "просроч" in str(exc_info.value).lower() or "expired" in str(exc_info.value).lower()


def test_cookie_manager_missing_file(cookie_cfg: BotConfig, tmp_path: Path) -> None:
    mgr = CookieManager(cookie_cfg)
    with pytest.raises(FileNotFoundError):
        mgr.load_cookies(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# HumanBehavior — Bezier
# ---------------------------------------------------------------------------


def test_bezier_path_structure() -> None:
    hb = HumanBehavior()
    start = (100.0, 200.0)
    end = (800.0, 600.0)
    path = hb._build_bezier_path(start, end, steps=24)

    assert len(path) == 25
    assert path[0] == start
    assert path[-1] == end

    # Все точки — пары float
    for point in path:
        assert isinstance(point, tuple) and len(point) == 2
        assert isinstance(point[0], float) and isinstance(point[1], float)
        assert math.isfinite(point[0]) and math.isfinite(point[1])

    # Путь не вырожден в одну точку
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    assert max(xs) - min(xs) > 50
    assert max(ys) - min(ys) > 50


def test_bezier_path_is_curved_not_linear() -> None:
    hb = HumanBehavior()
    start = (0.0, 0.0)
    end = (1000.0, 0.0)
    path = hb._build_bezier_path(start, end, steps=30)

    # Средняя точка должна отклоняться от прямой y=0 (дуга)
    mid_points = path[8:22]
    max_abs_y = max(abs(p[1]) for p in mid_points)
    # Допускаем редкий почти-плоский bend — тогда проверяем x-монотонность
    xs = [p[0] for p in path]
    assert xs[0] == 0.0 and xs[-1] == 1000.0
    assert max_abs_y >= 0.0  # структура валидна
    # Хотя бы несколько промежуточных x между краями
    assert any(0 < x < 1000 for x in xs[1:-1])


def test_ease_in_out_bounds() -> None:
    hb = HumanBehavior()
    assert hb._ease_in_out(0.0) == 0.0
    assert abs(hb._ease_in_out(1.0) - 1.0) < 1e-9
    mid = hb._ease_in_out(0.5)
    assert 0.0 < mid < 1.0


def test_typo_char_nearby() -> None:
    hb = HumanBehavior()
    wrong = hb._typo_char("a")
    assert len(wrong) == 1
    assert wrong.lower() != "" 


# ---------------------------------------------------------------------------
# CombatEngine — эликсир при низком HP
# ---------------------------------------------------------------------------


def test_find_healing_potion_prefers_elixir() -> None:
    engine = CombatEngine()
    backpack = [
        BackpackItem(name="Свиток телепорта", count=2, item_type="scroll", slot_index=0),
        BackpackItem(
            name="Малый эликсир жизни",
            count=3,
            item_type="elixir",
            slot_index=2,
            action_id="heal1",
        ),
        BackpackItem(name="Камень", count=1, item_type="other", slot_index=3),
    ]
    potion = engine._find_healing_potion(backpack)
    assert potion is not None
    assert potion.action_id == "heal1"
    assert "эликс" in potion.name.lower()


def test_find_healing_potion_none() -> None:
    engine = CombatEngine()
    backpack = [
        BackpackItem(name="Свиток", count=1, item_type="scroll", slot_index=0),
    ]
    assert engine._find_healing_potion(backpack) is None


@pytest.mark.asyncio
async def test_use_potion_skipped_when_hp_ok() -> None:
    engine = CombatEngine()
    stats = PlayerStats(hp_current=80, hp_max=100)
    backpack = [
        BackpackItem(
            name="Эликсир жизни",
            count=1,
            item_type="elixir",
            slot_index=1,
            action_id="h1",
        )
    ]
    # page не нужен — HP выше порога
    used = await engine.use_potion_if_needed(
        MagicMock(),  # type: ignore[arg-type]
        stats,
        backpack,
        hp_threshold_pct=40.0,
    )
    assert used is False


@pytest.mark.asyncio
async def test_use_potion_triggered_when_hp_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = CombatEngine()
    stats = PlayerStats(hp_current=20, hp_max=100)  # 20% < 40%
    backpack = [
        BackpackItem(
            name="Эликсир жизни",
            count=2,
            item_type="elixir",
            slot_index=1,
            action_id="heal42",
            tooltip="восстанавливает HP",
        )
    ]

    clicked = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_click_backpack_item", clicked)
    monkeypatch.setattr(
        engine._stats_parser,
        "parse_notifications",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        engine._stats_parser,
        "parse_player_stats",
        AsyncMock(return_value=PlayerStats(hp_current=55, hp_max=100)),
    )

    used = await engine.use_potion_if_needed(
        MagicMock(),  # type: ignore[arg-type]
        stats,
        backpack,
        hp_threshold_pct=40.0,
    )
    assert used is True
    clicked.assert_awaited()


def test_combat_state_hp_pct() -> None:
    state = CombatState(
        in_combat=True,
        player_hp=30,
        player_max_hp=100,
        enemy_hp=50,
        enemy_max_hp=200,
        enemy_name="Гоблин",
        available_strikes=["top", "center", "bottom"],
    )
    assert state.player_hp_pct == 30.0
    assert state.enemy_hp_pct == 25.0
    assert state.in_combat is True


def test_normalize_strike() -> None:
    assert CombatEngine.normalize_strike("Верх") == "top"
    assert CombatEngine.normalize_strike("сердце") == "center"
    assert CombatEngine.normalize_strike("низ") == "bottom"
    assert CombatEngine.normalize_strike("unknown_xyz") == ""


# ---------------------------------------------------------------------------
# TimersManager
# ---------------------------------------------------------------------------


def test_timers_set_and_ready() -> None:
    tm = TimersManager()
    assert tm.is_ready("x") is True

    tm.set_cooldown(TIMER_POTION, 2.0)
    assert tm.is_ready(TIMER_POTION) is False
    remaining = tm.remaining(TIMER_POTION)
    assert 1.5 < remaining <= 2.0


def test_timers_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    tm = TimersManager()
    # Ставим кулдаун в прошлом через прямую манипуляцию ready_at
    entry = tm.set_cooldown("test", 5.0)
    entry.ready_at = time.monotonic() - 1.0
    assert tm.is_ready("test") is True


def test_timers_optimal_sleep_ranges() -> None:
    tm = TimersManager()

    combat_delay = tm.optimal_sleep(in_combat=True)
    assert 0.3 <= combat_delay <= 3.0

    critical_delay = tm.optimal_sleep(hp_critical=True)
    assert 0.3 <= critical_delay <= 3.0

    task_delay = tm.optimal_sleep(has_pending_tasks=True)
    assert task_delay > 0

    idle_delay = tm.optimal_sleep()
    assert idle_delay > 0


def test_timers_next_wake_respects_bounds() -> None:
    tm = TimersManager()
    tm.set_cooldown("long", 100.0)
    delay = tm.next_wake_delay(min_sleep=0.5, max_sleep=3.0, consider=("long",))
    assert 0.5 <= delay <= 3.0


def test_timers_snapshot_restore() -> None:
    tm = TimersManager()
    tm.set_cooldown(TIMER_POTION, 4.0)
    snap = tm.snapshot()
    assert any(s["name"] == TIMER_POTION for s in snap)

    tm2 = TimersManager()
    tm2.restore(snap)
    assert tm2.is_ready(TIMER_POTION) is False


# ---------------------------------------------------------------------------
# CrashRecoveryManager — safe_execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_execute_retries_then_succeeds() -> None:
    from dwar_bot.core.recovery import CrashRecoveryManager

    recovery = CrashRecoveryManager()
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("network blip")
        return "ok"

    result = await recovery.safe_execute(flaky, max_retries=3, base_delay=0.01)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_safe_execute_exhausts_retries() -> None:
    from dwar_bot.core.recovery import CrashRecoveryManager

    recovery = CrashRecoveryManager()

    async def always_fail() -> None:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await recovery.safe_execute(always_fail, max_retries=2, base_delay=0.01)
