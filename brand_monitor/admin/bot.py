"""
Admin control panel on aiogram 3.x — live monitoring, analytics, kill switch.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from brand_monitor.config import Settings
from brand_monitor.core.userbot_manager import UserbotManager
from brand_monitor.database.repository import Database

logger = logging.getLogger(__name__)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

STATUS_EMOJI = {
    "active": "🟢 Active",
    "cooldown": "🟡 Cooldown",
    "banned": "🔴 Banned",
    "scheduled_off": "⚪️ Scheduled Off",
    "paused": "⏸ Paused",
    "starting": "🔵 Starting",
}


class AuthStates(StatesGroup):
    waiting_phone = State()
    waiting_api = State()
    waiting_code = State()
    waiting_password = State()
    waiting_proxy = State()


class KBStates(StatesGroup):
    waiting_title = State()
    waiting_template = State()
    waiting_category = State()


class KeywordStates(StatesGroup):
    waiting_keyword = State()
    waiting_category = State()


class ExportStates(StatesGroup):
    waiting_period = State()


class AdminAccessMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
        if self.settings.admin_ids and user_id not in self.settings.admin_ids:
            if isinstance(event, Message):
                await event.answer("Access denied.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Access denied.", show_alert=True)
            return None
        return await handler(event, data)


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📡 Live Status", callback_data="panel:live"),
                InlineKeyboardButton(text="📊 Stats", callback_data="panel:stats"),
            ],
            [
                InlineKeyboardButton(text="📥 Export CSV", callback_data="panel:export"),
                InlineKeyboardButton(text="🛑 Emergency Stop", callback_data="panel:kill"),
            ],
            [
                InlineKeyboardButton(text="▶️ Resume All", callback_data="panel:resume"),
            ],
        ]
    )


def build_admin_dispatcher(
    db: Database,
    manager: UserbotManager,
    settings: Settings,
) -> Dispatcher:
    router = Router()
    pending_clients: dict[int, TelegramClient] = {}

    async def finalize_auth(message: Message, state: FSMContext) -> None:
        assert message.from_user is not None
        data = await state.get_data()
        client = pending_clients.get(message.from_user.id)
        if client is None:
            await state.clear()
            await message.answer("Auth session expired. /auth_agent again.")
            return

        session_string = StringSession.save(client.session)
        me = await client.get_me()
        agent_id = int(data["agent_id"])
        await db.update_agent_session(agent_id, session_string)
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE agents SET display_name = ? WHERE id = ?",
                (getattr(me, "username", None) or str(me.id), agent_id),
            )
        await client.disconnect()
        pending_clients.pop(message.from_user.id, None)
        await state.clear()
        agent = await db.ensure_fingerprint(agent_id)
        started = await manager.start_agent(agent_id)
        await message.answer(
            f"Agent #{agent_id} authorized "
            f"({agent.device_model} / {agent.system_version}) and "
            f"{'started' if started else 'saved (start failed — check logs)'}."
        )

    @router.message(Command("start", "help", "panel"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "<b>Brand Monitor — Admin Panel</b>\n\n"
            "/panel — control buttons\n"
            "/live — live operator status\n"
            "/stats — conversion table\n"
            "/export [days] — CSV of replies\n"
            "/kill — emergency stop\n"
            "/resume — resume all\n"
            "/status /agents — pool / DB list\n"
            "/auth_agent — authorize agent\n"
            "/set_schedule &lt;id&gt; HH:MM-HH:MM\n"
            "/start_agent /stop_agent &lt;id&gt;\n"
            "/keywords /add_keyword /del_keyword\n"
            "/stopwords /add_stopword /del_stopword\n"
            "/kb /add_kb /del_kb\n",
            parse_mode="HTML",
            reply_markup=_panel_keyboard(),
        )

    async def _render_live() -> str:
        rows = await manager.full_status()
        if not rows:
            return "No agents in database."
        kill = " ⚠ KILL SWITCH ON" if manager.is_paused else ""
        lines = [f"<b>Live Monitoring{kill}</b>:"]
        for item in rows:
            badge = STATUS_EMOJI.get(item["live_status"], item["live_status"])
            lines.append(
                f"{badge} <b>#{item['agent_id']}</b> {item['phone']}\n"
                f"  device={item.get('device_model') or '—'} | "
                f"window={item['work_window']} | "
                f"rate={item['replies_hour']}/h {item['replies_day']}/d"
                + (
                    f"\n  cooldown_until={item['cooldown_until']}"
                    if item.get("cooldown_until")
                    else ""
                )
            )
        return "\n".join(lines)

    async def _render_stats() -> str:
        by_chat = await db.stats_by_chat(days=7)
        by_hour = await db.stats_by_hour(days=7)
        lines = ["<b>Stats (7 days)</b>", "", "<b>By chat</b>:"]
        if not by_chat:
            lines.append("— no replies yet")
        else:
            for row in by_chat[:15]:
                bar = "█" * min(20, int(row["replies"]))
                lines.append(f"• <code>{row['chat_id']}</code> {row['replies']} {bar}")
        lines.append("")
        lines.append("<b>By hour (UTC)</b>:")
        if not by_hour:
            lines.append("— empty")
        else:
            for row in by_hour:
                bar = "▓" * min(20, int(row["replies"]))
                lines.append(f"• {row['hour']}:00 — {row['replies']} {bar}")
        return "\n".join(lines)

    @router.message(Command("live"))
    async def cmd_live(message: Message) -> None:
        await message.answer(await _render_live(), parse_mode="HTML")

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        await message.answer(await _render_stats(), parse_mode="HTML")

    @router.message(Command("export"))
    async def cmd_export(message: Message, command: CommandObject) -> None:
        days = 7
        if command.args and command.args.strip().isdigit():
            days = max(1, min(90, int(command.args.strip())))
        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=days)
        csv_data = await db.export_interactions_csv(
            date_from.isoformat(),
            date_to.isoformat(),
        )
        doc = BufferedInputFile(
            csv_data.encode("utf-8"),
            filename=f"interactions_{days}d.csv",
        )
        await message.answer_document(doc, caption=f"Export: last {days} day(s)")

    @router.message(Command("kill"))
    async def cmd_kill(message: Message) -> None:
        n = await manager.emergency_stop()
        await message.answer(
            f"🛑 <b>Emergency Stop</b>\nPaused agents: {n}",
            parse_mode="HTML",
            reply_markup=_panel_keyboard(),
        )

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        n = await manager.resume_all()
        await message.answer(f"▶️ Resumed agents from pause: {n}")

    @router.callback_query(F.data.startswith("panel:"))
    async def on_panel(callback: CallbackQuery) -> None:
        assert callback.data is not None
        action = callback.data.split(":", 1)[1]
        if action == "live":
            await callback.message.answer(await _render_live(), parse_mode="HTML")  # type: ignore[union-attr]
        elif action == "stats":
            await callback.message.answer(await _render_stats(), parse_mode="HTML")  # type: ignore[union-attr]
        elif action == "export":
            date_to = datetime.now(timezone.utc)
            date_from = date_to - timedelta(days=7)
            csv_data = await db.export_interactions_csv(
                date_from.isoformat(), date_to.isoformat()
            )
            doc = BufferedInputFile(csv_data.encode("utf-8"), filename="interactions_7d.csv")
            await callback.message.answer_document(doc, caption="Export: last 7 days")  # type: ignore[union-attr]
        elif action == "kill":
            n = await manager.emergency_stop()
            await callback.message.answer(  # type: ignore[union-attr]
                f"🛑 Emergency Stop — paused: {n}",
                reply_markup=_panel_keyboard(),
            )
        elif action == "resume":
            n = await manager.resume_all()
            await callback.message.answer(f"▶️ Resumed: {n}")  # type: ignore[union-attr]
        await callback.answer()

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await message.answer(await _render_live(), parse_mode="HTML")

    @router.message(Command("agents"))
    async def cmd_agents(message: Message) -> None:
        agents = await db.list_agents()
        if not agents:
            await message.answer("No agents in database.")
            return
        lines = ["<b>Agents</b>:"]
        for a in agents:
            lines.append(
                f"• #{a.id} {a.phone} [{a.status}] "
                f"{a.work_window_start}-{a.work_window_end}\n"
                f"  fp={a.device_model or '—'} / {a.system_version or '—'}"
                + (f"\n  err: {a.last_error}" if a.last_error else "")
            )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("auth_agent"))
    async def cmd_auth_agent(message: Message, state: FSMContext) -> None:
        await state.set_state(AuthStates.waiting_phone)
        await message.answer(
            "Send phone number in international format (e.g. +79001234567):"
        )

    @router.message(AuthStates.waiting_phone)
    async def auth_phone(message: Message, state: FSMContext) -> None:
        phone = (message.text or "").strip()
        if not phone.startswith("+"):
            await message.answer("Phone must start with +.")
            return
        await state.update_data(phone=phone)
        await state.set_state(AuthStates.waiting_api)
        await message.answer("Send <code>api_id:api_hash</code>", parse_mode="HTML")

    @router.message(AuthStates.waiting_api)
    async def auth_api(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if ":" not in raw:
            await message.answer("Format: api_id:api_hash")
            return
        api_id_s, api_hash = raw.split(":", 1)
        if not api_id_s.isdigit():
            await message.answer("api_id must be integer.")
            return
        await state.update_data(api_id=int(api_id_s), api_hash=api_hash.strip())
        await state.set_state(AuthStates.waiting_proxy)
        await message.answer(
            "Send proxy as <code>type:host:port:user:pass</code> or <code>none</code>",
            parse_mode="HTML",
        )

    @router.message(AuthStates.waiting_proxy)
    async def auth_proxy(message: Message, state: FSMContext) -> None:
        assert message.from_user is not None
        raw = (message.text or "").strip()
        proxy_kwargs: dict[str, Optional[str | int]] = {
            "proxy_type": None,
            "proxy_host": None,
            "proxy_port": None,
            "proxy_username": None,
            "proxy_password": None,
        }
        if raw.lower() != "none":
            parts = raw.split(":")
            if len(parts) < 3:
                await message.answer("Invalid proxy format.")
                return
            proxy_kwargs["proxy_type"] = parts[0].lower()
            proxy_kwargs["proxy_host"] = parts[1]
            proxy_kwargs["proxy_port"] = int(parts[2])
            if len(parts) >= 5:
                proxy_kwargs["proxy_username"] = parts[3]
                proxy_kwargs["proxy_password"] = ":".join(parts[4:])

        data = await state.get_data()
        agent_id = await db.upsert_agent(
            phone=data["phone"],
            api_id=data["api_id"],
            api_hash=data["api_hash"],
            status="inactive",
            proxy_type=proxy_kwargs["proxy_type"],  # type: ignore[arg-type]
            proxy_host=proxy_kwargs["proxy_host"],  # type: ignore[arg-type]
            proxy_port=proxy_kwargs["proxy_port"],  # type: ignore[arg-type]
            proxy_username=proxy_kwargs["proxy_username"],  # type: ignore[arg-type]
            proxy_password=proxy_kwargs["proxy_password"],  # type: ignore[arg-type]
        )
        await state.update_data(agent_id=agent_id)
        agent = await db.ensure_fingerprint(agent_id)
        assert agent is not None

        client = TelegramClient(
            StringSession(),
            data["api_id"],
            data["api_hash"],
            proxy=agent.proxy_tuple,
            device_model=agent.device_model,
            system_version=agent.system_version,
            app_version=agent.app_version,
            lang_code=agent.lang_code or "ru",
        )
        await client.connect()
        result = await client.send_code_request(data["phone"])
        pending_clients[message.from_user.id] = client
        await state.update_data(phone_code_hash=result.phone_code_hash)
        await state.set_state(AuthStates.waiting_code)
        await message.answer(
            f"OTP sent. Fingerprint locked: <code>{agent.device_model}</code>\n"
            f"Agent draft id=#{agent_id}",
            parse_mode="HTML",
        )

    @router.message(AuthStates.waiting_code)
    async def auth_code(message: Message, state: FSMContext) -> None:
        assert message.from_user is not None
        code = (message.text or "").strip().replace(" ", "")
        data = await state.get_data()
        client = pending_clients.get(message.from_user.id)
        if client is None:
            await state.clear()
            await message.answer("Auth session expired. /auth_agent again.")
            return
        try:
            await client.sign_in(
                phone=data["phone"],
                code=code,
                phone_code_hash=data["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            await state.set_state(AuthStates.waiting_password)
            await message.answer("2FA password required. Send it now:")
            return
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Sign-in failed: {exc}")
            return
        await finalize_auth(message, state)

    @router.message(AuthStates.waiting_password)
    async def auth_password(message: Message, state: FSMContext) -> None:
        assert message.from_user is not None
        client = pending_clients.get(message.from_user.id)
        if client is None:
            await state.clear()
            await message.answer("Auth session expired. /auth_agent again.")
            return
        try:
            await client.sign_in(password=(message.text or "").strip())
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"2FA failed: {exc}")
            return
        await finalize_auth(message, state)

    @router.message(Command("set_schedule"))
    async def cmd_set_schedule(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip().split()
        if len(args) != 2 or "-" not in args[1] or not args[0].isdigit():
            await message.answer("Usage: /set_schedule <agent_id> <HH:MM-HH:MM>")
            return
        start, end = args[1].split("-", 1)
        if not TIME_RE.match(start) or not TIME_RE.match(end):
            await message.answer("Invalid time. Use HH:MM.")
            return
        await db.update_agent_schedule(int(args[0]), start, end)
        await message.answer(f"Agent #{args[0]} schedule set to {start}-{end}")

    @router.message(Command("start_agent"))
    async def cmd_start_agent(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /start_agent <id>")
            return
        ok = await manager.start_agent(int(command.args.strip()))
        await message.answer("Started." if ok else "Failed — see logs / agents list.")

    @router.message(Command("stop_agent"))
    async def cmd_stop_agent(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /stop_agent <id>")
            return
        agent_id = int(command.args.strip())
        await manager.stop_agent(agent_id)
        await db.update_agent_status(agent_id, "inactive", last_error="Stopped by admin")
        await message.answer(f"Agent #{agent_id} stopped.")

    @router.message(Command("keywords"))
    async def cmd_keywords(message: Message) -> None:
        rows = await db.list_keywords()
        if not rows:
            await message.answer("No keywords.")
            return
        lines = [
            f"• #{k.id} [{k.category}] {'ON' if k.is_active else 'OFF'} — {k.keyword}"
            for k in rows
        ]
        await message.answer("<b>Keywords</b>:\n" + "\n".join(lines), parse_mode="HTML")

    @router.message(Command("add_keyword"))
    async def cmd_add_keyword(message: Message, state: FSMContext) -> None:
        await state.set_state(KeywordStates.waiting_keyword)
        await message.answer("Send keyword text:")

    @router.message(KeywordStates.waiting_keyword)
    async def kw_text(message: Message, state: FSMContext) -> None:
        await state.update_data(keyword=(message.text or "").strip())
        await state.set_state(KeywordStates.waiting_category)
        await message.answer("Send category (e.g. support):")

    @router.message(KeywordStates.waiting_category)
    async def kw_category(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        kid = await db.add_keyword(
            data["keyword"],
            category=(message.text or "general").strip(),
        )
        await state.clear()
        await manager.reload_filters()
        await message.answer(f"Keyword #{kid} added.")

    @router.message(Command("del_keyword"))
    async def cmd_del_keyword(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /del_keyword <id>")
            return
        await db.delete_keyword(int(command.args.strip()))
        await manager.reload_filters()
        await message.answer("Deleted.")

    @router.message(Command("reload_keywords"))
    async def cmd_reload_keywords(message: Message) -> None:
        await manager.reload_filters()
        await message.answer("Filters reloaded.")

    @router.message(Command("stopwords"))
    async def cmd_stopwords(message: Message) -> None:
        rows = await db.list_stop_words()
        if not rows:
            await message.answer("No stop-words.")
            return
        lines = [
            f"• #{s.id} {'ON' if s.is_active else 'OFF'} — {s.word}" for s in rows
        ]
        await message.answer("<b>Stop-words</b>:\n" + "\n".join(lines), parse_mode="HTML")

    @router.message(Command("add_stopword"))
    async def cmd_add_stopword(message: Message, command: CommandObject) -> None:
        word = (command.args or "").strip()
        if not word:
            await message.answer("Usage: /add_stopword <word>")
            return
        sid = await db.add_stop_word(word)
        await manager.reload_filters()
        await message.answer(f"Stop-word #{sid} added.")

    @router.message(Command("del_stopword"))
    async def cmd_del_stopword(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /del_stopword <id>")
            return
        await db.delete_stop_word(int(command.args.strip()))
        await manager.reload_filters()
        await message.answer("Deleted.")

    @router.message(Command("kb"))
    async def cmd_kb(message: Message) -> None:
        rows = await db.list_knowledge()
        if not rows:
            await message.answer("Knowledge base is empty.")
            return
        lines = []
        for e in rows:
            preview = e.response_template[:80].replace("<", "&lt;")
            lines.append(
                f"• #{e.id} [{e.category}] {'ON' if e.is_active else 'OFF'} "
                f"<b>{e.title}</b>\n  {preview}…"
            )
        await message.answer(
            "<b>Knowledge base</b>:\n" + "\n".join(lines),
            parse_mode="HTML",
        )

    @router.message(Command("add_kb"))
    async def cmd_add_kb(message: Message, state: FSMContext) -> None:
        await state.set_state(KBStates.waiting_title)
        await message.answer("Send title:")

    @router.message(KBStates.waiting_title)
    async def kb_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=(message.text or "").strip())
        await state.set_state(KBStates.waiting_category)
        await message.answer("Send category:")

    @router.message(KBStates.waiting_category)
    async def kb_category(message: Message, state: FSMContext) -> None:
        await state.update_data(category=(message.text or "general").strip())
        await state.set_state(KBStates.waiting_template)
        await message.answer(
            "Send nested spintax template.\n"
            "<code>{Hi|Hello|{Hey|Yo}}!</code>",
            parse_mode="HTML",
        )

    @router.message(KBStates.waiting_template)
    async def kb_template(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        eid = await db.add_knowledge(
            title=data["title"],
            response_template=(message.text or "").strip(),
            category=data["category"],
        )
        await state.clear()
        await message.answer(f"Knowledge entry #{eid} created.")

    @router.message(Command("del_kb"))
    async def cmd_del_kb(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /del_kb <id>")
            return
        await db.delete_knowledge(int(command.args.strip()))
        await message.answer("Deleted.")

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AdminAccessMiddleware(settings))
    dp.callback_query.middleware(AdminAccessMiddleware(settings))
    dp.include_router(router)
    return dp


async def make_admin_notifier(bot: Bot, settings: Settings):
    async def notify(agent_id: int, phone: str, reason: str) -> None:
        text = (
            f"⚠️ <b>Agent alert</b>\n"
            f"id=#{agent_id}\n"
            f"phone=<code>{phone}</code>\n"
            f"{reason}"
        )
        targets = settings.admin_ids
        if not targets:
            logger.warning("No ADMIN_IDS configured; alert dropped: %s", reason)
            return
        for admin_id in targets:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                logger.exception("Failed to notify admin %s", admin_id)

    return notify
