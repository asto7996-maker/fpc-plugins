"""
mangabuff_service.py — Playwright-автоматизация mangabuff.ru.

Отдельный persistent context: user_data_mangabuff.

Возможности:
- автологин (email/password из env или CLI);
- фарм: популярные тайтлы из каталога, чтение большого числа глав;
- сбор наград/модалок/уведомлений/карточных дейликов;
- обход основных разделов сайта (макеты);
- редкие «живые» комментарии: 65% благодарности (>1000 вариантов), 35% похожие на чужие;
- ночной перерыв 4 часа, днём — круглосуточное чтение;
- человекоподобные задержки и скролл.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import json
import time as time_mod
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from config import BASE_DIR, Config

logger = logging.getLogger(__name__)

DEFAULT_START_URL = "https://mangabuff.ru/"
DEFAULT_USER_DATA = BASE_DIR / "user_data_mangabuff"
STATS_PATH = BASE_DIR / "mangabuff_stats.json"
MSK = ZoneInfo("Europe/Moscow")

TOP_URL = "https://mangabuff.ru/manga/top"
CATALOG_URL = "https://mangabuff.ru/manga"
LAYOUT_URLS = (
    "https://mangabuff.ru/",
    "https://mangabuff.ru/manga",
    "https://mangabuff.ru/manga/top",
    "https://mangabuff.ru/cards",
    "https://mangabuff.ru/battle",
    "https://mangabuff.ru/products",
    "https://mangabuff.ru/notifications",
    "https://mangabuff.ru/feed",
    "https://mangabuff.ru/updates",
    "https://mangabuff.ru/collections",
    "https://mangabuff.ru/market",
    "https://mangabuff.ru/quiz",
    "https://mangabuff.ru/genres",
)

# Эвенты / карты / сундуки / паки — быстрый обход для автофарма
EVENT_FARM_URLS = (
    "https://mangabuff.ru/",
    "https://mangabuff.ru/notifications",
    "https://mangabuff.ru/notifications?type=other",
    "https://mangabuff.ru/battle",
    "https://mangabuff.ru/battle/awakening",
    "https://mangabuff.ru/battle/reroll",
    "https://mangabuff.ru/cards",
    "https://mangabuff.ru/cards/pack",
    "https://mangabuff.ru/cards?scroll_enable=1",
    "https://mangabuff.ru/decks",
    "https://mangabuff.ru/products",
    "https://mangabuff.ru/transactions",
    "https://mangabuff.ru/quiz",
    "https://mangabuff.ru/promo-code",
    "https://mangabuff.ru/club",
)

# Карты за чтение приходят в ленту уведомлений (вкладки ВСЕ / ДРУГОЕ)
CARD_NOTIFY_URLS = (
    "https://mangabuff.ru/notifications",
    "https://mangabuff.ru/notifications?type=other",
)

# Текст сайта про дроп карт/свитков в уведомлениях и reader-тостах
_CARD_NOTIFY_RE = re.compile(
    r"(карточк|получил[аи]?\s+карт|получен[ао]?\s+карт|новая\s+карт|"
    r"вам\s+выпал|выпал[аи]?\s+карт|забрать\s+карт|свит(ок|ка|ки))",
    re.I,
)

# Лестница редкости MangaBuff (как getNextRank на сайте) + X выше S.
# Индекс больше = карта дороже / реже.
CARD_RANK_LADDER = ("E", "D", "C", "B", "G", "P", "A", "S", "X")
MARKET_LOTS_PATH = BASE_DIR / "market_lots.json"
# Лимит сайта: одновременно не больше 10 лотов — берём самые дорогие.
MARKET_LOT_LIMIT = 10
# Режим цены: higher = 1× ранг выше (для X — 2×X); same2 = 2× тот же ранг.
MARKET_MODE_HIGHER = "higher"
MARKET_MODE_SAME2 = "same2"
MARKET_REPRICE_AFTER = timedelta(hours=24)


def rank_value(rank: str) -> int:
    """Чем выше — тем дороже карта (X максимум)."""
    r = (rank or "").strip().upper()
    if r in CARD_RANK_LADDER:
        return CARD_RANK_LADDER.index(r)
    return -1


def card_value_key(row: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Ключ сортировки «дороже → дешевле» (для reverse=True)."""
    return (
        rank_value(str(row.get("rank") or "")),
        int(row.get("has_shadow") or 0),
        -int(row.get("copy_number") or 10**9),  # меньший номер экземпляра ценнее
        int(row.get("id") or 0),
    )


def next_higher_rank(rank: str) -> Optional[str]:
    """Следующий ранг выше (цена лота: 1 карта этого ранга)."""
    r = (rank or "").strip().upper()
    if r not in CARD_RANK_LADDER:
        return None
    i = CARD_RANK_LADDER.index(r)
    if i >= len(CARD_RANK_LADDER) - 1:
        return None
    return CARD_RANK_LADDER[i + 1]


def format_rank_label(rank: str) -> str:
    r = (rank or "").strip().upper()
    return f"[{r}]" if r else ""


def market_price_for_mode(card_rank: str, mode: str) -> Tuple[str, int]:
    """
    Цена лота по режиму.
    higher → 1× ранг выше; если выше нет (X и пр.) → 2× тот же ранг.
    same2 → 2× тот же ранг.
    """
    r = (card_rank or "").strip().upper() or "E"
    m = (mode or MARKET_MODE_HIGHER).strip().lower()
    if m == MARKET_MODE_SAME2:
        return r, 2
    higher = next_higher_rank(r)
    if higher:
        return higher, 1
    return r, 2


def _normalize_card_image(image: str) -> str:
    raw = (image or "").strip()
    if not raw:
        return ""
    raw = raw.split("?")[0]
    raw = raw.replace("\\/", "/")
    if "/img/cards/" in raw:
        raw = raw.split("/img/cards/")[-1]
    return raw.lstrip("/").lower()


def _parse_lot_price_text(text: str) -> Tuple[int, str]:
    """'1 G' / '2X' → (value, rank)."""
    m = re.search(r"(\d+)\s*([A-Za-z])", text or "")
    if not m:
        return 0, ""
    return int(m.group(1)), m.group(2).upper()

# Ночной перерыв: 01:00–05:00 МСК (ровно 4 часа)
NIGHT_BREAK_START = time(1, 0)
NIGHT_BREAK_END = time(5, 0)

# Комментарии каждые 5–15 глав (на текущую главу, без прыжков)
COMMENT_EVERY_MIN = 5
COMMENT_EVERY_MAX = 15
# Читать ровно до 90% тайтла
TITLE_READ_RATIO = 0.90
# Запасной потолок, если число глав неизвестно (идём пока есть «след. глава»)
TITLE_READ_HARD_CAP = 5000
COMMENT_WORDS_MIN = 5
COMMENT_WORDS_MAX = 20
# 65% — благодарности, 35% — похожие на чужие
THANKS_COMMENT_CHANCE = 0.65

# Интонации: внутри одного тайтла голос стабильный, между тайтлами — разный.
# Комменты — только ЦЕЛЬНЫЕ фразы (не склейка случайных слов).
_COMMENT_VOICES: Tuple[Dict[str, Any], ...] = (
    {
        "key": "soft",
        "thanks": (
            "спасибо за главу приятно читать",
            "спасибо за новую главу буду ждать дальше",
            "спасибо за эту главу очень мягко зашло",
            "спасибо автору за труды читаю с удовольствием",
            "ну спасибо за главу как то тепло стало",
            "спасибо за тайтл мне правда нравится",
            "благодарю за атмосферу тихо сижу и читаю дальше",
            "спасибо за историю надеюсь не пропадёте",
            "искренне спасибо за главу приятно читать",
            "спасибо вам за спокойный вайб",
            "спасибо за то что выкладываете буду ждать дальше",
            "ну спасибочки за главу очень мягко зашло",
            "спасибо за новую главу читаю с удовольствием",
            "благодарю за главу мне правда нравится",
            "спасибо автору за главу как то тепло стало",
            "спасибо за продолжение приятно читать",
            "кстати спасибо за главу буду ждать дальше",
            "если честно спасибо за тайтл очень зашло",
            "просто хочу сказать спасибо за эту главу",
            "мне кажется спасибо за атмосферу здесь очень кстати",
            "спасибо за главу тихо сижу и читаю дальше",
            "спасибо большое за новую главу приятно читать",
            "благодарю за труды надеюсь не пропадёте",
            "спасибо за серию очень мягко зашло",
            "ну спасибо за работу читаю с удовольствием",
        ),
        "reactions": {
            "general": (
                "глава мягко зашла и приятно читать дальше",
                "в целом очень спокойно и мне нравится",
                "пока всё аккуратно и читается легко",
                "мне зашло без крика просто приятно",
                "тихо скажу глава норм и вайб хороший",
                "если честно пока держит и нравится",
            ),
            "art": (
                "рисовка приятная и глаз отдыхает",
                "арт мягкий и смотрится очень тепло",
                "рисунки аккуратные мне правда нравится",
                "визуал спокойный и приятный глазу",
            ),
            "plot": (
                "сюжет идёт ровно и интересно дальше",
                "история мягко тянет и не отпускает",
                "линия развивается спокойно но уверенно",
                "по сюжету пока всё складывается красиво",
            ),
            "wait": (
                "уже жду следующую главу спокойно но верно",
                "буду ждать продолжение очень интересно",
                "жду дальше надеюсь темп сохранится",
                "интересно что будет дальше читаю дальше",
            ),
            "emotion": (
                "атмосфера тёплая и как то уютно стало",
                "эмоции тихие но очень цепляют",
                "настроение главы мягкое и приятное",
                "после главы стало как то тепло на душе",
            ),
        },
    },
    {
        "key": "hype",
        "thanks": (
            "огромное спасибо за главу это пушка",
            "от души спасибо за эту главу я в восторге",
            "реально спасибо за свежую главу огонь просто",
            "спасибо за такой тайтл уже жду следующую",
            "прям спасибо за вайб не отпускает вообще",
            "спасибо огромное за кайф глава топ",
            "респект и спасибо за эмоции так держать",
            "спасибо автору за главу читаю и ору от кайфа",
            "ооо спасибо за продолжение выдаёт железно",
            "блин спасибо за главу я в восторге",
            "ну наконец спасибо за свежую главу огонь",
            "реально спасибо за крутой тайтл пушка",
            "короче спасибо за главу уже жду следующую",
            "ребята спасибо за вайб не отпускает вообще",
            "огромное спасибо за эмоции глава топ",
            "от души спасибо за тайтл так держать",
            "спасибо за продолжение это пушка",
            "прям спасибо за главу огонь просто",
            "спасибо огромное за сюжет не отпускает",
            "респект и спасибо за главу выдаёт железно",
            "спасибо за новую главу я в восторге",
            "огромное спасибо за работу уже жду следующую",
            "от души спасибо за серию глава топ",
            "реально спасибо за труды огонь просто",
            "спасибо за обновление не отпускает вообще",
        ),
        "reactions": {
            "general": (
                "глава просто огонь и я в восторге",
                "это пушка реально не отпускает",
                "короче топ глава уже жду следующую",
                "вайб жесткий и читать одно удовольствие",
                "ну наконец уровень снова космос",
                "прям кайф от главы без вопросов",
            ),
            "art": (
                "рисовка огонь глаз радуется железно",
                "арт пушка и кадры просто космос",
                "рисунки топ смотреть одно удовольствие",
                "визуал жесткий и очень цепляет",
            ),
            "plot": (
                "сюжет разгоняется и держит мертво",
                "история огонь уже не могу оторваться",
                "повороты пушка и интрига жжёт",
                "линия событий топ не отпускает совсем",
            ),
            "wait": (
                "уже орём в пустоту где следующая глава",
                "жду следующую главу как не в себе",
                "скорее бы продолжение это жесть",
                "жду дроп дальше просто не могу",
            ),
            "emotion": (
                "эмоции через край и я в шоке",
                "после главы до сих пор под впечатлением",
                "напряг такой что мурашки пошли",
                "атмосфера огонь и вайб космос",
            ),
        },
    },
    {
        "key": "polite",
        "thanks": (
            "благодарю за новую главу творческих успехов",
            "огромная благодарность за работу удачи вам",
            "благодарю автора за труд это правда важно",
            "спасибо большое за главу буду следить дальше",
            "благодарю команду за работу хорошая работа",
            "сердечно благодарю за выпуск продолжайте пожалуйста",
            "благодарю за старания сил и вдохновения",
            "спасибо большое за обновление уважение вам",
            "честно говоря благодарю за главу творческих успехов",
            "хочу сказать спасибо за новую главу",
            "с уважением благодарю за труды удачи в работе",
            "отдельно благодарю за работу буду следить дальше",
            "спасибо переводчику за работу хорошая работа",
            "огромная благодарность автору за труд",
            "благодарю за главу это правда важно",
            "спасибо большое за труды творческих успехов",
            "благодарю за обновление продолжайте пожалуйста",
            "честно говоря спасибо за работу уважение вам",
            "хочу сказать огромная благодарность за главу",
            "с уважением спасибо за новую главу",
            "благодарю за серию сил и вдохновения",
            "огромная благодарность за выпуск удачи в работе",
            "спасибо большое за старания хорошая работа",
            "благодарю автора за главу буду следить дальше",
            "благодарю команду за труды творческих успехов",
        ),
        "reactions": {
            "general": (
                "глава получилась достойная и приятно читать",
                "работа аккуратная и уровень чувствуется",
                "в целом очень качественно и спокойно",
                "читаю с уважением к труду авторов",
                "хорошая глава без лишнего шума",
                "честно говоря глава вышла удачной",
            ),
            "art": (
                "рисовка аккуратная и выглядит профессионально",
                "арт приятный глазу и выполнен тщательно",
                "рисунки качественные спасибо за труд",
                "визуальная часть очень достойная",
            ),
            "plot": (
                "сюжет развивается последовательно и интересно",
                "история выстроена спокойно но уверенно",
                "линия повествования держит внимание",
                "по сюжету всё складывается логично",
            ),
            "wait": (
                "с интересом жду следующую главу",
                "буду следить за продолжением дальше",
                "надеюсь скоро выйдет новая глава",
                "жду обновление с большим интересом",
            ),
            "emotion": (
                "атмосфера главы очень располагает",
                "эмоции переданы сдержанно но сильно",
                "настроение после главы тёплое",
                "после прочтения осталось приятное чувство",
            ),
        },
    },
    {
        "key": "chill",
        "thanks": (
            "спс за главу норм зашло",
            "спасибо за тайтл жду дальше",
            "ну спс за серию пока ок",
            "спасибочки за часть без лишнего",
            "от души за вайб держите уровень",
            "респект за контент неплохо вообще",
            "спасибо бро за обновление пойдёт",
            "кста спасибо за главу зашло",
            "короче спс за тайтл жду дальше",
            "ладно спасибо за серию пока ок",
            "типа спасибо за главу норм",
            "а вообще спс за вайб зашло",
            "спасибо за главу без лишнего",
            "ну спасибо за тайтл держите уровень",
            "спс за обновление неплохо вообще",
            "спасибочки за часть пойдёт",
            "от души за главу жду дальше",
            "респект за серию пока ок",
            "кста спс за работу зашло",
            "короче спасибо за главу норм",
            "ладно спс за продолжение жду дальше",
            "спасибо за контент пока ок",
            "ну спс за труды неплохо вообще",
            "спс за новую главу зашло",
            "спасибо за выпуск пойдёт",
        ),
        "reactions": {
            "general": (
                "глава норм без лишнего шума",
                "в целом ок и зашло спокойно",
                "пока держит уровень и читать можно",
                "короче нормас жду что дальше будет",
                "хз но мне зашло нормально",
                "по тихому хорошая глава",
            ),
            "art": (
                "рисовка норм глазу приятно",
                "арт спокойный и без перегиба",
                "рисунки ок смотрится ровно",
                "визуал нормальный зашло",
            ),
            "plot": (
                "сюжет пока ок и тянет дальше",
                "история идёт ровно без просадок",
                "по сюжету нормас интересно дальше",
                "линия держится и не скучно",
            ),
            "wait": (
                "жду дальше без паники но жду",
                "будет продолжение почитаем",
                "жду следующую главу когда будет",
                "ладно жду дроп дальше",
            ),
            "emotion": (
                "вайб спокойный и зашёл нормально",
                "настроение главы ровное и ок",
                "атмосфера без крика но приятная",
                "после главы просто норм ощущения",
            ),
        },
    },
    {
        "key": "warm",
        "thanks": (
            "огромное спасибо за главу мне очень приятно",
            "от души спасибо за этот тайтл читаю и улыбаюсь",
            "спасибо автору за труды обнимаю автора мысленно",
            "спасибо большое за историю уже жду новую главу",
            "благодарю от сердца за эмоции сердцу хорошо",
            "спасибо что есть такой тайтл вы реально свет",
            "ой спасибо за главу аж до слёз почти",
            "ну вот спасибо за атмосферу мне очень приятно",
            "как же спасибо за историю читаю и улыбаюсь",
            "честно спасибо за эмоции сердцу хорошо",
            "просто спасибо автору за труды вы реально свет",
            "слушайте спасибо за главу уже жду новую главу",
            "огромное спасибо за тайтл спасибо что радуете",
            "от души спасибо за тепло в главе",
            "спасибо за такую историю мне очень приятно",
            "благодарю от сердца за главу читаю и улыбаюсь",
            "спасибо что радуете уже жду новую главу",
            "ой спасибо за работу сердцу хорошо",
            "ну вот спасибо за серию обнимаю автора мысленно",
            "честно спасибо за новую главу вы реально свет",
            "спасибо большое за продолжение мне очень приятно",
            "огромное спасибо за старания читаю и улыбаюсь",
            "от души спасибо за выпуск уже жду новую главу",
            "спасибо автору за главу сердцу хорошо",
            "спасибо за обновление спасибо что радуете",
        ),
        "reactions": {
            "general": (
                "глава очень тёплая и зашла прямо в душу",
                "читаю и улыбаюсь такой уютный вайб",
                "очень по человечески и приятно стало",
                "после главы на душе реально светлее",
                "мне так зашло что сижу и улыбаюсь",
                "просто тепло и хорошо от этой главы",
            ),
            "art": (
                "рисовка тёплая и очень милая",
                "арт уютный глаз радуется от души",
                "рисунки нежные и прямо в сердце",
                "визуал тёплый смотреть одно удовольствие",
            ),
            "plot": (
                "история трогает и тянет дальше",
                "сюжет тёплый и очень человечный",
                "линия событий трогательная и живая",
                "по сюжету всё очень душевно вышло",
            ),
            "wait": (
                "уже жду новую главу всем сердцем",
                "буду ждать продолжение очень тепло",
                "скорее бы следующая глава согреться снова",
                "жду дальше с улыбкой на лице",
            ),
            "emotion": (
                "эмоции тёплые аж до мурашек",
                "настроение после главы очень светлое",
                "атмосфера уютная и родная",
                "после прочтения стало по настоящему тепло на душе",
            ),
        },
    },
    {
        "key": "slang",
        "thanks": (
            "красава спасибо за главу база",
            "от души за тайтл имба",
            "спасибо жирно за вайб жестко зашло",
            "ну респект за кайф не слабо",
            "апвоут и спасибо за арт жду дроп дальше",
            "спс огромное за сюжет чисто огонь",
            "йо спасибо за главу я в деле дальше",
            "ну кароч респект за тайтл база",
            "бро спасибо за вайб имба",
            "реально же от души за главу жестко зашло",
            "база спасибо за рисовку держи планку",
            "честно спс огромное за главу чисто огонь",
            "красава спасибо за продолжение жду дроп дальше",
            "от души за серию не слабо",
            "спасибо жирно за обновление база",
            "ну респект за труды имба",
            "йо от души за главу жестко зашло",
            "бро спс огромное за тайтл чисто огонь",
            "реально же спасибо за вайб я в деле дальше",
            "красава респект за сюжет держи планку",
            "от души за новую главу база",
            "спасибо жирно за работу имба",
            "ну респект за выпуск жестко зашло",
            "спс огромное за главу жду дроп дальше",
            "йо спасибо за серию чисто огонь",
        ),
        "reactions": {
            "general": (
                "глава база и жестко зашла",
                "ну кароч имба без вопросов",
                "вайб чистый огонь я в деле",
                "бро это реально не слабо",
                "короче топ глава держит планку",
                "честно кайф от главы полный",
            ),
            "art": (
                "рисовка имба глаз кайфует",
                "арт база и выглядит жирно",
                "рисунки огонь чисто по делу",
                "визуал жесткий и очень вкусный",
            ),
            "plot": (
                "сюжет база и разгон жёсткий",
                "история имба уже не оторвать",
                "повороты огонь интрига держит",
                "линия событий реально не слабо",
            ),
            "wait": (
                "жду дроп дальше уже не могу",
                "скорее бы следующую главу бро",
                "жду продолжение это база",
                "ну кароч жду дроп как не в себе",
            ),
            "emotion": (
                "эмоции жесткие я подсел",
                "атмосфера имба и вайб космос",
                "после главы до сих пор в кайфе",
                "напряг такой что мурашит жестко",
            ),
        },
    },
    {
        "key": "quiet",
        "thanks": (
            "тихо скажу спасибо за главу читаю дальше",
            "спасибо автору за серию без шума но ценю",
            "ну спасибо за работу мне достаточно",
            "тихое спасибо за старания спокойно зашло",
            "спасибо небольшое но искреннее за продолжение",
            "благодарю за тайтл буду рядом",
            "просто спасибо за главу просто нравится",
            "между прочим спасибо за аккуратную работу",
            "ну да спасибо за серию читаю дальше",
            "тихо скажу спасибо за главу спокойно зашло",
            "спасибо автору за работу без шума но ценю",
            "благодарю за главу не кричу но спасибо",
            "спасибо за продолжение мне достаточно",
            "тихое спасибо за тайтл буду рядом",
            "просто спасибо за серию читаю дальше",
            "ну спасибо за обновление спокойно зашло",
            "спасибо за главу без шума но ценю",
            "благодарю за труды просто нравится",
            "тихо скажу спасибо за новую главу",
            "спасибо небольшое но искреннее за работу",
            "ну да спасибо за главу буду рядом",
            "спасибо автору за главу читаю дальше",
            "благодарю за выпуск спокойно зашло",
            "просто спасибо за старания мне достаточно",
            "спасибо за серию не кричу но спасибо",
        ),
        "reactions": {
            "general": (
                "глава спокойно зашла и мне хватает",
                "без шума но уровень чувствуется",
                "тихо скажу мне правда нравится",
                "читаю дальше всё ровно и хорошо",
                "пока всё аккуратно и по делу",
                "мне достаточно такого темпа",
            ),
            "art": (
                "рисовка спокойная и приятная глазу",
                "арт аккуратный без лишнего шума",
                "рисунки ровные и смотрятся мягко",
                "визуал тихий но очень приятный",
            ),
            "plot": (
                "сюжет идёт ровно и без суеты",
                "история развивается спокойно но верно",
                "линия держится и читать интересно",
                "по сюжету всё складывается тихо и точно",
            ),
            "wait": (
                "буду ждать дальше без лишнего шума",
                "жду продолжение спокойно но жду",
                "интересно что будет дальше читаю",
                "жду новую главу в своём темпе",
            ),
            "emotion": (
                "атмосфера тихая и очень цепляет",
                "эмоции сдержанные но сильные",
                "настроение главы спокойное и тёплое",
                "после главы осталось тихое тепло",
            ),
        },
    },
    {
        "key": "fan",
        "thanks": (
            "огромное спасибо команде за главу вы лучшие",
            "спасибо автору за историю я с вами",
            "благодарю команду за труды поддерживаю вас",
            "спасибо переводчику за главу не останавливайтесь пожалуйста",
            "респект команде за тайтл фанатею дальше",
            "как фанат скажу спасибо за стабильные главы",
            "поддерживаю спасибо за то что не бросаете",
            "не могу не написать спасибо за труды вы супер",
            "обязательно спасибо за главу я с вами",
            "ну всё спасибо команде за работу вы лучшие",
            "огромное спасибо за тайтл поддерживаю вас",
            "спасибо автору за главу фанатею дальше",
            "благодарю команду за выпуск вы супер",
            "спасибо за то что радуете я с вами",
            "респект команде за долгую работу",
            "как фанат скажу спасибо за новую главу",
            "поддерживаю спасибо автору за историю",
            "не могу не написать спасибо за главу",
            "спасибо команде за труды не останавливайтесь пожалуйста",
            "огромное спасибо за стабильные главы вы лучшие",
            "спасибо за продолжение фанатею дальше",
            "благодарю за работу поддерживаю вас",
            "спасибо переводчику за труды вы супер",
            "респект команде за обновление я с вами",
            "спасибо за серию я с вами до конца",
        ),
        "reactions": {
            "general": (
                "как фанат скажу глава снова радует",
                "поддерживаю вас уровень на месте",
                "читаю и горжусь что слежу за тайтлом",
                "вы держите планку и это видно",
                "ну всё я снова в восторге от главы",
                "тайтл живой и это очень ценно",
            ),
            "art": (
                "рисовка как всегда радует фанатов",
                "арт на уровне и глаз не отвести",
                "рисунки снова огонь поддерживаю",
                "визуал радует выпуск за выпуском",
            ),
            "plot": (
                "сюжет развивается и фанаты довольны",
                "история держит и я с вами до конца",
                "линия событий снова цепляет сильно",
                "по сюжету всё идёт как надо",
            ),
            "wait": (
                "жду следующую главу уже вовсю",
                "буду ждать продолжение как всегда",
                "скорее бы новую главу я на низком старте",
                "жду обновление и морально поддерживаю",
            ),
            "emotion": (
                "эмоции снова накрыли по полной",
                "атмосфера родная и очень цепляет",
                "после главы сижу и перевариваю кайф",
                "вайб тайтла всё так же греет",
            ),
        },
    },
)

