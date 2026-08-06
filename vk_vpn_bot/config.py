"""
config.py — загрузка и валидация настроек бота из .env.

Все секреты читаются ТОЛЬКО через python-dotenv.
Никогда не хардкодьте токены в исходниках.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта (рядом с main.py)
BASE_DIR = Path(__file__).resolve().parent

# Загружаем переменные из .env (если файл существует)
load_dotenv(BASE_DIR / ".env")


def _require(name: str) -> str:
    """Обязательная переменная окружения — падаем с понятной ошибкой, если её нет."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Переменная окружения '{name}' не задана. "
            f"Скопируйте .env.example в .env и заполните значения."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    """Необязательная переменная с значением по умолчанию."""
    return os.getenv(name, default).strip() or default


@dataclass(frozen=True)
class Settings:
    """Иммутабельные настройки приложения."""

    vk_token: str
    group_id: int
    database_url: str
    trial_days: int
    trial_traffic_gb: int
    trial_devices: int
    vpn_key_type: str
    support_url: str
    support_text: str
    support_admin_ids: tuple[int, ...]
    main_admin_vk_id: int
    main_admin_username: str
    vless_template: str
    outline_keys: tuple[str, ...]
    bot_name: str
    welcome_text: str
    # Интеграция с Bedolaga / Paskod Remnawave
    bedolaga_api_url: str
    bedolaga_api_key: str
    cabinet_url: str
    cabinet_jwt_secret: str
    renew_days: int


def load_settings() -> Settings:
    """Читает .env и возвращает объект Settings."""
    # GROUP_ID необязателен для Long Poll (группа определяется токеном),
    # но полезен для ссылок поддержки и логов.
    group_raw = _optional("GROUP_ID", "0")
    try:
        group_id = int(group_raw)
    except ValueError as exc:
        raise RuntimeError("GROUP_ID должен быть целым числом") from exc

    trial_raw = _optional("TRIAL_DAYS", "3")
    try:
        trial_days = max(1, int(trial_raw))
    except ValueError as exc:
        raise RuntimeError("TRIAL_DAYS должен быть целым числом") from exc

    # Лимиты триала задаются в админке Bedolaga; здесь — чтобы честно
    # называть их до активации, когда подписки ещё нет.
    try:
        trial_traffic_gb = max(1, int(_optional("TRIAL_TRAFFIC_GB", "10")))
    except ValueError as exc:
        raise RuntimeError("TRIAL_TRAFFIC_GB должен быть целым числом") from exc
    try:
        trial_devices = max(1, int(_optional("TRIAL_DEVICES", "1")))
    except ValueError as exc:
        raise RuntimeError("TRIAL_DEVICES должен быть целым числом") from exc

    renew_raw = _optional("RENEW_DAYS", "30")
    try:
        renew_days = max(1, int(renew_raw))
    except ValueError as exc:
        raise RuntimeError("RENEW_DAYS должен быть целым числом") from exc

    outline_raw = _optional("OUTLINE_KEYS", "")
    outline_keys = tuple(k.strip() for k in outline_raw.split(",") if k.strip())

    # Кому пересылать вопросы из «Помощи». Пусто — спросим управляющих у VK.
    admins_raw = _optional("SUPPORT_ADMIN_IDS", "")
    support_admin_ids: tuple[int, ...] = tuple(
        int(part.strip())
        for part in admins_raw.replace(";", ",").split(",")
        if part.strip().lstrip("-").isdigit()
    )

    # Главный админ: все тикеты идут только ему; в боте видит раздел «Админ».
    main_admin_username = _optional("MAIN_ADMIN_USERNAME", "xylophaze").lstrip("@")
    main_admin_raw = _optional("MAIN_ADMIN_VK_ID", "634094665")
    try:
        main_admin_vk_id = int(main_admin_raw) if main_admin_raw else 0
    except ValueError as exc:
        raise RuntimeError("MAIN_ADMIN_VK_ID должен быть целым числом") from exc

    vpn_key_type = _optional("VPN_KEY_TYPE", "vless").lower()
    if vpn_key_type not in {"vless", "outline", "wireguard"}:
        raise RuntimeError("VPN_KEY_TYPE должен быть: vless | outline | wireguard")

    database_url = _optional(
        "DATABASE_URL",
        f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'vpn_bot.db'}",
    )

    return Settings(
        vk_token=_require("VK_TOKEN"),
        group_id=group_id,
        database_url=database_url,
        trial_days=trial_days,
        trial_traffic_gb=trial_traffic_gb,
        trial_devices=trial_devices,
        vpn_key_type=vpn_key_type,
        support_url=_optional("SUPPORT_URL", ""),
        support_text=_optional(
            "SUPPORT_TEXT",
            "Напишите в поддержку сообщества — поможем с подключением.",
        ),
        support_admin_ids=support_admin_ids,
        main_admin_vk_id=main_admin_vk_id,
        main_admin_username=main_admin_username,
        vless_template=_optional(
            "VLESS_TEMPLATE",
            "vless://{uuid}@vpn.example.com:443?encryption=none&security=reality"
            "&type=tcp#Bedolaga-VK-{user_id}",
        ),
        outline_keys=outline_keys,
        bot_name=_optional("BOT_NAME", "Бедолага VPN"),
        welcome_text=_optional(
            "WELCOME_TEXT",
            "Привет! Я помогу подключить быстрый и стабильный VPN за пару кликов.",
        ),
        bedolaga_api_url=_optional(
            "BEDOLAGA_API_URL",
            "https://cabinet.paskod.ru/api",
        ),
        bedolaga_api_key=_optional("BEDOLAGA_API_KEY", ""),
        cabinet_url=_optional("CABINET_URL", "https://cabinet.paskod.ru"),
        cabinet_jwt_secret=_optional("CABINET_JWT_SECRET", ""),
        renew_days=renew_days,
    )


# Глобальный экземпляр настроек (ленивая инициализация через get_settings)
_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton-доступ к настройкам."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings() -> Settings:
    """Перечитывает .env (после обновления секретов)."""
    global _settings
    load_dotenv(BASE_DIR / ".env", override=True)
    _settings = load_settings()
    return _settings
