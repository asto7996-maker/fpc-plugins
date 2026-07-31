"""
Боевой движок для «Легенда: Наследие Драконов».

Парсит боевой фрейм, выбирает направления ударов (верх/сердце/низ),
пьёт эликсиры при низком HP и ведёт асинхронный цикл боя с human-like
задержками через BrowserEngine.human_click.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from dwar_bot.config import BotConfig, config
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError
from dwar_bot.modules.stats_parser import BackpackItem, PlayerStats, StatsParser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Константы и регулярки
# ---------------------------------------------------------------------------

STRIKE_TOP = "top"
STRIKE_CENTER = "center"
STRIKE_BOTTOM = "bottom"
DEFAULT_STRIKES: Tuple[str, ...] = (STRIKE_TOP, STRIKE_CENTER, STRIKE_BOTTOM)

STRIKE_ALIASES: Dict[str, str] = {
    "top": STRIKE_TOP,
    "верх": STRIKE_TOP,
    "голова": STRIKE_TOP,
    "head": STRIKE_TOP,
    "up": STRIKE_TOP,
    "high": STRIKE_TOP,
    "center": STRIKE_CENTER,
    "центр": STRIKE_CENTER,
    "сердце": STRIKE_CENTER,
    "грудь": STRIKE_CENTER,
    "heart": STRIKE_CENTER,
    "mid": STRIKE_CENTER,
    "middle": STRIKE_CENTER,
    "bottom": STRIKE_BOTTOM,
    "низ": STRIKE_BOTTOM,
    "ноги": STRIKE_BOTTOM,
    "legs": STRIKE_BOTTOM,
    "low": STRIKE_BOTTOM,
    "down": STRIKE_BOTTOM,
}

RE_FRACTION = re.compile(r"(?P<cur>\d+)\s*/\s*(?P<max>\d+)")
RE_ENEMY_HP = re.compile(
    r"(?:противник|враг|enemy|mob).{0,40}?(?P<cur>\d+)\s*/\s*(?P<max>\d+)",
    re.IGNORECASE | re.DOTALL,
)
RE_PLAYER_HP = re.compile(
    r"(?:вы|you|игрок|player|hp|жизн).{0,40}?(?P<cur>\d+)\s*/\s*(?P<max>\d+)",
    re.IGNORECASE | re.DOTALL,
)
RE_ENEMY_NAME = re.compile(
    r"(?:противник|враг|enemy|бой\s+с)\s*[:=]?\s*(?P<name>[^\n|<]{2,60})",
    re.IGNORECASE,
)
RE_VICTORY = re.compile(
    r"(?:победа|вы\s+победили|противник\s+повержен|victory|won)",
    re.IGNORECASE,
)
RE_DEFEAT = re.compile(
    r"(?:поражение|вы\s+проиграли|вы\s+погибли|defeat|lost)",
    re.IGNORECASE,
)
RE_YOUR_TURN = re.compile(
    r"(?:ваш\s+ход|ваш\s+удар|ходите|your\s+turn|атакуйте)",
    re.IGNORECASE,
)
RE_HEAL_ITEM = re.compile(
    r"(?:эликс|лечеб|исцел|хил|heal|potion|банка|жизн|hp|здоров)",
    re.IGNORECASE,
)
RE_COOLDOWN = re.compile(
    r"(?:перезаряд|кулдаун|cooldown|подождите|нельзя\s+ещё|через\s+\d+)",
    re.IGNORECASE,
)

COMBAT_FRAME_NAMES: Tuple[str, ...] = (
    "fight",
    "combat",
    "battle",
    "duel",
    "arena",
)


class FightResult(str, Enum):
    VICTORY = "victory"
    DEFEAT = "defeat"
    ESCAPED = "escaped"
    TIMEOUT = "timeout"
    ERROR = "error"
    NOT_IN_COMBAT = "not_in_combat"


@dataclass(slots=True)
class CombatState:
    """Снимок состояния боя."""

    in_combat: bool = False
    player_hp: int = 0
    player_max_hp: int = 0
    enemy_hp: int = 0
    enemy_max_hp: int = 0
    enemy_name: str = ""
    available_strikes: List[str] = field(default_factory=list)
    combo_sequence: List[str] = field(default_factory=list)
    last_log_line: str = ""
    log_lines: List[str] = field(default_factory=list)
    is_player_turn: bool = False
    outcome: Optional[str] = None  # victory / defeat / None
    stale: bool = False
    parsed_at: float = field(default_factory=time.time)
    source_frame: str = ""

    @property
    def player_hp_pct(self) -> float:
        if self.player_max_hp <= 0:
            return 0.0
        return max(0.0, min(100.0, self.player_hp / self.player_max_hp * 100.0))

    @property
    def enemy_hp_pct(self) -> float:
        if self.enemy_max_hp <= 0:
            return 0.0
        return max(0.0, min(100.0, self.enemy_hp / self.enemy_max_hp * 100.0))

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CombatEngineError(Exception):
    """Ошибка боевого движка."""


class CombatEngine:
    """
    Движок боя: парсинг фрейма, удары, эликсиры, главный цикл.

    Можно передать готовый ``BrowserEngine`` — тогда клики идут через
    ``human_click``. Без него используется прямой Playwright click с
    теми же human-like задержками.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        browser: Optional[BrowserEngine] = None,
        stats_parser: Optional[StatsParser] = None,
        potion_cooldown_sec: float = 8.0,
        max_fight_seconds: float = 600.0,
        stuck_retries: int = 5,
    ) -> None:
        self._config = bot_config or config
        self._selectors = self._config.selectors
        self._browser = browser
        self._stats_parser = stats_parser or StatsParser(self._config)
        self._potion_cooldown_sec = potion_cooldown_sec
        self._max_fight_seconds = max_fight_seconds
        self._stuck_retries = stuck_retries

        self._last_state: Optional[CombatState] = None
        self._combo_history: List[str] = []
        self._last_potion_at: float = 0.0
        self._last_strike_fingerprint: str = ""
        self._strike_index: int = 0

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    async def parse_combat_state(self, page: Page) -> CombatState:
        """
        Сканирует боевой фрейм: HP сторон, доступные удары, лог боя.
        """
        try:
            frame = await self._resolve_combat_frame(page)
            if frame is None:
                # Боевой UI иногда рендерится в main / на page
                html = await self._safe_content(page)
                if not self._looks_like_combat(html):
                    state = CombatState(
                        in_combat=False,
                        stale=False,
                        parsed_at=time.time(),
                        source_frame="none",
                        combo_sequence=list(self._combo_history),
                    )
                    self._last_state = state
                    return state
                soup = BeautifulSoup(html, "html.parser")
                source = "page"
            else:
                html = await self._safe_frame_html(frame)
                if not html.strip():
                    raise RuntimeError("Пустой HTML боевого фрейма")
                soup = BeautifulSoup(html, "html.parser")
                source = frame.name or frame.url or "combat"

            text = self._visible_text(soup)
            player_hp, player_max = self._parse_player_hp(soup, text)
            enemy_hp, enemy_max = self._parse_enemy_hp(soup, text)
            enemy_name = self._parse_enemy_name(soup, text)
            strikes = await self._parse_available_strikes(page, frame, soup)
            log_lines = self._parse_combat_log(soup)
            last_log = log_lines[-1] if log_lines else ""

            outcome = None
            blob = f"{text}\n{last_log}"
            if RE_VICTORY.search(blob):
                outcome = FightResult.VICTORY.value
            elif RE_DEFEAT.search(blob):
                outcome = FightResult.DEFEAT.value

            in_combat = True
            if outcome is not None:
                in_combat = False
            elif not strikes and enemy_max <= 0 and not self._looks_like_combat(html):
                in_combat = False

            is_turn = bool(RE_YOUR_TURN.search(text)) or bool(strikes)

            state = CombatState(
                in_combat=in_combat,
                player_hp=player_hp,
                player_max_hp=player_max,
                enemy_hp=enemy_hp,
                enemy_max_hp=enemy_max,
                enemy_name=enemy_name,
                available_strikes=strikes,
                combo_sequence=list(self._combo_history),
                last_log_line=last_log,
                log_lines=log_lines[-30:],
                is_player_turn=is_turn,
                outcome=outcome,
                stale=False,
                parsed_at=time.time(),
                source_frame=source,
            )
            self._last_state = state
            logger.debug(
                "CombatState: in=%s enemy=%s HP %s/%s vs %s/%s strikes=%s log=%r",
                state.in_combat,
                state.enemy_name or "?",
                state.player_hp,
                state.player_max_hp,
                state.enemy_hp,
                state.enemy_max_hp,
                state.available_strikes,
                state.last_log_line[:80],
            )
            return state

        except Exception as exc:
            logger.error("Ошибка parse_combat_state: %s", exc, exc_info=True)
            if self._last_state is not None:
                stale = replace(
                    self._last_state, stale=True, parsed_at=time.time()
                )
                return stale
            return CombatState(in_combat=False, stale=True, parsed_at=time.time())

    async def execute_strike(self, page: Page, strike_type: str) -> bool:
        """
        Выбирает направление удара (top / center / bottom) через human_click.

        Returns:
            True если ход, похоже, прошёл (лог/состояние обновились).
        """
        normalized = self.normalize_strike(strike_type)
        think_delay = random.uniform(0.4, 1.2)
        logger.info(
            "Удар '%s' (нормализован: %s), раздумье %.2f сек",
            strike_type,
            normalized,
            think_delay,
        )
        await asyncio.sleep(think_delay)

        before = await self.parse_combat_state(page)
        if not before.in_combat and before.outcome:
            logger.info("Бой уже завершён (%s), удар пропущен", before.outcome)
            return False

        frame = await self._resolve_combat_frame(page)
        selector = await self._find_strike_selector(
            page, frame, normalized, before.available_strikes
        )
        if selector is None:
            logger.warning("Кнопка удара '%s' не найдена", normalized)
            return False

        try:
            await self._click(page, selector, frame=frame)
        except (BrowserEngineError, PlaywrightError) as exc:
            logger.error("Не удалось кликнуть удар %s: %s", normalized, exc)
            return False

        self._combo_history.append(normalized)
        if len(self._combo_history) > 32:
            self._combo_history = self._combo_history[-32:]

        # Ждём обновления фрейма
        await asyncio.sleep(random.uniform(0.35, 0.9))
        after = await self._wait_state_change(page, before, timeout_sec=4.0)
        success = self._strike_succeeded(before, after, normalized)
        if success:
            logger.info(
                "Ход выполнен: %s | log: %s | enemy HP %s/%s",
                normalized,
                after.last_log_line[:100],
                after.enemy_hp,
                after.enemy_max_hp,
            )
        else:
            logger.warning(
                "Ход '%s' не подтверждён обновлением состояния (stale=%s)",
                normalized,
                after.stale,
            )
        return success

    async def use_potion_if_needed(
        self,
        page: Page,
        stats: PlayerStats,
        backpack: List[BackpackItem],
        hp_threshold_pct: float = 40.0,
    ) -> bool:
        """
        Пьёт лечебный эликсир из пояса, если HP% ниже порога.

        Учитывает cooldown повторного использования.
        """
        hp_pct = stats.hp_ratio * 100.0
        if stats.hp_max <= 0 and self._last_state is not None:
            hp_pct = self._last_state.player_hp_pct

        if hp_pct >= hp_threshold_pct:
            return False

        now = time.time()
        remaining_cd = self._potion_cooldown_sec - (now - self._last_potion_at)
        if remaining_cd > 0:
            logger.info(
                "HP критично (%.1f%%), но potion cooldown ещё %.1f сек",
                hp_pct,
                remaining_cd,
            )
            return False

        potion = self._find_healing_potion(backpack)
        if potion is None:
            logger.warning(
                "HP %.1f%% < %.1f%%, но лечебных банок в поясе нет",
                hp_pct,
                hp_threshold_pct,
            )
            return False

        logger.warning(
            "Критическое HP %.1f%% — используем '%s' (slot=%s, id=%s)",
            hp_pct,
            potion.name,
            potion.slot_index,
            potion.usable_id,
        )

        clicked = await self._click_backpack_item(page, potion)
        if not clicked:
            return False

        self._last_potion_at = time.time()
        await asyncio.sleep(random.uniform(0.5, 1.1))

        # Проверка cooldown / ошибки в уведомлениях
        try:
            notes = await self._stats_parser.parse_notifications(page)
            for note in notes:
                if RE_COOLDOWN.search(note.text):
                    logger.info("Сервер сообщил о cooldown зелья: %s", note.text)
                    break
        except Exception as exc:
            logger.debug("Не удалось прочитать уведомления после зелья: %s", exc)

        # Обновляем статы для лога
        try:
            new_stats = await self._stats_parser.parse_player_stats(page)
            logger.info(
                "После эликсира HP %s/%s (%.1f%%)",
                new_stats.hp_current,
                new_stats.hp_max,
                new_stats.hp_ratio * 100.0,
            )
        except Exception as exc:
            logger.debug("Не удалось перечитать статы после зелья: %s", exc)

        return True

    async def process_fight(
        self,
        page: Page,
        target_combo: Optional[List[str]] = None,
        *,
        hp_threshold_pct: float = 40.0,
        refresh_backpack: bool = True,
    ) -> bool:
        """
        Главный асинхронный цикл боя.

        Поочерёдно бьёт по ``target_combo`` (или случайно), пьёт эликсиры
        при низком HP, завершается при победе/поражении/исчезновении фрейма.

        Returns:
            True при победе, False при поражении / таймауте / ошибке.
        """
        combo = [
            self.normalize_strike(s)
            for s in (target_combo or [])
            if self.normalize_strike(s)
        ]
        self._strike_index = 0
        started = time.time()
        stuck_count = 0
        backpack: List[BackpackItem] = []

        logger.info(
            "Старт боя: combo=%s threshold=%.1f%%",
            combo or "random",
            hp_threshold_pct,
        )

        if refresh_backpack:
            try:
                backpack = await self._stats_parser.parse_backpack(page)
            except Exception as exc:
                logger.warning("Не удалось прочитать рюкзак перед боем: %s", exc)

        while True:
            if time.time() - started > self._max_fight_seconds:
                logger.error(
                    "Таймаут боя (%.0f сек)", self._max_fight_seconds
                )
                return False

            state = await self._parse_with_stuck_guard(page)
            if state is None:
                stuck_count += 1
                if stuck_count >= self._stuck_retries:
                    logger.error(
                        "Боевой фрейм завис после %s попыток", self._stuck_retries
                    )
                    return False
                pause = random.uniform(1.0, 2.5)
                logger.warning(
                    "Фрейм боя недоступен, пауза %.1f сек (%s/%s)",
                    pause,
                    stuck_count,
                    self._stuck_retries,
                )
                await asyncio.sleep(pause)
                continue

            stuck_count = 0

            if state.outcome == FightResult.VICTORY.value:
                logger.info("Победа над '%s'", state.enemy_name or "противником")
                return True
            if state.outcome == FightResult.DEFEAT.value:
                logger.warning("Поражение в бою с '%s'", state.enemy_name or "?")
                return False
            if not state.in_combat:
                # Фрейм исчез — считаем завершением
                if self._last_state and self._last_state.in_combat:
                    # Короткое ожидание: фрейм мог перезагрузиться
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    recheck = await self.parse_combat_state(page)
                    if recheck.in_combat:
                        state = recheck
                    elif recheck.outcome == FightResult.VICTORY.value:
                        logger.info("Победа (подтверждена после исчезновения фрейма)")
                        return True
                    elif recheck.outcome == FightResult.DEFEAT.value:
                        logger.warning("Поражение (подтверждено после исчезновения фрейма)")
                        return False
                    else:
                        logger.info(
                            "Бой завершён: фрейм исчез (last_log=%r)",
                            state.last_log_line,
                        )
                        # Если враг был на 0 HP — победа
                        if (
                            self._last_state.enemy_max_hp > 0
                            and self._last_state.enemy_hp <= 0
                        ):
                            return True
                        return self._infer_result_from_log(state)
                logger.info("Не в бою — process_fight завершён")
                return False

            # Лечение
            stats = PlayerStats(
                hp_current=state.player_hp,
                hp_max=state.player_max_hp,
                in_combat=True,
            )
            if state.player_hp_pct > 0 and state.player_hp_pct < hp_threshold_pct + 15:
                # Периодически обновляем рюкзак при низком HP
                if refresh_backpack and (
                    not backpack or time.time() - self._last_potion_at > 15
                ):
                    try:
                        backpack = await self._stats_parser.parse_backpack(page)
                    except Exception as exc:
                        logger.debug("refresh backpack: %s", exc)

            used = await self.use_potion_if_needed(
                page, stats, backpack, hp_threshold_pct=hp_threshold_pct
            )
            if used:
                await asyncio.sleep(random.uniform(0.3, 0.8))
                continue

            if state.player_hp_pct > 0 and state.player_hp_pct < 25:
                logger.warning(
                    "Критическое HP в бою: %.1f%% (%s/%s)",
                    state.player_hp_pct,
                    state.player_hp,
                    state.player_max_hp,
                )

            # Если не наш ход — ждём
            if not state.is_player_turn and not state.available_strikes:
                await asyncio.sleep(random.uniform(0.5, 1.2))
                continue

            strike = self._next_strike(combo, state.available_strikes)
            ok = await self.execute_strike(page, strike)
            if not ok:
                # Смена тактики: другой удар
                alt = self._random_strike(state.available_strikes, exclude=strike)
                if alt and alt != strike:
                    logger.info("Повтор удара альтернативой: %s", alt)
                    await self.execute_strike(page, alt)
                else:
                    await asyncio.sleep(random.uniform(0.6, 1.4))

            # Пауза между ходами
            await asyncio.sleep(
                random.uniform(
                    self._config.delays.combat_min,
                    self._config.delays.combat_max,
                )
            )

    # ------------------------------------------------------------------
    # Парсинг боя
    # ------------------------------------------------------------------

    async def _parse_with_stuck_guard(self, page: Page) -> Optional[CombatState]:
        """Повторяет parse при stale/зависшем фрейме; None = нужен внешний retry."""
        state = await self.parse_combat_state(page)
        if state.stale:
            return None
        return state

    async def _resolve_combat_frame(self, page: Page) -> Optional[Frame]:
        selectors = self._selectors.combat_frame
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            for frame in page.frames:
                name = (frame.name or "").lower()
                if name in COMBAT_FRAME_NAMES:
                    return frame
                url = (frame.url or "").lower()
                if any(n in url for n in COMBAT_FRAME_NAMES):
                    return frame

            for selector in self._split_css(selectors):
                try:
                    handle = await page.query_selector(selector)
                except PlaywrightError:
                    continue
                if handle is None:
                    continue
                try:
                    content = await handle.content_frame()
                except PlaywrightError:
                    continue
                if content is not None:
                    return content

            await asyncio.sleep(0.15)
        return None

    def _looks_like_combat(self, html: str) -> bool:
        if not html:
            return False
        low = html.lower()
        markers = (
            "fight",
            "combat",
            "бой",
            "противник",
            "ваш ход",
            "удар",
            self._selectors.combat_panel.split(",")[0].strip().lstrip("#."),
        )
        return any(m in low for m in markers if m)

    def _parse_player_hp(self, soup: BeautifulSoup, text: str) -> Tuple[int, int]:
        for selector in self._split_css(
            (
                self._selectors.profile_hp,
                "#my_hp",
                ".my-hp",
                "#hp",
                ".hp",
                "[data-role='player-hp']",
                ".player-hp",
            )
        ):
            node = soup.select_one(selector)
            if node:
                pair = self._fraction_from_node(node)
                if pair != (0, 0):
                    return pair

        m = RE_PLAYER_HP.search(text)
        if m:
            return int(m.group("cur")), int(m.group("max"))

        # Первая дробь часто — HP игрока
        fractions = list(RE_FRACTION.finditer(text))
        if fractions:
            return int(fractions[0].group("cur")), int(fractions[0].group("max"))
        return 0, 0

    def _parse_enemy_hp(self, soup: BeautifulSoup, text: str) -> Tuple[int, int]:
        for selector in (
            "#enemy_hp",
            ".enemy-hp",
            "[data-role='enemy-hp']",
            ".mob-hp",
            "#mob_hp",
            ".opponent-hp",
        ):
            node = soup.select_one(selector)
            if node:
                pair = self._fraction_from_node(node)
                if pair != (0, 0):
                    return pair

        m = RE_ENEMY_HP.search(text)
        if m:
            return int(m.group("cur")), int(m.group("max"))

        fractions = list(RE_FRACTION.finditer(text))
        if len(fractions) >= 2:
            return int(fractions[1].group("cur")), int(fractions[1].group("max"))
        return 0, 0

    def _parse_enemy_name(self, soup: BeautifulSoup, text: str) -> str:
        for selector in (
            ".enemy-name",
            "#enemy_name",
            ".mob-name",
            "[data-role='enemy-name']",
            ".opponent-name",
            ".fighter-name",
        ):
            node = soup.select_one(selector)
            if node:
                name = node.get_text(" ", strip=True)
                if name:
                    return name[:80]

        m = RE_ENEMY_NAME.search(text)
        if m:
            return m.group("name").strip()[:80]
        return ""

    async def _parse_available_strikes(
        self,
        page: Page,
        frame: Optional[Frame],
        soup: BeautifulSoup,
    ) -> List[str]:
        found: List[str] = []
        # DOM-анализ
        button_selectors = self._split_css(
            (
                self._selectors.combat_attack_buttons,
                "button[data-strike]",
                "a[data-strike]",
                "input[data-strike]",
                ".strike",
                ".hit",
                ".attack",
                "button.attack",
                "a.attack",
            )
        )
        for selector in button_selectors:
            for node in soup.select(selector):
                strike = self._strike_from_node(node)
                if strike and strike not in found:
                    found.append(strike)

        # Текстовые кнопки Верх/Сердце/Низ
        for node in soup.find_all(["a", "button", "input", "div", "span", "td"]):
            label = self._node_label(node).lower()
            for alias, strike in STRIKE_ALIASES.items():
                if alias in label and strike not in found:
                    # Избегаем ложных срабатываний на длинных текстах лога
                    if len(label) <= 40:
                        found.append(strike)

        # Playwright: проверка видимости
        owner = frame or page
        for strike in DEFAULT_STRIKES:
            if strike in found:
                continue
            for selector in self._strike_selector_candidates(strike):
                try:
                    handle = await owner.query_selector(selector)
                    if handle is not None and await handle.is_visible():
                        found.append(strike)
                        break
                except PlaywrightError:
                    continue

        # Если кнопки есть, но тип не распознан — даём все три по умолчанию
        if not found:
            generic = soup.select(
                self._selectors.combat_attack_buttons.split(",")[0].strip()
            )
            if generic:
                return list(DEFAULT_STRIKES)

        return found

    def _parse_combat_log(self, soup: BeautifulSoup) -> List[str]:
        lines: List[str] = []
        for selector in self._split_css(
            (
                self._selectors.combat_log,
                "#fight_log",
                ".fight-log",
                ".combat-log",
                "#battle_log",
                ".log-line",
                ".fight-msg",
            )
        ):
            for node in soup.select(selector):
                # Контейнер лога — берём дочерние строки
                children = node.find_all(["div", "p", "li", "tr", "span"], recursive=False)
                if children:
                    for child in children:
                        text = child.get_text(" ", strip=True)
                        if text:
                            lines.append(text)
                else:
                    text = node.get_text("\n", strip=True)
                    lines.extend(
                        part.strip()
                        for part in text.splitlines()
                        if part.strip()
                    )

        if not lines:
            # Фоллбек: последние непустые строки body
            body = soup.body.get_text("\n", strip=True) if soup.body else ""
            lines = [ln.strip() for ln in body.splitlines() if ln.strip()][-20:]

        # Уникальные с сохранением порядка
        unique: List[str] = []
        seen: set[str] = set()
        for line in lines:
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(line[:300])
        return unique

    # ------------------------------------------------------------------
    # Удары
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_strike(strike_type: str) -> str:
        key = (strike_type or "").strip().lower()
        return STRIKE_ALIASES.get(key, key if key in DEFAULT_STRIKES else "")

    def _next_strike(
        self, combo: Sequence[str], available: Sequence[str]
    ) -> str:
        pool = list(available) if available else list(DEFAULT_STRIKES)
        if combo:
            # Циклически идём по комбо, пропуская недоступные
            for _ in range(len(combo)):
                candidate = combo[self._strike_index % len(combo)]
                self._strike_index += 1
                if candidate in pool or not available:
                    return candidate
            return combo[0]
        return self._random_strike(pool)

    def _random_strike(
        self, available: Sequence[str], exclude: str = ""
    ) -> str:
        pool = [s for s in (available or DEFAULT_STRIKES) if s != exclude]
        if not pool:
            pool = list(DEFAULT_STRIKES)
        # Лёгкий bias: не повторять последний удар слишком часто
        if self._combo_history and len(pool) > 1:
            last = self._combo_history[-1]
            weighted: List[str] = []
            for s in pool:
                weighted.extend([s] * (1 if s == last else 3))
            return random.choice(weighted)
        return random.choice(list(pool))

    def _strike_selector_candidates(self, strike: str) -> List[str]:
        labels = {
            STRIKE_TOP: ("top", "верх", "голова", "head", "up", "high"),
            STRIKE_CENTER: ("center", "центр", "сердце", "heart", "mid", "middle"),
            STRIKE_BOTTOM: ("bottom", "низ", "ноги", "legs", "low", "down"),
        }
        words = labels.get(strike, (strike,))
        selectors: List[str] = [
            f"[data-strike='{strike}']",
            f"[data-hit='{strike}']",
            f"[data-zone='{strike}']",
            f"button.{strike}",
            f"a.{strike}",
            f"#{strike}",
            f".strike-{strike}",
            f".hit-{strike}",
        ]
        for word in words:
            selectors.extend(
                [
                    f"button:has-text('{word}')",
                    f"a:has-text('{word}')",
                    f"input[value*='{word}' i]",
                    f"[title*='{word}' i]",
                    f"[alt*='{word}' i]",
                ]
            )
        return selectors

    async def _find_strike_selector(
        self,
        page: Page,
        frame: Optional[Frame],
        strike: str,
        available: Sequence[str],
    ) -> Optional[str]:
        owner: Any = frame or page
        for selector in self._strike_selector_candidates(strike):
            # :has-text / i-флаг — синтаксис Playwright, не query_selector CSS
            if ":has-text" in selector or " i]" in selector:
                try:
                    loc = owner.locator(selector)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        return selector
                except PlaywrightError:
                    continue
                continue
            try:
                handle = await owner.query_selector(selector)
                if handle is not None and await handle.is_visible():
                    return selector
            except PlaywrightError:
                continue

        # Фоллбек: N-я кнопка атаки
        order = {STRIKE_TOP: 0, STRIKE_CENTER: 1, STRIKE_BOTTOM: 2}
        idx = order.get(strike, 0)
        attack_sel = self._selectors.combat_attack_buttons
        try:
            loc = owner.locator(attack_sel)
            count = await loc.count()
            if count > 0:
                target_idx = min(idx, count - 1)
                # Возвращаем селектор через nth — кликнём по locator
                return f"__nth__:{target_idx}:{attack_sel}"
        except PlaywrightError:
            pass

        if available and strike not in available:
            logger.debug(
                "Удар %s нет среди available=%s", strike, list(available)
            )
        return None

    def _strike_from_node(self, node: Tag) -> str:
        for attr in ("data-strike", "data-hit", "data-zone", "data-action", "value", "name"):
            raw = node.get(attr)
            if raw:
                normalized = self.normalize_strike(str(raw))
                if normalized:
                    return normalized
        for attr in ("class",):
            classes = node.get(attr)
            if not classes:
                continue
            class_list = classes if isinstance(classes, list) else str(classes).split()
            for cls in class_list:
                normalized = self.normalize_strike(cls.replace("strike-", "").replace("hit-", ""))
                if normalized:
                    return normalized
        label = self._node_label(node)
        return self.normalize_strike(label)

    @staticmethod
    def _node_label(node: Tag) -> str:
        parts = [
            node.get("title") or "",
            node.get("alt") or "",
            node.get("value") or "",
            node.get_text(" ", strip=True),
        ]
        return " ".join(p for p in parts if p)

    async def _wait_state_change(
        self, page: Page, before: CombatState, timeout_sec: float
    ) -> CombatState:
        deadline = time.monotonic() + timeout_sec
        last = before
        while time.monotonic() < deadline:
            await asyncio.sleep(random.uniform(0.2, 0.45))
            current = await self.parse_combat_state(page)
            last = current
            if self._strike_succeeded(before, current, ""):
                return current
            if current.outcome:
                return current
            if not current.in_combat and before.in_combat:
                return current
        return last

    def _strike_succeeded(
        self, before: CombatState, after: CombatState, strike: str
    ) -> bool:
        if after.outcome:
            return True
        if after.last_log_line and after.last_log_line != before.last_log_line:
            return True
        if after.enemy_hp != before.enemy_hp and after.enemy_max_hp > 0:
            return True
        if after.player_hp != before.player_hp and after.player_max_hp > 0:
            return True
        fingerprint = f"{after.enemy_hp}:{after.player_hp}:{after.last_log_line}"
        if fingerprint != self._last_strike_fingerprint and after.in_combat:
            # Первое сравнение после старта — не считать успехом
            if self._last_strike_fingerprint:
                self._last_strike_fingerprint = fingerprint
                return True
        self._last_strike_fingerprint = fingerprint
        # Если ход ушёл (кнопки пропали / не наш ход) — тоже успех
        if before.is_player_turn and not after.is_player_turn and after.in_combat:
            return True
        return False

    # ------------------------------------------------------------------
    # Эликсиры
    # ------------------------------------------------------------------

    def _find_healing_potion(
        self, backpack: Sequence[BackpackItem]
    ) -> Optional[BackpackItem]:
        healers = [
            item
            for item in backpack
            if item.count > 0
            and (
                item.item_type in {"elixir", "potion"}
                or RE_HEAL_ITEM.search(item.name)
                or RE_HEAL_ITEM.search(item.tooltip)
            )
        ]
        if not healers:
            return None

        # Приоритет: явный heal в названии, затем elixir, меньше charges раньше
        def sort_key(item: BackpackItem) -> Tuple[int, int, int]:
            name_score = 0 if RE_HEAL_ITEM.search(item.name) else 1
            type_score = 0 if item.item_type == "elixir" else 1
            return (name_score, type_score, item.slot_index)

        healers.sort(key=sort_key)
        return healers[0]

    async def _click_backpack_item(self, page: Page, item: BackpackItem) -> bool:
        frame = None
        for name in ("backpack", "inventory", "main", "user"):
            for f in page.frames:
                if (f.name or "").lower() == name:
                    frame = f
                    break
            if frame:
                break

        selectors: List[str] = []
        if item.action_id:
            selectors.extend(
                [
                    f"[data-item-id='{item.action_id}']",
                    f"[data-id='{item.action_id}']",
                    f"#{item.action_id}",
                    f"a[href*='{item.action_id}']",
                ]
            )
        if item.slot_index >= 0:
            selectors.extend(
                [
                    f"[data-slot='{item.slot_index}']",
                    f"[data-slot-index='{item.slot_index}']",
                ]
            )
        if item.action_url:
            href = item.action_url.replace("'", "")
            selectors.append(f"a[href='{href}']")
            selectors.append(f"a[href*=\"{href.split('?')[0]}\"]")
        if item.name:
            short = item.name[:24].replace("'", "")
            selectors.append(f"[title*='{short}']")

        owner = frame or page
        for selector in selectors:
            try:
                handle = await owner.query_selector(selector)
                if handle is None:
                    # Playwright text selector
                    if selector.startswith("[title"):
                        loc = owner.locator(selector)
                        if await loc.count() == 0:
                            continue
                        await self._click(page, selector, frame=frame)
                        return True
                    continue
                if not await handle.is_visible():
                    continue
                await self._click(page, selector, frame=frame)
                logger.info("Клик по эликсиру: %s (%s)", item.name, selector)
                return True
            except (BrowserEngineError, PlaywrightError) as exc:
                logger.debug("Клик зелья selector=%s: %s", selector, exc)
                continue

        # Фоллбек: N-й слот пояса
        if item.slot_index >= 0:
            belt_sel = (
                f"{self._selectors.inventory_item}, .belt-item, "
                f".pocket-item, [data-slot]"
            )
            try:
                loc = owner.locator(belt_sel)
                count = await loc.count()
                if 0 <= item.slot_index < count:
                    await self._click(
                        page,
                        f"__nth__:{item.slot_index}:{belt_sel}",
                        frame=frame,
                    )
                    return True
            except (BrowserEngineError, PlaywrightError) as exc:
                logger.error("Фоллбек-клик слота пояса не удался: %s", exc)

        logger.error("Не удалось нажать эликсир '%s'", item.name)
        return False

    # ------------------------------------------------------------------
    # Клики / утилиты
    # ------------------------------------------------------------------

    async def _click(
        self,
        page: Page,
        selector: str,
        *,
        frame: Optional[Frame] = None,
    ) -> None:
        # Специальный формат nth-локатора
        if selector.startswith("__nth__:"):
            _, idx_s, real_sel = selector.split(":", 2)
            idx = int(idx_s)
            owner: Any = frame or page
            loc = owner.locator(real_sel).nth(idx)
            if self._browser is not None:
                # human_click ожидает str|ElementHandle — кликаем через мышь вручную
                await self._human_click_locator(page, loc)
            else:
                await asyncio.sleep(random.uniform(0.4, 1.0))
                await loc.click(timeout=self._config.browser.default_timeout_ms)
            return

        if self._browser is not None:
            await self._browser.human_click(selector, page=page, frame=frame)
            return

        owner = frame or page
        await asyncio.sleep(random.uniform(0.4, 1.0))
        try:
            await owner.click(
                selector, timeout=self._config.browser.default_timeout_ms
            )
        except PlaywrightError:
            # Playwright text/engines selectors
            loc = owner.locator(selector)
            await loc.first.click(timeout=self._config.browser.default_timeout_ms)

    async def _human_click_locator(self, page: Page, locator: Any) -> None:
        """Human-like клик по Playwright Locator (для nth-элементов)."""
        await asyncio.sleep(random.uniform(0.35, 1.0))
        try:
            handle = await locator.element_handle(timeout=5_000)
        except PlaywrightError as exc:
            raise BrowserEngineError(f"locator handle: {exc}") from exc
        if handle is None:
            raise BrowserEngineError("locator.element_handle вернул None")

        if self._browser is not None:
            await self._browser.human_click(handle, page=page)
            return

        box = await handle.bounding_box()
        if box is None:
            await handle.click()
            return
        dx = random.uniform(-3, 3)
        dy = random.uniform(-3, 3)
        x = box["x"] + box["width"] / 2 + dx
        y = box["y"] + box["height"] / 2 + dy
        await page.mouse.move(x, y, steps=random.randint(5, 14))
        await asyncio.sleep(random.uniform(0.08, 0.3))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.04, 0.1))
        await page.mouse.up()

    def _infer_result_from_log(self, state: CombatState) -> bool:
        blob = "\n".join(state.log_lines + [state.last_log_line])
        if RE_VICTORY.search(blob):
            return True
        if RE_DEFEAT.search(blob):
            return False
        # Неопределённый исход — False (осторожная семантика)
        return False

    def _fraction_from_node(self, node: Tag) -> Tuple[int, int]:
        for raw in (
            node.get("title"),
            node.get("aria-valuenow")
            and f"{node.get('aria-valuenow')}/{node.get('aria-valuemax') or ''}",
            node.get("data-value"),
            node.get_text(" ", strip=True),
        ):
            if not raw:
                continue
            m = RE_FRACTION.search(str(raw))
            if m:
                return int(m.group("cur")), int(m.group("max"))
        now, mx = node.get("aria-valuenow"), node.get("aria-valuemax")
        if now is not None and mx is not None:
            try:
                return int(float(str(now))), int(float(str(mx)))
            except ValueError:
                pass
        return 0, 0

    @staticmethod
    def _visible_text(soup: BeautifulSoup) -> str:
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)

    @staticmethod
    def _split_css(selectors: Sequence[str] | str) -> List[str]:
        if isinstance(selectors, str):
            parts = selectors.split(",")
        else:
            parts = []
            for item in selectors:
                parts.extend(item.split(","))
        result: List[str] = []
        seen: set[str] = set()
        for part in parts:
            sel = part.strip()
            if sel and sel not in seen:
                seen.add(sel)
                result.append(sel)
        return result

    async def _safe_content(self, page: Page) -> str:
        try:
            return await page.content()
        except PlaywrightError as exc:
            logger.warning("page.content() failed: %s", exc)
            return ""

    async def _safe_frame_html(self, frame: Frame) -> str:
        try:
            return await frame.content()
        except PlaywrightError as exc:
            logger.warning("frame.content() failed: %s — пробуем evaluate", exc)
            try:
                html = await frame.evaluate(
                    "() => document.documentElement.outerHTML"
                )
                return str(html or "")
            except PlaywrightError as exc2:
                logger.error("frame HTML недоступен: %s", exc2)
                return ""

    def reset_combo(self) -> None:
        """Сбрасывает историю комбинаций (между боями)."""
        self._combo_history.clear()
        self._strike_index = 0
        self._last_strike_fingerprint = ""
