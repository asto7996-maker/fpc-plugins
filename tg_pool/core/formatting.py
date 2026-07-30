"""
Safe Telegram text formatting helpers (HTML / Markdown cleanup).
"""

from __future__ import annotations

import html
import re


_MD_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_HTML_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)(\s[^>]*)?>")
_ALLOWED_HTML = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "code",
    "pre",
    "a",
    "blockquote",
    "tg-spoiler",
}


def sanitize_html(text: str, *, escape_all: bool = False) -> str:
    """
    Escape unsafe HTML. If escape_all=True, escape everything.
    Otherwise keep a small Telegram-safe tag whitelist and escape the rest.
    """
    if text is None:
        return ""
    raw = str(text)
    if escape_all:
        return html.escape(raw, quote=False)

    out: list[str] = []
    pos = 0
    for m in _HTML_TAG_RE.finditer(raw):
        out.append(html.escape(raw[pos : m.start()], quote=False))
        tag = m.group(1).lower()
        if tag in _ALLOWED_HTML:
            out.append(m.group(0))
        else:
            out.append(html.escape(m.group(0), quote=False))
        pos = m.end()
    out.append(html.escape(raw[pos:], quote=False))
    return "".join(out)


def sanitize_markdown(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    if text is None:
        return ""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def strip_html_tags(text: str) -> str:
    """Remove all HTML tags (for plain-text previews)."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", str(text))
