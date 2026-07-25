"""
browser_service.py — управление Playwright Persistent Context для боёв Remanga.

Ключевые идеи:
1. Сессия браузера хранится в папке `user_data` (cookies, localStorage, CF-токены).
2. Режим `setup` — ручной вход в графическом окне (headless=False).
3. Авторежим — headless=True на уже сохранённом профиле.
4. Клики с «человеческими» задержками 3–8 сек и явными ожиданиями Playwright.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    expect,
)

from config import Config

logger = logging.getLogger(__name__)


class BattleOutcome(str, Enum):
    """Итог одной попытки боя."""

    WIN = "win"
    LOSE = "lose"
    DRAW = "draw"
    SKIPPED = "skipped"  # кнопка неактивна / кулдаун / нет энергии
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class BattleResult:
    """Структурированный отчёт о бое для Telegram."""

    outcome: BattleOutcome
    message: str
    rating_change: Optional[str] = None
    rewards: Optional[str] = None
    raw_text: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_telegram(self) -> str:
        """Краткий человекочитаемый отчёт для чата."""
        icons = {
            BattleOutcome.WIN: "🏆 Победа",
            BattleOutcome.LOSE: "💀 Поражение",
            BattleOutcome.DRAW: "🤝 Ничья",
            BattleOutcome.SKIPPED: "⏸ Пропуск",
            BattleOutcome.ERROR: "⚠️ Ошибка",
            BattleOutcome.UNKNOWN: "❓ Неизвестно",
        }
        lines = [
            f"{icons.get(self.outcome, '❓')} — {self.message}",
            f"🕒 {self.timestamp.strftime('%d.%m.%Y %H:%M:%S')}",
        ]
        if self.rating_change:
            lines.append(f"📈 Рейтинг: {self.rating_change}")
        if self.rewards:
            lines.append(f"🎁 Награды: {self.rewards}")
        if self.raw_text and self.outcome not in (BattleOutcome.SKIPPED, BattleOutcome.ERROR):
            # Обрезаем длинный сырой текст, чтобы не спамить чат
            snippet = self.raw_text.strip().replace("\n", " ")
            if len(snippet) > 280:
                snippet = snippet[:277] + "..."
            lines.append(f"📝 {snippet}")
        return "\n".join(lines)


class BrowserService:
    """
    Сервис браузера: один persistent context на весь жизненный цикл автобоя.

    Использование:
        service = BrowserService(config)
        await service.start(headless=True)
        result = await service.do_battle()
        await service.stop()
    """

    # Текстовые маркеры кнопки «В БОЙ» (регистр не важен)
    BATTLE_BUTTON_PATTERNS = (
        re.compile(r"^\s*в\s*бой\s*$", re.IGNORECASE),
        re.compile(r"в\s*бой", re.IGNORECASE),
        re.compile(r"fight|battle|атаковать", re.IGNORECASE),
    )

    # Маркеры результата на странице
    WIN_MARKERS = re.compile(
        r"победа|вы\s*победили|victory|win\b|🏆",
        re.IGNORECASE,
    )
    LOSE_MARKERS = re.compile(
        r"поражение|вы\s*проиграли|defeat|lose\b|💀",
        re.IGNORECASE,
    )
    DRAW_MARKERS = re.compile(r"ничья|draw", re.IGNORECASE)
    RATING_MARKERS = re.compile(
        r"(рейтинг|mmr|elo|ранг)[^\n]{0,40}?([+\-−–]?\s*\d+)",
        re.IGNORECASE,
    )
    REWARD_MARKERS = re.compile(
        r"(награда|получено|опыт|монет|карт|фрагмент)[^\n]{0,60}",
        re.IGNORECASE,
    )
    COOLDOWN_MARKERS = re.compile(
        r"энерги|кулдаун|перезаряд|подождите|недостаточно|восстанавлив|cooldown",
        re.IGNORECASE,
    )

    def __init__(self, config: Config) -> None:
        self.config = config
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Жизненный цикл браузера
    # ------------------------------------------------------------------

    @property
    def is_started(self) -> bool:
        return self._started and self._context is not None

    async def start(self, headless: bool = True) -> None:
        """
        Запустить persistent context.

        Args:
            headless: True — фоновый режим; False — видимое окно (для setup).
        """
        if self._started:
            logger.debug("BrowserService уже запущен, повторный start пропущен.")
            return

        user_data = Path(self.config.user_data_dir)
        user_data.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Запуск Playwright Persistent Context (headless=%s, profile=%s)",
            headless,
            user_data,
        )

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
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
            # Небольшой «антидетект»: скрываем navigator.webdriver
            ignore_default_args=["--enable-automation"],
        )

        # Берём первую вкладку или открываем новую
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        self._page.set_default_timeout(self.config.selector_timeout_ms)
        self._started = True
        logger.info("Браузер готов к работе.")

    async def stop(self) -> None:
        """Корректно закрыть контекст и Playwright."""
        logger.info("Остановка BrowserService...")
        try:
            if self._context is not None:
                await self._context.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ошибка при закрытии context: %s", exc)
        finally:
            self._context = None
            self._page = None

        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ошибка при остановке playwright: %s", exc)
        finally:
            self._playwright = None
            self._started = False
            logger.info("BrowserService остановлен.")

    async def run_setup(self) -> None:
        """
        Режим первичной авторизации (ручной вход).

        Открывает браузер в графическом режиме, даёт пользователю:
        - пройти Cloudflare / капчу;
        - войти в аккаунт Remanga;
        - убедиться, что страница боёв доступна.

        После Enter в консоли контекст закрывается, а профиль сохраняется
        в `user_data` для последующих headless-запусков.
        """
        await self.start(headless=False)
        assert self._page is not None

        logger.info("SETUP: открываю %s", self.config.battle_url)
        await self._page.goto(self.config.battle_url, wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print("РЕЖИМ SETUP — ручная авторизация Remanga")
        print("=" * 60)
        print("1. В открывшемся окне пройдите Cloudflare / капчу (если есть).")
        print("2. Войдите в свой аккаунт Remanga.")
        print("3. Откройте страницу боёв и убедитесь, что видна кнопка «В БОЙ».")
        print("4. Вернитесь в этот терминал и нажмите Enter — сессия сохранится.")
        print("=" * 60 + "\n")

        # Блокирующее ожидание ввода в отдельном потоке, чтобы event loop жил
        await asyncio.to_thread(input, "Нажмите Enter после успешного входа... ")

        # Финальный «прогрев»: убеждаемся, что cookies записаны на диск
        try:
            await self._page.goto(self.config.battle_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось обновить страницу перед сохранением: %s", exc)

        await self.stop()
        print(
            f"\n✅ Сессия сохранена в: {self.config.user_data_dir}\n"
            "Теперь можно запускать бота:  python bot.py\n"
        )

    # ------------------------------------------------------------------
    # Боевая логика
    # ------------------------------------------------------------------

    async def do_battle(self) -> BattleResult:
        """
        Выполнить один бой: открыть страницу → дождаться кнопки → клик → результат.

        Потокобезопасно: одновременные вызовы сериализуются через asyncio.Lock.
        """
        async with self._lock:
            if not self.is_started:
                # Автозапуск в headless, если сервис ещё не поднят
                await self.start(headless=True)

            assert self._page is not None
            page = self._page

            try:
                logger.info("Переход на страницу боёв: %s", self.config.battle_url)
                await page.goto(self.config.battle_url, wait_until="domcontentloaded")
                # Даём SPA дорисовать динамический UI
                await page.wait_for_load_state("networkidle", timeout=self.config.selector_timeout_ms)
            except Exception as exc:  # noqa: BLE001
                logger.error("Не удалось открыть страницу боёв: %s", exc)
                return BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message=f"Не удалось открыть страницу боёв: {exc}",
                )

            # Случайная «человеческая» пауза перед поиском/кликом
            await self._human_delay("перед поиском кнопки «В БОЙ»")

            button = await self._find_battle_button(page)
            if button is None:
                # Возможно, на странице сообщение о кулдауне / нехватке энергии
                body_text = await self._safe_body_text(page)
                if body_text and self.COOLDOWN_MARKERS.search(body_text):
                    return BattleResult(
                        outcome=BattleOutcome.SKIPPED,
                        message="Кнопка «В БОЙ» недоступна (энергия/кулдаун).",
                        raw_text=body_text[:500],
                    )
                return BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message="Кнопка «В БОЙ» не найдена на странице.",
                    raw_text=(body_text or "")[:500],
                )

            # Проверяем, что кнопка видима и активна
            try:
                await expect(button).to_be_visible(timeout=self.config.selector_timeout_ms)
            except Exception as exc:  # noqa: BLE001
                return BattleResult(
                    outcome=BattleOutcome.SKIPPED,
                    message=f"Кнопка «В БОЙ» не видима: {exc}",
                )

            disabled = await button.is_disabled()
            aria_disabled = await button.get_attribute("aria-disabled")
            class_name = (await button.get_attribute("class")) or ""
            looks_disabled = (
                disabled
                or (aria_disabled or "").lower() in {"true", "1"}
                or "disabled" in class_name.lower()
                or "is-disabled" in class_name.lower()
            )
            if looks_disabled:
                body_text = await self._safe_body_text(page)
                return BattleResult(
                    outcome=BattleOutcome.SKIPPED,
                    message="Кнопка «В БОЙ» неактивна (восстановление энергии / кулдаун).",
                    raw_text=(body_text or "")[:500],
                )

            await self._human_delay("перед кликом «В БОЙ»")

            try:
                await button.scroll_into_view_if_needed()
                await button.click(timeout=self.config.selector_timeout_ms)
                logger.info("Клик по «В БОЙ» выполнен.")
            except Exception as exc:  # noqa: BLE001
                logger.error("Не удалось нажать «В БОЙ»: %s", exc)
                return BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message=f"Клик по «В БОЙ» не удался: {exc}",
                )

            # Ждём появления результата боя
            result = await self._wait_and_parse_result(page)
            logger.info("Итог боя: %s — %s", result.outcome.value, result.message)
            return result

    async def _find_battle_button(self, page: Page):
        """
        Найти кнопку «В БОЙ» несколькими стратегиями (устойчивость к смене вёрстки).

        Приоритет:
        1) role=button с текстом «В БОЙ»
        2) любой кликабельный элемент с текстом «В БОЙ»
        3) CSS-кнопки, содержащие подстроку в тексте
        """
        # 1. Семантический поиск по роли
        for pattern in self.BATTLE_BUTTON_PATTERNS:
            locator = page.get_by_role("button", name=pattern)
            try:
                if await locator.count() > 0:
                    candidate = locator.first
                    await candidate.wait_for(state="visible", timeout=3_000)
                    return candidate
            except Exception:  # noqa: BLE001
                pass

        # 2. Любой элемент с точным/похожим текстом
        for text in ("В БОЙ", "В бой", "в бой", "Fight", "Battle"):
            locator = page.get_by_text(text, exact=False)
            try:
                count = await locator.count()
                for i in range(min(count, 8)):
                    el = locator.nth(i)
                    if not await el.is_visible():
                        continue
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    role = await el.get_attribute("role")
                    if tag in {"button", "a", "div", "span"} or role == "button":
                        return el
            except Exception:  # noqa: BLE001
                continue

        # 3. CSS-fallback: кнопки / элементы с data-атрибутами
        css_candidates = [
            "button:has-text('В БОЙ')",
            "button:has-text('В бой')",
            "[role='button']:has-text('В БОЙ')",
            "a:has-text('В БОЙ')",
            "[data-testid*='battle']",
            "[class*='battle'] button",
            "[class*='fight'] button",
        ]
        for css in css_candidates:
            locator = page.locator(css)
            try:
                if await locator.count() > 0:
                    candidate = locator.first
                    await candidate.wait_for(state="visible", timeout=2_000)
                    return candidate
            except Exception:  # noqa: BLE001
                continue

        return None

    async def _wait_and_parse_result(self, page: Page) -> BattleResult:
        """
        Дождаться появления блока результата и распарсить победу/поражение/рейтинг.
        """
        # Небольшая пауза на анимацию боя / запрос к API
        await asyncio.sleep(2.0)

        # Ждём появления любого маркера результата или модалки
        result_selectors = [
            "text=/победа|поражение|ничья|victory|defeat/i",
            "[class*='result']",
            "[class*='battle-result']",
            "[class*='modal']",
            "[role='dialog']",
        ]

        appeared = False
        for selector in result_selectors:
            try:
                await page.wait_for_selector(selector, timeout=8_000, state="visible")
                appeared = True
                break
            except Exception:  # noqa: BLE001
                continue

        # Собираем текст из модалки/диалога, иначе — из body
        raw_text = ""
        for scope in ("[role='dialog']", "[class*='modal']", "[class*='result']", "body"):
            try:
                loc = page.locator(scope).first
                if await loc.count() == 0:
                    continue
                if not await loc.is_visible():
                    continue
                raw_text = (await loc.inner_text(timeout=3_000)) or ""
                if raw_text.strip():
                    break
            except Exception:  # noqa: BLE001
                continue

        if not raw_text.strip():
            raw_text = await self._safe_body_text(page) or ""

        outcome = BattleOutcome.UNKNOWN
        message = "Результат боя распознан неуверенно."

        if self.WIN_MARKERS.search(raw_text):
            outcome = BattleOutcome.WIN
            message = "Бой выигран."
        elif self.LOSE_MARKERS.search(raw_text):
            outcome = BattleOutcome.LOSE
            message = "Бой проигран."
        elif self.DRAW_MARKERS.search(raw_text):
            outcome = BattleOutcome.DRAW
            message = "Ничья."
        elif not appeared and self.COOLDOWN_MARKERS.search(raw_text):
            outcome = BattleOutcome.SKIPPED
            message = "Бой не запущен (энергия/кулдаун)."
        elif appeared:
            message = "Бой завершён, точный исход не определён по тексту."

        rating_change = None
        rating_match = self.RATING_MARKERS.search(raw_text)
        if rating_match:
            rating_change = rating_match.group(0).strip()
        else:
            # Запасной поиск «+12» / «−8» рядом со словом рейтинг
            plus_minus = re.search(r"([+\-−–]\s*\d+)\s*(?:рейтинг|mmr|elo)?", raw_text, re.I)
            if plus_minus:
                rating_change = plus_minus.group(1).replace(" ", "")

        rewards = None
        reward_match = self.REWARD_MARKERS.search(raw_text)
        if reward_match:
            rewards = reward_match.group(0).strip()

        return BattleResult(
            outcome=outcome,
            message=message,
            rating_change=rating_change,
            rewards=rewards,
            raw_text=raw_text,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    async def _human_delay(self, reason: str = "") -> None:
        """Случайная пауза 3–8 секунд для снижения риска бана."""
        delay = random.uniform(
            self.config.human_delay_min_sec,
            self.config.human_delay_max_sec,
        )
        logger.debug("Человеческая пауза %.1f сек %s", delay, f"({reason})" if reason else "")
        await asyncio.sleep(delay)

    @staticmethod
    async def _safe_body_text(page: Page) -> str:
        """Безопасно прочитать текст body (пустая строка при ошибке)."""
        try:
            return await page.locator("body").inner_text(timeout=5_000)
        except Exception:  # noqa: BLE001
            return ""


async def main_setup() -> None:
    """Точка входа для `python browser_service.py` — только режим setup."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    from config import load_config

    config = load_config()
    service = BrowserService(config)
    await service.run_setup()


if __name__ == "__main__":
    asyncio.run(main_setup())
