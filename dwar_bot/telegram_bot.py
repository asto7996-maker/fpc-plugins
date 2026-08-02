"""
Telegram control panel for DwarBot.

Features
--------
* Persistent ReplyKeyboard menu (кнопки внизу чата)
* Nested inline keyboards for autopilot / notify / reports
* Dozens of slash-commands registered in BotFather menu
* Cookie JSON paste, live toggles, reports, logs
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import httpx

from dwar_bot.modules.bot_settings import BotSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level Telegram API
# ---------------------------------------------------------------------------

class TelegramAPI:
    def __init__(self, token: str, owner_chat_id: str) -> None:
        self._token = token
        self._owner = str(owner_chat_id)
        self._base = f"https://api.telegram.org/bot{token}"
        self._offset: int = 0

    async def _post(self, method: str, payload: dict) -> Optional[Any]:
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.post(f"{self._base}/{method}", json=payload)
                data = r.json()
                if not data.get("ok"):
                    logger.debug("Telegram %s error: %s", method, data.get("description"))
                    return None
                return data.get("result")
        except Exception as exc:
            logger.debug("Telegram API error (%s): %s", method, exc)
            return None

    async def send(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[dict] = None,
        parse_mode: str = "HTML",
    ) -> None:
        if len(text) > 4000:
            text = text[:3900] + "\n…"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._post("sendMessage", payload)

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> None:
        if len(text) > 4000:
            text = text[:3900] + "\n…"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._post("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str = "", show_alert: bool = False) -> None:
        await self._post("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text[:180],
            "show_alert": show_alert,
        })

    async def get_updates(self) -> list[dict]:
        result = await self._post("getUpdates", {
            "offset": self._offset,
            "timeout": 25,
            "allowed_updates": ["message", "callback_query"],
        })
        if not result:
            return []
        updates = result if isinstance(result, list) else []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def set_commands(self, commands: list[dict]) -> None:
        await self._post("setMyCommands", {"commands": commands})

    def is_owner(self, chat_id: Any) -> bool:
        return str(chat_id) == self._owner


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

# Reply keyboard labels → command routing
_REPLY_MAP = {
    "🏠 Меню": "menu",
    "📊 Статус": "status",
    "🧙 Персонаж": "stats",
    "🤖 Автопилот": "autopilot",
    "⚔️ Бои": "combat",
    "📜 Квесты": "quests",
    "🗺 Локация": "area",
    "🔔 Уведомления": "notify",
    "📈 Отчёты": "reports",
    "⚙️ Настройки": "settings",
    "📋 Лог": "log",
    "🍪 Куки": "cookies",
    "⏸ Пауза": "stop",
    "▶️ Старт": "resume",
}


def _reply_keyboard() -> dict:
    """Persistent bottom menu — always visible in the chat."""
    rows = [
        ["🏠 Меню", "📊 Статус", "🧙 Персонаж"],
        ["🤖 Автопилот", "⚔️ Бои", "📜 Квесты"],
        ["🗺 Локация", "🔔 Уведомления", "📈 Отчёты"],
        ["⚙️ Настройки", "📋 Лог", "🍪 Куки"],
        ["⏸ Пауза", "▶️ Старт"],
    ]
    return {
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Команда или JSON куков…",
    }


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _inline(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def _toggle_label(title: str, on: bool) -> str:
    return f"{'🟢' if on else '🔴'} {title}"


def _menu_inline() -> dict:
    return _inline([
        [_btn("📊 Статус", "status"), _btn("🧙 Персонаж", "stats")],
        [_btn("🤖 Автопилот", "autopilot"), _btn("⚙️ Настройки", "settings")],
        [_btn("⚔️ Бои", "combat"), _btn("📜 Квесты", "quests")],
        [_btn("🗺 Локация", "area"), _btn("🎒 Рюкзак", "inventory")],
        [_btn("⏱ Таймеры", "timers"), _btn("✨ Эффекты", "effects")],
        [_btn("🔔 Уведомления", "notify"), _btn("📈 Отчёты", "reports")],
        [_btn("📋 Лог", "log"), _btn("🍪 Куки", "cookies")],
        [_btn("🛡 Сессия", "session"), _btn("ℹ️ Помощь", "help")],
        [_btn("⏸ Пауза", "stop"), _btn("▶️ Старт", "resume")],
    ])


def _autopilot_inline(s: BotSettings) -> dict:
    f = s.farm
    return _inline([
        [_btn(_toggle_label("Квесты", f.auto_quests), "tg:farm:auto_quests")],
        [_btn(_toggle_label("Бои авто", f.auto_combat), "tg:farm:auto_combat")],
        [_btn(_toggle_label("Фронты", f.farm_fronts), "tg:farm:farm_fronts"),
         _btn(_toggle_label("Арена", f.farm_arena), "tg:farm:farm_arena")],
        [_btn(_toggle_label("Точки локации", f.farm_area), "tg:farm:farm_area")],
        [_btn(_toggle_label("Переходы", f.auto_travel), "tg:farm:auto_travel")],
        [_btn(_toggle_label("Ремонт", f.auto_repair), "tg:farm:auto_repair"),
         _btn(_toggle_label("Экипировка", f.auto_equip), "tg:farm:auto_equip")],
        [_btn(_toggle_label("Лечение", f.auto_heal), "tg:farm:auto_heal"),
         _btn(_toggle_label("Idle-паузы", f.idle_pauses), "tg:farm:idle_pauses")],
        [_btn(_toggle_label("Агрессивный режим", f.aggressive), "tg:farm:aggressive")],
        [_btn("✅ Всё ВКЛ", "farm_all_on"), _btn("⛔ Всё ВЫКЛ", "farm_all_off")],
        [_btn("🏠 Меню", "menu")],
    ])


def _notify_inline(s: BotSettings) -> dict:
    n = s.notify
    return _inline([
        [_btn(_toggle_label("Бои", n.battles), "tg:notify:battles"),
         _btn(_toggle_label("Квесты", n.quests), "tg:notify:quests")],
        [_btn(_toggle_label("HP низко", n.hp_low), "tg:notify:hp_low"),
         _btn(_toggle_label("Токен", n.token), "tg:notify:token")],
        [_btn(_toggle_label("Уровень", n.level_up), "tg:notify:level_up"),
         _btn(_toggle_label("Деньги", n.money), "tg:notify:money")],
        [_btn(_toggle_label("Ошибки", n.errors), "tg:notify:errors"),
         _btn(_toggle_label("Эффекты", n.effects), "tg:notify:effects")],
        [_btn(_toggle_label("Локация", n.area), "tg:notify:area"),
         _btn(_toggle_label("Снаряжение", n.gear), "tg:notify:gear")],
        [_btn(_toggle_label("Heartbeat", n.heartbeat), "tg:notify:heartbeat")],
        [_btn("✅ Все ВКЛ", "notify_all_on"), _btn("⛔ Все ВЫКЛ", "notify_all_off")],
        [_btn("🏠 Меню", "menu")],
    ])


def _reports_inline(s: BotSettings) -> dict:
    r = s.report
    return _inline([
        [_btn(_toggle_label("Авто-отчёты", r.enabled), "tg:report:enabled")],
        [_btn("⏱ 15 мин", "report_int:15"), _btn("⏱ 30 мин", "report_int:30"),
         _btn("⏱ 60 мин", "report_int:60")],
        [_btn(_toggle_label("Бои в отчёте", r.include_combat), "tg:report:include_combat")],
        [_btn(_toggle_label("Квесты в отчёте", r.include_quests), "tg:report:include_quests")],
        [_btn(_toggle_label("Инвентарь", r.include_inventory), "tg:report:include_inventory")],
        [_btn(_toggle_label("Таймеры", r.include_timers), "tg:report:include_timers")],
        [_btn("📄 Отчёт сейчас", "report_now")],
        [_btn("🏠 Меню", "menu")],
    ])


def _settings_inline(s: BotSettings) -> dict:
    f = s.farm
    return _inline([
        [_btn("🤖 Автопилот", "autopilot"), _btn("🔔 Уведомления", "notify")],
        [_btn("📈 Отчёты", "reports")],
        [_btn(f"❤️ Retreat {f.hp_retreat:.0f}%", "noop"),
         _btn("−5%", "hp_retreat:-5"), _btn("+5%", "hp_retreat:+5")],
        [_btn(f"🧪 Heal {f.hp_heal:.0f}%", "noop"),
         _btn("−5%", "hp_heal:-5"), _btn("+5%", "hp_heal:+5")],
        [_btn(f"⚔️ Макс. боёв подряд: {f.max_battles_row}", "noop")],
        [_btn("−5", "max_battles:-5"), _btn("+5", "max_battles:+5")],
        [_btn("🏠 Меню", "menu")],
    ])


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _progress_bar(pct: int, length: int = 10) -> str:
    pct = max(0, min(100, int(pct)))
    filled = int(pct / 100 * length)
    return f"[{'█' * filled}{'░' * (length - filled)}] {pct}%"


def _esc(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class TelegramBotHandler:
    def __init__(
        self,
        token: str,
        owner_chat_id: str,
        get_status_fn: Callable[[], Coroutine],
        stop_fn: Callable[[], Coroutine],
        resume_fn: Callable[[], Coroutine],
        log_path: Path,
        settings: BotSettings,
        on_cookies_json: Optional[Callable[[str], Coroutine]] = None,
        on_report_fn: Optional[Callable[[], Coroutine]] = None,
        notify_fn: Optional[Callable[[str], Coroutine]] = None,
    ) -> None:
        self._api = TelegramAPI(token, owner_chat_id)
        self._get_status = get_status_fn
        self._stop_fn = stop_fn
        self._resume_fn = resume_fn
        self._log_path = log_path
        self._settings = settings
        self._on_cookies_json = on_cookies_json
        self._on_report_fn = on_report_fn
        self._notify_fn = notify_fn
        self._running = True
        self._paused = False
        self._cookie_buffer: list[str] = []

    # ------------------------------------------------------------------
    async def start(self) -> None:
        await self._api.set_commands([
            {"command": "start", "description": "🏠 Главное меню"},
            {"command": "menu", "description": "🏠 Панель управления"},
            {"command": "status", "description": "📊 Статус бота"},
            {"command": "stats", "description": "🧙 Статы персонажа"},
            {"command": "inventory", "description": "🎒 Рюкзак"},
            {"command": "combat", "description": "⚔️ Статистика боёв"},
            {"command": "quests", "description": "📜 Квесты и NPC"},
            {"command": "area", "description": "🗺 Локация"},
            {"command": "timers", "description": "⏱ Таймеры"},
            {"command": "effects", "description": "✨ Эффекты"},
            {"command": "autopilot", "description": "🤖 Автопилот (фарм/квесты)"},
            {"command": "notify", "description": "🔔 Уведомления"},
            {"command": "reports", "description": "📈 Отчёты"},
            {"command": "report", "description": "📄 Отчёт сейчас"},
            {"command": "settings", "description": "⚙️ Настройки"},
            {"command": "session", "description": "🛡 Сессия / куки"},
            {"command": "log", "description": "📋 Последний лог"},
            {"command": "cookies", "description": "🍪 Обновить куки"},
            {"command": "farm_on", "description": "✅ Включить весь фарм"},
            {"command": "farm_off", "description": "⛔ Выключить весь фарм"},
            {"command": "stop", "description": "⏸ Пауза игрового цикла"},
            {"command": "resume", "description": "▶️ Продолжить"},
            {"command": "help", "description": "ℹ️ Справка по командам"},
        ])
        # Push reply keyboard to owner on boot
        try:
            await self._api.send(
                self._api._owner,
                "🤖 <b>DwarBot онлайн</b>\nПанель управления обновлена. Кнопки меню внизу чата.",
                reply_markup=_reply_keyboard(),
            )
        except Exception:
            pass
        logger.info("Telegram control panel started.")
        await self._poll_loop()

    async def notify(self, text: str, category: str = "") -> None:
        """Send a notification to the owner if the category is enabled."""
        n = self._settings.notify
        gate = {
            "battles": n.battles,
            "quests": n.quests,
            "hp_low": n.hp_low,
            "token": n.token,
            "level_up": n.level_up,
            "money": n.money,
            "errors": n.errors,
            "effects": n.effects,
            "area": n.area,
            "gear": n.gear,
            "heartbeat": n.heartbeat,
        }
        if category and not gate.get(category, True):
            return
        await self._api.send(self._api._owner, text)
        self._settings.total_notifies_sent += 1
        if self._settings.total_notifies_sent % 10 == 0:
            self._settings.save()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await self._api.get_updates()
                for upd in updates:
                    asyncio.ensure_future(self._handle_update(upd))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Telegram poll error: %s", exc)
                await asyncio.sleep(5)

    async def _handle_update(self, upd: dict) -> None:
        msg = upd.get("message")
        if msg:
            chat_id = str(msg["chat"]["id"])
            if not self._api.is_owner(chat_id):
                await self._api.send(chat_id, "⛔ Нет доступа.")
                return
            text = (msg.get("text") or "").strip()
            await self._dispatch(chat_id, text)
            return

        cb = upd.get("callback_query")
        if cb:
            chat_id = str(cb["from"]["id"])
            if not self._api.is_owner(chat_id):
                await self._api.answer_callback(cb["id"], "⛔ Нет доступа.")
                return
            data = cb.get("data", "")
            mid = cb.get("message", {}).get("message_id")
            await self._api.answer_callback(cb["id"])
            await self._dispatch(chat_id, data, message_id=mid)

    # ------------------------------------------------------------------
    async def _dispatch(
        self, chat_id: str, text: str, message_id: Optional[int] = None
    ) -> None:
        stripped = text.strip()

        # Cookie JSON paste
        if self._looks_like_cookie_json(stripped) or (
            self._cookie_buffer and (
                stripped.startswith(("[", "{", '"'))
                or stripped.endswith("]")
                or "mycom" in stripped
                or "sess_" in stripped
            )
        ):
            await self._handle_cookie_paste(chat_id, stripped)
            return

        if self._cookie_buffer and stripped.startswith("/"):
            self._cookie_buffer.clear()

        # Reply keyboard labels
        if stripped in _REPLY_MAP:
            stripped = _REPLY_MAP[stripped]

        # Toggle callbacks: tg:group:key
        if stripped.startswith("tg:"):
            await self._handle_toggle(chat_id, stripped, message_id)
            return

        if stripped.startswith("report_int:"):
            mins = int(stripped.split(":")[1])
            self._settings.report.interval_min = mins
            self._settings.save()
            await self._show_reports(chat_id, message_id, flash=f"Интервал: {mins} мин")
            return

        if stripped.startswith("hp_retreat:"):
            delta = float(stripped.split(":")[1])
            self._settings.farm.hp_retreat = max(5.0, min(80.0, self._settings.farm.hp_retreat + delta))
            self._settings.save()
            await self._show_settings(chat_id, message_id)
            return

        if stripped.startswith("hp_heal:"):
            delta = float(stripped.split(":")[1])
            self._settings.farm.hp_heal = max(10.0, min(95.0, self._settings.farm.hp_heal + delta))
            self._settings.save()
            await self._show_settings(chat_id, message_id)
            return

        if stripped.startswith("max_battles:"):
            delta = int(stripped.split(":")[1])
            self._settings.farm.max_battles_row = max(1, min(100, self._settings.farm.max_battles_row + delta))
            self._settings.save()
            await self._show_settings(chat_id, message_id)
            return

        if stripped == "noop":
            return

        cmd = stripped.split()[0].lower().lstrip("/").split("@")[0] if stripped else ""
        handlers = {
            "start": self._cmd_start,
            "menu": self._cmd_menu,
            "help": self._cmd_help,
            "status": self._cmd_status,
            "stats": self._cmd_stats,
            "inventory": self._cmd_inventory,
            "combat": self._cmd_combat,
            "quests": self._cmd_quests,
            "area": self._cmd_area,
            "timers": self._cmd_timers,
            "effects": self._cmd_effects,
            "autopilot": self._cmd_autopilot,
            "notify": self._cmd_notify,
            "reports": self._cmd_reports,
            "report": self._cmd_report_now,
            "report_now": self._cmd_report_now,
            "settings": self._cmd_settings,
            "session": self._cmd_session,
            "log": self._cmd_log,
            "cookies": self._cmd_cookies,
            "farm_on": self._cmd_farm_all_on,
            "farm_off": self._cmd_farm_all_off,
            "farm_all_on": self._cmd_farm_all_on,
            "farm_all_off": self._cmd_farm_all_off,
            "notify_all_on": self._cmd_notify_all_on,
            "notify_all_off": self._cmd_notify_all_off,
            "stop": self._cmd_stop,
            "resume": self._cmd_resume,
        }
        handler = handlers.get(cmd)
        if handler:
            await handler(chat_id, message_id=message_id)
        else:
            await self._api.send(
                chat_id,
                "❓ Неизвестная команда. Жми <b>🏠 Меню</b> или /help.\n"
                "JSON Cookie Editor можно прислать прямо сюда.",
                reply_markup=_reply_keyboard(),
            )

    async def _handle_toggle(
        self, chat_id: str, data: str, message_id: Optional[int]
    ) -> None:
        # tg:farm:auto_quests
        parts = data.split(":")
        if len(parts) != 3:
            return
        _, group, key = parts
        new_val = self._settings.toggle(group, key)
        if new_val is None:
            await self._api.send(chat_id, f"⚠️ Неизвестный параметр: {group}.{key}")
            return
        # Refresh the relevant panel
        if group == "farm":
            await self._show_autopilot(chat_id, message_id, flash=f"{key} → {self._settings.on_off(new_val)}")
        elif group == "notify":
            await self._show_notify(chat_id, message_id, flash=f"{key} → {self._settings.on_off(new_val)}")
        elif group == "report":
            await self._show_reports(chat_id, message_id, flash=f"{key} → {self._settings.on_off(new_val)}")

    # ------------------------------------------------------------------
    # Show / edit panels
    # ------------------------------------------------------------------

    async def _reply(
        self,
        chat_id: str,
        text: str,
        markup: dict,
        message_id: Optional[int] = None,
        with_reply_kb: bool = False,
    ) -> None:
        if message_id:
            await self._api.edit_message(chat_id, message_id, text, reply_markup=markup)
            return
        extra = _reply_keyboard() if with_reply_kb else markup
        # If we want both — send reply keyboard once, then inline in same message if possible
        # Telegram allows only one reply_markup; prefer inline for panels, reply kb on start/menu.
        await self._api.send(chat_id, text, reply_markup=extra if with_reply_kb else markup)

    async def _show_autopilot(
        self, chat_id: str, message_id: Optional[int] = None, flash: str = ""
    ) -> None:
        lines = self._settings.farm_summary_lines()
        text = (
            "<b>🤖 Автопилот — фарм и квесты</b>\n\n"
            + "\n".join(lines)
            + "\n\nНажимай кнопки, чтобы включать/выключать модули."
        )
        if flash:
            text = f"<i>{_esc(flash)}</i>\n\n" + text
        await self._reply(chat_id, text, _autopilot_inline(self._settings), message_id)

    async def _show_notify(
        self, chat_id: str, message_id: Optional[int] = None, flash: str = ""
    ) -> None:
        lines = self._settings.notify_summary_lines()
        text = (
            "<b>🔔 Система уведомлений</b>\n\n"
            + "\n".join(lines)
            + f"\n\n📤 Отправлено всего: <b>{self._settings.total_notifies_sent}</b>"
        )
        if flash:
            text = f"<i>{_esc(flash)}</i>\n\n" + text
        await self._reply(chat_id, text, _notify_inline(self._settings), message_id)

    async def _show_reports(
        self, chat_id: str, message_id: Optional[int] = None, flash: str = ""
    ) -> None:
        r = self._settings.report
        last = "никогда"
        if self._settings.last_report_at:
            ago = int(time.time() - self._settings.last_report_at)
            last = f"{ago // 60} мин назад" if ago >= 60 else f"{ago} сек назад"
        text = (
            "<b>📈 Отчёты</b>\n\n"
            f"Авто-отчёты: {self._settings.on_off(r.enabled)}\n"
            f"Интервал: <b>{r.interval_min}</b> мин\n"
            f"Последний: {last}\n\n"
            f"В отчёте — бои: {self._settings.on_off(r.include_combat)}\n"
            f"В отчёте — квесты: {self._settings.on_off(r.include_quests)}\n"
            f"В отчёте — инвентарь: {self._settings.on_off(r.include_inventory)}\n"
            f"В отчёте — таймеры: {self._settings.on_off(r.include_timers)}"
        )
        if flash:
            text = f"<i>{_esc(flash)}</i>\n\n" + text
        await self._reply(chat_id, text, _reports_inline(self._settings), message_id)

    async def _show_settings(
        self, chat_id: str, message_id: Optional[int] = None
    ) -> None:
        f = self._settings.farm
        text = (
            "<b>⚙️ Настройки DwarBot</b>\n\n"
            f"❤️ Retreat HP: <b>{f.hp_retreat:.0f}%</b>\n"
            f"🧪 Heal HP: <b>{f.hp_heal:.0f}%</b>\n"
            f"⚔️ Макс. боёв подряд: <b>{f.max_battles_row}</b>\n\n"
            "Быстрые разделы:"
        )
        await self._reply(chat_id, text, _settings_inline(self._settings), message_id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_start(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        icon = "🟢" if st.get("running") else "⏸"
        text = (
            f"<b>🐉 DwarBot — Легенда: Наследие Драконов</b>\n\n"
            f"{icon} {('Работает' if st.get('running') else 'Пауза')} · "
            f"<b>{_esc(st.get('nick','?'))}</b> Lv{st.get('level','?')}\n"
            f"❤️ {st.get('hp','?')}/{st.get('hp_max','?')} · "
            f"💰 {st.get('money','?')} · "
            f"📍 {_esc(st.get('area_title') or st.get('area_id','?'))}\n\n"
            "Кнопки меню — <b>внизу чата</b>.\n"
            "Автопилот / уведомления / отчёты — ниже."
        )
        await self._api.send(chat_id, text, reply_markup=_reply_keyboard())
        await self._api.send(chat_id, "📂 <b>Панель управления</b>", reply_markup=_menu_inline())

    async def _cmd_menu(self, chat_id: str, message_id: Optional[int] = None) -> None:
        text = "📂 <b>Панель управления DwarBot</b>\nВыбери раздел:"
        await self._reply(chat_id, text, _menu_inline(), message_id, with_reply_kb=not message_id)

    async def _cmd_help(self, chat_id: str, message_id: Optional[int] = None) -> None:
        text = (
            "<b>ℹ️ Справка DwarBot</b>\n\n"
            "<b>Основные</b>\n"
            "/status /stats /inventory /area /timers /effects\n"
            "/combat /quests /session /log /cookies\n\n"
            "<b>Автопилот</b>\n"
            "/autopilot — вкл/выкл фарм боёв, квестов, переходов\n"
            "/farm_on /farm_off — всё сразу\n\n"
            "<b>Уведомления и отчёты</b>\n"
            "/notify — какие события слать в чат\n"
            "/reports — авто-отчёты · /report — отчёт сейчас\n\n"
            "<b>Управление</b>\n"
            "/stop · /resume · /settings · /menu\n\n"
            "🍪 Куки: Cookie Editor → Export JSON → пришли сюда."
        )
        await self._api.send(chat_id, text, reply_markup=_menu_inline())

    async def _cmd_status(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        f = self._settings.farm
        icon = "🟢" if st.get("running") else "⏸"
        token = "✅" if st.get("token_ok") else "❌"
        text = (
            f"<b>📊 Статус</b>\n\n"
            f"{icon} Цикл: <b>{'Работает' if st.get('running') else 'Пауза'}</b>\n"
            f"🔑 Токен: {token} · sess <code>{_esc(st.get('sess_sid','?'))}…</code>\n"
            f"🔄 Тик: {st.get('iteration', 0)} · ⏱ {st.get('uptime','?')}\n"
            f"📍 {_esc(st.get('area_title') or 'area')} ({st.get('area_id','?')})\n\n"
            f"<b>Автопилот</b>\n"
            f"📜 Квесты {self._settings.on_off(f.auto_quests)} · "
            f"⚔️ Бои {self._settings.on_off(f.auto_combat)}\n"
            f"🗺 Переходы {self._settings.on_off(f.auto_travel)} · "
            f"🧪 Heal {self._settings.on_off(f.auto_heal)}\n\n"
            f"⚔️ Боёв: {st.get('battles',0)} · 📜 Диалогов: {st.get('dialogues',0)}"
        )
        await self._reply(chat_id, text, _menu_inline(), message_id)

    async def _cmd_stats(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        hp, hp_max = int(st.get("hp") or 0), int(st.get("hp_max") or 1)
        mp, mp_max = int(st.get("mp") or 0), int(st.get("mp_max") or 0)
        hp_pct = int(hp / hp_max * 100) if hp_max else 0
        mp_pct = int(mp / mp_max * 100) if mp_max else 0
        text = (
            f"<b>🧙 {_esc(st.get('nick','?'))} — Ур. {st.get('level','?')}</b>\n\n"
            f"❤️ HP: {hp}/{hp_max}\n{_progress_bar(hp_pct)}\n\n"
            f"💧 MP: {mp}/{mp_max}\n{_progress_bar(mp_pct)}\n\n"
            f"💰 {_esc(st.get('money','0'))} зол.\n"
            f"📍 {_esc(st.get('area_title') or st.get('area_id','?'))}\n"
            f"🏁 flags: {st.get('flags',0)} / {st.get('flags2',0)} / {st.get('flags3',0)}"
        )
        await self._reply(chat_id, text, _menu_inline(), message_id)

    async def _cmd_inventory(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        inv = st.get("inventory") or []
        if not inv:
            text = "🎒 <b>Рюкзак пуст</b>"
        else:
            lines = []
            for it in inv[:25]:
                dur, dmax = it.get("dur", 0), it.get("dur_max", 0)
                dur_str = f" [{dur}/{dmax}]" if dmax else ""
                broken = " ⚠️" if dmax and dur <= 0 else ""
                lines.append(
                    f"• <b>{_esc(it.get('title','?'))}</b> "
                    f"<i>({_esc(it.get('kind','?'))})</i>{dur_str}{broken}"
                )
            text = (
                f"🎒 <b>Рюкзак — {len(inv)} шт.</b>\n\n"
                + "\n".join(lines)
                + f"\n\n🧪 Отваров: <b>{st.get('potions_count', 0)}</b>"
            )
        await self._reply(chat_id, text, _menu_inline(), message_id)

    async def _cmd_combat(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        f = self._settings.farm
        text = (
            f"<b>⚔️ Бои</b>\n\n"
            f"🗡 Начато: <b>{st.get('battles', 0)}</b>\n"
            f"🏆 Побед: <b>{st.get('wins', 0)}</b> · "
            f"💀 Поражений: <b>{st.get('losses', 0)}</b>\n"
            f"📈 Винрейт: <b>{st.get('win_rate', 0):.1f}%</b>\n"
            f"👊 Атак: {st.get('attacks', 0)} · 🧪 Зелий: {st.get('potions_used', 0)}\n"
            f"❤️ HP: {st.get('hp','?')}/{st.get('hp_max','?')}\n\n"
            f"<b>Фарм</b>\n"
            f"Авто: {self._settings.on_off(f.auto_combat)}\n"
            f"Фронты {self._settings.on_off(f.farm_fronts)} · "
            f"Арена {self._settings.on_off(f.farm_arena)} · "
            f"Точки {self._settings.on_off(f.farm_area)}"
        )
        kb = _inline([
            [_btn(_toggle_label("Бои авто", f.auto_combat), "tg:farm:auto_combat")],
            [_btn(_toggle_label("Фронты", f.farm_fronts), "tg:farm:farm_fronts"),
             _btn(_toggle_label("Арена", f.farm_arena), "tg:farm:farm_arena")],
            [_btn(_toggle_label("Точки", f.farm_area), "tg:farm:farm_area")],
            [_btn("🤖 Автопилот", "autopilot"), _btn("🏠 Меню", "menu")],
        ])
        await self._reply(chat_id, text, kb, message_id)

    async def _cmd_quests(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        f = self._settings.farm
        npcs = st.get("npcs") or []
        npc_lines = "\n".join(
            f"• {_esc(n.get('title','?'))} (⏱{n.get('time_left',0)}с)"
            for n in npcs[:6]
        ) or "• нет"
        text = (
            f"<b>📜 Квесты</b>\n\n"
            f"✅ Завершено: <b>{st.get('quests_completed', 0)}</b>\n"
            f"📝 Принято: <b>{st.get('quests_accepted', 0)}</b>\n"
            f"💬 Диалогов: <b>{st.get('dialogues', 0)}</b>\n"
            f"👥 NPC: <b>{st.get('npcs_visited', 0)}</b>\n"
            f"Авто-квесты: {self._settings.on_off(f.auto_quests)}\n\n"
            f"<b>События / NPC</b>\n{npc_lines}"
        )
        kb = _inline([
            [_btn(_toggle_label("Авто-квесты", f.auto_quests), "tg:farm:auto_quests")],
            [_btn("🤖 Автопилот", "autopilot"), _btn("🏠 Меню", "menu")],
        ])
        await self._reply(chat_id, text, kb, message_id)

    async def _cmd_area(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        items = st.get("area_items") or []
        npcs = st.get("npcs") or []
        items_text = "\n".join(
            f"• {_esc(i.get('name','?'))} <i>({_esc(i.get('item_type','?'))})</i>"
            for i in items[:8]
        ) or "• нет"
        npcs_text = "\n".join(
            f"• {_esc(n.get('title','?'))}"
            for n in npcs[:5]
        ) or "• нет"
        f = self._settings.farm
        text = (
            f"<b>🗺 {_esc(st.get('area_title') or 'Локация')}</b> "
            f"(id={st.get('area_id','?')})\n\n"
            f"🚪 Переходы / точки:\n{items_text}\n\n"
            f"👥 NPC:\n{npcs_text}\n\n"
            f"Авто-переходы: {self._settings.on_off(f.auto_travel)}"
        )
        kb = _inline([
            [_btn(_toggle_label("Переходы", f.auto_travel), "tg:farm:auto_travel")],
            [_btn("🏠 Меню", "menu")],
        ])
        await self._reply(chat_id, text, kb, message_id)

    async def _cmd_timers(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        timers = st.get("timers") or []
        if not timers:
            text = "⏱ <b>Активных таймеров нет</b>"
        else:
            lines = [
                f"• {_esc(t.get('description','?'))}: <b>{_esc(t.get('remaining','?'))}</b>"
                for t in timers[:20]
            ]
            text = f"⏱ <b>Таймеры ({len(timers)})</b>\n\n" + "\n".join(lines)
        await self._reply(chat_id, text, _menu_inline(), message_id)

    async def _cmd_effects(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        effects = st.get("effects") or []
        if not effects:
            text = "✨ <b>Активных эффектов нет</b>"
        else:
            lines = [f"• {_esc(e.get('title','?'))}" for e in effects[:20]]
            text = f"✨ <b>Эффекты ({len(effects)})</b>\n\n" + "\n".join(lines)
        await self._reply(chat_id, text, _menu_inline(), message_id)

    async def _cmd_autopilot(self, chat_id: str, message_id: Optional[int] = None) -> None:
        await self._show_autopilot(chat_id, message_id)

    async def _cmd_notify(self, chat_id: str, message_id: Optional[int] = None) -> None:
        await self._show_notify(chat_id, message_id)

    async def _cmd_reports(self, chat_id: str, message_id: Optional[int] = None) -> None:
        await self._show_reports(chat_id, message_id)

    async def _cmd_settings(self, chat_id: str, message_id: Optional[int] = None) -> None:
        await self._show_settings(chat_id, message_id)

    async def _cmd_session(self, chat_id: str, message_id: Optional[int] = None) -> None:
        st = await self._get_status()
        text = (
            f"<b>🛡 Сессия</b>\n\n"
            f"🔑 Токен: {'✅ OK' if st.get('token_ok') else '❌ Истёк'}\n"
            f"🪪 sess_sid: <code>{_esc(st.get('sess_sid','?'))}…</code>\n"
            f"👤 {_esc(st.get('nick','?'))} Lv{st.get('level','?')}\n"
            f"⏱ Аптайм: {st.get('uptime','?')}\n\n"
            "Чтобы обновить — /cookies или пришли Cookie Editor JSON."
        )
        await self._reply(chat_id, text, _menu_inline(), message_id)

    async def _cmd_log(self, chat_id: str, message_id: Optional[int] = None) -> None:
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
            clean = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in lines[-25:]]
            # Prefer game lines over telegram noise
            filtered = [
                l for l in clean
                if "getUpdates" not in l and "answerCallbackQuery" not in l
            ][-18:]
            log_text = "\n".join(filtered) or "\n".join(clean[-15:])
            if len(log_text) > 3500:
                log_text = "…\n" + log_text[-3500:]
            text = f"<b>📋 Лог</b>\n\n<pre>{_esc(log_text)}</pre>"
        except Exception as exc:
            text = f"⚠️ Не удалось прочитать лог: {_esc(exc)}"
        await self._api.send(chat_id, text)

    async def _cmd_cookies(self, chat_id: str, message_id: Optional[int] = None) -> None:
        text = (
            "<b>🍪 Обновление куков</b>\n\n"
            "1. Открой <b>https://w1.dwar.ru</b> и войди\n"
            "2. Cookie Editor → <b>Export as JSON</b>\n"
            "3. Пришли JSON <b>сюда</b> — сессия обновится без рестарта\n\n"
            "Нужен <code>mycom</code> (access_token). "
            "<code>sess_sid</code> бот создаст сам."
        )
        await self._api.send(chat_id, text)

    async def _cmd_report_now(self, chat_id: str, message_id: Optional[int] = None) -> None:
        if self._on_report_fn:
            text = await self._on_report_fn()
        else:
            text = await self._build_report()
        self._settings.last_report_at = time.time()
        self._settings.save()
        await self._api.send(chat_id, text, reply_markup=_reports_inline(self._settings))

    async def _build_report(self) -> str:
        st = await self._get_status()
        r = self._settings.report
        parts = [
            f"<b>📈 Отчёт DwarBot</b> · {time.strftime('%H:%M:%S')}",
            f"🧙 <b>{_esc(st.get('nick','?'))}</b> Lv{st.get('level','?')} · "
            f"❤️ {st.get('hp','?')}/{st.get('hp_max','?')} · "
            f"💰 {st.get('money','?')}",
            f"📍 {_esc(st.get('area_title') or st.get('area_id','?'))} · "
            f"⏱ {st.get('uptime','?')} · тик {st.get('iteration',0)}",
        ]
        if r.include_combat:
            parts.append(
                f"⚔️ Бои: {st.get('battles',0)} · "
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
                f"🧪 {st.get('potions_count',0)}"
            )
        if r.include_timers:
            timers = st.get("timers") or []
            if timers:
                tlines = ", ".join(
                    f"{t.get('description','?')}: {t.get('remaining','?')}"
                    for t in timers[:4]
                )
                parts.append(f"⏱ { _esc(tlines) }")
        f = self._settings.farm
        parts.append(
            f"🤖 Квесты {self._settings.on_off(f.auto_quests)} · "
            f"Бои {self._settings.on_off(f.auto_combat)} · "
            f"Переходы {self._settings.on_off(f.auto_travel)}"
        )
        return "\n".join(parts)

    async def _cmd_farm_all_on(self, chat_id: str, message_id: Optional[int] = None) -> None:
        for key in (
            "auto_quests", "auto_combat", "farm_fronts", "farm_arena", "farm_area",
            "auto_travel", "auto_repair", "auto_equip", "auto_heal",
        ):
            setattr(self._settings.farm, key, True)
        self._settings.save()
        await self._show_autopilot(chat_id, message_id, flash="Весь фарм включён")

    async def _cmd_farm_all_off(self, chat_id: str, message_id: Optional[int] = None) -> None:
        for key in (
            "auto_quests", "auto_combat", "farm_fronts", "farm_arena", "farm_area",
            "auto_travel", "aggressive",
        ):
            setattr(self._settings.farm, key, False)
        self._settings.save()
        await self._show_autopilot(chat_id, message_id, flash="Фарм выключен")

    async def _cmd_notify_all_on(self, chat_id: str, message_id: Optional[int] = None) -> None:
        for key in self._settings.notify.__dataclass_fields__:
            setattr(self._settings.notify, key, True)
        self._settings.save()
        await self._show_notify(chat_id, message_id, flash="Все уведомления ВКЛ")

    async def _cmd_notify_all_off(self, chat_id: str, message_id: Optional[int] = None) -> None:
        for key in self._settings.notify.__dataclass_fields__:
            setattr(self._settings.notify, key, False)
        self._settings.notify.token = True
        self._settings.notify.errors = True
        self._settings.save()
        await self._show_notify(chat_id, message_id, flash="Уведомления выкл (токен/ошибки оставлены)")

    async def _cmd_stop(self, chat_id: str, message_id: Optional[int] = None) -> None:
        self._paused = True
        await self._stop_fn()
        await self._api.send(
            chat_id,
            "⏸ Игровой цикл <b>на паузе</b>. Автопилот не крутится.\nЖми ▶️ Старт или /resume.",
            reply_markup=_reply_keyboard(),
        )

    async def _cmd_resume(self, chat_id: str, message_id: Optional[int] = None) -> None:
        self._paused = False
        await self._resume_fn()
        await self._api.send(
            chat_id,
            "▶️ Игровой цикл <b>запущен</b>.",
            reply_markup=_reply_keyboard(),
        )

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_cookie_json(text: str) -> bool:
        t = text.lstrip()
        if t.startswith("```"):
            t = t.strip("`")
            if t.lower().startswith("json"):
                t = t[4:].lstrip()
        return t.startswith("[") and (
            "mycom" in t or "sess_sid" in t or '"name"' in t[:240]
        )

    async def _handle_cookie_paste(self, chat_id: str, chunk: str) -> None:
        if not self._on_cookies_json:
            await self._api.send(chat_id, "⚠️ Приём куков не настроен.")
            return
        cleaned = chunk.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].lstrip()
        self._cookie_buffer.append(cleaned)
        combined = "\n".join(self._cookie_buffer).strip()
        if not (combined.startswith("[") and combined.endswith("]")):
            await self._api.send(
                chat_id,
                f"🍪 Фрагмент JSON ({len(self._cookie_buffer)}). Пришли остаток.",
            )
            return
        self._cookie_buffer.clear()
        await self._api.send(chat_id, "🍪 Принимаю куки…")
        try:
            result = await self._on_cookies_json(combined)
        except Exception as exc:
            result = f"❌ Ошибка: {exc}"
        await self._api.send(chat_id, result, reply_markup=_reply_keyboard())

    def stop(self) -> None:
        self._running = False
