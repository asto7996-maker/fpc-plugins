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

# Ночной перерыв: 01:00–05:00 МСК (ровно 4 часа)
NIGHT_BREAK_START = time(1, 0)
NIGHT_BREAK_END = time(5, 0)

# Комментарии каждые 5–15 глав на случайной главе текущего тайтла
COMMENT_EVERY_MIN = 5
COMMENT_EVERY_MAX = 15
# Читать тайтл почти до конца: оставить хвост 1–8 глав (или ~1–4%)
TITLE_LEAVE_MIN = 1
TITLE_LEAVE_MAX = 8
TITLE_LEAVE_PCT = (0.01, 0.04)
# Запасной потолок, если число глав неизвестно (идём пока есть «след. глава»)
TITLE_READ_HARD_CAP = 5000
COMMENT_WORDS_MIN = 5
COMMENT_WORDS_MAX = 20
# 65% — благодарности, 35% — похожие на чужие
THANKS_COMMENT_CHANCE = 0.65

# Интонации: внутри одного тайтла голос стабильный, между тайтлами — разный.
# У каждой — свои начала/концы, чтобы не звучало однотипно.
_COMMENT_VOICES: Tuple[Dict[str, Any], ...] = (
    {
        "key": "soft",
        "starts": (
            "",
            "ну",
            "вот",
            "эх",
            "ладно",
            "кстати",
            "если честно",
            "мне кажется",
            "тихий кайф но",
            "просто хочу сказать",
        ),
        "thanks": (
            "спасибо",
            "спасибо большое",
            "ну спасибо",
            "спасибочки",
            "благодарю",
            "спасибо вам",
            "искренне спасибо",
        ),
        "subjects": (
            "за главу",
            "за эту главу",
            "за новую главу",
            "за тайтл",
            "за атмосферу",
            "за историю",
            "автору за труды",
            "за спокойный вайб",
            "за то что выкладываете",
        ),
        "ends": (
            "",
            "приятно читать",
            "очень мягко зашло",
            "буду ждать дальше",
            "как-то тепло стало",
            "читаю с удовольствием",
            "надеюсь не пропадёте",
            "мне правда нравится",
            "тихо сижу и читаю дальше",
        ),
        "similar_openers": ("ну", "вот", "если честно", "мне кажется", "ладно"),
        "similar_tails": ("приятно", "мягко зашло", "жду дальше", "пока нравится"),
    },
    {
        "key": "hype",
        "starts": (
            "",
            "ооо",
            "блин",
            "ну наконец",
            "реально",
            "прям",
            "жесть но",
            "я в шоке",
            "короче",
            "ребята",
        ),
        "thanks": (
            "спасибо",
            "огромное спасибо",
            "от души спасибо",
            "реально спасибо",
            "прям спасибо",
            "спасибо огромное",
            "респект и спасибо",
        ),
        "subjects": (
            "за главу",
            "за эту главу",
            "за свежую главу",
            "за такой тайтл",
            "за крутой тайтл",
            "за вайб",
            "за кайф",
            "за эмоции",
            "автору за главу",
            "за продолжение",
        ),
        "ends": (
            "",
            "это пушка",
            "глава топ",
            "я в восторге",
            "уже жду следующую",
            "не отпускает вообще",
            "огонь просто",
            "так держать",
            "читаю и ору от кайфа",
            "выдаёт железно",
        ),
        "similar_openers": ("блин", "реально", "прям", "короче", "ооо"),
        "similar_tails": ("огонь", "пушка", "топ", "не отпускает", "жду следующую"),
    },
    {
        "key": "polite",
        "starts": (
            "",
            "добрый вечер",
            "хочу сказать",
            "разрешите",
            "отдельно",
            "честно говоря",
            "с уважением",
            "просто",
            "на всякий случай",
        ),
        "thanks": (
            "благодарю",
            "сердечно благодарю",
            "огромная благодарность",
            "благодарю вас",
            "благодарю автора",
            "благодарю команду",
            "спасибо большое вам",
        ),
        "subjects": (
            "за главу",
            "за новую главу",
            "за работу",
            "за труды",
            "за старания",
            "автору за труд",
            "команде за работу",
            "переводчику за работу",
            "за обновление",
            "за выпуск",
        ),
        "ends": (
            "",
            "творческих успехов",
            "сил и вдохновения",
            "удачи в работе",
            "продолжайте пожалуйста",
            "уважение вам",
            "это правда важно",
            "буду следить дальше",
            "хорошая работа",
        ),
        "similar_openers": ("честно говоря", "хочу сказать", "отдельно", "просто"),
        "similar_tails": ("с уважением", "успехов", "хорошая работа", "буду следить"),
    },
    {
        "key": "chill",
        "starts": (
            "",
            "кста",
            "ну",
            "хз",
            "короче",
            "по-тихому",
            "ладно",
            "типа",
            "а вообще",
        ),
        "thanks": (
            "спс",
            "спасибо",
            "ну спс",
            "спасибочки",
            "от души",
            "респект",
            "спасибо бро",
        ),
        "subjects": (
            "за главу",
            "за тайтл",
            "за серию",
            "за часть",
            "за вайб",
            "за контент",
            "за обновление",
            "за то что не бросаете",
        ),
        "ends": (
            "",
            "норм",
            "зашло",
            "жду дальше",
            "пока ок",
            "без лишнего",
            "держите уровень",
            "неплохо вообще",
            "пойдёт",
        ),
        "similar_openers": ("кста", "ну", "хз", "короче", "типа"),
        "similar_tails": ("норм", "зашло", "пока ок", "пойдёт", "жду дальше"),
    },
    {
        "key": "warm",
        "starts": (
            "",
            "ой",
            "боже",
            "ну вот",
            "как же",
            "мне так",
            "просто",
            "слушайте",
            "честно",
        ),
        "thanks": (
            "спасибо",
            "огромное спасибо",
            "от души спасибо",
            "спасибо автору",
            "спасибо большое",
            "благодарю от сердца",
            "спасибо что есть",
        ),
        "subjects": (
            "за главу",
            "за этот тайтл",
            "за историю",
            "за эмоции",
            "за атмосферу",
            "автору за труды",
            "за то что радуете",
            "за тепло в главе",
            "за такую историю",
        ),
        "ends": (
            "",
            "мне очень приятно",
            "аж до слёз почти",
            "обнимаю автора мысленно",
            "уже жду новую главу",
            "спасибо что радуете",
            "читаю и улыбаюсь",
            "вы реально свет",
            "сердцу хорошо",
        ),
        "similar_openers": ("ой", "ну вот", "честно", "просто", "как же"),
        "similar_tails": ("приятно", "тепло", "жду новую", "улыбнуло", "зашло в душу"),
    },
    {
        "key": "slang",
        "starts": (
            "",
            "йо",
            "ну кароч",
            "слыш",
            "имба но",
            "бро",
            "реально же",
            "база",
            "честно",
        ),
        "thanks": (
            "респект",
            "красава спасибо",
            "от души",
            "спасибо жирно",
            "ну респект",
            "апвоут и спасибо",
            "спс огромное",
        ),
        "subjects": (
            "за главу",
            "за тайтл",
            "за вайб",
            "за кайф",
            "за арт",
            "за рисовку",
            "за сюжет",
            "автору за главу",
            "за такую раздачу",
        ),
        "ends": (
            "",
            "база",
            "имба",
            "жестко зашло",
            "не слабо",
            "жду дроп дальше",
            "держи планку",
            "чисто огонь",
            "я в деле дальше",
        ),
        "similar_openers": ("йо", "ну кароч", "бро", "база", "реально же"),
        "similar_tails": ("база", "имба", "жестко", "не слабо", "жду дроп"),
    },
    {
        "key": "quiet",
        "starts": (
            "",
            "тихо скажу",
            "на заметку",
            "просто",
            "между прочим",
            "мне",
            "вроде как",
            "ну да",
        ),
        "thanks": (
            "спасибо",
            "благодарю",
            "спасибо автору",
            "ну спасибо",
            "тихое спасибо",
            "спасибо небольшое но искреннее",
        ),
        "subjects": (
            "за главу",
            "за тайтл",
            "за работу",
            "за старания",
            "за продолжение",
            "за серию",
            "за аккуратную работу",
            "переводчику за главу",
        ),
        "ends": (
            "",
            "читаю дальше",
            "без шума но ценю",
            "мне достаточно",
            "спокойно зашло",
            "буду рядом",
            "не кричу но спасибо",
            "просто нравится",
        ),
        "similar_openers": ("просто", "ну да", "вроде как", "тихо скажу", "мне"),
        "similar_tails": ("тихо зашло", "читаю дальше", "нравится", "спокойно"),
    },
    {
        "key": "fan",
        "starts": (
            "",
            "как фанат скажу",
            "поддерживаю",
            "сразу",
            "не могу не написать",
            "обязательно",
            "в комментах просто",
            "ну всё",
        ),
        "thanks": (
            "спасибо",
            "огромное спасибо",
            "спасибо команде",
            "спасибо автору",
            "благодарю команду",
            "спасибо переводчику",
            "респект команде",
        ),
        "subjects": (
            "за главу",
            "за тайтл",
            "за труды",
            "за то что не бросаете",
            "за стабильные главы",
            "команде за труды",
            "автору за историю",
            "за то что радуете",
            "за долгую работу",
        ),
        "ends": (
            "",
            "поддерживаю вас",
            "вы лучшие",
            "не останавливайтесь пожалуйста",
            "я с вами",
            "фанатею дальше",
            "ждите донаты моральные",
            "держась за тайтл",
            "вы супер",
        ),
        "similar_openers": ("поддерживаю", "как фанат", "сразу", "ну всё"),
        "similar_tails": ("поддерживаю", "вы лучшие", "я с вами", "фанатею"),
    },
)

