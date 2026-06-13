"""
Конфигурация Starvell Cardinal.
Все ключевые данные настраиваются через Telegram; файл settings.json — хранилище.
"""

from __future__ import annotations

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

VERSION = "1.2.0"
GITHUB_REPO = "asto7996-maker/fpc-plugins"
GITHUB_BRANCH = "cursor/fpc-parity-280c"
BACKUP_DIR = STORAGE_DIR / "backups"
PRODUCTS_DIR = STORAGE_DIR / "products"

DEFAULT_AI_SYSTEM_PROMPT = (
    "Ты — дружелюбный и профессиональный менеджер магазина на Starvell. "
    "Твоя цель — помочь клиенту, ответить на его вопросы и оставить исключительно положительное впечатление."
)

REFUND_DISCLAIMER = (
    "⚠️ ВАЖНО! Совершая покупку, вы автоматически соглашаетесь с правилами магазина. "
    "Возврат средств (рефанд) не предусмотрен ни при каких условиях."
)


@dataclass
class StarvellAccount:
    name: str
    session_cookie: str
    sid_cookie: str = ""
    my_games_cookie: str = ""
    enabled: bool = True


@dataclass
class Settings:
    # Telegram (только BOT_TOKEN нужен при установке)
    bot_token: str = ""
    owner_id: int = 0
    admin_ids: list[int] = field(default_factory=list)

    # Starvell
    session_cookie: str = ""
    sid_cookie: str = ""
    my_games_cookie: str = ""
    starvell_username: str = ""
    accounts: list[StarvellAccount] = field(default_factory=list)

    # Функции (как в FunPay Cardinal — секция main)
    auto_delivery_enabled: bool = True
    auto_bump_enabled: bool = True
    auto_welcome_enabled: bool = True
    auto_review_enabled: bool = True
    auto_response_enabled: bool = True
    ai_replies_enabled: bool = False
    multi_delivery: bool = True
    order_confirm_enabled: bool = True

    # Приветствие (gr)
    greetings_only_new_chats: bool = True
    greetings_ignore_system: bool = True
    welcome_inactivity_days: int = 2

    # Подтверждение заказа (oc)
    order_confirm_text: str = "Спасибо за подтверждение заказа! Будем рады видеть вас снова! ⭐"

    # Ответы на отзывы по звёздам (rr) — 1..5
    review_replies: dict[str, str] = field(default_factory=lambda: {
        "5": "Большое спасибо за отличный отзыв! ⭐",
        "4": "Спасибо за хорошую оценку! Рады, что всё понравилось 😊",
        "3": "Спасибо за отзыв! Если что-то можно улучшить — напишите нам.",
        "2": "Жаль, что не всё идеально. Мы уже работаем над улучшением сервиса.",
        "1": "Нам очень жаль за негативный опыт. Напишите в чат — разберёмся лично.",
    })
    review_use_gemini: bool = True

    # Тайминги
    chat_poll_interval: float = 5.0
    orders_poll_interval: float = 10.0
    bump_interval: float = 3600.0
    api_delay_seconds: float = 1.5
    api_max_per_minute: int = 40

    welcome_text: str = (
        "Здравствуйте! 👋 Рады видеть вас в нашем магазине на Starvell. "
        "Напишите, чем можем помочь!"
    )
    welcome_cooldown_minutes: int = 2880  # 2 суток — повторное приветствие после молчания

    delivery_template: str = (
        "✅ Ваш заказ выполнен!\n\n"
        "{product}\n\n"
        "📦 Товар:\n{content}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ ВАЖНО! Совершая покупку, вы автоматически соглашаетесь с правилами магазина.\n"
        "❌ Возврат средств (рефанд) не предусмотрен ни при каких условиях.\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    review_template: str = (
        "Спасибо за покупку! 🙏 Было приятно с вами работать. "
        "Будем рады видеть вас снова! ⭐"
    )

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    ai_system_prompt: str = DEFAULT_AI_SYSTEM_PROMPT

    watermark_on: bool = False
    watermark_text: str = "[Starvell Cardinal]"
    debug: bool = False

    def get_active_accounts(self) -> list[StarvellAccount]:
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

    def is_starvell_configured(self) -> bool:
        return bool(self.get_active_accounts())

    def is_gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.strip())

    def ensure_dirs(self) -> None:
        for path in (CONFIG_DIR, STORAGE_DIR, LOGS_DIR, PLUGINS_DIR, PLUGIN_STATE_PATH.parent, BACKUP_DIR, PRODUCTS_DIR):
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
    settings = Settings()
    settings.ensure_dirs()

    data: dict[str, Any] = {}
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}

    token_env = os.getenv("BOT_TOKEN", "").strip()
    if token_env:
        data["bot_token"] = token_env

    gemini_env = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_env:
        data["gemini_api_key"] = gemini_env

    if "accounts" in data and isinstance(data["accounts"], list):
        settings.accounts = [_account_from_dict(a) for a in data["accounts"] if isinstance(a, dict)]

    skip = {"accounts", "bot_password_md5", "bot_password", "openai_api_key", "ai_provider", "openai_model", "review_replies"}
    for key, value in data.items():
        if key in skip:
            continue
        if key == "review_replies" and isinstance(value, dict):
            settings.review_replies.update({str(k): str(v) for k, v in value.items()})
            continue
        if hasattr(settings, key):
            setattr(settings, key, value)

    if os.getenv("ADMIN_IDS"):
        settings.admin_ids = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

    return settings


def save_settings(settings: Settings) -> None:
    settings.ensure_dirs()
    data = asdict(settings)
    data["accounts"] = [_account_to_dict(a) for a in settings.accounts]
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(SETTINGS_PATH)


def create_default_settings_file() -> None:
    if SETTINGS_PATH.exists():
        return
    save_settings(Settings())
