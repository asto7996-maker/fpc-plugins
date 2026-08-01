"""Premium custom emoji helpers for the tgmacicons pack.

HTML: ``<tg-emoji emoji-id="...">⭐️</tg-emoji>``
Buttons: pass ``icon_custom_emoji_id`` only — never duplicate unicode in text.
Pack: https://t.me/addemoji/tgmacicons
"""

from __future__ import annotations

import re
from typing import Optional

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    KeyboardButton,
    LoginUrl,
    SwitchInlineQueryChosenChat,
    WebAppInfo,
)


# Primary IDs from https://t.me/addemoji/tgmacicons
TGMACICONS: dict[str, str] = {
    '⭐️': '5258185631355378853',
    '⭐': '5258185631355378853',
    '✨': '5258185631355378853',
    '👤': '5258362837411045098',
    '👥': '5258513401784573443',
    '💰': '5258204546391351475',
    '🪙': '5258368777350816286',
    '💳': '5258204546391351475',
    '💵': '5258368777350816286',
    '📝': '5257965174979042426',
    '💡': '5258216851472654189',
    '⚡️': '5258152182150077732',
    '⚡': '5258152182150077732',
    '🔒': '5258476306152038031',
    '📄': '5258477770735885832',
    '📖': '5258328383183396223',
    '✅': '5260726538302660868',
    '❌': '5258226313285607065',
    '❗️': '5258474669769497337',
    '⚠️': '5258474669769497337',
    '↗️': '5257991477358763590',
    '📣': '5260268501515377807',
    '🔄': '5258420634785947640',
    '💼': '5258260149037965799',
    '💻': '5258423306255604960',
    '💬': '5258215846450305872',
    '📦': '5258134813302332906',
    'ℹ️': '5258503720928288433',
    'ℹ': '5258503720928288433',
    '🏠': '5257963315258204021',
    '🏘': '5257963315258204021',
    '👋': '5258501105293205250',
    '📱': '5258423306255604960',
    '🤝': '5258513401784573443',
    '🛠️': '5258096772776991776',
    '🛠': '5258096772776991776',
    '⚙': '5258096772776991776',
    '💎': '5359719332542718652',
    '➡️': '5260450573768990626',
    '⬅️': '5258236805890710909',
    '➕': '5258108352008823107',
    '🔎': '5429571366384842791',
    '❓': '5429571366384842791',
    '📌': '5258461531464539536',
    '📍': '5258509201306557640',
    '🗓': '5258105663359294787',
    '🕔': '5258419835922030550',
    '👀': '5260341314095947411',
    '❤️': '5258179403652801593',
    '🚀': '5258152182150077732',
    '🎁': '5258368777350816286',
    '🟢': '5260726538302660868',
    '🔴': '5258226313285607065',
    '🟡': '5258474669769497337',
    '⚫': '5258226313285607065',
    '⏳': '5258258882022612173',
    '📜': '5258477770735885832',
    '✍️': '5258331647358540449',
    '🎫': '5258331647358540449',
    '📂': '5258514780469075716',
    '📋': '5258477770735885832',
    '📈': '5258391025281408576',
    '📊': '5258391025281408576',
    '📞': '5258020476977946656',
    '🛟': '5258215846450305872',
    '🛡️': '5258476306152038031',
    '🛡': '5258476306152038031',
    '🎯': '5296348778012361146',
    '📚': '5260512129240276089',
    '🔖': '5359629206948976159',
    '📰': '5249231689695115145',
    # Aliases used in subscription/trial notifications (closest tgmacicons)
    '⛔': '5258226313285607065',  # same as ❌
    '🚫': '5258226313285607065',
    '🔧': '5258096772776991776',  # same as 🛠️
    '🔥': '5258152182150077732',  # same as ⚡
    '⏰': '5258258882022612173',  # same as ⏳
    '⏱': '5258258882022612173',
    '📅': '5258105663359294787',  # same as 🗓
    '💤': '5258258882022612173',
    '🎉': '5258185631355378853',  # same as ⭐
    '🗑️': '5258226313285607065',
    '🗑': '5258226313285607065',
    '🔑': '5258476306152038031',  # same as 🔒
    '📡': '5258503720928288433',  # same as ℹ️
    '✖️': '5258226313285607065',
    '✖': '5258226313285607065',
    '▶️': '5258152182150077732',
    '⏸': '5258258882022612173',
    '⏸️': '5258258882022612173',
    '👛': '5258204546391351475',  # same as 💳
    '🧪': '5258368777350816286',  # same as 🎁
    '🌍': '5258509201306557640',  # same as 📍
    '⌛': '5258258882022612173',
}

# Semantic button icon IDs (same pack) — one premium emoji per button
ICON = {
    'accept': '5260726538302660868',
    'decline': '5258226313285607065',
    'check': '5260726538302660868',
    'cross': '5258226313285607065',
    'profile': '5258362837411045098',
    'user': '5258362837411045098',
    'connect': '5258152182150077732',
    'lightning': '5258152182150077732',
    'referral': '5258513401784573443',
    'users': '5258513401784573443',
    'privacy': '5258476306152038031',
    'lock': '5258476306152038031',
    'shield': '5258476306152038031',
    'agreement': '5258477770735885832',
    'doc': '5258477770735885832',
    'rules': '5258328383183396223',
    'balance': '5258204546391351475',
    'support': '5258215846450305872',
    'lifebuoy': '5258215846450305872',
    'contact': '5258020476977946656',
    'info': '5258503720928288433',
    'home': '5257963315258204021',
    'cabinet': '5258423306255604960',
    'faq': '5429571366384842791',
    'search': '5429571366384842791',
    'back': '5258236805890710909',
    'ticket': '5258331647358540449',
    'tickets': '5258514780469075716',
    'status': '5258391025281408576',
    'promo': '5296348778012361146',
    'offer': '5249231689695115145',
    'bookmark': '5359629206948976159',
    'buy': '5258152182150077732',  # ⚡ buy / renew CTA
    'renew': '5258152182150077732',
    'extend': '5258152182150077732',
    'topup': '5258204546391351475',  # 💳 balance top-up
    'wallet': '5258204546391351475',
    'tools': '5258096772776991776',
    'warning': '5258474669769497337',
    'gift': '5258368777350816286',
    'diamond': '5359719332542718652',
}