# Только однословные замены внутри УЖЕ цельной фразы.
# Нельзя раздувать слово в словосочетание — иначе ломается смысл.
_COMMENT_SYNONYMS = {
    "норм": ("нормально", "норм", "ок", "нормас"),
    "нормально": ("норм", "нормально", "ок"),
    "ок": ("норм", "ок", "нормально"),
    "огонь": ("огонь", "кайф", "топ", "круто"),
    "кайф": ("кайф", "огонь", "топ"),
    "топ": ("топ", "огонь", "круто"),
    "круто": ("круто", "топ", "огонь"),
    "зашло": ("зашло", "заходит"),
    "заходит": ("заходит", "зашло"),
    "интересно": ("интересно", "занятно"),
    "занятно": ("занятно", "интересно"),
    "приятно": ("приятно", "мило"),
    "мило": ("мило", "приятно"),
    "спокойно": ("спокойно", "ровно"),
    "ровно": ("ровно", "спокойно"),
}


@dataclass
class MangaBuffStats:
    running: bool = False
    chapters_read: int = 0
    pages_scrolled: int = 0
    rewards_claimed: int = 0
    cards_claimed: int = 0
    comments_posted: int = 0
    titles_visited: int = 0
    layouts_visited: int = 0
    events_actions: int = 0
    chests_opened: int = 0
    packs_opened: int = 0
    scrolls_claimed: int = 0
    errors: int = 0
    chapters_pending: int = 0
    last_url: str = ""
    last_action: str = ""
    last_at: str = ""
    started_at: str = ""
    night_break_until: str = ""
    last_card_drop: str = ""
    # Ключи глав, куда уже писали комментарий: "slug/vol/ch" — строго 1 на главу
    commented_chapters: List[str] = field(default_factory=list)
    # Уже обработанные уведомления сайта (id/hash) — без повторных TG-алертов
    seen_notifications: List[str] = field(default_factory=list)

    def touch(self, action: str, url: str = "") -> None:
        self.last_action = action
        self.last_at = datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S")
        if url:
            self.last_url = url

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MangaBuffStats":
        known = {f.name for f in fields(cls)}
        cleaned = {k: v for k, v in (data or {}).items() if k in known}
        # running всегда сбрасываем при загрузке с диска
        cleaned["running"] = False
        ch = cleaned.get("commented_chapters")
        if not isinstance(ch, list):
            cleaned["commented_chapters"] = []
        else:
            cleaned["commented_chapters"] = [str(x) for x in ch if x][-5000:]
        seen = cleaned.get("seen_notifications")
        if not isinstance(seen, list):
            cleaned["seen_notifications"] = []
        else:
            cleaned["seen_notifications"] = [str(x) for x in seen if x][-800:]
        return cls(**cleaned)

    def to_telegram(self, delay_range: Tuple[float, float] = (5.0, 15.0)) -> str:
        flag = "🟢 Фарм" if self.running else "🔴 Остановлен"
        return (
            f"<b>📚 MangaBuff — статус</b>\n\n"
            f"Состояние: {flag}\n"
            f"📖 Глав всего: <b>{self.chapters_read}</b>\n"
            f"📄 Скроллов: <b>{self.pages_scrolled}</b>\n"
            f"📚 Тайтлов: <b>{self.titles_visited}</b>\n"
            f"🎁 Наград: <b>{self.rewards_claimed}</b>\n"
            f"🃏 Карт: <b>{self.cards_claimed}</b>\n"
            f"💬 Комментов: <b>{self.comments_posted}</b>\n"
            f"🗺 Макеты: <b>{self.layouts_visited}</b>\n"
            f"🎯 Ивент-действия: <b>{self.events_actions}</b>\n"
            f"⚠️ Ошибки: {self.errors}\n"
            f"⏱ Пауза шага: {delay_range[0]:.2f}–{delay_range[1]:.2f} сек\n"
            f"🌙 Ночной стоп: <code>{self.night_break_until or '—'}</code>\n"
            f"🔗 <code>{(self.last_url or '—')[:120]}</code>\n"
            f"📝 {self.last_action or '—'}\n"
            f"🕒 {self.last_at or '—'}"
        )


@dataclass
class ClaimResult:
    claimed: int = 0
    cards: int = 0
    scrolls: int = 0
    chests: int = 0
    packs: int = 0
    events: int = 0
    details: List[str] = field(default_factory=list)
    message: str = ""
    card_names: List[str] = field(default_factory=list)

    def to_telegram(self) -> str:
        lines = [
            "<b>🃏 Сбор · карты / эвенты</b>",
            "",
            f"Действий: <b>{self.claimed}</b>",
            f"🃏 Карт: <b>{self.cards}</b>",
            f"📜 Свитков: <b>{self.scrolls}</b>",
            f"📦 Сундуков: <b>{self.chests}</b> · Паков: <b>{self.packs}</b>",
            f"🎯 Эвентов: <b>{self.events}</b>",
        ]
        if self.card_names:
            lines += ["", "Карты:"] + [f"• {n}" for n in self.card_names[:8]]
        if self.details:
            lines += ["", "Детали:"] + [f"• {d}" for d in self.details[:10]]
        if self.message:
            lines += ["", self.message]
        return "\n".join(lines)


@dataclass
class CardDropInfo:
    """Один дроп карт/свитков во время чтения или сбора."""

    cards: int = 0
    scrolls: int = 0
    names: List[str] = field(default_factory=list)
    ranks: List[str] = field(default_factory=list)
    user_card_ids: List[int] = field(default_factory=list)
    raw: str = ""
    source: str = ""

    def cards_line(self, limit: int = 4) -> str:
        """Строка для Telegram: [C] Имя, [E] Имя2."""
        parts: List[str] = []
        n = max(len(self.names), len(self.ranks), self.cards)
        for i in range(min(n, limit)):
            rank = self.ranks[i] if i < len(self.ranks) else ""
            name = self.names[i] if i < len(self.names) else ""
            bit = format_rank_label(rank)
            if name:
                bit = f"{bit} {name}".strip() if bit else name
            if bit:
                parts.append(bit)
        if not parts and self.ranks:
            parts = [format_rank_label(r) for r in self.ranks[:limit] if r]
        return ", ".join(p for p in parts if p)


@dataclass
class MarketListResult:
    """Результат выставления / обслуживания лотов на площадке."""

    listed: int = 0
    skipped: int = 0
    errors: int = 0
    repriced: int = 0
    removed: int = 0
    details: List[str] = field(default_factory=list)

    def to_telegram(self) -> str:
        lines = [
            "<b>📤 Площадка</b>",
            "",
            f"Выставлено: <b>{self.listed}</b>",
            f"Переценено: <b>{self.repriced}</b>",
            f"Пропущено: <b>{self.skipped}</b>",
            f"Снято/продано: <b>{self.removed}</b>",
            f"Ошибки: <b>{self.errors}</b>",
        ]
        if self.details:
            lines += ["", "Детали:"] + [f"• {d}" for d in self.details[:14]]
        return "\n".join(lines)


