"""
Telethon event handlers: setup flow, admin commands, and AI chat replies.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from telethon import events
from telethon.errors import RPCError
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.types import User

from ai_engine import GeminiEngine, ProxyFormatError, parse_proxy_string
from database import Database, ERROR_LOG_FILE

logger = logging.getLogger(__name__)

SETUP_PROMPT = (
    "привет. это первичная настройка.\n"
    "отправь gemini api key и прокси в формате:\n"
    "`ключ|ip:port:user:pass`\n"
    "или двумя строками:\n"
    "1) api key\n"
    "2) ip:port:user:pass\n\n"
    "можно также написать /parse_proxy — подтяну публичные socks5 и проверю их."
)

CREDENTIALS_RE = re.compile(
    r"^(?P<api_key>AIza[0-9A-Za-z_\-]{20,})\s*[\|,]\s*(?P<proxy>.+)$",
    re.DOTALL,
)

PROFILE_COMMAND_RE = re.compile(
    r"(?:сделай\s+аватар|поставь\s+аватар|аватарк).*?(?:с|из|на\s+)?(?P<topic>.+)",
    re.IGNORECASE,
)
NICK_COMMAND_RE = re.compile(
    r"(?:поставь\s+ник|смени\s+ник|ник)\s+['\"«](?P<nick>[^'\"»]+)['\"»]",
    re.IGNORECASE,
)
BIO_COMMAND_RE = re.compile(
    r"(?:поставь\s+био|смени\s+био|био)\s+['\"«](?P<bio>[^'\"»]+)['\"»]",
    re.IGNORECASE,
)


class EventHandlers:
    """Registers Telethon handlers and orchestrates bot behavior."""

    def __init__(
        self,
        client,
        db: Database,
        ai: GeminiEngine,
        *,
        owner_user_id: Optional[int] = None,
    ) -> None:
        self.client = client
        self.db = db
        self.ai = ai
        self.owner_user_id = owner_user_id
        self.started_at = datetime.now(timezone.utc)
        self._registered = False

    def register(self) -> None:
        if self._registered:
            return

        self.client.add_event_handler(self.on_new_message, events.NewMessage(incoming=True))
        self._registered = True
        logger.info("Telethon handlers registered")

    async def bootstrap_owner(self) -> None:
        me = await self.client.get_me()
        if me is None:
            raise RuntimeError("Cannot resolve current Telegram user")

        stored_owner = await self.db.get_owner_user_id()
        if stored_owner is None:
            await self.db.set_owner_user_id(me.id)
            self.owner_user_id = me.id
        else:
            self.owner_user_id = stored_owner

        if not await self.db.is_initialized():
            await self._send_setup_prompt()

        await self._backup_profile_if_needed(me)

    async def _send_setup_prompt(self) -> None:
        try:
            await self.client.send_message("me", SETUP_PROMPT)
        except RPCError as exc:
            await self.db.log_error(exc, error_type="SetupPrompt")

    async def _backup_profile_if_needed(self, me: User) -> None:
        backup = await self.db.get_profile_backup()
        if backup:
            return
        await self.db.save_profile_backup(me.first_name, me.last_name, me.about)

    def _is_owner(self, sender_id: Optional[int]) -> bool:
        return sender_id is not None and sender_id == self.owner_user_id

    async def on_new_message(self, event: events.NewMessage.Event) -> None:
        try:
            await self._handle_message(event)
        except Exception as exc:
            await self.db.log_error(
                exc,
                error_type="MessageHandler",
                context={"chat_id": event.chat_id, "message_id": event.message.id},
            )

    async def _handle_message(self, event: events.NewMessage.Event) -> None:
        text = (event.raw_text or "").strip()
        if not text:
            return

        sender = await event.get_sender()
        sender_id = sender.id if sender else None
        chat_id = event.chat_id
        is_saved_messages = bool(event.is_private and chat_id == self.owner_user_id)

        if is_saved_messages and self._is_owner(sender_id):
            if text.startswith("/"):
                handled = await self._handle_admin_command(event, text)
                if handled:
                    return
            if not await self.db.is_initialized():
                await self._handle_setup_message(event, text)
                return

        if not await self.db.is_initialized():
            return

        if await self.db.is_blacklisted(chat_id):
            return

        if self._is_owner(sender_id) and await self._handle_profile_command(event, text):
            return

        if event.out:
            return

        await self._handle_ai_reply(event, text, sender)

    async def _handle_setup_message(self, event: events.NewMessage.Event, text: str) -> None:
        if text.strip().lower() == "/parse_proxy":
            await self._cmd_parse_proxy(event)
            return

        api_key, proxy_raw = self._extract_credentials(text)
        if not api_key or not proxy_raw:
            await event.reply(
                "не понял формат. пример:\n"
                "`AIza...|127.0.0.1:1080:user:pass`"
            )
            return

        await event.reply("проверяю ключ и прокси...")
        try:
            gemini_health, proxy_health = await self.ai.validate_and_activate(api_key, proxy_raw)
            await self.ai.start()
            await self.db.mark_initialized()
            await event.reply(
                "готово.\n"
                f"gemini: {gemini_health.message} ({gemini_health.latency_ms:.0f} ms)\n"
                f"proxy: {proxy_health.proxy_url} ({proxy_health.latency_ms:.0f} ms)"
            )
        except ProxyFormatError as exc:
            await event.reply(f"прокси кривой: {exc}")
        except Exception as exc:
            await self.db.log_error(exc, error_type="SetupValidation")
            await event.reply(f"не вышло: {exc}")

    @staticmethod
    def _extract_credentials(text: str) -> tuple[Optional[str], Optional[str]]:
        match = CREDENTIALS_RE.match(text.strip())
        if match:
            return match.group("api_key"), match.group("proxy").strip()

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 2 and lines[0].startswith("AIza"):
            return lines[0], lines[1]
        return None, None

    async def _handle_admin_command(self, event: events.NewMessage.Event, text: str) -> bool:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "/logs":
            await self._cmd_logs(event)
            return True
        if command == "/status":
            await self._cmd_status(event)
            return True
        if command == "/set_prompt":
            await self._cmd_set_prompt(event, arg)
            return True
        if command == "/blacklist_add":
            await self._cmd_blacklist_add(event, arg)
            return True
        if command == "/blacklist_rm":
            await self._cmd_blacklist_rm(event, arg)
            return True
        if command == "/restore_profile":
            await self._cmd_restore_profile(event)
            return True
        if command == "/parse_proxy":
            await self._cmd_parse_proxy(event)
            return True
        return False

    async def _cmd_logs(self, event: events.NewMessage.Event) -> None:
        errors = await self.db.get_recent_errors(limit=50)
        if not errors:
            await event.reply("логов ошибок пока нет")
            return

        lines = []
        for item in reversed(errors):
            lines.append(
                f"[{item.timestamp}] {item.error_type}\n"
                f"{item.message}\n"
                f"context: {item.context or '-'}\n"
            )
        payload = "\n".join(lines)
        if len(payload) <= 3500:
            await event.reply(f"```\n{payload}\n```")
            return

        buffer = BytesIO(payload.encode("utf-8"))
        buffer.name = "recent_errors.txt"
        await event.reply("последние 50 ошибок:", file=buffer)
        if ERROR_LOG_FILE.exists():
            await event.reply("полный файл:", file=str(ERROR_LOG_FILE))

    async def _cmd_status(self, event: events.NewMessage.Event) -> None:
        uptime = datetime.now(timezone.utc) - self.started_at
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)

        gemini_health = await self.ai.test_gemini()
        active_proxy = await self.db.get_active_proxy()
        proxy_text = "нет"
        if active_proxy:
            proxy_text = (
                f"{active_proxy.proxy_url} | fail={active_proxy.fail_count} | "
                f"latency={active_proxy.latency_ms or '?'} ms"
            )

        text = (
            f"uptime: {hours}h {minutes}m {seconds}s\n"
            f"initialized: {await self.db.is_initialized()}\n"
            f"gemini: {'ok' if gemini_health.ok else 'fail'} "
            f"({gemini_health.latency_ms:.0f} ms) — {gemini_health.message}\n"
            f"proxy: {proxy_text}\n"
            f"blacklist: {len(await self.db.list_blacklisted())} chats"
        )
        await event.reply(text)

    async def _cmd_set_prompt(self, event: events.NewMessage.Event, prompt: str) -> None:
        if not prompt:
            current = await self.db.get_system_prompt()
            await event.reply(f"текущий промпт:\n{current}")
            return
        await self.db.set_system_prompt(prompt)
        await event.reply("промпт обновлён")

    async def _cmd_blacklist_add(self, event: events.NewMessage.Event, arg: str) -> None:
        if not arg.lstrip("-").isdigit():
            await event.reply("использование: /blacklist_add <chat_id>")
            return
        chat_id = int(arg)
        await self.db.blacklist_add(chat_id)
        await event.reply(f"chat {chat_id} в blacklist")

    async def _cmd_blacklist_rm(self, event: events.NewMessage.Event, arg: str) -> None:
        if not arg.lstrip("-").isdigit():
            await event.reply("использование: /blacklist_rm <chat_id>")
            return
        chat_id = int(arg)
        removed = await self.db.blacklist_remove(chat_id)
        await event.reply("убрано" if removed else "не было в blacklist")

    async def _cmd_restore_profile(self, event: events.NewMessage.Event) -> None:
        backup = await self.db.get_profile_backup()
        if not backup:
            await event.reply("backup профиля не найден")
            return
        try:
            await self.client(
                UpdateProfileRequest(
                    first_name=backup["first_name"] or "",
                    last_name=backup["last_name"] or "",
                    about=backup["about"] or "",
                )
            )
            await event.reply("профиль восстановлен")
        except RPCError as exc:
            await self.db.log_error(exc, error_type="RestoreProfile")
            await event.reply(f"ошибка: {exc}")

    async def _cmd_parse_proxy(self, event: events.NewMessage.Event) -> None:
        await event.reply("парсю публичные socks5, это может занять минуту...")
        try:
            stored = await self.ai.parse_and_store_public_proxies(limit=40)
            api_key = await self.db.get_gemini_api_key()
            if not api_key:
                await event.reply(f"сохранено прокси: {len(stored)}. добавь api key для проверки.")
                return

            working = await self.ai.ensure_working_proxy(api_key)
            if working:
                await event.reply(
                    f"готово. сохранено: {len(stored)}, рабочий: {working.proxy_url}"
                )
            else:
                await event.reply(
                    f"сохранено: {len(stored)}, но рабочий прокси не найден"
                )
        except Exception as exc:
            await self.db.log_error(exc, error_type="ParseProxy")
            await event.reply(f"ошибка парсинга: {exc}")

    async def _handle_profile_command(self, event: events.NewMessage.Event, text: str) -> bool:
        nick_match = NICK_COMMAND_RE.search(text)
        if nick_match:
            nick = nick_match.group("nick").strip()
            try:
                await self.client(UpdateProfileRequest(first_name=nick[:64]))
                await event.reply("ник обновлён")
            except RPCError as exc:
                await self.db.log_error(exc, error_type="ProfileNick")
                await event.reply(f"не смог: {exc}")
            return True

        bio_match = BIO_COMMAND_RE.search(text)
        if bio_match:
            bio = bio_match.group("bio").strip()
            try:
                await self.client(UpdateProfileRequest(about=bio[:70]))
                await event.reply("био обновлено")
            except RPCError as exc:
                await self.db.log_error(exc, error_type="ProfileBio")
                await event.reply(f"не смог: {exc}")
            return True

        avatar_match = PROFILE_COMMAND_RE.search(text)
        if avatar_match:
            topic = avatar_match.group("topic").strip()
            await event.reply("аватар через gemini image пока не подключён в core-модуле")
            logger.info("Avatar command received: %s", topic)
            return True

        return False

    async def _handle_ai_reply(
        self,
        event: events.NewMessage.Event,
        text: str,
        sender,
    ) -> None:
        chat_id = event.chat_id
        author = self._display_name(sender)

        await self.db.append_chat_message(
            chat_id,
            role="user",
            content=text,
            message_id=event.message.id,
            author=author,
        )

        context = await self._fetch_live_context(event, limit=25)
        for item in context:
            if item.get("message_id") == event.message.id:
                continue
            await self.db.append_chat_message(
                chat_id,
                role=item["role"],
                content=item["content"],
                message_id=item.get("message_id"),
                author=item.get("author") or "",
            )

        db_context = await self.db.get_chat_context(chat_id, limit=25)
        system_prompt = await self.db.get_system_prompt()

        try:
            reply = await self.ai.generate_reply(
                system_prompt=system_prompt,
                context_messages=db_context,
                user_message=text,
            )
        except Exception as exc:
            await self.db.log_error(
                exc,
                error_type="AIReply",
                context={"chat_id": chat_id},
            )
            return

        if not reply:
            return

        delay = self._typing_delay_seconds(reply)
        try:
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(delay)
            sent = await event.reply(reply)
        except RPCError as exc:
            await self.db.log_error(exc, error_type="SendReply", context={"chat_id": chat_id})
            return

        await self.db.append_chat_message(
            chat_id,
            role="assistant",
            content=reply,
            message_id=getattr(sent, "id", None),
            author="я",
        )

    async def _fetch_live_context(
        self,
        event: events.NewMessage.Event,
        *,
        limit: int,
    ) -> list[dict]:
        me = await self.client.get_me()
        my_id = me.id if me else None
        items: list[dict] = []

        try:
            async for message in self.client.iter_messages(event.chat_id, limit=limit):
                if not message.message:
                    continue
                role = "assistant" if message.out or message.sender_id == my_id else "user"
                sender = await message.get_sender()
                items.append(
                    {
                        "role": role,
                        "content": message.message,
                        "message_id": message.id,
                        "author": self._display_name(sender),
                    }
                )
        except RPCError as exc:
            await self.db.log_error(exc, error_type="FetchContext", context={"chat_id": event.chat_id})

        items.reverse()
        return items

    @staticmethod
    def _display_name(entity) -> str:
        if entity is None:
            return "unknown"
        for attr in ("first_name", "title", "username"):
            value = getattr(entity, attr, None)
            if value:
                return str(value)
        return "unknown"

    @staticmethod
    def _typing_delay_seconds(text: str, chars_per_second: float = 17.5) -> float:
        length = max(len(text), 1)
        base = length / chars_per_second
        return min(max(base, 0.8), 12.0)
