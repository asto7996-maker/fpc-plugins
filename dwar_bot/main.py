"""
Main entry point — pure HTTP orchestrator with Telegram bot interface.

Start:
    python -m dwar_bot.main
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import random
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dwar_bot.config import (
    DELAY_IDLE,
    DELAY_MAIN_LOOP,
    DELAY_RETRY,
    IDLE_PAUSE_PROBABILITY,
    LOG_FILE,
    MAX_RETRIES,
    TELEGRAM_ADMIN_IDS,
    TELEGRAM_ALLOW_GROUPS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_NOTIFY_CHAT_IDS,
    DEFAULT_COOKIE_FILE,
    COOKIES_DIR,
    resolve_telegram_admins,
    resolve_telegram_notify_chats,
)
from dwar_bot.logger import setup_logging, log_exception
from dwar_bot.auth.oauth_login import extract_access_token
from dwar_bot.core.game_client import (
    DwarGameClient,
    GameState,
    CharStats,
    TokenExpiredError,
    STATUS_OK,
    load_cookie_dict,
    persist_session_cookies,
)
from dwar_bot.telegram_bot import TelegramBotHandler
from dwar_bot.modules.stats_parser import StatsParser, FullProfile, is_fight_lock_html
from dwar_bot.modules.combat_engine import CombatEngine, BattleResult
from dwar_bot.modules.quest_tracker import QuestTracker
from dwar_bot.modules.timers_manager import TimersManager
from dwar_bot.modules.bot_settings import BotSettings
from dwar_bot.modules.progression_brain import (
    ActionType,
    GameOption,
    ProgressionBrain,
)
from dwar_bot.core.account_manager import AccountManager, ACCOUNTS_DIR
from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.cursor_self_healer import ensure_cursor_cli, _augment_path
from dwar_bot.core.auto_healer import bind_auto_healer, get_auto_healer
from dwar_bot.core.error_recovery import get_recovery_stats
from dwar_bot.core.self_healing import AutonomousLogWatcher
from dwar_bot.core.ai_healing import HealingOrchestrator
from dwar_bot.core.auto_coder import bind_auto_coder
from dwar_bot.core.master_controller import (
    StrategicDirective,
    bind_master_controller,
    get_master_controller,
)
from dwar_bot.core.game_knowledge_base import get_knowledge_base
from dwar_bot.core.telemetry_engine import TelemetryEngine
from dwar_bot.core.rich_notifications import RichNotifications
from dwar_bot.modules.leveling_engine import LevelingEngine
from dwar_bot.modules.analytics_reporter import AnalyticsReporter
from dwar_bot.modules.pure_farm import PureFarmEngine
from dwar_bot.modules.achievement_farmer import AchievementFarmer
from dwar_bot.modules.resurrection import ResurrectionEngine
from dwar_bot.modules.money_format import (
    format_money,
    format_money_delta,
    format_money_from_state,
    money_from_state,
)
from dwar_bot.modules.farm_optimizer import recommended_hp_thresholds, max_farm_kill_limit
from dwar_bot.modules.cookie_recovery import CookieRecovery
from dwar_bot.config import COMBAT, STATE_FILE

logger = logging.getLogger("dwar_bot.main")

_shutdown_event = asyncio.Event()


def _handle_signal(signum, frame) -> None:
    logger.warning("Signal %d received — graceful shutdown …", signum)
    _shutdown_event.set()


async def _sleep(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


def _tg_esc(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


class DwarBot:
    def __init__(
        self,
        client: DwarGameClient,
        settings: Optional[BotSettings] = None,
        *,
        account_id: str = "",
        owner_user_id: str = "",
    ) -> None:
        self._client = client
        self.settings = settings or BotSettings.load()
        self.account_id = account_id or "default"
        self.owner_user_id = str(owner_user_id or "")
        self.stats = StatsParser(client)
        self.combat = CombatEngine(client, self.stats)
        self.combat._resurrect_bot = self
        self.quests = QuestTracker(client)
        try:
            if self.quests.load_world_objective():
                wo = self.quests.pending_world_objective or {}
                logger.info(
                    "Restored world objective '%s' (flash_only=%s)",
                    wo.get("kind"),
                    bool(wo.get("flash_only")),
                )
                # Flash heal with no HTTP path: stall immediately so we hunt
                # instead of spinning empty story-checks on boot.
                if (
                    wo.get("kind") == "heal_wounded"
                    and (wo.get("flash_only") or wo.get("http_impossible"))
                    and float(wo.get("stalled_until") or 0) < time.time()
                ):
                    wo = dict(wo)
                    wo["stalled_until"] = time.time() + 900.0
                    wo["farm_open"] = True
                    self.quests.pending_world_objective = wo
                    self.quests._persist_world_objective()
                    logger.warning(
                        "Boot: heal_wounded Flash-stalled 15min — PureFarm hunt."
                    )
        except Exception as exc:
            logger.debug("load_world_objective: %s", exc)
        self.timers = TimersManager(client)
        self.brain = ProgressionBrain(self.settings)
        # Level-Up strategic stack (per-account KB file when account_id set)
        kb_path = None
        if account_id and account_id != "default":
            from pathlib import Path as _P
            kb_path = _P(__file__).resolve().parent.parent / "data" / f"knowledge_{account_id}.db"
        self.knowledge = get_knowledge_base(kb_path) if kb_path else get_knowledge_base()
        self.controller = get_master_controller()
        self.leveling = LevelingEngine(
            knowledge=self.knowledge,
            controller=self.controller,
            account_id=self.account_id,
        )
        self.telemetry = TelemetryEngine(
            account_id=self.account_id,
            price_lookup=self.knowledge,
        )
        self.rich = RichNotifications(self.telemetry)
        self.analytics = AnalyticsReporter(self)
        self.pure_farm = PureFarmEngine()
        # Achievements persist next to bot settings
        ach_path = None
        try:
            from pathlib import Path as _P
            if getattr(self.settings, "_path", None):
                ach_path = Path(self.settings._path)
            else:
                ach_path = _P(__file__).resolve().parent / "state.json"
        except Exception:
            ach_path = None
        self.achievements = AchievementFarmer(ach_path)
        self.resurrection = ResurrectionEngine()
        self._cookie_recovery = CookieRecovery(client)
        self._wire_telemetry_hooks()

        self._iteration = 0
        self._errors_in_row = 0
        self._profile = FullProfile()
        self._char = CharStats()
        self._state = GameState()
        self._area_title: str = ""
        self._area_items: list = []
        self._npcs: list = []
        self._paused = False
        self._started_at: float = time.time()
        self._token_ok: bool = True
        self._area_moves_tried: set[str] = set()
        self._tg: Optional[TelegramBotHandler] = None
        self._prev_level: int = 0
        self._prev_money: float = -1.0
        self._prev_area: str = ""
        self._hp_low_sent: bool = False
        self._last_focus_key: str = ""
        self._loot_claimed: int = 0
        self._apply_combat_thresholds()

    def bind_telegram(self, tg: TelegramBotHandler) -> None:
        self._tg = tg
        # Rich notifications go through the same owner-scoped notify path
        self.rich.bind_notify(self._rich_notify)

    async def _rich_notify(self, text: str, category: str = "") -> None:
        await self.notify(text, category)

    def _wire_telemetry_hooks(self) -> None:
        """Instrument combat potion use with exact consumable telemetry."""
        engine = self.combat
        orig = engine.use_potion

        async def _tracked_use_potion(artifact):
            ok = await orig(artifact)
            if ok:
                title = getattr(artifact, "title", "") or "зелье"
                kind = "scroll" if "свиток" in title.lower() else "potion"
                self.telemetry.note_consumable(
                    title,
                    kind=kind,
                    context=f"combat.use_potion art_id={getattr(artifact, 'art_id', '')}",
                )
            return ok

        engine.use_potion = _tracked_use_potion  # type: ignore[method-assign]

    def _telemetry_battle_begin(
        self,
        *,
        source: str,
        mob_name: str = "",
        mob_id: str = "",
    ) -> None:
        self.telemetry.start_battle(
            source=source,
            mob_id=mob_id,
            mob_name=mob_name,
            area_id=str(self._state.area_id or ""),
            potions_baseline=self.combat.session.potions_used,
            attacks_baseline=self.combat.session.attacks_made,
        )

    async def _telemetry_battle_end(
        self,
        result: BattleResult,
        *,
        mob_name: str = "",
        notify: bool = True,
    ) -> None:
        if result not in (
            BattleResult.WIN, BattleResult.LOSE, BattleResult.FLED, BattleResult.ERROR,
        ):
            return
        bt = self.telemetry.end_battle(
            result=result.name,
            potions_total=self.combat.session.potions_used,
            attacks_total=(
                self.combat.session.attacks_made
                + max(0, int(getattr(self.combat, "last_fight_attacks", 0) or 0) - 1)
            ),
            hits=int(getattr(self.combat, "last_fight_attacks", 0) or 0) or None,
            damage_dealt=int(getattr(self.combat, "last_fight_damage_dealt", 0) or 0) or None,
            damage_taken=int(getattr(self.combat, "last_fight_damage_taken", 0) or 0) or None,
        )
        if bt and mob_name and not bt.mob_name:
            bt.mob_name = mob_name
        if bt and notify and self.settings.notify.battles:
            wo = self.quests.pending_world_objective or {}
            # Flash-only heal wait: hunting does not progress quest — mute battle spam
            if wo.get("flash_only"):
                logger.debug("battle TG muted (flash_only world objective)")
                return
            await self.rich.notify_battle_finished(bt)

    def _telemetry_quest_begin(self, title: str, *, npc_id: str = "") -> None:
        self.telemetry.ensure_quest(
            title or "Квест",
            npc_id=npc_id,
            area_id=str(self._state.area_id or ""),
            profile=self._profile,
            gold=float(self._state.money or 0),
        )

    async def _telemetry_quest_complete(
        self,
        *,
        title: str = "",
        notify: bool = True,
    ) -> None:
        # Prefer KB quest rewards when known
        exp = 0.0
        valor = 0.0
        exp_pct = 0.0
        qtitle = title
        if self.telemetry.active_quest:
            qtitle = qtitle or self.telemetry.active_quest.title
        for q in self.knowledge.list_quests(max_level=self._char.level or 99)[:20]:
            if qtitle and qtitle.lower() in q.title.lower():
                exp = q.exp_reward
                valor = q.valor_reward
                break
        if exp <= 0:
            # LevelingEngine proxy from recent note
            exp = max(50.0, self.leveling.progress.exp_per_hour / 12.0)
        if self._char.level:
            # rough % of level bucket
            bucket = 800.0 * max(1, self._char.level)
            exp_pct = (exp / bucket) * 100.0 if bucket else 0.0
        qt = self.telemetry.complete_quest(
            profile=self._profile,
            gold=float(self._state.money or 0),
            exp_gained=exp,
            exp_pct_of_level=exp_pct,
            valor_gained=valor,
            title=qtitle or None,
        )
        if qt:
            self.leveling.note_quest_progress(title=qt.title, exp_reward=qt.exp_gained)
            if notify and self.settings.notify.quests:
                await self.rich.notify_quest_completed(qt)

    def _apply_combat_thresholds(self) -> None:
        f = self.settings.farm
        # Prefer safer MaxFarm thresholds when user left legacy deadly 10/30
        retreat, heal = float(f.hp_retreat), float(f.hp_heal)
        if f.max_farm or f.aggressive:
            rec_r, rec_h = recommended_hp_thresholds(
                aggressive=bool(f.aggressive), max_farm=bool(f.max_farm),
            )
            # Only bump up — never lower a user-raised safe value
            if retreat < rec_r:
                retreat = rec_r
                f.hp_retreat = rec_r
            if heal < rec_h:
                heal = rec_h
                f.hp_heal = rec_h
            COMBAT.suis_session_kill_limit = max(
                int(getattr(COMBAT, "suis_session_kill_limit", 0) or 0),
                max_farm_kill_limit(
                    int(getattr(self._char, "level", 1) or 1),
                    aggressive=bool(f.aggressive or f.max_farm),
                ),
            )
        COMBAT.hp_retreat_threshold = float(retreat)
        COMBAT.hp_elixir_threshold = float(heal)
        COMBAT.max_consecutive_battles = int(f.max_battles_row or 80)
        # Mid-fight potions follow farm.use_potions / mid_fight_potions
        if not getattr(f, "use_potions", True):
            COMBAT.post_battle_heal = False
        else:
            COMBAT.post_battle_heal = True

    async def notify(self, text: str, category: str = "") -> None:
        # Always deliver only to this account's owner — never broadcast to others.
        if self._tg and self.owner_user_id:
            await self._tg.notify_user(self.owner_user_id, text, category=category)
        elif self._tg:
            await self._tg.notify(text, category=category)
        else:
            await self._send_telegram(text)

    def get_status(self) -> dict:
        elapsed = int(time.time() - self._started_at)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        cs = self.combat.session
        qs = self.quests.session
        f = self.settings.farm
        return {
            "running":    not self._paused and not _shutdown_event.is_set(),
            "token_ok":   self._token_ok and not self._client.auth_blocked,
            "account_id": self.account_id,
            "owner_user_id": self.owner_user_id,
            "nick":       self._char.nick,
            "level":      self._char.level,
            "hp":         self._char.hp,
            "hp_max":     self._char.hp_max,
            "mp":         self._char.mp,
            "mp_max":     self._char.mp_max,
            "money":      money_from_state(self._state),
            "money_display": format_money_from_state(self._state),
            "money_gold": getattr(self._state, "money_gold", 0),
            "money_silver": getattr(self._state, "money_silver", 0),
            "area_id":    self._state.area_id,
            "area_title": self._area_title,
            "area_items": self._area_items,
            "npcs":       self._npcs,
            "flags":      self._state.flags,
            "flags2":     self._state.flags2,
            "flags3":     self._state.flags3,
            "iteration":  self._iteration,
            "uptime":     f"{h}ч {m}м {s}с",
            "battles":    cs.battles_joined,
            "wins":       cs.wins,
            "losses":     cs.losses,
            "win_rate":   cs.win_rate,
            "potions_used": cs.potions_used,
            "attacks":    cs.attacks_made,
            "quests_accepted":  qs.quests_accepted,
            "quests_completed": qs.quests_completed,
            "dialogues":        qs.dialogues_handled,
            "npcs_visited":     qs.npcs_visited,
            "inventory":  [
                {"title": a.title, "kind": a.kind,
                 "dur": a.durability, "dur_max": a.durability_max}
                for a in self._profile.inventory
            ],
            "potions_count": len(self._profile.potions),
            "effects": [
                {"title": e.title, "id": e.effect_id}
                for e in self._profile.effects
            ],
            "timers": self.timers.summary(),
            "sess_sid": (self._client._session.get("sess_sid") or "")[:8],
            "farm": {
                "auto_quests": f.auto_quests,
                "auto_combat": f.auto_combat,
                "farm_fronts": f.farm_fronts,
                "farm_arena": f.farm_arena,
                "farm_area": f.farm_area,
                "auto_travel": f.auto_travel,
                "auto_loot": f.auto_loot,
                "max_farm": f.max_farm,
            },
            "progress": self.brain.last.to_dict(),
            "leveling": {
                "mode": self.leveling.progress.mode,
                "level": self.leveling.progress.level,
                "exp_pct": self.leveling.progress.exp_pct,
                "exp_per_hour": self.leveling.progress.exp_per_hour,
                "eta_seconds": self.leveling.progress.eta_seconds,
                "priority": self.leveling.progress.priority_title,
                "directive": self.controller.directive_summary(),
            },
            "telemetry": {
                "rates": self.telemetry.rates(window_sec=3600.0),
                "battles": self.telemetry.battle_stats_summary(),
                "active_quest": (
                    {
                        "title": self.telemetry.active_quest.title,
                        "started_at": self.telemetry.active_quest.started_at,
                        "consumables": len(self.telemetry.active_quest.consumables),
                    }
                    if self.telemetry.active_quest else None
                ),
                "active_battle": (
                    {
                        "mob": self.telemetry.active_battle.mob_name,
                        "source": self.telemetry.active_battle.source,
                        "started_at": self.telemetry.active_battle.started_at,
                    }
                    if self.telemetry.active_battle else None
                ),
            },
            "loot_claimed": self._loot_claimed,
            "settings": self.settings.to_dict(),
            "bot_state": get_bot_state().name,
            "auth_blocked": self._client.auth_blocked,
            "fight_id": getattr(self._state, "fight_id", 0) or 0,
            "need_quest_unlock": self.brain.need_quest_unlock,
            "awaiting_quest_turnin": self.brain.awaiting_quest_turnin,
            "pending_hunt_mob": self.brain.pending_hunt_mob or self.quests.pending_hunt_mob,
            "world_objective": dict(self.quests.pending_world_objective or {}),
            "recovery": {
                "last_kind": get_recovery_stats().last_kind,
                "auth_waits": get_recovery_stats().auth_waits,
                "network_retries": get_recovery_stats().network_retries,
                "protocol_recovers": get_recovery_stats().protocol_recovers,
                "stagnation_local": get_recovery_stats().stagnation_local,
                "cursor_heals": get_recovery_stats().cursor_heals,
                "history": list(get_recovery_stats().history[-8:]),
            },
        }

    async def build_report(self) -> str:
        """Full analytics report — telemetry + knowledge + state machine."""
        return self.analytics.build_full_report()

    async def pause(self, *, quiet: bool = False) -> None:
        self._paused = True
        set_bot_state(BotState.PAUSED)
        logger.info("Game loop paused%s.", " (heal)" if quiet else " via Telegram")
        if not quiet:
            await self.notify("⏸ Автопилот на паузе.", "heartbeat")

    async def resume_game(self, *, quiet: bool = False) -> None:
        self._paused = False
        set_bot_state(BotState.RUNNING)
        logger.info("Game loop resumed%s.", " (heal)" if quiet else " via Telegram")
        if not quiet:
            await self.notify("▶️ Автопилот возобновлён.", "heartbeat")

    async def pause_for_heal(self) -> None:
        """Pause gameplay for heal WITHOUT overwriting BotState.HEALING."""
        self._paused = True
        # Keep HEALING if already set by AutoHealer; else mark paused quietly
        if get_bot_state() != BotState.HEALING:
            set_bot_state(BotState.HEALING)
        logger.info("Game loop paused (heal).")

    async def resume_after_heal(self) -> None:
        self._paused = False
        set_bot_state(BotState.RUNNING)
        logger.info("Game loop resumed (heal).")

    async def apply_cookie_json(self, raw_json: str) -> str:
        """
        Accept Cookie Editor JSON pasted via Telegram into THIS account only.
        Returns a short human-readable status string.
        """
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            return f"❌ JSON не разобран: {exc}"

        if not isinstance(data, list):
            return "❌ Ожидался JSON-массив Cookie Editor."

        path = Path(self._client._cookie_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

        cookies = {str(c.get("name")): str(c.get("value", "")) for c in data if isinstance(c, dict)}
        mycom = cookies.get("mycom", "")
        token = extract_access_token(mycom) if mycom else ""
        if not token and not cookies.get("sess_sid"):
            return "❌ В JSON нет mycom/access_token и нет sess_sid."

        self._client.apply_cookies(cookies, mark_fresh=bool(cookies.get("sess_sid")))
        self._client.unblock_auth()
        if token:
            self._client._access_token = token

        # If no sess_sid — create one via OAuth now
        if not cookies.get("sess_sid"):
            try:
                await self._client.invalidate_session("fresh mycom without sess")
                await self._client.ensure_session()
            except TokenExpiredError as exc:
                self._token_ok = False
                return f"❌ Токен из JSON тоже недействителен: {exc}"

        self._token_ok = True
        try:
            char = await self._client.get_char_stats()
            state = await self._client.get_state()
            persist_session_cookies(self._client._session, path, base_cookies=data)
            self._char = char
            self._state = state
            return (
                f"✅ Куки приняты в ваш слот <code>{self.account_id}</code>.\n"
                f"{char.nick or '?'} Lv{char.level} "
                f"HP {char.hp}/{char.hp_max} area={state.area_id}\n"
                f"Другие пользователи этот аккаунт не увидят."
            )
        except TokenExpiredError as exc:
            self._token_ok = False
            return f"❌ Сессия не поднялась: {exc}"
        except Exception as exc:
            return f"⚠️ Куки сохранены, но проверка не удалась: {exc}"

    async def run(self) -> None:
        logger.info("DwarBot HTTP loop started (world=%s).", self._client._world_url)
        while not _shutdown_event.is_set():
            if self._paused:
                await asyncio.sleep(5)
                continue

            self._iteration += 1
            self._apply_combat_thresholds()
            try:
                if get_auto_healer().network_blocked():
                    await asyncio.sleep(5)
                    continue
                await self._client.maybe_reload_cookie_file()
                await self._tick()
                await self._maybe_send_report()
                self._errors_in_row = 0
            except asyncio.CancelledError:
                break
            except TokenExpiredError as exc:
                self._token_ok = False
                if self.settings.notify.token:
                    await self.notify(
                        "⚠️ OAuth токен истёк. Пришли Cookie Editor JSON в этот чат.",
                        "token",
                    )
                await self._handle_token_expired(str(exc))
                self._token_ok = True
            except Exception as exc:
                self._errors_in_row += 1
                log_exception(logger, f"Error in tick #{self._iteration}", exc)
                if self.settings.notify.errors:
                    await self.notify(
                        f"❌ Ошибка тика #{self._iteration}: {exc}",
                        "errors",
                    )
                # Immediate auto-heal (don't wait for log watcher)
                try:
                    await get_auto_healer().handle_exception(exc, where=f"tick#{self._iteration}")
                except Exception as heal_exc:
                    logger.debug("inline heal failed: %s", heal_exc)
                if self._errors_in_row >= MAX_RETRIES:
                    logger.critical("%d consecutive errors — pausing 5min.", MAX_RETRIES)
                    await asyncio.sleep(300)
                    self._errors_in_row = 0
                    # Soft recheck only — do NOT wipe sess when token is dead
                    ok = await self._client.soft_recheck_session()
                    if not ok:
                        logger.warning("After error pause session still dead.")
                else:
                    await _sleep(DELAY_RETRY.min, DELAY_RETRY.max)

            if not _shutdown_event.is_set():
                # Keepalive every ~10 ticks
                if self._iteration % 10 == 0:
                    try:
                        await self._client.keepalive()
                    except Exception:
                        pass
                await _sleep(DELAY_MAIN_LOOP.min, DELAY_MAIN_LOOP.max)
                # No long idle while max-farm / farm-push / open farm (efficiency)
                if (
                    self.settings.farm.idle_pauses
                    and not self.settings.farm.max_farm
                    and not self.brain.farm_push_active()
                    and not self._wo_farm_open()
                    and random.random() < IDLE_PAUSE_PROBABILITY
                ):
                    s = random.uniform(DELAY_IDLE.min, DELAY_IDLE.max)
                    logger.info("Idle pause: %.0fs.", s)
                    await asyncio.sleep(s)

    def _level_hunt_mob(self, fallback: str = "") -> str:
        """SUIS-aware default hunt pin for current character level / area."""
        try:
            from dwar_bot.modules.suis_knowledge import default_hunt_mob, village_hunt_mob
            lvl = int(getattr(self._char, "level", 1) or 1)
            area = str(getattr(self._state, "area_id", "") or "")
            # Newbie village 932 has Крэтс only — Зигред-воин is not on the map
            if area in {"930", "931", "932"}:
                return village_hunt_mob(lvl)
            return default_hunt_mob(lvl)
        except Exception:
            area = str(getattr(self._state, "area_id", "") or "")
            if area in {"930", "931", "932"}:
                return fallback or "Крэтс"
            return fallback or "Крэтс"

    def _wo_farm_open(self, wo: Optional[dict] = None) -> bool:
        """Unified Flash side-quest → open farm gate."""
        try:
            from dwar_bot.modules.suis_knowledge import is_farm_open
            return is_farm_open(
                wo if wo is not None else (self.quests.pending_world_objective or {}),
                level=int(getattr(self._char, "level", 1) or 1),
            )
        except Exception:
            w = wo if wo is not None else (self.quests.pending_world_objective or {})
            return bool(
                w.get("farm_open")
                or (
                    w.get("flash_only")
                    and int(getattr(self._char, "level", 1) or 1) >= 3
                )
            )

    def _quest_kill_gated(self) -> bool:
        """True only for real type=2 / quest hunt pin — not open-farm soft targets."""
        try:
            if self.quests.has_pending_type2():
                return True
        except Exception:
            pass
        qmob = (self.quests.pending_hunt_mob or "").strip()
        if qmob:
            return True
        # brain.pending_hunt_mob alone is NOT a gate (open-farm uses farm_push)
        return False

    def _on_level_up_adapt_farm(self, new_level: int) -> None:
        """Retarget hunt / unlock travel after a level-up (esp. Lv3+ village exit)."""
        lvl = int(new_level or 1)
        try:
            from dwar_bot.modules.suis_knowledge import default_hunt_mob
            mob = default_hunt_mob(lvl)
        except Exception:
            mob = "Крэтс" if lvl <= 2 else "Зигред-воин"
        # Drop stale village pin (Крэтс) once we outgrew the newbie bracket
        qmob = (self.quests.pending_hunt_mob or "").strip()
        qlow = qmob.lower()
        village_stale = qlow in {"крэтс", "крейтс", "krats"} or qlow == "крейт"
        try:
            has_t2 = bool(self.quests.has_pending_type2())
        except Exception:
            has_t2 = False
        if lvl >= 3 and village_stale and not has_t2:
            self.quests.pending_hunt_mob = ""
            qmob = ""
            self.brain.need_quest_unlock = False
        # Real quest kill pin only — never arm open-farm as pending_hunt_mob
        # (pending_hunt_mob → every WIN arms turn-in and stalls casual farm).
        if has_t2 and qmob and not (lvl >= 3 and village_stale):
            self.brain.pending_hunt_mob = qmob
        else:
            self.brain.pending_hunt_mob = ""
            if not has_t2:
                self.brain.awaiting_quest_turnin = False
        self.brain.push_farm(600.0 if lvl >= 3 else 300.0)
        self.brain.mark_cooldown("Расселина", 300)
        logger.info(
            "Level-up adapt farm → Lv%d prefer '%s' via farm_push (quest_pin=%s).",
            lvl, mob, self.brain.pending_hunt_mob or "—",
        )
        wo = self.quests.pending_world_objective or {}
        if wo.get("kind") == "heal_wounded" and wo.get("flash_only") and lvl >= 3:
            wo = dict(wo)
            wo["farm_open"] = True
            wo["flash_notified"] = True
            self.quests.pending_world_objective = wo
            try:
                self.quests._persist_world_objective()
            except Exception:
                pass
            logger.info(
                "Lv%d: heal_wounded stays soft (farm_open) — hunt/exit unlocked.",
                lvl,
            )

    async def _maybe_send_report(self) -> None:
        try:
            await self.analytics.maybe_send_heartbeat()
        except Exception as exc:
            logger.debug("auto report failed: %s", exc)

    async def _emit_state_notifications(self) -> None:
        """Compare with previous snapshot and push Telegram alerts."""
        # Level up — adapt farm priorities for the new bracket
        if self._prev_level and self._char.level > self._prev_level:
            new_lv = int(self._char.level or 0)
            await self.notify(
                f"⬆️ Уровень! <b>{self._char.nick}</b> теперь Lv{new_lv}",
                "level_up",
            )
            try:
                self._on_level_up_adapt_farm(new_lv)
            except Exception as exc:
                logger.debug("level_up farm adapt: %s", exc)
        elif not self._prev_level and int(self._char.level or 0) >= 3:
            # Already Lv3+ at start / after restart — unlock open farm once
            try:
                self._on_level_up_adapt_farm(int(self._char.level))
            except Exception as exc:
                logger.debug("startup farm adapt: %s", exc)
        self._prev_level = self._char.level or self._prev_level

        # Money change — notify on ≥0.01 (1 серебро), show зол./сер.
        cur_money = money_from_state(self._state)
        if self._prev_money >= 0 and abs(cur_money - self._prev_money) >= 0.01:
            delta = cur_money - self._prev_money
            await self.notify(
                f"💰 Деньги: {format_money_delta(delta)} → "
                f"<b>{format_money_from_state(self._state)}</b>",
                "money",
            )
        self._prev_money = cur_money
        try:
            self.achievements.note_money_level(
                cur_money, int(self._char.level or 0),
            )
            if self._state.area_id:
                self.achievements.note_area(str(self._state.area_id))
        except Exception:
            pass

        # Area change
        if self._prev_area and self._state.area_id and self._state.area_id != self._prev_area:
            await self.notify(
                f"🗺 Новая локация: <b>{self._area_title or self._state.area_id}</b> "
                f"(id={self._state.area_id})",
                "area",
            )
        if self._state.area_id:
            self._prev_area = self._state.area_id

        # HP low
        if self._char.hp_max and self._char.hp_percent < self.settings.farm.hp_retreat:
            if not self._hp_low_sent:
                await self.notify(
                    f"❤️ HP низко: {self._char.hp}/{self._char.hp_max} "
                    f"({self._char.hp_percent:.0f}%)",
                    "hp_low",
                )
                self._hp_low_sent = True
        else:
            self._hp_low_sent = False

        # Effects
        if self.settings.notify.effects and self._profile.effects:
            titles = ", ".join(e.title for e in self._profile.effects[:4])
            if self._iteration % 20 == 1:
                await self.notify(f"✨ Эффекты: {titles}", "effects")

    async def _tick(self) -> None:
        """Sense → plan → execute one progression action."""
        farm = self.settings.farm
        self.brain.settings = self.settings  # keep toggles live

        self._profile = await self.stats.read_full_profile()
        self._char = self._profile.char
        self._state = self._profile.state

        # Fight-first: never plan NPC/travel while locked in battle
        if bool(getattr(self._state, "fight_id", 0)) or bool(
            getattr(self._state, "flags", 0) & 0x1
        ):
            logger.info(
                "⚔️ Активный бой fight_id=%s — завершаю до планирования.",
                getattr(self._state, "fight_id", 0) or "?",
            )
            result = await self.combat.finish_fight(timeout=180.0)
            logger.info("Fight-first → %s", result.name)
            if result == BattleResult.WIN:
                self.combat.session.battles_joined += 1
                self.quests.clear_exhausted(local_only=True)
                gated = self._quest_kill_gated()
                self.brain.mark_hunt_kill_done(quest_gate=gated)
                if gated:
                    try:
                        turned = await self.quests.retry_pending_type2()
                        if turned:
                            self.brain.clear_hunt_gate()
                            self.quests.clear_hunt_gate()
                    except Exception as exc:
                        logger.debug("fight-first turn-in: %s", exc)
                elif not self.brain.farm_push_active():
                    self.brain.push_farm(180.0)
            await _sleep(1.0, 2.5)
            return

        # Overloaded bag blocks travel and quest objectives — free junk first
        try:
            dropped = await self.combat.free_backpack()
            if dropped:
                self._profile = await self.stats.read_full_profile()
                self._char = self._profile.char
                self._state = self._profile.state
        except Exception as exc:
            logger.debug("free_backpack: %s", exc)

        if not self._char.nick:
            # In-fight lock: user.php returns a fight.php redirect stub without
            # ``var par`` — soft recheck still passes because state.fight_id is set.
            # Old path skipped forever → "Soft recheck OK but nick still empty".
            fight_id = int(getattr(self._state, "fight_id", 0) or 0)
            html_hint = ""
            try:
                html_hint = await self.stats._fetch_user_page()
            except Exception:
                html_hint = ""
            fight_locked = bool(fight_id) or is_fight_lock_html(html_hint)
            if fight_locked:
                logger.warning(
                    "Nick empty but fight active (fight_id=%s) — finishing fight…",
                    fight_id or "?",
                )
                result = await self.combat.finish_fight(timeout=180.0)
                logger.info("In-fight recover → %s", result.name)
                if result == BattleResult.WIN:
                    self.combat.session.battles_joined += 1
                    self.quests.clear_exhausted(local_only=True)
                    gated = self._quest_kill_gated()
                    self.brain.mark_hunt_kill_done(quest_gate=gated)
                    if gated:
                        try:
                            turned = await self.quests.retry_pending_type2()
                            if turned:
                                self.brain.clear_hunt_gate()
                                self.quests.clear_hunt_gate()
                                logger.info("After fight recover: type=2 turn-in OK")
                        except Exception as exc:
                            logger.debug("turn-in after recover: %s", exc)
                    elif not self.brain.farm_push_active():
                        self.brain.push_farm(180.0)
                # Re-read profile after fight unlocks user.php
                self._profile = await self.stats.read_full_profile()
                self._char = self._profile.char
                self._state = self._profile.state
                if self._char.nick:
                    logger.info(
                        "After fight: %s Lv%d fight_id=%s",
                        self._char.nick, self._char.level, self._state.fight_id,
                    )
                else:
                    logger.warning("Still no nick after fight finish — soft wait.")
                    await _sleep(3.0, 6.0)
                    return
            else:
                logger.warning("No character data — soft recheck (no invalidate).")
                ok = await self._client.soft_recheck_session()
                if ok:
                    st2 = await self._client.get_state()
                    if st2.fight_id:
                        self._state = st2
                        logger.warning(
                            "Soft OK + fight_id=%s — finish fight next.",
                            st2.fight_id,
                        )
                        result = await self.combat.finish_fight(timeout=180.0)
                        logger.info("Deferred fight finish → %s", result.name)
                        if result == BattleResult.WIN:
                            self.combat.session.battles_joined += 1
                            self.quests.clear_exhausted(local_only=True)
                            gated = self._quest_kill_gated()
                            self.brain.mark_hunt_kill_done(quest_gate=gated)
                            if gated:
                                try:
                                    turned = await self.quests.retry_pending_type2()
                                    if turned:
                                        self.brain.clear_hunt_gate()
                                        self.quests.clear_hunt_gate()
                                except Exception as exc:
                                    logger.debug("turn-in after deferred fight: %s", exc)
                            elif not self.brain.farm_push_active():
                                self.brain.push_farm(180.0)
                        self._profile = await self.stats.read_full_profile()
                        self._char = self._profile.char
                        self._state = self._profile.state
                        if self._char.nick:
                            logger.info(
                                "Recovered after deferred fight — nick=%s",
                                self._char.nick,
                            )
                    if not self._char.nick:
                        self._profile = await self.stats.read_full_profile()
                        self._char = self._profile.char
                        self._state = self._profile.state
                        if self._char.nick:
                            logger.info("Soft recheck OK — nick=%s", self._char.nick)
                        else:
                            logger.warning(
                                "Soft recheck OK but nick still empty — skip tick."
                            )
                            await _sleep(5.0, 10.0)
                            return
                else:
                    logger.warning(
                        "Soft recheck failed — waiting for cookies (no blind renew)."
                    )
                    self._token_ok = False
                    raise TokenExpiredError(
                        "Session dead and OAuth renew unsafe — paste fresh cookies."
                    )

        if not self._char.nick:
            await _sleep(3.0, 6.0)
            return

        logger.info(
            "[%d] %s Lv%d | HP %d/%d (%.0f%%) | MP %d/%d | area=%s | %s | предметов=%d | sid=%s…",
            self._iteration,
            self._char.nick, self._char.level,
            self._char.hp, self._char.hp_max, self._char.hp_percent,
            self._char.mp, self._char.mp_max,
            self._state.area_id, format_money_from_state(self._state),
            len(self._profile.inventory),
            (self._client._session.get("sess_sid") or "")[:8],
        )

        # HP≈0: auto-resurrect (phoenix / altar / codes). Never sit 180s on a ghost.
        if self._char.hp_max and self._char.hp_percent <= 0.5:
            if await self.combat.is_in_battle():
                result = await self.combat.finish_fight(timeout=180.0)
                logger.info("HP≈0 fight finish → %s", result.name)
                await _sleep(1.0, 2.0)
                return

            rez_ok = False
            if getattr(farm, "auto_resurrect", True):
                try:
                    # Clear resurrect cooldown so we can retry every tick when stuck
                    self.resurrection.session.last_attempt_at = 0.0
                    rez = await self.resurrection.ensure_alive(self)
                    if rez.ok:
                        rez_ok = True
                        if rez.method != "already_alive":
                            logger.info("Auto-resurrect: %s", rez.summary)
                        # Heal up after rez before farming
                        if (
                            farm.auto_heal
                            and self._char.hp_max
                            and self._char.hp_percent < float(farm.hp_heal or 55)
                            and self._char.hp > 0
                        ):
                            try:
                                await self.combat.heal_if_needed(self._profile)
                            except Exception:
                                pass
                            try:
                                await self.timers.wait_for_hp(
                                    target_percent=max(50.0, float(farm.hp_heal or 55)),
                                    max_wait=45,
                                )
                            except Exception:
                                await _sleep(5.0, 10.0)
                        return
                    logger.warning("Auto-resurrect failed: %s", rez.summary)
                except Exception as exc:
                    logger.warning("auto-resurrect error: %s", exc)

            if farm.auto_heal and getattr(farm, "use_potions", True) and self._char.hp > 0:
                try:
                    drank = await self.combat.heal_if_needed(
                        self._profile, force_threshold=100.0,
                    )
                    if drank:
                        self._profile = await self.stats.read_full_profile()
                        self._char = self._profile.char
                        self._state = self._profile.state
                except Exception as exc:
                    logger.debug("HP≈0 potion: %s", exc)

            if self._char.hp_max and self._char.hp_percent <= 0.5:
                # Ghost-locked: short pause then retry resurrect next tick
                # (do NOT wait_for_hp 180s — regen never ticks at HP=0).
                if not rez_ok and getattr(farm, "auto_resurrect", True):
                    logger.warning(
                        "HP≈0 ghost-locked — retry resurrect next tick "
                        "(skip long regen wait)."
                    )
                    try:
                        if self.settings.notify.hp_low:
                            now = time.time()
                            last = float(getattr(self, "_ghost_tg_at", 0.0) or 0.0)
                            if now - last >= 300.0:
                                self._ghost_tg_at = now
                                await self.notify(
                                    "💀 HP=0 (дух). Реген не работает — "
                                    "бот пробует возрождение. "
                                    "Нужны Перо Феникса / алтарь «Возродиться».",
                                    "hp_low",
                                )
                    except Exception:
                        pass
                    await _sleep(8.0, 15.0)
                    return

                logger.info("HP≈0 — short regen probe (alive but empty).")
                if farm.auto_heal:
                    try:
                        ok = await self.timers.wait_for_hp(
                            target_percent=50.0, max_wait=45,
                        )
                        if not ok and getattr(farm, "auto_resurrect", True):
                            self.resurrection.session.last_attempt_at = 0.0
                            await self.resurrection.ensure_alive(self)
                    except Exception:
                        await _sleep(10.0, 20.0)
                else:
                    await _sleep(10.0, 20.0)
                return

        await self.timers.update_regen(self._char.hp, self._char.mp)
        await self._emit_state_notifications()

        # ------------------------------------------------------------------
        # PURE FARM: hunt-only when quests off, PURE_FARM_ONLY=1, or story
        # stalled on Flash heal_wounded (no HTTP progress possible).
        # ------------------------------------------------------------------
        pure_farm_force = os.getenv("PURE_FARM_ONLY", "0").strip().lower() in (
            "1", "true", "yes", "on",
        )
        # Post-village open farm: area 227 spiders etc. give real gold;
        # flavor NPCs must not block hunting after heal/arsenal unlock.
        # Rare planner yield (every 12th tick) for fronts / real quests / events.
        try:
            area_now = str(self._state.area_id or "")
            wo_empty = not bool(self.quests.pending_world_objective)
        except Exception:
            area_now, wo_empty = "", True
        post_village_open_farm = (
            area_now in {"192", "227", "226", "159", "228"}
            and wo_empty
            and int(self._char.level or 1) >= 3
            and bool(farm.max_farm and farm.auto_combat and farm.farm_area)
        )
        planner_tick = (int(getattr(self, "_iteration", 0) or 0) % 12 == 0)
        if post_village_open_farm and not planner_tick:
            pure_farm_force = True
        elif post_village_open_farm and planner_tick:
            pure_farm_force = False
            logger.info(
                "MaxFarm planner tick#%d — quests/events/fronts this cycle.",
                getattr(self, "_iteration", 0),
            )
        wo_early = {}
        try:
            wo_early = dict(self.quests.pending_world_objective or {})
        except Exception:
            wo_early = {}
        story_stalled = (
            str(wo_early.get("kind") or "") == "heal_wounded"
            and bool(wo_early.get("flash_only") or wo_early.get("http_impossible"))
            and float(wo_early.get("stalled_until") or 0) > time.time()
        )
        # Expired stall → one story-check this tick, then re-stall if needed
        if (
            str(wo_early.get("kind") or "") == "heal_wounded"
            and float(wo_early.get("stalled_until") or 0)
            and float(wo_early.get("stalled_until") or 0) <= time.time()
        ):
            try:
                wo_clr = dict(wo_early)
                wo_clr.pop("stalled_until", None)
                self.quests.pending_world_objective = wo_clr
                self.quests._persist_world_objective()
                self.quests.clear_exhausted(local_only=True)
                self.quests.clear_world_objective_npc_ban(
                    str(wo_clr.get("npc_id") or "409")
                )
                logger.info(
                    "heal_wounded stall expired — story-check Вождя this tick."
                )
            except Exception as exc:
                logger.debug("clear stall: %s", exc)
            story_stalled = False

        if self.pure_farm.should_run(
            max_farm=bool(farm.max_farm),
            area_id=str(self._state.area_id or ""),
            level=int(self._char.level or 1),
            auto_quests=bool(farm.auto_quests),
            force=pure_farm_force,
            story_stalled=story_stalled,
        ):
            reason = (
                "Flash heal stalled — hunt for real wins"
                if story_stalled
                else (
                    "post-village open farm"
                    if post_village_open_farm
                    else ("hunt-only force" if pure_farm_force else "hunt-only filler")
                )
            )
            try:
                await self.controller.apply_directive(
                    StrategicDirective(
                        state=BotState.FARMING,
                        priority=1,
                        title="Pure Farm",
                        reason=reason,
                        area_id=str(self._state.area_id or ""),
                        exp_per_hour=0.0,
                    )
                )
            except Exception:
                pass
            if story_stalled and not getattr(self, "_flash_stall_logged", False):
                self._flash_stall_logged = True
                logger.info(
                    "Story stalled on Flash heal_wounded — PureFarm idle "
                    "(0-exp Cretas skipped; click wounded in client to unlock exit)."
                )
            handled = await self.pure_farm.run_tick(self)
            if handled:
                # Real gold; Exp only from leveling engine (never invent Cretas Exp)
                exp_proxy = 0.0
                try:
                    if not getattr(self.pure_farm.stats, "zero_reward", False):
                        exp_proxy = float(
                            self.leveling.progress.exp_per_hour
                            * max(
                                0.01,
                                (time.time() - self.leveling._session_started) / 3600.0,
                            )
                        )
                except Exception:
                    exp_proxy = 0.0
                self.telemetry.note_economy(
                    gold=money_from_state(self._state),
                    exp_proxy=exp_proxy,
                    battles=self.combat.session.battles_joined,
                    wins=max(self.combat.session.wins, self.pure_farm.stats.wins),
                    potions_used=self.combat.session.potions_used,
                    quests_completed=self.quests.session.quests_completed,
                )
                # Zero-reward idle already slept; keep loop calm
                if getattr(self.pure_farm.stats, "zero_reward", False):
                    await _sleep(2.0, 5.0)
                else:
                    await _sleep(1.0, 2.5)
                return
        elif farm.auto_quests and self._iteration <= 2:
            logger.info(
                "Story/quests mode ON — PureFarm filler skipped "
                "(set PURE_FARM_ONLY=1 to force hunt-only)."
            )

        # Village: equip bag gear once per session (not every 3 ticks)
        if (
            farm.auto_quests
            and str(self._state.area_id or "") in {"930", "931", "932"}
            and not getattr(self, "_bag_equipped_once", False)
        ):
            try:
                # Open kits/captives first (meta.id protocol) — real loot progress
                n_open = await self.combat.open_bag_actions(max_actions=6)
                if n_open:
                    logger.info("Village bag actions: %d", n_open)
                    await self.combat.free_backpack(target_free=3, max_drops=25)
                n_eq = await self.combat.equip_from_bag()
                self._bag_equipped_once = True
                if n_eq and self.settings.notify.gear:
                    await self.notify(f"👕 Из сумки надето: {n_eq}", "gear")
                if n_open and self.settings.notify.loot:
                    await self.notify(f"🎁 Сумка: открыто действий {n_open}", "loot")
            except Exception as exc:
                logger.debug("village equip_from_bag: %s", exc)
                self._bag_equipped_once = True

        # Periodically open bag loot even after first equip (kits stack up)
        if (
            farm.auto_quests
            and str(self._state.area_id or "") in {"930", "931", "932"}
            and self._iteration > 0
            and self._iteration % 8 == 0
            and not await self.combat.is_in_battle()
        ):
            try:
                n_open = await self.combat.open_bag_actions(max_actions=3)
                if n_open:
                    await self.combat.free_backpack(target_free=2, max_drops=15)
            except Exception as exc:
                logger.debug("periodic bag actions: %s", exc)

        # Economy snapshot every tick (feeds Gold/hr Exp/hr)
        self.telemetry.note_economy(
            gold=money_from_state(self._state),
            exp_proxy=self.leveling.progress.exp_per_hour * max(
                0.01, (time.time() - self.leveling._session_started) / 3600.0
            ),
            battles=self.combat.session.battles_joined,
            wins=self.combat.session.wins,
            potions_used=self.combat.session.potions_used,
            quests_completed=self.quests.session.quests_completed,
        )

        for note in self._profile.notifications[:3]:
            logger.info("📢 %s", note.text[:150])

        if self._profile.effects:
            logger.info(
                "Эффекты: %s",
                ", ".join(e.title for e in self._profile.effects[:4]),
            )

        # World sense — fetch each resource at most once per tick
        area = await self._client.get_area_info()
        if area.title:
            self._area_title = area.title
        self._area_items = [
            {
                "name": i.name,
                "item_type": i.item_type,
                "code": i.code,
                "npc_id": i.npc_id,
                "area_id": i.area_id,
                "action_id": i.action_id,
            }
            for i in area.items
        ]

        hunt: dict = {}
        try:
            hunt = await self._client.get_hunt_conf()
        except Exception:
            hunt = {}
        self._npcs = [
            {
                "title": n.get("title", ""),
                "time_left": n.get("time_left", 0),
                "npc_id": n.get("npc_id", ""),
                "url": n.get("url", ""),
            }
            for n in (hunt.get("npcs") or [])
        ]

        local_npcs = await self.quests.list_available_npcs(area=area, hunt=hunt)
        story_npc = None
        try:
            story_npc = await self.quests.resolve_current_npc()
        except Exception as exc:
            logger.debug("resolve_current_npc: %s", exc)

        # fight_id / flags already on state from profile — no extra dummy round-trip
        in_battle = bool(self._state.flags & 0x1) or bool(self._state.fight_id)

        # Quest type=2 kill gate ↔ world objective
        if self.quests.has_world_objective():
            wo = self.quests.pending_world_objective or {}
            farm_open = self._wo_farm_open(wo)
            keep_unlock = bool(farm_open and self.brain.need_quest_unlock)
            has_t2 = False
            try:
                has_t2 = bool(self.quests.has_pending_type2())
            except Exception:
                has_t2 = False
            if farm_open and has_t2:
                # Real story kill under open farm — keep quest pin
                if self.quests.pending_hunt_mob:
                    self.brain.pending_hunt_mob = self.quests.pending_hunt_mob
            else:
                # Soft WO / flash wait: drop false open-farm hunt pins
                self.brain.clear_hunt_gate()
                if not has_t2:
                    self.quests.clear_hunt_gate()
                self.brain.awaiting_quest_turnin = False
            if keep_unlock:
                self.brain.need_quest_unlock = True
        elif self.quests.pending_hunt_mob:
            self.brain.pending_hunt_mob = self.quests.pending_hunt_mob
            if not self.brain.awaiting_quest_turnin:
                self.brain.need_quest_unlock = True
        elif not self.brain.awaiting_quest_turnin:
            # No live quest kill — drop soft brain pin so open farm stays ungated
            if self.brain.pending_hunt_mob:
                self.brain.pending_hunt_mob = ""
                self.brain.need_quest_unlock = False

        # Attempt world objective (medicine USE etc.) before planning
        if self.quests.has_world_objective():
            wo = self.quests.pending_world_objective or {}
            # Self-heal: known Flash heal never probes HTTP USE again
            if wo.get("kind") == "heal_wounded" and (
                wo.get("flash_only") or "не задано" in str(wo.get("last_error") or "").lower()
            ):
                try:
                    self.quests.lock_flash_world_objective(cooldown_sec=86400.0)
                except Exception:
                    pass
            # Every few ticks under farm_open: unban local story NPCs for dialogue
            if (
                (wo.get("farm_open") or int(getattr(self._char, "level", 1) or 1) >= 3)
                and self._iteration % 4 == 1
            ):
                if not wo.get("farm_open"):
                    wo = dict(wo)
                    wo["farm_open"] = True
                    self.quests.pending_world_objective = wo
                    try:
                        self.quests._persist_world_objective()
                    except Exception:
                        pass
                cleared = self.quests.clear_exhausted(local_only=True)
                # Do NOT clear WO giver ban every tick when flash-locked —
                # that re-opens «излечение ополченцев» and SET-spams Telegram.
                wo_now = self.quests.pending_world_objective or {}
                if not (
                    wo_now.get("kind") == "heal_wounded"
                    and (wo_now.get("flash_only") or wo_now.get("http_impossible"))
                ):
                    cleared += self.quests.clear_world_objective_npc_ban()
                if cleared:
                    logger.info(
                        "farm_open story refresh: cleared %d NPC ban(s).",
                        cleared,
                    )
            try:
                done = await self.quests.pursue_world_objective()
                if done:
                    logger.info("World objective completed this tick.")
                    await _sleep(1.0, 2.0)
                    return
                wo = self.quests.pending_world_objective or {}
                if wo.get("flash_only") and wo.get("flash_notified") and not wo.get("tg_flash_sent"):
                    wo["tg_flash_sent"] = True
                    self.quests.pending_world_objective = dict(wo)
                    await self.notify(
                        "🧪 <b>Снадобье — только Flash/клиент</b>\n"
                        "HTTP USE не принимает цель «раненые».\n"
                        "Кликните раненых ополченцев в игре, либо дождитесь "
                        "обновления протокола. Бот не спамит NPC #816/#817.",
                        "quests",
                    )
            except Exception as exc:
                logger.debug("pursue_world_objective: %s", exc)

        event_timers = await self.timers.scrape_event_timers(hunt=hunt)
        # Empty bag: light farm nudge, not a 10‑minute hunt lock
        if not self._profile.inventory and not self.brain.farm_push_active():
            if self._iteration <= 2 and self.brain.empty_streak("Расселина") >= 2:
                self.brain.push_farm(180.0)

        # Knowledge Base 24/7 ingest (mobs from hunt_farm)
        hunt_bots: list = []
        try:
            hunt_bots = await self._client.get_hunt_bots(self._state.area_id or "")
        except Exception as exc:
            logger.debug("get_hunt_bots: %s", exc)
        self.leveling.observe_world(
            profile=self._profile,
            area_id=str(self._state.area_id or ""),
            area_title=self._area_title,
            hunt_bots=hunt_bots,
            npc_id=str(getattr(story_npc, "npc_id", "") or ""),
            event_timers=event_timers,
        )

        snap = self.brain.analyze(
            profile=self._profile,
            area=area,
            npcs=self._npcs,
            story_npc=story_npc,
            local_npcs=local_npcs,
            in_battle=in_battle,
            event_timers=event_timers,
            exhausted_npcs=self.quests.exhausted_npc_ids(),
        )

        # Level-Up Decision Tree → MasterController + optional focus override
        wo = self.quests.pending_world_objective or {}
        farm_open = self._wo_farm_open(wo)
        decision = self.leveling.decide(
            profile=self._profile,
            brain_focus=snap.focus,
            brain_options=snap.options,
            area_id=str(self._state.area_id or ""),
            pending_hunt_mob=(
                "" if (self.quests.has_world_objective() and not farm_open)
                else (self.brain.pending_hunt_mob or self.quests.pending_hunt_mob)
            ),
            awaiting_turnin=(
                False if (self.quests.has_world_objective() and not farm_open)
                else (
                    self.brain.awaiting_quest_turnin
                    if (
                        self.brain.need_quest_unlock
                        or self.brain.pending_hunt_mob
                        or self.quests.has_pending_type2()
                    )
                    else False
                )
            ),
            need_quest_unlock=(
                False if (self.quests.has_world_objective() and not farm_open)
                else self.brain.need_quest_unlock
            ),
            in_battle=in_battle,
            world_objective_kind=str(wo.get("kind") or ""),
            world_objective_flash_only=bool(wo.get("flash_only")),
            blocked_npc_ids=self.quests.world_objective_npc_ids(),
        )
        await self.leveling.apply(decision)
        if decision.focus_override is not None:
            ov = decision.focus_override
            brain = snap.focus
            # Under world_objective farm_open: keep brain GEAR/QUEST if they beat farm override
            keep_brain = False
            flash_locked = bool(
                wo.get("kind") == "heal_wounded"
                and (wo.get("flash_only") or wo.get("http_impossible"))
            )
            brain_npc = ""
            if brain is not None and brain.action == ActionType.QUEST_NPC:
                brain_npc = str((brain.payload or {}).get("npc_id") or "")
            skip_keep_quest = bool(
                flash_locked
                and brain is not None
                and brain.action == ActionType.QUEST_NPC
                and brain_npc
                and brain_npc in self.quests.world_objective_npc_ids()
            )
            if (
                brain is not None
                and not skip_keep_quest
                and getattr(decision.progress, "mode", "") == "world_objective"
                and brain.action in (
                    ActionType.EQUIP,
                    ActionType.REPAIR,
                    ActionType.QUEST_NPC,
                    ActionType.USE_ITEM,
                )
                and float(brain.score) + 20.0 >= float(ov.score)
            ):
                keep_brain = True
                logger.info(
                    "Keep brain '%s' (%.0f) over world override '%s' (%.0f)",
                    brain.title, brain.score, ov.title, ov.score,
                )
            if keep_brain:
                pass
            elif (
                snap.focus is None
                or ov.score >= (snap.focus.score - 50)
                or decision.directive.priority <= 1
                or decision.boost_needed
                or getattr(decision.progress, "mode", "") == "world_objective"
            ):
                snap.focus = ov
                snap.now = f"Level-Up: {ov.title}"

        focus = snap.focus
        focus_key = (
            f"{focus.action.value}:{focus.title}" if focus else "none"
        )
        logger.info(
            "🧠 Сейчас: %s | сила=%.0f | вариантов=%d | LV↑ %s (%.0f exp/h)",
            snap.now, snap.power_score, len(snap.options),
            decision.directive.state.name, decision.progress.exp_per_hour,
        )
        if focus:
            logger.info(
                "👉 Выбрано: %s [%s] score=%.0f — %s",
                focus.title, focus.action.value, focus.score, focus.detail,
            )
            if snap.plan:
                logger.info(
                    "📋 План: %s",
                    " → ".join(s.title for s in snap.plan[:4]),
                )

        # Notify on plan change (not every tick) — suppress hunt spam + village-exit bounce
        if focus_key != self._last_focus_key and focus and focus.action != ActionType.IDLE:
            prev = self._last_focus_key
            self._last_focus_key = focus_key
            same_hunt = (
                focus.action == ActionType.HUNT_MOB
                and prev.startswith("hunt_mob:")
            )
            village_bounce = (
                focus.action == ActionType.TRAVEL
                and any(
                    kw in (focus.title or "").lower()
                    for kw in ("сопк", "дымн")
                )
                and self.brain.village_exit_blocked()
            )
            if self.settings.notify.plan and not same_hunt and not village_bounce:
                await self.notify(
                    f"🧠 <b>{focus.title}</b>\n"
                    f"<i>{snap.now}</i>\n"
                    f"Сила {snap.power_score:.0f}/100 · "
                    f"вариантов {len(snap.options)}",
                    "plan",
                )
        elif focus_key != self._last_focus_key:
            self._last_focus_key = focus_key

        if not focus:
            logger.info("💤 Нет плана — жду.")
            await _sleep(8.0, 18.0)
            return

        loot_before = self._loot_claimed
        battles_before = self.combat.session.battles_joined
        wins_before = self.combat.session.wins
        dialogues_before = self.quests.session.dialogues_handled

        acted = await self._execute_focus(focus)

        progressed = (
            self._loot_claimed > loot_before
            or self.combat.session.battles_joined > battles_before
            or self.combat.session.wins > wins_before
            or self.quests.session.dialogues_handled > dialogues_before
            or (
                focus.action in (
                    ActionType.TRAVEL, ActionType.REPAIR, ActionType.EQUIP,
                    ActionType.HEAL, ActionType.WAIT_REGEN, ActionType.HUNT_MOB,
                )
                and acted
            )
            # Intentional world-objective NPC skip must not trigger Local recover
            or (
                acted
                and focus.action == ActionType.QUEST_NPC
                and self.quests.has_world_objective()
            )
            # Flash-only medicine wait is intentional — not stagnation
            or (
                acted
                and focus.action == ActionType.IDLE
                and bool((self.quests.pending_world_objective or {}).get("flash_only"))
            )
            # Skipping Расселина under flash_only is also intentional
            or (
                acted
                and focus.action in (ActionType.COMBAT_AREA, ActionType.AREA_ACTION)
                and bool((self.quests.pending_world_objective or {}).get("flash_only"))
            )
        )
        self.brain.note_result(focus, progressed=progressed)

        # Feed LevelingEngine kill / quest samples
        if self.combat.session.wins > wins_before:
            mob_name = (
                focus.payload.get("mob_name")
                if focus and isinstance(focus.payload, dict)
                else ""
            ) or self.brain.pending_hunt_mob or self.quests.pending_hunt_mob or focus.title
            self.leveling.note_kill(
                mob_name=str(mob_name),
                area_id=str(self._state.area_id or ""),
                fight_sec=40.0,
                level=self._char.level,
            )
        if self.quests.session.dialogues_handled > dialogues_before:
            self.leveling.note_quest_progress(title=focus.title if focus else "")
        if focus and focus.action == ActionType.BUFF and acted:
            self.leveling.mark_boost_used()

        # Idle multitasking while regenerating / between fights
        if decision.idle_tasks and (
            focus.action == ActionType.WAIT_REGEN
            or self._char.hp_percent < 55
        ):
            try:
                await self.controller.apply_directive(
                    StrategicDirective(
                        state=BotState.IDLE_TASKS,
                        priority=4,
                        title="Idle multitasking",
                        reason=",".join(decision.idle_tasks),
                        area_id=str(self._state.area_id or ""),
                        exp_per_hour=decision.progress.exp_per_hour,
                    )
                )
                done = await self.leveling.run_idle_tasks(self, decision.idle_tasks)
                if done:
                    logger.info("Level-Up idle tasks: %s", ", ".join(done))
            except Exception as exc:
                logger.debug("idle multitasking: %s", exc)

        # Periodic Telegram Level-Up Update (rich + telemetry rates)
        # Skip when Flash-stalled village farm produces 0 Exp/Gold — spam only.
        if self.leveling.should_report() and self.settings.notify.level_up:
            try:
                wo = self.quests.pending_world_objective or {}
                flash_stall = (
                    str(wo.get("kind") or "") == "heal_wounded"
                    and bool(wo.get("flash_only") or wo.get("farm_open"))
                )
                zero_pf = bool(getattr(self.pure_farm.stats, "zero_reward", False))
                eph = float(self.leveling.progress.exp_per_hour or 0)
                rates = self.telemetry.rates() if self.telemetry else {}
                tel_eph = float((rates or {}).get("exp_per_hour") or 0)
                gph = float((rates or {}).get("gold_per_hour") or 0)
                if (flash_stall or zero_pf) and eph <= 0.5 and gph <= 0.01:
                    # Mark reported so we don't retry every tick; quiet for interval
                    self.leveling.mark_reported()
                    logger.info(
                        "Skip Level-Up TG: flash/0-reward stall "
                        "(eph=%.0f tel=%.0f gph=%.2f).",
                        eph, tel_eph, gph,
                    )
                else:
                    text = self.rich.format_level_up_rich(
                        level=self.leveling.progress.level,
                        exp_pct=self.leveling.progress.exp_pct,
                        exp_per_hour=self.leveling.progress.exp_per_hour,
                        eta_seconds=self.leveling.progress.eta_seconds,
                        priority=self.leveling.progress.priority_title,
                        directive_state=self.controller.directive_summary().get(
                            "state", ""
                        ),
                    )
                    await self.notify(text, "level_up")
                    self.leveling.mark_reported()
            except Exception as exc:
                logger.debug("level-up update: %s", exc)

        # Stagnation → local recover / Cursor auto-heal
        try:
            await get_auto_healer().note_progress(focus_key, progressed)
        except Exception as exc:
            logger.debug("stagnation watch: %s", exc)

        if not acted and focus.action != ActionType.IDLE:
            if farm.auto_quests and focus.action == ActionType.QUEST_NPC:
                logger.info("Квест-действие без прогресса — помечаю NPC исчерпанным.")
            await _sleep(3.0, 7.0)
        elif focus.action == ActionType.IDLE:
            active_cd = self.timers.active_cooldowns()
            if active_cd:
                logger.info(
                    "⏱ Таймеры: %s",
                    ", ".join(
                        f"{c.description or c.name} {c.format_remaining()}"
                        for c in active_cd[:3]
                    ),
                )
            wo = self.quests.pending_world_objective or {}
            farm_open = self._wo_farm_open(wo)
            if wo.get("flash_only") and not farm_open:
                # Hard flash wait — no open farm yet
                await _sleep(45.0, 90.0)
            elif farm_open or farm.max_farm:
                # Open farm: don't freeze the loop on idle ticks
                await _sleep(0.8, 2.0)
            else:
                await _sleep(8.0, 18.0)

    async def admin_diagnose(self) -> str:
        """Full diagnostic dump for Telegram /diagnose."""
        st = self.get_status()
        rs = get_recovery_stats()
        lines = [
            "<b>🔬 Диагностика</b>",
            f"state=<code>{st.get('bot_state')}</code> token={'OK' if st.get('token_ok') else 'DEAD'}",
            f"auth_blocked={st.get('auth_blocked')} sid=<code>{st.get('sess_sid')}</code>",
            f"area={st.get('area_id')} fight_id={st.get('fight_id')}",
            f"unlock={st.get('need_quest_unlock')} hunt={st.get('pending_hunt_mob') or '—'}",
            f"world_obj={((st.get('world_objective') or {}).get('kind') or '—')}",
            f"wins={st.get('wins')} losses={st.get('losses')} loot={st.get('loot_claimed')}",
            "",
            "<b>Recovery</b>",
            f"last={rs.last_kind or '—'} auth={rs.auth_waits} net={rs.network_retries}",
            f"proto={rs.protocol_recovers} stag={rs.stagnation_local} cursor={rs.cursor_heals}",
        ]
        if rs.history:
            lines.append("<b>История:</b>")
            for h in rs.history[-6:]:
                lines.append(f"• <code>{_tg_esc(h)}</code>")
        # Soft session probe
        try:
            ok = await self._client.soft_recheck_session()
            lines.append(f"soft_recheck={'✅' if ok else '❌'}")
        except Exception as exc:
            lines.append(f"soft_recheck err: {exc}")
        return "\n".join(lines)

    async def admin_force_hunt(self, mob: str = "") -> str:
        mob = (
            mob
            or self.brain.pending_hunt_mob
            or self.quests.pending_hunt_mob
            or self._level_hunt_mob()
        )
        self.brain.mark_hunt_for_quest(mob)
        if await self.combat.is_in_battle():
            result = await self.combat.finish_fight()
            return f"Доигрывал бой → {result.name}"
        result = await self.combat.try_hunt_attack(name_substr=mob)
        if result == BattleResult.WIN:
            self.quests.clear_exhausted(local_only=True)
        return f"Охота '{mob}' → {result.name}"

    async def admin_recover(self) -> str:
        ok = await self._local_recover_stagnation("telegram:/recover")
        return "✅ Local recover сработал" if ok else "⚠️ Local recover без эффекта"

    async def admin_trigger_heal(self, reason: str = "manual") -> str:
        from dwar_bot.core.auto_healer import HealRequest
        ok = await get_auto_healer().heal(HealRequest(
            failed_file="dwar_bot/main.py",
            traceback_text=(
                "MANUAL HEAL via Telegram.\n"
                "Проверь hunt_farm + fight WS + quest type=2 + session soft path.\n"
                f"reason={reason}"
            ),
            reason="manual",
            force=True,
        ))
        return "✅ Heal запущен" if ok else "⚠️ Heal не стартовал (cooldown/AUTH/занят)"

    async def admin_restart_service(self) -> str:
        from dwar_bot.core.cursor_self_healer import mark_skip_boot_scan
        mark_skip_boot_scan("telegram-restart")
        # Detached restart like healer
        import subprocess
        subprocess.Popen(
            ["bash", "-c", "sleep 2; systemctl restart dwar_bot.service"],
            start_new_session=True,
        )
        return "♻️ Restart через 2с…"

    async def _local_recover_stagnation(self, focus_key: str) -> bool:
        """Break stuck loops → hunt kill / farm. Prefer quest mob over fronts."""
        logger.warning("Local recover for stagnation: %s → hunt/farm", focus_key)
        # Never open a parallel fight WS while the main tick is mid-combat
        if getattr(self.combat, "_fight_busy", False):
            logger.info(
                "Local recover: бой уже идёт (_fight_busy) — не стартую второй WS."
            )
            return False

        wo = self.quests.pending_world_objective or {}
        # Flash-only heal_wounded: intentional wait — try light hunt, never escalate TG spam
        if (
            wo.get("kind") == "heal_wounded"
            and (
                wo.get("flash_only")
                or wo.get("http_impossible")
                or "не задано" in focus_key.lower()
                or "heal_wounded" in focus_key.lower()
            )
        ):
            try:
                self.quests.lock_flash_world_objective(cooldown_sec=86400.0)
            except Exception:
                pass
            kind = wo.get("kind") or "world"
            logger.info(
                "Local recover: flash_only '%s' — quiet hunt/idle (no AutoHealer spam).",
                kind,
            )
            if "снадоб" in focus_key.lower() or "flash" in focus_key.lower():
                self.brain.mark_cooldown("Ждать снадобье / Flash", 120)
            if self.settings.farm.farm_area and self.settings.farm.auto_combat:
                if await self.combat.is_in_battle():
                    result = await self.combat.finish_fight()
                    return result in (
                        BattleResult.WIN, BattleResult.LOSE, BattleResult.ONGOING,
                    )
                result = await self.combat.try_hunt_attack(name_substr="")
                if result in (
                    BattleResult.WIN, BattleResult.JOINED,
                    BattleResult.ONGOING, BattleResult.LOSE,
                ):
                    logger.info("Local recover flash hunt → %s", result.name)
                    return True
            # Intentional open-farm under flash_only — consume stagnation,
            # do NOT escalate AutoHealer→Cursor (that was the FAIL spam).
            farm_open = self._wo_farm_open(wo)
            if farm_open or int(getattr(self._char, "level", 1) or 1) >= 3:
                self.brain.push_farm(300.0)
                return True
            return True
        # World objective (non-flash): pursue once, no hunt storm
        if self.quests.has_world_objective():
            kind = wo.get("kind")
            logger.info(
                "Local recover: world objective '%s' — pursue / idle, no hunt storm.",
                kind,
            )
            try:
                if await self.quests.pursue_world_objective():
                    return True
            except Exception as exc:
                logger.debug("local recover pursue_wo: %s", exc)
            if "quest_npc" in focus_key.lower() or "816" in focus_key or "817" in focus_key:
                self.brain.mark_cooldown(focus_key.split(":", 1)[-1][:40], 600)
            return True
        self.brain.push_farm(600.0)
        if "расселин" in focus_key.lower() or "combat_area" in focus_key.lower():
            self.brain.mark_cooldown("Расселина", 180)
        # Finish an abandoned fight first
        if await self.combat.is_in_battle():
            result = await self.combat.finish_fight()
            if result in (BattleResult.WIN, BattleResult.LOSE, BattleResult.ONGOING):
                logger.info("Local recover: finished active fight → %s", result.name)
                return True
        # Newbie unlock: kill Крэтс on hunt_farm
        if self.settings.farm.farm_area and self.settings.farm.auto_combat:
            # World objective / no live type=2 gate → light farm only, not forced Крэтс
            if self.quests.has_world_objective():
                # Lv3+ open farm under flash_only — prefer level mob, not empty/Крэтс
                wo = self.quests.pending_world_objective or {}
                if int(getattr(self._char, "level", 1) or 1) >= 3 or wo.get("farm_open"):
                    mob = (
                        self.brain.pending_hunt_mob
                        or self.quests.pending_hunt_mob
                        or self._level_hunt_mob()
                    )
                else:
                    mob = ""
            else:
                mob = (
                    self.brain.pending_hunt_mob
                    or self.quests.pending_hunt_mob
                    or (
                        "Крэтс" if self.brain.need_quest_unlock
                        else self._level_hunt_mob()
                    )
                )
            result = await self.combat.try_hunt_attack(name_substr=mob)
            if result in (
                BattleResult.WIN, BattleResult.JOINED,
                BattleResult.ONGOING, BattleResult.LOSE,
            ):
                logger.info("Local recover: hunt attack → %s", result.name)
                if result == BattleResult.WIN:
                    self.quests.clear_exhausted(local_only=True)
                return True
        # Re-open local quest NPC after kill attempt
        if self.brain.need_quest_unlock:
            cleared = self.quests.clear_exhausted(local_only=True)
            logger.info("Local recover: cleared exhausted NPCs=%d", cleared)
        if self.settings.farm.auto_travel and not self.brain.need_quest_unlock:
            moved = await self._try_area_progress()
            if moved:
                logger.info("Local recover: travelled to new area.")
                return True
        if (
            self.settings.farm.farm_fronts
            and self.settings.farm.auto_combat
            and not self.brain.need_quest_unlock
        ):
            result = await self.combat.try_join_front()
            if result in (BattleResult.JOINED, BattleResult.ONGOING):
                logger.info("Local recover: joined front.")
                return True
        return False

    async def _process_loot_response(self, resp, label: str = "") -> int:
        """Log / notify bonus_text + artifact macros from an API response."""
        if resp is None:
            return 0
        lines = []
        try:
            lines = resp.loot_lines()
        except Exception:
            for b in getattr(resp, "bonus_text", None) or []:
                if b:
                    lines.append(str(b))
        if not lines:
            return 0
        self._loot_claimed += len(lines)
        for line in lines[:6]:
            logger.info("🎁 %s%s", f"[{label}] " if label else "", line[:160])
        if self.settings.notify.loot:
            preview = "\n".join(f"• {l[:120]}" for l in lines[:4])
            await self.notify(
                f"🎁 Награда{f' · {label}' if label else ''}:\n{preview}",
                "loot",
            )
        return len(lines)

    async def _execute_focus(self, focus: GameOption) -> bool:
        """Run the single auto-selected action. Returns True if something useful happened."""
        farm = self.settings.farm
        action = focus.action
        payload = focus.payload or {}

        try:
            if action == ActionType.HUNT_MOB:
                wo = self.quests.pending_world_objective or {}
                if wo.get("flash_only"):
                    # Heal is Flash-only — village hunt still OK for Exp while waiting
                    logger.info(
                        "Hunt while flash_only '%s' — Exp farm, снадобье в клиенте.",
                        wo.get("kind"),
                    )
                mob_name = str(
                    payload.get("name")
                    or payload.get("mob_name")
                    or self.brain.pending_hunt_mob
                    or self.quests.pending_hunt_mob
                    or self._level_hunt_mob()
                )
                self._telemetry_battle_begin(source="hunt", mob_name=mob_name)
                if self.brain.pending_hunt_mob or self.quests.pending_hunt_mob:
                    self._telemetry_quest_begin(
                        f"Охота: {self.brain.pending_hunt_mob or self.quests.pending_hunt_mob}",
                    )
                if payload.get("finish_only") or await self.combat.is_in_battle():
                    result = await self.combat.finish_fight()
                else:
                    result = await self.combat.try_hunt_attack(
                        name_substr=mob_name,
                        area_id=str(payload.get("area_id") or ""),
                    )
                if result == BattleResult.WIN:
                    await self._telemetry_battle_end(result, mob_name=mob_name)
                    gated = self._quest_kill_gated()
                    self.brain.mark_hunt_kill_done(quest_gate=gated)
                    self.quests.clear_exhausted(local_only=True)
                    # Wear/repair loot before next pull — gear first, then story
                    try:
                        self._profile = await self.stats.read_full_profile()
                        self._char = self._profile.char
                        self._state = self._profile.state
                    except Exception:
                        pass
                    if farm.auto_repair:
                        try:
                            await self.combat.repair_broken_gear(self._profile)
                            self._profile = await self.stats.read_full_profile()
                            self._char = self._profile.char
                            self._state = self._profile.state
                        except Exception as exc:
                            logger.debug("post-hunt repair: %s", exc)
                    if farm.auto_equip:
                        try:
                            equipped = await self.combat.auto_equip(self._profile)
                            if equipped and self.settings.notify.gear:
                                await self.notify(f"👕 Надето после боя: {equipped}", "gear")
                        except Exception as exc:
                            logger.debug("post-hunt equip: %s", exc)
                    # Quest kill gate only — do not spam type=2 after casual farm
                    turned = 0
                    if gated:
                        try:
                            turned = await self.quests.retry_pending_type2()
                        except Exception as exc:
                            logger.debug("retry_pending_type2: %s", exc)
                    if turned:
                        self.brain.clear_hunt_gate()
                        self.quests.clear_hunt_gate()
                        # Refresh profile for accurate quest reward diffs
                        try:
                            self._profile = await self.stats.read_full_profile()
                            self._char = self._profile.char
                            self._state = self._profile.state
                        except Exception:
                            pass
                        await self._telemetry_quest_complete(
                            title=f"Охота на {mob_name}" if mob_name else "Охота",
                        )
                        return True
                    if self.quests.pending_hunt_mob:
                        self.brain.pending_hunt_mob = self.quests.pending_hunt_mob
                    # Soft story nudge only for real kill-gate; else keep open farm
                    if gated:
                        self.brain.farm_push_until = min(
                            self.brain.farm_push_until,
                            time.time() + 45.0,
                        )
                        logger.info(
                            "Post-hunt quest-gate: cleared NPCs + gear — check сюжет."
                        )
                    else:
                        if not self.brain.farm_push_active():
                            self.brain.push_farm(180.0)
                        logger.info("Post-hunt open-farm: gear done, continue pulls.")
                    return True
                if result in (BattleResult.JOINED, BattleResult.ONGOING, BattleResult.LOSE):
                    if result == BattleResult.LOSE:
                        await self._telemetry_battle_end(result, mob_name=mob_name)
                    return True
                # NO_BATTLE = soft skip (SUIS kill-limit / RF hygiene / no free mobs)
                # Must NOT log telemetry battle ERROR — that false-trips AutonomousLogWatcher.
                if result == BattleResult.NO_BATTLE:
                    try:
                        self.telemetry.cancel_battle()
                    except Exception:
                        pass
                    rem = 0.0
                    try:
                        rem = float(self.combat.hygiene_remaining_sec() or 0.0)
                    except Exception:
                        rem = 0.0
                    if rem > 1.0:
                        # Real wait — do NOT spin hunt every 10s with empty ticks
                        wait = min(rem, 90.0)
                        self.brain.mark_cooldown(focus.title, max(60.0, wait))
                        logger.info(
                            "Hunt hygiene break — sleeping %.0fs (%.0fs left total)",
                            wait, rem,
                        )
                        await _sleep(wait * 0.85, wait)
                        return False
                    self.brain.mark_cooldown(focus.title, 45)
                    logger.info(
                        "Hunt soft-skip (NO_BATTLE) «%s» — limit/hygiene/empty.",
                        mob_name or "?",
                    )
                    await _sleep(8.0, 15.0)
                    return False
                self.brain.mark_cooldown(focus.title, 60)
                await self._telemetry_battle_end(BattleResult.ERROR, mob_name=mob_name, notify=False)
                return False

            # Legacy: "already in fight" combat_area without action_id
            if "схватк" in (focus.detail or "") or (
                action == ActionType.COMBAT_AREA and not payload.get("action_id")
            ):
                result = await self.combat.finish_fight()
                return result in (
                    BattleResult.WIN, BattleResult.LOSE,
                    BattleResult.ONGOING, BattleResult.JOINED,
                )

            if action == ActionType.HEAL:
                healed = await self.combat.heal_if_needed(self._profile)
                return bool(healed)

            if action == ActionType.WAIT_REGEN:
                # If already dead — resurrect, don't sit on 600s regen
                if self._char.hp_max and self._char.hp <= 0:
                    logger.info("WAIT_REGEN but HP=0 — auto-resurrect instead.")
                    if getattr(farm, "auto_resurrect", True):
                        self.resurrection.session.last_attempt_at = 0.0
                        rez = await self.resurrection.ensure_alive(self)
                        return bool(rez.ok)
                    await _sleep(8.0, 15.0)
                    return False
                logger.info("🩹 Реген HP до безопасного порога…")
                if farm.auto_heal:
                    await self.notify("🩹 Восстановление HP…", "hp_low")
                    ok = await self.timers.wait_for_hp(
                        target_percent=70.0, max_wait=180,
                    )
                    if not ok and self._char.hp_max and self._char.hp <= 0:
                        if getattr(farm, "auto_resurrect", True):
                            self.resurrection.session.last_attempt_at = 0.0
                            await self.resurrection.ensure_alive(self)
                    return True
                await _sleep(15.0, 40.0)
                return True

            if action == ActionType.REPAIR:
                repaired = await self.combat.repair_broken_gear(self._profile)
                if repaired:
                    logger.info("Отремонтировано предметов: %d", repaired)
                    if self.settings.notify.gear:
                        await self.notify(f"🔧 Отремонтировано: {repaired}", "gear")
                return repaired > 0

            if action == ActionType.EQUIP:
                equipped = await self.combat.auto_equip(self._profile)
                if equipped:
                    logger.info("Надето предметов: %d", equipped)
                    if self.settings.notify.gear:
                        await self.notify(f"👕 Надето: {equipped}", "gear")
                return equipped > 0

            if action == ActionType.QUEST_NPC:
                npc_id = str(payload.get("npc_id") or "")
                if not npc_id:
                    return False
                # heal_wounded NPC stays blocked; under farm_open other local story NPCs OK
                wo = self.quests.pending_world_objective or {}
                farm_open = bool(
                    wo.get("farm_open")
                    or (
                        wo.get("flash_only")
                        and int(getattr(self._char, "level", 1) or 1) >= 3
                    )
                )
                wo_npcs = self.quests.world_objective_npc_ids()
                flash_locked = bool(
                    wo.get("kind") == "heal_wounded"
                    and (wo.get("flash_only") or wo.get("http_impossible"))
                )
                # Flash-locked heal giver: periodic story-check (new points / turn-in)
                # instead of permanent skip — click on wounded still needed in client.
                if npc_id in wo_npcs and flash_locked:
                    if self._iteration % 4 != 1:
                        logger.info(
                            "Skip NPC %s — heal_wounded flash-locked "
                            "(story-check next ticks).",
                            npc_id,
                        )
                        self.quests.mark_npc_exhausted(
                            npc_id,
                            global_npc=int(payload.get("global_npc", 0) or 0),
                            link_id=str(payload.get("link_id") or "0"),
                        )
                        return True
                    logger.info(
                        "Story-check NPC %s under flash heal_wounded — "
                        "ищу новые точки / приказ военачальника.",
                        npc_id,
                    )
                    self.quests.clear_world_objective_npc_ban(npc_id)
                    # fall through to walk_npc_api (story check inside)
                # Under farm_open, story NPC (often same id as heal_wounded giver, e.g. 409)
                # must be talkable again — Flash heal stays soft side-quest.
                if npc_id in wo_npcs and not farm_open:
                    logger.info(
                        "Skip NPC %s — world objective '%s' still pending.",
                        npc_id,
                        wo.get("kind"),
                    )
                    self.quests.mark_npc_exhausted(
                        npc_id,
                        global_npc=int(payload.get("global_npc", 0) or 0),
                        link_id=str(payload.get("link_id") or "0"),
                    )
                    key = (
                        f"{int(payload.get('global_npc', 0) or 0)}:"
                        f"{npc_id}:{payload.get('link_id') or '0'}"
                    )
                    self.quests._world_objective_keys.add(key)
                    self.quests._exhausted_dialogues.add(key)
                    self.quests._soft_ban_until[key] = time.time() + 1800.0
                    return False
                if npc_id in wo_npcs and farm_open:
                    logger.info(
                        "Allow story NPC %s under farm_open (was world-obj ban) — "
                        "сюжет важнее soft Flash '%s'.",
                        npc_id,
                        wo.get("kind"),
                    )
                    self.quests.clear_world_objective_npc_ban(npc_id)
                if self.quests.has_world_objective() and not farm_open:
                    logger.info(
                        "Skip NPC %s — active world objective '%s' (only %s).",
                        npc_id,
                        wo.get("kind"),
                        ",".join(sorted(wo_npcs)) or "—",
                    )
                    self.quests.mark_npc_exhausted(
                        npc_id,
                        global_npc=int(payload.get("global_npc", 0) or 0),
                        link_id=str(payload.get("link_id") or "0"),
                    )
                    # Intentional skip — do NOT count as stagnation
                    return True
                if farm_open and self.quests.has_world_objective():
                    logger.info(
                        "Story NPC %s while farm_open '%s' — сюжет/приказ поверх фарма.",
                        npc_id,
                        wo.get("kind"),
                    )
                self._telemetry_quest_begin(
                    str(focus.title or payload.get("title") or "Квест"),
                    npc_id=npc_id,
                )
                # Prefer finishing pending type=2 turn-in first
                if self.quests.has_pending_type2() and self.brain.awaiting_quest_turnin:
                    turned = await self.quests.retry_pending_type2()
                    if turned:
                        self.brain.clear_hunt_gate()
                        try:
                            self._profile = await self.stats.read_full_profile()
                            self._char = self._profile.char
                            self._state = self._profile.state
                        except Exception:
                            pass
                        await self._telemetry_quest_complete(title=str(focus.title or ""))
                        return True
                steps = await self.quests.walk_npc_api(
                    npc_id,
                    global_npc=int(payload.get("global_npc", 0) or 0),
                    link_id=str(payload.get("link_id") or "0"),
                    f_id=str(payload.get("f_id") or "0"),
                    area_id=str(payload.get("area_id") or "0"),
                    href=str(payload.get("href") or payload.get("url") or ""),
                )
                if steps:
                    logger.info("📜 Квестовых шагов: %d (NPC %s)", steps, npc_id)
                    if self.quests.has_world_objective():
                        self.brain.clear_hunt_gate()
                        self.brain.awaiting_quest_turnin = False
                        logger.info(
                            "Мир-цель '%s' — NPC %s забанен, иду выполнять вне диалога.",
                            self.quests.pending_world_objective.get("kind"),
                            npc_id,
                        )
                        await _sleep(1.0, 2.0)
                        return True
                    if self.quests.has_pending_type2() or self.quests.pending_hunt_mob:
                        # Type=2 asked for a kill mid-dialogue — hunt once, then turn in
                        mob = self.quests.pending_hunt_mob or "Крэтс"
                        self.brain.mark_hunt_for_quest(mob)
                        logger.info("Сюжет ждёт убийства '%s' — одна охота, потом сдача.", mob)
                    else:
                        self.brain.clear_hunt_gate()
                        # Dialogue-only progress without kill gate — still a milestone
                        if self.settings.notify.quests:
                            await self.notify(
                                f"📜 Диалог с NPC {npc_id}: <b>{steps}</b> шаг(ов)\n"
                                f"⏱ квест «{focus.title}» в трекинге с "
                                f"{time.strftime('%H:%M:%S', time.localtime(self.telemetry.active_quest.started_at)) if self.telemetry.active_quest else '—'}",
                                "quests",
                            )
                    await _sleep(2.0, 5.0)
                    return True
                self.quests.mark_npc_exhausted(
                    npc_id,
                    global_npc=int(payload.get("global_npc", 0) or 0),
                    link_id=str(payload.get("link_id") or "0"),
                )
                logger.info(
                    "NPC %s: диалог исчерпан / нужен прогресс квеста (бой/лут).",
                    npc_id,
                )
                return False

            if action == ActionType.USE_ITEM:
                if self.quests.has_world_objective():
                    return await self.quests.pursue_world_objective()
                return False

            if action in (ActionType.COMBAT_AREA, ActionType.AREA_ACTION):
                wo = self.quests.pending_world_objective or {}
                farm_open = bool(
                    wo.get("farm_open")
                    or (
                        wo.get("flash_only")
                        and int(getattr(self._char, "level", 1) or 1) >= 3
                    )
                )
                if wo.get("flash_only") and not farm_open:
                    logger.info(
                        "Точка '%s' пропущена — flash_only '%s' (нужен клик по раненым).",
                        payload.get("name") or "?",
                        wo.get("kind"),
                    )
                    self.brain.mark_cooldown(str(payload.get("name") or "точка"), 300)
                    await _sleep(20.0, 40.0)
                    return True
                if farm_open and wo.get("flash_only"):
                    logger.info(
                        "Лут-точка '%s' при farm_open — экип/добыча поверх Flash side-quest.",
                        payload.get("name") or "?",
                    )
                name = str(payload.get("name") or "точка")
                obj_id = str(payload.get("object_id") or self._state.area_id or "0")
                act_id = str(payload.get("action_id") or "")
                if not act_id:
                    return False
                logger.info("⚔️ Точка '%s' (action_id=%s)…", name, act_id)
                resp = await self._client.run_area_action(
                    object_id=obj_id,
                    action_id=act_id,
                    link_id=str(payload.get("link_id") or ""),
                    object_class=str(payload.get("object_class") or "AREA"),
                )
                loot_n = await self._process_loot_response(resp, label=name)
                err = str(resp.redirect_error or resp.error or "")
                # Always log outcome so empty loops are visible in /log
                if err and err.lower() not in ("false", "none", ""):
                    if resp.status == STATUS_OK and not resp.error:
                        logger.info("📖 %s: %s", name, err[:180])
                    else:
                        logger.info("Точка '%s': %s", name, err[:160])
                else:
                    logger.info(
                        "Точка '%s': status=%s loot=%d fight_hint=%s",
                        name, resp.status, loot_n,
                        bool(getattr(resp, "redirect_url", None)),
                    )
                if await self.combat.is_in_battle():
                    self.combat.session.battles_joined += 1
                    self.combat.session.consecutive_battles += 1
                    logger.info("⚔️ Бой начат через '%s'!", name)
                    self._telemetry_battle_begin(source="area", mob_name=name)
                    if self.settings.notify.battles:
                        await self.notify(f"⚔️ Бой через <b>{name}</b>!", "battles")
                    await _sleep(3.0, 6.0)
                    return True
                if loot_n > 0:
                    # Quest gather (e.g. ведро лавы) — return to story NPC ASAP
                    self.quests.clear_exhausted(local_only=True)
                    self.brain.pending_hunt_mob = ""
                    self.brain.mark_hunt_kill_done(quest_gate=True)
                    logger.info("📦 Добыча с точки — сдаём сюжетному NPC.")
                    await _sleep(1.0, 2.0)
                    return True
                # Empty / flavor-only → escalate CD (stop Расселина spam)
                if loot_n <= 0:
                    streak = self.brain.empty_streak(name) + 1  # note_result runs after
                    base = max(int(payload.get("ltime") or 0), int(payload.get("dtime") or 0), 90)
                    cd = min(600, base * max(1, streak))
                    self.brain.mark_cooldown(name, cd)
                    self.brain.push_farm(300.0)
                    # Also try action_run.php once (Flash client path)
                    link_href = str(payload.get("link_href") or "")
                    if link_href and "action_run.php" in link_href:
                        try:
                            ar = await self._client._get(link_href)
                            logger.debug("action_run.php → %s len=%d", ar.status_code, len(ar.text or ""))
                        except Exception:
                            pass
                    # Immediately try travel/front instead of waiting next tick
                    if streak >= 2 and self.settings.farm.auto_travel:
                        moved = await self._try_area_progress()
                        if moved:
                            return True
                    if streak >= 2 and self.settings.farm.farm_fronts:
                        fr = await self.combat.try_join_front()
                        if fr == BattleResult.JOINED:
                            await self.notify("⚔️ Фронт после пустой точки!", "battles")
                            return True
                await _sleep(1.5, 3.5)
                return loot_n > 0

            if action == ActionType.COMBAT_ARENA:
                time_left = int(payload.get("time_left") or 0)
                if time_left > 90:
                    logger.info("Арена на КД (%dс) — пропускаю, иду в фарм.", time_left)
                    self.brain.push_farm(300.0)
                    return False
                result = await self.combat.try_arena()
                if result == BattleResult.JOINED:
                    self._telemetry_battle_begin(source="arena", mob_name="Арена")
                    if self.settings.notify.battles:
                        await self.notify("⚔️ Арена: вступил в бой!", "battles")
                    return True
                if result == BattleResult.ONGOING:
                    return True
                # Failed join → don't retry for a while
                self.brain.mark_cooldown(focus.title, 120)
                self.brain.push_farm(300.0)
                return False

            if action == ActionType.COMBAT_FRONT:
                result = await self.combat.try_join_front()
                if result == BattleResult.JOINED:
                    await self.notify("⚔️ Фронт: вступил в бой!", "battles")
                    return True
                if result == BattleResult.ONGOING:
                    return True
                logger.info("Фронтов нет — продолжаю фарм/квест разблокировки.")
                self.brain.push_farm(300.0)
                return False

            if action == ActionType.TRAVEL:
                area_id = str(payload.get("area_id") or "")
                code = str(payload.get("code") or "COME_IN")
                name = str(payload.get("name") or area_id)
                if not area_id:
                    return False
                logger.info("🗺 Переход '%s' → area %s…", name, area_id)
                resp = await self._client.go_area(area_id, code=code)
                await self._process_loot_response(resp, label=f"переход {name}")
                err = str(resp.redirect_error or resp.error or "")
                if err and err.lower() not in ("false", "none", ""):
                    logger.info("Переход закрыт: %s", err[:160])
                    if "военачальник" in err.lower() or "покинуть селение" in err.lower():
                        wo = self.quests.pending_world_objective or {}
                        farm_open = bool(
                            wo.get("farm_open")
                            or (
                                wo.get("flash_only")
                                and int(getattr(self._char, "level", 1) or 1) >= 3
                            )
                            or int(getattr(self._char, "level", 1) or 1) >= 3
                        )
                        # Long ban — 5–10 min CD caused Hunt ↔ Sopki TG spam
                        self.brain.mark_village_exit_blocked(7200.0)
                        self.brain.mark_cooldown(f"Переход: {name}", 7200)
                        if farm_open:
                            # Stay farming in village; story NPC is Flash-locked
                            self.brain.need_quest_unlock = False
                            self.brain.push_farm(600.0)
                            logger.info(
                                "Выход закрыт военачальником — фарм в селении "
                                "(village exit banned 2h, Lv%d).",
                                int(getattr(self._char, "level", 1) or 1),
                            )
                        else:
                            cleared = self.quests.clear_exhausted(local_only=True)
                            cleared += self.quests.clear_world_objective_npc_ban()
                            self.brain.need_quest_unlock = True
                            self.brain.awaiting_quest_turnin = False
                            self.brain.push_farm(120.0)
                            logger.info(
                                "Выход закрыт военачальником — сюжетный диалог "
                                "(cleared=%d, need_quest_unlock=ON, Lv%d).",
                                cleared,
                                int(getattr(self._char, "level", 1) or 1),
                            )
                        return False
                    if "перегруз" in err.lower() or "рюкзак" in err.lower():
                        dropped = await self.combat.free_backpack(target_free=8)
                        if dropped:
                            return True
                    # Don't spam the same gated exit
                    self.brain.mark_cooldown(f"Переход: {name}", 300)
                    return False
                new_state = await self._client.get_state()
                if new_state.area_id and new_state.area_id != self._state.area_id:
                    logger.info("Перешёл в area %s (%s).", new_state.area_id, name)
                    self._area_moves_tried.clear()
                    self.brain.need_quest_unlock = False
                    self.brain.clear_hunt_gate()
                    self.quests.clear_hunt_gate()
                    if hasattr(self.brain, "village_exit_blocked_until"):
                        self.brain.village_exit_blocked_until = 0.0
                    return True
                return False

            if action == ActionType.BUFF:
                resp = await self._client.use_effect(show=True)
                await self._process_loot_response(resp, label="эффект")
                await _sleep(1.0, 2.0)
                return True

            if action == ActionType.IDLE:
                wo = self.quests.pending_world_objective or {}
                farm_open = self._wo_farm_open(wo)
                if wo.get("flash_only") and not farm_open:
                    logger.info(
                        "Idle 45–90с — мир-цель '%s' ждёт Flash; охота на след. тике.",
                        wo.get("kind"),
                    )
                    await _sleep(45.0, 90.0)
                    return True
                if farm_open or farm.max_farm:
                    # Flash side-quest soft: replan immediately for hunt/story
                    await _sleep(0.4, 1.2)
                    return True
                return False

        except TokenExpiredError:
            raise
        except Exception as exc:
            log_exception(logger, f"execute_focus({action})", exc)
            return False

        return False

    async def _combat_tick_filtered(self) -> BattleResult:
        """Combat tick that respects farm_fronts / farm_arena / farm_area toggles."""
        farm = self.settings.farm
        profile = self._profile
        hp_pct = profile.char.hp_percent

        if farm.auto_heal:
            if hp_pct < COMBAT.hp_retreat_threshold:
                healed = await self.combat.heal_if_needed(profile)
                if not healed:
                    logger.info("Resting to recover HP …")
                self.combat.session.consecutive_battles = 0
                return BattleResult.FLED
            await self.combat.heal_if_needed(profile)
            await self.combat.restore_mana_if_needed(profile)

        if await self.combat.is_in_battle():
            return await self.combat.finish_fight()

        if await self.combat.needs_rest():
            self.combat.session.consecutive_battles = 0
            await asyncio.sleep(random.uniform(20, 60))
            return BattleResult.NO_BATTLE

        if farm.farm_area:
            mob = self.brain.pending_hunt_mob or self.quests.pending_hunt_mob or ""
            result = await self.combat.try_hunt_attack(name_substr=mob)
            if result in (
                BattleResult.JOINED, BattleResult.WIN,
                BattleResult.ONGOING, BattleResult.LOSE,
            ):
                return result

        if farm.farm_fronts and not self.brain.need_quest_unlock:
            result = await self.combat.try_join_front()
            if result == BattleResult.JOINED:
                return result

        if farm.farm_arena:
            result = await self.combat.try_arena()
            if result == BattleResult.JOINED:
                return result

        if farm.farm_area:
            result = await self.combat.try_area_combat()
            if result == BattleResult.JOINED:
                return result

        return BattleResult.NO_BATTLE

    async def _try_area_progress(self) -> bool:
        """Try travel / hotspot actions that unlock after quest gates open."""
        try:
            area = await self._client.get_area_info()
            if area.title:
                self._area_title = area.title

            for item in area.items:
                key = f"{item.item_type}:{item.item_id}:{item.code}:{item.area_id}:{item.action_id}"
                if key in self._area_moves_tried:
                    continue

                # Travel
                if item.code == "COME_IN" and item.area_id:
                    low = (item.name or "").lower()
                    if self.brain.village_exit_blocked() and (
                        str(item.area_id) in {"192", "100"}
                        or any(kw in low for kw in ("сопк", "дымн"))
                    ):
                        continue
                    self._area_moves_tried.add(key)
                    logger.info("Пробую переход: %s → area %s", item.name, item.area_id)
                    resp = await self._client.go_area(item.area_id, code=item.code)
                    await self._process_loot_response(resp, label=item.name)
                    err = str(resp.redirect_error or resp.error or "")
                    if err and err.lower() not in ("false", "none", ""):
                        logger.info("Переход закрыт: %s", err[:160])
                        if "военачальник" in err.lower() or "покинуть селение" in err.lower():
                            self.brain.mark_village_exit_blocked(7200.0)
                        continue
                    new_state = await self._client.get_state()
                    if new_state.area_id and new_state.area_id != area.area_id:
                        logger.info(
                            "Перешёл в area %s (%s).",
                            new_state.area_id, item.name,
                        )
                        self._area_moves_tried.clear()
                        self.brain.village_exit_blocked_until = 0.0
                        return True

                # Hotspot actions (non-npc) — only count real loot / fight
                if item.action_id and item.item_type in ("action", "area", ""):
                    if item.on_cooldown:
                        continue
                    self._area_moves_tried.add(key)
                    logger.info("Пробую действие локации: %s", item.name)
                    resp = await self._client.run_area_action(
                        object_id=item.object_id or area.area_id,
                        action_id=item.action_id,
                        link_id=item.link_id,
                        object_class=item.object_class or "AREA",
                    )
                    loot_n = await self._process_loot_response(resp, label=item.name)
                    if await self.combat.is_in_battle() or loot_n > 0:
                        logger.info("Действие '%s' дало прогресс.", item.name)
                        await _sleep(2.0, 4.0)
                        return True
                    err = str(resp.redirect_error or resp.error or "")
                    if err and err.lower() not in ("false", "none", ""):
                        logger.debug("area action '%s': %s", item.name, err)
                    else:
                        logger.info("Действие '%s' пустое — не считаю прогрессом.", item.name)
                        self.brain.mark_cooldown(item.name or "точка", 120)

        except TokenExpiredError:
            raise
        except Exception as exc:
            logger.debug("_try_area_progress: %s", exc)
        return False

    async def _handle_token_expired(self, detail: str) -> None:
        msg = (
            "⚠️ DwarBot: OAuth токен истёк. Бот ждёт новые куки.\n\n"
            "1. Войди на https://w1.dwar.ru\n"
            "2. Cookie Editor → Export as JSON\n"
            "3. Пришли JSON сюда в этот чат\n\n"
            "Бот сначала проверит старые sess_sid — если живы, рестарт не нужен.\n"
            "Бот подхватит автоматически (без рестарта)."
        )
        logger.critical("TOKEN EXPIRED: %s", detail)
        await self._send_telegram(msg)

        # Immediate soft-keep attempt before waiting for paste
        try:
            if await self._cookie_recovery.try_keep_old_session("token_expired entry"):
                self._token_ok = True
                char = await self._client.get_char_stats()
                if char.nick:
                    await self._send_telegram(
                        f"✅ Старые куки ещё работают — {char.nick} Lv{char.level}."
                    )
                    return
        except Exception as exc:
            logger.debug("soft keep on token expire: %s", exc)

        last_mtime = 0.0
        try:
            if DEFAULT_COOKIE_FILE.exists():
                last_mtime = DEFAULT_COOKIE_FILE.stat().st_mtime
        except OSError:
            pass

        while not _shutdown_event.is_set():
            try:
                # Soft recheck periodically — sess may still be alive
                if await self._cookie_recovery.try_keep_old_session("token wait loop"):
                    self._token_ok = True
                    try:
                        char = await self._client.get_char_stats()
                        if char.nick:
                            await self._send_telegram(
                                f"✅ Сессия снова жива ({char.nick} Lv{char.level})."
                            )
                            return
                    except Exception:
                        self._token_ok = True
                        return

                reloaded = await self._client.maybe_reload_cookie_file()
                file_changed = (
                    DEFAULT_COOKIE_FILE.exists()
                    and DEFAULT_COOKIE_FILE.stat().st_mtime != last_mtime
                )
                if reloaded or file_changed:
                    last_mtime = DEFAULT_COOKIE_FILE.stat().st_mtime
                    ok, human = await self._cookie_recovery.recover_after_cookie_file_update()
                    if ok:
                        try:
                            char = await self._client.get_char_stats()
                            if char.nick:
                                self._token_ok = True
                                logger.info(
                                    "Fresh cookies OK — %s Lv%d (%s)",
                                    char.nick, char.level, human,
                                )
                                await self._send_telegram(
                                    f"✅ DwarBot: сессия восстановлена "
                                    f"({char.nick} Lv{char.level}).\n{human}"
                                )
                                return
                        except TokenExpiredError:
                            pass
                    else:
                        logger.warning("Cookie recover failed: %s", human)
                        await self._send_telegram(
                            f"⚠️ Куки обновились, но сессия не поднялась: {human}\n"
                            "Пришли свежий Export JSON (желательно с sess_sid)."
                        )
            except Exception as exc:
                logger.debug("token wait loop: %s", exc)
            await asyncio.sleep(15)

    async def _send_telegram(self, text: str) -> None:
        import httpx
        token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
        # Only the account owner — never all admins
        chat_id = self.owner_user_id or os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
        if not token or not chat_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                )
        except Exception as exc:
            logger.debug("Telegram send failed: %s", exc)


def _load_bootstrap() -> tuple[str, str, dict[str, str], Path]:
    """Return (access_token, mycom_value, cookie_dict, cookie_path)."""
    files = sorted(
        list(COOKIES_DIR.glob("*.json")) + list(COOKIES_DIR.glob("*.txt")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return "", "", {}, DEFAULT_COOKIE_FILE
    path = files[0]
    cookies = load_cookie_dict(path)
    mycom = cookies.get("mycom", "")
    token = extract_access_token(mycom) if mycom else ""
    return token or "", mycom, cookies, path


async def main() -> None:
    # Load .env (CURSOR_API_KEY etc.) before anything else
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        from dwar_bot.core.cursor_self_healer import _load_dotenv
        _load_dotenv()
        _load_dotenv(Path(__file__).resolve().parent / ".env")

    # Ensure agent CLI is on PATH for this process
    os.environ.update(_augment_path(os.environ.copy()))

    setup_logging(
        level=os.getenv("DWAR_LOG_LEVEL", "INFO"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        telegram_chat_id=",".join(
            resolve_telegram_notify_chats(
                chat_id=os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
                admin_ids=os.getenv("TELEGRAM_ADMIN_IDS", TELEGRAM_ADMIN_IDS),
                notify_ids=os.getenv("TELEGRAM_NOTIFY_CHAT_IDS", TELEGRAM_NOTIFY_CHAT_IDS),
            )
        ),
        telegram_min_level=os.getenv("TELEGRAM_MIN_LEVEL", "WARNING"),
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    _world = os.getenv("DWAR_WORLD", "w1")
    _world_url = os.getenv("DWAR_WORLD_URL", f"https://{_world}.dwar.ru")

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    tg_chatid = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    tg_admins = resolve_telegram_admins(
        chat_id=tg_chatid,
        admin_ids=os.getenv("TELEGRAM_ADMIN_IDS", TELEGRAM_ADMIN_IDS),
    )
    tg_allow_groups = os.getenv(
        "TELEGRAM_ALLOW_GROUPS",
        "1" if TELEGRAM_ALLOW_GROUPS else "0",
    ).lower() in ("1", "true", "yes", "on")

    if not tg_admins:
        logger.critical("TELEGRAM_ADMIN_IDS / TELEGRAM_CHAT_ID required for multi-account.")
        sys.exit(1)

    accounts = AccountManager(
        allowed_user_ids=tg_admins,
        default_world=_world,
        default_world_url=_world_url,
    )
    primary = tg_chatid or tg_admins[0]
    accounts.migrate_legacy(primary, DEFAULT_COOKIE_FILE, legacy_state=STATE_FILE)
    runtimes = accounts.bootstrap_all(DwarBot)
    set_bot_state(BotState.RUNNING)
    logger.info("Multi-account: %d user slot(s) under %s", len(runtimes), ACCOUNTS_DIR)

    primary_rt = accounts.get_runtime(primary) or (runtimes[0] if runtimes else None)
    if primary_rt is None:
        logger.critical("No account runtimes bootstrapped.")
        sys.exit(1)
    bot = primary_rt.bot

    async def _notify_plain(text: str) -> None:
        await bot.notify(text, "errors")

    healer = bind_auto_healer(
        notify_fn=_notify_plain,
        pause_fn=bot.pause_for_heal,
        resume_fn=bot.resume_after_heal,
        on_local_recover=bot._local_recover_stagnation,
    )

    # Shared MasterController for LevelingEngine + AutonomousLogWatcher
    master_controller = bind_master_controller(
        pause_fn=bot.pause_for_heal,
        resume_fn=bot.resume_after_heal,
        notify_fn=_notify_plain,
    )
    bot.controller = master_controller
    bot.leveling.controller = master_controller

    tg_task: asyncio.Task | None = None
    if tg_token:
        tg_handler = TelegramBotHandler(
            token=tg_token,
            owner_chat_id=primary,
            admin_ids=tg_admins,
            notify_chat_ids=tg_admins,
            allow_groups=tg_allow_groups,
            log_path=LOG_FILE,
            account_manager=accounts,
        )
        for rt in runtimes:
            rt.bot.bind_telegram(tg_handler)
        tg_task = asyncio.ensure_future(tg_handler.start())
        logger.info("Telegram multi-account panel (users=%s).", ",".join(tg_admins))

    async def _boot_heal_ready() -> None:
        try:
            path = await asyncio.to_thread(ensure_cursor_cli)
            logger.info("Cursor Agent CLI ready: %s", path)
        except Exception as exc:
            logger.error("Cursor CLI bootstrap failed (will retry on heal): %s", exc)
        await healer.ensure_ready()

    asyncio.create_task(_boot_heal_ready(), name="cursor_cli_boot")

    # Industrial 300s self-healing orchestrator (AST + Cursor CLI + Circuit Breaker)
    autonomous_watcher = AutonomousLogWatcher(
        log_path=LOG_FILE,
        interval_seconds=300,
        controller=master_controller,
        notify_fn=_notify_plain,
    )
    log_watcher_task = asyncio.create_task(
        autonomous_watcher.start_monitoring(interval_seconds=300),
        name="autonomous_log_watcher_300s",
    )

    # Gemini + AutoCoder OFF by default — they false-pause village farm.
    # Enable with ENABLE_SELF_HEAL=1 if you want Cursor auto-patches.
    enable_self_heal = os.getenv("ENABLE_SELF_HEAL", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    pure_farm_only = os.getenv("PURE_FARM_ONLY", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    healing_orchestrator = None
    healing_orch_task = None
    auto_coder = None
    auto_coder_task = None
    if enable_self_heal:
        async def _orch_pause() -> None:
            for rt in accounts.all_runtimes():
                try:
                    await rt.bot.pause(quiet=True)
                except Exception:
                    pass

        async def _orch_resume() -> None:
            for rt in accounts.all_runtimes():
                try:
                    await rt.bot.resume_game(quiet=True)
                except Exception:
                    pass

        healing_orchestrator = HealingOrchestrator(
            log_path=LOG_FILE,
            interval_seconds=120,
            notify_fn=_notify_plain,
            pause_fn=_orch_pause,
            resume_fn=_orch_resume,
        )
        healing_orch_task = asyncio.create_task(
            healing_orchestrator.run_forever(),
            name="gemini_healing_orchestrator_120s",
        )
        auto_coder = bind_auto_coder(
            log_path=LOG_FILE,
            notify_fn=_notify_plain,
            interval_min=120,
            interval_max=300,
        )
        auto_coder_task = asyncio.create_task(
            auto_coder.run_forever(),
            name="auto_coder_120_300s",
        )
        logger.info("Self-heal ON (Gemini+AutoCoder) ENABLE_SELF_HEAL=1")
    else:
        logger.info(
            "Gemini/AutoCoder OFF — story/quests first "
            "(PURE_FARM_ONLY=%s).",
            "1" if pure_farm_only else "0",
        )

    async def _heal_watchdog() -> None:
        while not _shutdown_event.is_set():
            await asyncio.sleep(900)
            if get_bot_state() == BotState.HEALING:
                continue
            try:
                await healer.ensure_ready()
            except Exception as exc:
                logger.debug("heal watchdog: %s", exc)

    asyncio.create_task(_heal_watchdog(), name="heal_watchdog")
    logger.info(
        "Story/quests mode · PureFarm filler=%s · LogWatcher(300s) · "
        "self_heal=%s · CURSOR=%s GEMINI=%s",
        "ON" if pure_farm_only else "OFF(auto_quests)",
        "ON" if enable_self_heal else "OFF",
        "set" if os.getenv("CURSOR_API_KEY") else "MISSING",
        "set" if os.getenv("GEMINI_API_KEY") else "MISSING",
    )

    await accounts.start_all()

    try:
        while not _shutdown_event.is_set():
            await asyncio.sleep(2)
            for rt in accounts.all_runtimes():
                if rt.task and rt.task.done() and not _shutdown_event.is_set():
                    exc = None
                    try:
                        exc = rt.task.exception()
                    except asyncio.CancelledError:
                        pass
                    if exc:
                        logger.error("[%s] loop died: %s — restarting in 15s", rt.spec.slot_id, exc)
                    await asyncio.sleep(15)
                    if not _shutdown_event.is_set():
                        await accounts.start_runtime(rt)
    finally:
        for rt in accounts.all_runtimes():
            if rt.task and not rt.task.done():
                rt.task.cancel()
            try:
                await rt.bot.timers.stop_background_tasks()
            except Exception:
                pass
            try:
                await rt.client.aclose()
            except Exception:
                pass
        if not log_watcher_task.done():
            log_watcher_task.cancel()
            try:
                await log_watcher_task
            except asyncio.CancelledError:
                pass
        if healing_orch_task is not None and not healing_orch_task.done():
            if healing_orchestrator is not None:
                healing_orchestrator.stop()
            healing_orch_task.cancel()
            try:
                await healing_orch_task
            except asyncio.CancelledError:
                pass
        if auto_coder_task is not None and not auto_coder_task.done():
            if auto_coder is not None:
                auto_coder.stop()
            auto_coder_task.cancel()
            try:
                await auto_coder_task
            except asyncio.CancelledError:
                pass
        if tg_task:
            tg_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
