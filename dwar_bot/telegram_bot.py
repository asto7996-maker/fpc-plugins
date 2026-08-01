"""
Telegram bot command interface for DwarBot.

Runs as a background asyncio task alongside the main game loop.
Polls Telegram for updates and handles commands from the owner.

Available commands:
  /start    — приветствие и главное меню
  /status   — состояние бота (работает / ждёт куки)
  /stats    — статы персонажа (HP, MP, уровень, деньги)
  /area     — текущая локация и доступные действия
  /log      — последние 20 строк лога
  /cookies  — инструкция по обновлению куков
  /stop     — остановить игровой цикл
  /resume   — возобновить игровой цикл
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"

# ---------------------------------------------------------------------------
# Low-level Telegram API helpers
# ---------------------------------------------------------------------------

class TelegramAPI:
    def __init__(self, token: str, owner_chat_id: str) -> None:
        self._token = token
        self._owner = str(owner_chat_id)
        self._base = f"https://api.telegram.org/bot{token}"
        self._offset: int = 0

    async def _post(self, method: str, payload: dict) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
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
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._post("sendMessage", payload)

    async def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        await self._post("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        })

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

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
# Inline keyboard builder
# ---------------------------------------------------------------------------

def _main_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Статы", "callback_data": "stats"},
                {"text": "🗺 Локация", "callback_data": "area"},
            ],
            [
                {"text": "📋 Лог", "callback_data": "log"},
                {"text": "🔄 Обновить статус", "callback_data": "status"},
            ],
            [
                {"text": "🍪 Обновить куки", "callback_data": "cookies"},
            ],
        ]
    }


# ---------------------------------------------------------------------------
# TelegramBotHandler
# ---------------------------------------------------------------------------

class TelegramBotHandler:
    """
    Asynchronous Telegram command handler.

    Parameters
    ----------
    token:
        Telegram bot token.
    owner_chat_id:
        Chat ID of the bot owner — only this user can send commands.
    get_status_fn:
        Async callable that returns a dict with keys:
        running (bool), nick (str), level (int), hp (int), hp_max (int),
        mp (int), mp_max (int), area_id (str), money (float), iteration (int)
    stop_fn / resume_fn:
        Async callables to stop/resume the game loop.
    log_path:
        Path to the bot log file.
    """

    def __init__(
        self,
        token: str,
        owner_chat_id: str,
        get_status_fn: Callable[[], Coroutine],
        stop_fn: Callable[[], Coroutine],
        resume_fn: Callable[[], Coroutine],
        log_path: Path,
    ) -> None:
        self._api = TelegramAPI(token, owner_chat_id)
        self._get_status = get_status_fn
        self._stop_fn = stop_fn
        self._resume_fn = resume_fn
        self._log_path = log_path
        self._running = True
        self._paused = False

    async def start(self) -> None:
        """Register commands with Telegram and start the polling loop."""
        await self._api.set_commands([
            {"command": "start",   "description": "Главное меню"},
            {"command": "status",  "description": "Состояние бота"},
            {"command": "stats",   "description": "Статы персонажа"},
            {"command": "area",    "description": "Текущая локация"},
            {"command": "log",     "description": "Последние строки лога"},
            {"command": "cookies", "description": "Как обновить куки"},
            {"command": "stop",    "description": "Остановить игровой цикл"},
            {"command": "resume",  "description": "Возобновить игровой цикл"},
        ])
        logger.info("Telegram bot commands registered. Starting polling …")
        await self._poll_loop()

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
        # Message command
        msg = upd.get("message")
        if msg:
            chat_id = str(msg["chat"]["id"])
            if not self._api.is_owner(chat_id):
                await self._api.send(chat_id, "⛔ Нет доступа.")
                return
            text = msg.get("text", "").strip()
            await self._dispatch_command(chat_id, text)
            return

        # Inline button callback
        cb = upd.get("callback_query")
        if cb:
            chat_id = str(cb["from"]["id"])
            if not self._api.is_owner(chat_id):
                await self._api.answer_callback(cb["id"], "⛔ Нет доступа.")
                return
            data = cb.get("data", "")
            await self._api.answer_callback(cb["id"])
            await self._dispatch_command(chat_id, f"/{data}")

    async def _dispatch_command(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        handlers = {
            "start":   self._cmd_start,
            "status":  self._cmd_status,
            "stats":   self._cmd_stats,
            "area":    self._cmd_area,
            "log":     self._cmd_log,
            "cookies": self._cmd_cookies,
            "stop":    self._cmd_stop,
            "resume":  self._cmd_resume,
            "help":    self._cmd_start,
        }
        handler = handlers.get(cmd)
        if handler:
            await handler(chat_id)
        else:
            await self._api.send(chat_id,
                "❓ Неизвестная команда. Используй /start для меню.")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(self, chat_id: str) -> None:
        st = await self._get_status()
        state_icon = "🟢" if st.get("running") else "🔴"
        text = (
            f"<b>🤖 DwarBot — Легенда: Наследие Драконов</b>\n\n"
            f"{state_icon} Статус: {'Работает' if st.get('running') else 'Остановлен'}\n"
            f"👤 Персонаж: <b>{st.get('nick','?')}</b> Lv{st.get('level','?')}\n"
            f"❤️ HP: {st.get('hp','?')}/{st.get('hp_max','?')}\n"
            f"💰 Деньги: {st.get('money','?')}\n\n"
            "Выбери действие:"
        )
        await self._api.send(chat_id, text, reply_markup=_main_keyboard())

    async def _cmd_status(self, chat_id: str) -> None:
        st = await self._get_status()
        state_icon = "🟢" if st.get("running") else ("⏸" if self._paused else "🔴")
        token_ok = st.get("token_ok", True)
        text = (
            f"<b>📊 Статус DwarBot</b>\n\n"
            f"{state_icon} Цикл: {'Работает' if st.get('running') else ('Пауза' if self._paused else 'Остановлен')}\n"
            f"🔑 Токен: {'✅ Действителен' if token_ok else '❌ Истёк — нужны новые куки'}\n"
            f"🔄 Итераций: {st.get('iteration', 0)}\n"
            f"⏱ Время работы: {st.get('uptime','?')}\n"
            f"📍 Локация: area_id={st.get('area_id','?')}\n"
        )
        await self._api.send(chat_id, text, reply_markup=_main_keyboard())

    async def _cmd_stats(self, chat_id: str) -> None:
        st = await self._get_status()
        hp = st.get("hp", 0)
        hp_max = st.get("hp_max", 1)
        hp_pct = int(hp / hp_max * 100) if hp_max else 0
        hp_bar = _progress_bar(hp_pct)
        mp = st.get("mp", 0)
        mp_max = st.get("mp_max", 0)
        mp_pct = int(mp / mp_max * 100) if mp_max else 0
        mp_bar = _progress_bar(mp_pct)
        text = (
            f"<b>🧙 {st.get('nick','?')} — Уровень {st.get('level','?')}</b>\n\n"
            f"❤️ HP: {hp}/{hp_max}  ({hp_pct}%)\n{hp_bar}\n\n"
            f"💧 MP: {mp}/{mp_max}  ({mp_pct}%)\n{mp_bar}\n\n"
            f"💰 Деньги: <b>{st.get('money','0')}</b> зол.\n"
            f"📍 Локация: area {st.get('area_id','?')}\n"
            f"🏆 Флаги: {st.get('flags',0)} / {st.get('flags2',0)} / {st.get('flags3',0)}"
        )
        await self._api.send(chat_id, text, reply_markup=_main_keyboard())

    async def _cmd_area(self, chat_id: str) -> None:
        st = await self._get_status()
        area_title = st.get("area_title", "Неизвестно")
        items = st.get("area_items", [])
        npcs = st.get("npcs", [])

        items_text = "\n".join(
            f"  • {i.get('name','?')} ({i.get('item_type','?')})"
            for i in items[:5]
        ) or "  нет"

        npcs_text = "\n".join(
            f"  • {n.get('title','?')} (id={n.get('npc_id','?')}, ⏱{n.get('time_left',0)}с)"
            for n in npcs[:5]
        ) or "  нет"

        text = (
            f"<b>🗺 Локация: {area_title}</b> (id={st.get('area_id','?')})\n\n"
            f"🚪 Переходы:\n{items_text}\n\n"
            f"👥 NPC:\n{npcs_text}"
        )
        await self._api.send(chat_id, text, reply_markup=_main_keyboard())

    async def _cmd_log(self, chat_id: str) -> None:
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
            # Strip ANSI colour codes
            import re
            clean = [re.sub(r'\x1b\[[0-9;]*m', '', l) for l in lines[-20:]]
            log_text = "\n".join(clean)
            if len(log_text) > 3800:
                log_text = "…\n" + log_text[-3800:]
            text = f"<b>📋 Последние строки лога:</b>\n\n<pre>{log_text}</pre>"
        except Exception as exc:
            text = f"⚠️ Не удалось прочитать лог: {exc}"
        await self._api.send(chat_id, text)

    async def _cmd_cookies(self, chat_id: str) -> None:
        text = (
            "<b>🍪 Обновление куков (токен истёк)</b>\n\n"
            "1️⃣ Открой <b>https://w1.dwar.ru</b> в браузере и войди в игру\n\n"
            "2️⃣ Установи расширение <b>Cookie Editor</b>\n\n"
            "3️⃣ Нажми расширение → <b>Export → Export as JSON</b>\n\n"
            "4️⃣ Скопируй JSON и пришли его <b>сюда в чат</b> — "
            "бот обновится автоматически\n\n"
            "Или через SSH:\n"
            "<code>scp cookies.json root@31.76.30.135:"
            "/root/dwar_bot/cookies/session_cookies.json</code>"
        )
        await self._api.send(chat_id, text)

    async def _cmd_stop(self, chat_id: str) -> None:
        self._paused = True
        await self._stop_fn()
        await self._api.send(chat_id,
            "⏸ Игровой цикл <b>остановлен</b>.\nИспользуй /resume для возобновления.")

    async def _cmd_resume(self, chat_id: str) -> None:
        self._paused = False
        await self._resume_fn()
        await self._api.send(chat_id,
            "▶️ Игровой цикл <b>возобновлён</b>.")

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _progress_bar(pct: int, length: int = 10) -> str:
    filled = int(pct / 100 * length)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {pct}%"
