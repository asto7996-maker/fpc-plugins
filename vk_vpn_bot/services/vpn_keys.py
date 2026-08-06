"""
Генерация VPN-ключей / ссылок конфигурации.

В продакшене здесь обычно вызывается API вашей панели
(3x-ui, Marzban, Outline Manager и т.д.).
Сейчас реализована локальная генерация демо/шаблонных ключей,
чтобы бот был полностью рабочим «из коробки».
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Literal

from config import Settings

KeyType = Literal["vless", "outline", "wireguard"]


def generate_vpn_key(user_id: int, settings: Settings) -> str:
    """
    Генерирует или выдаёт ключ нужного типа для пользователя.

    :param user_id: VK ID — используется в имени конфига / детерминизме
    :param settings: настройки из .env
    """
    key_type: KeyType = settings.vpn_key_type  # type: ignore[assignment]

    if key_type == "vless":
        return _generate_vless(user_id, settings)
    if key_type == "outline":
        return _generate_outline(user_id, settings)
    return _generate_wireguard(user_id)


def _generate_vless(user_id: int, settings: Settings) -> str:
    """VLESS Reality-ссылка по шаблону из .env."""
    client_uuid = str(uuid.uuid4())
    return settings.vless_template.format(uuid=client_uuid, user_id=user_id)


def _generate_outline(user_id: int, settings: Settings) -> str:
    """
    Outline ss:// ключ.
    Если в OUTLINE_KEYS задан пул — берём свободный по хэшу user_id.
    Иначе генерируем демо-ключ (для теста интерфейса).
    """
    if settings.outline_keys:
        idx = user_id % len(settings.outline_keys)
        return settings.outline_keys[idx]

    # Демо-ключ (не рабочий сервер — замените на реальный пул)
    secret = secrets.token_urlsafe(16)
    return (
        f"ss://Y2hhY2hhMjAtaWV0Zi1wb2x5MTMwNTp7{secret}"
        f"@outline.example.com:8388#Bedolaga-VK-{user_id}"
    )


def _generate_wireguard(user_id: int) -> str:
    """
    Текстовый WireGuard-конфиг.
    Приватный ключ — демо; в проде выдавайте с вашего WG-сервера.
    """
    # Детерминированный «ключ» на основе user_id (только для демо-вида)
    seed = hashlib.sha256(f"wg-{user_id}-{secrets.token_hex(8)}".encode()).hexdigest()
    private_key = secrets.token_urlsafe(32)
    address_octet = (user_id % 250) + 2

    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = 10.8.0.{address_octet}/32\n"
        "DNS = 1.1.1.1\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {seed[:43]}=\n"
        "Endpoint = vpn.example.com:51820\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        "PersistentKeepalive = 25"
    )


def mask_key(key: str, visible: int = 24) -> str:
    """Маскирует длинный ключ для безопасного превью в профиле."""
    key = key.strip()
    if len(key) <= visible + 3:
        return key
    return key[:visible] + "…"
