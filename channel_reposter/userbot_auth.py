"""
userbot_auth.py — авторизация Pyrogram-юзербота.

Сценарий входа через админ-бота:
  1) API_ID
  2) API_HASH
  3) номер телефона
  4) код из Telegram / SMS
  5) облачный пароль 2FA (если включён)

Сессия сохраняется в .session — повторный вход без кода.
В источнике достаточно подписки; в назначении аккаунт должен быть админом.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pyrogram import Client
from pyrogram.errors import (
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

import config
from database import Database

logger = logging.getLogger(__name__)

KEY_API_ID = "auth_api_id"
KEY_API_HASH = "auth_api_hash"
KEY_PHONE = "auth_phone"
KEY_PASSWORD = "auth_password"


@dataclass
class AuthCredentials:
    api_id: int
    api_hash: str
    phone: str
    password: str = ""


class UserbotAuth:
    def __init__(self, db: Database, workdir: Path) -> None:
        self.db = db
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)

        self.client: Optional[Client] = None
        self._phone_code_hash: Optional[str] = None
        self._pending: Optional[AuthCredentials] = None
        self._authorized = False

    def save_credentials(self, creds: AuthCredentials) -> None:
        self.db.set(KEY_API_ID, str(creds.api_id))
        self.db.set(KEY_API_HASH, creds.api_hash)
        self.db.set(KEY_PHONE, creds.phone)
        if creds.password:
            self.db.set(KEY_PASSWORD, creds.password)

    def load_credentials(self) -> Optional[AuthCredentials]:
        api_id_s = self.db.get(KEY_API_ID) or (
            str(config.API_ID) if config.API_ID else ""
        )
        api_hash = self.db.get(KEY_API_HASH) or (config.API_HASH or "")
        phone = self.db.get(KEY_PHONE) or (config.PHONE or "")
        password = self.db.get(KEY_PASSWORD) or (config.PASSWORD or "")

        if not api_id_s or not api_hash:
            return None
        try:
            api_id = int(api_id_s)
        except ValueError:
            return None
        return AuthCredentials(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone or "",
            password=password or "",
        )

    def session_path(self) -> Path:
        return self.workdir / f"{config.SESSION_NAME}.session"

    def has_session_file(self) -> bool:
        return self.session_path().exists()

    @property
    def is_ready(self) -> bool:
        return bool(self._authorized and self.client is not None)

    async def _close_client(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.stop()
        except Exception:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self._authorized = False

    async def try_start_existing(self) -> bool:
        creds = self.load_credentials()
        if not creds or not creds.api_id or not creds.api_hash:
            logger.info("Нет API_ID/API_HASH — юзербот не запущен")
            return False

        await self._close_client()

        probe = Client(
            name=config.SESSION_NAME,
            api_id=creds.api_id,
            api_hash=creds.api_hash,
            workdir=str(self.workdir),
            no_updates=True,
        )
        try:
            # В Pyrogram 2.x connect() возвращает bool: авторизована ли сессия
            authorized = await probe.connect()
            await probe.disconnect()
        except Exception:
            logger.exception("Не удалось проверить сессию")
            try:
                await probe.disconnect()
            except Exception:
                pass
            return False

        if not authorized:
            logger.info("Сессия не авторизована — нужен /login")
            return False

        self.client = Client(
            name=config.SESSION_NAME,
            api_id=creds.api_id,
            api_hash=creds.api_hash,
            workdir=str(self.workdir),
            no_updates=True,
        )
        try:
            await self.client.start()
            me = await self.client.get_me()
            self._authorized = True
            logger.info(
                "Юзербот онлайн: %s (id=%s)",
                me.username or me.first_name,
                me.id,
            )
            return True
        except Exception:
            logger.exception("Не удалось поднять юзербота")
            await self._close_client()
            return False

    async def begin_login(self, creds: AuthCredentials) -> str:
        phone = creds.phone.strip().replace(" ", "")
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("+")

        creds = AuthCredentials(
            api_id=creds.api_id,
            api_hash=creds.api_hash.strip(),
            phone=phone,
            password=creds.password or "",
        )
        self.save_credentials(creds)
        self._pending = creds

        await self._close_client()

        self.client = Client(
            name=config.SESSION_NAME,
            api_id=creds.api_id,
            api_hash=creds.api_hash,
            workdir=str(self.workdir),
            no_updates=True,
        )
        await self.client.connect()

        try:
            sent = await self.client.send_code(creds.phone)
        except PhoneNumberInvalid as e:
            await self.client.disconnect()
            self.client = None
            raise ValueError(f"Некорректный номер: {creds.phone}") from e

        self._phone_code_hash = sent.phone_code_hash
        logger.info("Код отправлен на %s", creds.phone)
        return f"Код отправлен на {creds.phone}"

    async def confirm_code(self, code: str) -> str:
        if not self.client or not self._phone_code_hash or not self._pending:
            raise RuntimeError("Сначала /login")

        code = code.strip().replace(" ", "").replace("-", "")
        try:
            await self.client.sign_in(
                phone_number=self._pending.phone,
                phone_code_hash=self._phone_code_hash,
                phone_code=code,
            )
        except SessionPasswordNeeded:
            return "password_required"
        except PhoneCodeInvalid as e:
            raise ValueError("Неверный код") from e
        except PhoneCodeExpired as e:
            raise ValueError("Код истёк — /login заново") from e

        await self._finalize_login()
        return "ok"

    async def confirm_password(self, password: str) -> None:
        if not self.client or not self._pending:
            raise RuntimeError("Сначала код из /login")

        password = password.strip()
        self._pending = AuthCredentials(
            api_id=self._pending.api_id,
            api_hash=self._pending.api_hash,
            phone=self._pending.phone,
            password=password,
        )
        self.save_credentials(self._pending)
        await self.client.check_password(password)
        await self._finalize_login()

    async def _finalize_login(self) -> None:
        if not self.client or not self._pending:
            raise RuntimeError("Клиент не создан")

        creds = self._pending
        try:
            await self.client.disconnect()
        except Exception:
            pass

        self.client = Client(
            name=config.SESSION_NAME,
            api_id=creds.api_id,
            api_hash=creds.api_hash,
            workdir=str(self.workdir),
            no_updates=True,
        )
        await self.client.start()
        me = await self.client.get_me()
        self._authorized = True
        self._phone_code_hash = None
        logger.info(
            "Вход выполнен: %s (id=%s)",
            me.username or me.first_name,
            me.id,
        )

    async def stop(self) -> None:
        await self._close_client()

    async def status_text(self) -> str:
        if self.is_ready and self.client:
            me = await self.client.get_me()
            return (
                f"🟢 Юзербот: <b>{me.first_name}</b> "
                f"(@{me.username or '—'}), id=<code>{me.id}</code>"
            )
        if self.has_session_file():
            return "🟡 Есть сессия, но не авторизован — «🔐 Вход»"
        return "🔴 Юзербот не авторизован — «🔐 Вход» /login"
