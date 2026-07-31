"""
Трекер квестов и диалогов NPC для «Легенда: Наследие Драконов».

Парсит диалоговый фрейм, выбирает реплики через human_click, перемещает
персонажа по локациям и выполняет сценарии квестов. При появлении боя
временно передаёт управление CombatEngine.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from playwright.async_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from dwar_bot.config import BotConfig, config
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError
from dwar_bot.modules.combat_engine import CombatEngine
from dwar_bot.modules.stats_parser import BackpackItem, StatsParser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Регулярки и маркеры
# ---------------------------------------------------------------------------

RE_QUEST_MARKERS = re.compile(
    r"(?:квест|задание|миссия|quest|награда|сдать|заверш|принять|получить\s+задан)",
    re.IGNORECASE,
)
RE_DIALOG_CONTINUE = re.compile(
    r"(?:далее|продолж|слушать|дальше|next|continue|>>>|\.\.\.)",
    re.IGNORECASE,
)
RE_TRAVEL_TIME = re.compile(
    r"(?:через|осталось|переход|идти|скакать|верхом|пешком)?\s*"
    r"(?P<min>\d+)\s*(?:мин|m|:)(?:\s*(?P<sec>\d+)\s*(?:сек|s)?)?"
    r"|(?P<sec_only>\d+)\s*(?:сек|sec|s)\b",
    re.IGNORECASE,
)
RE_TRAVEL_DONE = re.compile(
    r"(?:прибыл|дош[её]л|вы\s+на месте|локаци[яи]\s+изменен|arrived)",
    re.IGNORECASE,
)
RE_BACKPACK_FULL = re.compile(
    r"(?:рюкзак\s+полон|нет\s+места|переполнен|inventory\s+full|не\s+хватает\s+места)",
    re.IGNORECASE,
)
RE_NPC_LINK = re.compile(
    r"(?:npc|talk|speak|dialog|говор|поговор)",
    re.IGNORECASE,
)
RE_LOCATION_LINK = re.compile(
    r"(?:loc|location|area|zone|go|move|перейт|идти|войти|выйти)",
    re.IGNORECASE,
)

MAIN_FRAME_NAMES: Tuple[str, ...] = (
    "main",
    "user",
    "pers",
    "game",
    "location",
    "map",
)
NAV_FRAME_NAMES: Tuple[str, ...] = ("menu", "nav", "map", "location", "main")

DEFAULT_BACKPACK_SOFT_LIMIT = 40


class QuestStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    READY_TO_COMPLETE = "ready_to_complete"
    FAILED = "failed"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class NPCDialogOption:
    """Вариант ответа / реплики в диалоге с NPC."""

    text: str
    option_id: str = ""
    url: str = ""
    is_quest_trigger: bool = False
    index: int = -1
    css_hint: str = ""

    @property
    def action_target(self) -> str:
        return self.option_id or self.url or self.css_hint

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class QuestState:
    """Состояние активного квеста."""

    title: str = ""
    current_step: str = ""
    status: QuestStatus = QuestStatus.UNKNOWN
    quest_id: str = ""
    stale: bool = False
    parsed_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(slots=True)
class LocationExit:
    """Переход в другую локацию."""

    name: str
    location_id: str
    url: str = ""
    travel_seconds: float = 0.0
    css_hint: str = ""


class QuestTrackerError(Exception):
    """Ошибка трекера квестов."""


class QuestTracker:
    """
    Навигация по квестам: диалоги NPC, переходы по локациям, сценарии.

    ``quest_script`` — список шагов вида::

        [
            {"loc": "town_square", "npc": "elder", "choose_option": 2},
            {"loc": "forest", "npc": "hunter", "choose_text": "Принять задание"},
            {"action": "turn_in", "npc": "elder", "choose_option": 0},
        ]
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        browser: Optional[BrowserEngine] = None,
        combat_engine: Optional[CombatEngine] = None,
        stats_parser: Optional[StatsParser] = None,
        backpack_soft_limit: int = DEFAULT_BACKPACK_SOFT_LIMIT,
        location_retry: int = 3,
        travel_timeout_sec: float = 180.0,
    ) -> None:
        self._config = bot_config or config
        self._selectors = self._config.selectors
        self._browser = browser
        self._stats_parser = stats_parser or StatsParser(self._config)
        self._combat = combat_engine or CombatEngine(
            self._config, browser=browser, stats_parser=self._stats_parser
        )
        self._backpack_soft_limit = backpack_soft_limit
        self._location_retry = location_retry
        self._travel_timeout_sec = travel_timeout_sec
        self._last_dialog_text: str = ""
        self._last_options: List[NPCDialogOption] = []
        self._current_location_id: str = ""

    # ------------------------------------------------------------------
    # Диалоги
    # ------------------------------------------------------------------

    async def parse_dialog_frame(
        self, page: Page
    ) -> Tuple[str, List[NPCDialogOption]]:
        """
        Сканирует main_frame при разговоре с NPC.

        Returns:
            (текст NPC, список вариантов ответов)
        """
        try:
            await self._handle_combat_if_any(page)

            frame = await self._resolve_main_frame(page)
            html = await self._frame_or_page_html(page, frame)
            if not html.strip():
                logger.warning("Пустой HTML диалогового фрейма")
                return self._last_dialog_text, list(self._last_options)

            soup = BeautifulSoup(html, "html.parser")
            npc_text = self._extract_npc_text(soup)
            options = self._extract_dialog_options(soup)

            # Playwright-дополнение для динамических кнопок
            if frame is not None:
                pw_options = await self._extract_options_via_playwright(frame)
                options = self._merge_options(options, pw_options)

            self._last_dialog_text = npc_text
            self._last_options = options
            logger.info(
                "Диалог NPC (%s симв.), вариантов: %s (квестовых: %s)",
                len(npc_text),
                len(options),
                sum(1 for o in options if o.is_quest_trigger),
            )
            return npc_text, options

        except Exception as exc:
            logger.error("Ошибка parse_dialog_frame: %s", exc, exc_info=True)
            return self._last_dialog_text, list(self._last_options)

    async def select_dialog_option(
        self, page: Page, option: NPCDialogOption
    ) -> bool:
        """
        Нажимает выбранную реплику через human_click с паузой чтения.
        """
        read_pause = self._reading_delay(option.text, self._last_dialog_text)
        logger.info(
            "Выбор реплики [%s]: %r (quest=%s), пауза чтения %.2f сек",
            option.index,
            option.text[:80],
            option.is_quest_trigger,
            read_pause,
        )
        await asyncio.sleep(read_pause)

        # Перед сдачей/получением квеста — проверка рюкзака
        if option.is_quest_trigger:
            ok_space = await self._ensure_backpack_space(page)
            if not ok_space:
                logger.error(
                    "Рюкзак переполнен — нельзя получить/сдать квестовую награду"
                )
                return False

        before_text = self._last_dialog_text
        before_opts = [o.text for o in self._last_options]

        frame = await self._resolve_main_frame(page)
        clicked = await self._click_dialog_option(page, frame, option)
        if not clicked:
            logger.error("Не удалось кликнуть реплику: %r", option.text)
            return False

        await asyncio.sleep(random.uniform(0.6, 1.4))
        await self._handle_combat_if_any(page)

        new_text, new_opts = await self.parse_dialog_frame(page)
        changed = (
            new_text != before_text
            or [o.text for o in new_opts] != before_opts
            or not new_opts
        )
        if changed:
            logger.info("Реплика применена, диалог обновлён")
        else:
            logger.warning("После клика диалог не изменился заметно")
        return True

    # ------------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------------

    async def navigate_to_location(
        self, page: Page, target_location_id: str
    ) -> bool:
        """
        Находит переход в целевую локацию и выполняет перемещение,
        ожидая таймер перехода (пешком / верхом).
        """
        target = (target_location_id or "").strip()
        if not target:
            logger.error("navigate_to_location: пустой target_location_id")
            return False

        if self._location_matches(self._current_location_id, target):
            logger.info("Уже в локации %s", target)
            return True

        for attempt in range(1, self._location_retry + 1):
            try:
                await self._handle_combat_if_any(page)
                exit_link = await self._find_location_exit(page, target)
                if exit_link is None:
                    logger.warning(
                        "Переход в '%s' не найден (попытка %s/%s) — "
                        "переоткрываем локацию",
                        target,
                        attempt,
                        self._location_retry,
                    )
                    await self._reopen_location(page)
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue

                logger.info(
                    "Переход в '%s' через '%s' (eta≈%.0f сек)",
                    target,
                    exit_link.name,
                    exit_link.travel_seconds,
                )
                clicked = await self._click_location_exit(page, exit_link)
                if not clicked:
                    continue

                arrived = await self._wait_travel_complete(
                    page, target, hint_seconds=exit_link.travel_seconds
                )
                if arrived:
                    self._current_location_id = target
                    logger.info("Прибыли в локацию %s", target)
                    return True

            except Exception as exc:
                logger.error(
                    "Ошибка навигации в %s (попытка %s): %s",
                    target,
                    attempt,
                    exc,
                    exc_info=True,
                )
                await self._reopen_location(page)
                await asyncio.sleep(random.uniform(1.2, 2.5))

        logger.error("Не удалось перейти в локацию %s", target)
        return False

    # ------------------------------------------------------------------
    # Сценарий квеста
    # ------------------------------------------------------------------

    async def execute_quest_sequence(
        self, page: Page, quest_script: List[dict]
    ) -> bool:
        """
        Выполняет последовательность шагов квеста.

        Ключи шага:
          - loc / location — id локации
          - npc — имя/id NPC
          - choose_option — индекс реплики (int)
          - choose_text — подстрока текста реплики
          - prefer_quest — предпочитать квестовые ветки (bool)
          - action — talk | turn_in | continue | wait
          - combo — список ударов для боя по пути
          - hp_threshold_pct — порог зелий в бою
        """
        if not quest_script:
            logger.warning("Пустой quest_script")
            return False

        logger.info("Старт квест-сценария: %s шагов", len(quest_script))

        for step_idx, step in enumerate(quest_script):
            if not isinstance(step, dict):
                logger.error("Шаг #%s не dict: %r", step_idx, step)
                return False

            logger.info("Шаг #%s: %s", step_idx, step)
            try:
                ok = await self._execute_step(page, step)
            except Exception as exc:
                logger.error(
                    "Критическая ошибка на шаге #%s: %s",
                    step_idx,
                    exc,
                    exc_info=True,
                )
                return False

            if not ok:
                logger.error("Шаг #%s провален: %s", step_idx, step)
                return False

            await asyncio.sleep(random.uniform(0.5, 1.2))

        logger.info("Квест-сценарий успешно завершён")
        return True

    async def parse_quest_states(self, page: Page) -> List[QuestState]:
        """Читает панель активных квестов (если доступна)."""
        try:
            frame = await self._resolve_main_frame(page)
            html = await self._frame_or_page_html(page, frame)
            soup = BeautifulSoup(html, "html.parser")
            states: List[QuestState] = []

            for node in soup.select(
                f"{self._selectors.quest_panel} .quest-item, "
                f"{self._selectors.quest_active}, "
                ".quest, .quest-row, [data-quest-id]"
            ):
                title = (
                    self._child_text(node, ".quest-title, .title, b, strong")
                    or node.get("title")
                    or node.get_text(" ", strip=True)
                )
                step = self._child_text(
                    node, ".quest-step, .step, .description, .desc"
                )
                status = self._quest_status_from_node(node)
                quest_id = (
                    node.get("data-quest-id")
                    or node.get("data-id")
                    or node.get("id")
                    or ""
                )
                if title:
                    states.append(
                        QuestState(
                            title=title[:120],
                            current_step=step[:300],
                            status=status,
                            quest_id=str(quest_id),
                        )
                    )
            return states
        except Exception as exc:
            logger.error("parse_quest_states: %s", exc, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Шаги сценария
    # ------------------------------------------------------------------

    async def _execute_step(self, page: Page, step: Dict[str, Any]) -> bool:
        await self._handle_combat_if_any(
            page,
            combo=step.get("combo"),
            hp_threshold_pct=float(step.get("hp_threshold_pct", 40.0)),
        )

        loc = str(step.get("loc") or step.get("location") or "").strip()
        if loc:
            if not await self.navigate_to_location(page, loc):
                return False
            await self._handle_combat_if_any(page, combo=step.get("combo"))

        action = str(step.get("action") or "talk").lower()
        if action == "wait":
            await asyncio.sleep(float(step.get("seconds", 2.0)))
            return True

        npc = str(step.get("npc") or "").strip()
        if npc:
            opened = await self._open_npc_dialog(page, npc)
            if not opened:
                logger.error("NPC '%s' не найден — переоткрываем локацию", npc)
                await self._reopen_location(page)
                opened = await self._open_npc_dialog(page, npc)
                if not opened:
                    return False

        if action == "continue":
            return await self._click_continue(page)

        # Выбор реплики
        npc_text, options = await self.parse_dialog_frame(page)
        if not options and action in {"talk", "turn_in"}:
            # Возможно, нужно сначала нажать «Далее»
            if await self._click_continue(page):
                npc_text, options = await self.parse_dialog_frame(page)

        if "choose_option" not in step and "choose_text" not in step:
            if action == "talk" and not options:
                return True
            if step.get("prefer_quest"):
                option = self._pick_quest_option(options)
                if option:
                    return await self.select_dialog_option(page, option)
            return True

        option = self._resolve_script_option(step, options)
        if option is None:
            logger.error(
                "Реплика не найдена (options=%s, step=%s)",
                [o.text for o in options],
                step,
            )
            # Диалог мог сброситься — пробуем переоткрыть NPC
            if npc:
                await self._reopen_location(page)
                if await self._open_npc_dialog(page, npc):
                    _, options = await self.parse_dialog_frame(page)
                    option = self._resolve_script_option(step, options)
            if option is None:
                return False

        return await self.select_dialog_option(page, option)

    def _resolve_script_option(
        self, step: Dict[str, Any], options: Sequence[NPCDialogOption]
    ) -> Optional[NPCDialogOption]:
        if not options:
            return None

        if "choose_option" in step:
            try:
                idx = int(step["choose_option"])
            except (TypeError, ValueError):
                return None
            for opt in options:
                if opt.index == idx:
                    return opt
            if 0 <= idx < len(options):
                return options[idx]
            return None

        needle = str(step.get("choose_text") or "").strip().lower()
        if needle:
            for opt in options:
                if needle in opt.text.lower():
                    return opt

        if step.get("prefer_quest"):
            return self._pick_quest_option(options)
        return None

    @staticmethod
    def _pick_quest_option(
        options: Sequence[NPCDialogOption],
    ) -> Optional[NPCDialogOption]:
        for opt in options:
            if opt.is_quest_trigger:
                return opt
        return options[0] if options else None

    # ------------------------------------------------------------------
    # NPC / локации
    # ------------------------------------------------------------------

    async def _open_npc_dialog(self, page: Page, npc: str) -> bool:
        """Ищет NPC в локации и открывает диалог."""
        for attempt in range(1, self._location_retry + 1):
            await self._handle_combat_if_any(page)
            frame = await self._resolve_main_frame(page)
            html = await self._frame_or_page_html(page, frame)
            soup = BeautifulSoup(html, "html.parser")

            link = self._find_npc_link(soup, npc)
            if link is None and frame is not None:
                # Playwright text search
                clicked = await self._click_by_text(
                    page, frame, npc, role_hint="npc"
                )
                if clicked:
                    await asyncio.sleep(random.uniform(0.8, 1.6))
                    text, opts = await self.parse_dialog_frame(page)
                    if text or opts:
                        return True

            if link is None:
                logger.warning(
                    "NPC '%s' не найден в локации (попытка %s/%s)",
                    npc,
                    attempt,
                    self._location_retry,
                )
                await self._reopen_location(page)
                await asyncio.sleep(random.uniform(0.8, 1.5))
                continue

            ok = await self._click_target(
                page,
                frame,
                text=link.text,
                url=link.url,
                option_id=link.option_id,
                css_hint=link.css_hint,
            )
            if not ok:
                continue

            await asyncio.sleep(random.uniform(0.8, 1.6))
            text, opts = await self.parse_dialog_frame(page)
            if text or opts:
                logger.info("Диалог с NPC '%s' открыт", npc)
                return True

            # Иногда диалог в том же фрейме без явных options — успех, если текст есть
            if self._looks_like_dialog(BeautifulSoup(
                await self._frame_or_page_html(page, frame), "html.parser"
            )):
                return True

        return False

    def _find_npc_link(
        self, soup: BeautifulSoup, npc: str
    ) -> Optional[NPCDialogOption]:
        needle = npc.lower()
        candidates: List[Tag] = []
        for selector in (
            "a[href*='npc']",
            "a[href*='talk']",
            "a[href*='speak']",
            "a[href*='dialog']",
            "[data-npc]",
            "[data-npc-id]",
            ".npc",
            ".npc-link",
            "a",
        ):
            candidates.extend(soup.select(selector))

        for node in candidates:
            label = node.get_text(" ", strip=True)
            attrs = " ".join(
                str(node.get(a) or "")
                for a in ("href", "data-npc", "data-npc-id", "id", "title", "onclick")
            )
            blob = f"{label} {attrs}".lower()
            if needle not in blob and needle not in label.lower():
                continue
            # Отсекаем явные переходы локаций без npc-маркера, если имя слишком общее
            href = str(node.get("href") or "")
            if RE_LOCATION_LINK.search(href) and not RE_NPC_LINK.search(href):
                if needle not in label.lower():
                    continue
            return NPCDialogOption(
                text=label or npc,
                option_id=str(
                    node.get("data-npc-id")
                    or node.get("data-npc")
                    or node.get("id")
                    or ""
                ),
                url=href,
                is_quest_trigger=bool(RE_QUEST_MARKERS.search(label)),
                css_hint=self._css_path_hint(node),
            )
        return None

    async def _find_location_exit(
        self, page: Page, target: str
    ) -> Optional[LocationExit]:
        frame = await self._resolve_main_frame(page)
        nav_frame = await self._resolve_nav_frame(page) or frame
        html = await self._frame_or_page_html(page, nav_frame)
        soup = BeautifulSoup(html, "html.parser")

        exits = self._extract_location_exits(soup)
        for exit_link in exits:
            if self._location_matches(exit_link.location_id, target):
                return exit_link
            if self._location_matches(exit_link.name, target):
                return exit_link

        # Playwright: кликабельный текст
        owner = nav_frame or page
        for selector in (
            f"a[href*='{target}']",
            f"[data-loc='{target}']",
            f"[data-location='{target}']",
            f"[data-location-id='{target}']",
            f"a:has-text('{target}')",
        ):
            try:
                if ":has-text" in selector:
                    loc = owner.locator(selector)
                    if await loc.count() == 0:
                        continue
                    text = (await loc.first.inner_text()).strip()
                    href = await loc.first.get_attribute("href") or ""
                else:
                    handle = await owner.query_selector(selector)
                    if handle is None:
                        continue
                    text = (await handle.inner_text() or "").strip()
                    href = (await handle.get_attribute("href")) or ""
                return LocationExit(
                    name=text or target,
                    location_id=target,
                    url=href,
                    css_hint=selector,
                )
            except PlaywrightError:
                continue
        return None

    def _extract_location_exits(self, soup: BeautifulSoup) -> List[LocationExit]:
        exits: List[LocationExit] = []
        seen: set[str] = set()
        for node in soup.select(
            "a[href*='loc'], a[href*='location'], a[href*='go'], "
            "a[href*='move'], a[href*='area'], [data-loc], [data-location], "
            ".exit, .location-link, .map-link, .go-link"
        ):
            name = node.get_text(" ", strip=True) or str(node.get("title") or "")
            href = str(node.get("href") or "")
            loc_id = (
                node.get("data-loc")
                or node.get("data-location")
                or node.get("data-location-id")
                or self._id_from_url(href)
                or name
            )
            key = f"{loc_id}|{href}|{name}".lower()
            if not loc_id or key in seen:
                continue
            seen.add(key)
            travel = self._parse_travel_seconds(
                f"{name} {node.get('title') or ''} {node.get_text(' ', strip=True)}"
            )
            exits.append(
                LocationExit(
                    name=name[:80],
                    location_id=str(loc_id),
                    url=href,
                    travel_seconds=travel,
                    css_hint=self._css_path_hint(node),
                )
            )
        return exits

    async def _click_location_exit(
        self, page: Page, exit_link: LocationExit
    ) -> bool:
        frame = await self._resolve_nav_frame(page) or await self._resolve_main_frame(
            page
        )
        return await self._click_target(
            page,
            frame,
            text=exit_link.name,
            url=exit_link.url,
            option_id=exit_link.location_id,
            css_hint=exit_link.css_hint,
        )

    async def _wait_travel_complete(
        self, page: Page, target: str, *, hint_seconds: float
    ) -> bool:
        wait_budget = max(
            hint_seconds + random.uniform(2.0, 5.0),
            random.uniform(2.0, 4.0),
        )
        wait_budget = min(wait_budget, self._travel_timeout_sec)
        logger.info("Ожидание перехода ≈ %.1f сек", wait_budget)

        deadline = time.monotonic() + wait_budget + 15.0
        started = time.monotonic()

        while time.monotonic() < deadline:
            await self._handle_combat_if_any(page)

            # Обновляем текущую локацию из UI
            current = await self._detect_current_location(page)
            if current and self._location_matches(current, target):
                return True

            html = await self._frame_or_page_html(
                page, await self._resolve_main_frame(page)
            )
            if RE_TRAVEL_DONE.search(html) and self._location_matches(
                await self._detect_current_location(page) or "", target
            ):
                return True

            # Если таймер ещё тикает — ждём
            remaining = self._parse_travel_seconds(
                BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
            )
            if remaining > 0:
                await asyncio.sleep(min(remaining + 0.5, 3.0))
                continue

            # После hint_seconds считаем прибывшими, если target виден в UI
            if time.monotonic() - started >= max(hint_seconds, 1.5):
                if target.lower() in html.lower():
                    # target упоминается — вероятно, уже на месте / выход доступен иначе
                    current = await self._detect_current_location(page)
                    if current and self._location_matches(current, target):
                        return True
                    # мягкий успех: переход кликнут, таймер вышел
                    if not self._extract_location_exits(
                        BeautifulSoup(html, "html.parser")
                    ) or current:
                        if current:
                            return self._location_matches(current, target)

            await asyncio.sleep(random.uniform(0.8, 1.6))

        # Финальная проверка
        current = await self._detect_current_location(page)
        if current and self._location_matches(current, target):
            return True
        logger.warning(
            "Таймаут перехода в %s (current=%s)", target, current or "?"
        )
        return False

    async def _detect_current_location(self, page: Page) -> str:
        try:
            stats = await self._stats_parser.parse_player_stats(page)
            if stats.location:
                self._current_location_id = stats.location
                return stats.location
        except Exception as exc:
            logger.debug("detect location via stats: %s", exc)

        frame = await self._resolve_main_frame(page)
        html = await self._frame_or_page_html(page, frame)
        soup = BeautifulSoup(html, "html.parser")
        for selector in (
            ".location",
            "#location",
            ".loc-name",
            "[data-role='location']",
            "[data-current-location]",
        ):
            node = soup.select_one(selector)
            if node:
                value = (
                    node.get("data-current-location")
                    or node.get_text(" ", strip=True)
                )
                if value:
                    self._current_location_id = value
                    return value
        return self._current_location_id

    async def _reopen_location(self, page: Page) -> None:
        """Переоткрывает текущую локацию / game entry при сбросе диалога."""
        logger.info("Переоткрытие локации / игрового экрана")
        try:
            if self._browser is not None:
                await self._browser.goto_with_retry(
                    self._config.server.game_entry_url,
                    wait_until="domcontentloaded",
                )
            else:
                await page.goto(
                    self._config.server.game_entry_url,
                    wait_until="domcontentloaded",
                    timeout=self._config.browser.navigation_timeout_ms,
                )
            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception as exc:
            logger.error("Не удалось переоткрыть локацию: %s", exc, exc_info=True)
            try:
                await page.reload(
                    wait_until="domcontentloaded",
                    timeout=self._config.browser.navigation_timeout_ms,
                )
            except PlaywrightError as reload_exc:
                logger.error("reload failed: %s", reload_exc)

    # ------------------------------------------------------------------
    # Бой / рюкзак
    # ------------------------------------------------------------------

    async def _handle_combat_if_any(
        self,
        page: Page,
        *,
        combo: Optional[List[str]] = None,
        hp_threshold_pct: float = 40.0,
    ) -> bool:
        """Если активен бой — передаёт управление CombatEngine."""
        try:
            state = await self._combat.parse_combat_state(page)
        except Exception as exc:
            logger.debug("combat probe failed: %s", exc)
            return False

        if not state.in_combat:
            return False

        logger.warning(
            "На пути бой с '%s' — передаём управление CombatEngine",
            state.enemy_name or "противником",
        )
        self._combat.reset_combo()
        won = await self._combat.process_fight(
            page,
            target_combo=combo,
            hp_threshold_pct=hp_threshold_pct,
        )
        logger.info("Бой завершён: %s", "победа" if won else "поражение/сбой")
        await asyncio.sleep(random.uniform(0.8, 1.5))
        return won

    async def _ensure_backpack_space(self, page: Page) -> bool:
        """Проверяет переполнение рюкзака перед квестовой наградой."""
        try:
            notes = await self._stats_parser.parse_notifications(page)
            for note in notes:
                if RE_BACKPACK_FULL.search(note.text):
                    logger.error("Уведомление: рюкзак полон — %s", note.text)
                    return False

            items = await self._stats_parser.parse_backpack(page)
            if len(items) >= self._backpack_soft_limit:
                logger.error(
                    "Рюкзак почти полон: %s/%s предметов",
                    len(items),
                    self._backpack_soft_limit,
                )
                return False
            return True
        except Exception as exc:
            logger.warning(
                "Не удалось проверить рюкзак (%s) — продолжаем осторожно",
                exc,
            )
            return True

    # ------------------------------------------------------------------
    # Парсинг диалога
    # ------------------------------------------------------------------

    def _extract_npc_text(self, soup: BeautifulSoup) -> str:
        for selector in self._split_css(
            (
                self._selectors.npc_dialog_text,
                ".dialog-text",
                ".npc-text",
                ".npc-speech",
                "#dialog_text",
                ".speech",
                ".replica",
            )
        ):
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return text[:2000]

        dialog = soup.select_one(self._selectors.npc_dialog.split(",")[0].strip())
        if dialog:
            # Убираем кнопки выбора из текста
            clone = BeautifulSoup(str(dialog), "html.parser")
            for sel in self._split_css(
                (self._selectors.npc_dialog_choices, "button", ".choice")
            ):
                for node in clone.select(sel):
                    node.decompose()
            text = clone.get_text(" ", strip=True)
            if text:
                return text[:2000]

        return ""

    def _extract_dialog_options(self, soup: BeautifulSoup) -> List[NPCDialogOption]:
        options: List[NPCDialogOption] = []
        nodes: List[Tag] = []
        for selector in self._split_css(
            (
                self._selectors.npc_dialog_choices,
                ".dialog-choice",
                ".npc-choice",
                ".dialog-options a",
                ".dialog-options button",
                "#dialog a",
                ".quest-option",
                "a[href*='talk']",
                "a[href*='answer']",
                "a[href*='reply']",
            )
        ):
            nodes.extend(soup.select(selector))

        seen: set[str] = set()
        index = 0
        for node in nodes:
            text = node.get_text(" ", strip=True)
            if not text or len(text) > 300:
                continue
            href = str(node.get("href") or "")
            option_id = str(
                node.get("data-option-id")
                or node.get("data-id")
                or node.get("id")
                or ""
            )
            key = f"{text.lower()}|{href}|{option_id}"
            if key in seen:
                continue
            seen.add(key)

            is_quest = self._is_quest_option(node, text)
            options.append(
                NPCDialogOption(
                    text=text,
                    option_id=option_id,
                    url=href,
                    is_quest_trigger=is_quest,
                    index=index,
                    css_hint=self._css_path_hint(node),
                )
            )
            index += 1
        return options

    def _is_quest_option(self, node: Tag, text: str) -> bool:
        if RE_QUEST_MARKERS.search(text):
            return True
        classes = " ".join(
            node.get("class") if isinstance(node.get("class"), list) else [str(node.get("class") or "")]
        ).lower()
        if any(
            token in classes
            for token in ("quest", "q-icon", "quest-opt", "exclamation", "reward")
        ):
            return True
        # Цветовые маркеры / иконки
        style = str(node.get("style") or "").lower()
        if "gold" in style or "#ff" in style or "yellow" in style:
            if RE_QUEST_MARKERS.search(text) or "!" in text:
                return True
        # Вложенная иконка квеста
        if node.select_one(
            "img[src*='quest'], img[src*='exclamation'], .quest-icon, .q-mark, .icon-quest"
        ):
            return True
        if "!" in text and len(text) < 80:
            return True
        return False

    async def _extract_options_via_playwright(
        self, frame: Frame
    ) -> List[NPCDialogOption]:
        options: List[NPCDialogOption] = []
        selectors = self._split_css(
            (
                self._selectors.npc_dialog_choices,
                ".dialog-choice",
                ".npc-choice",
                ".dialog-options a",
                ".dialog-options button",
            )
        )
        index = 0
        for selector in selectors:
            try:
                loc = frame.locator(selector)
                count = await loc.count()
            except PlaywrightError:
                continue
            for i in range(min(count, 30)):
                try:
                    item = loc.nth(i)
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text(timeout=1_000)).strip()
                    if not text:
                        continue
                    href = await item.get_attribute("href") or ""
                    option_id = (
                        await item.get_attribute("data-option-id")
                        or await item.get_attribute("data-id")
                        or await item.get_attribute("id")
                        or ""
                    )
                    class_attr = await item.get_attribute("class") or ""
                    is_quest = bool(RE_QUEST_MARKERS.search(text)) or (
                        "quest" in class_attr.lower()
                    )
                    options.append(
                        NPCDialogOption(
                            text=text[:300],
                            option_id=option_id,
                            url=href,
                            is_quest_trigger=is_quest,
                            index=index,
                            css_hint=selector,
                        )
                    )
                    index += 1
                except (PlaywrightError, PlaywrightTimeoutError):
                    continue
        return options

    @staticmethod
    def _merge_options(
        primary: List[NPCDialogOption], secondary: List[NPCDialogOption]
    ) -> List[NPCDialogOption]:
        merged = list(primary)
        seen = {f"{o.text.lower()}|{o.url}|{o.option_id}" for o in primary}
        for opt in secondary:
            key = f"{opt.text.lower()}|{opt.url}|{opt.option_id}"
            if key in seen:
                continue
            seen.add(key)
            opt.index = len(merged)
            merged.append(opt)
        return merged

    def _looks_like_dialog(self, soup: BeautifulSoup) -> bool:
        if soup.select_one(self._selectors.npc_dialog):
            return True
        if soup.select_one(self._selectors.npc_dialog_text):
            return True
        text = soup.get_text(" ", strip=True).lower()
        return any(w in text for w in ("говорит", "сказал", "npc", "диалог"))

    # ------------------------------------------------------------------
    # Клики
    # ------------------------------------------------------------------

    async def _click_dialog_option(
        self,
        page: Page,
        frame: Optional[Frame],
        option: NPCDialogOption,
    ) -> bool:
        return await self._click_target(
            page,
            frame,
            text=option.text,
            url=option.url,
            option_id=option.option_id,
            css_hint=option.css_hint,
            index=option.index,
        )

    async def _click_continue(self, page: Page) -> bool:
        frame = await self._resolve_main_frame(page)
        for selector in self._split_css(
            (
                self._selectors.npc_dialog_continue,
                "button[data-action='continue']",
                ".dialog-continue",
                "a:has-text('Далее')",
                "button:has-text('Далее')",
                "a:has-text('Продолжить')",
            )
        ):
            try:
                owner: Any = frame or page
                if ":has-text" in selector:
                    loc = owner.locator(selector)
                    if await loc.count() == 0:
                        continue
                    await self._human_click_selector(page, selector, frame=frame)
                    return True
                handle = await owner.query_selector(selector)
                if handle and await handle.is_visible():
                    await self._human_click_selector(page, selector, frame=frame)
                    return True
            except (BrowserEngineError, PlaywrightError):
                continue

        # Фоллбек по тексту
        return await self._click_by_text(page, frame, "Далее", role_hint="continue")

    async def _click_target(
        self,
        page: Page,
        frame: Optional[Frame],
        *,
        text: str = "",
        url: str = "",
        option_id: str = "",
        css_hint: str = "",
        index: int = -1,
    ) -> bool:
        owner: Any = frame or page
        selectors: List[str] = []
        if css_hint:
            selectors.append(css_hint)
        if option_id:
            selectors.extend(
                [
                    f"[data-option-id='{option_id}']",
                    f"[data-id='{option_id}']",
                    f"[data-npc-id='{option_id}']",
                    f"[data-loc='{option_id}']",
                    f"[data-location='{option_id}']",
                    f"#{option_id}",
                    f"a[href*='{option_id}']",
                ]
            )
        if url:
            safe = url.replace("'", "")
            selectors.append(f"a[href='{safe}']")
            if "?" in safe:
                selectors.append(f"a[href*='{safe.split('?')[0]}']")

        for selector in selectors:
            try:
                if ":has-text" in selector:
                    loc = owner.locator(selector)
                    if await loc.count() == 0:
                        continue
                    await self._human_click_selector(page, selector, frame=frame)
                    return True
                handle = await owner.query_selector(selector)
                if handle is not None and await handle.is_visible():
                    await self._human_click_selector(page, selector, frame=frame)
                    return True
            except (BrowserEngineError, PlaywrightError) as exc:
                logger.debug("click_target %s: %s", selector, exc)

        if text:
            if await self._click_by_text(page, frame, text, role_hint="option"):
                return True

        if index >= 0:
            choice_sel = self._selectors.npc_dialog_choices
            try:
                loc = owner.locator(choice_sel)
                if await loc.count() > index:
                    handle = await loc.nth(index).element_handle(timeout=3_000)
                    if handle is not None and self._browser is not None:
                        await self._browser.human_click(handle, page=page, frame=frame)
                        return True
                    await loc.nth(index).click(
                        timeout=self._config.browser.default_timeout_ms
                    )
                    return True
            except (BrowserEngineError, PlaywrightError) as exc:
                logger.debug("click by index %s: %s", index, exc)

        return False

    async def _click_by_text(
        self,
        page: Page,
        frame: Optional[Frame],
        text: str,
        *,
        role_hint: str = "",
    ) -> bool:
        if not text:
            return False
        short = text[:40].replace("'", "\\'")
        owner: Any = frame or page
        selectors = [
            f"a:has-text('{short}')",
            f"button:has-text('{short}')",
            f"text={short}",
        ]
        for selector in selectors:
            try:
                loc = owner.locator(selector)
                count = await loc.count()
                if count == 0:
                    continue
                await self._human_click_selector(page, selector, frame=frame)
                logger.debug("Клик по тексту (%s): %r", role_hint, short)
                return True
            except (BrowserEngineError, PlaywrightError):
                continue
        return False

    async def _human_click_selector(
        self,
        page: Page,
        selector: str,
        *,
        frame: Optional[Frame] = None,
    ) -> None:
        if self._browser is not None and ":has-text" not in selector and not selector.startswith("text="):
            await self._browser.human_click(selector, page=page, frame=frame)
            return

        owner: Any = frame or page
        await asyncio.sleep(random.uniform(0.35, 1.0))
        try:
            if self._browser is not None:
                loc = owner.locator(selector)
                handle = await loc.first.element_handle(timeout=5_000)
                if handle is not None:
                    await self._browser.human_click(handle, page=page, frame=frame)
                    return
            await owner.locator(selector).first.click(
                timeout=self._config.browser.default_timeout_ms
            )
        except PlaywrightError as exc:
            raise BrowserEngineError(f"click failed for {selector}: {exc}") from exc

    # ------------------------------------------------------------------
    # Фреймы / утилиты
    # ------------------------------------------------------------------

    async def _resolve_main_frame(self, page: Page) -> Optional[Frame]:
        return await self._resolve_frame(
            page, self._selectors.main_frame, MAIN_FRAME_NAMES
        )

    async def _resolve_nav_frame(self, page: Page) -> Optional[Frame]:
        return await self._resolve_frame(
            page, self._selectors.navigation_frame, NAV_FRAME_NAMES
        )

    async def _resolve_frame(
        self,
        page: Page,
        css: str,
        names: Sequence[str],
    ) -> Optional[Frame]:
        for frame in page.frames:
            name = (frame.name or "").lower()
            if name in {n.lower() for n in names}:
                return frame
            url = (frame.url or "").lower()
            if any(n.lower() in url for n in names):
                return frame

        for selector in self._split_css(css):
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

    async def _frame_or_page_html(
        self, page: Page, frame: Optional[Frame]
    ) -> str:
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
                    logger.debug("frame html failed: %s", exc)
        try:
            return await page.content()
        except PlaywrightError as exc:
            logger.error("page.content failed: %s", exc)
            return ""

    def _reading_delay(self, option_text: str, npc_text: str) -> float:
        """Пауза чтения 1.5–3.5 сек, слегка зависит от длины текста."""
        length = len(npc_text or "") + len(option_text or "")
        # База + вклад длины, затем clamp в [1.5, 3.5]
        raw = 1.5 + min(length, 400) / 400.0 * 1.5
        jitter = random.uniform(-0.25, 0.35)
        return max(1.5, min(3.5, raw + jitter))

    @staticmethod
    def _location_matches(left: str, right: str) -> bool:
        a = (left or "").strip().lower().replace(" ", "_")
        b = (right or "").strip().lower().replace(" ", "_")
        if not a or not b:
            return False
        return a == b or a in b or b in a

    @staticmethod
    def _id_from_url(url: str) -> str:
        if not url:
            return ""
        match = re.search(
            r"(?:loc|location|area|id|go)=([\w.-]+)", url, re.IGNORECASE
        )
        return match.group(1) if match else ""

    @staticmethod
    def _parse_travel_seconds(text: str) -> float:
        match = RE_TRAVEL_TIME.search(text or "")
        if not match:
            return 0.0
        if match.groupdict().get("sec_only"):
            return float(match.group("sec_only"))
        minutes = int(match.group("min") or 0)
        seconds = int(match.group("sec") or 0)
        return float(minutes * 60 + seconds)

    @staticmethod
    def _quest_status_from_node(node: Tag) -> QuestStatus:
        blob = f"{node.get('class')} {node.get('data-status')} {node.get_text(' ', strip=True)}".lower()
        if any(w in blob for w in ("complete", "сдать", "готов", "ready", "finish")):
            return QuestStatus.READY_TO_COMPLETE
        if any(w in blob for w in ("fail", "провал", "провален")):
            return QuestStatus.FAILED
        if any(w in blob for w in ("done", "сдан", "completed")):
            return QuestStatus.COMPLETED
        if any(w in blob for w in ("progress", "актив", "active", "взят")):
            return QuestStatus.IN_PROGRESS
        return QuestStatus.UNKNOWN

    @staticmethod
    def _child_text(node: Tag, selectors: str) -> str:
        for selector in selectors.split(","):
            child = node.select_one(selector.strip())
            if child:
                text = child.get_text(" ", strip=True)
                if text:
                    return text
        return ""

    @staticmethod
    def _css_path_hint(node: Tag) -> str:
        if node.get("id"):
            return f"#{node.get('id')}"
        classes = node.get("class")
        if isinstance(classes, list) and classes:
            return f"{node.name}.{classes[0]}"
        if node.name:
            return node.name
        return ""

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
