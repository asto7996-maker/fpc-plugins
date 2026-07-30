"""Add account via StringSession or TData ZIP."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from tg_pool.admin.keyboards import add_account_kb, main_menu_kb, tdata_success_kb
from tg_pool.admin.nav import ALL_REPLY_NAV
from tg_pool.admin.routers.common import safe_answer, safe_edit
from tg_pool.admin.states import AddAccountStates, ImportTDataStates
from tg_pool.clients.tdata_converter import (
    TDataConversionError,
    cleanup_tree,
    convert_tdata_zip,
)
from tg_pool.config import Settings
from tg_pool.db.models import AccountStatus, Proxy
from tg_pool.db.session import session_scope
from tg_pool.services.account_service import AccountService
from tg_pool.services.proxy_finder import ensure_working_proxy

logger = logging.getLogger(__name__)

# Only one heavy TData/Telethon conversion at a time — keeps the admin UI snappy
_tdata_lock = asyncio.Lock()


def build_add_account_router(settings: Settings) -> Router:
    router = Router(name="add_account")

    @router.callback_query(F.data == "menu:add")
    async def cb_add_menu(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await safe_edit(
            callback,
            "➕ <b>Добавить аккаунт</b>\n\n"
            "<blockquote>Выберите способ импорта сессии.</blockquote>",
            add_account_kb(),
        )
        await safe_answer(callback)

    # ---- StringSession flow ---------------------------------------------
    @router.callback_query(F.data == "add:session")
    async def cb_add_session(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddAccountStates.phone)
        await callback.message.answer(  # type: ignore[union-attr]
            "🧬 <b>StringSession</b>\n\n"
            "Номер телефона в формате <code>+79001234567</code>:",
            parse_mode="HTML",
        )
        await safe_answer(callback)

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
        data = await state.get_data()
        await state.clear()
        wait = await message.answer(
            "🔍 Подбираю рабочий прокси…\n"
            "<i>Меню продолжает работать — импорт идёт в фоне.</i>",
            parse_mode="HTML",
        )

        async def _job() -> None:
            try:
                proxy = await ensure_working_proxy()
                proxy_id = proxy.id if proxy else None
                proxy_label = (
                    f"{proxy.protocol.value}://{proxy.ip}:{proxy.port}" if proxy else "—"
                )
                async with session_scope() as session:
                    account = await AccountService(session).create_account(
                        phone_number=data["phone"],
                        session_string=data["session_string"],
                        api_id=api_id,
                        api_hash=api_hash,
                        proxy_id=proxy_id,
                        status=AccountStatus.paused,
                    )
                    account_id = account.id
                    device = account.device_model
                await wait.edit_text(
                    f"✅ Аккаунт <b>#{account_id}</b> создан\n"
                    f"fingerprint: <code>{device}</code>\n"
                    f"proxy: <code>{proxy_label}</code>\n"
                    f"Статус: <b>paused</b> — активируйте в списке.",
                    parse_mode="HTML",
                    reply_markup=main_menu_kb(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("StringSession import failed")
                try:
                    await wait.edit_text(
                        f"❌ Ошибка: <code>{type(exc).__name__}: {exc}</code>",
                        parse_mode="HTML",
                    )
                except Exception:  # noqa: BLE001
                    await message.answer(
                        f"❌ Ошибка: <code>{type(exc).__name__}: {exc}</code>",
                        parse_mode="HTML",
                    )

        asyncio.create_task(_job(), name="string-session-import")

    # ---- TData ZIP flow (ZIP only; auto-find working public proxy) -----
    async def _begin_tdata(message: Message, state: FSMContext) -> None:
        await state.set_state(ImportTDataStates.archive)
        max_mb = settings.tdata_max_zip_bytes // (1024 * 1024)
        await message.answer(
            "📦 <b>Import TData</b>\n\n"
            "<blockquote>Просто пришлите ZIP с папкой <code>tdata</code>.\n"
            "Рабочий прокси бот найдёт сам из публичных списков.</blockquote>\n\n"
            f"Макс. размер: <b>{max_mb} МБ</b>",
            parse_mode="HTML",
        )

    @router.message(Command("import_tdata"))
    async def cmd_import_tdata(message: Message, state: FSMContext) -> None:
        await _begin_tdata(message, state)

    @router.callback_query(F.data == "add:tdata")
    async def cb_add_tdata(callback: CallbackQuery, state: FSMContext) -> None:
        await safe_answer(callback)
        await _begin_tdata(callback.message, state)  # type: ignore[arg-type]

    # Recover users stuck in the old multi-step FSM
    @router.message(ImportTDataStates.proxy, F.document)
    @router.message(ImportTDataStates.passcode, F.document)
    async def tdata_legacy_doc(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.set_state(ImportTDataStates.archive)
        await tdata_archive(message, state, bot)

    @router.message(ImportTDataStates.proxy)
    @router.message(ImportTDataStates.passcode)
    async def tdata_legacy_text(message: Message, state: FSMContext) -> None:
        await state.set_state(ImportTDataStates.archive)
        max_mb = settings.tdata_max_zip_bytes // (1024 * 1024)
        await message.answer(
            "Шаги прокси/passcode больше не нужны.\n"
            f"Просто пришлите <b>ZIP</b> с <code>tdata</code> (макс. {max_mb} МБ).",
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

        if _tdata_lock.locked():
            await message.answer(
                "⏳ Уже идёт импорт TData. Дождитесь результата — меню пока работает."
            )
            return

        # Clear FSM immediately so reply-keyboard nav is never swallowed
        await state.clear()
        status_msg = await message.answer(
            "📥 Скачиваю ZIP…\n"
            "<i>Кнопки меню работают — тяжёлая работа в фоне.</i>",
            parse_mode="HTML",
        )
        tmp_root = Path(tempfile.mkdtemp(prefix="tg_pool_tdata_"))
        zip_path = tmp_root / "upload.zip"

        try:
            await bot.download(doc, destination=zip_path)
        except Exception as exc:  # noqa: BLE001
            cleanup_tree(tmp_root)
            await status_msg.edit_text(
                f"❌ Не удалось скачать ZIP: <code>{type(exc).__name__}: {exc}</code>",
                parse_mode="HTML",
            )
            return

        await status_msg.edit_text(
            "🔍 Ищу рабочий прокси и конвертирую TData…\n"
            "<i>Это может занять до минуты. Меню не блокируется.</i>",
            parse_mode="HTML",
        )

        async def _job() -> None:
            async with _tdata_lock:
                tried_proxy_ids: set[int] = set()
                last_err: Exception | None = None
                try:
                    for attempt in range(1, 4):
                        proxy = await ensure_working_proxy()
                        if proxy is None:
                            await status_msg.edit_text(
                                "❌ Не удалось найти рабочий прокси до Telegram.\n"
                                "Попробуйте позже или нажмите 🌐 Прокси → 🔍 Найти.",
                            )
                            return
                        proxy_id = int(proxy.id)
                        if proxy_id in tried_proxy_ids:
                            async with session_scope() as session:
                                dead = await session.get(Proxy, proxy_id)
                                if dead is not None:
                                    dead.is_alive = False
                            continue
                        tried_proxy_ids.add(proxy_id)
                        proxy_dict = {
                            "protocol": proxy.protocol.value,
                            "ip": proxy.ip,
                            "port": proxy.port,
                            "username": proxy.username,
                            "password": proxy.password,
                        }
                        proxy_label = (
                            f"#{proxy_id} {proxy.protocol.value}://"
                            f"{proxy.ip}:{proxy.port}"
                        )
                        await status_msg.edit_text(
                            f"✅ Прокси: <code>{proxy_label}</code> "
                            f"(попытка {attempt}/3)\n"
                            "⏳ Конвертирую TData…",
                            parse_mode="HTML",
                        )
                        try:
                            converted = await convert_tdata_zip(
                                zip_path,
                                work_dir=tmp_root,
                                proxy=proxy_dict,
                                passcode=None,
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

                            uname = (
                                f"@{converted.username}"
                                if converted.username
                                else converted.display_name
                            )
                            await status_msg.edit_text(
                                f"✅ Аккаунт <b>{uname}</b> "
                                f"(<code>{converted.phone_number}</code>) "
                                f"успешно добавлен из TData!\n\n"
                                f"id=<b>#{account_id}</b>\n"
                                f"proxy: <code>{proxy_label}</code>\n"
                                f"device: <code>{converted.device_model}</code> / "
                                f"<code>{converted.system_version}</code>\n"
                                f"status: <b>active</b>",
                                parse_mode="HTML",
                                reply_markup=tdata_success_kb(account_id),
                            )
                            return
                        except TDataConversionError as exc:
                            last_err = exc
                            msg = str(exc).lower()
                            if "прокси" in msg or "proxy" in msg or "connection" in msg:
                                async with session_scope() as session:
                                    dead = await session.get(Proxy, proxy_id)
                                    if dead is not None:
                                        dead.is_alive = False
                                await status_msg.edit_text(
                                    f"⚠️ Прокси не подошёл: <code>{exc}</code>\n"
                                    "Пробую другой…",
                                    parse_mode="HTML",
                                )
                                continue
                            await status_msg.edit_text(
                                f"❌ <b>Ошибка импорта TData</b>\n"
                                f"<blockquote>{exc}</blockquote>",
                                parse_mode="HTML",
                            )
                            return

                    await status_msg.edit_text(
                        "❌ Не удалось импортировать: все подобранные прокси "
                        "не подключились.\n"
                        f"<blockquote>{last_err}</blockquote>",
                        parse_mode="HTML",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("TData import failed")
                    try:
                        await status_msg.edit_text(
                            f"❌ Непредвиденная ошибка: "
                            f"<code>{type(exc).__name__}: {exc}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    cleanup_tree(tmp_root)

        asyncio.create_task(_job(), name="tdata-import")

    @router.message(
        ImportTDataStates.archive,
        F.text,
        ~F.text.in_(ALL_REPLY_NAV),
        ~F.text.startswith("/"),
    )
    async def tdata_not_doc(message: Message) -> None:
        await message.answer(
            "Пришлите ZIP именно как <b>документ</b>, либо нажмите 🏠 Меню для отмены.",
            parse_mode="HTML",
        )

    return router
