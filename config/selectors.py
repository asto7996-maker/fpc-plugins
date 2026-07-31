"""
config/selectors.py — CSS / XPath селекторы и структура фреймов Dwar
====================================================================

Игра: «Легенда: Наследие Драконов» (w1.dwar.ru / w2.dwar.ru).

Этот файл — единая точка тонкой настройки DOM-селекторов. Модули бота
(combat / backpack / quests / farm) читают значения отсюда. После обновления
клиента игры селекторы часто ломаются — правьте ТОЛЬКО этот файл.

---------------------------------------------------------------------------
КАК ПРОВЕРИТЬ И ОБНОВИТЬ УСТАРЕВШИЙ СЕЛЕКТОР (DevTools / F12)
---------------------------------------------------------------------------

1. Откройте игру в браузере, войдите в аккаунт, дождитесь загрузки frameset.
2. DevTools → Elements / Inspector. Найдите нужный элемент (кнопка удара,
   полоска HP, слот пояса, ссылка локации и т.д.).
3. ПКМ по элементу → Copy → Copy selector (CSS) или Copy → Copy XPath.
4. Проверьте в Console, что селектор живой:

       // CSS
       document.querySelector("#hp_val")
       document.querySelectorAll(".attack-btn")

       // внутри iframe / frame (если document — родительский):
       const f = window.frames["main"]   // или ["user"], ["chat"], ["fight"]
       f.document.querySelector("#hp_val")

       // XPath (возвращает Node):
       document.evaluate(
         '//*[@id="hp_val"]',
         document,
         null,
         XPathResult.FIRST_ORDERED_NODE_TYPE,
         null
       ).singleNodeValue

5. Вставьте новый CSS/XPath в соответствующее поле dataclass ниже
   (CombatSelectors.hp_current, FrameSelectors.main_name и т.д.).
6. Сохраните файл и перезапустите бота / контейнер.
7. Прогон валидатора (из кода бота):

       from config.selectors import validate_selectors
       report = await validate_selectors(page)
       print(report)   # { "combat.strike_top": True, ... }

Подсказка: у Dwar классический frameset. Селекторы часто живут НЕ в top-
document, а внутри frame[name=main|user|chat|fight]. Playwright-валидатор
ниже обходит все фреймы автоматически.

Зарезервированные константы имён фреймов (заполните после съёма DevTools):
  MAIN_FRAME_NAME, CHAT_FRAME_NAME, USER_FRAME_NAME
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

try:
    from playwright.async_api import Frame, Page
except ImportError:  # pragma: no cover — для статической проверки без Playwright
    Page = Any  # type: ignore[misc, assignment]
    Frame = Any  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# ===========================================================================
# 1. ИМЕНА / ID ФРЕЙМОВ (заполните через DevTools → Elements → <frame>/<iframe>)
# ===========================================================================

# Главный игровой фрейм (карта, локация, объекты)
MAIN_FRAME_NAME: str = "main"
# Фрейм чата и системных логов
CHAT_FRAME_NAME: str = "chat"
# Фрейм характеристик, HP и рюкзака / пояса
USER_FRAME_NAME: str = "user"
# Боевой фрейм (удары, лог боя)
COMBAT_FRAME_NAME: str = "fight"
# Навигация / меню
NAV_FRAME_NAME: str = "menu"

# Альтернативные имена (фоллбек, если клиент переименовал frame)
MAIN_FRAME_ALIASES: Tuple[str, ...] = (
    MAIN_FRAME_NAME,
    "main_frame",
    "game",
    "location",
    "map",
    "pers",
)
CHAT_FRAME_ALIASES: Tuple[str, ...] = (
    CHAT_FRAME_NAME,
    "syschat",
    "system",
    "log",
    "chat_frame",
)
USER_FRAME_ALIASES: Tuple[str, ...] = (
    USER_FRAME_NAME,
    "pers",
    "person",
    "char",
    "character",
    "stats",
    "user_frame",
)
COMBAT_FRAME_ALIASES: Tuple[str, ...] = (
    COMBAT_FRAME_NAME,
    "combat",
    "battle",
    "fight_frame",
    "combat_frame",
)
NAV_FRAME_ALIASES: Tuple[str, ...] = (
    NAV_FRAME_NAME,
    "nav",
    "map",
    "location",
    "menu_frame",
)


# ===========================================================================
# 2. DATACLASSES СЕЛЕКТОРОВ
# ===========================================================================


@dataclass(frozen=True, slots=True)
class FrameSelectors:
    """Имена фреймов и CSS к самим <frame>/<iframe> элементам."""

    main_name: str = MAIN_FRAME_NAME
    chat_name: str = CHAT_FRAME_NAME
    user_name: str = USER_FRAME_NAME
    combat_name: str = COMBAT_FRAME_NAME
    nav_name: str = NAV_FRAME_NAME

    # CSS к тегам frame/iframe в родительском document
    main_css: str = (
        "frame[name='main'], iframe[name='main'], "
        "#main_frame, iframe#main, frame#main, "
        "iframe[src*='game'], iframe[src*='main']"
    )
    chat_css: str = (
        "frame[name='chat'], iframe[name='chat'], "
        "#chat_frame, iframe[src*='chat']"
    )
    user_css: str = (
        "frame[name='user'], iframe[name='user'], "
        "frame[name='pers'], iframe[name='pers'], "
        "#user_frame, #pers_frame, iframe[src*='user'], iframe[src*='pers']"
    )
    combat_css: str = (
        "frame[name='fight'], iframe[name='fight'], "
        "frame[name='combat'], iframe[name='combat'], "
        "#fight_frame, #combat_frame, iframe[src*='fight']"
    )
    nav_css: str = (
        "frame[name='menu'], iframe[name='menu'], "
        "frame[name='nav'], iframe[name='nav'], "
        "#menu_frame, #nav_frame, iframe[src*='menu']"
    )
    frameset_css: str = "frameset, #game_frameset, frameset#main"
    game_iframe_css: str = (
        "iframe#game_frame, iframe[src*='game'], iframe.game-frame"
    )

    def aliases_for(self, role: str) -> Tuple[str, ...]:
        mapping = {
            "main": MAIN_FRAME_ALIASES,
            "chat": CHAT_FRAME_ALIASES,
            "user": USER_FRAME_ALIASES,
            "combat": COMBAT_FRAME_ALIASES,
            "nav": NAV_FRAME_ALIASES,
        }
        if role not in mapping:
            raise KeyError(f"Неизвестная роль фрейма: {role}")
        return mapping[role]


@dataclass(frozen=True, slots=True)
class CombatSelectors:
    """
    Боевой DOM: удары Верх / Сердце / Низ, HP, лог, панель боя.

    Заполните точные CSS/XPath после съёма через DevTools.
    Поддерживаются оба формата: CSS («#id .cls») и XPath («//*[@id=…]»).
    XPath должен начинаться с «/» или «(».
    """

    # Панель / контейнер боя
    panel: str = "#fight, .combat, [data-panel='combat'], #battle, .fight-panel"
    log: str = "#fight_log, .fight-log, .combat-log, #battle_log, #flog"
    turn_indicator: str = (
        ".turn-indicator, .your-turn, [data-turn='player'], "
        "#your_turn, .fight-turn"
    )

    # Кнопки ударов (Верх / Сердце / Низ)
    strike_top: str = (
        "[data-strike='top'], [data-hit='top'], [data-zone='top'], "
        "button.top, a.top, #top, .strike-top, .hit-top, "
        "input[value*='Верх'], a[title*='Верх'], button[title*='Верх']"
    )
    strike_center: str = (
        "[data-strike='center'], [data-hit='center'], [data-zone='center'], "
        "[data-strike='heart'], [data-hit='heart'], "
        "button.center, a.center, #center, #heart, .strike-center, .hit-center, "
        "input[value*='Сердце'], a[title*='Сердце'], button[title*='Сердце']"
    )
    strike_bottom: str = (
        "[data-strike='bottom'], [data-hit='bottom'], [data-zone='bottom'], "
        "button.bottom, a.bottom, #bottom, .strike-bottom, .hit-bottom, "
        "input[value*='Низ'], a[title*='Низ'], button[title*='Низ']"
    )
    # Общий набор кнопок атаки (фоллбек, если зональные селекторы пусты)
    attack_buttons: str = (
        ".attack-btn, button[data-action='attack'], .fight-actions button, "
        "a[data-strike], button[data-strike], input[data-strike], .strike"
    )

    # HP игрока / противника
    # Пример после DevTools: id="hp_val" → "#hp_val"
    hp_current: str = (
        "#hp_val, #hp_cur, #cur_hp, #currenthp, "
        "#hp, .hp-current, [data-stat='hp-cur'], [data-hp='current']"
    )
    hp_max: str = (
        "#hp_max, #max_hp, #maxhp, "
        ".hp-max, [data-stat='hp-max'], [data-hp='max']"
    )
    # Единый блок «123/456» (если current/max не разнесены)
    hp_text: str = (
        "#hp_val, #hp, .hp, [data-stat='hp'], "
        ".player-hp, #my_hp, .fight-hp-self"
    )
    hp_xpath: str = (
        '//*[@id="hp_val"] | //*[@id="hp"] | '
        '//*[contains(@class,"hp") and contains(text(),"/")]'
    )

    enemy_hp_text: str = (
        "#enemy_hp, .enemy-hp, .fight-hp-enemy, "
        "[data-stat='enemy-hp'], #opp_hp"
    )
    enemy_name: str = (
        "#enemy_name, .enemy-name, .opponent-name, "
        "[data-role='enemy-name'], #opp_name"
    )

    # Эликсиры / спеллы в бою
    elixir_slot: str = ".elixir-slot, [data-slot='elixir'], .fight-elixir"
    spell_slot: str = ".spell-slot, [data-slot='spell'], .fight-spell"

    # Исход боя
    victory_marker: str = (
        ".victory, #victory, [data-result='win'], "
        "text=/победа|вы победили/i"
    )
    defeat_marker: str = (
        ".defeat, #defeat, [data-result='lose'], "
        "text=/поражение|вы проиграли|погибли/i"
    )


@dataclass(frozen=True, slots=True)
class BackpackSelectors:
    """
    Рюкзак, пояс (карманы 1–8), вес, предметы.

    Слоты пояса — типичное место банок/эликсиров в Dwar.
    """

    panel: str = "#inventory, .inventory, [data-panel='inventory'], #backpack, .backpack"
    item: str = (
        ".inv-item, .inventory-item, [data-item-id], "
        ".belt-item, .pocket-item, .bag-item"
    )
    item_name: str = ".item-name, .inv-item-name, .name, b, strong"
    item_count: str = ".item-count, .inv-item-count, .count, .qty"
    weight: str = (
        "#weight, .weight, .bag-weight, [data-stat='weight'], "
        "#inv_weight, .inventory-weight"
    )
    weight_max: str = (
        "#weight_max, .weight-max, [data-stat='weight-max']"
    )

    # Пояс / карманы 1–8 (банки, эликсиры, свитки)
    belt_container: str = (
        "#belt, .belt, .pockets, #pockets, [data-panel='belt'], .hotbar"
    )
    pocket_1: str = (
        "[data-slot='1'], [data-pocket='1'], #pocket1, #belt_1, "
        ".pocket-1, .belt-slot-1, .pocket:nth-child(1)"
    )
    pocket_2: str = (
        "[data-slot='2'], [data-pocket='2'], #pocket2, #belt_2, "
        ".pocket-2, .belt-slot-2, .pocket:nth-child(2)"
    )
    pocket_3: str = (
        "[data-slot='3'], [data-pocket='3'], #pocket3, #belt_3, "
        ".pocket-3, .belt-slot-3, .pocket:nth-child(3)"
    )
    pocket_4: str = (
        "[data-slot='4'], [data-pocket='4'], #pocket4, #belt_4, "
        ".pocket-4, .belt-slot-4, .pocket:nth-child(4)"
    )
    pocket_5: str = (
        "[data-slot='5'], [data-pocket='5'], #pocket5, #belt_5, "
        ".pocket-5, .belt-slot-5, .pocket:nth-child(5)"
    )
    pocket_6: str = (
        "[data-slot='6'], [data-pocket='6'], #pocket6, #belt_6, "
        ".pocket-6, .belt-slot-6, .pocket:nth-child(6)"
    )
    pocket_7: str = (
        "[data-slot='7'], [data-pocket='7'], #pocket7, #belt_7, "
        ".pocket-7, .belt-slot-7, .pocket:nth-child(7)"
    )
    pocket_8: str = (
        "[data-slot='8'], [data-pocket='8'], #pocket8, #belt_8, "
        ".pocket-8, .belt-slot-8, .pocket:nth-child(8)"
    )

    # Общий селектор любого кармана пояса
    pocket_any: str = (
        "[data-slot], [data-pocket], .pocket, .belt-slot, .belt-item, .pocket-item"
    )

    def pocket(self, index: int) -> str:
        """Вернуть CSS для кармана 1..8."""
        if index < 1 or index > 8:
            raise ValueError("Индекс кармана должен быть в диапазоне 1..8")
        return getattr(self, f"pocket_{index}")

    def all_pockets(self) -> Tuple[str, ...]:
        return tuple(self.pocket(i) for i in range(1, 9))


@dataclass(frozen=True, slots=True)
class QuestSelectors:
    """Диалоги NPC, варианты ответов, панель квестов."""

    panel: str = "#quests, .quest-panel, [data-panel='quests'], #quest_list"
    active: str = ".quest-active, .quest-item.active, .quest.current"
    quest_item: str = ".quest-item, .quest, [data-quest-id], li.quest"
    quest_title: str = ".quest-title, .quest-name, .title, h3, h4"
    quest_status: str = ".quest-status, .status, [data-quest-status]"

    dialog: str = ".npc-dialog, .dialog-window, #dialog, #npc_dialog, .talk-window"
    dialog_text: str = ".dialog-text, .npc-text, #dialog_text, .talk-text"
    dialog_choices: str = (
        ".dialog-choice, .npc-choice, .dialog-options button, "
        ".dialog-options a, #dialog_choices a, #dialog_choices button, "
        ".talk-choice, a[data-choice], button[data-choice]"
    )
    dialog_continue: str = (
        ".dialog-continue, button[data-action='continue'], "
        "a.continue, #dialog_continue, input[value*='Далее']"
    )
    dialog_close: str = (
        ".dialog-close, #dialog_close, .npc-dialog .close, "
        "button[data-action='close']"
    )
    npc_link: str = (
        "a[href*='npc'], a[href*='talk'], a[href*='speak'], "
        "a[href*='dialog'], [data-npc-id], .npc-link"
    )


@dataclass(frozen=True, slots=True)
class LocationSelectors:
    """
    Переходы между локациями и интерактивные объекты сбора ресурсов.

    Идентификаторы рыбы / руды / травы — зарезервированные CSS/XPath;
    уточните через DevTools на конкретной локации фарма.
    """

    # Список выходов / переходов
    exits_list: str = (
        "#exits, .exits, .location-exits, #locations, "
        ".loc-list, [data-panel='exits'], #map_exits"
    )
    exit_item: str = (
        "#exits a, .exits a, .location-exit, .loc-link, "
        "a[href*='loc'], a[href*='location'], a[href*='go='], "
        "[data-loc-id], [data-location-id]"
    )
    location_title: str = (
        "#loc_name, .location-name, #location_title, "
        "[data-role='location-name'], h1.loc, .loc-title"
    )
    coords: str = (
        "#coords, .coords, [data-role='coords'], #xy, .loc-coords"
    )

    # Объекты сбора (профессии)
    resource_any: str = (
        "[data-resource], [data-node], .resource, .harvest, "
        ".gather, a[href*='harvest'], a[href*='gather'], "
        "a[href*='mine'], a[href*='fish'], a[href*='herb']"
    )
    resource_fish: str = (
        "[data-resource='fish'], [data-node='fish'], "
        ".resource-fish, #fish, a[href*='fish'], "
        "a[title*='Рыб'], a[title*='рыб'], "
        "[data-profession='fishing']"
    )
    resource_ore: str = (
        "[data-resource='ore'], [data-node='ore'], "
        ".resource-ore, #ore, a[href*='mine'], a[href*='ore'], "
        "a[title*='Руд'], a[title*='руд'], a[title*='Жил'], "
        "[data-profession='mining']"
    )
    resource_herb: str = (
        "[data-resource='herb'], [data-node='herb'], "
        ".resource-herb, #herb, a[href*='herb'], a[href*='plant'], "
        "a[title*='Трав'], a[title*='трав'], a[title*='Растен'], "
        "[data-profession='herbalism']"
    )
    resource_wood: str = (
        "[data-resource='wood'], [data-node='wood'], "
        ".resource-wood, a[href*='wood'], a[href*='chop'], "
        "a[title*='Дер'], a[title*='дер'], [data-profession='woodcutting']"
    )
    harvest_progress: str = (
        ".harvest-progress, #harvest_timer, .gather-timer, "
        "[data-harvest='progress'], .profession-timer"
    )
    harvest_ready: str = (
        ".harvest-ready, [data-harvest='ready'], .gather-ready"
    )


@dataclass(frozen=True, slots=True)
class StatsSelectors:
    """Характеристики персонажа во фрейме user/main."""

    nickname: str = ".nick, .user-nick, #nick, [data-role='nickname'], #user_name"
    level: str = ".level, #level, [data-role='level'], #user_level"
    hp: str = ".hp, #hp, [data-stat='hp'], #hp_val"
    mp: str = ".mp, #mp, [data-stat='mp'], #mp_val"
    energy: str = ".energy, #energy, [data-stat='energy'], #energy_val"
    gold: str = ".gold, .money, #gold, [data-currency='gold']"
    silver: str = ".silver, #silver, [data-currency='silver']"
    brass: str = ".brass, #brass, [data-currency='brass'], .copper, #copper"


@dataclass(frozen=True, slots=True)
class AuctionSelectors:
    """Аукцион / биржа и торговый чат (уточняйте через DevTools)."""

    panel: str = (
        "#auction, .auction, #market, .market, #exchange, "
        "[data-panel='auction'], table.auction, table#lots"
    )
    row: str = (
        "tr.lot, tr.auction-row, tr[data-lot-id], .auction-item, "
        "table.auction tbody tr, #auction_list tr"
    )
    search_input: str = (
        "input[name='search'], input[name='item'], #auction_search, input.search"
    )
    search_submit: str = (
        "button[type='submit'], #auction_search_btn, button.search"
    )
    buy_button: str = (
        "a[href*='buy'], button[data-action='buy'], .buyout, a.buy, button.buy"
    )
    sell_tab: str = (
        "a[href*='sell'], [data-tab='sell'], #sell_tab, .tab-sell"
    )
    next_page: str = (
        "a.next, a[rel='next'], .pagination .next, #next_page"
    )
    tax: str = ".tax, .fee, .commission, #tax, [data-tax]"
    trade_chat_line: str = (
        ".chat-message, .message, .msg, #chat .line, .trade-chat .line"
    )


@dataclass(frozen=True, slots=True)
class DwarSelectors:
    """Сводный реестр всех селекторов Dwar."""

    frames: FrameSelectors = field(default_factory=FrameSelectors)
    combat: CombatSelectors = field(default_factory=CombatSelectors)
    backpack: BackpackSelectors = field(default_factory=BackpackSelectors)
    quests: QuestSelectors = field(default_factory=QuestSelectors)
    location: LocationSelectors = field(default_factory=LocationSelectors)
    stats: StatsSelectors = field(default_factory=StatsSelectors)
    auction: AuctionSelectors = field(default_factory=AuctionSelectors)

    def as_flat_dict(self) -> Dict[str, str]:
        """Плоский словарь «группа.поле → css/xpath» для валидации и логов."""
        result: Dict[str, str] = {}
        for group_name in (
            "frames",
            "combat",
            "backpack",
            "quests",
            "location",
            "stats",
            "auction",
        ):
            group = getattr(self, group_name)
            for f in fields(group):
                value = getattr(group, f.name)
                if isinstance(value, str) and value.strip():
                    result[f"{group_name}.{f.name}"] = value
        # Зарезервированные имена фреймов отдельными ключами
        result["frames.MAIN_FRAME_NAME"] = MAIN_FRAME_NAME
        result["frames.CHAT_FRAME_NAME"] = CHAT_FRAME_NAME
        result["frames.USER_FRAME_NAME"] = USER_FRAME_NAME
        result["frames.COMBAT_FRAME_NAME"] = COMBAT_FRAME_NAME
        return result


# Singleton по умолчанию — правьте dataclasses выше или создайте свой экземпляр
SELECTORS: DwarSelectors = DwarSelectors()


# ===========================================================================
# 3. ВСПОМОГАТЕЛЬНЫЕ УТИЛИТЫ
# ===========================================================================

_XPATH_PREFIX = re.compile(r"^\s*(\(|/|id\()")
_PLAYWRIGHT_TEXT = re.compile(r"^\s*text\s*=", re.I)


def is_xpath(selector: str) -> bool:
    """True, если строка похожа на XPath (не CSS и не Playwright text=)."""
    s = (selector or "").strip()
    if not s or _PLAYWRIGHT_TEXT.match(s):
        return False
    return bool(_XPATH_PREFIX.match(s))


def split_selector_list(raw: str) -> List[str]:
    """
    Разбить строку с несколькими CSS через запятую.

    XPath и Playwright-псевдоселекторы (text=) не режем по запятой целиком —
    возвращаем как один кандидат, если вся строка — xpath/text.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    if is_xpath(raw) or _PLAYWRIGHT_TEXT.match(raw):
        return [raw]
    # Не режем запятые внутри [...] и (...)
    parts: List[str] = []
    buf: List[str] = []
    depth_sq = 0
    depth_par = 0
    for ch in raw:
        if ch == "[":
            depth_sq += 1
        elif ch == "]":
            depth_sq = max(0, depth_sq - 1)
        elif ch == "(":
            depth_par += 1
        elif ch == ")":
            depth_par = max(0, depth_par - 1)
        if ch == "," and depth_sq == 0 and depth_par == 0:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _owner_label(owner: Any) -> str:
    name = getattr(owner, "name", None)
    url = getattr(owner, "url", None) or ""
    if name:
        return f"frame:{name}"
    if url:
        short = url.split("/")[-1][:40] or url[:40]
        return f"frame-url:{short}"
    return "page"


async def _query_exists(owner: Any, selector: str) -> bool:
    """Проверить наличие элемента CSS/XPath в page или frame."""
    selector = (selector or "").strip()
    if not selector:
        return False
    # Playwright text=/.../ — пропускаем в DOM-валидации (не query_selector)
    if _PLAYWRIGHT_TEXT.match(selector):
        try:
            loc = owner.locator(selector)
            return await loc.count() > 0
        except Exception:
            return False
    try:
        if is_xpath(selector):
            handle = await owner.query_selector(f"xpath={selector}")
        else:
            handle = await owner.query_selector(selector)
        return handle is not None
    except Exception as exc:
        logger.debug("query_exists(%s) failed: %s", selector[:80], exc)
        return False


async def _any_candidate_exists(
    owners: Sequence[Any], candidates: Sequence[str]
) -> bool:
    for owner in owners:
        for cand in candidates:
            if await _query_exists(owner, cand):
                return True
    return False


async def _collect_owners(page: Page) -> List[Any]:
    """Page + все frame (включая вложенные)."""
    owners: List[Any] = [page]
    try:
        for fr in page.frames:
            if fr is not page.main_frame:
                owners.append(fr)
    except Exception as exc:
        logger.debug("Не удалось получить page.frames: %s", exc)
    return owners


def _frame_name_present(page: Page, names: Sequence[str]) -> bool:
    wanted = {n.lower() for n in names if n}
    try:
        for fr in page.frames:
            fname = (fr.name or "").lower()
            if fname in wanted:
                return True
            url = (fr.url or "").lower()
            if any(n in url for n in wanted):
                return True
    except Exception:
        return False
    return False


# ===========================================================================
# 4. ВАЛИДАТОР СЕЛЕКТОРОВ
# ===========================================================================


async def validate_selectors(
    page: Page,
    *,
    selectors: Optional[DwarSelectors] = None,
    groups: Optional[Sequence[str]] = None,
) -> Dict[str, bool]:
    """
    Поочерёдно проверить наличие селекторов на текущей странице.

    Обходит top-level document и все iframe/frame. Для каждого ключа
    ``группа.поле`` возвращает True, если хотя бы один кандидат из
    CSS-списка (или XPath) найден.

    Параметры
    ---------
    page:
        Playwright ``Page`` с загруженной игрой.
    selectors:
        Реестр селекторов (по умолчанию ``SELECTORS``).
    groups:
        Ограничить проверку группами: frames, combat, backpack, quests,
        location, stats. ``None`` — проверить всё.

    Возвращает
    ----------
    Dict[str, bool]
        Отчёт вида ``{"combat.strike_top": True, "combat.hp_current": False, ...}``.
        ``False`` означает: элемент не найден — селектор устарел или фрейм
        ещё не загрузился / вы не в том игровом состоянии (например, не в бою).

    Пример
    -------
    >>> report = await validate_selectors(page)
    >>> broken = [k for k, ok in report.items() if not ok]
    >>> print("Устарели:", broken)
    """
    registry = selectors or SELECTORS
    owners = await _collect_owners(page)
    allowed = set(groups) if groups else None
    report: Dict[str, bool] = {}

    # --- Имена фреймов (отдельная проверка по page.frames) ---
    frame_checks: Dict[str, Tuple[str, ...]] = {
        "frames.MAIN_FRAME_NAME": MAIN_FRAME_ALIASES,
        "frames.CHAT_FRAME_NAME": CHAT_FRAME_ALIASES,
        "frames.USER_FRAME_NAME": USER_FRAME_ALIASES,
        "frames.COMBAT_FRAME_NAME": COMBAT_FRAME_ALIASES,
        "frames.NAV_FRAME_NAME": NAV_FRAME_ALIASES,
    }
    if allowed is None or "frames" in allowed:
        for key, aliases in frame_checks.items():
            report[key] = _frame_name_present(page, aliases)

    # --- Поля dataclass ---
    for key, raw in registry.as_flat_dict().items():
        group = key.split(".", 1)[0]
        if allowed is not None and group not in allowed:
            continue
        # Имена фреймов уже проверены выше; CSS к <frame> тегам — ниже
        if key.endswith("_FRAME_NAME"):
            continue
        candidates = split_selector_list(raw)
        if not candidates:
            report[key] = False
            continue
        found = await _any_candidate_exists(owners, candidates)
        report[key] = found
        if not found:
            logger.debug(
                "validate_selectors: не найден %s (кандидаты=%s)",
                key,
                candidates[:3],
            )

    found_n = sum(1 for v in report.values() if v)
    total = len(report)
    logger.info(
        "validate_selectors: %s/%s селекторов найдены на странице",
        found_n,
        total,
    )
    return report


def format_validation_report(report: Mapping[str, bool]) -> str:
    """Человекочитаемый отчёт для логов / Telegram."""
    lines = ["Отчёт validate_selectors:", ""]
    ok_keys = sorted(k for k, v in report.items() if v)
    bad_keys = sorted(k for k, v in report.items() if not v)
    lines.append(f"Найдено: {len(ok_keys)} / {len(report)}")
    if bad_keys:
        lines.append("")
        lines.append("Не найдены (обновите через DevTools):")
        for key in bad_keys:
            lines.append(f"  ✗ {key}")
    if ok_keys:
        lines.append("")
        lines.append("OK:")
        for key in ok_keys:
            lines.append(f"  ✓ {key}")
    return "\n".join(lines)


def missing_selectors(report: Mapping[str, bool]) -> List[str]:
    """Список ключей с False — кандидаты на обновление после патча игры."""
    return sorted(k for k, ok in report.items() if not ok)


# ===========================================================================
# 5. МОСТ К dwar_bot.config.Selectors (плоские CSS-поля)
# ===========================================================================


def to_legacy_selector_kwargs(selectors: Optional[DwarSelectors] = None) -> Dict[str, str]:
    """
    Преобразовать реестр в kwargs, совместимые с ``dwar_bot.config.Selectors``.

    Использование::

        from dwar_bot.config import Selectors
        from config.selectors import to_legacy_selector_kwargs, SELECTORS

        s = Selectors(**{
            k: v for k, v in to_legacy_selector_kwargs().items()
            if k in {f.name for f in fields(Selectors)}
        })
    """
    s = selectors or SELECTORS
    return {
        "main_frame": s.frames.main_css,
        "combat_frame": s.frames.combat_css,
        "backpack_frame": s.frames.user_css,  # рюкзак часто в user-фрейме
        "chat_frame": s.frames.chat_css,
        "navigation_frame": s.frames.nav_css,
        "game_iframe": s.frames.game_iframe_css,
        "frameset": s.frames.frameset_css,
        "profile_nickname": s.stats.nickname,
        "profile_level": s.stats.level,
        "profile_hp": s.stats.hp,
        "profile_mp": s.stats.mp,
        "profile_energy": s.stats.energy,
        "profile_gold": s.stats.gold,
        "profile_silver": s.stats.silver,
        "profile_brass": s.stats.brass,
        "inventory_panel": s.backpack.panel,
        "inventory_item": s.backpack.item,
        "inventory_item_name": s.backpack.item_name,
        "inventory_item_count": s.backpack.item_count,
        "combat_panel": s.combat.panel,
        "combat_log": s.combat.log,
        "combat_attack_buttons": s.combat.attack_buttons,
        "combat_elixir_slot": s.combat.elixir_slot,
        "combat_spell_slot": s.combat.spell_slot,
        "combat_turn_indicator": s.combat.turn_indicator,
        "quest_panel": s.quests.panel,
        "quest_active": s.quests.active,
        "npc_dialog": s.quests.dialog,
        "npc_dialog_text": s.quests.dialog_text,
        "npc_dialog_choices": s.quests.dialog_choices,
        "npc_dialog_continue": s.quests.dialog_continue,
        "profession_timer": s.location.harvest_progress,
    }


def dump_selectors_json(selectors: Optional[DwarSelectors] = None) -> Dict[str, Any]:
    """Сериализация реестра в JSON-совместимый dict (для отладки)."""
    s = selectors or SELECTORS
    return {
        "MAIN_FRAME_NAME": MAIN_FRAME_NAME,
        "CHAT_FRAME_NAME": CHAT_FRAME_NAME,
        "USER_FRAME_NAME": USER_FRAME_NAME,
        "COMBAT_FRAME_NAME": COMBAT_FRAME_NAME,
        "NAV_FRAME_NAME": NAV_FRAME_NAME,
        "frames": asdict(s.frames),
        "combat": asdict(s.combat),
        "backpack": asdict(s.backpack),
        "quests": asdict(s.quests),
        "location": asdict(s.location),
        "stats": asdict(s.stats),
        "auction": asdict(s.auction),
    }


__all__ = [
    "MAIN_FRAME_NAME",
    "CHAT_FRAME_NAME",
    "USER_FRAME_NAME",
    "COMBAT_FRAME_NAME",
    "NAV_FRAME_NAME",
    "MAIN_FRAME_ALIASES",
    "CHAT_FRAME_ALIASES",
    "USER_FRAME_ALIASES",
    "COMBAT_FRAME_ALIASES",
    "NAV_FRAME_ALIASES",
    "FrameSelectors",
    "CombatSelectors",
    "BackpackSelectors",
    "QuestSelectors",
    "LocationSelectors",
    "StatsSelectors",
    "AuctionSelectors",
    "DwarSelectors",
    "SELECTORS",
    "is_xpath",
    "split_selector_list",
    "validate_selectors",
    "format_validation_report",
    "missing_selectors",
    "to_legacy_selector_kwargs",
    "dump_selectors_json",
]