# Explicit unicode escapes — avoids broken char-range literals across encodings.
_LEADING_EMOJI_RE = re.compile(
    r'^'
    r'['
    r'\u2100-\u214F'  # Letterlike
    r'\u2190-\u21FF'  # Arrows
    r'\u2300-\u23FF'  # Misc Technical
    r'\u2460-\u24FF'  # Enclosed Alphanumerics
    r'\u25A0-\u25FF'  # Geometric Shapes
    r'\u2600-\u27BF'  # Misc Symbols + Dingbats
    r'\u2900-\u297F'  # Supplemental Arrows-B
    r'\u2B00-\u2BFF'  # Misc Symbols and Arrows
    r'\u3030\u303D\u3297\u3299'
    r'\U0001F000-\U0001FFFF'
    r']'
    r'(?:'
    r'[\uFE0F\u200D\U0001F3FB-\U0001F3FF]'
    r'|'
    r'['
    r'\u2100-\u214F\u2190-\u21FF\u2300-\u23FF\u2460-\u24FF'
    r'\u25A0-\u25FF\u2600-\u27BF\u2900-\u297F\u2B00-\u2BFF'
    r'\u3030\u303D\u3297\u3299\U0001F000-\U0001FFFF'
    r']'
    r')*'
    r'\s*'
)


def clean_button_text(text: str) -> str:
    """Strip leading unicode emoji so only ``icon_custom_emoji_id`` is shown."""
    if not text:
        return text
    cleaned = _LEADING_EMOJI_RE.sub('', text, count=1).strip()
    return cleaned or text.strip()


# Backwards-compatible alias used by miniapp / cabinet builders
strip_leading_emoji = clean_button_text


def pe(emoji_or_name: str, fallback: str | None = None) -> str:
    """Wrap unicode emoji or ICON name into a premium ``<tg-emoji>`` HTML tag."""
    emoji_id = TGMACICONS.get(emoji_or_name) or ICON.get(emoji_or_name)
    if not emoji_id:
        return fallback if fallback is not None else emoji_or_name
    display = fallback if fallback is not None else (
        emoji_or_name if emoji_or_name in TGMACICONS else '•'
    )
    return f'<tg-emoji emoji-id="{emoji_id}">{display}</tg-emoji>'


def icon_id(name: str) -> str:
    """Return custom emoji id for a named button icon."""
    return ICON.get(name, '') or TGMACICONS.get(name, '')


_EMOJI_PATTERN = re.compile(
    '|'.join(sorted((re.escape(e) for e in TGMACICONS), key=len, reverse=True))
)
_TG_EMOJI_BLOCK_RE = re.compile(r'<tg-emoji\b[^>]*>.*?</tg-emoji>', re.IGNORECASE | re.DOTALL)


def inject_premium_emojis(text: str) -> str:
    """Replace bare unicode emojis with premium ``<tg-emoji>`` tags."""
    if not text:
        return text

    parts: list[str] = []
    last = 0
    for block in _TG_EMOJI_BLOCK_RE.finditer(text):
        parts.append(_replace_bare(text[last : block.start()]))
        parts.append(block.group(0))
        last = block.end()
    parts.append(_replace_bare(text[last:]))
    return ''.join(parts)


def _replace_bare(chunk: str) -> str:
    if not chunk:
        return chunk
    return _EMOJI_PATTERN.sub(lambda m: pe(m.group(0)), chunk)


def premium_button(
    text: str,
    *,
    icon: Optional[str] = None,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    web_app: Optional[WebAppInfo] = None,
    login_url: Optional[LoginUrl] = None,
    switch_inline_query: Optional[str] = None,
    switch_inline_query_current_chat: Optional[str] = None,
    switch_inline_query_chosen_chat: Optional[SwitchInlineQueryChosenChat] = None,
    copy_text: Optional[CopyTextButton] = None,
    style: Optional[str] = None,
    pay: Optional[bool] = None,
    icon_custom_emoji_id: Optional[str] = None,
) -> InlineKeyboardButton:
    """Inline button with exactly one premium emoji (via icon_custom_emoji_id)."""
    kwargs = {
        'text': clean_button_text(text),
        'callback_data': callback_data,
        'url': url,
        'web_app': web_app,
        'login_url': login_url,
        'switch_inline_query': switch_inline_query,
        'switch_inline_query_current_chat': switch_inline_query_current_chat,
        'switch_inline_query_chosen_chat': switch_inline_query_chosen_chat,
        'copy_text': copy_text,
        'style': style,
        'pay': pay,
    }
    eid = icon_custom_emoji_id or (icon_id(icon) if icon else '')
    if eid:
        kwargs['icon_custom_emoji_id'] = eid
    return InlineKeyboardButton(**{k: v for k, v in kwargs.items() if v is not None})


def premium_reply_button(text: str, *, icon: Optional[str] = None, **kwargs) -> KeyboardButton:
    """Reply keyboard button with one premium emoji."""
    payload = dict(kwargs)
    payload['text'] = clean_button_text(text)
    eid = icon_id(icon) if icon else ''
    if eid:
        payload['icon_custom_emoji_id'] = eid
    return KeyboardButton(**payload)
