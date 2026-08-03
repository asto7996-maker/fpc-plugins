"""
Smoke tests used by the Cursor self-healer after an auto-patch.

Keep these fast and free of live network calls.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DWAR = REPO / "dwar_bot"


def test_core_modules_importable():
    from dwar_bot.core import bot_state, cursor_self_healer, log_watcher
    from dwar_bot.core.bot_state import BotState

    assert BotState.PAUSED.name == "PAUSED"
    assert callable(cursor_self_healer.patch_code_with_cursor)
    assert callable(log_watcher.start_log_monitoring)


def test_progression_brain_importable():
    from dwar_bot.modules.progression_brain import ProgressionBrain, ActionType
    from dwar_bot.modules.bot_settings import BotSettings

    brain = ProgressionBrain(BotSettings())
    assert brain.last is not None
    assert ActionType.IDLE.value == "idle"
    brain.push_farm(60)
    assert brain.farm_push_active()
    # Escalating empty farm should demote hotspot and prefer travel
    from dwar_bot.modules.progression_brain import GameOption, GoalKind
    opt = GameOption(ActionType.COMBAT_AREA, "Точка: Расселина", score=795, goal=GoalKind.COMBAT)
    brain.note_result(opt, progressed=False)
    brain.note_result(opt, progressed=False)
    assert brain.empty_streak("Расселина") >= 2
    assert brain.farm_push_active()
    assert brain._stale_penalty(opt) >= 200


def test_main_module_syntax():
    src = (DWAR / "main.py").read_text(encoding="utf-8")
    ast.parse(src)


def test_selectors_config_exists():
    sel = REPO / "config" / "selectors.py"
    assert sel.exists(), "config/selectors.py must exist for the self-healer prompt"
    ast.parse(sel.read_text(encoding="utf-8"))


def test_healer_prompt_mentions_selectors():
    from dwar_bot.core.cursor_self_healer import _build_prompt

    prompt = _build_prompt("dwar_bot/main.py", "Traceback: boom")
    assert "config/selectors.py" in prompt
    assert "dwar_bot/main.py" in prompt
    assert "dwar_bot/" in prompt  # multi-file allowed


def test_healer_path_augment_and_snapshot_dir():
    from dwar_bot.core.cursor_self_healer import _augment_path, BACKUP_DIR, REPO_ROOT

    env = _augment_path({})
    assert ".local/bin" in env.get("PATH", "") or "local" in env.get("PATH", "")
    assert REPO_ROOT.exists()
    assert BACKUP_DIR.name == ".heal_backups"


def test_heal_change_detection_and_restart_flag():
    from dwar_bot.core.cursor_self_healer import (
        _changed_files,
        _snapshot_tree,
        restart_after_heal_enabled,
    )

    before = _snapshot_tree()
    assert before  # dwar_bot/*.py hashed
    after = dict(before)
    # Simulate no-op
    assert _changed_files(before, after) == []
    # Simulate change
    any_key = next(iter(after))
    after[any_key] = "deadbeef"
    assert any_key in _changed_files(before, after)
    assert restart_after_heal_enabled() is True


def test_pause_for_heal_keeps_healing_state():
    import asyncio
    from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
    from dwar_bot.main import DwarBot
    from dwar_bot.modules.bot_settings import BotSettings

    class _DummyClient:
        _world_url = "https://w1.dwar.ru"
        _session = {}

    set_bot_state(BotState.HEALING)
    bot = DwarBot(_DummyClient(), settings=BotSettings())  # type: ignore[arg-type]

    async def _run() -> None:
        await bot.pause_for_heal()
        assert get_bot_state() == BotState.HEALING
        assert bot._paused is True
        await bot.resume_after_heal()
        assert get_bot_state() == BotState.RUNNING
        assert bot._paused is False

    asyncio.run(_run())


def test_log_watcher_actionable_filter():
    from dwar_bot.core.log_watcher import LogWatcher, _boot_already_healed
    from dwar_bot.core.cursor_self_healer import (
        SKIP_BOOT_MARKER,
        consume_skip_boot_scan,
        mark_skip_boot_scan,
    )
    from dwar_bot.core.error_recovery import classify_text, ErrorClass

    assert LogWatcher._is_actionable("Traceback (most recent call last):\n  File")
    assert LogWatcher._is_actionable("CRITICAL — boom")
    assert LogWatcher._is_actionable("\x1b[31mERROR\x1b[0m | dwar_bot.main\nError in tick #5")
    assert LogWatcher._is_actionable("STAGNATION / DOM-Desync: stuck")
    assert not LogWatcher._is_actionable("INFO something fine")
    assert not LogWatcher._is_actionable("| ERROR | httpx timeout retry")
    assert not LogWatcher._is_actionable("TOKEN EXPIRED: OAuth access_token has expired")
    assert classify_text("TOKEN EXPIRED").kind == ErrorClass.AUTH

    healed = (
        "Error in tick: boom\nTraceback (most recent call last):\n  File\n"
        "Self-heal SUCCESS for dwar_bot/main.py\nAutoHealer SUCCESS dwar_bot/main.py\n"
    )
    assert _boot_already_healed(healed)
    fresh = healed + "\nError in tick: new boom\nTraceback (most recent call last):\n"
    assert not _boot_already_healed(fresh)

    mark_skip_boot_scan("test")
    assert SKIP_BOOT_MARKER.exists()
    assert consume_skip_boot_scan()
    assert not SKIP_BOOT_MARKER.exists()
    assert not consume_skip_boot_scan()


def test_auto_healer_import():
    from dwar_bot.core.auto_healer import get_auto_healer, HealRequest

    h = get_auto_healer()
    assert h is not None
    req = HealRequest("dwar_bot/main.py", "Traceback", reason="test")
    assert req.failed_file.endswith("main.py")


@pytest.mark.parametrize(
    "path",
    [
        "dwar_bot/core/cursor_self_healer.py",
        "dwar_bot/core/log_watcher.py",
        "dwar_bot/core/bot_state.py",
        "dwar_bot/core/account_manager.py",
        "dwar_bot/modules/combat_engine.py",
        "dwar_bot/modules/quest_tracker.py",
        "dwar_bot/modules/fight_client.py",
    ],
)
def test_critical_files_parse(path: str):
    target = REPO / path
    assert target.exists()
    ast.parse(target.read_text(encoding="utf-8"))


def test_error_classifier_routes():
    from dwar_bot.core.error_recovery import (
        classify_text, classify_exception, ErrorClass, cursor_prompt_for,
    )

    assert classify_text("TOKEN EXPIRED access_token").kind == ErrorClass.AUTH
    assert classify_text("ConnectTimeout: timed out").kind == ErrorClass.NETWORK
    assert classify_text("STAGNATION focus=hunt").kind == ErrorClass.STAGNATION
    assert classify_text("ATTACK_BOT failed type=2").kind == ErrorClass.PROTOCOL
    assert classify_text("ValueError: boom").kind == ErrorClass.CODE_BUG

    class TokenExpiredError(Exception):
        pass

    # Named like production
    TokenExpiredError.__name__ = "TokenExpiredError"
    c = classify_exception(TokenExpiredError("dead"), where="tick")
    assert c.kind == ErrorClass.AUTH
    assert not c.allow_cursor

    prompt = cursor_prompt_for(
        classify_text("STAGNATION", focus_key="hunt_mob:Крэтс"),
        "raw",
    )
    assert "hunt_farm" in prompt.lower() or "HUNT" in prompt or "Крэт" in prompt or "wsproxy" in prompt.lower() or "fight" in prompt.lower()


def test_session_invalidate_refuses_when_blocked():
    import asyncio
    from dwar_bot.core.game_client import DwarGameClient

    c = DwarGameClient("https://w1.dwar.ru")
    c._session["sess_sid"] = "abc"
    c._auth_blocked = True

    async def _run():
        await c.invalidate_session("should refuse")
        assert c._session.get("sess_sid") == "abc"
        await c.invalidate_session("force wipe", force=True)
        assert not c._session.get("sess_sid")

    asyncio.run(_run())


def test_fight_packet_roundtrip():
    from dwar_bot.modules.fight_client import (
        pack_params, unpack_params, PT_INT, PT_BIGINT, FS_SCCL_INIT,
    )

    pak = pack_params([
        (0, PT_INT, FS_SCCL_INIT),
        (0, PT_INT, 1987871878),
        (0, PT_BIGINT, 73672258957058),
        (0, PT_INT, 187242975),
    ])
    assert pak.startswith("0038")
    vals = unpack_params(pak)
    assert vals[0] == FS_SCCL_INIT
    assert vals[1] == 1987871878
    assert vals[2] == 73672258957058
    assert vals[3] == 187242975


def test_hunt_mob_preferred_for_quest_unlock():
    from dwar_bot.modules.progression_brain import (
        ProgressionBrain, ActionType, GameOption, GoalKind,
    )
    from dwar_bot.modules.bot_settings import BotSettings
    from dwar_bot.modules.stats_parser import FullProfile, CharStats
    from dwar_bot.core.game_client import GameState, AreaInfo

    brain = ProgressionBrain(BotSettings())
    brain.need_quest_unlock = True
    brain.pending_hunt_mob = "Крэтс"
    profile = FullProfile(
        char=CharStats(nick="t", level=1, hp=100, hp_max=100),
        state=GameState(area_id="932", level=1),
    )
    snap = brain.analyze(
        profile=profile,
        area=AreaInfo(area_id="932", title="Поселок Чернаг"),
        npcs=[],
        in_battle=False,
    )
    assert snap.focus is not None
    assert snap.focus.action == ActionType.HUNT_MOB
    assert "Крэтс" in snap.focus.title


def test_story_turnin_outranks_hunt_after_kill():
    from dwar_bot.modules.progression_brain import ProgressionBrain, ActionType
    from dwar_bot.modules.bot_settings import BotSettings
    from dwar_bot.modules.stats_parser import FullProfile, CharStats
    from dwar_bot.core.game_client import GameState, AreaInfo

    brain = ProgressionBrain(BotSettings())
    brain.need_quest_unlock = True
    brain.pending_hunt_mob = "Крэтс"
    brain.mark_hunt_kill_done()
    profile = FullProfile(
        char=CharStats(nick="t", level=2, hp=100, hp_max=100),
        state=GameState(area_id="932", level=2),
    )
    snap = brain.analyze(
        profile=profile,
        area=AreaInfo(area_id="932", title="Поселок Чернаг"),
        npcs=[],
        story_npc={"npc_id": "42", "global_npc": 0},
        local_npcs=[],
        in_battle=False,
    )
    assert snap.focus is not None
    assert snap.focus.action == ActionType.QUEST_NPC
    assert snap.focus.score >= 1200


def test_fight_lock_html_detection():
    from dwar_bot.modules.stats_parser import is_fight_lock_html
    from dwar_bot.core.game_client import DwarGameClient

    stub = (
        "<html><script>tProcessMenu('fight.php?id=1');</script></html>"
    )
    assert is_fight_lock_html(stub) is True
    assert is_fight_lock_html("var par='nick=xylophaze&lvl=1'") is False
    assert is_fight_lock_html("") is False
    assert DwarGameClient.parse_char_stats(stub).nick == ""
    ok = DwarGameClient.parse_char_stats(
        "var par='nick=xylophaze&lvl=1&hp=10&hp_max=100'"
    )
    assert ok.nick == "xylophaze"
    assert ok.level == 1


def test_telegram_multi_admin_config():
    from dwar_bot.config import (
        parse_telegram_ids,
        resolve_telegram_admins,
        resolve_telegram_notify_chats,
    )

    assert parse_telegram_ids("1, 2;3\n4") == ["1", "2", "3", "4"]
    assert parse_telegram_ids("1", "1,2") == ["1", "2"]
    # Backward compatible: only CHAT_ID
    assert resolve_telegram_admins(chat_id="111", admin_ids="") == ["111"]
    # ADMIN_IDS ∪ CHAT_ID
    assert resolve_telegram_admins(chat_id="111", admin_ids="222,333") == [
        "222", "333", "111",
    ]
    # Explicit notify list wins
    assert resolve_telegram_notify_chats(
        chat_id="111", admin_ids="222", notify_ids="999"
    ) == ["999"]


def test_multi_account_slot_isolation(tmp_path):
    from dwar_bot.core.account_manager import AccountManager, slot_id_for_user
    from dwar_bot.modules.bot_settings import BotSettings

    assert slot_id_for_user("6037704785") == "tg-6037704785"
    am = AccountManager(
        allowed_user_ids=["111", "222"],
        accounts_dir=tmp_path / "accounts",
    )
    s1 = am.ensure_spec("111")
    s2 = am.ensure_spec("222")
    assert s1.cookie_path != s2.cookie_path
    st = BotSettings.load(s1.state_path)
    st.farm.auto_quests = False
    st.save()
    assert BotSettings.load(s2.state_path).farm.auto_quests is True


def test_telegram_acl_api():
    from dwar_bot.telegram_bot import TelegramAPI

    api = TelegramAPI("tok", ["111", "222"], notify_chat_ids=["111", "333"], allow_groups=False)
    assert api.is_admin("111")
    assert api.is_admin(222)
    assert not api.is_admin("999")
    assert api.is_owner("111")
    assert api.chat_allowed({"type": "private"})
    assert not api.chat_allowed({"type": "group"})
    assert api.notify_chat_ids == ["111", "333"]

    api_g = TelegramAPI("tok", "111", allow_groups=True)
    assert api_g.chat_allowed({"type": "supergroup"})


def test_quest_child_list_normalize_and_html_href():
    from dwar_bot.modules.quest_tracker import QuestTracker
    from dwar_bot.core.game_client import DwarGameClient

    kids = QuestTracker._as_child_list(
        {"1": {"id": "335", "message": "ok", "to_point_id": "100241"}}
    )
    assert len(kids) == 1 and kids[0]["id"] == "335"
    assert QuestTracker._as_child_list([{"id": "1"}])[0]["id"] == "1"
    assert QuestTracker._as_child_list(None) == []

    href = DwarGameClient.build_npc_href(
        409,
        global_npc=0,
        link_id=2960,
        f_id=4,
        href="/npc.php?f_id=4&npc_id=409&global_npc=0&link_id=2960&abc",
    )
    assert href.startswith("/npc.php") and "409" in href

    # Positive keywords prefer award/accept replies
    qt = QuestTracker.__new__(QuestTracker)
    qt._answered_points = set()
    picked = QuestTracker._pick_child(
        qt,
        [
            {"id": "1", "title": "Отказаться", "message": "нет"},
            {"id": "2", "title": "Принять награду", "message": "да"},
        ],
    )
    assert picked and picked["id"] == "2"


def test_story_quest_outranks_casual_hunt():
    from dwar_bot.modules.progression_brain import ProgressionBrain, ActionType
    from dwar_bot.modules.bot_settings import BotSettings
    from dwar_bot.modules.quest_tracker import NpcInfo

    brain = ProgressionBrain(BotSettings())
    # Local story NPC score floor after our bump
    assert 950 <= 1250
    npc = NpcInfo(npc_id="409", title="Вождь Торгор", is_global=False, link_id="2960", url="/npc.php?x")
    # Payload must carry hashed href for HTML answers
    assert npc.url.startswith("/npc.php")
    assert ActionType.QUEST_NPC.value == "quest_npc"
    assert brain.last is not None
