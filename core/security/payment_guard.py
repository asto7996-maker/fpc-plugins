"""
Anti-Abuse: детекция поддельных «системных» сообщений об оплате в чате.
Реальные оплаты — только через API заказов Starvell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Паттерны имитации оплаты в тексте чата
_FAKE_PAYMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"оплат[аа].*получен",
        r"плат[её]ж.*подтвержд",
        r"заказ.*оплачен",
        r"payment\s+received",
        r"order\s+paid",
        r"перевод.*зачислен",
        r"средства.*зачислен",
        r"✅.*оплат",
        r"💰.*оплат",
        r"системн.*уведомлен",
        r"\[system\].*paid",
        r"starvell.*оплат.*успеш",
    )
]


@dataclass(frozen=True)
class PaymentGuardResult:
    is_suspicious: bool
    reason: str = ""
    matched_pattern: str = ""


class PaymentGuard:
    """Проверяет текст сообщений на попытки имитации оплаты."""

    def __init__(self, extra_patterns: list[str] | None = None) -> None:
        self._patterns = list(_FAKE_PAYMENT_PATTERNS)
        for raw in extra_patterns or []:
            try:
                self._patterns.append(re.compile(raw, re.IGNORECASE | re.UNICODE))
            except re.error:
                pass

    def inspect(self, text: str, *, author_is_system: bool = False) -> PaymentGuardResult:
        if not text or author_is_system:
            return PaymentGuardResult(False)

        normalized = text.strip()
        for pattern in self._patterns:
            if pattern.search(normalized):
                return PaymentGuardResult(
                    is_suspicious=True,
                    reason="Текст похож на поддельное уведомление об оплате",
                    matched_pattern=pattern.pattern,
                )
        return PaymentGuardResult(False)

    def format_admin_alert(self, username: str, chat_id: str, text: str) -> str:
        preview = text[:200].replace("<", "&lt;")
        user = username or "неизвестный"
        return (
            "⚡️ <b>КРИТИЧНО — Anti-Abuse</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ Попытка обхода платежной системы!\n\n"
            f"👤 Покупатель: <code>{user}</code>\n"
            f"💬 Чат: <code>{chat_id}</code>\n"
            f"📝 Сообщение:\n<i>{preview}</i>\n\n"
            f"<b>Действие:</b> оплата учитывается ТОЛЬКО через API Starvell."
        )
