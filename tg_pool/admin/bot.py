"""
aiogram 3.x admin panel for the userbot account pool.

Capabilities
------------
* Inline account list with live statuses
* Add account (StringSession + optional proxy bind)
* Import account from Telegram Desktop TData ZIP (opentele)
* Add proxy
* Enqueue SpamBot / ping tasks
* AlertService integration (bound from main)
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, TelegramObject

from tg_pool.admin.keyboards import (
    account_actions_kb,
    accounts_kb,
    main_menu_kb,
    tdata_success_kb,
)
from tg_pool.clients.tdata_converter import (
    TDataConversionError,
    cleanup_tree,
    convert_tdata_zip,
)
from tg_pool.config import Settings
from tg_pool.db.models import AccountStatus, Proxy, ProxyProtocol
from tg_pool.db.session import session_scope
from tg_pool.queue.broker import PoolTask, RedisTaskBroker
from tg_pool.services.account_service import AccountService

logger = logging.getLogger(__name__)

PROXY_RE = re.compile(
    r"^(?P<proto>socks5|http)://(?:(?P<user>[^:]+):(?P<password>[^@]+)@)?"
    r"(?P<ip>[^:]+):(?P<port>\d+)$",
    re.IGNORECASE,
)


class AddAccountStates(StatesGroup):
    phone = State()
    session = State()
    api = State()
    proxy = State()


class AddProxyStates(StatesGroup):
    raw = State()


class ImportTDataStates(StatesGroup):
    proxy = State()
    passcode = State()
    archive = State()


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


def build_dispatcher(
    settings: Settings,
    broker: RedisTaskBroker,
) -> Dispatcher:
    router = Router()

    @router.message(Command("start", "help", "menu"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "<b>TG Account Pool — Admin</b>\n"
            "Управление юзерботами, прокси и очередью задач.\n"
            "Импорт: 📦 <b>Import TData ZIP</b> или /import_tdata",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )

    @router.callback_query(F.data == "menu:home")
    async def cb_home(callback: CallbackQuery) -> None:
        await callback.message.edit_text(  # type: ignore[union-attr]
            "<b>TG Account Pool — Admin</b>",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:accounts")
    async def cb_accounts(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            accounts = list(await AccountService(session).list_accounts())
        if not accounts:
            await callback.message.edit_text(  # type: ignore[union-attr]
                "Нет аккаунтов. Добавьте через ➕ Add account.",
                reply_markup=main_menu_kb(),
            )
        else:
            await callback.message.edit_text(  # type: ignore[union-attr]
                "<b>Accounts</b> — выберите для действий:",
                parse_mode="HTML",
                reply_markup=accounts_kb(accounts),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("acc:"))
    async def cb_account_detail(callback: CallbackQuery) -> None:
        assert callback.data is not None
        account_id = int(callback.data.split(":")[1])
        async with session_scope() as session:
            acc = await AccountService(session).get_account(account_id)
        if acc is None:
            await callback.answer("Not found", show_alert=True)
            return
        proxy_info = (
            f"{acc.proxy.protocol.value}://{acc.proxy.ip}:{acc.proxy.port}"
            if acc.proxy
            else "—"
        )
        text = (
            f"<b>Account #{acc.id}</b>\n"
            f"phone: <code>{acc.phone_number}</code>\n"
            f"status: <b>{acc.status.value}</b>\n"
            f"spambot: {'YES' if acc.is_spambot_restricted else 'no'}\n"
            f"actions today: {acc.total_actions_today}\n"
            f"flood_until: {acc.flood_until or '—'}\n"
            f"device: <code>{acc.device_model}</code> / {acc.system_version}\n"
            f"proxy: <code>{proxy_info}</code>\n"
            f"error: {acc.last_error or '—'}"
        )
        await callback.message.edit_text(  # type: ignore[union-attr]
            text,
            parse_mode="HTML",
            reply_markup=account_actions_kb(account_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("accact:"))
    async def cb_account_action(callback: CallbackQuery) -> None:
        assert callback.data is not None
        _, account_id_s, action = callback.data.split(":", 2)
        account_id = int(account_id_s)

        if action == "activate":
            async with session_scope() as session:
                await AccountService(session).set_status(
                    account_id, AccountStatus.active, last_error=None, flood_until=None
                )
            await callback.answer("Activated")
        elif action == "pause":
            async with session_scope() as session:
                await AccountService(session).set_status(
                    account_id, AccountStatus.paused
                )
            await callback.answer("Paused")
        elif action == "spambot":
            await broker.enqueue(
                PoolTask(kind="spambot_check", account_id=account_id)
            )
            await callback.answer("SpamBot check queued")
        elif action == "ping":
            await broker.enqueue(PoolTask(kind="ping_me", account_id=account_id))
            await callback.answer("Ping queued")
        else:
            await callback.answer("Unknown action", show_alert=True)
            return

        # Refresh detail
        callback.data = f"acc:{account_id}"
        await cb_account_detail(callback)

    @router.callback_query(F.data == "menu:spambot_all")
    async def cb_spambot_all(callback: CallbackQuery) -> None:
        async with session_scope() as session:
            accounts = await AccountService(session).list_accounts()
        n = 0
        for acc in accounts:
            if acc.session_string and acc.status != AccountStatus.banned:
                await broker.enqueue(
                    PoolTask(kind="spambot_check", account_id=acc.id)
                )
                n += 1
        await callback.answer(f"Queued {n} SpamBot checks", show_alert=True)

    # ---- add account FSM -------------------------------------------------
    @router.callback_query(F.data == "menu:add_account")
    async def cb_add_account(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddAccountStates.phone)
        await callback.message.answer(  # type: ignore[union-attr]
            "Номер телефона в международном формате (например <code>+79001234567</code>):",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(AddAccountStates.phone)
    async def add_phone(message: Message, state: FSMContext) -> None:
        phone = (message.text or "").strip()
        if not phone.startswith("+"):
            await message.answer("Номер должен начинаться с +")
            return
        await state.update_data(phone=phone)
        await state.set_state(AddAccountStates.session)
        await message.answer("Пришлите <b>StringSession</b> (Telethon):", parse_mode="HTML")

    @router.message(AddAccountStates.session)
    async def add_session(message: Message, state: FSMContext) -> None:
        session_string = (message.text or "").strip()
        if len(session_string) < 20:
            await message.answer("Похоже, сессия слишком короткая.")
            return
        await state.update_data(session_string=session_string)
        await state.set_state(AddAccountStates.api)
        await message.answer(
            "Отправьте <code>api_id:api_hash</code> "
            "(или <code>default</code> — взять из ENV):",
            parse_mode="HTML",
        )

    @router.message(AddAccountStates.api)
    async def add_api(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if raw.lower() == "default":
            if not settings.telegram_api_id or not settings.telegram_api_hash:
                await message.answer("TELEGRAM_API_ID/HASH не заданы в ENV.")
                return
            api_id, api_hash = settings.telegram_api_id, settings.telegram_api_hash
        else:
            if ":" not in raw:
                await message.answer("Формат: api_id:api_hash")
                return
            api_id_s, api_hash = raw.split(":", 1)
            if not api_id_s.isdigit():
                await message.answer("api_id должен быть числом")
                return
            api_id, api_hash = int(api_id_s), api_hash.strip()
        await state.update_data(api_id=api_id, api_hash=api_hash)
        await state.set_state(AddAccountStates.proxy)
        await message.answer(
            "ID прокси из БД (число) или <code>none</code> / "
            "строка <code>socks5://user:pass@ip:port</code>:",
            parse_mode="HTML",
        )

    @router.message(AddAccountStates.proxy)
    async def add_proxy_bind(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        data = await state.get_data()
        proxy_id: Optional[int] = None

        async with session_scope() as session:
            svc = AccountService(session)
            if raw.lower() == "none":
                proxy_id = None
            elif raw.isdigit():
                proxy_id = int(raw)
            else:
                m = PROXY_RE.match(raw)
                if not m:
                    await message.answer("Неверный формат прокси.")
                    return
                proxy = await svc.create_proxy(
                    ip=m.group("ip"),
                    port=int(m.group("port")),
                    protocol=ProxyProtocol(m.group("proto").lower()),
                    username=m.group("user"),
                    password=m.group("password"),
                )
                proxy_id = proxy.id

            account = await svc.create_account(
                phone_number=data["phone"],
                session_string=data["session_string"],
                api_id=data["api_id"],
                api_hash=data["api_hash"],
                proxy_id=proxy_id,
                status=AccountStatus.paused,
            )
            account_id = account.id
            device = account.device_model

        await state.clear()
        await message.answer(
            f"✅ Аккаунт #{account_id} создан\n"
            f"fingerprint: <code>{device}</code>\n"
            f"Статус: paused — активируйте в меню.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )

    # ---- import TData ZIP ------------------------------------------------
    async def _begin_tdata_import(message: Message, state: FSMContext) -> None:
        await state.set_state(ImportTDataStates.proxy)
        await message.answer(
            "<b>Import TData</b>\n"
            "Сначала укажите <b>постоянный</b> прокси для этой сессии:\n"
            "• ID прокси из БД\n"
            "• <code>socks5://user:pass@ip:port</code>\n"
            "• <code>none</code> (не рекомендуется — высокий риск AuthKeyDuplicated)\n\n"
            "Затем пришлите ZIP с папкой <code>tdata</code>.",
            parse_mode="HTML",
        )

    @router.message(Command("import_tdata"))
    async def cmd_import_tdata(message: Message, state: FSMContext) -> None:
        await _begin_tdata_import(message, state)

    @router.callback_query(F.data == "menu:add_tdata")
    async def cb_add_tdata(callback: CallbackQuery, state: FSMContext) -> None:
        await _begin_tdata_import(callback.message, state)  # type: ignore[arg-type]
        await callback.answer()

    @router.message(ImportTDataStates.proxy)
    async def tdata_proxy(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        proxy_id: Optional[int] = None
        proxy_dict: Optional[dict[str, Any]] = None

        async with session_scope() as session:
            svc = AccountService(session)
            if raw.lower() == "none":
                proxy_id = None
                proxy_dict = None
            elif raw.isdigit():
                proxy = await session.get(Proxy, int(raw))
                if proxy is None:
                    await message.answer(f"Proxy #{raw} не найден.")
                    return
                proxy_id = proxy.id
                proxy_dict = {
                    "protocol": proxy.protocol.value,
                    "ip": proxy.ip,
                    "port": proxy.port,
                    "username": proxy.username,
                    "password": proxy.password,
                }
            else:
                m = PROXY_RE.match(raw)
                if not m:
                    await message.answer(
                        "Формат: socks5://user:pass@ip:port | <id> | none"
                    )
                    return
                proxy = await svc.create_proxy(
                    ip=m.group("ip"),
                    port=int(m.group("port")),
                    protocol=ProxyProtocol(m.group("proto").lower()),
                    username=m.group("user"),
                    password=m.group("password"),
                )
                proxy_id = proxy.id
                proxy_dict = {
                    "protocol": proxy.protocol.value,
                    "ip": proxy.ip,
                    "port": proxy.port,
                    "username": proxy.username,
                    "password": proxy.password,
                }

        await state.update_data(proxy_id=proxy_id, proxy_dict=proxy_dict)
        await state.set_state(ImportTDataStates.passcode)
        await message.answer(
            "Локальный passcode TData (если есть) или отправьте <code>none</code>:",
            parse_mode="HTML",
        )

    @router.message(ImportTDataStates.passcode)
    async def tdata_passcode(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        passcode = None if raw.lower() in {"none", "-", ""} else raw
        await state.update_data(passcode=passcode)
        await state.set_state(ImportTDataStates.archive)
        max_mb = settings.tdata_max_zip_bytes // (1024 * 1024)
        await message.answer(
            f"Пришлите <b>ZIP</b>-архив с папкой <code>tdata</code> "
            f"(макс. {max_mb} МБ).",
            parse_mode="HTML",
        )

    @router.message(ImportTDataStates.archive, F.document)
    async def tdata_archive(message: Message, state: FSMContext, bot: Bot) -> None:
        """
        Receive ZIP → extract to temp dir → opentele convert → persist StringSession.

        Temp files are always wiped in `finally`, even on FloodWait / ban / checksum errors.
        """
        doc = message.document
        assert doc is not None

        file_name = (doc.file_name or "").lower()
        if not file_name.endswith(".zip"):
            await message.answer("❌ Нужен файл с расширением <code>.zip</code>.", parse_mode="HTML")
            return
        if doc.file_size and doc.file_size > settings.tdata_max_zip_bytes:
            await message.answer(
                f"❌ ZIP слишком большой ({doc.file_size} bytes). "
                f"Лимит: {settings.tdata_max_zip_bytes}."
            )
            return

        data = await state.get_data()
        proxy_id = data.get("proxy_id")
        proxy_dict = data.get("proxy_dict")
        passcode = data.get("passcode")

        status_msg = await message.answer("⏳ Загружаю и конвертирую TData…")
        tmp_root = Path(tempfile.mkdtemp(prefix="tg_pool_tdata_"))
        zip_path = tmp_root / "upload.zip"

        try:
            # aiogram downloads the file into our temp path
            await bot.download(doc, destination=zip_path)
            if not zip_path.exists() or zip_path.stat().st_size == 0:
                raise TDataConversionError("Не удалось скачать ZIP из Telegram.")

            converted = await convert_tdata_zip(
                zip_path,
                work_dir=tmp_root,
                proxy=proxy_dict,
                passcode=passcode,
                max_bytes=settings.tdata_max_zip_bytes,
            )

            async with session_scope() as session:
                account = await AccountService(session).upsert_from_tdata(
                    phone_number=converted.phone_number,
                    session_string=converted.session_string,
                    api_id=converted.api_id,
                    api_hash=converted.api_hash,
                    device_model=converted.device_model,
                    system_version=converted.system_version,
                    app_version=converted.app_version,
                    lang_code=converted.lang_code,
                    proxy_id=proxy_id,
                    display_name=converted.display_name,
                    telegram_user_id=converted.user_id,
                    status=AccountStatus.active,
                )
                account_id = account.id

            uname = f"@{converted.username}" if converted.username else converted.display_name
            await status_msg.edit_text(
                f"✅ Аккаунт {uname} ({converted.phone_number}) успешно добавлен из TData!\n"
                f"id=#{account_id} · device=<code>{converted.device_model}</code> / "
                f"<code>{converted.system_version}</code> / <code>{converted.app_version}</code>\n"
                f"status: <b>active</b>",
                parse_mode="HTML",
                reply_markup=tdata_success_kb(account_id),
            )
            await state.clear()
        except TDataConversionError as exc:
            logger.warning("TData import failed: %s", exc)
            await status_msg.edit_text(f"❌ Ошибка импорта TData:\n<code>{exc}</code>", parse_mode="HTML")
            # Keep FSM on archive state so admin can re-upload another ZIP
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected TData import error")
            await status_msg.edit_text(
                f"❌ Непредвиденная ошибка:\n<code>{type(exc).__name__}: {exc}</code>",
                parse_mode="HTML",
            )
        finally:
            # CRITICAL: wipe tdata artifacts — only StringSession remains in DB
            cleanup_tree(tmp_root)

    @router.message(ImportTDataStates.archive)
    async def tdata_archive_not_document(message: Message) -> None:
        await message.answer("Пришлите именно ZIP-документ (как файл), не текстом.")

    # ---- add proxy FSM ---------------------------------------------------
    @router.callback_query(F.data == "menu:add_proxy")
    async def cb_add_proxy(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddProxyStates.raw)
        await callback.message.answer(  # type: ignore[union-attr]
            "Прокси: <code>socks5://user:pass@ip:port</code>",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:proxies")
    async def cb_proxies(callback: CallbackQuery) -> None:
        from sqlalchemy import select
        from tg_pool.db.models import Proxy

        async with session_scope() as session:
            rows = (
                await session.execute(select(Proxy).order_by(Proxy.id))
            ).scalars().all()
        if not rows:
            text = "Прокси нет."
        else:
            lines = ["<b>Proxies</b>:"]
            for p in rows:
                lines.append(
                    f"• #{p.id} {p.protocol.value}://{p.ip}:{p.port} "
                    f"→ account={p.assigned_account_id or '—'}"
                )
            text = "\n".join(lines)
        await callback.message.edit_text(  # type: ignore[union-attr]
            text,
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()

    @router.message(AddProxyStates.raw)
    async def add_proxy_raw(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        m = PROXY_RE.match(raw)
        if not m:
            await message.answer("Формат: socks5://user:pass@ip:port")
            return
        async with session_scope() as session:
            proxy = await AccountService(session).create_proxy(
                ip=m.group("ip"),
                port=int(m.group("port")),
                protocol=ProxyProtocol(m.group("proto").lower()),
                username=m.group("user"),
                password=m.group("password"),
            )
            pid = proxy.id
        await state.clear()
        await message.answer(
            f"✅ Proxy #{pid} сохранён",
            reply_markup=main_menu_kb(),
        )

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AdminAccessMiddleware(settings))
    dp.callback_query.middleware(AdminAccessMiddleware(settings))
    dp.include_router(router)
    return dp
