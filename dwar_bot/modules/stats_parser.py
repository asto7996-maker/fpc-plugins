"""
Парсер статистики персонажа, рюкзака и системных уведомлений Dwar.

Извлекает данные из игровых фреймов (main / user / backpack / chat)
через Playwright + BeautifulSoup с безопасным фоллбеком на последнее
известное состояние при перезагрузке фреймов.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from dwar_bot.config import BotConfig, config, get_delay_range

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Регулярные выражения для парсинга текста Dwar
# ---------------------------------------------------------------------------

# HP / MP / энергия: "123/456", "HP: 12 / 34", "hp=10 max=20"
RE_FRACTION = re.compile(
    r"(?P<cur>\d+)\s*/\s*(?P<max>\d+)",
    re.IGNORECASE,
)
RE_HP_LABEL = re.compile(
    r"(?:hp|здоров|жит)\s*[:=]?\s*(?P<cur>\d+)\s*(?:/|из)\s*(?P<max>\d+)",
    re.IGNORECASE,
)
RE_MP_LABEL = re.compile(
    r"(?:mp|мана|эйфор|эф)\s*[:=]?\s*(?P<cur>\d+)\s*(?:/|из)\s*(?P<max>\d+)",
    re.IGNORECASE,
)

# Монеты: "12 зол. 3 сер. 45 мед.", "золото: 10", иконки + числа рядом
RE_GOLD = re.compile(
    r"(?P<value>\d[\d\s]*)\s*(?:зол(?:ота|ото|\.?|от)?|gold)",
    re.IGNORECASE,
)
RE_SILVER = re.compile(
    r"(?P<value>\d[\d\s]*)\s*(?:сер(?:ебра|ебро|\.?|еб)?|silver)",
    re.IGNORECASE,
)
RE_COPPER = re.compile(
    r"(?P<value>\d[\d\s]*)\s*(?:мед(?:и|ь|\.?)|медяк(?:ов|а)?|copper|brass)",
    re.IGNORECASE,
)
# Компактный формат "12.3.45" или "12з 3с 45м"
RE_MONEY_COMPACT = re.compile(
    r"(?P<gold>\d+)\s*[зz.]\s*(?P<silver>\d+)\s*[сs.]\s*(?P<copper>\d+)\s*[мm]?",
    re.IGNORECASE,
)
RE_MONEY_DOTTED = re.compile(r"(?P<gold>\d+)\.(?P<silver>\d+)\.(?P<copper>\d+)")

# Координаты / локация: "[12, 34]", "x:12 y:34", "Локация: Пещера"
RE_COORDS = re.compile(
    r"(?:\[|\()\s*(?P<x>-?\d+)\s*[,;:]\s*(?P<y>-?\d+)\s*(?:\]|\))"
)
RE_COORDS_XY = re.compile(
    r"(?:^|[^\w])x\s*[:=]\s*(?P<x>-?\d+)\s*[,;\s]+y\s*[:=]\s*(?P<y>-?\d+)",
    re.IGNORECASE,
)
RE_LOCATION = re.compile(
    r"(?:локац(?:ия|ии)|location|место)\s*[:=]\s*(?P<name>[^\n|<]{2,80})",
    re.IGNORECASE,
)

# Предметы: "Эликсир (3)", "x5", "зарядов: 2"
RE_ITEM_COUNT = re.compile(
    r"(?:\((?P<a>\d+)\)|\bx\s*(?P<b>\d+)\b|(?:кол(?:ичество)?|зарядов?|шт)\s*[:=]\s*(?P<c>\d+))",
    re.IGNORECASE,
)
RE_SLOT_INDEX = re.compile(r"(?:slot|карман|ячейк)\s*[:=_]?\s*(?P<idx>\d+)", re.I)
RE_ACTION_ID = re.compile(
    r"(?:item[_-]?id|id|use|action)\s*[:=]\s*['\"]?(?P<id>[\w.-]+)",
    re.IGNORECASE,
)
RE_ACTION_URL = re.compile(
    r"(?:href|action|onclick)\s*[:=]\s*['\"](?P<url>[^'\"]+)['\"]",
    re.IGNORECASE,
)

# Уведомления
RE_DAMAGE = re.compile(
    r"(?:нанес(?:ено|ли)?|получен(?:о)?\s+урон|урон(?:а)?)\s*[:=]?\s*\d+",
    re.IGNORECASE,
)
RE_ITEM_GAIN = re.compile(
    r"(?:получен(?:о|а)?|наход(?:ишь|ите)|подобрал(?:и)?|добавлен)\s+",
    re.IGNORECASE,
)
RE_ERROR = re.compile(
    r"(?:ошибк|невозможн|не удалось|нельзя|запрещ|fail|error)",
    re.IGNORECASE,
)
RE_WARNING = re.compile(
    r"(?:вниман|предупрежд|мало\s+(?:hp|жизн|маны|энерг)|warning)",
    re.IGNORECASE,
)

FrameLike = Union[Page, Frame]


class NotificationType(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class PlayerStats:
    """Состояние персонажа."""

    hp_current: int = 0
    hp_max: int = 0
    mp_current: int = 0
    mp_max: int = 0
    in_combat: bool = False
    gold: int = 0
    silver: int = 0
    copper: int = 0
    location: str = ""
    coord_x: Optional[int] = None
    coord_y: Optional[int] = None
    nickname: str = ""
    level: int = 0
    energy_current: int = 0
    energy_max: int = 0
    stale: bool = False
    parsed_at: float = field(default_factory=time.time)
    source_frame: str = ""
    raw_snippet: str = ""

    @property
    def hp_ratio(self) -> float:
        if self.hp_max <= 0:
            return 0.0
        return max(0.0, min(1.0, self.hp_current / self.hp_max))

    @property
    def mp_ratio(self) -> float:
        if self.mp_max <= 0:
            return 0.0
        return max(0.0, min(1.0, self.mp_current / self.mp_max))

    @property
    def total_copper(self) -> int:
        """Баланс в медных монетах (1з = 100с, 1с = 100м — типичная схема)."""
        return self.gold * 10_000 + self.silver * 100 + self.copper

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BackpackItem:
    """Предмет в рюкзаке / кармане пояса."""

    name: str
    count: int = 1
    slot_index: int = -1
    action_url: str = ""
    action_id: str = ""
    item_type: str = ""  # elixir / scroll / potion / other
    charges: Optional[int] = None
    tooltip: str = ""
    stale: bool = False

    @property
    def usable_id(self) -> str:
        return self.action_id or self.action_url

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GameNotification:
    """Системное сообщение / уведомление."""

    text: str
    timestamp: float = field(default_factory=time.time)
    type: NotificationType = NotificationType.INFO
    source: str = ""  # banner / chat / popup
    stale: bool = False

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        data["timestamp_iso"] = datetime.fromtimestamp(
            self.timestamp, tz=timezone.utc
        ).isoformat()
        return data


class StatsParser:
    """
    Асинхронный парсер статов, рюкзака и уведомлений.

    При недоступности фреймов возвращает последнее известное состояние
    с флагом ``stale=True``.
    """

    USER_FRAME_NAMES: Tuple[str, ...] = (
        "main",
        "user",
        "pers",
        "person",
        "char",
        "character",
        "stats",
    )
    BACKPACK_FRAME_NAMES: Tuple[str, ...] = (
        "backpack",
        "inventory",
        "inv",
        "bag",
        "items",
    )
    CHAT_FRAME_NAMES: Tuple[str, ...] = ("chat", "syschat", "system", "log")

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        frame_timeout_ms: int = 8_000,
    ) -> None:
        self._config = bot_config or config
        self._selectors = self._config.selectors
        self._frame_timeout_ms = frame_timeout_ms

        self._last_stats: Optional[PlayerStats] = None
        self._last_backpack: List[BackpackItem] = []
        self._last_notifications: List[GameNotification] = []
        self._seen_notification_keys: set[str] = set()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    async def parse_player_stats(self, page: Page) -> PlayerStats:
        """
        Парсит HP/MP, деньги, локацию и статус боя из main/user фрейма.

        При ошибке или недоступности фрейма возвращает последнее состояние
        с ``stale=True`` (или безопасный дефолт, если кэша ещё нет).
        """
        await self._human_pause("action")
        try:
            frame = await self._resolve_frame(
                page,
                css_selectors=self._selectors.main_frame,
                frame_names=self.USER_FRAME_NAMES,
                extra_css=(
                    "frame[name='user'], iframe[name='user'], "
                    "#user_frame, frame[name='pers'], iframe[name='pers']"
                ),
            )
            html, source = await self._get_frame_html(page, frame)
            if not html.strip():
                raise RuntimeError("Пустой HTML фрейма персонажа")

            soup = BeautifulSoup(html, "html.parser")
            text = self._visible_text(soup)

            hp_cur, hp_max = self._parse_bar_pair(
                soup,
                text,
                css_candidates=(
                    self._selectors.profile_hp,
                    "#hp",
                    ".hp",
                    "[data-stat='hp']",
                    "#life",
                    ".life",
                    "img[src*='hp']",
                    "div[id*='hp']",
                    "span[id*='hp']",
                ),
                label_re=RE_HP_LABEL,
                width_hints=("hp", "life", "health"),
            )
            mp_cur, mp_max = self._parse_bar_pair(
                soup,
                text,
                css_candidates=(
                    self._selectors.profile_mp,
                    "#mp",
                    ".mp",
                    "[data-stat='mp']",
                    "#mana",
                    ".mana",
                    "#euphoria",
                    ".euphoria",
                    "div[id*='mp']",
                    "span[id*='mp']",
                ),
                label_re=RE_MP_LABEL,
                width_hints=("mp", "mana", "euphoria", "ef"),
            )
            energy_cur, energy_max = self._parse_bar_pair(
                soup,
                text,
                css_candidates=(
                    self._selectors.profile_energy,
                    "#energy",
                    ".energy",
                    "[data-stat='energy']",
                ),
                label_re=re.compile(
                    r"(?:energy|энерг)\s*[:=]?\s*(?P<cur>\d+)\s*(?:/|из)\s*(?P<max>\d+)",
                    re.I,
                ),
                width_hints=("energy", "en"),
            )

            gold, silver, copper = self._parse_money(soup, text)
            location, coord_x, coord_y = self._parse_location(soup, text)
            in_combat = await self._detect_combat(page, soup, text)
            nickname = self._first_text(
                soup,
                (
                    self._selectors.profile_nickname,
                    ".nick",
                    "#nick",
                    ".user-nick",
                    "[data-role='nickname']",
                ),
            )
            level = self._parse_int_from_selectors(
                soup,
                (
                    self._selectors.profile_level,
                    ".level",
                    "#level",
                    "[data-role='level']",
                ),
            )

            stats = PlayerStats(
                hp_current=hp_cur,
                hp_max=hp_max,
                mp_current=mp_cur,
                mp_max=mp_max,
                in_combat=in_combat,
                gold=gold,
                silver=silver,
                copper=copper,
                location=location,
                coord_x=coord_x,
                coord_y=coord_y,
                nickname=nickname,
                level=level,
                energy_current=energy_cur,
                energy_max=energy_max,
                stale=False,
                parsed_at=time.time(),
                source_frame=source,
                raw_snippet=text[:500],
            )
            self._last_stats = stats
            logger.info(
                "Статы: HP %s/%s MP %s/%s money=%s.%s.%s loc=%s combat=%s",
                stats.hp_current,
                stats.hp_max,
                stats.mp_current,
                stats.mp_max,
                stats.gold,
                stats.silver,
                stats.copper,
                stats.location or "?",
                stats.in_combat,
            )
            return stats

        except Exception as exc:
            logger.error(
                "Ошибка parse_player_stats: %s",
                exc,
                exc_info=True,
            )
            return self._stale_stats(reason=str(exc))

    async def parse_backpack(self, page: Page) -> List[BackpackItem]:
        """
        Сканирует фрейм рюкзака и собирает предметы пояса
        (банки, эликсиры, свитки) с количеством/зарядами.
        """
        await self._human_pause("action")
        try:
            frame = await self._resolve_frame(
                page,
                css_selectors=self._selectors.backpack_frame,
                frame_names=self.BACKPACK_FRAME_NAMES,
                extra_css=self._selectors.inventory_panel,
            )
            html, source = await self._get_frame_html(page, frame)
            if not html.strip():
                # Иногда предметы висят в main-фрейме (пояс)
                frame = await self._resolve_frame(
                    page,
                    css_selectors=self._selectors.main_frame,
                    frame_names=self.USER_FRAME_NAMES,
                )
                html, source = await self._get_frame_html(page, frame)
                if not html.strip():
                    raise RuntimeError("Пустой HTML фрейма рюкзака")

            soup = BeautifulSoup(html, "html.parser")
            items = self._extract_backpack_items(soup)

            # Дополнительно: Playwright-локаторы для data-атрибутов
            if frame is not None:
                pw_items = await self._extract_backpack_via_playwright(frame)
                items = self._merge_backpack_items(items, pw_items)

            if not items:
                # Фоллбек: ищем на всей странице
                page_html = await page.content()
                items = self._extract_backpack_items(
                    BeautifulSoup(page_html, "html.parser")
                )

            for item in items:
                item.stale = False

            self._last_backpack = items
            logger.info(
                "Рюкзак: %s предметов (source=%s)",
                len(items),
                source,
            )
            return list(items)

        except Exception as exc:
            logger.error("Ошибка parse_backpack: %s", exc, exc_info=True)
            if self._last_backpack:
                stale_items = [
                    replace(item, stale=True) for item in self._last_backpack
                ]
                return stale_items
            return []

    async def parse_notifications(self, page: Page) -> List[GameNotification]:
        """
        Читает верхнюю плашку системных сообщений и системный чат.

        Фильтрует важные события: урон, получение предмета, ошибки действий.
        """
        await self._human_pause("action")
        notifications: List[GameNotification] = []

        try:
            # 1) Попапы / плашки на основной странице
            banner_notes = await self._parse_banner_notifications(page)
            notifications.extend(banner_notes)

            # 2) Системный чат
            chat_frame = await self._resolve_frame(
                page,
                css_selectors=self._selectors.chat_frame,
                frame_names=self.CHAT_FRAME_NAMES,
                required=False,
            )
            if chat_frame is not None:
                chat_html, _ = await self._get_frame_html(page, chat_frame)
                notifications.extend(
                    self._parse_notifications_from_html(chat_html, source="chat")
                )
            else:
                # Чат может быть встроен в main
                main_frame = await self._resolve_frame(
                    page,
                    css_selectors=self._selectors.main_frame,
                    frame_names=self.USER_FRAME_NAMES,
                    required=False,
                )
                if main_frame is not None:
                    html, _ = await self._get_frame_html(page, main_frame)
                    notifications.extend(
                        self._parse_notifications_from_html(html, source="main")
                    )

            # Дедупликация и отбор важных
            unique = self._dedupe_notifications(notifications)
            important = [n for n in unique if self._is_important_notification(n)]
            # Если важных нет — возвращаем последние info (чтобы модуль не был «пустым»)
            result = important if important else unique[-10:]

            self._last_notifications = result
            logger.info("Уведомления: %s шт. (важных: %s)", len(unique), len(important))
            return list(result)

        except Exception as exc:
            logger.error("Ошибка parse_notifications: %s", exc, exc_info=True)
            if self._last_notifications:
                return [
                    replace(n, stale=True) for n in self._last_notifications
                ]
            return []

    async def parse_all(
        self, page: Page
    ) -> Tuple[PlayerStats, List[BackpackItem], List[GameNotification]]:
        """Парсит статы, рюкзак и уведомления последовательно с паузами."""
        stats = await self.parse_player_stats(page)
        backpack = await self.parse_backpack(page)
        notes = await self.parse_notifications(page)
        return stats, backpack, notes

    # ------------------------------------------------------------------
    # Фреймы и HTML
    # ------------------------------------------------------------------

    async def _resolve_frame(
        self,
        page: Page,
        *,
        css_selectors: str,
        frame_names: Sequence[str],
        extra_css: str = "",
        required: bool = True,
    ) -> Optional[Frame]:
        """Ищет фрейм по CSS и по имени; ждёт появления с таймаутом."""
        combined = css_selectors
        if extra_css:
            combined = f"{css_selectors}, {extra_css}"

        deadline = time.monotonic() + (self._frame_timeout_ms / 1000.0)
        last_error: Optional[BaseException] = None

        while time.monotonic() < deadline:
            try:
                # 1) По имени frame
                for frame in page.frames:
                    name = (frame.name or "").lower()
                    if name in {n.lower() for n in frame_names}:
                        return frame
                    url = (frame.url or "").lower()
                    if any(n.lower() in url for n in frame_names):
                        return frame

                # 2) Через DOM element -> content_frame
                for selector in self._split_selectors(combined):
                    try:
                        handle = await page.query_selector(selector)
                    except PlaywrightError as exc:
                        last_error = exc
                        continue
                    if handle is None:
                        continue
                    content = await handle.content_frame()
                    if content is not None:
                        return content

                # 3) Иногда контент прямо в page (без фреймов)
                if any(
                    (f.name or "").lower() in {n.lower() for n in frame_names}
                    for f in page.frames
                ):
                    pass
            except PlaywrightError as exc:
                last_error = exc
                logger.debug("Фрейм временно недоступен: %s", exc)

            await asyncio.sleep(random.uniform(0.15, 0.4))

        if required:
            logger.warning(
                "Фрейм не найден (%s), last_error=%s",
                frame_names,
                last_error,
            )
        return None

    async def _get_frame_html(
        self, page: Page, frame: Optional[Frame]
    ) -> Tuple[str, str]:
        """Возвращает (html, source_label). При отсутствии фрейма — page.content()."""
        if frame is not None:
            try:
                # content() может бросить, если фрейм перезагружается
                html = await frame.content()
                label = frame.name or frame.url or "frame"
                return html, label
            except PlaywrightError as exc:
                logger.warning("frame.content() недоступен: %s", exc)
                try:
                    html = await frame.evaluate("() => document.documentElement.outerHTML")
                    return str(html or ""), frame.name or "frame-eval"
                except PlaywrightError as exc2:
                    logger.warning("frame.evaluate HTML failed: %s", exc2)

        try:
            return await page.content(), "page"
        except PlaywrightError as exc:
            logger.error("page.content() failed: %s", exc)
            return "", "unavailable"

    # ------------------------------------------------------------------
    # Парсинг статов
    # ------------------------------------------------------------------

    def _parse_bar_pair(
        self,
        soup: BeautifulSoup,
        text: str,
        *,
        css_candidates: Sequence[str],
        label_re: re.Pattern[str],
        width_hints: Sequence[str],
    ) -> Tuple[int, int]:
        """Извлекает current/max из текста, title, aria и width прогресс-бара."""
        # 1) Явная метка в тексте
        match = label_re.search(text)
        if match:
            return int(match.group("cur")), int(match.group("max"))

        # 2) Селекторы элементов
        for selector in self._flatten_css(css_candidates):
            for node in soup.select(selector):
                cur, mx = self._pair_from_element(node)
                if mx > 0 or cur > 0:
                    return cur, mx

        # 3) Прогресс-бары по style=width и соседнему тексту
        for hint in width_hints:
            for node in soup.find_all(
                True,
                attrs={"id": re.compile(hint, re.I)},
            ):
                cur, mx = self._pair_from_element(node)
                if mx > 0 or cur > 0:
                    return cur, mx
            for node in soup.find_all(
                True,
                attrs={"class": re.compile(hint, re.I)},
            ):
                cur, mx = self._pair_from_element(node)
                if mx > 0 or cur > 0:
                    return cur, mx

        # 4) Любая дробь рядом с hint-словом
        for hint in width_hints:
            hint_re = re.compile(
                rf"{re.escape(hint)}.{{0,40}}?(\d+)\s*/\s*(\d+)",
                re.IGNORECASE | re.DOTALL,
            )
            m = hint_re.search(text)
            if m:
                return int(m.group(1)), int(m.group(2))

        return 0, 0

    def _pair_from_element(self, node: Tag) -> Tuple[int, int]:
        candidates = [
            node.get("title"),
            node.get("aria-valuenow"),
            node.get("data-value"),
            node.get("data-hp"),
            node.get("data-mp"),
            node.get("data-current"),
            node.get("alt"),
            node.get_text(" ", strip=True),
        ]
        # aria-valuenow / aria-valuemax
        now = node.get("aria-valuenow")
        mx = node.get("aria-valuemax")
        if now is not None and mx is not None:
            try:
                return int(float(str(now))), int(float(str(mx)))
            except ValueError:
                pass

        for raw in candidates:
            if not raw:
                continue
            parsed = self._fraction_from_text(str(raw))
            if parsed != (0, 0):
                return parsed

        # width: 45% при известном max из data-max
        data_max = node.get("data-max") or node.get("data-maxvalue")
        style = node.get("style") or ""
        width_m = re.search(r"width\s*:\s*([0-9.]+)\s*%", style, re.I)
        if data_max and width_m:
            try:
                max_v = int(float(str(data_max)))
                ratio = float(width_m.group(1)) / 100.0
                return int(round(max_v * ratio)), max_v
            except ValueError:
                pass

        return 0, 0

    @staticmethod
    def _fraction_from_text(text: str) -> Tuple[int, int]:
        match = RE_FRACTION.search(text)
        if not match:
            return 0, 0
        return int(match.group("cur")), int(match.group("max"))

    def _parse_money(self, soup: BeautifulSoup, text: str) -> Tuple[int, int, int]:
        """Парсит золото / серебро / медь регулярками и селекторами."""
        gold = self._parse_int_from_selectors(
            soup,
            (self._selectors.profile_gold, ".gold", "#gold", "[data-currency='gold']"),
        )
        silver = self._parse_int_from_selectors(
            soup,
            (
                self._selectors.profile_silver,
                ".silver",
                "#silver",
                "[data-currency='silver']",
            ),
        )
        copper = self._parse_int_from_selectors(
            soup,
            (
                self._selectors.profile_brass,
                ".brass",
                ".copper",
                "#brass",
                "#copper",
                "[data-currency='brass']",
                "[data-currency='copper']",
            ),
        )

        if gold or silver or copper:
            return gold, silver, copper

        compact = RE_MONEY_COMPACT.search(text) or RE_MONEY_DOTTED.search(text)
        if compact:
            return (
                int(compact.group("gold")),
                int(compact.group("silver")),
                int(compact.group("copper")),
            )

        gold = self._first_regex_int(RE_GOLD, text)
        silver = self._first_regex_int(RE_SILVER, text)
        copper = self._first_regex_int(RE_COPPER, text)
        return gold, silver, copper

    def _parse_location(
        self, soup: BeautifulSoup, text: str
    ) -> Tuple[str, Optional[int], Optional[int]]:
        location = ""
        loc_match = RE_LOCATION.search(text)
        if loc_match:
            location = loc_match.group("name").strip()

        for selector in (
            ".location",
            "#location",
            ".loc-name",
            "[data-role='location']",
            ".room",
            "#room",
        ):
            node = soup.select_one(selector)
            if node:
                location = node.get_text(" ", strip=True) or location
                break

        coord_x: Optional[int] = None
        coord_y: Optional[int] = None
        coords = RE_COORDS.search(text) or RE_COORDS_XY.search(text)
        if coords:
            coord_x = int(coords.group("x"))
            coord_y = int(coords.group("y"))
        else:
            x_node = soup.select_one("[data-x], #coord_x, .coord-x")
            y_node = soup.select_one("[data-y], #coord_y, .coord-y")
            if x_node and y_node:
                try:
                    coord_x = int(
                        x_node.get("data-x") or x_node.get_text(strip=True)
                    )
                    coord_y = int(
                        y_node.get("data-y") or y_node.get_text(strip=True)
                    )
                except ValueError:
                    pass

        return location, coord_x, coord_y

    async def _detect_combat(
        self, page: Page, soup: BeautifulSoup, text: str
    ) -> bool:
        combat_markers = (
            "бой",
            "сражен",
            "in fight",
            "combat",
            "ваш ход",
            "ход противника",
        )
        if any(marker in text.lower() for marker in combat_markers):
            if soup.select_one(self._selectors.combat_panel):
                return True
            if soup.select_one(self._selectors.combat_attack_buttons):
                return True

        # Фрейм боя
        for frame in page.frames:
            name = (frame.name or "").lower()
            if name in {"fight", "combat", "battle"}:
                try:
                    frame_text = await frame.inner_text("body")
                    if frame_text and len(frame_text.strip()) > 20:
                        return True
                except PlaywrightError:
                    return True
                return True

        try:
            combat = await page.query_selector(self._selectors.combat_panel)
            if combat is not None and await combat.is_visible():
                return True
        except PlaywrightError:
            pass

        return False

    # ------------------------------------------------------------------
    # Рюкзак
    # ------------------------------------------------------------------

    def _extract_backpack_items(self, soup: BeautifulSoup) -> List[BackpackItem]:
        items: List[BackpackItem] = []
        seen: set[Tuple[str, int, str]] = set()

        selectors = [
            self._selectors.inventory_item,
            ".belt-item",
            ".pocket",
            ".pocket-item",
            ".item",
            "a[href*='use']",
            "a[href*='item']",
            "[data-item-id]",
            "[data-slot]",
            "td.item",
            "div.item",
        ]

        nodes: List[Tag] = []
        for selector in self._flatten_css(selectors):
            try:
                nodes.extend(soup.select(selector))
            except Exception:
                continue

        # Уникализируем по id объекта
        unique_nodes: List[Tag] = []
        seen_ids: set[int] = set()
        for node in nodes:
            node_id = id(node)
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            unique_nodes.append(node)

        for index, node in enumerate(unique_nodes):
            item = self._item_from_node(node, fallback_slot=index)
            if not item.name:
                continue
            key = (item.name.lower(), item.slot_index, item.action_id)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

        return items

    def _item_from_node(self, node: Tag, *, fallback_slot: int) -> BackpackItem:
        title = (
            node.get("title")
            or node.get("data-title")
            or node.get("alt")
            or ""
        )
        text = node.get_text(" ", strip=True)
        name_node = None
        for sel in self._flatten_css(
            (self._selectors.inventory_item_name, ".item-name", ".name", "b", "strong")
        ):
            name_node = node.select_one(sel)
            if name_node:
                break

        name = ""
        if name_node:
            name = name_node.get_text(" ", strip=True)
        if not name:
            name = (title or text).strip()
        name = re.sub(r"\s+", " ", name)
        # Обрезаем хвост с количеством
        name = RE_ITEM_COUNT.sub("", name).strip(" -–—|")

        count = 1
        charges: Optional[int] = None
        count_node = None
        for sel in self._flatten_css(
            (self._selectors.inventory_item_count, ".item-count", ".count", ".qty")
        ):
            count_node = node.select_one(sel)
            if count_node:
                break
        if count_node:
            count = self._to_int(count_node.get_text(), default=1)
        else:
            blob = f"{title} {text}"
            m = RE_ITEM_COUNT.search(blob)
            if m:
                raw = m.group("a") or m.group("b") or m.group("c") or "1"
                count = int(raw)
                charges = count

        slot_index = fallback_slot
        for attr in ("data-slot", "data-slot-index", "data-pocket", "slot"):
            if node.get(attr) is not None:
                slot_index = self._to_int(node.get(attr), default=fallback_slot)
                break
        else:
            parent = node.parent
            if parent is not None:
                for attr in ("data-slot", "data-slot-index"):
                    if parent.get(attr) is not None:
                        slot_index = self._to_int(
                            parent.get(attr), default=fallback_slot
                        )
                        break
            slot_match = RE_SLOT_INDEX.search(str(node))
            if slot_match:
                slot_index = int(slot_match.group("idx"))

        action_id = (
            node.get("data-item-id")
            or node.get("data-id")
            or node.get("data-action-id")
            or node.get("id")
            or ""
        )
        action_url = node.get("href") or node.get("data-href") or node.get("action") or ""
        onclick = node.get("onclick") or ""
        if not action_url and onclick:
            url_m = re.search(r"['\"]([^'\"]*(?:use|item)[^'\"]*)['\"]", onclick, re.I)
            if url_m:
                action_url = url_m.group(1)
            id_m = RE_ACTION_ID.search(onclick)
            if id_m and not action_id:
                action_id = id_m.group("id")

        if not action_id:
            id_m = RE_ACTION_ID.search(str(node))
            if id_m:
                action_id = id_m.group("id")

        item_type = self._classify_item(name, title or text)

        return BackpackItem(
            name=name[:120],
            count=max(1, count),
            slot_index=slot_index,
            action_url=str(action_url),
            action_id=str(action_id),
            item_type=item_type,
            charges=charges if charges is not None else (count if count > 1 else None),
            tooltip=str(title)[:300],
        )

    async def _extract_backpack_via_playwright(
        self, frame: Frame
    ) -> List[BackpackItem]:
        """Дополнительный проход через locator API Playwright."""
        items: List[BackpackItem] = []
        selectors = [
            self._selectors.inventory_item,
            "[data-item-id]",
            ".belt-item",
            ".pocket-item",
        ]
        for selector in self._flatten_css(selectors):
            try:
                locator = frame.locator(selector)
                count = await locator.count()
            except PlaywrightError:
                continue

            for i in range(min(count, 60)):
                try:
                    loc = locator.nth(i)
                    name = (
                        await loc.get_attribute("title")
                        or await loc.get_attribute("data-title")
                        or await loc.inner_text(timeout=1_000)
                        or ""
                    )
                    name = re.sub(r"\s+", " ", name).strip()
                    name = RE_ITEM_COUNT.sub("", name).strip(" -–—|")
                    if not name:
                        continue

                    action_id = (
                        await loc.get_attribute("data-item-id")
                        or await loc.get_attribute("data-id")
                        or await loc.get_attribute("id")
                        or ""
                    )
                    action_url = (
                        await loc.get_attribute("href")
                        or await loc.get_attribute("data-href")
                        or ""
                    )
                    slot_raw = await loc.get_attribute("data-slot")
                    slot_index = self._to_int(slot_raw, default=i)
                    count_raw = await loc.get_attribute("data-count")
                    item_count = self._to_int(count_raw, default=0)
                    if item_count <= 0:
                        m = RE_ITEM_COUNT.search(
                            (await loc.get_attribute("title")) or name
                        )
                        item_count = (
                            int(m.group("a") or m.group("b") or m.group("c") or "1")
                            if m
                            else 1
                        )

                    items.append(
                        BackpackItem(
                            name=name[:120],
                            count=max(1, item_count),
                            slot_index=slot_index,
                            action_url=action_url,
                            action_id=action_id,
                            item_type=self._classify_item(name, name),
                            charges=item_count if item_count > 1 else None,
                        )
                    )
                except (PlaywrightError, PlaywrightTimeoutError):
                    continue
                except Exception as exc:
                    logger.debug("PW backpack item #%s: %s", i, exc)

        return items

    @staticmethod
    def _merge_backpack_items(
        primary: List[BackpackItem], secondary: List[BackpackItem]
    ) -> List[BackpackItem]:
        merged: List[BackpackItem] = list(primary)
        seen = {
            (i.name.lower(), i.slot_index, i.action_id, i.action_url)
            for i in primary
        }
        for item in secondary:
            key = (item.name.lower(), item.slot_index, item.action_id, item.action_url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    @staticmethod
    def _classify_item(name: str, blob: str) -> str:
        text = f"{name} {blob}".lower()
        if any(w in text for w in ("эликс", "elixir", "банка", "зелье", "potion")):
            return "elixir"
        if any(w in text for w in ("свиток", "scroll", "пергамент")):
            return "scroll"
        if any(w in text for w in ("бомб", "флакон", "настой")):
            return "potion"
        return "other"

    # ------------------------------------------------------------------
    # Уведомления
    # ------------------------------------------------------------------

    async def _parse_banner_notifications(
        self, page: Page
    ) -> List[GameNotification]:
        notes: List[GameNotification] = []
        selectors = [
            self._selectors.notification_popup,
            self._selectors.error_message,
            ".sys-message",
            ".system-message",
            "#sysmsg",
            ".top-message",
            ".info-bar",
            ".msg",
        ]
        for selector in self._flatten_css(selectors):
            try:
                locator = page.locator(selector)
                count = await locator.count()
            except PlaywrightError:
                continue

            for i in range(min(count, 30)):
                try:
                    loc = locator.nth(i)
                    if not await loc.is_visible():
                        continue
                    text = (await loc.inner_text(timeout=1_000)).strip()
                    if not text:
                        continue
                    notes.append(
                        GameNotification(
                            text=re.sub(r"\s+", " ", text)[:500],
                            timestamp=time.time(),
                            type=self._classify_notification(text),
                            source="banner",
                        )
                    )
                except (PlaywrightError, PlaywrightTimeoutError):
                    continue
        return notes

    def _parse_notifications_from_html(
        self, html: str, *, source: str
    ) -> List[GameNotification]:
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        notes: List[GameNotification] = []

        candidates: List[Tag] = []
        for selector in (
            ".chat-line",
            ".sys",
            ".system",
            ".message",
            ".msg",
            "li",
            "p",
            "div.line",
            "tr",
        ):
            candidates.extend(soup.select(selector))

        # Если специфичных узлов мало — режем текст по строкам
        texts: List[str] = []
        for node in candidates:
            t = node.get_text(" ", strip=True)
            if t and 3 <= len(t) <= 400:
                texts.append(t)

        if len(texts) < 3:
            body_text = self._visible_text(soup)
            texts.extend(
                line.strip()
                for line in body_text.splitlines()
                if 3 <= len(line.strip()) <= 400
            )

        now = time.time()
        for text in texts[-80:]:
            notes.append(
                GameNotification(
                    text=re.sub(r"\s+", " ", text)[:500],
                    timestamp=now,
                    type=self._classify_notification(text),
                    source=source,
                )
            )
        return notes

    def _classify_notification(self, text: str) -> NotificationType:
        if RE_ERROR.search(text):
            return NotificationType.ERROR
        if RE_WARNING.search(text) or RE_DAMAGE.search(text):
            return NotificationType.WARNING
        return NotificationType.INFO

    def _is_important_notification(self, note: GameNotification) -> bool:
        text = note.text
        if note.type in {NotificationType.ERROR, NotificationType.WARNING}:
            return True
        if RE_DAMAGE.search(text) or RE_ITEM_GAIN.search(text) or RE_ERROR.search(text):
            return True
        return False

    def _dedupe_notifications(
        self, notes: Sequence[GameNotification]
    ) -> List[GameNotification]:
        result: List[GameNotification] = []
        for note in notes:
            key = f"{note.type.value}|{note.text.lower()}"
            if key in self._seen_notification_keys:
                continue
            self._seen_notification_keys.add(key)
            result.append(note)

        # Ограничиваем память дедупликации
        if len(self._seen_notification_keys) > 2_000:
            self._seen_notification_keys = set(
                list(self._seen_notification_keys)[-800:]
            )
        return result

    # ------------------------------------------------------------------
    # Фоллбеки и утилиты
    # ------------------------------------------------------------------

    def _stale_stats(self, reason: str = "") -> PlayerStats:
        if self._last_stats is not None:
            stale = replace(self._last_stats, stale=True, parsed_at=time.time())
            logger.warning(
                "Возврат stale PlayerStats (%s): HP %s/%s",
                reason,
                stale.hp_current,
                stale.hp_max,
            )
            return stale

        logger.warning("Нет кэша статов, возвращаем дефолт (%s)", reason)
        return PlayerStats(stale=True, parsed_at=time.time(), raw_snippet=reason[:200])

    async def _human_pause(self, kind: str = "action") -> None:
        min_d, max_d = get_delay_range(kind)
        await asyncio.sleep(random.uniform(min_d * 0.25, max_d * 0.35))

    @staticmethod
    def _split_selectors(css: str) -> List[str]:
        return [part.strip() for part in css.split(",") if part.strip()]

    @classmethod
    def _flatten_css(cls, selectors: Sequence[str]) -> List[str]:
        result: List[str] = []
        for item in selectors:
            result.extend(cls._split_selectors(item))
        # уникальные с сохранением порядка
        seen: set[str] = set()
        unique: List[str] = []
        for sel in result:
            if sel not in seen:
                seen.add(sel)
                unique.append(sel)
        return unique

    @staticmethod
    def _visible_text(soup: BeautifulSoup) -> str:
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)

    def _first_text(self, soup: BeautifulSoup, selectors: Sequence[str]) -> str:
        for selector in self._flatten_css(selectors):
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
        return ""

    def _parse_int_from_selectors(
        self, soup: BeautifulSoup, selectors: Sequence[str]
    ) -> int:
        for selector in self._flatten_css(selectors):
            node = soup.select_one(selector)
            if not node:
                continue
            for raw in (
                node.get("data-value"),
                node.get("title"),
                node.get_text(" ", strip=True),
            ):
                if raw is None:
                    continue
                value = self._to_int(raw, default=-1)
                if value >= 0:
                    return value
        return 0

    @staticmethod
    def _first_regex_int(pattern: re.Pattern[str], text: str) -> int:
        match = pattern.search(text)
        if not match:
            return 0
        raw = match.group("value")
        digits = re.sub(r"\s+", "", raw)
        try:
            return int(digits)
        except ValueError:
            return 0

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, int):
            return value
        text = str(value)
        match = re.search(r"-?\d+", text.replace("\xa0", "").replace(" ", ""))
        if not match:
            return default
        try:
            return int(match.group(0))
        except ValueError:
            return default
