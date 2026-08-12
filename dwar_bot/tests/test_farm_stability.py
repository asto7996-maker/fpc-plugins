"""Tests for money format, potions, cookies, farm optimizer, achievements."""

from __future__ import annotations

from pathlib import Path

from dwar_bot.modules.money_format import (
    format_money,
    format_money_delta,
    money_to_float,
    split_money,
)
from dwar_bot.modules.potion_manager import (
    PotionManager,
    is_hp_consumable,
    score_hp_potion,
)
from dwar_bot.modules.farm_optimizer import (
    best_route,
    max_farm_kill_limit,
    preferred_mobs,
    recommended_hp_thresholds,
    should_leave_village,
)
from dwar_bot.modules.achievement_farmer import AchievementFarmer, ACHIEVEMENT_CATALOG
from dwar_bot.modules.battle_strategy import FightBrain

ROOT = Path(__file__).resolve().parents[1]


def test_split_money_float():
    g, s = split_money(158.13)
    assert g == 158 and s == 13
    assert money_to_float(158.13) == 158.13
    assert "158 зол." in format_money(158.13)
    assert "13 сер." in format_money(158.13)
    assert format_money(10, short=True) == "10.00"
    assert format_money_delta(1.25, short=True).startswith("+")


def test_split_money_gold_silver_fields():
    g, s = split_money(0, money_gold=12, money_silver=5)
    assert g == 12 and s == 5


def test_hp_potion_matching():
    assert is_hp_consumable("Отвар восстановления", "Отвар")
    assert is_hp_consumable("Зелье здоровья", "Зелье")
    assert is_hp_consumable("Эликсир жизни", "Эликсир")
    assert not is_hp_consumable("Отвар гнева", "Отвар")
    assert score_hp_potion("Отвар восстановления", "Отвар") > score_hp_potion(
        "Малое зелье", "Зелье"
    )


def test_potion_manager_gates():
    pm = PotionManager()
    assert pm.should_drink_out_of_fight(40.0, 55.0) is True
    assert pm.should_drink_out_of_fight(80.0, 55.0) is False
    assert pm.should_drink_out_of_fight(0.0, 55.0) is False
    assert pm.should_drink_mid_fight(30.0, 55.0) is True
    pm.note_drink(mid_fight=True, ok=True)
    assert pm.session.mid_fight_drinks == 1


def test_fight_brain_requests_elixir():
    brain = FightBrain(elixir_hp_percent=55.0)
    brain.seed_hp(20, 100)
    d = brain.decide_turn(now=1000.0)
    assert d.drink_elixir is True
    d2 = brain.decide_turn(now=1001.0)  # cooldown
    assert d2.drink_elixir is False


def test_safer_hp_thresholds():
    r, h = recommended_hp_thresholds(aggressive=True, max_farm=True)
    assert r >= 25 and h >= 50
    assert max_farm_kill_limit(3) >= 80


def test_farm_routes_leave_village():
    assert should_leave_village(3, "932", zero_reward=True) is True
    assert should_leave_village(1, "932", zero_reward=False) is False
    route = best_route(3)
    assert route is not None
    assert "227" in route.area_ids or "паук" in "".join(route.mob_keywords)
    assert "паук" in preferred_mobs(3, "227") or preferred_mobs(3)


def test_achievements_progress(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    af = AchievementFarmer(path)
    assert len(ACHIEVEMENT_CATALOG) >= 10
    done = af.note_kill(mob="Огненный паук", area_id="227", level=3)
    assert af.state.total_kills == 1
    af.note_area("192")
    assert af._prog("leave_village").done
    af.note_money_level(120.0, 5)
    assert af._prog("gold_hoarder_100").done
    assert af._prog("level_5").done
    goals = af.next_goals(level=3, limit=5)
    assert goals
    html = af.telegram_html(level=3)
    assert "Ачивки" in html


def test_cookie_recovery_module_exists():
    from dwar_bot.modules.cookie_recovery import CookieRecovery
    assert CookieRecovery is not None


def test_main_wires_cookie_recovery_and_potions():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "CookieRecovery" in src
    assert "AchievementFarmer" in src
    assert "format_money_from_state" in src
    assert "recommended_hp_thresholds" in src
    assert "try_keep_old_session" in src


def test_combat_mid_fight_potion_wired():
    src = (ROOT / "modules" / "combat_engine.py").read_text(encoding="utf-8")
    assert "drink_mid_fight" in src
    assert "heal_callback" in (ROOT / "modules" / "fight_client.py").read_text(
        encoding="utf-8"
    )
    assert "drink_elixir" in (ROOT / "modules" / "battle_strategy.py").read_text(
        encoding="utf-8"
    )


def test_pure_farm_drinks_potions():
    src = (ROOT / "modules" / "pure_farm.py").read_text(encoding="utf-8")
    assert "heal_if_needed" in src
    assert "use_potions" in src
    assert "farm_achievements" in src
    assert "format_money" in src


def test_bot_settings_safer_defaults():
    from dwar_bot.modules.bot_settings import FarmSettings
    f = FarmSettings()
    assert f.hp_retreat >= 25
    assert f.hp_heal >= 50
    assert f.use_potions is True
    assert f.mid_fight_potions is True
    assert f.farm_achievements is True


def test_stats_parser_finds_otvar():
    src = (ROOT / "modules" / "stats_parser.py").read_text(encoding="utf-8")
    assert "восстанов" in src
    assert "PotionManager" in src