class MangaBuffService:
    REWARD_TEXTS = (
        "Забрать",
        "Забрать награду",
        "Забрать карту",
        "Забрать свиток",
        "Забрать всё",
        "Забрать бонус",
        "Получить",
        "Получить награду",
        "Получить карту",
        "Собрать",
        "Открыть",
        "Открыть пак",
        "Открыть сундук",
        "Ежедневная награда",
        "Подтвердить",
        "Продолжить",
        "Отлично",
        "Круто",
        "Понятно",
        "Claim",
    )

    def __init__(
        self,
        config: Config,
        user_data_dir: Optional[Path] = None,
        start_url: str = DEFAULT_START_URL,
        delay_min_sec: float = 5.0,
        delay_max_sec: float = 15.0,
        email: str = "",
        password: str = "",
    ) -> None:
        self.config = config
        self.user_data_dir = Path(user_data_dir or DEFAULT_USER_DATA)
        self.start_url = start_url
        self.delay_min_sec = delay_min_sec
        self.delay_max_sec = delay_max_sec
        self.email = (email or os.getenv("MANGABUFF_EMAIL", "")).strip()
        self.password = (password or os.getenv("MANGABUFF_PASSWORD", "")).strip()

        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._started = False
        self._stop_flag = asyncio.Event()
        self.stats = self._load_stats()
        self._chapters_since_comment = 0
        self._next_comment_after = random.randint(COMMENT_EVERY_MIN, COMMENT_EVERY_MAX)
        self._read_urls: set[str] = set()
        self._skip_title = asyncio.Event()
        self.steps_min = 8
        self.steps_max = 12
        # slug → голос интонации; last start/end чтобы не повторять подряд
        self._title_voices: Dict[str, Dict[str, Any]] = {}
        self._last_comment_bits: Dict[str, str] = {}
        self._current_comment_slug: str = ""
        # Главы, куда уже писали (строго 1 комментарий на главу)
        self._commented_chapters: set[str] = set(self.stats.commented_chapters or [])
        # База глав на старт текущей сессии фарма + таймстемпы для живого темпа
        self._session_chapters_base: int = int(self.stats.chapters_read or 0)
        self._session_cards_base: int = int(self.stats.cards_claimed or 0)
        self._chapter_ts: List[float] = []
        self._events_stop = asyncio.Event()
        self._events_running: bool = False
        # колбэк: async (CardDropInfo) -> None — уведомление о картах
        self.on_card_drop = None
        self._seen_notifications: set[str] = set(self.stats.seen_notifications or [])
        self._mb_user_id: str = ""
        self._last_history_post_at: float = 0.0
        self._history_min_gap_sec: float = 20.0
        self._migrate_stats_if_needed()
        # сброс ложных дропов («Тайтлы» из навбара) — портили статистику карт
        if "тайтл" in (self.stats.last_card_drop or "").lower():
            logger.warning(
                "MangaBuff reset fake card stats (last_card_drop=%s, cards=%s)",
                self.stats.last_card_drop,
                self.stats.cards_claimed,
            )
            self.stats.cards_claimed = 0
            self.stats.last_card_drop = ""
            self._session_cards_base = 0
            self._persist_stats()

    def _migrate_stats_if_needed(self) -> None:
        """Сброс завышенного счётчика глав (старая логика считала скролл, не addHistory)."""
        try:
            from settings_store import load_settings, update_settings

            s = load_settings()
            if s.mangabuff_stats_site_only:
                return
            logger.warning(
                "MangaBuff reset chapter counters (old inflated stats: %s)",
                self.stats.chapters_read,
            )
            self.stats.chapters_read = 0
            self.stats.chapters_pending = 0
            self._session_chapters_base = 0
            self._chapter_ts.clear()
            self._persist_stats()
            update_settings(mangabuff_stats_site_only=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("stats migration: %s", exc)

    def mark_farm_session_start(self) -> None:
        """Зафиксировать начало сессии: главы «за сессию» считаются отсюда."""
        self._session_chapters_base = int(self.stats.chapters_read or 0)
        self._session_cards_base = int(self.stats.cards_claimed or 0)
        self._chapter_ts.clear()

    @property
    def session_chapters(self) -> int:
        return max(0, int(self.stats.chapters_read or 0) - int(self._session_chapters_base or 0))

    @property
    def session_cards(self) -> int:
        return max(0, int(self.stats.cards_claimed or 0) - int(self._session_cards_base or 0))

    def note_chapter_finished(self) -> None:
        """Вызывать после успешного прочтения главы — для живого замера темпа."""
        now = time_mod.time()
        self._chapter_ts.append(now)
        if len(self._chapter_ts) > 40:
            self._chapter_ts = self._chapter_ts[-40:]

    def measured_sec_per_chapter(self) -> float:
        """Среднее время на главу по последним ~40 главам текущей сессии."""
        ts = self._chapter_ts
        if len(ts) < 2:
            return 0.0
        return max(0.01, (ts[-1] - ts[0]) / (len(ts) - 1))

    def measured_cph(self) -> float:
        spc = self.measured_sec_per_chapter()
        if spc <= 0:
            return 0.0
        return 3600.0 / spc

    def _load_stats(self) -> MangaBuffStats:
        if not STATS_PATH.exists():
            return MangaBuffStats()
        try:
            raw = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return MangaBuffStats.from_dict(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mangabuff_stats load: %s", exc)
        return MangaBuffStats()

    def _persist_stats(self) -> None:
        try:
            tmp = STATS_PATH.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.stats.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(STATS_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mangabuff_stats save: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_started(self) -> bool:
        return self._started and self._context is not None

    @property
    def is_running(self) -> bool:
        return self.stats.running

    async def start(self, headless: bool = True) -> None:
        if self._started:
            return
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "MangaBuff: Playwright headless=%s profile=%s",
            headless,
            self.user_data_dir,
        )
        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=headless,
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            user_agent=self.config.user_agent,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        self._page.set_default_timeout(self.config.selector_timeout_ms)
        self._started = True

    async def stop(self) -> None:
        self._stop_flag.set()
        self.stats.running = False
        try:
            if self._context is not None:
                await self._context.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff context close: %s", exc)
        finally:
            self._context = None
            self._page = None
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff playwright stop: %s", exc)
        finally:
            self._playwright = None
            self._started = False
            logger.info("MangaBuff: остановлен.")

    def request_stop(self) -> None:
        self._stop_flag.set()
        self.stats.running = False
        self.stats.touch("стоп запрошен")

    def request_skip_title(self) -> None:
        """Пропустить текущий тайтл и взять следующий из каталога."""
        self._skip_title.set()
        self.stats.touch("пропуск тайтла запрошен")

    def _chapter_farm_active(self) -> bool:
        """Идёт основной фарм глав — маркет/эвенты не должны отбирать браузер."""
        return bool(self.stats.running and not self._stop_flag.is_set())

    async def _acquire_browser_lock(self, purpose: str, timeout: float = 120.0) -> bool:
        """Взять lock с таймаутом и логом ожидания (для фарма глав)."""
        deadline = asyncio.get_event_loop().time() + max(1.0, float(timeout))
        warned = False
        while not self._stop_flag.is_set():
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=3.0)
                return True
            except asyncio.TimeoutError:
                if not warned:
                    logger.warning("MangaBuff waiting for browser lock (%s)", purpose)
                    warned = True
                if asyncio.get_event_loop().time() >= deadline:
                    logger.error("MangaBuff browser lock timeout (%s)", purpose)
                    return False
        return False

    def set_delay(
        self,
        delay_min: float,
        delay_max: float,
        steps_min: int = 8,
        steps_max: int = 12,
    ) -> None:
        self.delay_min_sec = max(0.02, float(delay_min))
        self.delay_max_sec = max(self.delay_min_sec, float(delay_max))
        self.steps_min = max(1, int(steps_min))
        self.steps_max = max(self.steps_min, int(steps_max))
        logger.info(
            "MangaBuff tempo set delay=%.2f-%.2f steps=%s-%s tier=%s",
            self.delay_min_sec,
            self.delay_max_sec,
            self.steps_min,
            self.steps_max,
            self._tempo_tier(),
        )

    def _tempo_tier(self) -> str:
        d = self.delay_min_sec
        if d <= 0.06:
            return "turbo"
        if d <= 0.14:
            return "fast"
        if d <= 0.35:
            return "lively"
        if d <= 0.75:
            return "normal"
        if d <= 1.40:
            return "slow"
        return "crawl"

    def _tempo_factor(self) -> float:
        """Множитель для «человеческих» пауз вне скролла."""
        return {
            "turbo": 0.06,
            "fast": 0.14,
            "lively": 0.32,
            "normal": 0.65,
            "slow": 1.0,
            "crawl": 1.35,
        }[self._tempo_tier()]

    async def _tempo_pause(self, a: float, b: float) -> None:
        """Пауза, масштабируемая текущим темпом (в турбо почти исчезает)."""
        f = self._tempo_factor()
        lo = max(0.0, a * f)
        hi = max(lo, b * f)
        if hi < 0.015:
            return
        await self._human_pause(max(0.01, lo), hi)

    def _scroll_plan(self, total_height: int, viewport: int) -> Tuple[int, int]:
        """
        Жёсткий план: steps из пресета, chunk подогнан под высоту главы.
        Больше не раздуваем шаги от высоты страницы.
        """
        viewport = max(int(viewport), 600)
        total_height = max(int(total_height), viewport + 1)
        target_steps = random.randint(self.steps_min, self.steps_max)
        cover = max(total_height - int(viewport * 0.35), viewport)
        chunk = max(int(viewport * 0.9), int(cover / max(1, target_steps - 1)))
        tier = self._tempo_tier()
        # в турбо/быстром — ещё крупнее прыжки
        if tier == "turbo":
            chunk = max(chunk, int(viewport * random.uniform(6.0, 10.0)))
            target_steps = min(target_steps, self.steps_max)
        elif tier == "fast":
            chunk = max(chunk, int(viewport * random.uniform(3.5, 5.5)))
        return target_steps, chunk

    def _chapter_gap_pause(self) -> Tuple[float, float]:
        """Пауза между главами по темпу."""
        return {
            "turbo": (0.05, 0.18),
            "fast": (0.12, 0.35),
            "lively": (0.30, 0.70),
            "normal": (0.70, 1.50),
            "slow": (1.20, 2.80),
            "crawl": (2.00, 4.50),
        }[self._tempo_tier()]

    async def run_setup(self) -> None:
        await self.start(headless=False)
        assert self._page is not None
        if self.email and self.password:
            await self.ensure_login()
            print("\n✅ Автологин выполнен. Проверьте окно и нажмите Enter.\n")
        else:
            await self._page.goto(self.start_url, wait_until="domcontentloaded")
            print("\nMangaBuff setup: войдите вручную, затем Enter.\n")
        await asyncio.to_thread(input, "Enter для сохранения сессии... ")
        await self.stop()
        print(f"Сессия: {self.user_data_dir}")

    # ------------------------------------------------------------------
    # Night break
    # ------------------------------------------------------------------

    def _night_break_remaining(self) -> Optional[timedelta]:
        now = datetime.now(MSK)
        t = now.time()
        if NIGHT_BREAK_START <= t < NIGHT_BREAK_END:
            end = datetime.combine(now.date(), NIGHT_BREAK_END, tzinfo=MSK)
            return end - now
        return None

    async def _await_night_break_if_needed(self) -> None:
        remaining = self._night_break_remaining()
        if remaining is None:
            self.stats.night_break_until = ""
            return
        until = datetime.now(MSK) + remaining
        self.stats.night_break_until = until.strftime("%d.%m.%Y %H:%M")
        self.stats.touch(f"ночной перерыв до {self.stats.night_break_until}")
        logger.info("MangaBuff night break %s sec", int(remaining.total_seconds()))
        # Спим кусками по wall-clock, чтобы не «залипнуть» после сдвига времени
        while not self._stop_flag.is_set():
            left = self._night_break_remaining()
            if left is None:
                break
            chunk = min(30.0, max(0.5, left.total_seconds()))
            await asyncio.sleep(chunk)
        self.stats.night_break_until = ""
        logger.info("MangaBuff night break finished, resume farm")
        self.stats.touch("ночной перерыв окончен")
        self._persist_stats()

    # ------------------------------------------------------------------
    # Human helpers
    # ------------------------------------------------------------------

    async def _human_pause(self, a: float = 0.8, b: float = 2.2) -> None:
        try:
            await asyncio.wait_for(self._stop_flag.wait(), timeout=random.uniform(a, b))
        except asyncio.TimeoutError:
            pass

    async def _dismiss_overlays(self, page: Page) -> None:
        selectors = (
            ".close-adult-modal-btn",
            ".update-toast__close",
            ".tg-prompt__close",
            ".modal__close",
            "button.close-adult-modal-btn",
            "button:has-text('Подтвердить')",
            "button:has-text('Мне 18')",
            "button:has-text('Мне есть 18')",
            "button:has-text('Понятно')",
            "button:has-text('Закрыть')",
            "button:has-text('Продолжить')",
            "button:has-text('Смотреть')",
            "button:has-text('Да, мне есть')",
            "a:has-text('Продолжить')",
        )
        for sel in selectors:
            try:
                loc = page.locator(sel)
                n = await loc.count()
                for i in range(min(n, 3)):
                    btn = loc.nth(i)
                    if await btn.is_visible(timeout=600):
                        await btn.click(force=True, timeout=1500)
                        await self._human_pause(0.2, 0.6)
            except Exception:  # noqa: BLE001
                continue

    async def _safe_goto(self, page: Page, url: str) -> bool:
        try:
            tier = self._tempo_tier()
            # turbo/fast: не ждём полной отрисовки — хватает commit/domcontentloaded
            wait_until = "commit" if tier == "turbo" else "domcontentloaded"
            timeout = 20000 if tier in ("turbo", "fast") else self.config.selector_timeout_ms
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            if tier == "turbo":
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=2500)
                except Exception:  # noqa: BLE001
                    pass
                await self._tempo_pause(0.08, 0.20)
            else:
                await self._tempo_pause(0.35, 0.90)
            # в турбо оверлеи гасим быстро и без долгих таймаутов
            try:
                await asyncio.wait_for(
                    self._dismiss_overlays(page),
                    timeout=1.2 if tier in ("turbo", "fast") else 6.0,
                )
            except Exception:  # noqa: BLE001
                pass
            self.stats.touch("переход", page.url)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("goto %s: %s", url, exc)
            self.stats.errors += 1
            return False

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def ensure_login(self) -> bool:
        if not self.is_started:
            await self.start(headless=True)
        # лок: параллельный events/read не уводит вкладку во время логина.
        # Из кода под уже взятым _lock вызывайте _ensure_login_unlocked().
        async with self._lock:
            return await self._ensure_login_unlocked()

    async def _ensure_login_unlocked(self) -> bool:
        assert self._page is not None
        page = self._page

        if await self._is_logged_in(page):
            self.stats.touch("уже авторизован", page.url)
            return True

        await self._safe_goto(page, "https://mangabuff.ru/")
        if await self._is_logged_in(page):
            self.stats.touch("уже авторизован", page.url)
            return True

        if not self.email or not self.password:
            logger.error("MangaBuff: нет MANGABUFF_EMAIL/PASSWORD")
            return False

        await self._safe_goto(page, "https://mangabuff.ru/login")
        try:
            email = page.locator("input[name=email]").first
            pwd = page.locator("input[name=password]").first
            await email.wait_for(state="visible", timeout=8000)
            await email.fill(self.email, timeout=8000)
            await self._human_pause(0.3, 0.7)
            await pwd.fill(self.password, timeout=8000)
            await self._human_pause(0.4, 0.9)
            await page.locator("button.login-button").first.click(timeout=8000)
            await self._human_pause(2.0, 3.5)
            await self._dismiss_overlays(page)
            ok = await self._is_logged_in(page)
            self.stats.touch("логин ok" if ok else "логин fail", page.url)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.exception("MangaBuff login: %s", exc)
            self.stats.errors += 1
            return False

    async def _is_logged_in(self, page: Page) -> bool:
        try:
            # закладки /users/<id>/bookmarks появляются после логина
            html = await page.content()
            if re.search(r"/users/\d+/bookmarks", html):
                return True
            if await page.locator("a[href*='/users/'][href*='bookmarks']").count() > 0:
                return True
            # форма логина на /login
            if "login" in page.url and await page.locator("button.login-button").count():
                return False
            # если есть уведомления и нет кнопки Войти в шапке — считаем ок
            body = await page.inner_text("body")
            if "Выйти" in body or "уведомлен" in body.lower():
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # ------------------------------------------------------------------
    # Rewards / events / cards
    # ------------------------------------------------------------------

    async def claim_rewards(self) -> ClaimResult:
        """Полный сбор наград/эвентов (ручной или из раздела Карты)."""
        async with self._lock:
            return await self._farm_events_unlocked(quick=False)

    async def farm_events_once(self, *, quick: bool = True) -> ClaimResult:
        """Один быстрый проход по эвентам/картам под локом."""
        async with self._lock:
            return await self._farm_events_unlocked(quick=quick)

    def request_stop_events(self) -> None:
        self._events_stop.set()

    async def events_loop(self, on_progress=None, interval_sec: float = 90.0) -> None:
        """
        Отдельный автофарм эвентов/карт: быстрые циклы сбора.
        Можно запускать параллельно с чтением — берёт тот же lock.
        """
        self._events_stop.clear()
        self._events_running = True
        self.stats.touch("эвент-фарм запущен")
        if not self.is_started:
            await self.start(headless=True)
        try:
            while not self._events_stop.is_set() and not self._stop_flag.is_set():
                await self._await_night_break_if_needed()
                if self._events_stop.is_set() or self._stop_flag.is_set():
                    break
                # Пока идёт фарм глав — не трогаем браузер: эвенты между тайтлами
                # уже собирает сам farm_loop.
                if not self._chapter_farm_active():
                    try:
                        result = await self.farm_events_once(quick=True)
                        if on_progress is not None:
                            try:
                                await on_progress(result)
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("events_loop: %s", exc)
                        self.stats.errors += 1
                else:
                    logger.debug("events_loop idle — chapter farm active")
                # пока идёт чтение — реже (лок и так сериализует), иначе плотнее
                tier = self._tempo_tier()
                if self._chapter_farm_active():
                    pause = 120.0 if tier in ("turbo", "fast") else 180.0
                elif tier == "turbo":
                    pause = 28.0
                elif tier == "fast":
                    pause = 40.0
                else:
                    pause = float(interval_sec)
                end = asyncio.get_event_loop().time() + pause
                while asyncio.get_event_loop().time() < end:
                    if self._events_stop.is_set() or self._stop_flag.is_set():
                        break
                    await asyncio.sleep(1.0)
        finally:
            self._events_running = False
            self.stats.touch("эвент-фарм остановлен")

    async def _claim_rewards_unlocked(self, quick: bool = False) -> ClaimResult:
        return await self._farm_events_unlocked(quick=quick)

    async def _farm_events_unlocked(self, quick: bool = False) -> ClaimResult:
        if not self.is_started:
            await self.start(headless=True)
        assert self._page is not None
        page = self._page
        await self._ensure_login_unlocked()
        result = ClaimResult()
        before_cards = int(self.stats.cards_claimed or 0)

        await self._refresh_user_id(page)
        if quick:
            # лента уведомлений — главный источник дропов карт за чтение
            urls = list(self._card_notify_urls()) + [
                "https://mangabuff.ru/battle",
                "https://mangabuff.ru/cards/pack",
                "https://mangabuff.ru/transactions",
            ]
        else:
            urls = list(self._card_notify_urls()) + [
                u for u in EVENT_FARM_URLS if "notifications" not in u
            ]

        for url in urls:
            if self._stop_flag.is_set() or self._events_stop.is_set():
                break
            logger.info("MangaBuff event visit %s", url)
            if not await self._safe_goto(page, url):
                continue

            if "/notifications" in url:
                feed = await self._harvest_notifications_feed(page)
                if feed.cards or feed.scrolls:
                    result.cards += feed.cards
                    result.scrolls += feed.scrolls
                    result.card_names.extend(feed.names)
                    result.details.append(
                        f"уведомления: +{feed.cards} карт / +{feed.scrolls} свитков"
                    )
                continue

            # сундуки / паки / награды
            c, cards, scrolls, chests, packs, details = await self._click_reward_buttons(page)
            result.claimed += c
            result.cards += cards
            result.scrolls += scrolls
            result.chests += chests
            result.packs += packs
            result.details.extend(details)
            self.stats.rewards_claimed += c
            self.stats.cards_claimed += cards
            self.stats.scrolls_claimed += scrolls
            self.stats.chests_opened += chests
            self.stats.packs_opened += packs

            drop = await self._harvest_card_drops(page, source=url)
            if drop.cards or drop.scrolls:
                result.cards += drop.cards
                result.scrolls += drop.scrolls
                result.card_names.extend(drop.names)
                result.details.append(
                    f"дроп: +{drop.cards} карт / +{drop.scrolls} свитков"
                )

            if "/battle" in url:
                ev = await self._farm_battle_events(page)
                result.events += len(ev)
                result.details.extend(ev)
                self.stats.events_actions += len(ev)
                # ещё раз сундук после дейликов
                c2, cards2, sc2, ch2, pk2, d2 = await self._click_reward_buttons(page)
                result.claimed += c2
                result.cards += cards2
                result.scrolls += sc2
                result.chests += ch2
                result.packs += pk2
                result.details.extend(d2)
                self.stats.rewards_claimed += c2
                self.stats.cards_claimed += cards2
                self.stats.scrolls_claimed += sc2
                self.stats.chests_opened += ch2
                self.stats.packs_opened += pk2

        gained = max(0, int(self.stats.cards_claimed or 0) - before_cards)
        if gained > 0 and not result.cards:
            result.cards = gained
        if result.claimed == 0 and result.cards == 0 and result.events == 0:
            result.message = "Сейчас забирать нечего — всё собрано."
        else:
            result.message = "Сбор завершён."
        self.stats.touch(
            f"эвенты: +{result.claimed} · карт +{result.cards}",
            page.url if self._page else "",
        )
        self._persist_stats()
        # уведомление уже шлёт _harvest_card_drops — без второго emit
        return result

    async def explore_layouts(self) -> int:
        """Пройти основные разделы сайта (изучение макетов)."""
        if self.stats.running:
            logger.info("MangaBuff layouts skipped: farm running")
            return 0
        async with self._lock:
            return await self._explore_layouts_unlocked()

    async def _explore_layouts_unlocked(self) -> int:
        if not self.is_started:
            await self.start(headless=True)
        assert self._page is not None
        page = self._page
        visited = 0
        for url in LAYOUT_URLS:
            if self._stop_flag.is_set():
                break
            if await self._safe_goto(page, url):
                visited += 1
                self.stats.layouts_visited += 1
                await self._click_reward_buttons(page)
                await self._harvest_card_drops(page, source=url)
                try:
                    await page.mouse.wheel(0, random.randint(400, 1200))
                except Exception:  # noqa: BLE001
                    pass
                await self._tempo_pause(0.8, 1.8)
        self.stats.touch(f"макеты: {visited}")
        return visited

    async def _farm_battle_events(self, page: Page) -> List[str]:
        details: List[str] = []
        # сундук / забрать дейлик / усиление — без агрессивного старта боёв
        for text in (
            "Открыть сундук",
            "Забрать",
            "Получить",
            "Забрать награду",
            "Забрать всё",
            "Выбрать",
        ):
            loc = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                count = 0
            # также текстовые ссылки/кнопки
            if count == 0:
                loc = page.get_by_text(re.compile(f"^{re.escape(text)}$", re.I))
                try:
                    count = await loc.count()
                except Exception:  # noqa: BLE001
                    count = 0
            for i in range(min(count, 6)):
                btn = loc.nth(i)
                try:
                    if not await btn.is_visible(timeout=400):
                        continue
                    label = ((await btn.inner_text(timeout=600)) or text).strip()
                    await btn.click(force=True, timeout=2000)
                    details.append(f"battle: {label[:50]}")
                    await self._tempo_pause(0.35, 0.9)
                    drop = await self._harvest_card_drops(page, source="battle")
                    if drop.cards:
                        details.append(f"battle-drop: +{drop.cards} карт")
                except Exception:  # noqa: BLE001
                    continue
        return details

    async def _click_reward_buttons(
        self, page: Page
    ) -> Tuple[int, int, int, int, int, List[str]]:
        """returns claimed, cards, scrolls, chests, packs, details"""
        claimed = 0
        cards = 0
        scrolls = 0
        chests = 0
        packs = 0
        details: List[str] = []
        await self._dismiss_overlays(page)

        try:
            labels = await page.evaluate(
                """() => [...document.querySelectorAll('button, a.button, a.btn, [role=button]')]
                  .filter(el => {
                    const s = getComputedStyle(el);
                    return s && s.display !== 'none' && s.visibility !== 'hidden'
                      && (el.offsetParent !== null || s.position === 'fixed');
                  })
                  .map(el => (el.innerText||'').trim().slice(0,80))
                  .filter(t => t && /забрать|получить|собрать|открыть|подтвердить|claim|ежеднев|продолж|отлично|круто|понятно|сундук|пак|свит/i.test(t))
                  .slice(0, 16)"""
            )
        except Exception:  # noqa: BLE001
            labels = []

        for text in labels:
            text = (text or "").strip()
            if not text or len(text) > 48:
                continue
            # не тратим алмазы на платные паки без пометки «бесплат»
            low = text.lower()
            if "пак" in low and "бесплат" not in low and "ежеднев" not in low:
                if re.search(r"\d+\s*алмаз", low):
                    continue
            try:
                loc = page.locator("button, a.button, a.btn, [role=button]").filter(
                    has_text=re.compile(re.escape(text), re.I)
                ).first
                if not await loc.is_visible(timeout=500):
                    continue
                await loc.click(force=True, timeout=1800)
                claimed += 1
                if "сундук" in low:
                    chests += 1
                if "пак" in low:
                    packs += 1
                # карты/свитки считаем только из модалок (_harvest_card_drops),
                # иначе кнопка «Забрать карту» + модалка дают двойной счёт
                details.append(f"клик: {text[:60]}")
                await self._tempo_pause(0.25, 0.7)
                await self._dismiss_overlays(page)
            except Exception:  # noqa: BLE001
                continue

        for sel in (
            ".close-adult-modal-btn",
            ".modal button.button--primary",
            "button:has-text('Забрать')",
            "button:has-text('Открыть сундук')",
        ):
            try:
                loc = page.locator(sel)
                n = min(await loc.count(), 3)
                for i in range(n):
                    el = loc.nth(i)
                    if await el.is_visible(timeout=400):
                        txt = ((await el.inner_text(timeout=300)) or "modal").strip()[:50]
                        await el.click(force=True, timeout=1500)
                        claimed += 1
                        low = txt.lower()
                        if "сундук" in low:
                            chests += 1
                        if "пак" in low:
                            packs += 1
                        details.append(f"modal: {txt}")
                        await self._tempo_pause(0.2, 0.5)
            except Exception:  # noqa: BLE001
                continue
        return claimed, cards, scrolls, chests, packs, details

    async def _harvest_card_drops(self, page: Page, source: str = "") -> CardDropInfo:
        """Детект только реальных модалок дропа карт/свитков (без навбара)."""
        info = CardDropInfo(source=source)
        try:
            blobs = await page.evaluate(
                """() => {
                  const sel = '.modal.show, .modal.is-active, .modal[style*="display"],'
                    + '[class*=toast]:not(nav):not(header),'
                    + '[class*=reward-modal], [class*=card-drop], [class*=prize],'
                    + '[class*=received], .swal2-popup, [role=dialog]';
                  const roots = [...document.querySelectorAll(sel)];
                  // fallback: любой видимый modal с кнопкой забрать карту
                  if (!roots.length) {
                    roots.push(...document.querySelectorAll('.modal, [class*=modal]'));
                  }
                  const out = [];
                  for (const el of roots) {
                    const st = getComputedStyle(el);
                    if (!st || st.display === 'none' || st.visibility === 'hidden'
                        || Number(st.opacity||1) < 0.05) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 80 || rect.height < 40) continue;
                    const t = (el.innerText || '').trim();
                    if (!t || t.length < 8 || t.length > 500) continue;
                    // строго: фразы получения, не меню «Карты / Тайтлы»
                    if (!/(получен|выпал|выпала|новая карт|забрать карт|вам карт|получил|открыт|свиток|свитк)/i.test(t)) {
                      continue;
                    }
                    if (/тайтлы|колоды|уведомления|главное меню|каталог/i.test(t)
                        && !/(получен|выпал|забрать карт)/i.test(t)) {
                      continue;
                    }
                    out.push(t.slice(0, 280));
                  }
                  return out.slice(0, 4);
                }"""
            )
        except Exception:  # noqa: BLE001
            blobs = []

        nav_noise = re.compile(
            r"^(тайтлы|карты|колоды|уведомления|профиль|битва|магазин|лента)$",
            re.I,
        )
        for raw in blobs or []:
            low = raw.lower()
            # явные количества: «+2 карты», «получено 3 карты»
            m = re.search(r"(?:\+|получен[оа]?\s*)(\d+)\s*карт", low)
            if m:
                n_cards = min(10, int(m.group(1)))
            elif re.search(r"(получен|выпал|новая карт|забрать карт|вам карт)", low):
                n_cards = 1
            else:
                n_cards = 0
            m2 = re.search(r"(?:\+|получен[оа]?\s*)(\d+)\s*свит", low)
            if m2:
                n_scroll = min(5, int(m2.group(1)))
            elif "свит" in low and re.search(r"(получен|выпал|забрать)", low):
                n_scroll = 1
            else:
                n_scroll = 0
            info.cards += max(0, n_cards)
            info.scrolls += max(0, n_scroll)
            for line in raw.splitlines():
                line = line.strip()
                if not (2 <= len(line) <= 60):
                    continue
                if nav_noise.match(line):
                    continue
                if re.search(
                    r"забрать|получить|закрыть|продолж|отлично|понятно|уведомл|тайтл",
                    line,
                    re.I,
                ):
                    continue
                if line not in info.names:
                    info.names.append(line)
                break
            info.raw = raw[:200]

        info.cards = min(10, info.cards)
        info.scrolls = min(5, info.scrolls)

        if info.cards or info.scrolls:
            for text in (
                "Забрать карту",
                "Забрать",
                "Отлично",
                "Круто",
                "Продолжить",
                "Понятно",
                "OK",
                "Ок",
            ):
                try:
                    loc = page.get_by_role(
                        "button", name=re.compile(f"^{re.escape(text)}$", re.I)
                    )
                    if await loc.count() and await loc.first.is_visible(timeout=300):
                        await loc.first.click(force=True, timeout=1200)
                        await self._tempo_pause(0.15, 0.4)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if info.cards:
                self.stats.cards_claimed += info.cards
                line = info.cards_line(2)
                self.stats.last_card_drop = (
                    f"+{info.cards} · {line}" if line else f"+{info.cards}"
                )
            if info.scrolls:
                self.stats.scrolls_claimed += info.scrolls
            self.stats.touch(
                f"дроп карт +{info.cards}"
                + (f" свит +{info.scrolls}" if info.scrolls else ""),
                page.url,
            )
            self._persist_stats()
            logger.info(
                "MangaBuff card drop +%s scrolls +%s from %s ranks=%s names=%s",
                info.cards,
                info.scrolls,
                source,
                info.ranks[:3],
                info.names[:3],
            )
            await self._emit_card_drop(info, page)
        return info

    async def _emit_card_drop(
        self, info: CardDropInfo, page: Optional[Page] = None
    ) -> None:
        if not info.cards and not info.scrolls:
            return
        if page is not None and info.cards:
            try:
                await self._enrich_card_drop_rarity(page, info)
            except Exception as exc:  # noqa: BLE001
                logger.warning("enrich card rarity: %s", exc)
            if info.ranks:
                line = info.cards_line(3)
                self.stats.last_card_drop = (
                    f"+{info.cards} · {line}" if line else f"+{info.cards}"
                )
                self._persist_stats()
        cb = self.on_card_drop
        if cb is None:
            return
        try:
            await cb(info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("on_card_drop: %s", exc)

    def _card_notify_urls(self) -> List[str]:
        urls = list(CARD_NOTIFY_URLS)
        if self._mb_user_id:
            # персональная ссылка вида /notifications?<user_id>
            urls.insert(0, f"https://mangabuff.ru/notifications?{self._mb_user_id}")
        return urls

    async def _refresh_user_id(self, page: Page) -> None:
        try:
            uid = await page.evaluate(
                "() => (window.user_id != null ? String(window.user_id) : '')"
            )
            if uid and uid.isdigit():
                self._mb_user_id = uid
        except Exception:  # noqa: BLE001
            pass

    def _remember_notification(self, key: str) -> bool:
        """True если ключ новый (ещё не видели)."""
        key = (key or "").strip()
        if not key or key in self._seen_notifications:
            return False
        self._seen_notifications.add(key)
        # trim + persist
        recent = list(self._seen_notifications)[-800:]
        self._seen_notifications = set(recent)
        self.stats.seen_notifications = recent
        return True

    def _parse_ranks_from_text(self, text: str) -> List[str]:
        ranks: List[str] = []
        for m in re.finditer(r"ранг\s*[:\-]?\s*([A-Za-z])", text or "", re.I):
            r = m.group(1).upper()
            if r in CARD_RANK_LADDER or r in "HNVQLK":
                ranks.append(r)
        for m in re.finditer(
            r"\[([XSAPGBCDEHNVQLK])\]|\b([XSAPGBCDE])\s*ранг", text or "", re.I
        ):
            r = (m.group(1) or m.group(2) or "").upper()
            if r and r not in ranks:
                ranks.append(r)
        return ranks[:5]

    def _parse_card_notify_text(
        self, text: str
    ) -> Tuple[int, int, List[str], List[str]]:
        """Из текста уведомления → (cards, scrolls, names, ranks)."""
        raw = (text or "").strip()
        if not raw or not _CARD_NOTIFY_RE.search(raw):
            return 0, 0, [], []
        low = raw.lower()
        # не путать с навбаром/вкладками
        if re.search(r"список уведомлений пуст|показать все", low):
            return 0, 0, [], []
        m = re.search(r"(?:\+|получен[ао]?\s*)(\d+)\s*карт", low)
        n_cards = min(10, int(m.group(1))) if m else (1 if "карт" in low else 0)
        m2 = re.search(r"(?:\+|получен[ао]?\s*)(\d+)\s*свит", low)
        n_scroll = min(5, int(m2.group(1))) if m2 else (1 if "свит" in low else 0)
        names: List[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not (2 <= len(line) <= 80):
                continue
            if re.search(
                r"уведомл|забрать|получить|закрыть|продолж|отлично|манга|коммент|обмен",
                line,
                re.I,
            ):
                continue
            if _CARD_NOTIFY_RE.search(line) and len(line) < 24:
                continue
            if re.fullmatch(r"[XSAPGBCDEHNVQLK]", line, re.I):
                continue
            names.append(line)
            if len(names) >= 3:
                break
        ranks = self._parse_ranks_from_text(raw)
        return n_cards, n_scroll, names, ranks

    def _load_market_lots_state(self) -> Dict[str, Dict[str, Any]]:
        if not MARKET_LOTS_PATH.exists():
            return {}
        try:
            raw = json.loads(MARKET_LOTS_PATH.read_text(encoding="utf-8"))
            lots = raw.get("lots") if isinstance(raw, dict) else raw
            if not isinstance(lots, dict):
                return {}
            out: Dict[str, Dict[str, Any]] = {}
            for k, v in lots.items():
                if isinstance(v, dict) and str(k).isdigit():
                    out[str(k)] = v
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("market_lots load: %s", exc)
            return {}

    def _save_market_lots_state(self, lots: Dict[str, Dict[str, Any]]) -> None:
        try:
            MARKET_LOTS_PATH.write_text(
                json.dumps({"lots": lots}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("market_lots save: %s", exc)

    async def _fetch_inventory_cards(
        self,
        page: Page,
        limit: int = 40,
        rank: str = "",
        search: str = "",
        page_no: int = 1,
    ) -> List[Dict[str, Any]]:
        """POST /cards-filter/<user_id> — одна страница инвентаря."""
        await self._refresh_user_id(page)
        uid = self._mb_user_id
        if not uid:
            return []
        try:
            data = await page.evaluate(
                """async ({uid, limit, rank, search, pageNo}) => {
                  const csrf = (document.querySelector('meta[name="csrf-token"]')
                    || {}).content || '';
                  const body = new URLSearchParams();
                  body.set('page', String(pageNo || 1));
                  body.set('per_page', String(limit || 40));
                  body.set('search', search || '');
                  body.set('rank', (rank || '').toLowerCase());
                  const resp = await fetch('/cards-filter/' + uid, {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                      'X-CSRF-TOKEN': csrf,
                      'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: body.toString(),
                    credentials: 'same-origin'
                  });
                  if (!resp.ok) return {ok: false, status: resp.status, data: [], last: 1};
                  let json = null;
                  try { json = await resp.json(); } catch (e) { json = null; }
                  const rows = (json && json.data) ? json.data : (Array.isArray(json) ? json : []);
                  return {
                    ok: true,
                    status: resp.status,
                    data: rows,
                    last: (json && json.last_page) ? json.last_page : 1,
                    total: (json && json.total) ? json.total : rows.length
                  };
                }""",
                {
                    "uid": uid,
                    "limit": int(limit),
                    "rank": (rank or "").strip(),
                    "search": (search or "").strip(),
                    "pageNo": int(page_no),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cards-filter: %s", exc)
            return []
        if not data or not data.get("ok"):
            logger.warning("cards-filter fail: %s", data)
            return []
        rows = data.get("data") or []
        return [r for r in rows if isinstance(r, dict)]

    async def _fetch_all_inventory_cards(
        self,
        page: Page,
        per_page: int = 70,
        rank: str = "",
        search: str = "",
    ) -> List[Dict[str, Any]]:
        """Все страницы инвентаря."""
        await self._refresh_user_id(page)
        uid = self._mb_user_id
        if not uid:
            return []
        try:
            data = await page.evaluate(
                """async ({uid, perPage, rank, search}) => {
                  const csrf = (document.querySelector('meta[name="csrf-token"]')
                    || {}).content || '';
                  const all = [];
                  let pageNo = 1;
                  let last = 1;
                  while (pageNo <= last && pageNo <= 40) {
                    const body = new URLSearchParams();
                    body.set('page', String(pageNo));
                    body.set('per_page', String(perPage || 70));
                    body.set('search', search || '');
                    body.set('rank', (rank || '').toLowerCase());
                    const resp = await fetch('/cards-filter/' + uid, {
                      method: 'POST',
                      headers: {
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-CSRF-TOKEN': csrf,
                        'X-Requested-With': 'XMLHttpRequest'
                      },
                      body: body.toString(),
                      credentials: 'same-origin'
                    });
                    if (!resp.ok) break;
                    let json = null;
                    try { json = await resp.json(); } catch (e) { break; }
                    const rows = (json && json.data) ? json.data : [];
                    all.push(...rows);
                    last = (json && json.last_page) ? Number(json.last_page) : pageNo;
                    if (!rows.length) break;
                    pageNo += 1;
                  }
                  return {ok: true, data: all};
                }""",
                {
                    "uid": uid,
                    "perPage": int(per_page),
                    "rank": (rank or "").strip(),
                    "search": (search or "").strip(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cards-filter all: %s", exc)
            return []
        rows = (data or {}).get("data") or []
        return [r for r in rows if isinstance(r, dict)]

    async def _enrich_card_drop_rarity(self, page: Page, info: CardDropInfo) -> None:
        """Подтянуть редкость/id из инвентаря, если в уведомлении её нет."""
        if info.cards <= 0:
            return
        if info.ranks and len(info.ranks) >= min(info.cards, 1) and info.user_card_ids:
            return
        inv = await self._fetch_inventory_cards(
            page, limit=max(40, info.cards * 6)
        )
        if not inv:
            return
        matched: List[Dict[str, Any]] = []
        used: set[int] = set(int(x) for x in info.user_card_ids if x)

        def _take(row: Dict[str, Any]) -> None:
            rid = int(row.get("id") or 0)
            if not rid or rid in used:
                return
            matched.append(row)
            used.add(rid)

        for name in info.names:
            name_l = (name or "").strip().lower()
            if not name_l:
                continue
            for row in inv:
                cname = str(row.get("card_name") or "").strip().lower()
                if not cname:
                    continue
                if name_l == cname or name_l in cname or cname in name_l:
                    _take(row)
                    break

        need = max(0, info.cards - len(matched))
        if need:
            for row in inv:
                if int(row.get("in_trade") or 0):
                    continue
                _take(row)
                need -= 1
                if need <= 0:
                    break

        if not matched:
            return

        info.user_card_ids = [int(r.get("id") or 0) for r in matched if r.get("id")]
        info.ranks = [
            str(r.get("rank") or "").strip().upper() for r in matched if r.get("rank")
        ]
        inv_names = [
            str(r.get("card_name") or "").strip() for r in matched if r.get("card_name")
        ]
        if inv_names:
            info.names = inv_names
        logger.info(
            "MangaBuff rarity enrich ranks=%s names=%s ids=%s",
            info.ranks[:4],
            info.names[:4],
            info.user_card_ids[:4],
        )

    async def _post_market_lot(
        self, page: Page, user_card_id: int, price_rank: str, value: int = 1
    ) -> Dict[str, Any]:
        """POST /market/ — выставить карту: цена = value карт ранга price_rank."""
        try:
            return await page.evaluate(
                """async ({id, rank, value}) => {
                  const csrf = (document.querySelector('meta[name="csrf-token"]')
                    || {}).content || '';
                  const body = new URLSearchParams({
                    id: String(id),
                    rank: String(rank),
                    value: String(value)
                  });
                  const resp = await fetch('/market/', {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                      'X-CSRF-TOKEN': csrf,
                      'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: body.toString(),
                    credentials: 'same-origin'
                  });
                  let data = null;
                  try { data = await resp.json(); } catch (e) {
                    try { data = {raw: await resp.text()}; } catch (e2) { data = null; }
                  }
                  return {status: resp.status, data};
                }""",
                {
                    "id": int(user_card_id),
                    "rank": str(price_rank).upper(),
                    "value": int(value),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": 0, "data": {"message": str(exc)}}

    async def _delete_market_lot(self, page: Page, market_id: int) -> Dict[str, Any]:
        """POST /market/<id>/delete — снять лот."""
        try:
            return await page.evaluate(
                """async (id) => {
                  const csrf = (document.querySelector('meta[name="csrf-token"]')
                    || {}).content || '';
                  const resp = await fetch('/market/' + id + '/delete', {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                      'X-CSRF-TOKEN': csrf,
                      'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: '',
                    credentials: 'same-origin'
                  });
                  let data = null;
                  try { data = await resp.json(); } catch (e) {
                    try { data = {raw: await resp.text()}; } catch (e2) { data = null; }
                  }
                  return {status: resp.status, data};
                }""",
                int(market_id),
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": 0, "data": {"message": str(exc)}}

    async def _fetch_own_market_lots(self, page: Page) -> List[Dict[str, Any]]:
        """Свои лоты с /market: market_id, image, price_rank, price_value."""
        if "mangabuff.ru/market" not in (page.url or ""):
            if not await self._safe_goto(page, "https://mangabuff.ru/market"):
                return []
            await self._tempo_pause(0.3, 0.6)
        try:
            items = await page.evaluate(
                """() => {
                  const root = document.querySelector('.market-list__cards--my');
                  if (!root) return [];
                  return [...root.querySelectorAll('.manga-cards__item-wrapper')].map(el => {
                    const imgEl = el.querySelector('.manga-cards__image, img');
                    let image = '';
                    if (imgEl) {
                      image = imgEl.getAttribute('src')
                        || (imgEl.style && imgEl.style.backgroundImage)
                        || '';
                      const m = String(image).match(/url\\([\"']?([^\"')]+)[\"']?\\)/);
                      if (m) image = m[1];
                    }
                    const priceBtn = el.querySelector('.market-list__cards-button:not(.market-list__cards-del-btn)');
                    const priceText = (priceBtn && priceBtn.innerText || el.innerText || '')
                      .replace(/\\s+/g, ' ').trim();
                    return {
                      market_id: String(el.dataset.id || ''),
                      image: String(image || ''),
                      price_text: priceText
                    };
                  });
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("own market lots: %s", exc)
            return []
        out: List[Dict[str, Any]] = []
        for it in items or []:
            mid = int(str(it.get("market_id") or "0") or 0)
            if not mid:
                continue
            value, price_rank = _parse_lot_price_text(str(it.get("price_text") or ""))
            out.append(
                {
                    "market_id": mid,
                    "image": _normalize_card_image(str(it.get("image") or "")),
                    "price_rank": price_rank,
                    "price_value": value,
                    "price_text": str(it.get("price_text") or ""),
                }
            )
        return out

    def _infer_price_mode(
        self, card_rank: str, price_rank: str, price_value: int
    ) -> str:
        exp_h_rank, exp_h_val = market_price_for_mode(card_rank, MARKET_MODE_HIGHER)
        exp_s_rank, exp_s_val = market_price_for_mode(card_rank, MARKET_MODE_SAME2)
        pr = (price_rank or "").upper()
        pv = int(price_value or 0)
        if pr == exp_s_rank and pv == exp_s_val and (
            exp_h_rank != exp_s_rank or exp_h_val != exp_s_val
        ):
            # явно режим 2× тот же (и он отличается от higher)
            if pr == (card_rank or "").upper() and pv == 2:
                return MARKET_MODE_SAME2
        if pr == exp_h_rank and pv == exp_h_val:
            return MARKET_MODE_HIGHER
        if pr == (card_rank or "").upper() and pv == 2:
            return MARKET_MODE_SAME2
        return MARKET_MODE_HIGHER

    async def list_cards_on_market(
        self,
        *,
        limit: Optional[int] = None,
        user_card_ids: Optional[Sequence[int]] = None,
        maintain: bool = True,
        lock_timeout: float = 120.0,
    ) -> MarketListResult:
        """
        Выставить до MARKET_LOT_LIMIT самых дорогих карт.
        Цена: 1× ранг выше; для X (нет выше) — 2×X.
        При maintain=True также переключает цену раз в сутки (1×выше ↔ 2×та же).
        """
        if self._chapter_farm_active():
            logger.info("market list deferred — chapter farm active")
            return MarketListResult(details=["отложено: идёт фарм глав"])
        if not self.is_started:
            await self.start(headless=True)
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=float(lock_timeout))
        except asyncio.TimeoutError:
            logger.warning("market list skipped — browser busy (farm)")
            return MarketListResult(
                details=["пропуск: идёт чтение, повторю позже"]
            )
        try:
            return await self._maintain_market_unlocked(
                limit=limit if limit is not None else MARKET_LOT_LIMIT,
                user_card_ids=user_card_ids,
                do_list=True,
                do_reprice=maintain,
            )
        finally:
            self._lock.release()

    async def maintain_market_lots(self) -> MarketListResult:
        """Фоновая поддержка: топ-10 лотов + суточная смена цены."""
        if self._chapter_farm_active():
            logger.info("market maintain deferred — chapter farm active")
            return MarketListResult(details=["отложено: идёт фарм глав"])
        if not self.is_started:
            await self.start(headless=True)
        # не отбираем браузер у фарма надолго — лучше пропустить час
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=20.0)
        except asyncio.TimeoutError:
            logger.info("market maintain deferred — farm holds lock")
            return MarketListResult(details=["отложено: идёт фарм глав"])
        try:
            return await self._maintain_market_unlocked(
                limit=MARKET_LOT_LIMIT,
                user_card_ids=None,
                do_list=True,
                do_reprice=True,
            )
        finally:
            self._lock.release()

    async def _maintain_market_unlocked(
        self,
        *,
        limit: Optional[int] = None,
        user_card_ids: Optional[Sequence[int]] = None,
        do_list: bool = True,
        do_reprice: bool = True,
    ) -> MarketListResult:
        """Топ-N самых дорогих лотов + суточный флип цены. Под self._lock."""
        result = MarketListResult()
        page = self._page
        assert page is not None
        await self._ensure_login_unlocked()
        await self._refresh_user_id(page)

        if not await self._safe_goto(page, "https://mangabuff.ru/market"):
            result.errors += 1
            result.details.append("не открылась /market")
            return result

        lot_cap = max(1, int(limit) if limit is not None else MARKET_LOT_LIMIT)
        lot_cap = min(lot_cap, MARKET_LOT_LIMIT)

        state = self._load_market_lots_state()
        now = datetime.now(MSK)
        now_iso = now.isoformat()

        inv = await self._fetch_all_inventory_cards(page, per_page=70)
        inv_by_id: Dict[int, Dict[str, Any]] = {}
        inv_by_image: Dict[str, Dict[str, Any]] = {}
        for row in inv:
            uid = int(row.get("id") or 0)
            if not uid:
                continue
            inv_by_id[uid] = row
            img = _normalize_card_image(str(row.get("image") or ""))
            if img:
                inv_by_image[img] = row

        own_lots = await self._fetch_own_market_lots(page)
        own_by_market: Dict[int, Dict[str, Any]] = {
            int(x["market_id"]): x for x in own_lots
        }

        # синхронизация state ↔ живые лоты
        live_user_ids: set[int] = set()
        for lot in own_lots:
            img = lot.get("image") or ""
            row = inv_by_image.get(img)
            if not row:
                continue
            uid = int(row.get("id") or 0)
            if not uid:
                continue
            live_user_ids.add(uid)
            key = str(uid)
            rank = str(row.get("rank") or "").strip().upper()
            name = str(row.get("card_name") or "").strip() or f"#{uid}"
            mode = self._infer_price_mode(
                rank, str(lot.get("price_rank") or ""), int(lot.get("price_value") or 0)
            )
            prev = state.get(key) or {}
            state[key] = {
                "user_card_id": uid,
                "market_id": int(lot["market_id"]),
                "card_rank": rank,
                "name": name,
                "image": img,
                "mode": prev.get("mode") or mode,
                "price_rank": str(lot.get("price_rank") or ""),
                "price_value": int(lot.get("price_value") or 0),
                "listed_at": prev.get("listed_at") or now_iso,
                "mode_since": prev.get("mode_since") or now_iso,
            }

        # проданные / снятые вручную
        for key in list(state.keys()):
            uid = int(key)
            st = state[key]
            mid = int(st.get("market_id") or 0)
            if uid in live_user_ids:
                continue
            if mid and mid not in own_by_market:
                result.removed += 1
                result.details.append(
                    f"{format_rank_label(str(st.get('card_rank') or ''))} "
                    f"{st.get('name') or key}: продана/снята"
                )
                del state[key]

        # топ самых дорогих карт, которые можно держать в лотах
        tradable_pool = [
            r
            for r in inv
            if int(r.get("id") or 0)
            and not int(r.get("is_not_tradable") or 0)
            and not int(r.get("is_lock") or 0)
            and str(r.get("rank") or "").strip()
        ]
        tradable_pool.sort(key=card_value_key, reverse=True)
        desired_rows = tradable_pool[:lot_cap]
        desired_ids = {int(r.get("id") or 0) for r in desired_rows}

        # снять лишние / более дешёвые лоты, чтобы освободить слоты под топ
        for key in list(state.keys()):
            uid = int(key)
            if uid in desired_ids:
                continue
            st = state[key]
            mid = int(st.get("market_id") or 0)
            rank = str(st.get("card_rank") or "")
            name = str(st.get("name") or f"#{uid}")
            if mid:
                del_resp = await self._delete_market_lot(page, mid)
                del_status = int((del_resp or {}).get("status") or 0)
                if 200 <= del_status < 300:
                    result.removed += 1
                    result.details.append(
                        f"{format_rank_label(rank)} {name}: снят (не в топ-{lot_cap})"
                    )
                    live_user_ids.discard(uid)
                    state.pop(key, None)
                else:
                    result.errors += 1
                    result.details.append(
                        f"{format_rank_label(rank)} {name}: не снят лишний лот"
                    )
                await self._tempo_pause(0.3, 0.55)
            else:
                state.pop(key, None)
                live_user_ids.discard(uid)

        # суточная смена режима цены — только для лотов из топа
        if do_reprice:
            for key, st in list(state.items()):
                uid = int(key)
                if uid not in desired_ids:
                    continue
                row = inv_by_id.get(uid)
                if not row:
                    continue
                rank = str(st.get("card_rank") or row.get("rank") or "").upper()
                name = str(st.get("name") or row.get("card_name") or f"#{uid}")
                mode = str(st.get("mode") or MARKET_MODE_HIGHER)
                mode_since_raw = str(st.get("mode_since") or "")
                try:
                    mode_since = datetime.fromisoformat(mode_since_raw)
                    if mode_since.tzinfo is None:
                        mode_since = mode_since.replace(tzinfo=MSK)
                except Exception:  # noqa: BLE001
                    mode_since = now - MARKET_REPRICE_AFTER - timedelta(minutes=1)

                exp_rank, exp_val = market_price_for_mode(rank, mode)
                cur_rank = str(st.get("price_rank") or "").upper()
                cur_val = int(st.get("price_value") or 0)
                due = (now - mode_since) >= MARKET_REPRICE_AFTER
                mismatch = cur_rank != exp_rank or cur_val != exp_val

                if due:
                    new_mode = (
                        MARKET_MODE_SAME2
                        if mode == MARKET_MODE_HIGHER
                        else MARKET_MODE_HIGHER
                    )
                    new_rank, new_val = market_price_for_mode(rank, new_mode)
                    # для X higher==same2 (оба 2×X) — флип бессмысленен
                    if (new_rank, new_val) == (exp_rank, exp_val) and not mismatch:
                        st["mode_since"] = now_iso
                        state[key] = st
                        continue
                    mode = new_mode
                    exp_rank, exp_val = new_rank, new_val
                    mismatch = True

                if not mismatch:
                    continue

                mid = int(st.get("market_id") or 0)
                if mid:
                    del_resp = await self._delete_market_lot(page, mid)
                    del_status = int((del_resp or {}).get("status") or 0)
                    if not (200 <= del_status < 300):
                        result.errors += 1
                        result.details.append(
                            f"{format_rank_label(rank)} {name}: не снялся лот {mid}"
                        )
                        await self._tempo_pause(0.25, 0.5)
                        continue
                    await self._tempo_pause(0.35, 0.7)

                ok, msg = await self._create_lot_only(
                    page, uid, exp_rank, exp_val
                )
                if ok:
                    result.repriced += 1
                    state[key] = {
                        "user_card_id": uid,
                        "market_id": 0,  # привяжем пачкой ниже
                        "card_rank": rank,
                        "name": name,
                        "image": _normalize_card_image(str(row.get("image") or "")),
                        "mode": mode,
                        "price_rank": exp_rank,
                        "price_value": exp_val,
                        "listed_at": st.get("listed_at") or now_iso,
                        "mode_since": now_iso,
                    }
                    result.details.append(
                        f"{format_rank_label(rank)} {name}: "
                        f"{cur_val}×{cur_rank or '?'} → {exp_val}×{exp_rank}"
                    )
                    live_user_ids.add(uid)
                else:
                    result.errors += 1
                    result.details.append(
                        f"{format_rank_label(rank)} {name}: переоценка — {msg}"
                    )
                    state.pop(key, None)
                await self._tempo_pause(0.25, 0.45)

        # выставить недостающие из топ-N (самые дорогие)
        if do_list:
            want_ids = {int(x) for x in (user_card_ids or []) if x}
            candidates = list(desired_rows)
            # новые дропы из want_ids — первыми среди топа
            if want_ids:
                candidates.sort(
                    key=lambda r: (
                        0 if int(r.get("id") or 0) in want_ids else 1,
                        -rank_value(str(r.get("rank") or "")),
                    )
                )

            free_slots = max(0, lot_cap - len(live_user_ids))
            for row in candidates:
                if free_slots <= 0:
                    break
                uid = int(row.get("id") or 0)
                if not uid or uid in live_user_ids:
                    result.skipped += 1
                    continue
                if int(row.get("in_trade") or 0):
                    # уже в лоте, но не в live_user_ids — пропускаем
                    result.skipped += 1
                    continue

                rank = str(row.get("rank") or "").strip().upper()
                name = str(row.get("card_name") or "").strip() or f"#{uid}"
                image = _normalize_card_image(str(row.get("image") or ""))
                if not rank:
                    result.skipped += 1
                    continue

                mode = MARKET_MODE_HIGHER
                price_rank, price_value = market_price_for_mode(rank, mode)
                ok, msg = await self._create_lot_only(
                    page, uid, price_rank, price_value
                )
                if ok:
                    free_slots -= 1
                    result.listed += 1
                    live_user_ids.add(uid)
                    state[str(uid)] = {
                        "user_card_id": uid,
                        "market_id": 0,
                        "card_rank": rank,
                        "name": name,
                        "image": image,
                        "mode": mode,
                        "price_rank": price_rank,
                        "price_value": price_value,
                        "listed_at": now_iso,
                        "mode_since": now_iso,
                    }
                    result.details.append(
                        f"{format_rank_label(rank)} {name} → {price_value}×{price_rank}"
                    )
                    logger.info(
                        "MangaBuff market listed id=%s rank=%s price=%s×%s",
                        uid,
                        rank,
                        price_rank,
                        price_value,
                    )
                else:
                    result.errors += 1
                    result.details.append(
                        f"{format_rank_label(rank)} {name}: {msg}"
                    )
                    if "429" in msg or "too many" in msg.lower():
                        result.details.append(
                            "пауза из‑за лимита сайта, продолжим позже"
                        )
                        break
                await self._tempo_pause(0.8, 1.4)

        # привязать market_id по картинкам одним проходом
        if result.listed or result.repriced or result.removed:
            await self._safe_goto(page, "https://mangabuff.ru/market")
            await self._tempo_pause(0.3, 0.6)
            own = await self._fetch_own_market_lots(page)
            by_img = {x["image"]: x for x in own if x.get("image")}
            for key, st in list(state.items()):
                img = st.get("image") or ""
                lot = by_img.get(img)
                if not lot:
                    continue
                st["market_id"] = int(lot.get("market_id") or 0)
                st["price_rank"] = str(
                    lot.get("price_rank") or st.get("price_rank") or ""
                )
                st["price_value"] = int(
                    lot.get("price_value") or st.get("price_value") or 0
                )
                state[key] = st

        self._save_market_lots_state(state)
        if result.listed or result.repriced or result.removed:
            self.stats.touch(
                f"площадка: +{result.listed} / ↔{result.repriced} / −{result.removed}",
                page.url,
            )
            self._persist_stats()
        return result

    async def _create_lot_only(
        self,
        page: Page,
        user_card_id: int,
        price_rank: str,
        price_value: int,
    ) -> Tuple[bool, str]:
        """POST /market/ без перезагрузки страницы (с backoff на 429)."""
        msg = ""
        for attempt in range(4):
            resp = await self._post_market_lot(
                page, user_card_id, price_rank, price_value
            )
            status = int((resp or {}).get("status") or 0)
            payload = (resp or {}).get("data") if isinstance(resp, dict) else {}
            msg = ""
            if isinstance(payload, dict):
                msg = str(payload.get("message") or payload.get("raw") or "")
            if 200 <= status < 300:
                return True, msg or "ok"
            if status == 429 or "too many" in msg.lower():
                wait_for = 8.0 + attempt * 6.0
                logger.warning(
                    "MangaBuff market 429 id=%s — sleep %.0fs (try %s)",
                    user_card_id,
                    wait_for,
                    attempt + 1,
                )
                await asyncio.sleep(wait_for)
                continue
            return False, (msg or f"HTTP {status}")[:120]
        return False, (msg or "HTTP 429")[:120]

    async def _harvest_notifications_feed(self, page: Page) -> CardDropInfo:
        """
        Главный источник: лента https://mangabuff.ru/notifications
        Элементы .notifications__item — карты за чтение попадают сюда.
        """
        info = CardDropInfo(source=page.url)
        try:
            # фильтр «непрочитанные» — без select_option (скрытый select часто висит)
            try:
                await page.evaluate(
                    """() => {
                      const sel = document.querySelector('select.sort-select, select[name=sort]');
                      if (!sel) return;
                      sel.value = 'not_read';
                      sel.dispatchEvent(new Event('change', {bubbles: true}));
                    }"""
                )
                await self._tempo_pause(0.2, 0.45)
            except Exception:  # noqa: BLE001
                pass

            items = await page.evaluate(
                """() => {
                  const nodes = [...document.querySelectorAll('.notifications__item')];
                  return nodes.slice(0, 40).map((el, idx) => {
                    const id = el.getAttribute('data-id')
                      || el.dataset.id
                      || el.id
                      || '';
                    const link = el.getAttribute('href')
                      || (el.querySelector('a') && el.querySelector('a').getAttribute('href'))
                      || '';
                    const ranks = [...el.querySelectorAll('[data-rank]')]
                      .map(n => (n.dataset.rank || '').toUpperCase())
                      .filter(Boolean);
                    const cardNotify = [...el.querySelectorAll('.card-notification')].map(n => ({
                      name: n.getAttribute('data-card-name') || '',
                      image: n.getAttribute('data-card-image') || '',
                      cardId: n.getAttribute('data-card-id') || '',
                      rank: (n.getAttribute('data-card-rank') || n.dataset.rank || '').toUpperCase()
                    }));
                    return {
                      id: String(id || ''),
                      unread: el.classList.contains('notifications__item--not-read'),
                      text: (el.innerText || '').trim().slice(0, 400),
                      href: String(link || ''),
                      ranks,
                      cardNotify,
                      idx: idx
                    };
                  });
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("notifications feed: %s", exc)
            return info

        if not items:
            logger.info("MangaBuff notifications: empty feed on %s", page.url)
            return info

        for it in items:
            text = (it.get("text") or "").strip()
            n_cards, n_scroll, names, ranks = self._parse_card_notify_text(text)
            for cn in it.get("cardNotify") or []:
                nm = str((cn or {}).get("name") or "").strip()
                rk = str((cn or {}).get("rank") or "").strip().upper()
                if nm and nm not in names:
                    names.append(nm)
                if rk and rk not in ranks:
                    ranks.append(rk)
                if n_cards <= 0 and (nm or rk):
                    n_cards = 1
            for rk in it.get("ranks") or []:
                rk = str(rk or "").strip().upper()
                if rk and rk not in ranks:
                    ranks.append(rk)
            if n_cards <= 0 and n_scroll <= 0:
                continue
            key = (
                str(it.get("id") or "").strip()
                or str(it.get("href") or "").strip()
                or f"hash:{hash(text) & 0xFFFFFFFF:x}"
            )
            if not self._remember_notification(key):
                continue
            info.cards += n_cards
            info.scrolls += n_scroll
            for n in names:
                if n not in info.names:
                    info.names.append(n)
            for r in ranks:
                if r and r not in info.ranks:
                    info.ranks.append(r)
            info.raw = text[:200]
            logger.info(
                "MangaBuff notify-feed card +%s scroll +%s ranks=%s key=%s text=%s",
                n_cards,
                n_scroll,
                ranks[:3],
                key,
                text[:120].replace("\n", " | "),
            )

        if info.cards or info.scrolls:
            if info.cards:
                self.stats.cards_claimed += info.cards
                line = info.cards_line(2)
                self.stats.last_card_drop = (
                    f"+{info.cards} · {line}" if line else f"+{info.cards}"
                )
            if info.scrolls:
                self.stats.scrolls_claimed += info.scrolls
            self.stats.touch(
                f"уведомления: карт +{info.cards}"
                + (f" свит +{info.scrolls}" if info.scrolls else ""),
                page.url,
            )
            self._persist_stats()
            await self._emit_card_drop(info, page)
        else:
            self._persist_stats()
        return info

    async def _harvest_reader_toast(self, page: Page) -> CardDropInfo:
        """Тост .reader__notification в читалке (socket new-notify)."""
        info = CardDropInfo(source="reader-toast")
        try:
            blobs = await page.evaluate(
                """() => [...document.querySelectorAll('.reader__notification, .club-card-notify, .mb-toast-wrap .mb-toast, .card-notification')]
                  .map(el => ({
                    text: (el.innerText || '').trim().slice(0, 300),
                    html: (el.innerHTML || '').trim().slice(0, 500),
                    rank: (el.getAttribute('data-card-rank') || el.dataset.rank || '').toUpperCase(),
                    name: el.getAttribute('data-card-name') || ''
                  }))
                  .filter(x => x.text || x.name)
                  .slice(0, 6)"""
            )
        except Exception:  # noqa: BLE001
            blobs = []
        for raw in blobs or []:
            if isinstance(raw, str):
                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
                rank_hint = ""
                name_hint = ""
            else:
                text = re.sub(r"<[^>]+>", " ", str(raw.get("text") or raw.get("html") or ""))
                text = re.sub(r"\s+", " ", text).strip()
                rank_hint = str(raw.get("rank") or "").upper()
                name_hint = str(raw.get("name") or "").strip()
            n_cards, n_scroll, names, ranks = self._parse_card_notify_text(text)
            if name_hint and name_hint not in names:
                names.insert(0, name_hint)
            if rank_hint and rank_hint not in ranks:
                ranks.insert(0, rank_hint)
            if n_cards <= 0 and n_scroll <= 0 and not (name_hint or rank_hint):
                continue
            if n_cards <= 0 and (name_hint or rank_hint):
                n_cards = 1
            key = f"toast:{hash(text or name_hint) & 0xFFFFFFFF:x}"
            if not self._remember_notification(key):
                continue
            info.cards += n_cards
            info.scrolls += n_scroll
            info.names.extend(names)
            for r in ranks:
                if r and r not in info.ranks:
                    info.ranks.append(r)
            info.raw = (text or name_hint)[:200]
        if info.cards or info.scrolls:
            if info.cards:
                self.stats.cards_claimed += info.cards
                line = info.cards_line(2)
                self.stats.last_card_drop = (
                    f"+{info.cards} · {line}" if line else f"+{info.cards}"
                )
            if info.scrolls:
                self.stats.scrolls_claimed += info.scrolls
            self.stats.touch(f"тост: карт +{info.cards}", page.url)
            self._persist_stats()
            logger.info(
                "MangaBuff reader toast +%s scrolls +%s ranks=%s names=%s",
                info.cards,
                info.scrolls,
                info.ranks[:3],
                info.names[:3],
            )
            await self._emit_card_drop(info, page)
        return info

    # ------------------------------------------------------------------
    # Catalog / reading
    # ------------------------------------------------------------------

    async def fetch_popular_titles(self, limit: int = 30) -> List[dict]:
        assert self._page is not None
        page = self._page
        await self._safe_goto(page, TOP_URL)
        titles = await page.evaluate(
            """(limit) => {
              const as = [...document.querySelectorAll('a[href*="/manga/"]')];
              const out = [];
              const seen = new Set();
              for (const a of as) {
                const h = a.href.split('?')[0];
                const m = h.match(/mangabuff\\.ru\\/manga\\/([^\\/\\?#]+)$/);
                if (!m) continue;
                const slug = m[1];
                if (['top','genre','genres'].includes(slug)) continue;
                if (seen.has(slug)) continue;
                seen.add(slug);
                const title = (a.innerText || a.getAttribute('title') || slug)
                  .trim().split('\\n')[0].slice(0, 80);
                out.push({slug, title, href: h});
                if (out.length >= limit) break;
              }
              return out;
            }""",
            limit,
        )
        # Дополнительно — каталог
        if len(titles) < limit:
            await self._safe_goto(page, CATALOG_URL)
            more = await page.evaluate(
                """(limit) => {
                  const as = [...document.querySelectorAll('a[href*="/manga/"]')];
                  const out = [];
                  const seen = new Set();
                  for (const a of as) {
                    const h = a.href.split('?')[0];
                    const m = h.match(/mangabuff\\.ru\\/manga\\/([^\\/\\?#]+)$/);
                    if (!m) continue;
                    const slug = m[1];
                    if (seen.has(slug)) continue;
                    seen.add(slug);
                    out.push({slug, title: (a.innerText||slug).trim().split('\\n')[0].slice(0,80), href:h});
                    if (out.length >= limit) break;
                  }
                  return out;
                }""",
                limit,
            )
            have = {t["slug"] for t in titles}
            for t in more:
                if t["slug"] not in have:
                    titles.append(t)
                    have.add(t["slug"])
                if len(titles) >= limit:
                    break
        random.shuffle(titles)
        # чуть чаще оставляем топовые в начале
        return titles[:limit]

    async def _estimate_title_chapters(self, slug: str) -> int:
        """
        Оценить число глав тайтла по странице /manga/{slug}.
        0 = неизвестно (тогда читаем до конца по next-chapter).
        """
        if not slug or not self._page:
            return 0
        page = self._page
        title_url = f"https://mangabuff.ru/manga/{slug}"
        try:
            if not await self._safe_goto(page, title_url):
                return 0
            await self._dismiss_overlays(page)
            await self._tempo_pause(0.8, 1.6)
            data = await page.evaluate(
                """() => {
                  const text = document.body ? (document.body.innerText || '') : '';
                  const hrefs = [...document.querySelectorAll('a[href*="/manga/"]')]
                    .map(el => el.getAttribute('href') || '');
                  const nums = [];
                  for (const h of hrefs) {
                    const m = h.match(/\\/manga\\/[^/]+\\/(\\d+)\\/(\\d+)/);
                    if (m) nums.push(parseInt(m[2], 10));
                  }
                  let fromText = 0;
                  const m1 = text.match(/(\\d+)\\s*глав/i);
                  if (m1) fromText = parseInt(m1[1], 10) || 0;
                  if (!fromText) {
                    const m2 = text.match(/глав[аыи]?\\s*[:\\-]?\\s*(\\d+)/i);
                    if (m2) fromText = parseInt(m2[1], 10) || 0;
                  }
                  const fromLinks = nums.length ? Math.max.apply(null, nums) : 0;
                  return { fromText, fromLinks, n: nums.length };
                }"""
            )
            from_text = int((data or {}).get("fromText") or 0)
            from_links = int((data or {}).get("fromLinks") or 0)
            # если в тексте явно «12 глав», а в ссылках только превью — верим тексту,
            # но не меньше max по ссылкам
            total = max(from_text, from_links)
            if total <= 0:
                return 0
            # отсечь мусорные выбросы
            if total > 20000:
                total = from_links or from_text
            logger.info(
                "MangaBuff title %s chapters≈%s (text=%s links=%s)",
                slug,
                total,
                from_text,
                from_links,
            )
            return total
        except Exception as exc:  # noqa: BLE001
            logger.info("MangaBuff chapter estimate failed for %s: %s", slug, exc)
            return 0

    def _chapters_target_for_title(self, total_chapters: int) -> int:
        """Сколько глав читать: не меньше 90% тайтла (по порядку с 1-й)."""
        if total_chapters <= 0:
            return TITLE_READ_HARD_CAP
        if total_chapters <= 3:
            return total_chapters
        # ceil, чтобы на коротких тайтлах не уйти ниже 90%
        target = int(total_chapters * TITLE_READ_RATIO + 0.999999)
        return max(1, min(target, total_chapters))

    def _is_sequential_next(self, before_url: str, after_url: str) -> bool:
        """True только если after = следующая глава (ch+1) или следующий том /1."""
        a = self._parse_chapter_url(before_url)
        b = self._parse_chapter_url(after_url)
        if not a or not b:
            return False
        slug_a, vol_a, ch_a = a
        slug_b, vol_b, ch_b = b
        if slug_a != slug_b:
            return False
        if vol_b == vol_a and ch_b == ch_a + 1:
            return True
        if vol_b == vol_a + 1 and ch_b == 1:
            return True
        return False

    async def _reader_resume_chapter(self, page: Page) -> int:
        """Номер следующей непрочитанной главы (current_chapter.current на reader)."""
        try:
            n = await page.evaluate(
                """() => {
                  const ch = window.current_chapter;
                  if (!ch) return 0;
                  const cur = parseInt(ch.current, 10);
                  return Number.isFinite(cur) ? cur : 0;
                }"""
            )
            return max(0, int(n or 0))
        except Exception:  # noqa: BLE001
            return 0

    async def _probe_title_resume_chapter(self, page: Page, slug: str) -> int:
        """Узнать, с какой главы продолжать чтение тайтла."""
        probe_url = f"https://mangabuff.ru/manga/{slug}/1/1"
        if not await self._safe_goto(page, probe_url):
            return 1
        await self._dismiss_overlays(page)
        if not await self._wait_for_chapter_context(page, timeout_sec=6.0):
            return 1
        resume = await self._reader_resume_chapter(page)
        return max(1, resume or 1)

    async def _open_first_chapter(self, title_href: str) -> Optional[str]:
        assert self._page is not None
        page = self._page
        slug_m = re.search(r"/manga/([^/?#]+)/?$", title_href.rstrip("/"))
        slug = slug_m.group(1) if slug_m else ""

        async def _ok_reader() -> Optional[str]:
            await self._dismiss_overlays(page)
            title = ""
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                pass
            if "404" in title.lower():
                return None
            if re.search(r"/manga/[^/]+/\d+/\d+", page.url):
                if page.url.rstrip("/").endswith("/0") and slug:
                    await self._safe_goto(page, f"https://mangabuff.ru/manga/{slug}/1/1")
                    await self._dismiss_overlays(page)
                if re.search(r"/manga/[^/]+/\d+/\d+", page.url):
                    return page.url.split("?")[0]
            return None

        # С первой НЕпрочитанной главы — иначе сайт не засчитывает повторное чтение.
        if slug:
            resume = await self._probe_title_resume_chapter(page, slug)
            total = 0
            try:
                total = int(
                    await page.evaluate(
                        """() => parseInt(
                          (window.current_chapter && window.current_chapter.total) || '0',
                          10
                        )"""
                    )
                    or 0
                )
            except Exception:  # noqa: BLE001
                total = 0
            if total > 0 and resume > total:
                logger.info(
                    "MangaBuff title %s fully read (resume=%s total=%s)",
                    slug,
                    resume,
                    total,
                )
                return None
            upper = resume + 4 if total <= 0 else min(resume + 4, total)
            for start_ch in range(resume, upper + 1):
                candidate = f"https://mangabuff.ru/manga/{slug}/1/{start_ch}"
                if await self._safe_goto(page, candidate):
                    got = await _ok_reader()
                    if got:
                        logger.info(
                            "MangaBuff resume %s from ch %s (next unread=%s)",
                            slug,
                            start_ch,
                            resume,
                        )
                        return got

        if not await self._safe_goto(page, title_href):
            return None
        await self._dismiss_overlays(page)
        try:
            for name in (
                r"^Читать$",
                r"Продолжить с\s*1",
                r"Продолжить",
                r"Начать чтение",
                r"Читать с начала",
            ):
                read_btn = page.get_by_role("link", name=re.compile(name, re.I))
                if await read_btn.count():
                    await read_btn.first.click()
                    await self._tempo_pause(2.0, 3.5)
                    got = await _ok_reader()
                    if got:
                        return got
        except Exception:  # noqa: BLE001
            pass
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href*='/manga/']",
                """els => els.map(e => e.href)
                   .filter(h => /\\/manga\\/[^/]+\\/\\d+\\/\\d+/.test(h))""",
            )
            nums = []
            for h in hrefs:
                m = re.search(r"/manga/[^/]+/(\d+)/(\d+)", h)
                if m:
                    nums.append((int(m.group(1)), int(m.group(2)), h.split("?")[0]))
            if nums:
                nums.sort()
                for _vol, chn, h in nums:
                    if chn >= 1:
                        await self._safe_goto(page, h)
                        await self._dismiss_overlays(page)
                        return page.url
        except Exception as exc:  # noqa: BLE001
            logger.warning("open first chapter: %s", exc)
        return None

    async def _smooth_read_chapter(self, page: Page) -> int:
        """Доскроллить главу до конца строго по пресету steps/delay."""
        steps = 0
        chapter_url = page.url.split("?")[0]
        tier = self._tempo_tier()
        try:
            total_height = await page.evaluate(
                "() => document.body.scrollHeight || 4000"
            )
            viewport = await page.evaluate("() => window.innerHeight || 900")
        except Exception:  # noqa: BLE001
            total_height, viewport = 4000, 900

        viewport = max(int(viewport), 600)
        max_steps, chunk = self._scroll_plan(int(total_height), viewport)
        # turbo/fast: почти без запаса; медленные: чуть больше на lazy-load
        hard_cap = max_steps + (0 if tier in ("turbo", "fast") else 2)
        position = 0
        logger.info(
            "MangaBuff scroll start height=%s viewport=%s steps=%s/%s "
            "chunk=%spx(%.1fx) delay=%.2f-%.2f tier=%s",
            total_height,
            viewport,
            max_steps,
            hard_cap,
            chunk,
            chunk / viewport,
            self.delay_min_sec,
            self.delay_max_sec,
            tier,
        )
        while position + viewport < total_height - 60 and steps < hard_cap:
            if self._stop_flag.is_set() or self._skip_title.is_set():
                break
            if page.url.split("?")[0] != chapter_url and not re.search(
                r"/manga/[^/]+/\d+/\d+", page.url
            ):
                logger.warning(
                    "MangaBuff left chapter during scroll (%s) → restore %s",
                    page.url,
                    chapter_url,
                )
                if not await self._safe_goto(page, chapter_url):
                    break
                await self._dismiss_overlays(page)

            target = min(position + chunk, int(total_height))
            await self._animate_scroll(page, position, target)
            position = target
            steps += 1
            self.stats.pages_scrolled += 1

            delay = random.uniform(self.delay_min_sec, self.delay_max_sec)
            try:
                await asyncio.wait_for(self._stop_flag.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

            try:
                new_h = int(
                    await page.evaluate("() => document.body.scrollHeight || 4000")
                )
                if new_h > total_height:
                    total_height = new_h
                    left = max(1, hard_cap - steps)
                    # добить остаток крупнее, не добавляя шагов в турбо
                    chunk = max(chunk, int((total_height - position) / left))
            except Exception:  # noqa: BLE001
                pass

        try:
            await page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"
            )
            await self._tempo_pause(0.12, 0.35)
        except Exception:  # noqa: BLE001
            pass
        await self._scroll_until_site_read(page)
        try:
            reward_timeout = 3.0 if tier in ("turbo", "fast") else 8.0
            c, cards, scrolls, chests, packs, _ = await asyncio.wait_for(
                self._click_reward_buttons(page), timeout=reward_timeout
            )
            self.stats.rewards_claimed += c
            self.stats.cards_claimed += cards
            self.stats.scrolls_claimed += scrolls
            self.stats.chests_opened += chests
            self.stats.packs_opened += packs
            await self._harvest_reader_toast(page)
            await self._harvest_card_drops(page, source="chapter-end")
        except Exception:  # noqa: BLE001
            pass
        return steps

    async def _wait_for_chapter_context(self, page: Page, timeout_sec: float = 8.0) -> bool:
        """Дождаться window.current_chapter — без него addHistory не сработает."""
        deadline = time_mod.time() + max(1.0, float(timeout_sec))
        while time_mod.time() < deadline:
            if self._stop_flag.is_set():
                return False
            try:
                ok = await page.evaluate(
                    """() => !!(window.current_chapter
                      && window.current_chapter.id
                      && window.current_chapter.chapter_id)"""
                )
                if ok:
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.35)
        return False

    async def _scroll_until_site_read(self, page: Page) -> bool:
        """
        Прокрутить главу так, чтобы reader.js выставил is_read=true (≥50% высоты).
        Без этого addHistory() на сайте не срабатывает — только комментарии «живут».
        """
        tier = self._tempo_tier()
        pause = 0.12 if tier == "turbo" else (0.22 if tier == "fast" else 0.45)
        deadline = time_mod.time() + (35.0 if tier in ("turbo", "fast") else 55.0)
        chapter_url = page.url.split("?")[0]

        async def _fire_scroll(target_y: int) -> None:
            await page.evaluate(
                """(ty) => {
                  window.scrollTo(0, ty);
                  window.dispatchEvent(new Event('scroll'));
                  if (window.jQuery) window.jQuery(window).trigger('scroll');
                }""",
                int(target_y),
            )

        while time_mod.time() < deadline and not self._stop_flag.is_set():
            if page.url.split("?")[0] != chapter_url:
                break
            state = await page.evaluate(
                """() => ({
                  is_read: (typeof is_read !== 'undefined') ? !!is_read : false,
                  y: window.scrollY || 0,
                  h: document.body.scrollHeight || 0,
                  vh: window.innerHeight || 900,
                })"""
            )
            if state.get("is_read"):
                return True
            h = max(int(state.get("h") or 0), 1200)
            vh = max(int(state.get("vh") or 0), 600)
            y = int(state.get("y") or 0)
            half = max(int((h - vh) * 0.52), vh)
            step = max(int(vh * 0.45), 350) if y + vh < half else max(int(vh * 0.85), 500)
            target = min(y + step, max(0, h - vh))
            await _fire_scroll(target)
            await asyncio.sleep(pause)

        for _ in range(10):
            if self._stop_flag.is_set():
                break
            state = await page.evaluate(
                """() => ({
                  is_read: (typeof is_read !== 'undefined') ? !!is_read : false,
                  h: document.body.scrollHeight || 0,
                  y: window.scrollY || 0,
                  vh: window.innerHeight || 900,
                })"""
            )
            if state.get("is_read"):
                return True
            h = int(state.get("h") or 0)
            y = int(state.get("y") or 0)
            vh = int(state.get("vh") or 0)
            if y + vh >= h - 40:
                break
            await _fire_scroll(max(0, h - vh))
            await asyncio.sleep(0.35 if tier in ("turbo", "fast") else 0.6)

        ok = bool(
            await page.evaluate(
                "(typeof is_read !== 'undefined') ? !!is_read : false"
            )
        )
        if not ok:
            logger.warning(
                "MangaBuff is_read still false after scroll: %s", chapter_url
            )
        return ok

    async def _wait_for_native_history_flush(
        self,
        page: Page,
        *,
        pool_before: int,
        before_resume: int,
        timeout_sec: float = 12.0,
    ) -> int:
        """Дождаться, пока reader.js отправит history_pool, и проверить рост current."""
        if pool_before <= 0:
            return 0
        deadline = time_mod.time() + max(2.0, float(timeout_sec))
        while time_mod.time() < deadline:
            pool = await self._history_pool_size(page)
            after_resume = await self._reader_resume_chapter(page)
            progress = max(0, after_resume - before_resume)
            if pool < pool_before:
                if progress > 0:
                    self._last_history_post_at = time_mod.time()
                    return progress
                logger.info(
                    "MangaBuff native flush cleared pool but progress=0 (re-read?)"
                )
                return 0
            if progress > 0:
                self._last_history_post_at = time_mod.time()
                return progress
            await asyncio.sleep(0.35)
        return 0

    async def _history_pool_size(self, page: Page) -> int:
        try:
            n = await page.evaluate(
                """() => {
                  try {
                    const items = JSON.parse(localStorage.getItem('history_pool') || '[]');
                    return Array.isArray(items) ? items.length : 0;
                  } catch (e) { return 0; }
                }"""
            )
            return max(0, int(n or 0))
        except Exception:  # noqa: BLE001
            return 0

    async def _queue_chapter_in_pool(self, page: Page) -> Optional[Dict[str, Any]]:
        """Поставить главу в history_pool через нативный reader.js addHistory()."""
        try:
            meta = await page.evaluate(
                """() => {
                  const ch = window.current_chapter;
                  if (!ch || !ch.id || !ch.chapter_id) {
                    return {ok: false, reason: 'no current_chapter', url: location.href};
                  }
                  if (typeof is_read === 'undefined' || !is_read) {
                    return {
                      ok: false,
                      reason: 'not_read_yet',
                      chapter: String(ch.chapter || ''),
                      url: location.href
                    };
                  }
                  if (typeof read_status_send !== 'undefined' && read_status_send) {
                    return {
                      ok: false,
                      reason: 'already_sent',
                      chapter: String(ch.chapter || ''),
                      url: location.href
                    };
                  }
                  if (typeof addHistory !== 'function') {
                    return {ok: false, reason: 'no addHistory', url: location.href};
                  }
                  let before = 0;
                  try {
                    before = JSON.parse(localStorage.getItem('history_pool') || '[]').length;
                  } catch (e) { before = 0; }
                  addHistory();
                  let after = before;
                  try {
                    after = JSON.parse(localStorage.getItem('history_pool') || '[]').length;
                  } catch (e) { after = before; }
                  return {
                    ok: true,
                    manga_id: ch.id,
                    chapter_id: ch.chapter_id,
                    chapter: String(ch.chapter || ''),
                    slug: String(ch.slug || ''),
                    pool: after,
                    pool_before: before,
                    ccl: Number(window.ccl || 2),
                    is_read: !!is_read,
                    read_status_send: !!read_status_send,
                    url: location.href
                  };
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("queue chapter pool: %s", exc)
            return None
        if not meta or not meta.get("ok"):
            logger.warning(
                "MangaBuff chapter NOT queued: %s (%s)",
                (meta or {}).get("reason") or "unknown",
                (meta or {}).get("chapter") or (meta or {}).get("url") or "",
            )
            return None
        return meta

    async def _flush_history_pool_confirmed(
        self, page: Page, *, before_resume: int = 0, max_retries: int = 5
    ) -> int:
        """POST /addHistory с паузой и повторами. Возвращает главы по росту current."""
        for attempt in range(1, max_retries + 1):
            if self._stop_flag.is_set():
                return 0
            now = time_mod.time()
            gap = self._history_min_gap_sec
            if (now - self._last_history_post_at) < gap:
                wait_for = max(0.2, gap - (now - self._last_history_post_at))
                logger.info(
                    "MangaBuff addHistory throttle %.1fs (attempt %s/%s)",
                    wait_for,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(wait_for)
            if attempt > 1:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                    await self._dismiss_overlays(page)
                    await self._wait_for_chapter_context(page, timeout_sec=5.0)
                except Exception:  # noqa: BLE001
                    pass
            count, gift = await self._flush_history_pool(page)
            after_resume = await self._reader_resume_chapter(page)
            progress = max(0, after_resume - before_resume)
            if progress > 0:
                if gift and (gift.cards or gift.scrolls):
                    await self._emit_card_drop(gift, page)
                self.stats.chapters_pending = await self._history_pool_size(page)
                return progress
            if count > 0:
                logger.info(
                    "MangaBuff addHistory status=OK but progress=0 "
                    "(re-read or duplicate, posted=%s)",
                    count,
                )
                self.stats.chapters_pending = await self._history_pool_size(page)
                return 0
            pool = await self._history_pool_size(page)
            if pool <= 0:
                return 0
            backoff = 8.0 + (attempt - 1) * 6.0
            logger.warning(
                "MangaBuff addHistory retry in %.0fs (pool=%s, attempt %s/%s)",
                backoff,
                pool,
                attempt,
                max_retries,
            )
            await asyncio.sleep(backoff)
        return 0

    async def _register_chapter_read(self, page: Page, *, force_flush: bool = False) -> int:
        """
        Засчитать главу на сайте через history_pool + POST /addHistory.

        Возвращает число глав, реально принятых сайтом (по росту current_chapter.current).
        """
        before_resume = await self._reader_resume_chapter(page)
        meta: Optional[Dict[str, Any]] = None
        for attempt in range(2):
            meta = await self._queue_chapter_in_pool(page)
            if meta:
                break
            if attempt == 0:
                await self._scroll_until_site_read(page)
                await asyncio.sleep(0.25)
        if not meta:
            self.stats.chapters_pending = await self._history_pool_size(page)
            if force_flush:
                return await self._flush_history_pool_confirmed(
                    page, before_resume=before_resume
                )
            return 0

        pool = int(meta.get("pool") or 0)
        ccl = max(1, int(meta.get("ccl") or 2))
        self.stats.chapters_pending = pool
        logger.info(
            "MangaBuff site-read queued manga=%s ch=%s pool=%s/%s native=1 resume=%s",
            meta.get("manga_id"),
            meta.get("chapter") or meta.get("chapter_id"),
            pool,
            ccl,
            before_resume,
        )

        if pool >= ccl:
            native_flushed = await self._wait_for_native_history_flush(
                page, pool_before=pool, before_resume=before_resume
            )
            if native_flushed > 0:
                self.stats.chapters_pending = await self._history_pool_size(page)
                return native_flushed

        if not force_flush and pool < ccl:
            return 0

        return await self._flush_history_pool_confirmed(
            page, before_resume=before_resume
        )

    async def _flush_history_pool(self, page: Page) -> Tuple[int, Optional[CardDropInfo]]:
        """POST /addHistory — сайт засчитывает главы и может выдать карту."""
        try:
            data = await page.evaluate(
                """async () => {
                  let items = [];
                  try {
                    items = JSON.parse(localStorage.getItem('history_pool') || '[]') || [];
                  } catch (e) { items = []; }
                  if (!items.length) return {posted: false, empty: true};
                  const body = new URLSearchParams();
                  items.forEach((it, i) => {
                    body.append('items[' + i + '][manga_id]', String(it.manga_id));
                    body.append('items[' + i + '][chapter_id]', String(it.chapter_id));
                  });
                  const csrfEl = document.querySelector('meta[name="csrf-token"]');
                  const csrf = csrfEl ? csrfEl.getAttribute('content') : '';
                  const resp = await fetch('/addHistory?r=702', {
                    method: 'POST',
                    headers: {
                      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                      'X-CSRF-TOKEN': csrf || '',
                      'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: body.toString(),
                    credentials: 'same-origin'
                  });
                  let json = null;
                  try { json = await resp.json(); } catch (e) {
                    try { json = {raw: await resp.text()}; } catch (e2) { json = null; }
                  }
                  // пул чистим ТОЛЬКО при успехе — иначе 429 съест главы
                  if (resp.status >= 200 && resp.status < 300) {
                    localStorage.setItem('history_pool', JSON.stringify([]));
                  }
                  return {
                    posted: true,
                    status: resp.status,
                    count: items.length,
                    data: json,
                    cleared: resp.status >= 200 && resp.status < 300
                  };
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("flush history_pool: %s", exc)
            return 0, None

        if not data or not data.get("posted"):
            return 0, None

        status = int(data.get("status") or 0)
        count = int(data.get("count") or 0)
        cleared = bool(data.get("cleared"))
        logger.info(
            "MangaBuff addHistory status=%s chapters=%s cleared=%s data=%s",
            status,
            count,
            cleared,
            (
                list((data.get("data") or {}).keys())[:8]
                if isinstance(data.get("data"), dict)
                else data.get("data")
            ),
        )
        if not cleared or status == 429 or status >= 400:
            return 0, None

        self._last_history_post_at = time_mod.time()
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        info = CardDropInfo(source="addHistory")
        if payload.get("image") or payload.get("name") or payload.get("rank"):
            info.cards = 1
            name = str(
                payload.get("name") or payload.get("card_name") or ""
            ).strip()
            if name:
                info.names.append(name)
            rank = str(payload.get("rank") or "").strip().upper()
            if rank:
                info.ranks.append(rank)
            for key in ("insert_user_id", "user_card_id", "card_user_id", "id"):
                raw_id = payload.get(key)
                if raw_id and str(raw_id).isdigit():
                    info.user_card_ids.append(int(raw_id))
                    break
            info.raw = str(payload)[:200]
            self.stats.cards_claimed += 1
            line = info.cards_line(1)
            self.stats.last_card_drop = f"+1 · {line}" if line else "+1"
            self.stats.touch("карта из addHistory", page.url)
            self._persist_stats()
            logger.info(
                "MangaBuff card from addHistory: %s rank=%s",
                name or payload.get("image"),
                rank or "?",
            )

        await self._tempo_pause(0.25, 0.6)
        try:
            toast = await self._harvest_reader_toast(page)
            if toast.cards and not info.cards:
                return count, None
        except Exception:  # noqa: BLE001
            pass
        try:
            drop = await self._harvest_card_drops(page, source="addHistory-ui")
            if drop.cards and not info.cards:
                return count, None
        except Exception:  # noqa: BLE001
            pass
        return count, (info if info.cards else None)

    async def _animate_scroll(self, page: Page, start: int, end: int) -> None:
        tier = self._tempo_tier()
        if tier == "turbo":
            frames, frame_sleep = 1, (0.0, 0.0)
        elif tier == "fast":
            frames, frame_sleep = 1, (0.0, 0.01)
        elif tier == "lively":
            frames, frame_sleep = random.randint(2, 3), (0.01, 0.03)
        elif tier == "normal":
            frames, frame_sleep = random.randint(3, 5), (0.02, 0.05)
        else:
            frames, frame_sleep = random.randint(6, 12), (0.04, 0.12)

        if frames <= 1:
            try:
                await page.evaluate("(y) => window.scrollTo(0, y)", int(end))
            except Exception:  # noqa: BLE001
                pass
            return

        for i in range(1, frames + 1):
            if self._stop_flag.is_set():
                break
            y = int(start + (end - start) * (i / frames))
            try:
                await page.evaluate("(y) => window.scrollTo(0, y)", y)
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(random.uniform(*frame_sleep))

    async def _click_sequential_next(self, page: Page, before: str) -> bool:
        """Клик «След. глава» только если href/URL строго следующая."""
        tier = self._tempo_tier()
        vis_timeout = 250 if tier in ("turbo", "fast") else 700
        candidates = (
            page.get_by_role("link", name=re.compile(r"След\.?\s*глава", re.I)),
            page.locator("a[href*='/manga/']").filter(
                has_text=re.compile(r"След\.?\s*глава", re.I)
            ),
        )
        for loc in candidates:
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(count, 3)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible(timeout=vis_timeout):
                        continue
                    href = (await el.get_attribute("href")) or ""
                    # заранее отсечь прыжки по href
                    if href:
                        abs_href = href
                        if href.startswith("/"):
                            abs_href = "https://mangabuff.ru" + href
                        if not self._is_sequential_next(before, abs_href):
                            continue
                    await el.click(force=True)
                    gap_lo, gap_hi = self._chapter_gap_pause()
                    await self._human_pause(gap_lo, gap_hi)
                    try:
                        await page.wait_for_load_state(
                            "domcontentloaded",
                            timeout=2000 if tier in ("turbo", "fast") else 8000,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    await self._dismiss_overlays(page)
                    now = page.url.split("?")[0]
                    if self._is_sequential_next(before, now):
                        logger.info("MangaBuff next chapter via click %s", now)
                        return True
                    logger.warning(
                        "MangaBuff reject chapter jump %s → %s (href=%s)",
                        before,
                        now,
                        href[:80],
                    )
                    await self._safe_goto(page, before)
                except Exception:  # noqa: BLE001
                    try:
                        await self._safe_goto(page, before)
                    except Exception:  # noqa: BLE001
                        pass
        return False

    async def _go_next_chapter(self, page: Page) -> bool:
        """Строго следующая глава по порядку — без перескоков."""
        before = page.url.split("?")[0]
        parsed = self._parse_chapter_url(before)
        if not parsed:
            return False
        slug, vol, ch = parsed
        ordered = [
            f"https://mangabuff.ru/manga/{slug}/{vol}/{ch + 1}",
            f"https://mangabuff.ru/manga/{slug}/{vol + 1}/1",
        ]
        tier = self._tempo_tier()

        # turbo/fast: клик быстрее полного goto, если кнопка есть
        if tier in ("turbo", "fast"):
            if await self._click_sequential_next(page, before):
                return True

        # URL следующей главы
        for nxt in ordered:
            if not await self._safe_goto(page, nxt):
                continue
            title = ""
            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                pass
            now = page.url.split("?")[0]
            if "404" in title.lower() or now == before:
                continue
            if self._is_sequential_next(before, now):
                logger.info("MangaBuff next chapter via URL %s", now)
                return True
            await self._safe_goto(page, before)

        # медленные режимы / fallback: клик
        if tier not in ("turbo", "fast"):
            if await self._click_sequential_next(page, before):
                return True
        return False

    # ------------------------------------------------------------------
    # Comments — каждые 5–15 глав, только на текущую главу
    # ------------------------------------------------------------------

    def _parse_chapter_url(self, url: str) -> Optional[Tuple[str, int, int]]:
        m = re.search(r"mangabuff\.ru/manga/([^/]+)/(\d+)/(\d+)", url)
        if not m:
            return None
        return m.group(1), int(m.group(2)), int(m.group(3))

    @staticmethod
    def _chapter_comment_key(slug: str, vol: int, ch: int) -> str:
        return f"{slug}/{int(vol)}/{int(ch)}"

    def _mark_chapter_commented(self, slug: str, vol: int, ch: int) -> None:
        key = self._chapter_comment_key(slug, vol, ch)
        if key in self._commented_chapters:
            return
        self._commented_chapters.add(key)
        lst = list(self.stats.commented_chapters or [])
        lst.append(key)
        # не раздувать файл бесконечно
        self.stats.commented_chapters = lst[-5000:]

    async def _open_first_chapter_with_retry(
        self, title_href: str, slug: str, attempts: int = 4
    ) -> Optional[str]:
        """Не скипаем тайтл с первого фейла — несколько попыток открыть 1-ю главу."""
        assert self._page is not None
        page = self._page
        for attempt in range(1, attempts + 1):
            start_url = await self._open_first_chapter(title_href)
            if start_url and re.search(r"/manga/[^/]+/\d+/\d+", start_url):
                return start_url
            logger.warning(
                "MangaBuff cannot open %s (attempt %s/%s)",
                slug or title_href,
                attempt,
                attempts,
            )
            await self._tempo_pause(1.0, 2.2)
            if slug:
                await self._safe_goto(page, f"https://mangabuff.ru/manga/{slug}")
        return None

    async def _maybe_comment(self, page: Page, confirmed: int = 0) -> bool:
        """Каждые 5–15 засчитанных глав — коммент на текущую главу."""
        if confirmed <= 0:
            return False
        self._chapters_since_comment += int(confirmed)
        if self._chapters_since_comment < self._next_comment_after:
            return False

        current_url = page.url.split("?")[0]
        parsed = self._parse_chapter_url(current_url)
        if not parsed:
            logger.info("MangaBuff comment skip: not on chapter url %s", current_url)
            self._next_comment_after = self._chapters_since_comment + random.randint(1, 3)
            return False

        slug, vol, cur_ch = parsed
        key = self._chapter_comment_key(slug, vol, cur_ch)
        if key in self._commented_chapters:
            self._next_comment_after = self._chapters_since_comment + random.randint(1, 3)
            return False

        logger.info(
            "MangaBuff comment due after %s chapters → current %s",
            self._chapters_since_comment,
            current_url,
        )
        try:
            ok = await self._post_human_comment(page, slug=slug)
            # всегда остаёмся / возвращаемся на ту же главу
            if page.url.split("?")[0] != current_url:
                await self._safe_goto(page, current_url)
                await self._dismiss_overlays(page)
            if ok:
                self._mark_chapter_commented(slug, vol, cur_ch)
                self._chapters_since_comment = 0
                self._next_comment_after = random.randint(
                    COMMENT_EVERY_MIN, COMMENT_EVERY_MAX
                )
                self._persist_stats()
                logger.info("MangaBuff comment locked to chapter %s", key)
            else:
                self._next_comment_after = self._chapters_since_comment + random.randint(
                    1, 3
                )
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff comment flow failed: %s", exc)
            self._next_comment_after = self._chapters_since_comment + random.randint(1, 3)
            try:
                if page.url.split("?")[0] != current_url:
                    await self._safe_goto(page, current_url)
            except Exception:  # noqa: BLE001
                pass
            return False

    async def _open_comments_panel(self, page: Page) -> bool:
        await self._dismiss_overlays(page)
        # уже открыто?
        try:
            if await page.locator(".comments__send-form textarea").count():
                if await page.locator(".comments__send-form textarea").first.is_visible(
                    timeout=500
                ):
                    return True
        except Exception:  # noqa: BLE001
            pass

        for sel in (
            "button.reader__show-comments-btn",
            "button.reader-menu__item--comment",
            "button:has-text('Комментарии')",
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=800):
                    await loc.click(force=True)
                    await self._tempo_pause(1.2, 2.2)
                    break
            except Exception:  # noqa: BLE001
                continue

        # вкладка «Популярные» — там живее примеры
        try:
            pop = page.locator("button.comments__change-sort", has_text="Популярные")
            if await pop.count() and await pop.first.is_visible(timeout=600):
                await pop.first.click(force=True)
                await self._tempo_pause(0.8, 1.5)
        except Exception:  # noqa: BLE001
            pass

        try:
            return await page.locator(".comments__send-form textarea").first.is_visible(
                timeout=2000
            )
        except Exception:  # noqa: BLE001
            return False

    async def _collect_comment_samples(self, page: Page) -> List[str]:
        samples: List[str] = []
        try:
            samples = await page.locator(".comments__body").all_inner_texts()
        except Exception:  # noqa: BLE001
            samples = []
        cleaned: List[str] = []
        for s in samples:
            s = (s or "").strip()
            s = re.sub(r"https?://\S+", "", s)
            # убрать эмодзи / символы
            s = re.sub(
                r"[\U0001F300-\U0010ffff\u2600-\u27BF]",
                "",
                s,
            )
            s = re.sub(r"\s+", " ", s).strip()
            if 5 <= len(s) <= 140 and self._is_safe_comment(s.lower().rstrip(".,!?")):
                cleaned.append(s)
        return cleaned[:40]

    async def _post_human_comment(self, page: Page, slug: str = "") -> bool:
        if not await self._open_comments_panel(page):
            logger.info("MangaBuff comment: panel not opened")
            return False

        samples = await self._collect_comment_samples(page)
        # если мало — переключить на «Новые»
        if len(samples) < 3:
            try:
                neu = page.locator("button.comments__change-sort", has_text="Новые")
                if await neu.count():
                    await neu.first.click(force=True)
                    await self._tempo_pause(0.8, 1.4)
                    samples = await self._collect_comment_samples(page)
            except Exception:  # noqa: BLE001
                pass

        if not slug:
            parsed = self._parse_chapter_url(page.url)
            slug = parsed[0] if parsed else ""
        text = self._craft_comment(samples, slug=slug)
        if not text or not self._is_safe_comment(text):
            logger.info("MangaBuff comment: craft failed (samples=%s)", len(samples))
            return False

        area = page.locator(".comments__send-form textarea").first
        if await area.count() == 0:
            area = page.locator("textarea").first
        if await area.count() == 0:
            logger.info("MangaBuff comment: no textarea")
            return False

        try:
            await area.click(force=True)
            await self._tempo_pause(0.3, 0.8)
            await area.fill("")
            # «печатает» с паузами
            tier = self._tempo_tier()
            if tier == "turbo":
                type_delay = (8, 25)
                think_p = 0.01
            elif tier == "fast":
                type_delay = (15, 40)
                think_p = 0.02
            else:
                type_delay = (35, 120)
                think_p = 0.04
            for ch in text:
                await area.type(ch, delay=random.randint(*type_delay))
                if random.random() < think_p:
                    await asyncio.sleep(random.uniform(0.05, 0.25))
            await self._tempo_pause(0.7, 1.8)

            send = page.locator("button.comments__send-btn").first
            if await send.count() == 0:
                send = page.get_by_role("button", name=re.compile(r"отправ", re.I)).first
            await send.click(force=True, timeout=4000)
            await self._tempo_pause(1.5, 3.0)

            self.stats.comments_posted += 1
            self.stats.touch(f"коммент: {text[:50]}", page.url)
            voice = self._voice_for_slug(slug).get("key", "?")
            logger.info(
                "MangaBuff comment posted on %s voice=%s (next in %s–%s): %s",
                page.url,
                voice,
                COMMENT_EVERY_MIN,
                COMMENT_EVERY_MAX,
                text,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff comment send failed: %s", exc)
            return False

    def _normalize_comment_sample(self, raw: str) -> str:
        s = (raw or "").strip().lower()
        s = re.sub(r"https?://\S+", "", s)
        s = re.sub(r"[\U0001F300-\U0010ffff\u2600-\u27BF]", "", s)
        s = re.sub(r"[^\w\sа-яА-ЯёЁ\-']", " ", s, flags=re.UNICODE)
        s = re.sub(r"\s+", " ", s).strip(" -'")
        return s

    def _synonymize_token(self, token: str) -> str:
        key = token.lower().strip(".,!?;:-")
        if not key:
            return token
        variants = _COMMENT_SYNONYMS.get(key)
        if variants:
            return random.choice(variants)
        return key

    def _light_synonymize_phrase(self, phrase: str, max_swaps: int = 1) -> str:
        """Заменить 0–1 слово внутри уже цельной фразы — без ломки смысла."""
        words = phrase.split()
        idxs = []
        for i, w in enumerate(words):
            variants = _COMMENT_SYNONYMS.get(w)
            if not variants:
                continue
            # только однословные варианты
            if any((" " in v) for v in variants):
                continue
            idxs.append(i)
        if not idxs:
            return phrase
        # предпочтительно менять ближе к концу — меньше риск сломать конструкцию
        idxs.sort(reverse=True)
        i = idxs[0]
        cand = self._synonymize_token(words[i])
        if " " in cand or not cand:
            return phrase
        words[i] = cand
        return " ".join(words)

    def _comment_word_set(self, text: str) -> set:
        return {w for w in re.findall(r"[а-яёa-z0-9\-']+", text.lower()) if len(w) > 1}

    _DANGLING_ENDS = frozenset(
        {
            "и",
            "а",
            "но",
            "же",
            "ли",
            "бы",
            "то",
            "не",
            "ни",
            "в",
            "на",
            "с",
            "у",
            "к",
            "о",
            "об",
            "по",
            "за",
            "от",
            "из",
            "до",
            "для",
            "про",
            "при",
            "без",
            "что",
            "как",
            "это",
            "ещё",
            "еще",
            "уже",
            "кста",
            "типа",
            "просто",
            "мне",
            "вам",
            "ему",
            "её",
            "ее",
            "его",
            "хотя",
            "плюс",
            "если",
            "когда",
            "чтобы",
            "или",
        }
    )

    _SENSE_STEMS = (
        "спасибо",
        "благодар",
        "нрав",
        "зайд",
        "заход",
        "кайф",
        "топ",
        "жду",
        "глав",
        "сюжет",
        "истор",
        "рис",
        "арт",
        "атмосфер",
        "интерес",
        "крут",
        "огонь",
        "пушк",
        "держ",
        "тян",
        "норм",
        "мил",
        "груст",
        "смеш",
        "респект",
        "успех",
        "работ",
        "труд",
        "вау",
        "кайф",
        "зашло",
        "приятн",
        "хорош",
        "достойн",
        "аккурат",
        "уверен",
        "поддерж",
        "фанат",
        "читал",
        "читаю",
        "эмоц",
        "настро",
        "вайб",
        "имба",
        "база",
        "огн",
        "ценил",
        "ценю",
        "рад",
        "супер",
        "лучш",
    )

    def _is_coherent_comment(self, text: str) -> bool:
        """Фраза должна быть цельной мыслью, а не набором обрывков."""
        words = [w for w in (text or "").lower().split() if w]
        if not (COMMENT_WORDS_MIN <= len(words) <= COMMENT_WORDS_MAX):
            return False
        if words[-1] in self._DANGLING_ENDS:
            return False
        if words[0] in {"и", "а", "но", "или", "хотя", "плюс"}:
            return False
        # слишком много коротких служебных подряд — признак каши
        short = self._DANGLING_ENDS | {"ну", "вот", "там", "тут", "же"}
        run = 0
        for w in words:
            if w in short:
                run += 1
                if run >= 3:
                    return False
            else:
                run = 0
        joined = " ".join(words)
        if not any(stem in joined for stem in self._SENSE_STEMS):
            return False
        # обрывки вроде «кста не у», «ещё не могу» без ясной мысли
        if re.search(r"\b(не у|не в|ещё не могу|еще не могу)\b", joined):
            return False
        if " спойлер " in f" {joined} ":
            return False
        return True

    def _detect_comment_theme(self, samples: Sequence[str]) -> str:
        """Тема по чужим комментам: art/plot/wait/emotion/general."""
        blob = " ".join(samples).lower()
        scores = {
            "art": len(re.findall(r"рис|арт|визуал|кадр|стиль|drawn", blob)),
            "plot": len(re.findall(r"сюжет|истори|лини|поворот|интриг|событ", blob)),
            "wait": len(re.findall(r"жду|дальше|продолжен|следующ|скорей|когда выйд", blob)),
            "emotion": len(
                re.findall(r"эмоц|атмосфер|вайб|настрое|слёз|слез|мураш|душ", blob)
            ),
        }
        best = max(scores, key=scores.get)
        if scores[best] <= 0:
            return "general"
        return best

    def _voice_for_slug(self, slug: str) -> Dict[str, Any]:
        """Одинаковая интонация внутри тайтла, разная между тайтлами."""
        key = (slug or "default").strip().lower() or "default"
        if key in self._title_voices:
            return self._title_voices[key]
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()
        idx = int(digest[:8], 16) % len(_COMMENT_VOICES)
        voice = _COMMENT_VOICES[idx]
        self._title_voices[key] = voice
        logger.debug("MangaBuff comment voice for %s → %s", key, voice.get("key"))
        return voice

    def _pick_varied(self, options: Sequence[str], avoid: str = "") -> str:
        pool = [x for x in options if x != avoid]
        if not pool:
            pool = list(options)
        return random.choice(pool)

    @staticmethod
    def thanks_variants_count() -> int:
        """Число уникальных цельных благодарностей по всем голосам."""
        uniq = set()
        for voice in _COMMENT_VOICES:
            for phrase in voice.get("thanks") or ():
                text = re.sub(r"\s+", " ", str(phrase)).strip().lower()
                n = len(text.split())
                if COMMENT_WORDS_MIN <= n <= COMMENT_WORDS_MAX:
                    uniq.add(text)
        return len(uniq)

    def _finalize_comment_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", (text or "")).strip().lower().rstrip(".,!?;:…")
        words = []
        for w in text.split():
            if words and words[-1] == w:
                continue
            words.append(w)
        if len(words) < COMMENT_WORDS_MIN:
            return ""
        words = words[:COMMENT_WORDS_MAX]
        text = " ".join(words)
        if text:
            text = text[0].lower() + text[1:]
        if not self._is_safe_comment(text):
            return ""
        if not self._is_coherent_comment(text):
            return ""
        return text

    def _craft_thanks_comment(self, slug: str = "") -> str:
        """Цельная благодарность в интонации тайтла (без склейки обрывков)."""
        voice = self._voice_for_slug(slug)
        last = self._last_comment_bits.get(slug or "default", "")
        pool = tuple(voice.get("thanks") or ())
        if not pool:
            return "спасибо за главу жду дальше"
        for _ in range(40):
            phrase = self._pick_varied(pool, avoid=last)
            if random.random() < 0.30:
                phrase = self._light_synonymize_phrase(phrase, max_swaps=1)
            text = self._finalize_comment_text(phrase)
            if text and text != last:
                self._last_comment_bits[slug or "default"] = text
                return text
        fallback = self._finalize_comment_text(random.choice(pool))
        return fallback or "спасибо за главу жду дальше"

    def _craft_comment(self, samples: Sequence[str], slug: str = "") -> str:
        """
        65% — цельные благодарности в голосе тайтла,
        35% — цельная реакция в теме чужих комментов.
        """
        self._current_comment_slug = slug or ""
        if random.random() < THANKS_COMMENT_CHANCE:
            return self._craft_thanks_comment(slug=slug)
        return self._craft_similar_comment(samples, slug=slug)

    def _craft_similar_comment(self, samples: Sequence[str], slug: str = "") -> str:
        """Похожий по смыслу: одна цельная фраза по теме чужих комментов."""
        cleaned: List[str] = []
        for s in samples:
            norm = self._normalize_comment_sample(s)
            if 8 <= len(norm) <= 160 and self._is_safe_comment(norm):
                cleaned.append(norm)

        voice = self._voice_for_slug(slug)
        last = self._last_comment_bits.get(slug or "default", "")
        reactions = voice.get("reactions") or {}
        theme = self._detect_comment_theme(cleaned) if cleaned else "general"
        pool = list(reactions.get(theme) or ())
        if not pool:
            pool = list(reactions.get("general") or ())
        # иногда берём соседнюю тему, но всё равно цельную фразу
        if cleaned and random.random() < 0.2:
            alt = random.choice(["general", "plot", "wait", "emotion", "art"])
            pool = list(reactions.get(alt) or pool) or pool
        if not pool:
            return self._craft_thanks_comment(slug=slug)

        for _ in range(40):
            phrase = self._pick_varied(pool, avoid=last)
            if random.random() < 0.35:
                phrase = self._light_synonymize_phrase(phrase, max_swaps=1)
            text = self._finalize_comment_text(phrase)
            if text and text != last:
                self._last_comment_bits[slug or "default"] = text
                return text
        text = self._finalize_comment_text(random.choice(pool))
        if text:
            self._last_comment_bits[slug or "default"] = text
            return text
        return self._craft_thanks_comment(slug=slug)

    def _is_safe_comment(self, text: str) -> bool:
        if not text or len(text) < 4:
            return False
        if re.search(r"[A-ZА-Я]{5,}", text):
            return False
        if re.search(r"[!?]{2,}|@{1,}|#\w+|https?://", text):
            return False
        if any(ord(c) >= 0x1F300 for c in text):
            return False
        banned = (
            "убей",
            "суицид",
            "ненавиж",
            "дурак",
            "идиот",
            "репорт",
            "жалоб",
            "спам",
            "реклам",
            "подпиш",
            "ссылк",
            "http",
            "t.me",
            "телег",
            "промокод",
        )
        low = text.lower()
        return not any(b in low for b in banned)

    # ------------------------------------------------------------------
    # Main farm loop
    # ------------------------------------------------------------------

    async def _go_next_chapter_with_retry(self, page: Page, attempts: int = 3) -> bool:
        """Несколько попыток строго следующей главы, прежде чем сдаться."""
        for attempt in range(1, attempts + 1):
            if await self._go_next_chapter(page):
                return True
            logger.warning(
                "MangaBuff next chapter retry %s/%s from %s",
                attempt,
                attempts,
                page.url.split("?")[0],
            )
            await self._tempo_pause(0.6, 1.4)
        return False

    async def _read_title_almost_end(
        self,
        page: Page,
        title: Dict[str, Any],
        on_progress=None,
    ) -> int:
        """Прочитать один тайтл по порядку до ~90%. Вызывать под self._lock."""
        slug = str(title.get("slug") or "")
        # новый тайтл — не путаем с уже прочитанными URL прошлого
        self._read_urls.clear()
        self._skip_title.clear()

        logger.info("MangaBuff open title %s", slug)
        total_chapters = await self._estimate_title_chapters(slug)
        max_per_title = self._chapters_target_for_title(total_chapters)
        pct = (
            (100.0 * max_per_title / total_chapters) if total_chapters > 0 else 90.0
        )
        logger.info(
            "MangaBuff will read %s/%s chapters of %s (~%.0f%%)",
            max_per_title,
            total_chapters or "?",
            slug,
            pct,
        )

        start_url = await self._open_first_chapter_with_retry(
            title.get("href") or "", slug
        )
        if not start_url:
            logger.error("MangaBuff defer title %s: cannot open first chapter", slug)
            return 0

        self.stats.titles_visited += 1
        self.stats.touch(f"тайтл: {str(title.get('title') or '')[:40]}", start_url)
        logger.info(
            "MangaBuff reading %s sequentially from %s (target=%s)",
            slug,
            start_url,
            max_per_title,
        )
        self._persist_stats()

        chapters_this_title = 0
        last_chapter_url = start_url.split("?")[0]

        while (
            not self._stop_flag.is_set()
            and not self._skip_title.is_set()
            and chapters_this_title < max_per_title
        ):
            # ночной стоп — выходим, farm_loop подождёт снаружи
            if self._in_night_break_window():
                logger.info("MangaBuff pause title for night break")
                break

            url = page.url.split("?")[0]
            if not re.search(r"/manga/[^/]+/\d+/\d+", url):
                recover = last_chapter_url or (
                    f"https://mangabuff.ru/manga/{slug}/1/"
                    f"{max(1, chapters_this_title + 1)}"
                )
                logger.warning(
                    "MangaBuff not on chapter page: %s — recover %s",
                    url,
                    recover,
                )
                if not await self._safe_goto(page, recover):
                    break
                url = page.url.split("?")[0]
                if not re.search(r"/manga/[^/]+/\d+/\d+", url):
                    break

            if url in self._read_urls:
                if not await self._go_next_chapter_with_retry(page):
                    break
                continue

            parsed = self._parse_chapter_url(url)
            if parsed:
                slug_p, _vol, ch_num = parsed
                resume = await self._reader_resume_chapter(page)
                if resume > 0 and ch_num < resume:
                    logger.info(
                        "MangaBuff skip already-read %s ch %s (next unread=%s)",
                        slug_p,
                        ch_num,
                        resume,
                    )
                    if resume - ch_num > 2:
                        jump = f"https://mangabuff.ru/manga/{slug_p}/1/{resume}"
                        if await self._safe_goto(page, jump):
                            await self._dismiss_overlays(page)
                            continue
                    if not await self._go_next_chapter_with_retry(page):
                        break
                    continue

            self._read_urls.add(url)
            last_chapter_url = url
            logger.info("MangaBuff scroll chapter %s", url)

            overlay_t = 2.5 if self._tempo_tier() in ("turbo", "fast") else 8.0
            reward_t = 3.0 if self._tempo_tier() in ("turbo", "fast") else 12.0
            try:
                await asyncio.wait_for(self._dismiss_overlays(page), timeout=overlay_t)
            except Exception:  # noqa: BLE001
                pass
            try:
                c, cards, scrolls, chests, packs, _ = await asyncio.wait_for(
                    self._click_reward_buttons(page), timeout=reward_t
                )
                self.stats.rewards_claimed += c
                self.stats.cards_claimed += cards
                self.stats.scrolls_claimed += scrolls
                self.stats.chests_opened += chests
                self.stats.packs_opened += packs
            except Exception:  # noqa: BLE001
                pass

            steps = await self._smooth_read_chapter(page)
            final_url = page.url.split("?")[0]
            if not re.search(r"/manga/[^/]+/\d+/\d+", final_url):
                logger.warning(
                    "MangaBuff chapter aborted (left reader): %s", final_url
                )
                if not await self._safe_goto(page, url):
                    break
                continue

            # критично: засчитать главу на сайте (иначе турбо уходит до addHistory)
            if not await self._wait_for_chapter_context(page):
                logger.warning(
                    "MangaBuff no reader context, reload: %s", final_url
                )
                if not await self._safe_goto(page, final_url):
                    break
                if not await self._wait_for_chapter_context(page, timeout_sec=6.0):
                    logger.warning(
                        "MangaBuff skip chapter (no current_chapter): %s",
                        final_url,
                    )
                    if not await self._go_next_chapter_with_retry(page):
                        break
                    continue

            confirmed = 0
            try:
                confirmed = await self._register_chapter_read(page, force_flush=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("register chapter read: %s", exc)

            pending = await self._history_pool_size(page)
            self.stats.chapters_pending = pending

            # пул полон, но сайт не принял — не уходим дальше, пока не зачтёт
            if confirmed <= 0 and pending >= 2:
                logger.warning(
                    "MangaBuff addHistory backlog pool=%s at %s — retry flush",
                    pending,
                    final_url,
                )
                backlog_resume = await self._reader_resume_chapter(page)
                confirmed = await self._flush_history_pool_confirmed(
                    page, before_resume=backlog_resume
                )
                pending = await self._history_pool_size(page)
                self.stats.chapters_pending = pending

            # тосты/модалки после flush (без ухода с главы)
            try:
                await self._harvest_reader_toast(page)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._harvest_card_drops(page, source=final_url)
            except Exception:  # noqa: BLE001
                pass

            if confirmed <= 0:
                if pending >= 2:
                    logger.error(
                        "MangaBuff addHistory stuck pool=%s at %s — retry same chapter",
                        pending,
                        final_url,
                    )
                    self.stats.errors += 1
                    await self._tempo_pause(8.0, 15.0)
                    continue
                if pending > 0:
                    self.stats.touch(f"в очереди ({pending})", final_url)
                    self._persist_stats()
                else:
                    logger.warning(
                        "MangaBuff chapter not queued: %s", final_url
                    )
                    self.stats.errors += 1
                    await self._tempo_pause(2.0, 4.0)
                    if not await self._go_next_chapter_with_retry(page):
                        break
                    continue
            else:
                self.stats.chapters_read += confirmed
                chapters_this_title += confirmed
                for _ in range(confirmed):
                    self.note_chapter_finished()
                last_chapter_url = final_url
                self.stats.touch(f"зачтено сайтом +{confirmed}", final_url)
                self._persist_stats()
                logger.info(
                    "MangaBuff chapter done steps=%s session=%s total=%s "
                    "url=%s site=+%s pending=%s",
                    steps,
                    self.session_chapters,
                    self.stats.chapters_read,
                    final_url,
                    confirmed,
                    pending,
                )

                await self._maybe_comment(page, confirmed)
            if not re.search(r"/manga/[^/]+/\d+/\d+", page.url.split("?")[0]):
                await self._safe_goto(page, final_url)

            if on_progress is not None:
                try:
                    await on_progress(self.stats)
                except Exception:  # noqa: BLE001
                    pass

            gap_lo, gap_hi = self._chapter_gap_pause()
            await self._human_pause(gap_lo, gap_hi)
            if chapters_this_title >= max_per_title:
                break
            if not await self._go_next_chapter_with_retry(page):
                logger.info(
                    "MangaBuff title %s stopped at %s/%s (no sequential next)",
                    slug,
                    chapters_this_title,
                    max_per_title,
                )
                break

        # добить остаток history_pool перед уходом с тайтла
        try:
            tail = await self._register_chapter_read(page, force_flush=True)
            if tail > 0:
                self.stats.chapters_read += tail
                chapters_this_title += tail
                for _ in range(tail):
                    self.note_chapter_finished()
                self.stats.chapters_pending = await self._history_pool_size(page)
                self.stats.touch(f"зачтено сайтом +{tail} (хвост)", page.url)
                self._persist_stats()
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "MangaBuff title %s progress: read %s (target %s, total≈%s)",
            slug,
            chapters_this_title,
            max_per_title,
            total_chapters or "?",
        )
        return chapters_this_title

    def _in_night_break_window(self) -> bool:
        return self._night_break_remaining() is not None

    async def farm_loop(self, on_progress=None) -> MangaBuffStats:
        """
        Основной цикл: награды → макеты → популярные тайтлы → чтение глав.
        Ночью 01:00–05:00 МСК — пауза 4 часа.
        """
        self._stop_flag.clear()
        self.stats.running = True
        self.stats.started_at = datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S")
        self.mark_farm_session_start()
        self.stats.touch("фарм запущен")

        if not self.is_started:
            await self.start(headless=True)
        assert self._page is not None
        page = self._page

        if not await self._acquire_browser_lock("farm login", 90.0):
            self.stats.running = False
            self.stats.touch("браузер занят")
            return self.stats
        try:
            ok = await self._ensure_login_unlocked()
        finally:
            self._lock.release()
        if not ok:
            self.stats.running = False
            self.stats.touch("нет авторизации")
            return self.stats

        cycle = 0
        while not self._stop_flag.is_set():
            await self._await_night_break_if_needed()
            if self._stop_flag.is_set():
                break

            cycle += 1
            try:
                # Лок на навигацию: параллельные задачи не уводят вкладку.
                if not await self._acquire_browser_lock(f"cycle {cycle} catalog", 90.0):
                    self.stats.touch("ожидание браузера")
                    await self._tempo_pause(3.0, 6.0)
                    continue
                try:
                    logger.info("MangaBuff farm cycle %s: fetch popular", cycle)
                    titles = await self.fetch_popular_titles(limit=24)
                finally:
                    self._lock.release()

                if not titles:
                    self.stats.touch("каталог пуст — пауза")
                    await self._tempo_pause(20, 40)
                    continue
                logger.info(
                    "MangaBuff titles: %s",
                    ", ".join(t["slug"] for t in titles[:8]),
                )

                deferred: List[Dict[str, Any]] = []
                for title in titles:
                    if self._stop_flag.is_set():
                        break
                    await self._await_night_break_if_needed()
                    if self._stop_flag.is_set():
                        break

                    slug = str(title.get("slug") or "")
                    logger.info("MangaBuff start title %s", slug or title.get("href"))
                    if not await self._acquire_browser_lock(f"read {slug}", 120.0):
                        deferred.append(title)
                        logger.warning(
                            "MangaBuff title %s deferred — browser lock busy",
                            slug,
                        )
                        await self._tempo_pause(2.0, 4.0)
                        continue
                    try:
                        chapters_this_title = await self._read_title_almost_end(
                            page,
                            title,
                            on_progress=on_progress,
                        )
                        if chapters_this_title > 0:
                            try:
                                await self._farm_events_unlocked(quick=True)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("inter-title events: %s", exc)
                    finally:
                        self._lock.release()
                    if chapters_this_title <= 0:
                        # не скипаем навсегда — вернёмся в конце цикла
                        deferred.append(title)
                        logger.warning(
                            "MangaBuff title %s deferred (0 chapters) — retry later",
                            slug,
                        )
                        await self._tempo_pause(2.0, 4.0)
                        continue
                    logger.info(
                        "MangaBuff finished title %s: read %s chapters",
                        slug,
                        chapters_this_title,
                    )
                    await self._tempo_pause(2.0, 6.0)

                # повтор отложенных тайтлов в том же цикле
                for title in deferred:
                    if self._stop_flag.is_set():
                        break
                    await self._await_night_break_if_needed()
                    if self._stop_flag.is_set():
                        break
                    slug = str(title.get("slug") or "")
                    logger.info("MangaBuff retry deferred title %s", slug)
                    if not await self._acquire_browser_lock(f"retry {slug}", 120.0):
                        logger.warning(
                            "MangaBuff retry %s skipped — browser lock busy",
                            slug,
                        )
                        continue
                    try:
                        chapters_this_title = await self._read_title_almost_end(
                            page,
                            title,
                            on_progress=on_progress,
                        )
                    finally:
                        self._lock.release()
                    if chapters_this_title <= 0:
                        logger.error(
                            "MangaBuff title %s still unreadable — next cycle",
                            slug,
                        )
                    else:
                        logger.info(
                            "MangaBuff finished deferred title %s: read %s chapters",
                            slug,
                            chapters_this_title,
                        )
                    await self._tempo_pause(3.0, 7.0)

            except Exception as exc:  # noqa: BLE001
                logger.exception("MangaBuff farm_loop")
                self.stats.errors += 1
                self.stats.touch(f"ошибка: {exc}")
                await self._tempo_pause(5.0, 12.0)

        self.stats.running = False
        self.stats.touch("фарм остановлен", page.url if self._page else "")
        return self.stats

    # обратная совместимость с bot.py
    async def read_loop(
        self,
        start_url: Optional[str] = None,
        max_chapters: int = 0,
        on_progress=None,
    ) -> MangaBuffStats:
        """Если указан start_url — читаем с него; иначе полный фарм каталога."""
        if start_url and "mangabuff.ru/manga/" in start_url and re.search(
            r"/\d+/\d+", start_url
        ):
            return await self._read_from_url(start_url, max_chapters, on_progress)
        if start_url and re.search(r"mangabuff\.ru/manga/[^/]+$", start_url):
            # страница тайтла
            self.start_url = start_url
        return await self.farm_loop(on_progress=on_progress)

    async def _read_from_url(
        self,
        start_url: str,
        max_chapters: int,
        on_progress,
    ) -> MangaBuffStats:
        self._stop_flag.clear()
        self.stats.running = True
        if not self.is_started:
            await self.start(headless=True)
        assert self._page is not None
        page = self._page
        await self.ensure_login()
        await self._safe_goto(page, start_url)
        done = 0
        while not self._stop_flag.is_set():
            if max_chapters > 0 and done >= max_chapters:
                break
            await self._await_night_break_if_needed()
            await self._dismiss_overlays(page)
            await self._smooth_read_chapter(page)
            if not await self._wait_for_chapter_context(page):
                if not await self._go_next_chapter(page):
                    break
                continue
            confirmed = await self._register_chapter_read(page, force_flush=False)
            pending = await self._history_pool_size(page)
            self.stats.chapters_pending = pending
            if confirmed <= 0 and pending >= 2:
                backlog_resume = await self._reader_resume_chapter(page)
                confirmed = await self._flush_history_pool_confirmed(
                    page, before_resume=backlog_resume
                )
            if confirmed <= 0:
                if pending <= 0:
                    if not await self._go_next_chapter(page):
                        break
                continue
            self.stats.chapters_read += confirmed
            for _ in range(confirmed):
                self.note_chapter_finished()
            done += confirmed
            await self._maybe_comment(page, confirmed)
            if on_progress:
                try:
                    await on_progress(self.stats)
                except Exception:  # noqa: BLE001
                    pass
            if not await self._go_next_chapter(page):
                break
        self.stats.running = False
        return self.stats


async def main_setup() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    from config import load_config
    from settings_store import update_settings

    cfg = load_config()
    svc = MangaBuffService(cfg)
    await svc.run_setup()
    update_settings(mangabuff_setup_done=True)
    print("mangabuff_setup_done=True")


async def main_login_headless() -> None:
    """CLI: автологин без GUI (для сервера)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    from config import load_config
    from settings_store import update_settings

    cfg = load_config()
    svc = MangaBuffService(cfg)
    await svc.start(headless=True)
    ok = await svc.ensure_login()
    print("LOGIN", "OK" if ok else "FAIL")
    if ok:
        update_settings(mangabuff_setup_done=True)
    await svc.stop()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--login":
        asyncio.run(main_login_headless())
    else:
        asyncio.run(main_setup())
