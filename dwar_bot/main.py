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

sys.path.insert(0, str(Path(__file__).parent.parent))

from dwar_bot.config import (
    DELAY_IDLE,
    DELAY_MAIN_LOOP,
    DELAY_RETRY,
    IDLE_PAUSE_PROBABILITY,
    LOG_FILE,
    MAX_RETRIES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    DEFAULT_COOKIE_FILE,
    COOKIES_DIR,
)
from dwar_bot.logger import setup_logging, log_exception
from dwar_bot.auth.oauth_login import extract_access_token
from dwar_bot.core.game_client import (
    DwarGameClient,
    GameState,
    CharStats,
    TokenExpiredError,
    load_cookie_dict,
    persist_session_cookies,
)
from dwar_bot.telegram_bot import TelegramBotHandler
from dwar_bot.modules.stats_parser import StatsParser, FullProfile
from dwar_bot.modules.combat_engine import CombatEngine, BattleResult
from dwar_bot.modules.quest_tracker import QuestTracker
from dwar_bot.modules.timers_manager import TimersManager

logger = logging.getLogger("dwar_bot.main")

_shutdown_event = asyncio.Event()


def _handle_signal(signum, frame) -> None:
    logger.warning("Signal %d received — graceful shutdown …", signum)
    _shutdown_event.set()


async def _sleep(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _maybe_idle() -> None:
    if random.random() < IDLE_PAUSE_PROBABILITY:
        s = random.uniform(DELAY_IDLE.min, DELAY_IDLE.max)
        logger.info("Idle pause: %.0fs.", s)
        await asyncio.sleep(s)


class DwarBot:
    def __init__(self, client: DwarGameClient) -> None:
        self._client = client
        self.stats = StatsParser(client)
        self.combat = CombatEngine(client, self.stats)
        self.quests = QuestTracker(client)
        self.timers = TimersManager(client)

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

    def get_status(self) -> dict:
        elapsed = int(time.time() - self._started_at)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        cs = self.combat.session
        qs = self.quests.session
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
        }

    async def pause(self) -> None:
        self._paused = True
        logger.info("Game loop paused via Telegram.")

    async def resume_game(self) -> None:
        self._paused = False
        logger.info("Game loop resumed via Telegram.")

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
            try:
                # Hot-reload cookies dropped on disk / via Telegram
                await self._client.maybe_reload_cookie_file()
                await self._tick()
                self._errors_in_row = 0
            except asyncio.CancelledError:
                break
            except TokenExpiredError as exc:
                self._token_ok = False
                await self._handle_token_expired(str(exc))
                self._token_ok = True
            except Exception as exc:
                self._errors_in_row += 1
                log_exception(logger, f"Error in tick #{self._iteration}", exc)
                if self._errors_in_row >= MAX_RETRIES:
                    logger.critical("%d consecutive errors — pausing 5min.", MAX_RETRIES)
                    await asyncio.sleep(300)
                    self._errors_in_row = 0
                    await self._client.invalidate_session("too many errors")
                else:
                    await _sleep(DELAY_RETRY.min, DELAY_RETRY.max)

            if not _shutdown_event.is_set():
                await _sleep(DELAY_MAIN_LOOP.min, DELAY_MAIN_LOOP.max)
                await _maybe_idle()

    async def _tick(self) -> None:
        self._profile = await self.stats.read_full_profile()
        self._char = self._profile.char
        self._state = self._profile.state

        if not self._char.nick:
            logger.warning("No character data — session may be stale, renewing once.")
            await self._client.invalidate_session("empty character")
            await self._client.ensure_session()
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

        for note in self._profile.notifications[:3]:
            logger.info("📢 %s", note.text[:150])

        if self._profile.effects:
            logger.info(
                "Эффекты: %s",
                ", ".join(e.title for e in self._profile.effects[:4]),
            )

        if self._profile.broken_items:
            repaired = await self.combat.repair_broken_gear(self._profile)
            if repaired:
                logger.info("Отремонтировано предметов: %d", repaired)

        equipped = await self.combat.auto_equip(self._profile)
        if equipped:
            logger.info("Надето предметов: %d", equipped)

        # Quests first for newbies — story unlocks combat/travel
        quest_actions = await self.quests.quest_tick()
        if quest_actions:
            logger.info("📜 Квестовых действий: %d", quest_actions)
            await _sleep(2.0, 5.0)
            return

        result = await self.combat.combat_tick(self._profile)
        if result == BattleResult.JOINED:
            logger.info("⚔️ Вступил в бой!")
            await _sleep(3.0, 6.0)
            return
        if result == BattleResult.ONGOING:
            logger.info("⚔️ Бой продолжается …")
            return
        if result == BattleResult.FLED:
            logger.info("🩹 Восстанавливаю здоровье …")
            await self.timers.wait_for_hp(target_percent=70.0, max_wait=600)
            return

        # Area navigation / exploration
        moved = await self._try_area_progress()
        if moved:
            return

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
                }
                for i in area.items
            ]

        event_timers = await self.timers.scrape_event_timers()
        if event_timers:
            self._npcs = [
                {"title": t, "time_left": s, "npc_id": ""}
                for t, s in event_timers.items()
            ]

        active_cd = self.timers.active_cooldowns()
        if active_cd:
            logger.info(
                "⏱ Таймеры: %s",
                ", ".join(
                    f"{c.description or c.name} {c.format_remaining()}"
                    for c in active_cd[:3]
                ),
            )

        logger.info("💤 Нет доступных действий — жду.")
        await _sleep(8.0, 18.0)

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

                # Hotspot actions (non-npc)
                if item.action_id and item.item_type in ("action", "area", ""):
                    self._area_moves_tried.add(key)
                    logger.info("Пробую действие локации: %s", item.name)
                    resp = await self._client.run_area_action(
                        object_id=item.object_id or area.area_id,
                        action_id=item.action_id,
                        link_id=item.link_id,
                        object_class=item.object_class or "AREA",
                    )
                    err = str(resp.redirect_error or resp.error or "")
                    if err and err.lower() not in ("false", "none", ""):
                        logger.debug("area action '%s': %s", item.name, err)
                    else:
                        logger.info("Действие '%s' выполнено.", item.name)
                        await _sleep(2.0, 4.0)
                        return True

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
        chat_id = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
        if not token or not chat_id:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
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
    setup_logging(
        level=os.getenv("DWAR_LOG_LEVEL", "INFO"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
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

    bot = DwarBot(client)

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    tg_chatid = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    tg_task: asyncio.Task | None = None
    if tg_token and tg_chatid:
        async def _get_status() -> dict:
            return bot.get_status()

        tg_handler = TelegramBotHandler(
            token=tg_token,
            owner_chat_id=tg_chatid,
            get_status_fn=_get_status,
            stop_fn=bot.pause,
            resume_fn=bot.resume_game,
            log_path=LOG_FILE,
            on_cookies_json=bot.apply_cookie_json,
        )
        tg_task = asyncio.ensure_future(tg_handler.start())
        logger.info("Telegram bot started (chat_id=%s).", tg_chatid)

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
            break
        except TokenExpiredError as exc:
            await bot._handle_token_expired(str(exc))
        except Exception as exc:
            log_exception(logger, "Fatal startup error", exc)
            sys.exit(2)

    bot.timers.start_background_tasks()
    await bot.timers.sync_server_time()

    try:
        await bot.run()
    finally:
        await bot.timers.stop_background_tasks()
        await client.aclose()
        if tg_task:
            tg_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
