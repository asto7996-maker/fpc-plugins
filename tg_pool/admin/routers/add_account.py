"""Add account via StringSession or TData ZIP."""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tg_pool.admin.keyboards import add_account_kb, main_menu_kb, tdata_success_kb
from tg_pool.admin.routers.common import safe_edit
from tg_pool.admin.states import AddAccountStates, ImportTDataStates
from tg_pool.clients.tdata_converter import (
    TDataConversionError,
    cleanup_tree,
    convert_tdata_zip,
)
from tg_pool.config import Settings
from tg_pool.db.models import AccountStatus, Proxy, ProxyProtocol
from tg_pool.db.session import session_scope
from tg_pool.services.account_service import AccountService

logger = logging.getLogger(__name__)

PROXY_RE = re.compile(
    r"^(?P<proto>socks5|http)://(?:(?P<user>[^:]+):(?P<password>[^@]+)@)?"
    r"(?P<ip>[^:]+):(?P<port>\d+)$",
    re.IGNORECASE,
)


def build_add_account_router(settings: Settings) -> Router:
    router = Router(name="add_account")

    @router.callback_query(F.data == "menu:add")
    async def cb_add_menu(callback: CallbackQuery) -> None:
        await safe_edit(
            callback,
            "➕ <b>Добавить аккаунт</b>\n\n"
            "<blockquote>Выберите способ импорта сессии.</blockquote>",
            add_account_kb(),
        )
        await callback.answer()

    # ---- StringSession flow ---------------------------------------------
    @router.callback_query(F.data == "add:session")
    async def cb_add_session(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddAccountStates.phone)
        await callback.message.answer(  # type: ignore[union-attr]
            "🧬 <b>StringSession</b>\n\n"
            "Номер телефона в формате <code>+79001234567</code>:",
            parse_mode="HTML",
        )
        await callback.answer()

    @router.message(AddAccountStates.phone)
    async def add_phone(message: Message, state: FSMContext) -> None:
        phone = (message.text or "").strip()
        if not phone.startswith("+"):
            await message.answer("Номер должен начинаться с <code>+</code>", parse_mode="HTML")
            return
        await state.update_data(phone=phone)
        await state.set_state(AddAccountStates.session)
        await message.answer("Пришлите <b>StringSession</b> (Telethon):", parse_mode="HTML")

    @router.message(AddAccountStates.session)
    async def add_session(message: Message, state: FSMContext) -> None:
        session_string = (message.text or "").strip()
        if len(session_string) < 20:
            await message.answer("❌ Сессия слишком короткая.")
            return
        await state.update_data(session_string=session_string)
        await state.set_state(AddAccountStates.api)
        await message.answer(
            "Отправьте <code>api_id:api_hash</code> или <code>default</code>:",
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
            if ":" not in raw or not raw.split(":", 1)[0].isdigit():
                await message.answer("Формат: <code>api_id:api_hash</code>", parse_mode="HTML")
                return
            api_id_s, api_hash = raw.split(":", 1)
            api_id, api_hash = int(api_id_s), api_hash.strip()
        await state.update_data(api_id=api_id, api_hash=api_hash)
        await state.set_state(AddAccountStates.proxy)
        await message.answer(
            "Прокси: ID / <code>socks5://…</code> / <code>none</code>",
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
                    await message.answer("❌ Неверный формат прокси.")
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
            f"✅ Аккаунт <b>#{account_id}</b> создан\n"
            f"fingerprint: <code>{device}</code>\n"
            f"Статус: <b>paused</b> — активируйте в списке.",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )

    # ---- TData ZIP flow -------------------------------------------------
    async def _begin_tdata(message: Message, state: FSMContext) -> None:
        await state.set_state(ImportTDataStates.proxy)
        await message.answer(
            "📦 <b>Import TData</b>\n\n"
            "<blockquote>Укажите постоянный прокси для сессии — "
            "ротация IP под живой auth key опасна.</blockquote>\n\n"
            "• ID прокси\n"
            "• <code>socks5://user:pass@ip:port</code>\n"
            "• <code>none</code>",
            parse_mode="HTML",
        )

    @router.message(Command("import_tdata"))
    async def cmd_import_tdata(message: Message, state: FSMContext) -> None:
        await _begin_tdata(message, state)

    @router.callback_query(F.data == "add:tdata")
    async def cb_add_tdata(callback: CallbackQuery, state: FSMContext) -> None:
        await _begin_tdata(callback.message, state)  # type: ignore[arg-type]
        await callback.answer()

    @router.message(ImportTDataStates.proxy)
    async def tdata_proxy(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        proxy_id: Optional[int] = None
        proxy_dict: Optional[dict[str, Any]] = None

        async with session_scope() as session:
            svc = AccountService(session)
            if raw.lower() == "none":
                pass
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
                    await message.answer("Формат: socks5://… | id | none")
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
            "Локальный passcode TData или <code>none</code>:",
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
            f"Пришлите <b>ZIP</b> с папкой <code>tdata</code> (макс. {max_mb} МБ).",
            parse_mode="HTML",
        )

    @router.message(ImportTDataStates.archive, F.document)
    async def tdata_archive(message: Message, state: FSMContext, bot: Bot) -> None:
        doc = message.document
        assert doc is not None
        file_name = (doc.file_name or "").lower()
        if not file_name.endswith(".zip"):
            await message.answer("❌ Нужен файл <code>.zip</code>", parse_mode="HTML")
            return
        if doc.file_size and doc.file_size > settings.tdata_max_zip_bytes:
            await message.answer("❌ ZIP слишком большой.")
            return

        data = await state.get_data()
        status_msg = await message.answer("⏳ Конвертирую TData через <b>opentele</b>…", parse_mode="HTML")
        tmp_root = Path(tempfile.mkdtemp(prefix="tg_pool_tdata_"))
        zip_path = tmp_root / "upload.zip"

        try:
            await bot.download(doc, destination=zip_path)
            converted = await convert_tdata_zip(
                zip_path,
                work_dir=tmp_root,
                proxy=data.get("proxy_dict"),
                passcode=data.get("passcode"),
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
                    proxy_id=data.get("proxy_id"),
                    display_name=converted.display_name,
                    telegram_user_id=converted.user_id,
                    status=AccountStatus.active,
                )
                account_id = account.id

            uname = f"@{converted.username}" if converted.username else converted.display_name
            await status_msg.edit_text(
                f"✅ Аккаунт <b>{uname}</b> (<code>{converted.phone_number}</code>) "
                f"успешно добавлен из TData!\n\n"
                f"id=<b>#{account_id}</b>\n"
                f"device: <code>{converted.device_model}</code> / "
                f"<code>{converted.system_version}</code>\n"
                f"status: <b>active</b>",
                parse_mode="HTML",
                reply_markup=tdata_success_kb(account_id),
            )
            await state.clear()
        except TDataConversionError as exc:
            await status_msg.edit_text(
                f"❌ <b>Ошибка импорта TData</b>\n<blockquote>{exc}</blockquote>",
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("TData import failed")
            await status_msg.edit_text(
                f"❌ Непредвиденная ошибка: <code>{type(exc).__name__}: {exc}</code>",
                parse_mode="HTML",
            )
        finally:
            cleanup_tree(tmp_root)

    @router.message(ImportTDataStates.archive)
    async def tdata_not_doc(message: Message) -> None:
        await message.answer("Пришлите ZIP именно как <b>документ</b>.", parse_mode="HTML")

    return router
