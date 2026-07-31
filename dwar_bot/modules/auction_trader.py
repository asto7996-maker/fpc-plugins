"""
Торговая автоматизация аукциона и торгового чата Dwar.

Сканирует лоты биржи, выкупает недооценённые предметы по watch-list,
выставляет свои лоты на продажу и парсит торговый чат на выгодные
предложения. Клики идут через BrowserEngine.human_click (Bezier /
HumanBehavior); между страницами — случайные «человеческие» паузы.
Перед покупкой сверяется баланс PlayerStats (резерв монет не тратится).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from dwar_bot.config import BotConfig, config, get_delay_range
from dwar_bot.core.anti_bot import HumanBehavior
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError
from dwar_bot.modules.stats_parser import BackpackItem, PlayerStats, StatsParser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Денежная система Dwar: 1 золото = 100 серебра, 1 серебро = 100 меди
# ---------------------------------------------------------------------------

COPPER_PER_SILVER = 100
SILVER_PER_GOLD = 100
COPPER_PER_GOLD = SILVER_PER_GOLD * COPPER_PER_SILVER  # 10_000

AUCTION_FRAME_NAMES: Tuple[str, ...] = (
    "main",
    "auction",
    "market",
    "trade",
    "exchange",
    "bazar",
    "shop",
)
CHAT_FRAME_NAMES: Tuple[str, ...] = ("chat", "syschat", "system", "log", "trade")

# Селекторы UI аукциона (фоллбеки; уточняйте в config/selectors.py / DevTools)
AUCTION_PANEL_SELECTORS: Tuple[str, ...] = (
    "#auction",
    ".auction",
    "#market",
    ".market",
    "#exchange",
    "[data-panel='auction']",
    "[data-panel='market']",
    "form[action*='auction']",
    "table.auction",
    "table#lots",
)
AUCTION_ROW_SELECTORS: Tuple[str, ...] = (
    "tr.lot",
    "tr.auction-row",
    "tr[data-lot-id]",
    "tr[data-item-id]",
    ".auction-item",
    ".lot-row",
    "table.auction tbody tr",
    "table#lots tbody tr",
    "#auction_list tr",
)
AUCTION_SEARCH_INPUT: Tuple[str, ...] = (
    "input[name='search']",
    "input[name='item']",
    "input[name='query']",
    "input#auction_search",
    "input.search",
    "#search_item",
    "input[placeholder*='назван']",
)
AUCTION_SEARCH_SUBMIT: Tuple[str, ...] = (
    "button[type='submit']",
    "input[type='submit']",
    "button.search",
    "#auction_search_btn",
    "a[href*='search']",
)
AUCTION_CATEGORY_LINK: Tuple[str, ...] = (
    "a[href*='cat=']",
    "a[href*='category']",
    "[data-category]",
    ".auction-category a",
    "#categories a",
)
AUCTION_BUY_BUTTON: Tuple[str, ...] = (
    "a[href*='buy']",
    "button[data-action='buy']",
    "input[value*='Купить']",
    "a.buy",
    "button.buy",
    ".buyout",
    "[data-buyout]",
    "a[href*='buyout']",
)
AUCTION_NEXT_PAGE: Tuple[str, ...] = (
    "a.next",
    "a[rel='next']",
    ".pagination .next",
    "a[href*='page=']",
    "button.next-page",
    "#next_page",
)
AUCTION_SELL_TAB: Tuple[str, ...] = (
    "a[href*='sell']",
    "a[href*='sale']",
    "[data-tab='sell']",
    ".tab-sell",
    "#sell_tab",
    "a:has-text('Продать')",
    "a:has-text('Выставить')",
)
AUCTION_CONFIRM: Tuple[str, ...] = (
    "button[type='submit']",
    "input[type='submit']",
    "button.confirm",
    "#confirm_buy",
    "#confirm_sell",
    "a.confirm",
    "button:has-text('Подтвердить')",
    "input[value*='Подтверд']",
    "input[value*='Купить']",
    "input[value*='Выставить']",
)
SELL_QTY_INPUT: Tuple[str, ...] = (
    "input[name='count']",
    "input[name='qty']",
    "input[name='quantity']",
    "input#sell_count",
    "#lot_count",
)
SELL_BID_INPUT: Tuple[str, ...] = (
    "input[name='bid']",
    "input[name='start_price']",
    "input[name='min_bid']",
    "#bid_price",
    "#start_price",
)
SELL_BUYOUT_INPUT: Tuple[str, ...] = (
    "input[name='buyout']",
    "input[name='buyout_price']",
    "input[name='price']",
    "#buyout_price",
    "#lot_price",
)
SELL_DURATION_INPUT: Tuple[str, ...] = (
    "select[name='duration']",
    "select[name='hours']",
    "input[name='duration']",
    "#lot_duration",
)
SELL_GOLD_INPUT: Tuple[str, ...] = (
    "input[name='gold']",
    "input[name='g']",
    "#price_gold",
)
SELL_SILVER_INPUT: Tuple[str, ...] = (
    "input[name='silver']",
    "input[name='s']",
    "#price_silver",
)
SELL_COPPER_INPUT: Tuple[str, ...] = (
    "input[name='copper']",
    "input[name='brass']",
    "input[name='c']",
    "#price_copper",
)

RE_MONEY = re.compile(
    r"(?:(?P<gold>\d+)\s*(?:зол(?:ота|ото|\.?)|з\.?|g|gold))?"
    r"(?:\s*(?P<silver>\d+)\s*(?:сер(?:ебра|ебро|\.?)|с\.?|s|silver))?"
    r"(?:\s*(?P<copper>\d+)\s*(?:мед(?:и|ь|\.?)|медяк(?:ов|а)?|м\.?|c|copper|brass))?",
    re.IGNORECASE,
)
RE_MONEY_DOTTED = re.compile(
    r"(?P<gold>\d+)\s*[.зz]\s*(?P<silver>\d+)\s*[.сs]\s*(?P<copper>\d+)",
    re.IGNORECASE,
)
RE_MONEY_COMPACT = re.compile(
    r"(?P<gold>\d+)\s*з\s*(?P<silver>\d+)\s*с\s*(?P<copper>\d+)\s*м?",
    re.IGNORECASE,
)
RE_TIME_LEFT = re.compile(
    r"(?P<h>\d+)\s*(?:ч|h|час)|(?P<m>\d+)\s*(?:м|min|мин)|(?P<s>\d+)\s*(?:с|sec|сек)",
    re.IGNORECASE,
)
RE_LOT_ID = re.compile(
    r"(?:lot[_-]?id|item[_-]?id|id|auction)\s*[=:]\s*['\"]?(?P<id>[\w.-]+)",
    re.IGNORECASE,
)
RE_COUNT = re.compile(
    r"(?:\bx\s*(?P<a>\d+)\b|\((?P<b>\d+)\)|(?:кол(?:ичество)?|шт)\s*[:=]\s*(?P<c>\d+))",
    re.IGNORECASE,
)
RE_TAX = re.compile(
    r"(?:налог|комисси[яи]|сбор|fee|tax|commission)\s*[:=]?\s*"
    r"(?P<body>[\d\s.зscмзолотосеребромедь]+)",
    re.IGNORECASE,
)
RE_BUY_OK = re.compile(
    r"(?:вы\s+купили|лота?\s+куплен|покупка\s+успеш|bought|purchase\s+success)",
    re.IGNORECASE,
)
RE_BUY_FAIL = re.compile(
    r"(?:не\s+хватает|недостаточно|лот\s+снят|уже\s+куплен|ошибк|"
    r"не\s+удалось|cannot|failed|sold\s+out)",
    re.IGNORECASE,
)
RE_SELL_OK = re.compile(
    r"(?:лот\s+выставлен|выставлено\s+на\s+аукцион|продаж[аи]\s+создан|"
    r"лот\s+создан|listed|posted)",
    re.IGNORECASE,
)

# Торговый чат
RE_TRADE_SELL = re.compile(
    r"(?:прода[мют]|продам|продаю|selling|wts|отдам\s+за)",
    re.IGNORECASE,
)
RE_TRADE_BUY = re.compile(
    r"(?:купл[юят]|куплю|скупаю|buying|wtb|возьму)",
    re.IGNORECASE,
)
RE_TRADE_ITEM_HINTS: Tuple[str, ...] = (
    "эликс",
    "зелье",
    "банка",
    "свиток",
    "руда",
    "трава",
    "шкура",
    "кристалл",
    "аметист",
    "железо",
    "медь",
    "серебро",
    "золото",
    "омела",
    "тайна",
    "камень",
    "гриб",
    "древес",
    "ресурсы",
    "рецепт",
)

DEFAULT_RESERVE_GOLD = 1.0  # не тратить ниже этого остатка (в золотых)
DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_BUYS_PER_RUN = 10


# ---------------------------------------------------------------------------
# Dataclass-структуры
# ---------------------------------------------------------------------------


def empty_money() -> Dict[str, int]:
    return {"gold": 0, "silver": 0, "copper": 0}


@dataclass(slots=True)
class AuctionItem:
    """Лот на игровом аукционе / бирже."""

    item_id: str
    name: str
    count: int = 1
    buyout_price: Dict[str, int] = field(default_factory=empty_money)
    bid_price: Dict[str, int] = field(default_factory=empty_money)
    time_left: str = ""
    buy_selector: str = ""
    buy_href: str = ""
    seller: str = ""
    category_id: str = ""
    raw_row: str = ""

    @property
    def buyout_copper(self) -> int:
        return money_dict_to_copper(self.buyout_price)

    @property
    def bid_copper(self) -> int:
        return money_dict_to_copper(self.bid_price)

    @property
    def buyout_gold(self) -> float:
        return self.buyout_copper / float(COPPER_PER_GOLD)

    @property
    def unit_buyout_gold(self) -> float:
        qty = max(1, int(self.count or 1))
        return self.buyout_gold / qty

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TradeOffer:
    """Правило отслеживания покупки / продажи."""

    item_name: str
    target_price: float  # целевая цена за 1 шт. в золотых (дробная)
    max_quantity: int = 1
    category_id: str = ""
    prefer_buyout: bool = True

    def matches(self, item_name: str) -> bool:
        needle = (self.item_name or "").strip().lower()
        hay = (item_name or "").strip().lower()
        if not needle or not hay:
            return False
        return needle in hay or hay in needle

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AuctionTraderError(Exception):
    """Ошибка модуля аукциона."""


# ---------------------------------------------------------------------------
# Деньги
# ---------------------------------------------------------------------------


def money_dict_to_copper(price: Optional[Dict[str, int]]) -> int:
    if not price:
        return 0
    gold = int(price.get("gold", price.get("золото", 0)) or 0)
    silver = int(price.get("silver", price.get("серебро", 0)) or 0)
    copper = int(price.get("copper", price.get("медь", price.get("brass", 0))) or 0)
    return gold * COPPER_PER_GOLD + silver * COPPER_PER_SILVER + copper


def copper_to_money_dict(copper: int) -> Dict[str, int]:
    copper = max(0, int(copper))
    gold, rem = divmod(copper, COPPER_PER_GOLD)
    silver, copper_left = divmod(rem, COPPER_PER_SILVER)
    return {"gold": gold, "silver": silver, "copper": copper_left}


def gold_to_copper(gold: float) -> int:
    return int(round(float(gold) * COPPER_PER_GOLD))


def copper_to_gold(copper: int) -> float:
    return float(copper) / float(COPPER_PER_GOLD)


def parse_money_text(text: str) -> Dict[str, int]:
    """Извлечь {gold, silver, copper} из произвольной строки цены."""
    raw = (text or "").replace("\xa0", " ").strip()
    if not raw:
        return empty_money()

    for pattern in (RE_MONEY_COMPACT, RE_MONEY_DOTTED):
        m = pattern.search(raw)
        if m:
            return {
                "gold": int(m.group("gold") or 0),
                "silver": int(m.group("silver") or 0),
                "copper": int(m.group("copper") or 0),
            }

    # Полный RE_MONEY может частично матчить — берём максимальный осмысленный
    best = empty_money()
    best_total = -1
    for m in RE_MONEY.finditer(raw):
        g = int(m.group("gold") or 0)
        s = int(m.group("silver") or 0)
        c = int(m.group("copper") or 0)
        if g == 0 and s == 0 and c == 0:
            continue
        total = g * COPPER_PER_GOLD + s * COPPER_PER_SILVER + c
        if total > best_total:
            best_total = total
            best = {"gold": g, "silver": s, "copper": c}
    if best_total >= 0:
        return best

    # Только число — считаем медью (осторожный фоллбек)
    digits = re.search(r"(\d[\d\s]*)", raw)
    if digits:
        value = int(re.sub(r"\s+", "", digits.group(1)))
        return copper_to_money_dict(value)
    return empty_money()


def format_money(price: Dict[str, int]) -> str:
    g = int(price.get("gold", 0) or 0)
    s = int(price.get("silver", 0) or 0)
    c = int(price.get("copper", 0) or 0)
    return f"{g}з {s}с {c}м"


# ---------------------------------------------------------------------------
# AuctionTrader
# ---------------------------------------------------------------------------


class AuctionTrader:
    """
    Автоматизация аукциона / биржи и торгового чата.

    Клики выполняются через ``BrowserEngine.human_click`` (кривые Безье
    HumanBehavior). Баланс читается из ``StatsParser.parse_player_stats``.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        browser: Optional[BrowserEngine] = None,
        human: Optional[HumanBehavior] = None,
        stats_parser: Optional[StatsParser] = None,
        reserve_gold: float = DEFAULT_RESERVE_GOLD,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_buys_per_run: int = DEFAULT_MAX_BUYS_PER_RUN,
        page_delay_range: Optional[Tuple[float, float]] = None,
    ) -> None:
        self._config = bot_config or config
        self._browser = browser
        self._human = human or (
            browser.human if browser is not None and hasattr(browser, "human") else HumanBehavior(self._config)
        )
        self._stats_parser = stats_parser or StatsParser(self._config)
        self.reserve_gold = max(0.0, float(reserve_gold))
        self.max_pages = max(1, int(max_pages))
        self.max_buys_per_run = max(1, int(max_buys_per_run))
        self.page_delay_range = page_delay_range or (1.8, 4.5)

        self._last_lots: List[AuctionItem] = []
        self._spent_copper: int = 0
        self._bought_count: int = 0
        self._last_tax: Dict[str, int] = empty_money()

    # ------------------------------------------------------------------
    # Публичное API
    # ------------------------------------------------------------------

    async def scan_auction_category(
        self,
        page: Page,
        category_id: str,
        item_name: Optional[str] = None,
    ) -> List[AuctionItem]:
        """
        Перейти во фрейм аукциона (main), открыть категорию / поиск
        и распарсить таблицу лотов.
        """
        try:
            frame = await self._resolve_auction_frame(page)
            await self._open_auction_panel(page, frame)
            await self._human_page_pause(page, kind="navigation")

            if category_id:
                await self._select_category(page, frame, category_id)
                await self._human_page_pause(page, kind="action")

            if item_name:
                await self._run_search(page, frame, item_name)
                await self._human_page_pause(page, kind="action")

            lots: List[AuctionItem] = []
            seen_ids: set[str] = set()

            for page_idx in range(self.max_pages):
                frame = await self._resolve_auction_frame(page) or frame
                html = await self._frame_html(page, frame)
                soup = BeautifulSoup(html, "html.parser")
                page_lots = self._parse_lots_from_soup(
                    soup, category_id=category_id or ""
                )

                # Playwright-фоллбек по строкам таблицы
                if not page_lots and frame is not None:
                    page_lots = await self._parse_lots_via_playwright(
                        frame, category_id=category_id or ""
                    )

                for lot in page_lots:
                    if item_name and not self._name_matches(lot.name, item_name):
                        continue
                    key = lot.item_id or f"{lot.name}:{lot.buyout_copper}:{lot.count}"
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    lots.append(lot)

                logger.info(
                    "Аукцион cat=%s page=%s: +%s лотов (всего %s)",
                    category_id or "-",
                    page_idx + 1,
                    len(page_lots),
                    len(lots),
                )

                if page_idx + 1 >= self.max_pages:
                    break
                moved = await self._goto_next_page(page, frame)
                if not moved:
                    break
                # Человеческая пауза между страницами прайса
                await self._human_page_pause(page, kind="navigation")
                if random.random() < 0.25:
                    await self._human.random_idle(page, chance=1.0)

            self._last_lots = lots
            return list(lots)

        except Exception as exc:
            logger.error("scan_auction_category: %s", exc, exc_info=True)
            return list(self._last_lots)

    async def buy_underpriced_items(
        self,
        page: Page,
        watch_list: List[TradeOffer],
    ) -> int:
        """
        Сканирует товары из ``watch_list`` и выкупает лоты дешевле
        ``target_price`` (за штуку, в золотых). Возвращает число покупок.
        """
        if not watch_list:
            logger.warning("buy_underpriced_items: пустой watch_list")
            return 0

        bought = 0
        spent = 0

        for offer in watch_list:
            if bought >= self.max_buys_per_run:
                break

            remaining = max(0, int(offer.max_quantity))
            if remaining <= 0:
                continue

            lots = await self.scan_auction_category(
                page,
                category_id=offer.category_id or "",
                item_name=offer.item_name,
            )
            # Сортируем по цене за штуку — сначала самые дешёвые
            candidates = [
                lot
                for lot in lots
                if offer.matches(lot.name)
                and lot.buyout_copper > 0
                and lot.unit_buyout_gold <= float(offer.target_price) + 1e-9
            ]
            candidates.sort(key=lambda x: x.unit_buyout_gold)

            for lot in candidates:
                if bought >= self.max_buys_per_run or remaining <= 0:
                    break

                stats = await self._stats_parser.parse_player_stats(page)
                if not self._can_afford(stats, lot):
                    logger.warning(
                        "Пропуск лота '%s' (%s): баланс %sз (резерв %.2fз)",
                        lot.name,
                        format_money(lot.buyout_price),
                        copper_to_gold(stats.total_copper),
                        self.reserve_gold,
                    )
                    continue

                ok = await self._buy_lot(page, lot)
                await self._human_page_pause(page, kind="action")

                if ok:
                    bought += 1
                    remaining -= max(1, min(remaining, lot.count))
                    spent += lot.buyout_copper
                    self._bought_count += 1
                    self._spent_copper += lot.buyout_copper
                    logger.info(
                        "Выкуплен лот id=%s '%s' x%s за %s (unit=%.4fз ≤ target=%.4fз)",
                        lot.item_id,
                        lot.name,
                        lot.count,
                        format_money(lot.buyout_price),
                        lot.unit_buyout_gold,
                        offer.target_price,
                    )
                else:
                    logger.warning(
                        "Не удалось выкупить лот id=%s '%s'",
                        lot.item_id,
                        lot.name,
                    )

        logger.info(
            "buy_underpriced_items: куплено=%s, затрачено=%s (всего за сессию %s / %s лотов)",
            bought,
            format_money(copper_to_money_dict(spent)),
            format_money(copper_to_money_dict(self._spent_copper)),
            self._bought_count,
        )
        return bought

    async def post_item_for_sale(
        self,
        page: Page,
        item_name: str,
        buyout_price: dict,
        duration_hours: int = 24,
        *,
        quantity: int = 1,
        bid_price: Optional[dict] = None,
    ) -> bool:
        """
        Открыть вкладку продажи, найти предмет в рюкзаке, выставить
        количество / ставку / выкуп и подтвердить лот (с проверкой налога).
        """
        try:
            price = self._normalize_money(buyout_price)
            bid = self._normalize_money(bid_price) if bid_price else self._default_bid(price)
            qty = max(1, int(quantity))
            hours = max(1, int(duration_hours))

            frame = await self._resolve_auction_frame(page)
            await self._open_auction_panel(page, frame)
            await self._human_page_pause(page, kind="navigation")

            if not await self._open_sell_tab(page, frame):
                logger.error("Не удалось открыть вкладку продажи аукциона")
                return False
            await self._human_page_pause(page, kind="action")

            # Обновляем frame после навигации
            frame = await self._resolve_auction_frame(page) or frame

            backpack = await self._stats_parser.parse_backpack(page)
            item = self._find_backpack_item(backpack, item_name)
            if item is None:
                logger.error("В рюкзаке нет предмета '%s' для продажи", item_name)
                return False
            if item.count < qty:
                logger.warning(
                    "В рюкзаке '%s' x%s, запрошено x%s — уменьшаем количество",
                    item.name,
                    item.count,
                    qty,
                )
                qty = max(1, item.count)

            if not await self._select_sell_item(page, frame, item):
                logger.error("Не удалось выбрать '%s' на вкладке продажи", item.name)
                return False
            await self._human_page_pause(page, kind="click")

            await self._fill_sell_form(
                page,
                frame,
                quantity=qty,
                bid=bid,
                buyout=price,
                duration_hours=hours,
            )
            await self._human_page_pause(page, kind="action")

            tax = await self._read_tax(page, frame)
            self._last_tax = tax
            if money_dict_to_copper(tax) > 0:
                logger.info(
                    "Комиссия/налог за выставление '%s': %s",
                    item.name,
                    format_money(tax),
                )

            stats = await self._stats_parser.parse_player_stats(page)
            tax_copper = money_dict_to_copper(tax)
            if tax_copper > 0 and not self._has_reserve_after(stats, tax_copper):
                logger.error(
                    "Недостаточно монет для налога %s (резерв %.2fз) — лот не выставляем",
                    format_money(tax),
                    self.reserve_gold,
                )
                return False

            if not await self._confirm_action(page, frame):
                logger.error("Подтверждение выставления лота не найдено")
                return False

            await self._human_page_pause(page, kind="navigation")
            ok = await self._verify_sell_success(page, frame)
            if ok:
                logger.info(
                    "Лот выставлен: '%s' x%s buyout=%s bid=%s duration=%sч tax=%s",
                    item.name,
                    qty,
                    format_money(price),
                    format_money(bid),
                    hours,
                    format_money(tax),
                )
            else:
                logger.warning(
                    "Выставление '%s' не подтверждено по тексту страницы — проверьте вручную",
                    item.name,
                )
            return ok

        except Exception as exc:
            logger.error("post_item_for_sale: %s", exc, exc_info=True)
            return False

    async def parse_trade_chat(self, page: Page) -> List[dict]:
        """
        Сканирует торговый чат: «продам» / «куплю» + названия ресурсов/эликсиров.
        Возвращает отфильтрованные предложения с оценкой выгоды.
        """
        try:
            frame = await self._resolve_chat_frame(page)
            html = await self._frame_html(page, frame)
            if not html.strip():
                # Фоллбек: main / page
                main = await self._resolve_auction_frame(page)
                html = await self._frame_html(page, main)

            soup = BeautifulSoup(html, "html.parser")
            messages = self._extract_chat_messages(soup)
            offers: List[dict] = []

            for msg in messages:
                parsed = self._parse_trade_message(msg)
                if parsed is None:
                    continue
                offers.append(parsed)

            # Сортировка: сначала выгодные buy (низкая цена), потом sell
            offers.sort(
                key=lambda o: (
                    0 if o.get("side") == "sell" else 1,
                    float(o.get("unit_gold") or 10**9),
                    -float(o.get("score") or 0),
                )
            )
            logger.info("Торговый чат: найдено предложений=%s", len(offers))
            return offers

        except Exception as exc:
            logger.error("parse_trade_chat: %s", exc, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Покупка лота
    # ------------------------------------------------------------------

    async def _buy_lot(self, page: Page, lot: AuctionItem) -> bool:
        frame = await self._resolve_auction_frame(page)
        clicked = False

        selectors: List[str] = []
        if lot.buy_selector:
            selectors.append(lot.buy_selector)
        if lot.buy_href:
            selectors.append(f"a[href='{lot.buy_href}']")
            selectors.append(f"a[href*=\"{lot.buy_href.split('?')[0]}\"]")
        if lot.item_id:
            selectors.extend(
                [
                    f"[data-lot-id='{lot.item_id}'] a[href*='buy']",
                    f"[data-item-id='{lot.item_id}'] a[href*='buy']",
                    f"a[href*='lot_id={lot.item_id}']",
                    f"a[href*='id={lot.item_id}']",
                    f"tr[data-lot-id='{lot.item_id}'] button",
                ]
            )
        selectors.extend(AUCTION_BUY_BUTTON)

        for selector in selectors:
            if await self._human_click_selector(page, selector, frame=frame):
                clicked = True
                break

        if not clicked:
            logger.debug("Кнопка покупки для '%s' не найдена", lot.name)
            return False

        await self._human_page_pause(page, kind="click")
        # Подтверждение модалки, если есть
        frame = await self._resolve_auction_frame(page) or frame
        await self._confirm_action(page, frame)
        await self._human_page_pause(page, kind="action")

        return await self._verify_buy_success(page, frame, lot)

    def _can_afford(self, stats: PlayerStats, lot: AuctionItem) -> bool:
        cost = lot.buyout_copper
        if cost <= 0:
            return False
        return self._has_reserve_after(stats, cost)

    def _has_reserve_after(self, stats: PlayerStats, cost_copper: int) -> bool:
        reserve = gold_to_copper(self.reserve_gold)
        return stats.total_copper - int(cost_copper) >= reserve

    # ------------------------------------------------------------------
    # Парсинг лотов
    # ------------------------------------------------------------------

    def _parse_lots_from_soup(
        self, soup: BeautifulSoup, *, category_id: str
    ) -> List[AuctionItem]:
        rows: List[Tag] = []
        for sel in AUCTION_ROW_SELECTORS:
            try:
                rows.extend(soup.select(sel))
            except Exception:
                continue

        # Фоллбек: любые tr внутри auction-таблицы
        if not rows:
            for table in soup.find_all("table"):
                blob = " ".join(
                    filter(
                        None,
                        [
                            " ".join(table.get("class", []) or []),
                            str(table.get("id") or ""),
                            table.get_text(" ", strip=True)[:200].lower(),
                        ],
                    )
                )
                if any(k in blob for k in ("аукцион", "лот", "auction", "buyout", "выкуп")):
                    rows.extend(table.find_all("tr"))

        lots: List[AuctionItem] = []
        seen: set[str] = set()
        for idx, row in enumerate(rows):
            lot = self._lot_from_row(row, category_id=category_id, fallback_index=idx)
            if not lot.name:
                continue
            key = lot.item_id or f"{lot.name}:{lot.buyout_copper}:{idx}"
            if key in seen:
                continue
            seen.add(key)
            lots.append(lot)
        return lots

    def _lot_from_row(
        self, row: Tag, *, category_id: str, fallback_index: int
    ) -> AuctionItem:
        text = row.get_text(" ", strip=True)
        item_id = (
            str(row.get("data-lot-id") or row.get("data-item-id") or row.get("id") or "")
            .strip()
        )
        if not item_id:
            m = RE_LOT_ID.search(str(row))
            if m:
                item_id = m.group("id")
        if not item_id:
            item_id = f"lot_{fallback_index}_{abs(hash(text)) % 10_000_000}"

        name = self._extract_item_name(row, text)
        count = self._extract_count(row, text)

        buyout = empty_money()
        bid = empty_money()
        # Явные ячейки цены
        buyout_el = row.select_one(
            ".buyout, .buyout-price, .price-buyout, [data-buyout], td.buyout, .instant"
        )
        bid_el = row.select_one(
            ".bid, .bid-price, .price-bid, [data-bid], td.bid, .current-bid"
        )
        if buyout_el is not None:
            buyout = parse_money_text(buyout_el.get_text(" ", strip=True))
        if bid_el is not None:
            bid = parse_money_text(bid_el.get_text(" ", strip=True))

        if money_dict_to_copper(buyout) == 0 and money_dict_to_copper(bid) == 0:
            # Две денежные строки в тексте строки
            moneys = list(RE_MONEY_COMPACT.finditer(text)) + list(
                RE_MONEY_DOTTED.finditer(text)
            )
            parsed = [parse_money_text(m.group(0)) for m in moneys]
            parsed = [p for p in parsed if money_dict_to_copper(p) > 0]
            if len(parsed) >= 2:
                bid, buyout = parsed[0], parsed[1]
            elif len(parsed) == 1:
                buyout = parsed[0]
            else:
                buyout = parse_money_text(text)

        time_left = ""
        time_el = row.select_one(".time, .time-left, .expires, td.time, [data-time]")
        if time_el is not None:
            time_left = time_el.get_text(" ", strip=True)
        elif RE_TIME_LEFT.search(text):
            time_left = " ".join(m.group(0) for m in RE_TIME_LEFT.finditer(text))

        buy_href = ""
        buy_selector = ""
        for a in row.find_all(["a", "button", "input"]):
            href = str(a.get("href") or "")
            label = " ".join(
                filter(
                    None,
                    [
                        a.get_text(" ", strip=True),
                        str(a.get("value") or ""),
                        str(a.get("title") or ""),
                        " ".join(a.get("class", []) or []),
                    ],
                )
            ).lower()
            if "buy" in href.lower() or "buyout" in href.lower() or "куп" in label:
                buy_href = href
                if href:
                    buy_selector = f"a[href='{href}']"
                elif a.get("id"):
                    buy_selector = f"#{a.get('id')}"
                break

        seller_el = row.select_one(".seller, .nick, td.seller, [data-seller]")
        seller = seller_el.get_text(" ", strip=True) if seller_el else ""

        return AuctionItem(
            item_id=item_id,
            name=name,
            count=count,
            buyout_price=buyout,
            bid_price=bid,
            time_left=time_left,
            buy_selector=buy_selector,
            buy_href=buy_href,
            seller=seller,
            category_id=category_id,
            raw_row=text[:300],
        )

    def _extract_item_name(self, row: Tag, text: str) -> str:
        for sel in (
            ".item-name",
            ".lot-name",
            ".name",
            "a[href*='item']",
            "td.name",
            "b",
            "strong",
        ):
            el = row.select_one(sel)
            if el is not None:
                name = el.get_text(" ", strip=True)
                if name and not RE_MONEY_COMPACT.search(name):
                    return name[:120]
        # Первая «словесная» часть до цены
        cleaned = RE_MONEY_COMPACT.sub("", text)
        cleaned = RE_MONEY_DOTTED.sub("", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|")
        return cleaned[:80]

    def _extract_count(self, row: Tag, text: str) -> int:
        for attr in ("data-count", "data-qty", "data-quantity"):
            raw = row.get(attr)
            if raw and str(raw).isdigit():
                return max(1, int(raw))
        m = RE_COUNT.search(text)
        if m:
            for g in ("a", "b", "c"):
                if m.group(g):
                    return max(1, int(m.group(g)))
        return 1

    async def _parse_lots_via_playwright(
        self, frame: Frame, *, category_id: str
    ) -> List[AuctionItem]:
        lots: List[AuctionItem] = []
        for sel in AUCTION_ROW_SELECTORS:
            try:
                loc = frame.locator(sel)
                count = await loc.count()
            except PlaywrightError:
                continue
            if count <= 0:
                continue
            for i in range(min(count, 80)):
                try:
                    row = loc.nth(i)
                    html = await row.inner_html()
                    text = await row.inner_text()
                except PlaywrightError:
                    continue
                soup = BeautifulSoup(f"<tr>{html}</tr>", "html.parser")
                tr = soup.find("tr")
                if tr is None:
                    continue
                # Прокидываем текст, если inner пуст
                if not tr.get_text(strip=True) and text:
                    tr.string = text
                lot = self._lot_from_row(tr, category_id=category_id, fallback_index=i)
                if lot.name:
                    lots.append(lot)
            if lots:
                break
        return lots

    # ------------------------------------------------------------------
    # Навигация по UI
    # ------------------------------------------------------------------

    async def _open_auction_panel(
        self, page: Page, frame: Optional[Frame]
    ) -> bool:
        owner: Any = frame or page
        # Уже на странице аукциона?
        for sel in AUCTION_PANEL_SELECTORS + AUCTION_ROW_SELECTORS:
            try:
                handle = await owner.query_selector(sel)
                if handle is not None:
                    return True
            except PlaywrightError:
                continue

        # Ссылки входа
        entry = (
            "a[href*='auction']",
            "a[href*='market']",
            "a[href*='exchange']",
            "a[href*='trade']",
            "[data-panel='auction']",
            "text=/аукцион|биржа|рынок/i",
        )
        for sel in entry:
            if await self._human_click_selector(page, sel, frame=frame):
                await self._human_page_pause(page, kind="navigation")
                return True
        logger.debug("Панель аукциона не найдена — продолжаем парсить текущий DOM")
        return False

    async def _select_category(
        self, page: Page, frame: Optional[Frame], category_id: str
    ) -> bool:
        cat = (category_id or "").strip()
        if not cat:
            return False
        selectors = [
            f"a[href*='cat={cat}']",
            f"a[href*='category={cat}']",
            f"a[href*='category_id={cat}']",
            f"[data-category='{cat}']",
            f"[data-cat='{cat}']",
            f"#cat_{cat}",
            f"a:has-text('{cat}')",
        ]
        selectors.extend(AUCTION_CATEGORY_LINK)
        for sel in selectors:
            if await self._human_click_selector(page, sel, frame=frame):
                logger.debug("Категория аукциона выбрана: %s", cat)
                return True
        logger.debug("Категория '%s' не найдена кликом — поиск по имени", cat)
        return False

    async def _run_search(
        self, page: Page, frame: Optional[Frame], item_name: str
    ) -> bool:
        owner: Any = frame or page
        filled = False
        for sel in AUCTION_SEARCH_INPUT:
            try:
                handle = await owner.query_selector(sel)
                if handle is None:
                    continue
                await self._human_type_into(page, sel, item_name, frame=frame)
                filled = True
                break
            except Exception as exc:
                logger.debug("search input %s: %s", sel, exc)
        if not filled:
            logger.debug("Поле поиска аукциона не найдено")
            return False

        for sel in AUCTION_SEARCH_SUBMIT:
            if await self._human_click_selector(page, sel, frame=frame):
                return True
        # Enter
        try:
            await page.keyboard.press("Enter")
            return True
        except PlaywrightError:
            return filled

    async def _goto_next_page(
        self, page: Page, frame: Optional[Frame]
    ) -> bool:
        for sel in AUCTION_NEXT_PAGE:
            if await self._human_click_selector(page, sel, frame=frame):
                return True
        return False

    async def _open_sell_tab(
        self, page: Page, frame: Optional[Frame]
    ) -> bool:
        for sel in AUCTION_SELL_TAB:
            if await self._human_click_selector(page, sel, frame=frame):
                return True
        return False

    async def _select_sell_item(
        self,
        page: Page,
        frame: Optional[Frame],
        item: BackpackItem,
    ) -> bool:
        name = item.name
        selectors: List[str] = []
        if item.action_id:
            selectors.append(f"[data-item-id='{item.action_id}']")
            selectors.append(f"[data-id='{item.action_id}']")
        if item.action_url:
            selectors.append(f"a[href='{item.action_url}']")
        safe = name.replace("'", "\\'")
        selectors.extend(
            [
                f"a:has-text('{safe}')",
                f"label:has-text('{safe}')",
                f"[title*='{safe}' i]",
                f".inv-item:has-text('{safe}')",
                f".inventory-item:has-text('{safe}')",
            ]
        )
        for sel in selectors:
            if await self._human_click_selector(page, sel, frame=frame):
                return True
        # Фоллбек: клик по слоту рюкзака
        if item.slot_index >= 0:
            nth = (
                f"__nth__:{item.slot_index}:"
                f"{self._config.selectors.inventory_item}, .belt-item, .pocket-item"
            )
            return await self._human_click_selector(page, nth, frame=frame)
        return False

    async def _fill_sell_form(
        self,
        page: Page,
        frame: Optional[Frame],
        *,
        quantity: int,
        bid: Dict[str, int],
        buyout: Dict[str, int],
        duration_hours: int,
    ) -> None:
        await self._fill_first(page, frame, SELL_QTY_INPUT, str(quantity))

        # Раздельные поля валют или одно поле
        gold_filled = await self._fill_first(
            page, frame, SELL_GOLD_INPUT, str(buyout.get("gold", 0))
        )
        if gold_filled:
            await self._fill_first(
                page, frame, SELL_SILVER_INPUT, str(buyout.get("silver", 0))
            )
            await self._fill_first(
                page, frame, SELL_COPPER_INPUT, str(buyout.get("copper", 0))
            )
        else:
            await self._fill_first(
                page, frame, SELL_BUYOUT_INPUT, format_money(buyout)
            )
            # Иногда отдельное поле ожидает только число (медь)
            await self._fill_first(
                page,
                frame,
                ("input[name='buyout_copper']", "input[name='price_copper']"),
                str(money_dict_to_copper(buyout)),
            )

        bid_filled = await self._fill_first(
            page, frame, SELL_BID_INPUT, format_money(bid)
        )
        if not bid_filled:
            await self._fill_first(
                page,
                frame,
                ("input[name='bid_gold']",),
                str(bid.get("gold", 0)),
            )

        # Длительность
        owner: Any = frame or page
        for sel in SELL_DURATION_INPUT:
            try:
                handle = await owner.query_selector(sel)
                if handle is None:
                    continue
                tag = (await handle.evaluate("el => el.tagName")).lower()
                if tag == "select":
                    # Пробуем value=hours или label
                    try:
                        await handle.select_option(value=str(duration_hours))
                    except PlaywrightError:
                        await handle.select_option(label=re.compile(str(duration_hours)))
                else:
                    await self._human_type_into(
                        page, sel, str(duration_hours), frame=frame
                    )
                break
            except Exception as exc:
                logger.debug("duration field %s: %s", sel, exc)

    async def _fill_first(
        self,
        page: Page,
        frame: Optional[Frame],
        selectors: Sequence[str],
        value: str,
    ) -> bool:
        owner: Any = frame or page
        for sel in selectors:
            try:
                handle = await owner.query_selector(sel)
                if handle is None:
                    continue
                await self._human_type_into(page, sel, value, frame=frame)
                return True
            except Exception as exc:
                logger.debug("fill %s: %s", sel, exc)
        return False

    async def _read_tax(
        self, page: Page, frame: Optional[Frame]
    ) -> Dict[str, int]:
        html = await self._frame_html(page, frame)
        m = RE_TAX.search(html)
        if m:
            return parse_money_text(m.group("body"))
        soup = BeautifulSoup(html, "html.parser")
        for sel in (".tax", ".fee", ".commission", "#tax", "[data-tax]"):
            el = soup.select_one(sel)
            if el is not None:
                return parse_money_text(el.get_text(" ", strip=True))
        return empty_money()

    async def _confirm_action(
        self, page: Page, frame: Optional[Frame]
    ) -> bool:
        for sel in AUCTION_CONFIRM:
            if await self._human_click_selector(page, sel, frame=frame):
                return True
        return False

    async def _verify_buy_success(
        self, page: Page, frame: Optional[Frame], lot: AuctionItem
    ) -> bool:
        html = await self._frame_html(page, frame)
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if RE_BUY_FAIL.search(text):
            return False
        if RE_BUY_OK.search(text):
            return True
        # Эвристика: лот исчез из списка
        if lot.item_id and lot.item_id not in html:
            return True
        # Мягкий успех: не увидели явной ошибки после клика
        logger.debug("Нет явного подтверждения покупки '%s' — считаем условным OK", lot.name)
        return True

    async def _verify_sell_success(
        self, page: Page, frame: Optional[Frame]
    ) -> bool:
        html = await self._frame_html(page, frame)
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if RE_SELL_OK.search(text):
            return True
        if RE_BUY_FAIL.search(text):
            return False
        # Нет явной ошибки — частично успешный сценарий
        return "ошиб" not in text.lower()

    # ------------------------------------------------------------------
    # Торговый чат
    # ------------------------------------------------------------------

    def _extract_chat_messages(self, soup: BeautifulSoup) -> List[str]:
        messages: List[str] = []
        for sel in (
            ".chat-message",
            ".message",
            ".msg",
            "#chat .line",
            "#chat div",
            "#chat p",
            ".trade-chat .line",
            "li.message",
        ):
            try:
                for el in soup.select(sel):
                    text = el.get_text(" ", strip=True)
                    if text and len(text) >= 4:
                        messages.append(text)
            except Exception:
                continue
        if not messages:
            # Фоллбек: строки <br>-размеченного чата
            body = soup.get_text("\n", strip=True)
            for line in body.splitlines():
                line = line.strip()
                if len(line) >= 4:
                    messages.append(line)
        # Уникальные, порядок сохранён
        seen: set[str] = set()
        unique: List[str] = []
        for msg in messages:
            key = msg.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(msg)
        return unique[-200:]

    def _parse_trade_message(self, text: str) -> Optional[dict]:
        low = text.lower()
        side = ""
        if RE_TRADE_SELL.search(text):
            side = "sell"
        elif RE_TRADE_BUY.search(text):
            side = "buy"
        else:
            return None

        hint = next((h for h in RE_TRADE_ITEM_HINTS if h in low), "")
        if not hint and not re.search(r"[а-яa-z]{3,}", low):
            return None

        price = parse_money_text(text)
        price_copper = money_dict_to_copper(price)
        count = 1
        m_count = RE_COUNT.search(text)
        if m_count:
            for g in ("a", "b", "c"):
                if m_count.group(g):
                    count = max(1, int(m_count.group(g)))
                    break

        # Имя предмета — эвристика вокруг ключевого слова
        item_name = hint
        for pattern in (
            rf"(?:продам|куплю|продаю|скупаю)\s+(?P<n>[^\d,]{{3,60}}?)(?:\s+\d|\s+за|$)",
            rf"(?P<n>[А-Яа-яA-Za-z][А-Яа-яA-Za-z\-\s]{{2,40}})\s+за\s+\d",
        ):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                candidate = m.group("n").strip(" .:;-—")
                if len(candidate) >= 3:
                    item_name = candidate[:80]
                    break

        unit_gold = (
            copper_to_gold(price_copper) / float(count) if price_copper > 0 else 0.0
        )
        score = self._score_trade_offer(side, unit_gold, hint=hint, text=low)
        if score <= 0 and not hint:
            return None

        return {
            "side": side,
            "item_name": item_name,
            "hint": hint,
            "count": count,
            "price": price,
            "unit_gold": round(unit_gold, 4),
            "score": score,
            "text": text[:240],
            "profitable": score >= 0.55,
        }

    def _score_trade_offer(
        self, side: str, unit_gold: float, *, hint: str, text: str
    ) -> float:
        """
        Грубая оценка выгоды:
          - sell с низкой ценой за шт. → выше score (можно перекупить/сравнить);
          - buy с высокой ценой → выше score (можно продать в чат).
        """
        score = 0.3 if hint else 0.1
        if "эликс" in text or "зелье" in text:
            score += 0.15
        if unit_gold <= 0:
            return round(score * 0.5, 3)

        if side == "sell":
            # Чем дешевле предложение продавца — тем интереснее
            if unit_gold <= 0.5:
                score += 0.45
            elif unit_gold <= 2.0:
                score += 0.3
            elif unit_gold <= 5.0:
                score += 0.15
            else:
                score += 0.05
        else:  # buy
            if unit_gold >= 5.0:
                score += 0.4
            elif unit_gold >= 2.0:
                score += 0.25
            elif unit_gold >= 0.5:
                score += 0.1
        return round(min(1.0, score), 3)

    # ------------------------------------------------------------------
    # Human-like клики / паузы
    # ------------------------------------------------------------------

    async def _human_page_pause(self, page: Page, *, kind: str = "action") -> None:
        """Случайная задержка; для navigation — диапазон «просмотра прайса»."""
        if kind == "navigation":
            delay = random.uniform(*self.page_delay_range)
        else:
            try:
                lo, hi = get_delay_range(kind if kind in {"click", "action", "combat"} else "action")
            except KeyError:
                lo, hi = 0.6, 1.8
            delay = random.uniform(lo, hi)
        await asyncio.sleep(delay)
        if kind == "navigation" and random.random() < 0.2:
            try:
                await self._human.random_idle(page, chance=0.5)
            except Exception:
                await asyncio.sleep(random.uniform(0.4, 1.2))

    async def _human_click_selector(
        self,
        page: Page,
        selector: str,
        *,
        frame: Optional[Frame] = None,
    ) -> bool:
        if not selector:
            return False
        owner: Any = frame or page

        # Специальный nth-селектор
        if selector.startswith("__nth__:"):
            try:
                _, idx_s, rest = selector.split(":", 2)
                idx = int(idx_s)
                loc = owner.locator(rest)
                if await loc.count() <= idx:
                    return False
                handle = await loc.nth(idx).element_handle()
                if handle is None:
                    return False
                return await self._human_click_target(page, handle, frame=frame)
            except Exception as exc:
                logger.debug("nth click %s: %s", selector[:80], exc)
                return False

        # Playwright-only локаторы
        if ":has-text" in selector or selector.startswith("text=") or " i]" in selector:
            try:
                loc = owner.locator(selector)
                if await loc.count() == 0:
                    return False
                handle = await loc.first.element_handle()
                if handle is None:
                    return False
                return await self._human_click_target(page, handle, frame=frame)
            except Exception as exc:
                logger.debug("locator click %s: %s", selector[:80], exc)
                return False

        try:
            handle = await owner.query_selector(selector)
            if handle is None:
                return False
            if self._browser is not None:
                await self._browser.human_click(selector, page=page, frame=frame)
                return True
            # Без BrowserEngine: Bezier + mouse через HumanBehavior
            await self._human.bezier_mouse_move(
                page, selector, frame=frame, timeout_ms=5_000
            )
            await asyncio.sleep(random.uniform(0.08, 0.25))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.04, 0.1))
            await page.mouse.up()
            return True
        except Exception as exc:
            logger.debug("human_click %s: %s", selector[:80], exc)
            return False

    async def _human_click_target(
        self,
        page: Page,
        target: Any,
        *,
        frame: Optional[Frame] = None,
    ) -> bool:
        try:
            if self._browser is not None:
                await self._browser.human_click(target, page=page, frame=frame)
                return True
            box = await target.bounding_box()
            if box is None:
                await target.click(timeout=5_000)
                return True
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.08, 0.22))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.04, 0.1))
            await page.mouse.up()
            return True
        except (PlaywrightError, BrowserEngineError) as exc:
            logger.debug("human_click target: %s", exc)
            return False

    async def _human_type_into(
        self,
        page: Page,
        selector: str,
        text: str,
        *,
        frame: Optional[Frame] = None,
    ) -> None:
        if self._browser is not None and hasattr(self._browser, "human_type"):
            await self._browser.human_type(selector, text, page=page, frame=frame)
            return
        await self._human.human_type(page, selector, text, frame=frame)

    # ------------------------------------------------------------------
    # Фреймы / HTML
    # ------------------------------------------------------------------

    async def _resolve_auction_frame(self, page: Page) -> Optional[Frame]:
        return await self._resolve_frame(
            page,
            css=self._config.selectors.main_frame,
            names=AUCTION_FRAME_NAMES,
        )

    async def _resolve_chat_frame(self, page: Page) -> Optional[Frame]:
        return await self._resolve_frame(
            page,
            css=self._config.selectors.chat_frame,
            names=CHAT_FRAME_NAMES,
        )

    async def _resolve_frame(
        self,
        page: Page,
        *,
        css: str,
        names: Sequence[str],
    ) -> Optional[Frame]:
        wanted = {n.lower() for n in names}
        try:
            for fr in page.frames:
                fname = (fr.name or "").lower()
                if fname in wanted:
                    return fr
                url = (fr.url or "").lower()
                if any(n in url for n in wanted):
                    return fr
        except PlaywrightError:
            pass

        if self._browser is not None:
            try:
                fr = await self._browser.get_frame(css)
                if fr is not None:
                    return fr
            except Exception:
                pass

        # CSS к <frame> в parent — Playwright сам отдаёт content_frame через locator
        try:
            for part in css.split(","):
                part = part.strip()
                if not part:
                    continue
                loc = page.locator(part).first
                if await loc.count() == 0:
                    continue
                try:
                    handle = await loc.element_handle()
                    if handle is None:
                        continue
                    content = await handle.content_frame()
                    if content is not None:
                        return content
                except PlaywrightError:
                    continue
        except PlaywrightError:
            pass
        return page.main_frame

    async def _frame_html(
        self, page: Page, frame: Optional[Frame]
    ) -> str:
        target: Union[Page, Frame] = frame or page
        try:
            return await target.content()
        except PlaywrightError as exc:
            logger.debug("frame content: %s", exc)
            try:
                return await page.content()
            except PlaywrightError:
                return ""

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    @staticmethod
    def _name_matches(haystack: str, needle: str) -> bool:
        h = (haystack or "").strip().lower()
        n = (needle or "").strip().lower()
        if not n:
            return True
        return n in h or h in n

    @staticmethod
    def _normalize_money(price: Optional[dict]) -> Dict[str, int]:
        if not price:
            return empty_money()
        if isinstance(price, dict):
            return {
                "gold": int(price.get("gold", price.get("золото", 0)) or 0),
                "silver": int(price.get("silver", price.get("серебро", 0)) or 0),
                "copper": int(
                    price.get("copper", price.get("медь", price.get("brass", 0))) or 0
                ),
            }
        return parse_money_text(str(price))

    @staticmethod
    def _default_bid(buyout: Dict[str, int]) -> Dict[str, int]:
        """Стартовая ставка ≈ 70% от выкупа."""
        copper = money_dict_to_copper(buyout)
        return copper_to_money_dict(max(1, int(copper * 0.7)))

    @staticmethod
    def _find_backpack_item(
        items: Sequence[BackpackItem], name: str
    ) -> Optional[BackpackItem]:
        needle = (name or "").strip().lower()
        if not needle:
            return None
        for item in items:
            if needle in (item.name or "").lower():
                return item
        return None

    @property
    def session_stats(self) -> Dict[str, Any]:
        return {
            "bought_count": self._bought_count,
            "spent": copper_to_money_dict(self._spent_copper),
            "spent_gold": round(copper_to_gold(self._spent_copper), 4),
            "last_tax": dict(self._last_tax),
            "last_lots": len(self._last_lots),
            "reserve_gold": self.reserve_gold,
        }


__all__ = [
    "AuctionItem",
    "TradeOffer",
    "AuctionTrader",
    "AuctionTraderError",
    "money_dict_to_copper",
    "copper_to_money_dict",
    "gold_to_copper",
    "copper_to_gold",
    "parse_money_text",
    "format_money",
    "empty_money",
]
