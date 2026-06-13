"""
Конфигурация Starvell Cardinal.
Настройки хранятся в config/settings.json и могут переопределяться переменными окружения.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
STORAGE_DIR = BASE_DIR / "storage"
LOGS_DIR = BASE_DIR / "logs"
PLUGINS_DIR = BASE_DIR / "plugins"
DB_PATH = STORAGE_DIR / "cardinal.sqlite3"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
PLUGIN_STATE_PATH = STORAGE_DIR / "plugins" / "state.json"

DEFAULT_AI_SYSTEM_PROMPT = (
    "Ты — дружелюбный и профессиональный менеджер магазина на Starvell. "
    "Твоя цель — помочь клиенту, ответить на его вопросы и оставить исключительно положительное впечатление."
)

REFUND_DISCLAIMER = (
    "⚠️ ВАЖНО! Совершая покупку, вы автоматически соглашаетесь с правилами магазина. "
    "Возврат средств (рефанд) не предусмотрен ни при каких условиях."
)


def md5_hex(text: str) -> str:
    """Возвращает MD5-хеш строки (для пароля бота)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


@dataclass
class StarvellAccount:
    """Один аккаунт Starvell (поддержка нескольких сессий)."""

    name: str
    session_cookie: str
    sid_cookie: str = ""
    my_games_cookie: str = ""
    enabled: bool = True


@dataclass
class Settings:
    """Все настройки бота в одном объекте."""

    # Telegram
    bot_token: str = ""
    bot_password_md5: str = ""
    admin_ids: list[int] = field(default_factory=list)

    # Starvell — основной аккаунт (для обратной совместимости)
    session_cookie: str = ""
    sid_cookie: str = ""
    my_games_cookie: str = ""

    # Несколько аккаунтов
    accounts: list[StarvellAccount] = field(default_factory=list)

    # Функции автоматизации
    auto_delivery_enabled: bool = True
    auto_bump_enabled: bool = True
    auto_welcome_enabled: bool = True
    auto_review_enabled: bool = True
    ai_replies_enabled: bool = True

    # Уведомления в Telegram
    notify_orders: bool = True
    notify_chats: bool = True
    notify_bump: bool = True
    notify_auth: bool = True
    notify_delivery: bool = True

    # Тайминги (секунды)
    chat_poll_interval: float = 5.0
    orders_poll_interval: float = 10.0
    bump_interval: float = 3600.0
    api_delay_seconds: float = 1.5
    api_max_per_minute: int = 40

    # Приветствие
    welcome_text: str = (
        "Здравствуйте! 👋 Рады видеть вас в нашем магазине на Starvell. "
        "Напишите, чем можем помочь — ответим в ближайшее время!"
    )
    welcome_cooldown_minutes: int = 60

    # Автовыдача — шаблон сообщения
    delivery_template: str = (
        "✅ Ваш заказ выполнен!\n\n"
        "{product}\n\n"
        "📦 Товар:\n{content}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ ВАЖНО! Совершая покупку, вы автоматически соглашаетесь с правилами магазина.\n"
        "❌ Возврат средств (рефанд) не предусмотрен ни при каких условиях.\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    # Авто-отзыв (благодарность после закрытия сделки)
    review_template: str = (
        "Спасибо за покупку! 🙏 Было приятно с вами работать. "
        "Будем рады видеть вас снова! ⭐"
    )

    # AI
    ai_provider: str = "gemini"  # gemini | openai
    gemini_api_key: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    gemini_model: str = "gemini-2.0-flash"
    ai_system_prompt: str = DEFAULT_AI_SYSTEM_PROMPT

    # Прочее
    watermark_on: bool = False
    watermark_text: str = "[Starvell Cardinal]"
    debug: bool = False
    language: str = "ru"

    def get_active_accounts(self) -> list[StarvellAccount]:
        """Возвращает список активных аккаунтов Starvell."""
        if self.accounts:
            return [a for a in self.accounts if a.enabled and a.session_cookie]
        if self.session_cookie:
            return [
                StarvellAccount(
                    name="default",
                    session_cookie=self.session_cookie,
                    sid_cookie=self.sid_cookie,
                    my_games_cookie=self.my_games_cookie,
                )
            ]
        return []

    def ensure_dirs(self) -> None:
        """Создаёт необходимые каталоги."""
        for path in (CONFIG_DIR, STORAGE_DIR, LOGS_DIR, PLUGINS_DIR, PLUGIN_STATE_PATH.parent):
            path.mkdir(parents=True, exist_ok=True)


def _account_from_dict(data: dict[str, Any]) -> StarvellAccount:
    return StarvellAccount(
        name=str(data.get("name", "account")),
        session_cookie=str(data.get("session_cookie", "")),
        sid_cookie=str(data.get("sid_cookie", "")),
        my_games_cookie=str(data.get("my_games_cookie", "")),
        enabled=bool(data.get("enabled", True)),
    )


def _account_to_dict(acc: StarvellAccount) -> dict[str, Any]:
    return asdict(acc)


def load_settings() -> Settings:
    """Загружает настройки из JSON и переменных окружения."""
    settings = Settings()
    settings.ensure_dirs()

    data: dict[str, Any] = {}
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}

    # Переменные окружения имеют приоритет
    env_map = {
        "bot_token": "BOT_TOKEN",
        "bot_password_md5": "BOT_PASSWORD_MD5",
        "session_cookie": "SESSION_COOKIE",
        "sid_cookie": "SID_COOKIE",
        "my_games_cookie": "MY_GAMES_COOKIE",
        "gemini_api_key": "GEMINI_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
    }
    for attr, env_key in env_map.items():
        val = os.getenv(env_key, "").strip()
        if val:
            data[attr] = val

    plain_pass = os.getenv("BOT_PASSWORD", "").strip() or str(data.get("bot_password", "")).strip()
    if plain_pass and not data.get("bot_password_md5"):
        data["bot_password_md5"] = md5_hex(plain_pass)

    if "accounts" in data and isinstance(data["accounts"], list):
        settings.accounts = [_account_from_dict(a) for a in data["accounts"] if isinstance(a, dict)]

    for key, value in data.items():
        if key == "accounts":
            continue
        if hasattr(settings, key):
            setattr(settings, key, value)

    if os.getenv("ADMIN_IDS"):
        settings.admin_ids = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

    return settings


def save_settings(settings: Settings) -> None:
    """Сохраняет настройки в JSON."""
    settings.ensure_dirs()
    data = asdict(settings)
    data["accounts"] = [_account_to_dict(a) for a in settings.accounts]
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(SETTINGS_PATH)


def create_default_settings_file() -> None:
    """Создаёт файл настроек по умолчанию, если его нет."""
    if SETTINGS_PATH.exists():
        return
    save_settings(Settings())
