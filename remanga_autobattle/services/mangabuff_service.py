"""
mangabuff_service.py — Playwright-автоматизация для mangabuff.ru.

Отдельный persistent context (`user_data_mangabuff`), независимый от Remanga.

Возможности:
- setup: ручной вход (headless=False), сохранение cookies/сессии;
- авточтение: плавный скролл страниц, пауза 5–15 с, переход к следующей главе;
- сбор наград: ежедневные бонусы / карты / кнопки «Забрать».
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from config import BASE_DIR, Config

logger = logging.getLogger(__name__)

DEFAULT_START_URL = "https://mangabuff.ru/"
DEFAULT_USER_DATA = BASE_DIR / "user_data_mangabuff"


@dataclass
class MangaBuffStats:
    """Статистика сессии MangaBuff."""

    running: bool = False
    chapters_read: int = 0
    pages_scrolled: int = 0
    rewards_claimed: int = 0
    cards_claimed: int = 0
    errors: int = 0
    last_url: str = ""
    last_action: str = ""
    last_at: str = ""
    started_at: str = ""

    def touch(self, action: str, url: str = "") -> None:
        self.last_action = action
        self.last_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        if url:
            self.last_url = url

    def to_telegram(self, delay_range: Tuple[float, float] = (5.0, 15.0)) -> str:
        flag = "🟢 Авточтение" if self.running else "🔴 Остановлен"
        return (
            f"<b>📚 MangaBuff — статус</b>\n\n"
            f"Состояние: {flag}\n"
            f"📖 Глав прочитано: <b>{self.chapters_read}</b>\n"
            f"📄 Страниц (скроллов): <b>{self.pages_scrolled}</b>\n"
            f"🎁 Наград собрано: <b>{self.rewards_claimed}</b>\n"
            f"🃏 Карт получено: <b>{self.cards_claimed}</b>\n"
            f"⚠️ Ошибки: {self.errors}\n"
            f"⏱ Задержка чтения: {delay_range[0]:.0f}–{delay_range[1]:.0f} сек\n"
            f"🔗 Последний URL: <code>{(self.last_url or '—')[:120]}</code>\n"
            f"📝 Действие: {self.last_action or '—'}\n"
            f"🕒 {self.last_at or '—'}"
        )


@dataclass
class ClaimResult:
    """Итог разового сбора наград."""

    claimed: int = 0
    cards: int = 0
    details: List[str] = field(default_factory=list)
    message: str = ""

    def to_telegram(self) -> str:
        lines = [
            "<b>🎁 MangaBuff — сбор наград</b>",
            "",
            f"Забрано кнопок/бонусов: <b>{self.claimed}</b>",
            f"Карт: <b>{self.cards}</b>",
        ]
        if self.details:
            lines.append("")
            lines.append("Детали:")
            for d in self.details[:12]:
                lines.append(f"• {d}")
        if self.message:
            lines.append("")
            lines.append(self.message)
        return "\n".join(lines)


class MangaBuffService:
    """
    Сервис браузера для mangabuff.ru.

    Использование:
        svc = MangaBuffService(config, user_data_dir=...)
        await svc.start()
        await svc.run_setup()          # один раз
        await svc.claim_rewards()      # разово
        await svc.read_loop(...)       # авточтение (пока running)
    """

    # Кнопки сбора наград / карт
    REWARD_BUTTON_PATTERNS = (
        re.compile(r"забрать", re.I),
        re.compile(r"получить\s*(награду|бонус|карт)", re.I),
        re.compile(r"собрать", re.I),
        re.compile(r"claim", re.I),
        re.compile(r"ежедневн", re.I),
        re.compile(r"календар", re.I),
        re.compile(r"открыть\s*(пак|награду)?", re.I),
        re.compile(r"забрать\s*награду", re.I),
    )

    NEXT_CHAPTER_PATTERNS = (
        re.compile(r"следующ(ая|ую)\s*глав", re.I),
        re.compile(r"след\.?\s*глава", re.I),
        re.compile(r"next\s*chapter", re.I),
        re.compile(r"дальше", re.I),
        re.compile(r"^→$"),
        re.compile(r"next", re.I),
    )

    def __init__(
        self,
        config: Config,
        user_data_dir: Optional[Path] = None,
        start_url: str = DEFAULT_START_URL,
        delay_min_sec: float = 5.0,
        delay_max_sec: float = 15.0,
    ) -> None:
        self.config = config
        self.user_data_dir = Path(user_data_dir or DEFAULT_USER_DATA)
        self.start_url = start_url
        self.delay_min_sec = delay_min_sec
        self.delay_max_sec = delay_max_sec

        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._started = False
        self._stop_flag = asyncio.Event()
        self.stats = MangaBuffStats()

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    @property
    def is_started(self) -> bool:
        return self._started and self._context is not None

    async def start(self, headless: bool = True) -> None:
        if self._started:
            return
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "MangaBuff: запуск Playwright (headless=%s, profile=%s)",
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
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(self.config.selector_timeout_ms)
        self._started = True
        logger.info("MangaBuff: браузер готов.")

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

    async def run_setup(self) -> None:
        """
        Ручной вход: окно браузера, пользователь логинится на mangabuff.ru,
        сессия сохраняется в user_data_mangabuff.
        """
        await self.start(headless=False)
        assert self._page is not None
        await self._page.goto(self.start_url, wait_until="domcontentloaded")
        print("\n" + "=" * 60)
        print("MANGABUFF SETUP — ручная авторизация")
        print("=" * 60)
        print("1. Пройдите DDoS-Guard / капчу при необходимости.")
        print("2. Войдите в аккаунт MangaBuff.")
        print("3. Откройте любую мангу/читалку для проверки.")
        print("4. Вернитесь в терминал и нажмите Enter.")
        print("=" * 60 + "\n")
        await asyncio.to_thread(input, "Нажмите Enter после успешного входа... ")
        try:
            await self._page.goto(self.start_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff setup refresh: %s", exc)
        await self.stop()
        print(f"\n✅ Сессия MangaBuff сохранена в: {self.user_data_dir}\n")

    def request_stop(self) -> None:
        """Сигнал остановить авточтение."""
        self._stop_flag.set()
        self.stats.running = False
        self.stats.touch("стоп запрошен")

    def set_delay(self, delay_min: float, delay_max: float) -> None:
        self.delay_min_sec = max(1.0, float(delay_min))
        self.delay_max_sec = max(self.delay_min_sec, float(delay_max))

    # ------------------------------------------------------------------
    # Сбор наград
    # ------------------------------------------------------------------

    async def claim_rewards(self) -> ClaimResult:
        """Разовый обход главных страниц и клик по кнопкам наград/карт."""
        async with self._lock:
            if not self.is_started:
                await self.start(headless=True)
            assert self._page is not None
            page = self._page
            result = ClaimResult()

            urls = [
                self.start_url.rstrip("/") + "/",
                "https://mangabuff.ru/",
                "https://mangabuff.ru/profile",
                "https://mangabuff.ru/inventory",
                "https://mangabuff.ru/shop",
                "https://mangabuff.ru/cards",
            ]
            # Уникальные URL
            seen = set()
            unique_urls = []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    unique_urls.append(u)

            for url in unique_urls:
                if self._stop_flag.is_set() and self.stats.running:
                    break
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self.config.selector_timeout_ms)
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    claimed_here, cards_here, details = await self._click_reward_buttons(page)
                    result.claimed += claimed_here
                    result.cards += cards_here
                    result.details.extend(details)
                    self.stats.rewards_claimed += claimed_here
                    self.stats.cards_claimed += cards_here
                    self.stats.touch(f"сбор наград на {url}", url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("claim_rewards %s: %s", url, exc)
                    self.stats.errors += 1
                    result.details.append(f"ошибка {url}: {exc}")

            if result.claimed == 0 and result.cards == 0:
                result.message = "Активных кнопок наград не найдено (или уже собрано)."
            else:
                result.message = "Сбор завершён."
            return result

    async def _click_reward_buttons(self, page: Page) -> Tuple[int, int, List[str]]:
        claimed = 0
        cards = 0
        details: List[str] = []

        # Текстовые кнопки
        texts = [
            "Забрать",
            "Забрать награду",
            "Получить",
            "Получить награду",
            "Получить карту",
            "Собрать",
            "Открыть",
            "Ежедневная награда",
            "Календарь",
            "Claim",
        ]
        for text in texts:
            loc = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                count = 0
            for i in range(min(count, 5)):
                btn = loc.nth(i)
                try:
                    if not await btn.is_visible():
                        continue
                    label = ((await btn.inner_text(timeout=1000)) or text).strip()
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.4, 1.0))
                    box = await btn.bounding_box()
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                    else:
                        await btn.click(force=True)
                    claimed += 1
                    low = label.lower()
                    if "карт" in low or "card" in low or "пак" in low:
                        cards += 1
                    details.append(f"клик: {label[:60]}")
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                except Exception as exc:  # noqa: BLE001
                    details.append(f"пропуск «{text}»: {exc}")

        # Дополнительно — ссылки/div с текстом наград
        for pattern in self.REWARD_BUTTON_PATTERNS:
            loc = page.get_by_text(pattern)
            try:
                count = await loc.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(count, 4)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    txt = ((await el.inner_text(timeout=800)) or "").strip()
                    if len(txt) > 80:
                        continue
                    await el.click(force=True, timeout=3000)
                    claimed += 1
                    if re.search(r"карт|card|пак", txt, re.I):
                        cards += 1
                    details.append(f"текст: {txt[:60]}")
                    await asyncio.sleep(random.uniform(0.8, 1.6))
                except Exception:  # noqa: BLE001
                    continue

        return claimed, cards, details

    # ------------------------------------------------------------------
    # Авточтение
    # ------------------------------------------------------------------

    async def read_loop(
        self,
        start_url: Optional[str] = None,
        max_chapters: int = 0,
        on_progress=None,
    ) -> MangaBuffStats:
        """
        Цикл авточтения: скролл главы → пауза → следующая глава.

        Args:
            start_url: URL главы/тайтла (если пусто — self.start_url).
            max_chapters: 0 = бесконечно до request_stop().
            on_progress: optional async callback(stats) после каждой главы.
        """
        self._stop_flag.clear()
        self.stats.running = True
        self.stats.started_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.stats.touch("авточтение запущено")

        async with self._lock:
            if not self.is_started:
                await self.start(headless=True)
            assert self._page is not None
            page = self._page

            url = (start_url or self.start_url).strip()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.config.selector_timeout_ms)
                await asyncio.sleep(2.0)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                self.stats.running = False
                self.stats.touch(f"ошибка открытия: {exc}")
                return self.stats

            chapters_done = 0
            while not self._stop_flag.is_set():
                if max_chapters > 0 and chapters_done >= max_chapters:
                    break

                try:
                    # Сначала забрать всплывающие награды/карты на странице
                    c, cards, _ = await self._click_reward_buttons(page)
                    self.stats.rewards_claimed += c
                    self.stats.cards_claimed += cards

                    await self._smooth_read_chapter(page)
                    self.stats.chapters_read += 1
                    chapters_done += 1
                    self.stats.touch("глава прочитана", page.url)

                    if on_progress is not None:
                        try:
                            await on_progress(self.stats)
                        except Exception:  # noqa: BLE001
                            pass

                    if self._stop_flag.is_set():
                        break

                    moved = await self._go_next_chapter(page)
                    if not moved:
                        self.stats.touch("нет следующей главы — стоп", page.url)
                        break
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("MangaBuff read_loop")
                    self.stats.errors += 1
                    self.stats.touch(f"ошибка чтения: {exc}")
                    await asyncio.sleep(3.0)

            self.stats.running = False
            self.stats.touch("авточтение остановлено", page.url if self._page else "")
            return self.stats

    async def _smooth_read_chapter(self, page: Page) -> int:
        """
        Плавно проскроллить текущую главу вниз с паузами «чтения».
        Возвращает число шагов скролла.
        """
        steps = 0
        try:
            total_height = await page.evaluate("() => document.body.scrollHeight")
            viewport = await page.evaluate("() => window.innerHeight")
        except Exception:  # noqa: BLE001
            total_height, viewport = 3000, 900

        position = 0
        # Пока не дошли до низа (с запасом)
        while position + viewport < total_height - 40:
            if self._stop_flag.is_set():
                break
            # Плавный скролл несколькими микрошагами
            chunk = int(viewport * random.uniform(0.55, 0.85))
            target = min(position + chunk, total_height)
            await self._animate_scroll(page, position, target)
            position = target
            steps += 1
            self.stats.pages_scrolled += 1

            # Пауза «чтения» 5–15 сек (или из настроек)
            delay = random.uniform(self.delay_min_sec, self.delay_max_sec)
            # Прерываем паузу по стопу
            try:
                await asyncio.wait_for(self._stop_flag.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

            # Обновить высоту (lazy-load картинок)
            try:
                total_height = await page.evaluate("() => document.body.scrollHeight")
            except Exception:  # noqa: BLE001
                pass

            # Всплывшие карты во время скролла
            try:
                c, cards, _ = await self._click_reward_buttons(page)
                self.stats.rewards_claimed += c
                self.stats.cards_claimed += cards
            except Exception:  # noqa: BLE001
                pass

        # Финальный доскролл
        try:
            await page.evaluate("() => window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")
            await asyncio.sleep(1.0)
        except Exception:  # noqa: BLE001
            pass
        return steps

    async def _animate_scroll(self, page: Page, start: int, end: int) -> None:
        """Плавный скролл от start до end за несколько кадров."""
        frames = random.randint(6, 12)
        for i in range(1, frames + 1):
            if self._stop_flag.is_set():
                break
            y = int(start + (end - start) * (i / frames))
            try:
                await page.evaluate("(y) => window.scrollTo(0, y)", y)
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(random.uniform(0.04, 0.12))

    async def _go_next_chapter(self, page: Page) -> bool:
        """Клик по кнопке следующей главы."""
        for pattern in self.NEXT_CHAPTER_PATTERNS:
            for getter in (
                lambda p=pattern: page.get_by_role("link", name=p),
                lambda p=pattern: page.get_by_role("button", name=p),
                lambda p=pattern: page.get_by_text(p),
            ):
                try:
                    loc = getter()
                    count = await loc.count()
                except Exception:  # noqa: BLE001
                    continue
                for i in range(min(count, 6)):
                    el = loc.nth(i)
                    try:
                        if not await el.is_visible():
                            continue
                        txt = ((await el.inner_text(timeout=800)) or "").strip()
                        if len(txt) > 60:
                            continue
                        await el.scroll_into_view_if_needed()
                        box = await el.bounding_box()
                        if box:
                            await page.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                        else:
                            await el.click(force=True)
                        await asyncio.sleep(2.0)
                        logger.info("MangaBuff: переход к следующей главе (%r)", txt[:40])
                        return True
                    except Exception:  # noqa: BLE001
                        continue

        # CSS fallback
        for css in (
            "a:has-text('Следующая')",
            "button:has-text('Следующая')",
            "a:has-text('След')",
            "[class*='next']",
            "a[rel='next']",
        ):
            try:
                loc = page.locator(css).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(force=True)
                    await asyncio.sleep(2.0)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


async def main_setup() -> None:
    """CLI: python -m services.mangabuff_service  или  python bot.py --setup-mangabuff"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from config import load_config
    from settings_store import update_settings

    cfg = load_config()
    svc = MangaBuffService(cfg)
    await svc.run_setup()
    update_settings(mangabuff_setup_done=True)
    print("Флаг mangabuff_setup_done=True записан в settings.json")


if __name__ == "__main__":
    asyncio.run(main_setup())
