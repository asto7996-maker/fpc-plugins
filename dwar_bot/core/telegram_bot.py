"""
Удалённое управление и мониторинг бота через Telegram (aiohttp long-polling).

Слушает команды только от разрешённого CHAT_ID из config.
Флаги is_paused / should_stop разделяются с main_loop потокобезопасно
через asyncio.Event.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import aiohttp

from dwar_bot.config import BotConfig, SCREENSHOTS_DIR, config

logger = logging.getLogger(__name__)

StatsProvider = Callable[[], Awaitable[str]]
ScreenshotProvider = Callable[[], Awaitable[Optional[Path]]]
AlertCallback = Callable[[str], Awaitable[None]]

# Текст кнопок reply-клавиатуры (совпадает с обработчиками)
BTN_STATS = "📊 Статус"
BTN_SCREENSHOT = "📸 Скриншот"
BTN_PAUSE = "⏸ Пауза"
BTN_RESUME = "▶️ Продолжить"
BTN_STOP = "⏹ Стоп"
BTN_HELP = "❓ Помощь"


@dataclass
class RemoteControlState:
    """
    Общее состояние между Telegram-ботом и main_loop.

    asyncio.Event потокобезопасен в рамках одного event loop.
    """

    _pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    current_task: str = "idle"
    farm_summary: str = ""
    extra_status: str = ""

    def __post_init__(self) -> None:
        # pause_event SET = работаем; CLEARED = на паузе
        if not self._pause_event.is_set():
            self._pause_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    @property
    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def pause(self) -> None:
        self._pause_event.clear()
        logger.info("RemoteControl: пауза")

    def resume(self) -> None:
        self._pause_event.set()
        logger.info("RemoteControl: resume")

    def request_stop(self) -> None:
        self._stop_event.set()
        self._pause_event.set()  # разблокируем waiters
        logger.info("RemoteControl: stop")

    async def wait_if_paused(self, *, poll_sec: float = 1.0) -> None:
        """Блокируется, пока is_paused; периодически проверяет should_stop."""
        while self.is_paused and not self.should_stop:
            try:
                await asyncio.wait_for(self._pause_event.wait(), timeout=poll_sec)
            except asyncio.TimeoutError:
                continue


class TelegramRemoteControl:
    """
    Асинхронный Telegram remote control на long-polling (Bot API через aiohttp).
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        state: Optional[RemoteControlState] = None,
        stats_provider: Optional[StatsProvider] = None,
        screenshot_provider: Optional[ScreenshotProvider] = None,
    ) -> None:
        self._config = bot_config or config
        tg = self._config.telegram
        self._token = tg.bot_token
        self._chat_id = str(tg.chat_id)
        self._enabled = bool(tg.enabled and self._token and self._chat_id)

        self.state = state or RemoteControlState()
        self._stats_provider = stats_provider
        self._screenshot_provider = screenshot_provider

        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._offset: int = 0
        self._running = False
        self._api = f"https://api.telegram.org/bot{self._token}" if self._token else ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> Optional[asyncio.Task[None]]:
        """Запускает polling в фоновой задаче."""
        if not self._enabled:
            logger.info(
                "TelegramRemoteControl отключён "
                "(задайте DWAR_TELEGRAM_ENABLED, TOKEN, CHAT_ID)"
            )
            return None

        if self._task is not None and not self._task.done():
            return self._task

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, sock_read=55)
        )
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-remote")
        logger.info(
            "TelegramRemoteControl запущен (chat_id=%s)", self._chat_id
        )
        try:
            await self.send_alert(
                "🐉 DwarBot remote online\nКоманды: /help",
            )
        except Exception as exc:
            logger.debug("startup alert failed: %s", exc)
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("TelegramRemoteControl остановлен")

    def bind_providers(
        self,
        *,
        stats_provider: Optional[StatsProvider] = None,
        screenshot_provider: Optional[ScreenshotProvider] = None,
    ) -> None:
        if stats_provider is not None:
            self._stats_provider = stats_provider
        if screenshot_provider is not None:
            self._screenshot_provider = screenshot_provider

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    async def send_alert(
        self,
        text: str,
        photo_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """Отправка уведомления (текст и опционально фото) в CHAT_ID."""
        if not self._enabled:
            logger.debug("send_alert skipped (telegram disabled): %s", text[:80])
            return False

        session = await self._ensure_session()
        try:
            if photo_path:
                path = Path(photo_path)
                if path.is_file():
                    return await self._send_photo(session, path, caption=text)
            return await self._send_message(session, text)
        except Exception as exc:
            logger.error("send_alert failed: %s", exc, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        assert self._session is not None
        # Сброс webhook на всякий случай
        try:
            await self._api_call("deleteWebhook", {"drop_pending_updates": True})
        except Exception as exc:
            logger.debug("deleteWebhook: %s", exc)

        while self._running:
            try:
                updates = await self._get_updates(timeout=25)
                for update in updates:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    await self._dispatch_update(update)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                continue
            except aiohttp.ClientError as exc:
                logger.warning("Telegram poll network error: %s", exc)
                await asyncio.sleep(3.0)
            except Exception as exc:
                logger.error("Telegram poll error: %s", exc, exc_info=True)
                await asyncio.sleep(2.0)

    async def _get_updates(self, timeout: int = 25) -> List[Dict[str, Any]]:
        session = await self._ensure_session()
        params = {
            "timeout": timeout,
            "offset": self._offset,
            "allowed_updates": '["message","callback_query"]',
        }
        async with session.get(
            f"{self._api}/getUpdates",
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout + 15),
        ) as resp:
            data = await resp.json(content_type=None)
            if not data.get("ok"):
                logger.warning("getUpdates not ok: %s", data)
                return []
            result = data.get("result") or []
            return list(result) if isinstance(result, list) else []

    async def _dispatch_update(self, update: Dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            # callback_query от inline (если появится)
            cq = update.get("callback_query")
            if cq:
                msg = cq.get("message") or {}
                chat = msg.get("chat") or {}
                if not self._is_allowed_chat(chat.get("id")):
                    return
                data = str(cq.get("data") or "")
                await self._handle_command(data, chat.get("id"))
                await self._api_call(
                    "answerCallbackQuery",
                    {"callback_query_id": cq.get("id")},
                )
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self._is_allowed_chat(chat_id):
            logger.warning(
                "Игнор сообщения от чужого chat_id=%s", chat_id
            )
            return

        text = (message.get("text") or "").strip()
        if not text:
            return
        await self._handle_command(text, chat_id)

    def _is_allowed_chat(self, chat_id: Any) -> bool:
        if chat_id is None:
            return False
        return str(chat_id) == str(self._chat_id)

    async def _handle_command(self, text: str, chat_id: Any) -> None:
        cmd = text.strip()
        low = cmd.lower()

        # Нормализация /command@botname
        if low.startswith("/"):
            low = low.split("@", 1)[0]
            cmd_name = low.split()[0]
        else:
            cmd_name = low

        if cmd_name in {"/start", "/help", BTN_HELP.lower(), "помощь", "help"}:
            await self._cmd_help()
        elif cmd_name in {"/stats", "stats", BTN_STATS.lower(), "статус", "📊 статус"}:
            await self._cmd_stats()
        elif cmd_name in {
            "/screenshot",
            "screenshot",
            BTN_SCREENSHOT.lower(),
            "скриншот",
            "📸 скриншот",
        }:
            await self._cmd_screenshot()
        elif cmd_name in {"/pause", "pause", BTN_PAUSE.lower(), "пауза", "⏸ пауза"}:
            await self._cmd_pause()
        elif cmd_name in {
            "/resume",
            "resume",
            BTN_RESUME.lower(),
            "продолжить",
            "▶️ продолжить",
        }:
            await self._cmd_resume()
        elif cmd_name in {"/stop", "stop", BTN_STOP.lower(), "стоп", "⏹ стоп"}:
            await self._cmd_stop()
        else:
            # Кнопки с эмодзи — точное сравнение
            if cmd == BTN_STATS:
                await self._cmd_stats()
            elif cmd == BTN_SCREENSHOT:
                await self._cmd_screenshot()
            elif cmd == BTN_PAUSE:
                await self._cmd_pause()
            elif cmd == BTN_RESUME:
                await self._cmd_resume()
            elif cmd == BTN_STOP:
                await self._cmd_stop()
            elif cmd == BTN_HELP:
                await self._cmd_help()
            else:
                await self._send_message(
                    await self._ensure_session(),
                    f"Неизвестная команда: {cmd}\nОтправьте /help",
                    reply_markup=self._main_keyboard(),
                )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_help(self) -> None:
        text = (
            "🐉 <b>DwarBot — удалённое управление</b>\n\n"
            "/stats — HP, золото, бой, задача, фарм\n"
            "/screenshot — скриншот игры\n"
            "/pause — пауза главного цикла\n"
            "/resume — снять паузу\n"
            "/stop — graceful shutdown (cookie + state)\n"
            "/help — это меню"
        )
        await self._send_message(
            await self._ensure_session(),
            text,
            parse_mode="HTML",
            reply_markup=self._main_keyboard(),
        )

    async def _cmd_stats(self) -> None:
        if self._stats_provider is not None:
            try:
                body = await self._stats_provider()
            except Exception as exc:
                logger.error("stats_provider: %s", exc, exc_info=True)
                body = f"Ошибка получения статуса: {exc}"
        else:
            body = self._fallback_stats()

        status = "⏸ PAUSED" if self.state.is_paused else "▶️ RUNNING"
        if self.state.should_stop:
            status = "⏹ STOPPING"

        text = (
            f"📊 <b>Статус бота</b> [{status}]\n"
            f"Задача: <code>{self.state.current_task}</code>\n\n"
            f"{body}"
        )
        if self.state.farm_summary:
            text += f"\n\n🌾 Фарм:\n{self.state.farm_summary}"
        if self.state.extra_status:
            text += f"\n\n{self.state.extra_status}"

        await self._send_message(
            await self._ensure_session(),
            text,
            parse_mode="HTML",
            reply_markup=self._main_keyboard(),
        )

    def _fallback_stats(self) -> str:
        return (
            "Провайдер статистики не привязан.\n"
            f"paused={self.state.is_paused} stop={self.state.should_stop}"
        )

    async def _cmd_screenshot(self) -> None:
        session = await self._ensure_session()
        path: Optional[Path] = None
        if self._screenshot_provider is not None:
            try:
                path = await self._screenshot_provider()
            except Exception as exc:
                logger.error("screenshot_provider: %s", exc, exc_info=True)
                await self._send_message(session, f"Ошибка скриншота: {exc}")
                return

        if path is None or not path.is_file():
            await self._send_message(session, "Не удалось сделать скриншот")
            return

        ok = await self._send_photo(
            session, path, caption="📸 Текущий экран DwarBot"
        )
        if not ok:
            await self._send_message(session, f"Файл создан, но отправка не удалась: {path}")

    async def _cmd_pause(self) -> None:
        self.state.pause()
        self.state.current_task = "paused"
        await self.send_alert("⏸ Бот поставлен на паузу (/resume чтобы продолжить)")

    async def _cmd_resume(self) -> None:
        self.state.resume()
        if self.state.current_task == "paused":
            self.state.current_task = "idle"
        await self.send_alert("▶️ Бот возобновил работу")

    async def _cmd_stop(self) -> None:
        self.state.request_stop()
        await self.send_alert(
            "⏹ Запрошена остановка. Выполняется graceful shutdown "
            "(сохранение cookie и state)..."
        )

    # ------------------------------------------------------------------
    # Telegram API helpers
    # ------------------------------------------------------------------

    def _main_keyboard(self) -> Dict[str, Any]:
        return {
            "keyboard": [
                [{"text": BTN_STATS}, {"text": BTN_SCREENSHOT}],
                [{"text": BTN_PAUSE}, {"text": BTN_RESUME}],
                [{"text": BTN_STOP}, {"text": BTN_HELP}],
            ],
            "resize_keyboard": True,
            "persistent": True,
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def _api_call(
        self, method: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        session = await self._ensure_session()
        async with session.post(f"{self._api}/{method}", json=payload or {}) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400 or not data.get("ok", True):
                logger.warning("API %s failed: %s %s", method, resp.status, data)
            return data if isinstance(data, dict) else {}

    async def _send_message(
        self,
        session: aiohttp.ClientSession,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        payload: Dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        async with session.post(f"{self._api}/sendMessage", json=payload) as resp:
            data = await resp.json(content_type=None)
            ok = bool(data.get("ok"))
            if not ok:
                logger.warning("sendMessage failed: %s", data)
            return ok

    async def _send_photo(
        self,
        session: aiohttp.ClientSession,
        path: Path,
        *,
        caption: str = "",
    ) -> bool:
        form = aiohttp.FormData()
        form.add_field("chat_id", self._chat_id)
        if caption:
            form.add_field("caption", caption[:1000])
        form.add_field(
            "photo",
            path.read_bytes(),
            filename=path.name,
            content_type="image/png",
        )
        async with session.post(f"{self._api}/sendPhoto", data=form) as resp:
            data = await resp.json(content_type=None)
            ok = bool(data.get("ok"))
            if not ok:
                logger.warning("sendPhoto failed: %s", data)
            return ok
