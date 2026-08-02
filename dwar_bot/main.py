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
from dwar_bot.core.bot_state import BotState, get_bot_state, set_bot_state
from dwar_bot.core.log_watcher import start_log_monitoring
from dwar_bot.core.cursor_self_healer import ensure_cursor_cli, _augment_path
from dwar_bot.core.auto_healer import bind_auto_healer, get_auto_healer
from dwar_bot.core.error_recovery import get_recovery_stats
from dwar_bot.config import COMBAT

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
    def __init__(self, client: DwarGameClient, settings: Optional[BotSettings] = None) -> None:
        self._client = client
        self.settings = settings or BotSettings.load()
        self.stats = StatsParser(client)
        self.combat = CombatEngine(client, self.stats)
        self.quests = QuestTracker(client)
        self.timers = TimersManager(client)
        self.brain = ProgressionBrain(self.settings)

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

    def _apply_combat_thresholds(self) -> None:
        f = self.settings.farm
        COMBAT.hp_retreat_threshold = float(f.hp_retreat)
        COMBAT.hp_elixir_threshold = float(f.hp_heal)
        COMBAT.max_consecutive_battles = int(f.max_battles_row)

    async def notify(self, text: str, category: str = "") -> None:
        if self._tg:
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
            "nick":       self._char.nick,
            "level":      self._char.level,
            "hp":         self._char.hp,
            "hp_max":     self._char.hp_max,
            "mp":         self._char.mp,
            "mp_max":     self._char.mp_max,
            "money":      self._state.money,
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
            "loot_claimed": self._loot_claimed,
            "settings": self.settings.to_dict(),
            "bot_state": get_bot_state().name,
            "auth_blocked": self._client.auth_blocked,
            "fight_id": getattr(self._state, "fight_id", 0) or 0,
            "need_quest_unlock": self.brain.need_quest_unlock,
            "awaiting_quest_turnin": self.brain.awaiting_quest_turnin,
            "pending_hunt_mob": self.brain.pending_hunt_mob or self.quests.pending_hunt_mob,
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
        """Full progression-aware report for Telegram / heartbeat."""
        st = self.get_status()
        r = self.settings.report
        parts = [
            f"<b>📈 Отчёт DwarBot</b> · {time.strftime('%H:%M:%S')}",
            f"🧙 <b>{st.get('nick','?')}</b> Lv{st.get('level','?')} · "
            f"❤️ {st.get('hp','?')}/{st.get('hp_max','?')} · "
            f"💰 {st.get('money','?')}",
            f"📍 {st.get('area_title') or st.get('area_id','?')} · "
            f"⏱ {st.get('uptime','?')} · тик {st.get('iteration',0)}",
        ]
        if r.include_plan:
            parts.append("")
            parts.append(self.brain.last.report_html())
        if r.include_combat:
            parts.append(
                f"\n⚔️ Бои: {st.get('battles',0)} · "
                f"🏆{st.get('wins',0)} / 💀{st.get('losses',0)} · "
                f"WR {st.get('win_rate',0):.0f}%"
            )
        if r.include_quests:
            parts.append(
                f"📜 Квесты: ✅{st.get('quests_completed',0)} · "
                f"📝{st.get('quests_accepted',0)} · "
                f"💬{st.get('dialogues',0)}"
            )
        if r.include_inventory:
            parts.append(
                f"🎒 Предметов: {len(st.get('inventory') or [])} · "
                f"🧪 {st.get('potions_count',0)} · "
                f"🎁 лут-тиков: {self._loot_claimed}"
            )
        if r.include_timers:
            timers = st.get("timers") or []
            if timers:
                tlines = ", ".join(
                    f"{t.get('description','?')}: {t.get('remaining','?')}"
                    for t in timers[:4]
                )
                parts.append(f"⏱ {tlines}")
        f = self.settings.farm
        parts.append(
            f"🤖 Макс-фарм {self.settings.on_off(f.max_farm)} · "
            f"Квесты {self.settings.on_off(f.auto_quests)} · "
            f"Бои {self.settings.on_off(f.auto_combat)} · "
            f"Лут {self.settings.on_off(f.auto_loot)}"
        )
        return "\n".join(parts)

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
        Accept Cookie Editor JSON pasted via Telegram.
        Returns a short human-readable status string.
        """
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            return f"❌ JSON не разобран: {exc}"

        if not isinstance(data, list):
            return "❌ Ожидался JSON-массив Cookie Editor."

        path = DEFAULT_COOKIE_FILE
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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
            return (
                f"✅ Куки приняты. {char.nick or '?'} Lv{char.level} "
                f"HP {char.hp}/{char.hp_max} area={state.area_id}"
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
                # No long idle while pushing farm / leaving the village
                if (
                    self.settings.farm.idle_pauses
                    and not self.brain.farm_push_active()
                    and random.random() < IDLE_PAUSE_PROBABILITY
                ):
                    s = random.uniform(DELAY_IDLE.min, DELAY_IDLE.max)
                    logger.info("Idle pause: %.0fs.", s)
                    await asyncio.sleep(s)

    async def _maybe_send_report(self) -> None:
        r = self.settings.report
        if not r.enabled:
            return
        interval = max(5, int(r.interval_min)) * 60
        if time.time() - self.settings.last_report_at < interval:
            return
        try:
            text = await self.build_report()
            await self.notify(text, "heartbeat")
            self.settings.last_report_at = time.time()
            self.settings.save()
        except Exception as exc:
            logger.debug("auto report failed: %s", exc)

    async def _emit_state_notifications(self) -> None:
        """Compare with previous snapshot and push Telegram alerts."""
        # Level up
        if self._prev_level and self._char.level > self._prev_level:
            await self.notify(
                f"⬆️ Уровень! <b>{self._char.nick}</b> теперь Lv{self._char.level}",
                "level_up",
            )
        self._prev_level = self._char.level or self._prev_level

        # Money change (significant)
        if self._prev_money >= 0 and abs(self._state.money - self._prev_money) >= 1.0:
            delta = self._state.money - self._prev_money
            sign = "+" if delta > 0 else ""
            await self.notify(
                f"💰 Деньги: {sign}{delta:.2f} → <b>{self._state.money:.2f}</b> зол.",
                "money",
            )
        self._prev_money = self._state.money

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
                    self.brain.push_farm(300.0)
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
                            self.brain.push_farm(300.0)
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
            "[%d] %s Lv%d | HP %d/%d (%.0f%%) | MP %d/%d | area=%s | %.2f зол | предметов=%d | sid=%s…",
            self._iteration,
            self._char.nick, self._char.level,
            self._char.hp, self._char.hp_max, self._char.hp_percent,
            self._char.mp, self._char.mp_max,
            self._state.area_id, self._state.money,
            len(self._profile.inventory),
            (self._client._session.get("sess_sid") or "")[:8],
        )

        await self.timers.update_regen(self._char.hp, self._char.mp)
        await self._emit_state_notifications()

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

        # Quest type=2 kill gate → prefer hunt_farm mob (one kill, then turn-in)
        if self.quests.pending_hunt_mob:
            self.brain.pending_hunt_mob = self.quests.pending_hunt_mob
            if not self.brain.awaiting_quest_turnin:
                self.brain.need_quest_unlock = True
        elif not self.brain.awaiting_quest_turnin:
            # No live quest kill request — don't keep farming last mob forever
            if self.brain.pending_hunt_mob and self.brain._hunt_streak >= 1:
                self.brain.pending_hunt_mob = ""
                self.brain.need_quest_unlock = False

        event_timers = await self.timers.scrape_event_timers(hunt=hunt)
        # Empty bag: light farm nudge, not a 10‑minute hunt lock
        if not self._profile.inventory and not self.brain.farm_push_active():
            if self._iteration <= 2 and self.brain.empty_streak("Расселина") >= 2:
                self.brain.push_farm(180.0)
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

        focus = snap.focus
        focus_key = (
            f"{focus.action.value}:{focus.title}" if focus else "none"
        )
        logger.info(
            "🧠 Сейчас: %s | сила=%.0f | вариантов=%d",
            snap.now, snap.power_score, len(snap.options),
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

        # Notify on plan change (not every tick)
        if focus_key != self._last_focus_key and focus and focus.action != ActionType.IDLE:
            self._last_focus_key = focus_key
            if self.settings.notify.plan:
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
        )
        self.brain.note_result(focus, progressed=progressed)

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
        mob = mob or self.brain.pending_hunt_mob or self.quests.pending_hunt_mob or "Крэтс"
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
            mob = (
                self.brain.pending_hunt_mob
                or self.quests.pending_hunt_mob
                or ("Крэтс" if self.brain.need_quest_unlock else "")
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
                if payload.get("finish_only") or await self.combat.is_in_battle():
                    result = await self.combat.finish_fight()
                else:
                    result = await self.combat.try_hunt_attack(
                        name_substr=str(payload.get("name") or ""),
                        area_id=str(payload.get("area_id") or ""),
                    )
                if result == BattleResult.WIN:
                    await self.notify("⚔️ Охота: победа!", "battles")
                    self.brain.mark_hunt_kill_done()
                    self.quests.clear_exhausted(local_only=True)
                    # Immediately try type=2 turn-in — do not farm more Крэтс
                    turned = 0
                    try:
                        turned = await self.quests.retry_pending_type2()
                    except Exception as exc:
                        logger.debug("retry_pending_type2: %s", exc)
                    if turned:
                        self.brain.clear_hunt_gate()
                        self.quests.clear_hunt_gate()
                        await self.notify("📜 Квест: убийство сдано Вождю", "quests")
                        return True
                    if self.quests.pending_hunt_mob:
                        self.brain.pending_hunt_mob = self.quests.pending_hunt_mob
                    return True
                if result in (BattleResult.JOINED, BattleResult.ONGOING, BattleResult.LOSE):
                    return True
                self.brain.mark_cooldown(focus.title, 60)
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
                logger.info("🩹 Реген HP до безопасного порога…")
                if farm.auto_heal:
                    await self.notify("🩹 Восстановление HP…", "hp_low")
                    await self.timers.wait_for_hp(target_percent=70.0, max_wait=600)
                else:
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
                # Prefer finishing pending type=2 turn-in first
                if self.quests.has_pending_type2() and self.brain.awaiting_quest_turnin:
                    turned = await self.quests.retry_pending_type2()
                    if turned:
                        self.brain.clear_hunt_gate()
                        await self.notify("📜 Квест: убийство сдано", "quests")
                        return True
                steps = await self.quests.walk_npc_api(
                    npc_id,
                    global_npc=int(payload.get("global_npc", 0) or 0),
                    link_id=str(payload.get("link_id") or "0"),
                    f_id=str(payload.get("f_id") or "0"),
                    area_id=str(payload.get("area_id") or "0"),
                )
                if steps:
                    logger.info("📜 Квестовых шагов: %d (NPC %s)", steps, npc_id)
                    if not self.quests.pending_hunt_mob:
                        self.brain.clear_hunt_gate()
                    elif self.quests.pending_hunt_mob:
                        self.brain.mark_hunt_for_quest(self.quests.pending_hunt_mob)
                    await self.notify(
                        f"📜 Диалог с NPC {npc_id}: <b>{steps}</b> шаг(ов)",
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

            if action in (ActionType.COMBAT_AREA, ActionType.AREA_ACTION):
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
                    await self.notify(f"⚔️ Бой через <b>{name}</b>!", "battles")
                    await _sleep(3.0, 6.0)
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
                    # Don't spam the same gated exit
                    self.brain.mark_cooldown(f"Переход: {name}", 300)
                    if "военачальник" in err.lower() or "покинуть селение" in err.lower():
                        self.brain.need_quest_unlock = True
                        self.brain.awaiting_quest_turnin = False
                        # Soft nudge — story NPC first, hunt only if type=2 asks
                        self.brain.push_farm(120.0)
                        cleared = self.quests.clear_exhausted(local_only=True)
                        logger.info(
                            "Нужен приказ военачальника — возвращаюсь к сюжету "
                            "(снято exhausted: %d).",
                            cleared,
                        )
                    return False
                new_state = await self._client.get_state()
                if new_state.area_id and new_state.area_id != self._state.area_id:
                    logger.info("Перешёл в area %s (%s).", new_state.area_id, name)
                    self._area_moves_tried.clear()
                    self.brain.need_quest_unlock = False
                    self.brain.clear_hunt_gate()
                    self.quests.clear_hunt_gate()
                    return True
                return False

            if action == ActionType.BUFF:
                resp = await self._client.use_effect(show=True)
                await self._process_loot_response(resp, label="эффект")
                await _sleep(1.0, 2.0)
                return True

            if action == ActionType.IDLE:
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
                    self._area_moves_tried.add(key)
                    logger.info("Пробую переход: %s → area %s", item.name, item.area_id)
                    resp = await self._client.go_area(item.area_id, code=item.code)
                    await self._process_loot_response(resp, label=item.name)
                    err = str(resp.redirect_error or resp.error or "")
                    if err and err.lower() not in ("false", "none", ""):
                        logger.info("Переход закрыт: %s", err[:160])
                        continue
                    new_state = await self._client.get_state()
                    if new_state.area_id and new_state.area_id != area.area_id:
                        logger.info(
                            "Перешёл в area %s (%s).",
                            new_state.area_id, item.name,
                        )
                        self._area_moves_tried.clear()
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
            "Бот подхватит автоматически (без рестарта)."
        )
        logger.critical("TOKEN EXPIRED: %s", detail)
        await self._send_telegram(msg)

        last_mtime = 0.0
        try:
            if DEFAULT_COOKIE_FILE.exists():
                last_mtime = DEFAULT_COOKIE_FILE.stat().st_mtime
        except OSError:
            pass

        while not _shutdown_event.is_set():
            try:
                # Pause background spam while blocked
                reloaded = await self._client.maybe_reload_cookie_file()
                if reloaded or (
                    DEFAULT_COOKIE_FILE.exists()
                    and DEFAULT_COOKIE_FILE.stat().st_mtime != last_mtime
                ):
                    last_mtime = DEFAULT_COOKIE_FILE.stat().st_mtime
                    self._client.unblock_auth()
                    # Drop dead sess, keep new mycom
                    await self._client.invalidate_session("cookie file updated")
                    try:
                        await self._client.ensure_session()
                        char = await self._client.get_char_stats()
                        if char.nick:
                            logger.info("Fresh cookies OK — %s Lv%d", char.nick, char.level)
                            await self._send_telegram(
                                f"✅ DwarBot: сессия восстановлена ({char.nick} Lv{char.level})."
                            )
                            return
                    except TokenExpiredError:
                        logger.warning("Cookie file updated but token still invalid.")
                        await self._send_telegram(
                            "⚠️ Файл куков обновился, но токен всё ещё невалиден. "
                            "Пришли свежий Export JSON."
                        )
            except Exception as exc:
                logger.debug("token wait loop: %s", exc)
            await asyncio.sleep(15)

    async def _send_telegram(self, text: str) -> None:
        import httpx
        token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
        chats = resolve_telegram_notify_chats(
            chat_id=os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
            admin_ids=os.getenv("TELEGRAM_ADMIN_IDS", TELEGRAM_ADMIN_IDS),
            notify_ids=os.getenv("TELEGRAM_NOTIFY_CHAT_IDS", TELEGRAM_NOTIFY_CHAT_IDS),
        )
        if not token or not chats:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                for chat_id in chats:
                    try:
                        await c.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={"chat_id": chat_id, "text": text},
                        )
                    except Exception as exc:
                        logger.debug("Telegram send to %s failed: %s", chat_id, exc)
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

    while True:
        files = list(COOKIES_DIR.glob("*.json")) + list(COOKIES_DIR.glob("*.txt"))
        if files:
            logger.info("Cookie file found: %s", files[0].name)
            break
        logger.warning("Waiting for cookie file in '%s' …", COOKIES_DIR)
        await asyncio.sleep(30)

    access_token, mycom_value, cookie_dict, cookie_path = _load_bootstrap()
    if not access_token and not cookie_dict.get("sess_sid"):
        logger.critical("No access_token/sess_sid in cookie file — cannot start.")
        sys.exit(1)

    client = DwarGameClient(
        world_url=_world_url,
        access_token=access_token,
        mycom_cookie_value=mycom_value,
        cookie_file=cookie_path,
        initial_cookies=cookie_dict,
    )
    logger.info(
        "Bootstrap cookies: sess_sid=%s mycom=%s token=%s",
        "yes" if cookie_dict.get("sess_sid") else "no",
        "yes" if mycom_value else "no",
        "yes" if access_token else "no",
    )

    bot = DwarBot(client, settings=BotSettings.load())
    set_bot_state(BotState.RUNNING)

    async def _notify_plain(text: str) -> None:
        await bot.notify(text, "errors")

    # Bind global auto-healer (exceptions + stagnation + log watcher)
    healer = bind_auto_healer(
        notify_fn=_notify_plain,
        pause_fn=bot.pause_for_heal,
        resume_fn=bot.resume_after_heal,
        on_local_recover=bot._local_recover_stagnation,
    )

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    tg_chatid = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    tg_admins = resolve_telegram_admins(
        chat_id=tg_chatid,
        admin_ids=os.getenv("TELEGRAM_ADMIN_IDS", TELEGRAM_ADMIN_IDS),
    )
    tg_notify = resolve_telegram_notify_chats(
        chat_id=tg_chatid,
        admin_ids=os.getenv("TELEGRAM_ADMIN_IDS", TELEGRAM_ADMIN_IDS),
        notify_ids=os.getenv("TELEGRAM_NOTIFY_CHAT_IDS", TELEGRAM_NOTIFY_CHAT_IDS),
    )
    tg_allow_groups = os.getenv(
        "TELEGRAM_ALLOW_GROUPS",
        "1" if TELEGRAM_ALLOW_GROUPS else "0",
    ).lower() in ("1", "true", "yes", "on")
    tg_task: asyncio.Task | None = None
    if tg_token and tg_admins:
        async def _get_status() -> dict:
            return bot.get_status()

        tg_handler = TelegramBotHandler(
            token=tg_token,
            owner_chat_id=tg_chatid or tg_admins[0],
            admin_ids=tg_admins,
            notify_chat_ids=tg_notify,
            allow_groups=tg_allow_groups,
            get_status_fn=_get_status,
            stop_fn=bot.pause,
            resume_fn=bot.resume_game,
            log_path=LOG_FILE,
            settings=bot.settings,
            on_cookies_json=bot.apply_cookie_json,
            on_report_fn=bot.build_report,
            admin_fns={
                "diagnose": bot.admin_diagnose,
                "recover": bot.admin_recover,
                "hunt": bot.admin_force_hunt,
                "heal": bot.admin_trigger_heal,
                "restart": bot.admin_restart_service,
            },
        )
        bot.bind_telegram(tg_handler)
        tg_task = asyncio.ensure_future(tg_handler.start())
        logger.info(
            "Telegram control panel started (admins=%s notify=%s).",
            ",".join(tg_admins),
            ",".join(tg_notify),
        )

    # Pre-install / verify CLI + API key (full-auto readiness)
    async def _boot_heal_ready() -> None:
        try:
            path = await asyncio.to_thread(ensure_cursor_cli)
            logger.info("Cursor Agent CLI ready: %s", path)
        except Exception as exc:
            logger.error("Cursor CLI bootstrap failed (will retry on heal): %s", exc)
        await healer.ensure_ready()

    asyncio.create_task(_boot_heal_ready(), name="cursor_cli_boot")

    # Log watcher every 45s (boot-scan + ANSI-aware + AutoHealer)
    log_watcher_task = asyncio.create_task(
        start_log_monitoring(
            45,
            log_path=LOG_FILE,
            notify_fn=_notify_plain,
            pause_fn=bot.pause_for_heal,
            resume_fn=bot.resume_after_heal,
        ),
        name="log_watcher",
    )
    # Periodic readiness re-check (reinstall CLI / reload .env if needed)
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
        "AutoHealer + LogWatcher FULL-AUTO (45s) · CURSOR_API_KEY=%s",
        "set" if os.getenv("CURSOR_API_KEY") else "MISSING",
    )

    while not _shutdown_event.is_set():
        try:
            await client.ensure_session()
            state = await client.get_state()
            char = await client.get_char_stats()
            if not char.nick:
                # sess_* from file may be stale — renew once
                await client.invalidate_session("empty nick at startup")
                await client.ensure_session()
                state = await client.get_state()
                char = await client.get_char_stats()
            logger.info(
                "Connected! nick=%s level=%d hp=%d/%d area=%s money=%.2f sid=%s…",
                char.nick, char.level, char.hp, char.hp_max,
                state.area_id, state.money,
                (client._session.get("sess_sid") or "")[:8],
            )
            persist_session_cookies(client._session, cookie_path)
            try:
                client._cookie_mtime = cookie_path.stat().st_mtime
            except OSError:
                pass
            break
        except TokenExpiredError as exc:
            await bot._handle_token_expired(str(exc))
        except Exception as exc:
            log_exception(logger, "Fatal startup error", exc)
            try:
                await healer.handle_exception(exc, where="startup")
            except Exception as heal_exc:
                logger.error("Startup auto-heal failed: %s", heal_exc)
            # systemd Restart=always will bring us back with patched code
            sys.exit(2)

    bot.timers.start_background_tasks()
    await bot.timers.sync_server_time()

    try:
        await bot.run()
    finally:
        await bot.timers.stop_background_tasks()
        if not log_watcher_task.done():
            log_watcher_task.cancel()
            try:
                await log_watcher_task
            except asyncio.CancelledError:
                pass
        await client.aclose()
        if tg_task:
            tg_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