# Синонимы / близкие слова для «человеческого» перефраза чужих комментов
_COMMENT_SYNONYMS = {
    "норм": ("нормально", "норм", "ок", "нормас", "неплохо"),
    "нормально": ("норм", "нормально", "ок", "нормас"),
    "нормас": ("норм", "нормально", "нормас"),
    "ок": ("норм", "ок", "нормально"),
    "огонь": ("огонь", "кайф", "топ", "круто", "зашло"),
    "кайф": ("кайф", "огонь", "зашло", "топ"),
    "топ": ("топ", "огонь", "круто", "кайф"),
    "круто": ("круто", "топ", "огонь", "классно"),
    "классно": ("классно", "круто", "норм", "зашло"),
    "зашло": ("зашло", "заходит", "нравится", "кайф"),
    "заходит": ("заходит", "зашло", "держится", "нравится"),
    "нравится": ("нравится", "зашло", "импонирует", "заходит"),
    "держится": ("держится", "держит", "не проседает", "заходит"),
    "держит": ("держит", "держится", "тянет"),
    "интересно": ("интересно", "занятно", "интригует", "интереснее"),
    "интереснее": ("интереснее", "интереснее стало", "живее", "занятнее"),
    "занятно": ("занятно", "интересно", "прикольненько"),
    "жду": ("жду", "жду дальше", "интересно что дальше"),
    "дальше": ("дальше", "продолжения", "следующее"),
    "глава": ("глава", "серия", "часть"),
    "серия": ("серия", "глава", "часть"),
    "сюжет": ("сюжет", "история", "линия"),
    "история": ("история", "сюжет", "линия"),
    "арт": ("арт", "рисунок", "рисовка"),
    "рисунок": ("рисунок", "рисовка", "арт"),
    "рисовка": ("рисовка", "рисунок", "арт"),
    "персонаж": ("персонаж", "герой", "гг"),
    "герой": ("герой", "гг", "персонаж"),
    "гг": ("гг", "герой", "главный"),
    "странно": ("странно", "weird", "как-то странно", "необычно"),
    "необычно": ("необычно", "странно", "по-своему"),
    "плохо": ("слабо", "так себе", "не очень", "средне"),
    "слабо": ("слабо", "так себе", "не очень"),
    "средне": ("средне", "так себе", "на троечку"),
    "бомба": ("бомба", "огонь", "пушка", "топ"),
    "пушка": ("пушка", "бомба", "огонь"),
    "атмосфера": ("атмосфера", "вайб", "настроение"),
    "вайб": ("вайб", "атмосфера", "настроение"),
    "напряг": ("напряг", "напряжение", "интрига"),
    "интрига": ("интрига", "напряг", "завязка"),
    "смешно": ("смешно", "угар", "прикол", "забавно"),
    "угар": ("угар", "смешно", "прикол"),
    "грустно": ("грустно", "печально", "тяжеловато"),
    "мило": ("мило", "миленько", "милота"),
    "милота": ("милота", "мило", "миленько"),
    "тянет": ("тянет", "держит", "затягивает"),
    "затягивает": ("затягивает", "тянет", "не отпускает"),
    "не": ("не", "типа не", "как будто не"),
    "очень": ("очень", "прям", "довольно", "реально"),
    "прям": ("прям", "очень", "реально"),
    "реально": ("реально", "правда", "прям"),
    "конечно": ("конечно", "ясн", "ну да"),
    "вообще": ("вообще", "в целом", "если честно"),
    "кажется": ("кажется", "мне кажется", "по-моему"),
    "имхо": ("имхо", "по-мне", "мне кажется"),
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
    errors: int = 0
    last_url: str = ""
    last_action: str = ""
    last_at: str = ""
    started_at: str = ""
    night_break_until: str = ""
    # Ключи глав, куда уже писали комментарий: "slug/vol/ch" — строго 1 на главу
    commented_chapters: List[str] = field(default_factory=list)

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
        return cls(**cleaned)

    def to_telegram(self, delay_range: Tuple[float, float] = (5.0, 15.0)) -> str:
        flag = "🟢 Фарм" if self.running else "🔴 Остановлен"
        return (
            f"<b>📚 MangaBuff — статус</b>\n\n"
            f"Состояние: {flag}\n"
            f"📖 Глав: <b>{self.chapters_read}</b>\n"
            f"📄 Скроллов: <b>{self.pages_scrolled}</b>\n"
            f"📚 Тайтлов: <b>{self.titles_visited}</b>\n"
            f"🎁 Наград: <b>{self.rewards_claimed}</b>\n"
            f"🃏 Карт: <b>{self.cards_claimed}</b>\n"
            f"💬 Комментов: <b>{self.comments_posted}</b>\n"
            f"🗺 Макеты: <b>{self.layouts_visited}</b>\n"
            f"🎯 Ивент-действия: <b>{self.events_actions}</b>\n"
            f"⚠️ Ошибки: {self.errors}\n"
            f"⏱ Задержка: {delay_range[0]:.0f}–{delay_range[1]:.0f} сек\n"
            f"🌙 Ночной стоп: <code>{self.night_break_until or '—'}</code>\n"
            f"🔗 <code>{(self.last_url or '—')[:120]}</code>\n"
            f"📝 {self.last_action or '—'}\n"
            f"🕒 {self.last_at or '—'}"
        )


@dataclass
class ClaimResult:
    claimed: int = 0
    cards: int = 0
    details: List[str] = field(default_factory=list)
    message: str = ""

    def to_telegram(self) -> str:
        lines = [
            "<b>🎁 MangaBuff — сбор наград</b>",
            "",
            f"Забрано: <b>{self.claimed}</b>",
            f"Карт: <b>{self.cards}</b>",
        ]
        if self.details:
            lines += ["", "Детали:"] + [f"• {d}" for d in self.details[:12]]
        if self.message:
            lines += ["", self.message]
        return "\n".join(lines)


class MangaBuffService:
    REWARD_TEXTS = (
        "Забрать",
        "Забрать награду",
        "Получить",
        "Получить награду",
        "Получить карту",
        "Собрать",
        "Открыть",
        "Открыть пак",
        "Ежедневная награда",
        "Подтвердить",
        "Забрать бонус",
        "Забрать всё",
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
        self._last_comment_bits: Dict[str, Tuple[str, str]] = {}
        self._current_comment_slug: str = ""
        # Главы, куда уже писали (строго 1 комментарий на главу)
        self._commented_chapters: set[str] = set(self.stats.commented_chapters or [])

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

    def set_delay(
        self,
        delay_min: float,
        delay_max: float,
        steps_min: int = 8,
        steps_max: int = 12,
    ) -> None:
        self.delay_min_sec = max(0.08, float(delay_min))
        self.delay_max_sec = max(self.delay_min_sec, float(delay_max))
        self.steps_min = max(3, int(steps_min))
        self.steps_max = max(self.steps_min, int(steps_max))

    def _scroll_chunk_factor(self) -> float:
        """Крупнее прыжок скролла → меньше шагов (особенно в турбо)."""
        if self.delay_min_sec <= 0.2:
            return random.uniform(2.2, 3.2)
        if self.delay_min_sec <= 0.4:
            return random.uniform(1.7, 2.5)
        if self.delay_min_sec <= 0.8:
            return random.uniform(1.2, 1.8)
        if self.delay_min_sec <= 1.5:
            return random.uniform(0.95, 1.35)
        return random.uniform(0.70, 0.95)

    def _chapter_gap_pause(self) -> Tuple[float, float]:
        """Пауза между главами по темпу."""
        if self.delay_min_sec <= 0.2:
            return 0.25, 0.7
        if self.delay_min_sec <= 0.5:
            return 0.4, 1.1
        if self.delay_min_sec <= 1.0:
            return 0.8, 1.8
        return 1.5, 4.0

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
        # Спим кусками, чтобы реагировать на stop
        end_ts = asyncio.get_event_loop().time() + remaining.total_seconds()
        while asyncio.get_event_loop().time() < end_ts:
            if self._stop_flag.is_set():
                return
            await asyncio.sleep(min(30.0, end_ts - asyncio.get_event_loop().time()))
        self.stats.night_break_until = ""

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
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.config.selector_timeout_ms,
            )
            await self._human_pause(1.2, 2.8)
            await self._dismiss_overlays(page)
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
        assert self._page is not None
        page = self._page

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
            await email.fill(self.email)
            await self._human_pause(0.4, 1.0)
            await pwd.fill(self.password)
            await self._human_pause(0.5, 1.2)
            await page.locator("button.login-button").first.click()
            await self._human_pause(2.5, 4.5)
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
    # Rewards / events / layouts
    # ------------------------------------------------------------------

    async def claim_rewards(self) -> ClaimResult:
        # Во время фарма не уводим вкладку с главы — сбор уже есть в цикле
        if self.stats.running:
            return ClaimResult(
                message="Сейчас идёт чтение глав. Награды собираются в цикле фарма."
            )
        async with self._lock:
            return await self._claim_rewards_unlocked()

    async def _claim_rewards_unlocked(self, quick: bool = False) -> ClaimResult:
        if not self.is_started:
            await self.start(headless=True)
        assert self._page is not None
        page = self._page
        await self.ensure_login()
        result = ClaimResult()

        if quick:
            urls = [
                "https://mangabuff.ru/",
                "https://mangabuff.ru/notifications",
                "https://mangabuff.ru/battle",
                "https://mangabuff.ru/cards",
            ]
        else:
            urls = list(LAYOUT_URLS) + [
                "https://mangabuff.ru/promo-code",
                "https://mangabuff.ru/battle",
            ]
        for url in urls:
            if self._stop_flag.is_set():
                break
            logger.info("MangaBuff claim visit %s", url)
            if not await self._safe_goto(page, url):
                continue
            c, cards, details = await self._click_reward_buttons(page)
            result.claimed += c
            result.cards += cards
            result.details.extend(details)
            self.stats.rewards_claimed += c
            self.stats.cards_claimed += cards
            if "/battle" in url:
                ev = await self._farm_battle_events(page)
                result.details.extend(ev)
                self.stats.events_actions += len(ev)

        if result.claimed == 0 and result.cards == 0:
            result.message = "Активных кнопок наград не найдено (или уже собрано)."
        else:
            result.message = "Сбор завершён."
        self.stats.touch(f"сбор: +{result.claimed}")
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
                try:
                    await page.mouse.wheel(0, random.randint(400, 1200))
                except Exception:  # noqa: BLE001
                    pass
                await self._human_pause(1.5, 3.5)
        self.stats.touch(f"макеты: {visited}")
        return visited

    async def _farm_battle_events(self, page: Page) -> List[str]:
        details: List[str] = []
        # Кнопки забрать награду за выполненные дейлики / усиления
        for text in ("Забрать", "Получить", "Забрать награду", "Выбрать", "На арену", "В бой"):
            loc = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                count = 0
            for i in range(min(count, 4)):
                btn = loc.nth(i)
                try:
                    if not await btn.is_visible(timeout=700):
                        continue
                    label = ((await btn.inner_text(timeout=800)) or text).strip()
                    # не жмём «в бой» без колоды слишком агрессивно
                    if re.search(r"бой|арену", label, re.I) and random.random() > 0.25:
                        continue
                    await btn.click(force=True, timeout=2500)
                    details.append(f"battle: {label[:50]}")
                    await self._human_pause(1.0, 2.2)
                except Exception:  # noqa: BLE001
                    continue
        return details

    async def _click_reward_buttons(self, page: Page) -> Tuple[int, int, List[str]]:
        claimed = 0
        cards = 0
        details: List[str] = []
        await self._dismiss_overlays(page)

        # Один проход по видимым primary-кнопкам — быстрее десятков regex-локаторов
        try:
            labels = await page.evaluate(
                """() => [...document.querySelectorAll('button, a.button, [role=button]')]
                  .filter(el => {
                    const s = getComputedStyle(el);
                    return s && s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null;
                  })
                  .map(el => ({t: (el.innerText||'').trim().slice(0,80), c: el.className}))
                  .filter(x => x.t && /забрать|получить|собрать|открыть|подтвердить|claim|ежеднев/i.test(x.t))
                  .slice(0, 12)"""
            )
        except Exception:  # noqa: BLE001
            labels = []

        for item in labels:
            text = (item.get("t") or "").strip()
            if not text or len(text) > 40:
                continue
            try:
                loc = page.locator("button, a.button").filter(
                    has_text=re.compile(f"^{re.escape(text)}$", re.I)
                ).first
                if not await loc.is_visible(timeout=700):
                    continue
                await loc.click(force=True, timeout=2000)
                claimed += 1
                low = text.lower()
                if "карт" in low or "пак" in low or "card" in low:
                    cards += 1
                details.append(f"клик: {text[:60]}")
                await self._human_pause(0.4, 1.0)
                await self._dismiss_overlays(page)
            except Exception:  # noqa: BLE001
                continue

        for sel in (
            ".close-adult-modal-btn",
            ".modal button.button--primary",
        ):
            try:
                loc = page.locator(sel)
                n = min(await loc.count(), 2)
                for i in range(n):
                    el = loc.nth(i)
                    if await el.is_visible(timeout=700):
                        txt = ((await el.inner_text(timeout=400)) or "modal").strip()[:50]
                        await el.click(force=True, timeout=1500)
                        claimed += 1
                        details.append(f"modal: {txt}")
                        await self._human_pause(0.3, 0.8)
            except Exception:  # noqa: BLE001
                continue
        return claimed, cards, details

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
            await self._human_pause(0.8, 1.6)
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
        """Сколько глав читать: почти до конца, с небольшим хвостом."""
        if total_chapters <= 0:
            return TITLE_READ_HARD_CAP
        if total_chapters <= 3:
            return total_chapters
        leave_pct = int(total_chapters * random.uniform(*TITLE_LEAVE_PCT))
        leave = max(TITLE_LEAVE_MIN, min(TITLE_LEAVE_MAX, leave_pct or TITLE_LEAVE_MIN))
        # на коротких тайтлах хвост меньше
        if total_chapters < 20:
            leave = min(leave, max(1, total_chapters // 10))
        target = max(1, total_chapters - leave)
        # никогда не меньше ~90% тайтла
        target = max(target, int(total_chapters * 0.90))
        return min(target, total_chapters)

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

        # Прямой URL первой главы — самый надёжный путь
        if slug:
            for candidate in (
                f"https://mangabuff.ru/manga/{slug}/1/1",
                f"https://mangabuff.ru/manga/{slug}/1/0",
            ):
                if await self._safe_goto(page, candidate):
                    got = await _ok_reader()
                    if got:
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
                    await self._human_pause(2.0, 3.5)
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
        """Доскроллить главу до конца; темп зависит от пресета (турбо = крупные шаги)."""
        steps = 0
        chapter_url = page.url.split("?")[0]
        try:
            total_height = await page.evaluate(
                "() => document.body.scrollHeight || 4000"
            )
            viewport = await page.evaluate("() => window.innerHeight || 900")
        except Exception:  # noqa: BLE001
            total_height, viewport = 4000, 900

        viewport = max(int(viewport), 600)
        chunk_factor = self._scroll_chunk_factor()
        avg_chunk = max(viewport * 0.8, viewport * chunk_factor)
        pages_approx = max(1, int(total_height / avg_chunk))
        # запас на lazy-load, но без сотен мелких шагов
        max_steps = min(80, max(self.steps_max, pages_approx + random.randint(1, 3)))
        position = 0
        logger.info(
            "MangaBuff scroll start height=%s viewport=%s max_steps=%s "
            "chunk=%.1fx delay=%.2f-%.2f",
            total_height,
            viewport,
            max_steps,
            chunk_factor,
            self.delay_min_sec,
            self.delay_max_sec,
        )
        while position + viewport < total_height - 60 and steps < max_steps:
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

            factor = self._scroll_chunk_factor()
            chunk = int(viewport * factor)
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
                new_h = await page.evaluate(
                    "() => document.body.scrollHeight || 4000"
                )
                if new_h > total_height:
                    total_height = min(new_h, total_height + int(viewport * factor * 2))
                    if steps > max_steps - 2 and new_h > position + viewport:
                        max_steps = min(80, max_steps + 3)
            except Exception:  # noqa: BLE001
                pass

        try:
            # мгновенный доскролл вниз в турбо, иначе smooth
            if self.delay_min_sec <= 0.35:
                await page.evaluate(
                    "() => window.scrollTo(0, document.body.scrollHeight)"
                )
                await self._human_pause(0.15, 0.35)
            else:
                await page.evaluate(
                    "() => window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})"
                )
                await self._human_pause(0.35, 0.8)
            c, cards, _ = await asyncio.wait_for(
                self._click_reward_buttons(page), timeout=8
            )
            self.stats.rewards_claimed += c
            self.stats.cards_claimed += cards
        except Exception:  # noqa: BLE001
            pass
        return steps

    async def _animate_scroll(self, page: Page, start: int, end: int) -> None:
        # В турбо — почти без анимации кадров
        if self.delay_min_sec <= 0.2:
            frames, frame_sleep = 1, (0.0, 0.0)
        elif self.delay_min_sec <= 0.4:
            frames, frame_sleep = random.randint(2, 3), (0.01, 0.03)
        elif self.delay_min_sec <= 0.9:
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

    async def _go_next_chapter(self, page: Page) -> bool:
        before = page.url.split("?")[0]
        # Текст вида «След. глава 1 - 3»
        candidates = (
            page.get_by_role("link", name=re.compile(r"След\.?\s*глава", re.I)),
            page.get_by_text(re.compile(r"След\.?\s*глава", re.I)),
            page.locator("a[href*='/manga/']").filter(
                has_text=re.compile(r"След", re.I)
            ),
        )
        for loc in candidates:
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(count, 4)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible(timeout=700):
                        continue
                    await el.click(force=True)
                    await self._human_pause(1.8, 3.2)
                    await self._dismiss_overlays(page)
                    now = page.url.split("?")[0]
                    if now != before and re.search(r"/manga/[^/]+/\d+/\d+", now):
                        logger.info("MangaBuff next chapter via click %s", now)
                        return True
                except Exception:  # noqa: BLE001
                    continue

        # URL-инкремент: следующая глава или следующий том
        m = re.search(r"(https://mangabuff\.ru/manga/[^/]+/)(\d+)/(\d+)", before)
        if not m:
            return False
        base, vol, ch = m.group(1), int(m.group(2)), int(m.group(3))
        for nxt in (f"{base}{vol}/{ch + 1}", f"{base}{vol + 1}/1"):
            if not await self._safe_goto(page, nxt):
                continue
            title = await page.title()
            now = page.url.split("?")[0]
            if now == before or "404" in title.lower():
                continue
            if re.search(r"/manga/[^/]+/\d+/\d+", now):
                logger.info("MangaBuff next chapter via URL %s", now)
                return True
        return False

    # ------------------------------------------------------------------
    # Comments — каждые 5–15 глав, на рандомной главе тайтла
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

    def _pick_uncommented_chapter(
        self, slug: str, vol: int, cur_ch: int
    ) -> Optional[int]:
        """Случайная глава тайтла, куда ещё не писали. Строго 1 коммент на главу."""
        hi = max(cur_ch, 3)
        # сначала в уже «доступном» диапазоне, включая текущую
        for span in (hi, hi + 20, hi + 60, max(hi, 120)):
            candidates = [
                c
                for c in range(1, span + 1)
                if self._chapter_comment_key(slug, vol, c) not in self._commented_chapters
            ]
            if candidates:
                return random.choice(candidates)
        return None

    async def _maybe_comment(self, page: Page) -> bool:
        """Каждые 5–15 глав: 1 комментарий на ещё не комментированную главу тайтла."""
        self._chapters_since_comment += 1
        if self._chapters_since_comment < self._next_comment_after:
            return False

        resume_url = page.url.split("?")[0]
        parsed = self._parse_chapter_url(resume_url)
        if not parsed:
            logger.info("MangaBuff comment skip: not on chapter url %s", resume_url)
            self._next_comment_after = self._chapters_since_comment + random.randint(1, 3)
            return False

        slug, vol, cur_ch = parsed
        target_ch = self._pick_uncommented_chapter(slug, vol, cur_ch)
        if target_ch is None:
            logger.info(
                "MangaBuff comment skip: no uncommented chapters for %s/%s (known=%s)",
                slug,
                vol,
                sum(1 for k in self._commented_chapters if k.startswith(f"{slug}/{vol}/")),
            )
            self._next_comment_after = self._chapters_since_comment + random.randint(
                COMMENT_EVERY_MIN, COMMENT_EVERY_MAX
            )
            return False

        target_key = self._chapter_comment_key(slug, vol, target_ch)
        # двойная защита от гонки / повтора
        if target_key in self._commented_chapters:
            self._next_comment_after = self._chapters_since_comment + random.randint(1, 2)
            return False

        target_url = f"https://mangabuff.ru/manga/{slug}/{vol}/{target_ch}"

        logger.info(
            "MangaBuff comment due after %s chapters → chapter %s (resume %s)",
            self._chapters_since_comment,
            target_url,
            resume_url,
        )

        try:
            if not await self._safe_goto(page, target_url):
                self._next_comment_after = self._chapters_since_comment + random.randint(1, 2)
                return False
            await self._dismiss_overlays(page)
            await self._human_pause(1.0, 2.0)

            # ещё раз проверить ключ по фактическому URL
            landed = self._parse_chapter_url(page.url)
            if not landed:
                self._next_comment_after = self._chapters_since_comment + random.randint(1, 2)
                return False
            land_slug, land_vol, land_ch = landed
            land_key = self._chapter_comment_key(land_slug, land_vol, land_ch)
            if land_key in self._commented_chapters:
                logger.info("MangaBuff comment skip: already commented %s", land_key)
                self._next_comment_after = self._chapters_since_comment + random.randint(1, 3)
                if page.url.split("?")[0] != resume_url:
                    await self._safe_goto(page, resume_url)
                return False

            ok = await self._post_human_comment(page, slug=land_slug)
            if ok:
                self._mark_chapter_commented(land_slug, land_vol, land_ch)
                self._chapters_since_comment = 0
                self._next_comment_after = random.randint(
                    COMMENT_EVERY_MIN, COMMENT_EVERY_MAX
                )
                self._persist_stats()
                logger.info("MangaBuff comment locked to chapter %s", land_key)
            else:
                self._next_comment_after = self._chapters_since_comment + random.randint(
                    1, 3
                )

            # вернуться к чтению
            if page.url.split("?")[0] != resume_url:
                await self._safe_goto(page, resume_url)
                await self._dismiss_overlays(page)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff comment flow failed: %s", exc)
            self._next_comment_after = self._chapters_since_comment + random.randint(1, 3)
            try:
                await self._safe_goto(page, resume_url)
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
                    await self._human_pause(1.2, 2.2)
                    break
            except Exception:  # noqa: BLE001
                continue

        # вкладка «Популярные» — там живее примеры
        try:
            pop = page.locator("button.comments__change-sort", has_text="Популярные")
            if await pop.count() and await pop.first.is_visible(timeout=600):
                await pop.first.click(force=True)
                await self._human_pause(0.8, 1.5)
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
                    await self._human_pause(0.8, 1.4)
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
            await self._human_pause(0.3, 0.8)
            await area.fill("")
            # «печатает» с паузами
            for ch in text:
                await area.type(ch, delay=random.randint(35, 120))
                if random.random() < 0.04:
                    await asyncio.sleep(random.uniform(0.15, 0.45))
            await self._human_pause(0.7, 1.8)

            send = page.locator("button.comments__send-btn").first
            if await send.count() == 0:
                send = page.get_by_role("button", name=re.compile(r"отправ", re.I)).first
            await send.click(force=True, timeout=4000)
            await self._human_pause(1.5, 3.0)

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

    def _comment_word_set(self, text: str) -> set:
        return {w for w in re.findall(r"[а-яёa-z0-9\-']+", text.lower()) if len(w) > 1}

    def _comment_similarity(self, a: str, b: str) -> float:
        sa, sb = self._comment_word_set(a), self._comment_word_set(b)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / max(1, len(sa | sb))

    def _extract_comment_ideas(self, cleaned: Sequence[str]) -> List[str]:
        """Достать короткие смысловые куски (2–5 слов) из разных комментов."""
        ideas: List[str] = []
        stop = {
            "и",
            "а",
            "но",
            "же",
            "ли",
            "бы",
            "то",
            "это",
            "как",
            "что",
            "уже",
            "ещё",
            "еще",
            "вот",
            "там",
            "тут",
            "для",
            "про",
            "при",
            "над",
            "под",
            "без",
            "или",
            "если",
            "когда",
            "просто",
            "типа",
            "вообще",
            "очень",
            "прям",
            "просто",
        }
        for s in cleaned:
            words = [w for w in s.split() if w and w not in stop]
            if len(words) < 2:
                continue
            # окна по 2–4 значимых слова
            for i in range(len(words)):
                for n in (2, 3, 4):
                    chunk = words[i : i + n]
                    if len(chunk) < 2:
                        continue
                    ideas.append(" ".join(chunk))
        random.shuffle(ideas)
        return ideas

    def _paraphrase_idea(self, idea: str) -> str:
        """Синонимизировать значимые слова, служебные оставить."""
        keep = {
            "и",
            "а",
            "но",
            "же",
            "не",
            "бы",
            "то",
            "в",
            "на",
            "с",
            "у",
            "по",
            "из",
            "к",
            "от",
            "за",
            "мне",
            "ему",
            "её",
            "ее",
            "его",
            "уже",
            "ещё",
            "еще",
        }
        out: List[str] = []
        for w in idea.split():
            if w in keep or w not in _COMMENT_SYNONYMS:
                # иногда всё же заменить если есть словарь
                if w in _COMMENT_SYNONYMS and random.random() < 0.7:
                    out.append(self._synonymize_token(w))
                else:
                    out.append(w)
            else:
                out.append(self._synonymize_token(w))
        return " ".join(out)

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
        """Число уникальных благодарностей по всем голосам."""
        uniq = set()
        for voice in _COMMENT_VOICES:
            for start in voice["starts"]:
                for thanks in voice["thanks"]:
                    for subject in voice["subjects"]:
                        for end in voice["ends"]:
                            parts = [p for p in (start, thanks, subject, end) if p]
                            text = re.sub(r"\s+", " ", " ".join(parts)).strip().lower()
                            if text.count(" за ") >= 2:
                                continue
                            mentions = sum(
                                len(re.findall(stem, text))
                                for stem in ("автор", "переводчик", "команд")
                            )
                            if mentions >= 2:
                                continue
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
        return text if self._is_safe_comment(text) else ""

    def _craft_thanks_comment(self, slug: str = "") -> str:
        """Благодарность в интонации тайтла: разное начало/конец, 5–20 слов."""
        voice = self._voice_for_slug(slug)
        last_start, last_end = self._last_comment_bits.get(slug or "default", ("", ""))
        for _ in range(50):
            start = self._pick_varied(voice["starts"], avoid=last_start)
            thanks = random.choice(voice["thanks"])
            subject = random.choice(voice["subjects"])
            end = self._pick_varied(voice["ends"], avoid=last_end)
            # иногда без начала или без хвоста — но не оба сразу слишком часто
            roll = random.random()
            if roll < 0.22:
                start = ""
            elif roll > 0.78:
                end = ""
            # не начинать thanks-фразой которая уже в start
            if start and thanks.startswith(start):
                start = ""
            joined = " ".join(p for p in (start, thanks, subject, end) if p)
            low = joined.lower()
            mentions = sum(
                len(re.findall(stem, low)) for stem in ("автор", "переводчик", "команд")
            )
            if mentions >= 2 or low.count(" за ") >= 2:
                continue
            # thanks уже содержит «за …» — subject не должен дублировать
            if " за " in thanks and subject.startswith("за "):
                continue
            text = self._finalize_comment_text(joined)
            if not text:
                continue
            self._last_comment_bits[slug or "default"] = (start, end)
            return text
        return "спасибо за главу жду дальше"

    def _craft_comment(self, samples: Sequence[str], slug: str = "") -> str:
        """
        65% — благодарности в голосе тайтла,
        35% — похожие на чужие, тоже с интонацией тайтла.
        """
        self._current_comment_slug = slug or ""
        if random.random() < THANKS_COMMENT_CHANCE:
            return self._craft_thanks_comment(slug=slug)
        return self._craft_similar_comment(samples, slug=slug)

    def _craft_similar_comment(self, samples: Sequence[str], slug: str = "") -> str:
        """Похожий на чужие: синонимы + куски, интонация тайтла."""
        cleaned: List[str] = []
        for s in samples:
            norm = self._normalize_comment_sample(s)
            if 8 <= len(norm) <= 160 and self._is_safe_comment(norm):
                cleaned.append(norm)

        voice = self._voice_for_slug(slug)
        last_start, last_end = self._last_comment_bits.get(slug or "default", ("", ""))
        openers = tuple(voice.get("similar_openers") or ("ну", "кста", "честно"))
        bridges = ("и", "но", "хотя", "кста", "ещё", "плюс")
        tails = tuple(
            voice.get("similar_tails")
            or (
                "норм",
                "зашло",
                "держит",
                "жду дальше",
                "интереснее стало",
                "неплохо",
            )
        )
        fallbacks = (
            "ну глава в целом норм зашло",
            "имхо интереснее стало жду дальше",
            "кста сюжет держит и вайб кайф",
            "честно рисовка приятная и тянет читать",
            "по-моему пока ок и интрига есть",
            "ладно зашло сильнее чем думал",
            "хз но атмосфера огонь и держит",
            "короче норм глава жду продолжение",
        )

        target_words = random.randint(COMMENT_WORDS_MIN, COMMENT_WORDS_MAX)
        text = ""
        used_sources: List[str] = []

        if cleaned:
            # 2–3 идеи из разных комментов
            pool = list(cleaned)
            random.shuffle(pool)
            picked: List[str] = []
            for src in pool:
                if len(picked) >= 3:
                    break
                src_ideas = [
                    i
                    for i in self._extract_comment_ideas([src])
                    if 2 <= len(i.split()) <= 4
                ]
                if not src_ideas:
                    continue
                used_sources.append(src)
                picked.append(self._paraphrase_idea(random.choice(src_ideas)))

            used_start, used_end = "", ""
            if not picked:
                text = random.choice(fallbacks)
            else:
                chunks: List[str] = []
                if random.random() < 0.7:
                    used_start = self._pick_varied(openers, avoid=last_start)
                    chunks.append(used_start)
                chunks.append(picked[0])
                for idea in picked[1:]:
                    if random.random() < 0.75:
                        chunks.append(random.choice(bridges))
                    chunks.append(idea)
                if random.random() < 0.7:
                    used_end = self._pick_varied(tails, avoid=last_end)
                    chunks.append(used_end)

                words = " ".join(chunks).split()
                dangling = {
                    "и",
                    "а",
                    "но",
                    "хотя",
                    "кста",
                    "ещё",
                    "еще",
                    "плюс",
                    "типа",
                    "не",
                    "как",
                    "что",
                }
                while words and words[-1] in dangling:
                    words.pop()
                if len(words) < COMMENT_WORDS_MIN:
                    used_end = used_end or self._pick_varied(tails, avoid=last_end)
                    words.extend(used_end.split())
                if len(words) > target_words:
                    words = words[:target_words]
                    while words and words[-1] in dangling:
                        words.pop()
                if len(words) < COMMENT_WORDS_MIN:
                    words.extend(self._pick_varied(tails, avoid=last_end).split())
                words = words[:COMMENT_WORDS_MAX]
                text = " ".join(words)

            for _ in range(3):
                if not used_sources:
                    break
                if all(
                    self._comment_similarity(text, src) < 0.5 for src in used_sources
                ):
                    break
                text = self._paraphrase_idea(text)
            self._last_comment_bits[slug or "default"] = (used_start, used_end)
        else:
            text = random.choice(fallbacks)

        text = self._finalize_comment_text(text) or random.choice(fallbacks)
        return text

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

    async def _read_title_almost_end(
        self,
        page: Page,
        title: Dict[str, Any],
        on_progress=None,
    ) -> int:
        """Прочитать один тайтл почти до конца. Вызывать под self._lock."""
        slug = str(title.get("slug") or "")
        logger.info("MangaBuff open title %s", slug)
        total_chapters = await self._estimate_title_chapters(slug)
        max_per_title = self._chapters_target_for_title(total_chapters)
        logger.info(
            "MangaBuff will read ~%s/%s chapters of %s",
            max_per_title,
            total_chapters or "?",
            slug,
        )

        start_url = await self._open_first_chapter(title.get("href") or "")
        if not start_url or not re.search(r"/manga/[^/]+/\d+/\d+", start_url):
            logger.warning("MangaBuff cannot open %s", slug)
            return 0

        self.stats.titles_visited += 1
        self.stats.touch(f"тайтл: {str(title.get('title') or '')[:40]}", start_url)
        logger.info(
            "MangaBuff reading %s from %s (target=%s)",
            slug,
            start_url,
            max_per_title,
        )
        self._persist_stats()

        chapters_this_title = 0
        last_chapter_url = start_url.split("?")[0]
        self._skip_title.clear()

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
                if not await self._go_next_chapter(page):
                    break
                continue

            self._read_urls.add(url)
            last_chapter_url = url
            logger.info("MangaBuff scroll chapter %s", url)

            try:
                await asyncio.wait_for(self._dismiss_overlays(page), timeout=8)
            except Exception:  # noqa: BLE001
                pass
            try:
                c, cards, _ = await asyncio.wait_for(
                    self._click_reward_buttons(page), timeout=12
                )
                self.stats.rewards_claimed += c
                self.stats.cards_claimed += cards
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

            self.stats.chapters_read += 1
            chapters_this_title += 1
            last_chapter_url = final_url
            self.stats.touch("глава прочитана", final_url)
            self._persist_stats()
            logger.info(
                "MangaBuff chapter done steps=%s total_chapters=%s url=%s",
                steps,
                self.stats.chapters_read,
                final_url,
            )

            await self._maybe_comment(page)
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
            if not await self._go_next_chapter(page):
                logger.info(
                    "MangaBuff title %s ended naturally after %s chapters",
                    slug,
                    chapters_this_title,
                )
                break

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
        self.stats.touch("фарм запущен")

        if not self.is_started:
            await self.start(headless=True)
        assert self._page is not None
        page = self._page

        if not await self.ensure_login():
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
                # Лок на навигацию: параллельные «Награды/Макеты» больше
                # не уводят вкладку во время чтения.
                async with self._lock:
                    logger.info("MangaBuff farm cycle %s: rewards", cycle)
                    await self._claim_rewards_unlocked(quick=True)
                    if cycle == 1 or cycle % 4 == 0:
                        logger.info("MangaBuff farm cycle %s: layouts", cycle)
                        await self._explore_layouts_unlocked()
                    logger.info("MangaBuff farm cycle %s: fetch popular", cycle)
                    titles = await self.fetch_popular_titles(limit=24)

                if not titles:
                    self.stats.touch("каталог пуст — пауза")
                    await self._human_pause(20, 40)
                    continue
                logger.info(
                    "MangaBuff titles: %s",
                    ", ".join(t["slug"] for t in titles[:8]),
                )

                for title in titles:
                    if self._stop_flag.is_set():
                        break
                    await self._await_night_break_if_needed()
                    if self._stop_flag.is_set():
                        break

                    slug = str(title.get("slug") or "")
                    async with self._lock:
                        chapters_this_title = await self._read_title_almost_end(
                            page,
                            title,
                            on_progress=on_progress,
                        )
                    logger.info(
                        "MangaBuff finished title %s: read %s chapters",
                        slug,
                        chapters_this_title,
                    )
                    await self._human_pause(5.0, 12.0)

            except Exception as exc:  # noqa: BLE001
                logger.exception("MangaBuff farm_loop")
                self.stats.errors += 1
                self.stats.touch(f"ошибка: {exc}")
                await self._human_pause(5.0, 12.0)

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
            self.stats.chapters_read += 1
            done += 1
            await self._maybe_comment(page)
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
