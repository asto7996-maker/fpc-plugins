"""Сопоставление категорий FunPay → Starvell."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

from config import BASE_DIR

logger = logging.getLogger("starvell.category_mapper")

MAP_PATH = BASE_DIR / "storage" / "funpay_category_map.json"

STARVELL_SEARCH_URL = "https://starvell.com/api/games/search/by-keyword"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# FunPay node_id → Starvell (проверенные соответствия)
BUILTIN_FUNPAY_NODES: dict[int, dict[str, Any]] = {
    703: {
        "category_id": 175,
        "game_id": 14,
        "game_slug": "telegram",
        "category_slug": "services",
        "title": "Услуги Telegram",
    },
}

SECTION_FIRST = {
    "услуги", "прочее", "аккаунты", "аккаунт", "валюта", "предметы", "ключи",
    "буст", "обучение", "квесты", "продажа", "покупка", "аренда", "верификация",
    "накрутка", "реакции", "подписчики", "лайки", "просмотры", "рефералы",
    "безопасная", "смена", "донат", "coins", "skins", "vip", "premium",
}

CATEGORY_ALIASES: dict[str, list[str]] = {
    "услуги": ["услуги", "services", "smm", "накрутка", "продвижение", "boost"],
    "прочее": ["прочее", "other", "разное", "misc"],
    "аккаунты": ["аккаунты", "accounts", "аккаунт", "account"],
    "каналы": ["каналы", "channels"],
    "premium": ["premium", "премиум", "prem"],
    "звёзды": ["звёзды", "stars", "звезды", "star"],
    "подарки": ["подарки", "gifts", "gift"],
    "подписчики": ["подписчики", "followers", "subs", "subscriber"],
}


@dataclass
class StarvellCategoryMatch:
    category_id: int
    game_id: int
    game_name: str
    category_name: str
    funpay_node_id: int
    funpay_title: str
    confidence: float
    source: str
    game_slug: str = ""
    category_slug: str = ""


def _norm(text: str) -> str:
    text = (text or "").lower().strip()
    text = text.replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def split_funpay_category_title(title: str) -> tuple[str, str]:
    """Разбивает «Услуги Telegram» → (услуги, Telegram)."""
    title = (title or "").strip()
    if not title:
        return "", ""
    parts = title.split()
    if len(parts) >= 2 and parts[0].lower() in SECTION_FIRST:
        return parts[0], " ".join(parts[1:])
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return title, ""


def _similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _category_score(section: str, category_name: str) -> float:
    section_n = _norm(section)
    cat_n = _norm(category_name)
    if section_n == cat_n:
        return 1.0
    aliases = CATEGORY_ALIASES.get(section_n, [section_n])
    return max(_similarity(alias, cat_n) for alias in aliases)


def _load_map() -> dict[str, dict[str, Any]]:
    if not MAP_PATH.exists():
        return {}
    try:
        with open(MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("funpay map read: %s", exc)
        return {}


def save_category_mapping(node_id: int, match: StarvellCategoryMatch) -> None:
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _load_map()
    data[str(node_id)] = {
        "category_id": match.category_id,
        "game_id": match.game_id,
        "title": match.funpay_title,
        "game_name": match.game_name,
        "category_name": match.category_name,
    }
    tmp = MAP_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(MAP_PATH)


async def search_starvell_games(keyword: str) -> list[dict[str, Any]]:
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(STARVELL_SEARCH_URL, params={"keyword": keyword})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("starvell search %r: %s", keyword, exc)
        return []

    games: list[dict[str, Any]] = []
    if isinstance(data, list):
        for block in data:
            if isinstance(block, dict):
                games.extend(block.get("games") or [])
    return [g for g in games if isinstance(g, dict)]


async def resolve_starvell_category(
    funpay_node_id: int,
    funpay_title: str,
    *,
    settings_overrides: dict[str, Any] | None = None,
) -> StarvellCategoryMatch | None:
    """Находит category_id Starvell по категории FunPay."""
    if funpay_node_id <= 0:
        return None

    node_key = str(funpay_node_id)

    # 1) Пользовательские overrides из settings
    overrides = settings_overrides or {}
    if node_key in overrides:
        raw = overrides[node_key]
        if isinstance(raw, dict) and raw.get("category_id"):
            return StarvellCategoryMatch(
                category_id=int(raw["category_id"]),
                game_id=int(raw.get("game_id") or 0),
                game_name=str(raw.get("game_name") or ""),
                category_name=str(raw.get("category_name") or funpay_title),
                funpay_node_id=funpay_node_id,
                funpay_title=funpay_title,
                confidence=1.0,
                source="settings",
            )
        if isinstance(raw, int):
            return StarvellCategoryMatch(
                category_id=raw,
                game_id=0,
                game_name="",
                category_name=funpay_title,
                funpay_node_id=funpay_node_id,
                funpay_title=funpay_title,
                confidence=1.0,
                source="settings",
            )

    # 2) Локальный кеш
    cached = _load_map().get(node_key)
    if cached and cached.get("category_id"):
        return StarvellCategoryMatch(
            category_id=int(cached["category_id"]),
            game_id=int(cached.get("game_id") or 0),
            game_name=str(cached.get("game_name") or ""),
            category_name=str(cached.get("category_name") or funpay_title),
            funpay_node_id=funpay_node_id,
            funpay_title=funpay_title,
            confidence=1.0,
            source="cache",
        )

    # 3) Встроенная таблица
    builtin = BUILTIN_FUNPAY_NODES.get(funpay_node_id)
    if builtin:
        return StarvellCategoryMatch(
            category_id=int(builtin["category_id"]),
            game_id=int(builtin.get("game_id") or 0),
            game_name=str(builtin.get("game_name") or ""),
            category_name=str(builtin.get("title") or funpay_title),
            funpay_node_id=funpay_node_id,
            funpay_title=funpay_title,
            confidence=0.99,
            source="builtin",
            game_slug=str(builtin.get("game_slug") or ""),
            category_slug=str(builtin.get("category_slug") or ""),
        )

    section, game = split_funpay_category_title(funpay_title)
    if not game:
        game = funpay_title
        section = "услуги" if "услуг" in _norm(funpay_title) else "прочее"

    # 4) Поиск по каталогу Starvell
    search_terms = [game, funpay_title, f"{game} {section}", section]
    seen_ids: set[int] = set()
    best: StarvellCategoryMatch | None = None

    for term in search_terms:
        if not term.strip():
            continue
        for g in await search_starvell_games(term):
            gid = g.get("id")
            if gid in seen_ids:
                continue
            seen_ids.add(gid)

            game_name = str(g.get("name") or "")
            game_score = _similarity(game, game_name)
            if game_score < 0.55 and _norm(game) not in _norm(game_name):
                tags = g.get("tags") or []
                tag_hit = any(_norm(game) in _norm(str(t)) for t in tags)
                if not tag_hit:
                    continue

            for cat in g.get("categories") or []:
                if not isinstance(cat, dict) or not cat.get("id"):
                    continue
                cat_name = str(cat.get("name") or "")
                score = _category_score(section, cat_name) * 0.6 + game_score * 0.4
                if score < 0.62:
                    continue
                candidate = StarvellCategoryMatch(
                    category_id=int(cat["id"]),
                    game_id=int(gid),
                    game_name=game_name,
                    category_name=cat_name,
                    funpay_node_id=funpay_node_id,
                    funpay_title=funpay_title,
                    confidence=score,
                    source="search",
                    game_slug=str(g.get("slug") or ""),
                    category_slug=str(cat.get("slug") or ""),
                )
                if best is None or candidate.confidence > best.confidence:
                    best = candidate

    if best and best.confidence >= 0.75:
        try:
            save_category_mapping(funpay_node_id, best)
        except Exception as exc:
            logger.debug("save map: %s", exc)
    return best
