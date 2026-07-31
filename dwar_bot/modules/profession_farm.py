"""
Автоматическая добыча ресурсов и прокачка добывающих профессий Dwar.

Сканирует узлы сбора в локации, собирает приоритетные ресурсы через
HumanBehavior (Bezier), следит за рюкзаком и передаёт контроль в
CombatEngine при нападении.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, DefaultDict, Dict, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from dwar_bot.config import BotConfig, config
from dwar_bot.core.anti_bot import HumanBehavior
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError
from dwar_bot.modules.combat_engine import CombatEngine
from dwar_bot.modules.stats_parser import BackpackItem, PlayerStats, StatsParser
from dwar_bot.modules.timers_manager import TIMER_POTION, TIMER_PROFESSION, TimersManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Маркеры / регулярки
# ---------------------------------------------------------------------------

RE_SUCCESS = re.compile(
    r"(?:вы\s+успешно\s+добыл|успешно\s+собран|добыто|собрали|получили|"
    r"вы\s+добыли|harvested|gathered|collected)",
    re.IGNORECASE,
)
RE_FAIL = re.compile(
    r"(?:сорвал(?:ся|ось)?|не\s+удалось|ресурс\s+исчез|уже\s+занят|"
    r"перехватил|кто[- ]то\s+собирает|занят\s+другим|failed|interrupted|"
    r"слишком\s+далеко|недостаточно\s+уровн)",
    re.IGNORECASE,
)
RE_BUSY = re.compile(
    r"(?:занят|собирает|in\s+use|busy|чужой|другой\s+игрок)",
    re.IGNORECASE,
)
RE_SKILL_REQ = re.compile(
    r"(?:треб(?:ует(?:ся)?|ование)|нужен\s+уровень|min(?:imum)?\s*(?:skill|lvl|level)|"
    r"ур(?:овень|\.)?\s*[:=]?\s*)(?P<lvl>\d+)",
    re.IGNORECASE,
)
RE_PROGRESS = re.compile(
    r"(?:добыч|сбор|подсеч|прогресс|gather|harvest|progress)",
    re.IGNORECASE,
)
RE_INVENTORY_FULL = re.compile(
    r"(?:рюкзак\s+полон|нет\s+места|переполнен|inventory\s+full|"
    r"не\s+хватает\s+места)",
    re.IGNORECASE,
)
RE_WAREHOUSE = re.compile(
    r"(?:склад|хранилище|почта|банк|сундук|warehouse|mail|storage|bank|chest)",
    re.IGNORECASE,
)

KNOWN_RESOURCES: Tuple[str, ...] = (
    "тайна",
    "омела",
    "аметист",
    "железо",
    "медь",
    "серебро",
    "золото",
    "трава",
    "корень",
    "руда",
    "камень",
    "кристалл",
    "древес",
    "шкура",
    "гриб",
    "цветок",
    "ягод",
)

RESOURCE_SELECTORS: Tuple[str, ...] = (
    "[data-resource]",
    "[data-node]",
    "[data-harvest]",
    "[data-gather]",
    ".resource",
    ".resource-node",
    ".harvest",
    ".gather-node",
    ".mine-node",
    ".herb-node",
    ".ore-node",
    "a[href*='harvest']",
    "a[href*='gather']",
    "a[href*='mine']",
    "a[href*='collect']",
    "a[href*='craft']",
    "area[href*='harvest']",
    "img[src*='herb']",
    "img[src*='ore']",
    "img[src*='mine']",
)

PROGRESS_SELECTORS: Tuple[str, ...] = (
    ".progress",
    ".progress-bar",
    "#progress",
    ".gather-progress",
    ".harvest-bar",
    "[data-progress]",
    ".cooldown",
    ".timer-bar",
    "progress",
)

LOCATION_FRAME_NAMES: Tuple[str, ...] = (
    "main",
    "location",
    "map",
    "game",
    "pers",
    "user",
)

DEFAULT_BACKPACK_LIMIT = 40
DEFAULT_HARVEST_TIMEOUT = 45.0


@dataclass(slots=True)
class ResourceNode:
    """Узел добычи ресурса на локации."""

    name: str
    element_id: str
    min_skill_level: int = 0
    is_available: bool = True
    css_hint: str = ""
    tooltip: str = ""
    href: str = ""
    distance_hint: float = 0.0  # условный приоритет (меньше — ближе/лучше)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FarmStats:
    """Статистика фарм-сессии."""

    collected_count: Dict[str, int] = field(default_factory=dict)
    failed_attempts: int = 0
    skill_level: int = 0
    started_at: float = field(default_factory=time.time)
    last_success_at: float = 0.0
    intercepted: int = 0
    inventory_blocks: int = 0
    combat_interrupts: int = 0

    @property
    def total_collected(self) -> int:
        return sum(self.collected_count.values())

    @property
    def elapsed_hours(self) -> float:
        return max(1e-6, (time.time() - self.started_at) / 3600.0)

    @property
    def rate_per_hour(self) -> float:
        return self.total_collected / self.elapsed_hours

    def record_success(self, resource_name: str) -> None:
        key = resource_name.strip() or "unknown"
        self.collected_count[key] = self.collected_count.get(key, 0) + 1
        self.last_success_at = time.time()

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["total_collected"] = self.total_collected
        data["rate_per_hour"] = round(self.rate_per_hour, 2)
        return data

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in sorted(self.collected_count.items())]
        return (
            f"farm: total={self.total_collected} ({self.rate_per_hour:.1f}/h) "
            f"fail={self.failed_attempts} intercept={self.intercepted} "
            f"skill={self.skill_level} [{', '.join(parts) or '-'}]"
        )


class ProfessionFarmError(Exception):
    """Ошибка модуля добычи."""


class ProfessionFarm:
    """
    Цикл добычи ресурсов добывающих профессий.

    Приоритет: бой > полный рюкзак > сбор target_resources > прочие узлы.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        browser: Optional[BrowserEngine] = None,
        human: Optional[HumanBehavior] = None,
        stats_parser: Optional[StatsParser] = None,
        combat_engine: Optional[CombatEngine] = None,
        timers: Optional[TimersManager] = None,
        backpack_limit: int = DEFAULT_BACKPACK_LIMIT,
        harvest_timeout_sec: float = DEFAULT_HARVEST_TIMEOUT,
        skill_level: int = 0,
    ) -> None:
        self._config = bot_config or config
        self._browser = browser
        self._human = human or (
            browser.human if browser is not None else HumanBehavior(self._config)
        )
        self._stats_parser = stats_parser or StatsParser(self._config)
        self._combat = combat_engine or CombatEngine(
            self._config,
            browser=browser,
            stats_parser=self._stats_parser,
        )
        self._timers = timers or TimersManager(self._config)
        self._backpack_limit = backpack_limit
        self._harvest_timeout = harvest_timeout_sec

        self.stats = FarmStats(skill_level=skill_level)
        self._stop_event = asyncio.Event()
        self._paused = False
        self._last_nodes: List[ResourceNode] = []
        self._last_log_at = 0.0

    # ------------------------------------------------------------------
    # Управление циклом
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop_event.set()

    def pause(self) -> None:
        self._paused = True
        logger.warning("ProfessionFarm на паузе")

    def resume(self) -> None:
        self._paused = False
        logger.info("ProfessionFarm возобновлён")

    def set_skill_level(self, level: int) -> None:
        self.stats.skill_level = max(0, int(level))

    # ------------------------------------------------------------------
    # Сканирование
    # ------------------------------------------------------------------

    async def scan_location_resources(self, page: Page) -> List[ResourceNode]:
        """
        Сканирует main/location фрейм на узлы добычи.

        Фильтрует по ``stats.skill_level`` и доступности.
        """
        try:
            await self._handle_combat_if_any(page)

            frame = await self._resolve_location_frame(page)
            html = await self._frame_html(page, frame)
            if not html.strip():
                logger.warning("Пустой HTML локации при скане ресурсов")
                return list(self._last_nodes)

            soup = BeautifulSoup(html, "html.parser")
            nodes = self._extract_nodes_from_soup(soup)

            if frame is not None:
                pw_nodes = await self._extract_nodes_via_playwright(frame)
                nodes = self._merge_nodes(nodes, pw_nodes)

            # Фильтр по мастерству
            skill = self.stats.skill_level
            filtered = [
                n
                for n in nodes
                if n.min_skill_level <= skill
            ]
            skipped = len(nodes) - len(filtered)
            if skipped:
                logger.debug(
                    "Отфильтровано %s узлов выше skill=%s", skipped, skill
                )

            self._last_nodes = filtered
            logger.info(
                "Найдено ресурсов: %s (доступных: %s)",
                len(filtered),
                sum(1 for n in filtered if n.is_available),
            )
            return list(filtered)

        except Exception as exc:
            logger.error("scan_location_resources: %s", exc, exc_info=True)
            return list(self._last_nodes)

    def _extract_nodes_from_soup(self, soup: BeautifulSoup) -> List[ResourceNode]:
        nodes: List[ResourceNode] = []
        seen: set[str] = set()
        seen_ids: set[int] = set()
        candidates: List[Tag] = []

        for selector in RESOURCE_SELECTORS:
            try:
                candidates.extend(soup.select(selector))
            except Exception:
                continue

        # Дополнительно: ссылки/area с именами известных ресурсов
        for tag in soup.find_all(["a", "area", "div", "span", "img", "button"]):
            blob = self._node_blob(tag).lower()
            if any(name in blob for name in KNOWN_RESOURCES):
                candidates.append(tag)

        for index, tag in enumerate(candidates):
            node_id = id(tag)
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            node = self._node_from_tag(tag, fallback_index=index)
            if not node.name:
                continue
            key = f"{node.name.lower()}|{node.href}|{node.element_id}"
            # Нормализованный ключ без суффикса node_/pw_
            norm_id = node.element_id
            if norm_id.startswith(("node_", "pw_")):
                norm_id = ""
            soft_key = f"{node.name.lower()}|{node.href}|{norm_id}"
            if key in seen or soft_key in seen:
                continue
            seen.add(key)
            seen.add(soft_key)
            nodes.append(node)

        return nodes

    def _node_from_tag(self, tag: Tag, *, fallback_index: int) -> ResourceNode:
        blob = self._node_blob(tag)
        title = (
            tag.get("title")
            or tag.get("data-title")
            or tag.get("alt")
            or tag.get("data-name")
            or ""
        )
        text = tag.get_text(" ", strip=True)
        name = (title or text or tag.get("data-resource") or "").strip()
        name = re.sub(r"\s+", " ", name)[:80]

        element_id = str(
            tag.get("data-resource-id")
            or tag.get("data-node-id")
            or tag.get("data-id")
            or tag.get("id")
            or tag.get("name")
            or f"node_{fallback_index}"
        )
        href = str(tag.get("href") or tag.get("data-href") or "")
        tooltip = str(title or tag.get("data-tooltip") or blob)[:400]

        min_skill = 0
        skill_m = RE_SKILL_REQ.search(tooltip) or RE_SKILL_REQ.search(blob)
        if skill_m:
            min_skill = int(skill_m.group("lvl"))
        raw_skill = tag.get("data-min-skill") or tag.get("data-skill")
        if raw_skill is not None:
            try:
                min_skill = int(re.search(r"\d+", str(raw_skill)).group(0))  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass

        is_available = True
        classes = " ".join(
            tag.get("class") if isinstance(tag.get("class"), list) else [str(tag.get("class") or "")]
        ).lower()
        if (
            RE_BUSY.search(tooltip)
            or RE_BUSY.search(classes)
            or "disabled" in classes
            or "busy" in classes
            or "occupied" in classes
            or tag.get("disabled") is not None
            or tag.get("data-busy") in {"1", "true", "yes"}
        ):
            is_available = False

        css_hint = ""
        if tag.get("id"):
            css_hint = f"#{tag.get('id')}"
        elif element_id and not element_id.startswith("node_"):
            css_hint = f"[data-id='{element_id}'], [data-resource-id='{element_id}']"
        elif href:
            css_hint = f"a[href='{href}']"

        return ResourceNode(
            name=name or "resource",
            element_id=element_id,
            min_skill_level=min_skill,
            is_available=is_available,
            css_hint=css_hint,
            tooltip=tooltip,
            href=href,
            distance_hint=float(fallback_index),
        )

    async def _extract_nodes_via_playwright(
        self, frame: Frame
    ) -> List[ResourceNode]:
        nodes: List[ResourceNode] = []
        for selector in RESOURCE_SELECTORS:
            try:
                locator = frame.locator(selector)
                count = await locator.count()
            except PlaywrightError:
                continue

            for i in range(min(count, 40)):
                try:
                    loc = locator.nth(i)
                    if not await loc.is_visible():
                        continue
                    name = (
                        await loc.get_attribute("title")
                        or await loc.get_attribute("data-name")
                        or await loc.get_attribute("alt")
                        or (await loc.inner_text(timeout=800)).strip()
                        or await loc.get_attribute("data-resource")
                        or f"resource_{i}"
                    )
                    name = re.sub(r"\s+", " ", name)[:80]
                    element_id = (
                        await loc.get_attribute("data-resource-id")
                        or await loc.get_attribute("data-id")
                        or await loc.get_attribute("id")
                        or f"pw_{i}"
                    )
                    href = await loc.get_attribute("href") or ""
                    tooltip = (
                        await loc.get_attribute("title")
                        or await loc.get_attribute("data-tooltip")
                        or name
                    )
                    min_skill = 0
                    m = RE_SKILL_REQ.search(tooltip or "")
                    if m:
                        min_skill = int(m.group("lvl"))
                    busy_attr = await loc.get_attribute("data-busy")
                    class_attr = (await loc.get_attribute("class")) or ""
                    is_available = not (
                        busy_attr in {"1", "true", "yes"}
                        or RE_BUSY.search(class_attr)
                        or RE_BUSY.search(tooltip or "")
                    )
                    nodes.append(
                        ResourceNode(
                            name=name,
                            element_id=str(element_id),
                            min_skill_level=min_skill,
                            is_available=is_available,
                            css_hint=selector,
                            tooltip=(tooltip or "")[:400],
                            href=href,
                            distance_hint=float(i),
                        )
                    )
                except (PlaywrightError, PlaywrightTimeoutError):
                    continue
        return nodes

    @staticmethod
    def _merge_nodes(
        primary: List[ResourceNode], secondary: List[ResourceNode]
    ) -> List[ResourceNode]:
        merged = list(primary)
        seen = {f"{n.name.lower()}|{n.element_id}|{n.href}" for n in primary}
        for node in secondary:
            key = f"{node.name.lower()}|{node.element_id}|{node.href}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(node)
        return merged

    @staticmethod
    def _node_blob(tag: Tag) -> str:
        parts = [
            tag.get_text(" ", strip=True),
            str(tag.get("title") or ""),
            str(tag.get("alt") or ""),
            str(tag.get("href") or ""),
            str(tag.get("data-resource") or ""),
            str(tag.get("data-name") or ""),
            str(tag.get("onclick") or ""),
            " ".join(
                tag.get("class")
                if isinstance(tag.get("class"), list)
                else [str(tag.get("class") or "")]
            ),
        ]
        return " ".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Сбор
    # ------------------------------------------------------------------

    async def harvest_resource(self, page: Page, node: ResourceNode) -> bool:
        """
        Кликает по узлу через Bezier и ждёт успех/срыв добычи.
        """
        if not node.is_available:
            logger.info("Узел '%s' недоступен — пропуск", node.name)
            return False

        if not self._timers.is_ready(TIMER_PROFESSION):
            logger.debug(
                "Profession cooldown ещё %.1f сек",
                self._timers.remaining(TIMER_PROFESSION),
            )
            return False

        # Бой до клика
        if await self._handle_combat_if_any(page):
            return False

        logger.info(
            "Сбор '%s' (skill_req=%s, id=%s)",
            node.name,
            node.min_skill_level,
            node.element_id,
        )

        before_notes = await self._recent_chat_blob(page)
        clicked = await self._click_resource_node(page, node)
        if not clicked:
            logger.warning("Не удалось кликнуть по '%s'", node.name)
            self.stats.failed_attempts += 1
            return False

        # Короткая проверка: узел могли перехватить
        await asyncio.sleep(random.uniform(0.35, 0.8))
        if await self._was_intercepted(page, before_notes):
            logger.warning("Ресурс '%s' перехвачен другим игроком", node.name)
            self.stats.intercepted += 1
            self.stats.failed_attempts += 1
            return False

        outcome = await self._wait_harvest_outcome(page, node, before_notes)
        if outcome == "success":
            self.stats.record_success(node.name)
            self._timers.set_cooldown(
                TIMER_PROFESSION, random.uniform(1.5, 3.5)
            )
            logger.info(
                "Успешно добыто '%s' | %s",
                node.name,
                self.stats.summary(),
            )
            return True

        if outcome == "combat":
            self.stats.combat_interrupts += 1
            await self._handle_combat_if_any(page)
            self.stats.failed_attempts += 1
            return False

        if outcome == "intercepted":
            self.stats.intercepted += 1
            logger.warning("Срыв/перехват при сборе '%s'", node.name)
        else:
            logger.warning("Сбор '%s' не удался (outcome=%s)", node.name, outcome)

        self.stats.failed_attempts += 1
        self._timers.set_cooldown(TIMER_PROFESSION, random.uniform(0.8, 1.8))
        return False

    async def _click_resource_node(self, page: Page, node: ResourceNode) -> bool:
        frame = await self._resolve_location_frame(page)
        selectors = self._node_selectors(node)

        for selector in selectors:
            try:
                owner: Any = frame or page
                # Bezier к селектору (CSS без :has-text)
                if ":has-text" not in selector and not selector.startswith("text="):
                    try:
                        await self._human.bezier_mouse_move(
                            page,
                            selector,
                            frame=frame,
                            timeout_ms=5_000,
                        )
                        await asyncio.sleep(random.uniform(0.08, 0.28))
                        await page.mouse.down()
                        await asyncio.sleep(random.uniform(0.04, 0.11))
                        await page.mouse.up()
                        return True
                    except Exception as exc:
                        logger.debug("Bezier click %s: %s", selector, exc)

                if self._browser is not None:
                    try:
                        await self._browser.human_click(
                            selector, page=page, frame=frame, timeout_ms=5_000
                        )
                        return True
                    except (BrowserEngineError, PlaywrightError) as exc:
                        logger.debug("human_click %s: %s", selector, exc)

                handle = await owner.query_selector(selector)
                if handle is not None and await handle.is_visible():
                    await handle.click(timeout=5_000)
                    return True

                loc = owner.locator(selector)
                if await loc.count() > 0:
                    await loc.first.click(timeout=5_000)
                    return True
            except (PlaywrightError, PlaywrightTimeoutError, RuntimeError) as exc:
                logger.debug("click attempt %s failed: %s", selector, exc)
                continue

        # Фоллбек по тексту имени
        if node.name:
            short = node.name[:40].replace("'", "\\'")
            for selector in (f"a:has-text('{short}')", f"text={short}"):
                try:
                    owner = frame or page
                    loc = owner.locator(selector)
                    if await loc.count() == 0:
                        continue
                    if self._browser is not None:
                        handle = await loc.first.element_handle(timeout=3_000)
                        if handle is not None:
                            await self._browser.human_click(
                                handle, page=page, frame=frame
                            )
                            return True
                    await loc.first.click(timeout=5_000)
                    return True
                except (BrowserEngineError, PlaywrightError):
                    continue
        return False

    def _node_selectors(self, node: ResourceNode) -> List[str]:
        selectors: List[str] = []
        if node.css_hint:
            selectors.append(node.css_hint)
        if node.element_id and not node.element_id.startswith(("node_", "pw_")):
            eid = node.element_id.replace("'", "")
            selectors.extend(
                [
                    f"#{eid}",
                    f"[data-resource-id='{eid}']",
                    f"[data-node-id='{eid}']",
                    f"[data-id='{eid}']",
                    f"[id='{eid}']",
                ]
            )
        if node.href:
            href = node.href.replace("'", "")
            selectors.append(f"a[href='{href}']")
            if "?" in href:
                selectors.append(f"a[href*='{href.split('?')[0]}']")
        if node.name:
            short = node.name[:32].replace("'", "")
            selectors.append(f"[title*='{short}']")
            selectors.append(f"[data-name*='{short}']")
        # уникальные
        seen: set[str] = set()
        unique: List[str] = []
        for sel in selectors:
            if sel and sel not in seen:
                seen.add(sel)
                unique.append(sel)
        return unique

    async def _wait_harvest_outcome(
        self,
        page: Page,
        node: ResourceNode,
        before_notes: str,
    ) -> str:
        """
        Ждёт прогресс-бар / сообщение чата.

        Returns: success | fail | intercepted | combat | timeout
        """
        deadline = time.monotonic() + self._harvest_timeout
        saw_progress = False

        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return "fail"

            # Бой прерывает сбор
            try:
                combat = await self._combat.parse_combat_state(page)
                if combat.in_combat:
                    logger.warning("Сбор прерван боем!")
                    return "combat"
            except Exception:
                pass

            # Прогресс-бар
            if await self._progress_visible(page):
                saw_progress = True
                await asyncio.sleep(random.uniform(0.4, 0.9))
                continue

            blob = await self._recent_chat_blob(page)
            delta = blob[len(before_notes) :] if blob.startswith(before_notes) else blob

            if RE_SUCCESS.search(delta) or RE_SUCCESS.search(blob[-500:]):
                return "success"
            if RE_BUSY.search(delta) or (
                RE_FAIL.search(delta) and RE_BUSY.search(delta + blob[-300:])
            ):
                return "intercepted"
            if RE_FAIL.search(delta) or RE_FAIL.search(blob[-500:]):
                return "fail"

            # Прогресс пропал без явного сообщения — считаем успехом, если видели бар
            if saw_progress and not await self._progress_visible(page):
                await asyncio.sleep(random.uniform(0.3, 0.6))
                blob2 = await self._recent_chat_blob(page)
                if RE_FAIL.search(blob2[-400:]) or RE_BUSY.search(blob2[-400:]):
                    return "intercepted" if RE_BUSY.search(blob2[-400:]) else "fail"
                if RE_SUCCESS.search(blob2[-400:]) or True:
                    # Нет явного фейла после исчезновения бара — успех
                    return "success"

            await asyncio.sleep(random.uniform(0.35, 0.8))

        logger.warning(
            "Таймаут ожидания добычи '%s' (%.0f сек)",
            node.name,
            self._harvest_timeout,
        )
        return "timeout"

    async def _progress_visible(self, page: Page) -> bool:
        frame = await self._resolve_location_frame(page)
        owners: List[Any] = [page]
        if frame is not None:
            owners.insert(0, frame)
        for owner in owners:
            for selector in PROGRESS_SELECTORS:
                try:
                    handle = await owner.query_selector(selector)
                    if handle is not None and await handle.is_visible():
                        return True
                except PlaywrightError:
                    continue
        return False

    async def _was_intercepted(self, page: Page, before_notes: str) -> bool:
        blob = await self._recent_chat_blob(page)
        delta = blob[len(before_notes) :] if blob.startswith(before_notes) else blob[-400:]
        return bool(RE_BUSY.search(delta) or RE_FAIL.search(delta) and RE_BUSY.search(blob))

    async def _recent_chat_blob(self, page: Page) -> str:
        parts: List[str] = []
        try:
            notes = await self._stats_parser.parse_notifications(page)
            parts.extend(n.text for n in notes[-20:])
        except Exception as exc:
            logger.debug("notifications for harvest: %s", exc)

        try:
            for frame in page.frames:
                name = (frame.name or "").lower()
                if name in {"chat", "syschat", "main", "location"} or "chat" in (
                    frame.url or ""
                ).lower():
                    try:
                        text = await frame.inner_text("body")
                        if text:
                            parts.append(text[-1500:])
                    except PlaywrightError:
                        continue
        except PlaywrightError:
            pass

        try:
            parts.append((await page.content())[-2000:])
        except PlaywrightError:
            pass

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Рюкзак / склад
    # ------------------------------------------------------------------

    async def handle_inventory_full(
        self, page: Page, stats: PlayerStats
    ) -> bool:
        """
        Проверяет место в рюкзаке; при переполнении ищет склад/почту
        или ставит фарм на паузу с Telegram-уведомлением.

        Returns:
            True если можно продолжать сбор, False если места нет.
        """
        try:
            notes = await self._stats_parser.parse_notifications(page)
            for note in notes:
                if RE_INVENTORY_FULL.search(note.text):
                    logger.error("Уведомление: рюкзак полон — %s", note.text)
                    self.stats.inventory_blocks += 1
                    return await self._try_dump_or_pause(page)

            backpack = await self._stats_parser.parse_backpack(page)
            free_slots = self._backpack_limit - len(backpack)
            if free_slots <= 0:
                logger.error(
                    "Рюкзак полон: %s/%s предметов",
                    len(backpack),
                    self._backpack_limit,
                )
                self.stats.inventory_blocks += 1
                return await self._try_dump_or_pause(page)

            if free_slots <= 2:
                logger.warning("Мало места в рюкзаке: свободно ~%s", free_slots)

            return True
        except Exception as exc:
            logger.warning(
                "Не удалось проверить рюкзак (%s) — продолжаем осторожно",
                exc,
            )
            return True

    async def _try_dump_or_pause(self, page: Page) -> bool:
        dumped = await self._try_open_warehouse(page)
        if dumped:
            await asyncio.sleep(random.uniform(1.0, 2.0))
            # После склада — повторная проверка
            try:
                backpack = await self._stats_parser.parse_backpack(page)
                if len(backpack) < self._backpack_limit:
                    logger.info("После склада место освободилось")
                    return True
            except Exception:
                pass

        self.pause()
        logger.critical(
            "Фарм на паузе: рюкзак полон. Освободите инвентарь и вызовите resume()."
        )
        try:
            from dwar_bot.logger import notify_telegram

            await notify_telegram(
                "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Рюкзак полон во время фарма!\n"
                f"{self.stats.summary()}",
                critical=True,
            )
        except Exception as exc:
            logger.debug("telegram notify: %s", exc)
        return False

    async def _try_open_warehouse(self, page: Page) -> bool:
        """Пытается кликнуть по складу/почте/сундуку в локации."""
        frame = await self._resolve_location_frame(page)
        html = await self._frame_html(page, frame)
        soup = BeautifulSoup(html, "html.parser")

        target: Optional[Tag] = None
        for tag in soup.find_all(["a", "button", "div", "span"]):
            blob = self._node_blob(tag)
            if RE_WAREHOUSE.search(blob):
                target = tag
                break

        if target is None:
            logger.warning("Склад/почта в локации не найдены")
            return False

        name = target.get_text(" ", strip=True) or "warehouse"
        href = str(target.get("href") or "")
        css = f"#{target.get('id')}" if target.get("id") else ""
        selectors = [s for s in (css, f"a[href='{href}']" if href else "", f"a:has-text('{name[:30]}')") if s]

        for selector in selectors:
            try:
                if self._browser is not None and ":has-text" not in selector:
                    await self._browser.human_click(
                        selector, page=page, frame=frame, timeout_ms=5_000
                    )
                    logger.info("Открыт объект склада: %s", name)
                    return True
                owner: Any = frame or page
                loc = owner.locator(selector)
                if await loc.count() > 0:
                    await loc.first.click(timeout=5_000)
                    logger.info("Открыт объект склада: %s", name)
                    return True
            except (BrowserEngineError, PlaywrightError) as exc:
                logger.debug("warehouse click %s: %s", selector, exc)
        return False

    # ------------------------------------------------------------------
    # Главный цикл
    # ------------------------------------------------------------------

    async def start_farm_loop(
        self,
        page: Page,
        target_resources: List[str],
        *,
        max_iterations: Optional[int] = None,
        idle_when_empty_sec: Tuple[float, float] = (2.5, 6.0),
    ) -> None:
        """
        Непрерывный фарм приоритетных ресурсов из ``target_resources``.

        Останавливается по ``request_stop()`` или ``max_iterations``.
        """
        targets = [t.strip().lower() for t in target_resources if t.strip()]
        self._stop_event.clear()
        self.stats.started_at = time.time()
        iteration = 0

        logger.info(
            "Старт farm_loop: targets=%s skill=%s",
            targets or ["*"],
            self.stats.skill_level,
        )

        while not self._stop_event.is_set():
            if max_iterations is not None and iteration >= max_iterations:
                logger.info("farm_loop: достигнут max_iterations=%s", max_iterations)
                break

            iteration += 1

            # Пауза (рюкзак / ручная)
            while self._paused and not self._stop_event.is_set():
                await asyncio.sleep(1.5)

            if self._stop_event.is_set():
                break

            try:
                # 1) Бой — высший приоритет
                if await self._handle_combat_if_any(page):
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    continue

                # 2) Статы + рюкзак
                stats = await self._stats_parser.parse_player_stats(page)
                if stats.hp_max > 0 and stats.hp_ratio < 0.4:
                    backpack = await self._stats_parser.parse_backpack(page)
                    await self._combat.use_potion_if_needed(
                        page, stats, backpack, hp_threshold_pct=40.0
                    )

                can_farm = await self.handle_inventory_full(page, stats)
                if not can_farm:
                    await asyncio.sleep(random.uniform(3.0, 6.0))
                    continue

                # 3) Скан узлов
                await self._human.random_idle(page, chance=0.08)
                nodes = await self.scan_location_resources(page)
                prioritized = self._prioritize_nodes(nodes, targets)

                if not prioritized:
                    pause = random.uniform(*idle_when_empty_sec)
                    logger.debug(
                        "Нет доступных узлов — пауза %.1f сек", pause
                    )
                    await asyncio.sleep(pause)
                    await self._maybe_log_stats(force=False)
                    continue

                # 4) Сбор лучшего узла
                node = prioritized[0]
                # Микро-пауза реакции человека перед кликом
                await asyncio.sleep(random.uniform(0.35, 1.1))
                await self.harvest_resource(page, node)

                # Пауза между сборами
                await asyncio.sleep(random.uniform(0.6, 1.8))
                await self._human.random_idle(page, chance=0.12)
                await self._maybe_log_stats(force=False)

            except asyncio.CancelledError:
                logger.info("farm_loop cancelled")
                raise
            except Exception as exc:
                logger.error("Ошибка farm_loop: %s", exc, exc_info=True)
                await asyncio.sleep(random.uniform(2.0, 5.0))

        await self._maybe_log_stats(force=True)
        logger.info("farm_loop завершён | %s", self.stats.summary())

    def _prioritize_nodes(
        self, nodes: Sequence[ResourceNode], targets: Sequence[str]
    ) -> List[ResourceNode]:
        available = [n for n in nodes if n.is_available]
        if not available:
            return []

        def score(node: ResourceNode) -> Tuple[int, float, str]:
            name_l = node.name.lower()
            # 0 = точное попадание в target, 1 = частичное, 2 = прочее
            prio = 2
            if targets:
                for idx, target in enumerate(targets):
                    if target == name_l or target in name_l or name_l in target:
                        prio = 0 if target == name_l else 1
                        # Более ранний target — выше приоритет
                        return (prio, float(idx) + node.distance_hint, name_l)
                # Не в списке целей — в конец
                return (3, node.distance_hint, name_l)
            return (0, node.distance_hint, name_l)

        return sorted(available, key=score)

    async def _maybe_log_stats(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_log_at < 60.0:
            return
        self._last_log_at = now
        logger.info("Статистика добычи: %s", self.stats.summary())

    # ------------------------------------------------------------------
    # Бой
    # ------------------------------------------------------------------

    async def _handle_combat_if_any(self, page: Page) -> bool:
        try:
            state = await self._combat.parse_combat_state(page)
        except Exception as exc:
            logger.debug("combat probe: %s", exc)
            return False

        if not state.in_combat:
            # Дублирующая проверка через статы
            try:
                stats = await self._stats_parser.parse_player_stats(page)
                if not stats.in_combat:
                    return False
            except Exception:
                return False

        logger.warning(
            "Нападение во время фарма (%s) — CombatEngine",
            state.enemy_name or "?",
        )
        self.stats.combat_interrupts += 1
        self._combat.reset_combo()
        won = await self._combat.process_fight(page, hp_threshold_pct=40.0)
        logger.info("Бой после фарма: %s", "победа" if won else "поражение/сбой")
        await asyncio.sleep(random.uniform(0.8, 1.6))
        return True

    # ------------------------------------------------------------------
    # Фреймы / утилиты
    # ------------------------------------------------------------------

    async def _resolve_location_frame(self, page: Page) -> Optional[Frame]:
        for frame in page.frames:
            name = (frame.name or "").lower()
            if name in LOCATION_FRAME_NAMES:
                return frame
            url = (frame.url or "").lower()
            if any(n in url for n in LOCATION_FRAME_NAMES):
                return frame

        css = (
            f"{self._config.selectors.main_frame}, "
            "frame[name='location'], iframe[name='location'], #location_frame"
        )
        for selector in [p.strip() for p in css.split(",") if p.strip()]:
            try:
                handle = await page.query_selector(selector)
                if handle is None:
                    continue
                content = await handle.content_frame()
                if content is not None:
                    return content
            except PlaywrightError:
                continue
        return None

    async def _frame_html(self, page: Page, frame: Optional[Frame]) -> str:
        if frame is not None:
            try:
                return await frame.content()
            except PlaywrightError:
                try:
                    return str(
                        await frame.evaluate(
                            "() => document.documentElement.outerHTML"
                        )
                        or ""
                    )
                except PlaywrightError as exc:
                    logger.debug("frame html: %s", exc)
        try:
            return await page.content()
        except PlaywrightError as exc:
            logger.error("page.content: %s", exc)
            return ""
