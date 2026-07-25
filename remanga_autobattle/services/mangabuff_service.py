"""
mangabuff_service.py — Playwright-автоматизация mangabuff.ru.

Отдельный persistent context: user_data_mangabuff.

Возможности:
- автологин (email/password из env или CLI);
- фарм: популярные тайтлы из каталога, чтение большого числа глав;
- сбор наград/модалок/уведомлений/карточных дейликов;
- обход основных разделов сайта (макеты);
- редкие «живые» комментарии (без эмодзи, с маленькой буквы, без точки в конце);
- ночной перерыв 4 часа, днём — круглосуточное чтение;
- человекоподобные задержки и скролл.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
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

# Комментарии очень редко — снижение риска жалоб
COMMENT_CHANCE = 0.04
MIN_CHAPTERS_BETWEEN_COMMENTS = 18


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
        self._read_urls: set[str] = set()

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

    def set_delay(self, delay_min: float, delay_max: float) -> None:
        self.delay_min_sec = max(1.0, float(delay_min))
        self.delay_max_sec = max(self.delay_min_sec, float(delay_max))

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
            "button:has-text('Понятно')",
            "button:has-text('Закрыть')",
        )
        for sel in selectors:
            try:
                loc = page.locator(sel)
                n = await loc.count()
                for i in range(min(n, 3)):
                    btn = loc.nth(i)
                    if await btn.is_visible(timeout=800):
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

    async def _open_first_chapter(self, title_href: str) -> Optional[str]:
        assert self._page is not None
        page = self._page
        slug_m = re.search(r"/manga/([^/?#]+)/?$", title_href.rstrip("/"))
        slug = slug_m.group(1) if slug_m else ""

        # Прямой URL первой главы — самый надёжный путь
        if slug:
            for candidate in (
                f"https://mangabuff.ru/manga/{slug}/1/1",
                f"https://mangabuff.ru/manga/{slug}/1/0",
            ):
                if await self._safe_goto(page, candidate):
                    await self._dismiss_overlays(page)
                    title = await page.title()
                    if "404" not in title and re.search(
                        r"/manga/[^/]+/\d+/\d+", page.url
                    ):
                        # /1/0 часто редиректит/ведёт в ридер — ок
                        if page.url.rstrip("/").endswith("/0"):
                            # попробуем перейти на реальную 1
                            nxt = f"https://mangabuff.ru/manga/{slug}/1/1"
                            await self._safe_goto(page, nxt)
                            await self._dismiss_overlays(page)
                        return page.url

        if not await self._safe_goto(page, title_href):
            return None
        await self._dismiss_overlays(page)
        try:
            read_btn = page.get_by_role("link", name=re.compile(r"^Читать$", re.I))
            if await read_btn.count():
                await read_btn.first.click()
                await self._human_pause(2.0, 3.5)
                await self._dismiss_overlays(page)
                if re.search(r"/manga/[^/]+/\d+/\d+", page.url):
                    return page.url
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
        steps = 0
        try:
            total_height = await page.evaluate("() => document.body.scrollHeight || 4000")
            viewport = await page.evaluate("() => window.innerHeight || 900")
        except Exception:  # noqa: BLE001
            total_height, viewport = 4000, 900

        # лимит шагов — чтобы не зависнуть на бесконечном lazy-load
        max_steps = 25
        position = 0
        while position + viewport < total_height - 40 and steps < max_steps:
            if self._stop_flag.is_set():
                break
            chunk = int(viewport * random.uniform(0.55, 0.9))
            target = min(position + chunk, total_height)
            await self._animate_scroll(page, position, target)
            position = target
            steps += 1
            self.stats.pages_scrolled += 1

            # короче пауза на шаге, полная «человеческая» — раз в 2–3 шага
            delay = random.uniform(self.delay_min_sec, self.delay_max_sec)
            if steps % 2 == 0:
                delay = random.uniform(
                    max(2.0, self.delay_min_sec * 0.6),
                    max(4.0, self.delay_max_sec * 0.7),
                )
            try:
                await asyncio.wait_for(self._stop_flag.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

            try:
                total_height = await page.evaluate(
                    "() => document.body.scrollHeight || 4000"
                )
            except Exception:  # noqa: BLE001
                pass

            if steps % 3 == 0:
                c, cards, _ = await self._click_reward_buttons(page)
                self.stats.rewards_claimed += c
                self.stats.cards_claimed += cards

        try:
            await page.evaluate(
                "() => window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})"
            )
            await self._human_pause(0.6, 1.2)
        except Exception:  # noqa: BLE001
            pass
        return steps

    async def _animate_scroll(self, page: Page, start: int, end: int) -> None:
        frames = random.randint(6, 14)
        for i in range(1, frames + 1):
            if self._stop_flag.is_set():
                break
            y = int(start + (end - start) * (i / frames))
            try:
                await page.evaluate("(y) => window.scrollTo(0, y)", y)
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(random.uniform(0.04, 0.14))

    async def _go_next_chapter(self, page: Page) -> bool:
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
                        href = await el.get_attribute("href")
                        await el.click(force=True)
                    await self._human_pause(1.8, 3.2)
                    await self._dismiss_overlays(page)
                    if href or re.search(r"/manga/[^/]+/\d+/\d+", page.url):
                        return True
                except Exception:  # noqa: BLE001
                    continue

        # URL-инкремент
        m = re.search(r"(https://mangabuff\.ru/manga/[^/]+/)(\d+)/(\d+)", page.url)
        if m:
            base, vol, ch = m.group(1), int(m.group(2)), int(m.group(3))
            nxt = f"{base}{vol}/{ch + 1}"
            before = page.url
            if await self._safe_goto(page, nxt):
                if page.url != before and "404" not in (await page.title()):
                    return True
        return False

    # ------------------------------------------------------------------
    # Comments (rare, human-like, low report risk)
    # ------------------------------------------------------------------

    async def _maybe_comment(self, page: Page) -> bool:
        self._chapters_since_comment += 1
        if self._chapters_since_comment < MIN_CHAPTERS_BETWEEN_COMMENTS:
            return False
        if random.random() > COMMENT_CHANCE:
            return False

        try:
            # открыть панель комментариев
            btn = page.locator("button.reader__show-comments-btn").first
            if await btn.count() and await btn.is_visible():
                await btn.click(force=True)
                await self._human_pause(1.0, 2.0)

            samples = await page.locator(".comments__body").all_inner_texts()
            samples = [s.strip() for s in samples if s and 8 <= len(s.strip()) <= 120]
            text = self._craft_comment(samples)
            if not text:
                return False

            # фильтр риска жалоб
            if not self._is_safe_comment(text):
                return False

            area = page.locator(".comments__send-form textarea").first
            if await area.count() == 0:
                return False
            await area.click()
            await self._human_pause(0.4, 1.0)
            # печать по символам
            for ch in text:
                await area.type(ch, delay=random.randint(40, 140))
            await self._human_pause(0.8, 2.0)
            send = page.locator("button.comments__send-btn").first
            await send.click(force=True)
            await self._human_pause(1.5, 3.0)
            self.stats.comments_posted += 1
            self._chapters_since_comment = 0
            self.stats.touch(f"коммент: {text[:40]}")
            logger.info("MangaBuff comment posted: %s", text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("comment skip: %s", exc)
            return False

    def _craft_comment(self, samples: Sequence[str]) -> str:
        # Базовые нейтральные шаблоны + вариации из чужих комментов
        templates = [
            "ну интересно пошло",
            "глава норм зашла",
            "пока держит внимание",
            "рисуют приятно",
            "хочу дальше уже",
            "атмосфера огонь",
            "персонажи живые",
            "неплохо закрутили",
            "жду продолжение",
            "мне зашло",
        ]
        cleaned = []
        for s in samples:
            s = s.strip()
            s = re.sub(r"https?://\S+", "", s)
            s = re.sub(r"[\U00010000-\U0010ffff]", "", s)  # эмодзи-плоско
            s = re.sub(r"[^\w\sа-яА-ЯёЁ.,!?\-]", "", s, flags=re.UNICODE)
            s = s.strip(" .!?,;:-")
            if 6 <= len(s) <= 90 and not re.search(
                r"дур|идиот|убей|суицид|ненавиж|репорт|жалоб|спам|реклам|подпиш",
                s,
                re.I,
            ):
                cleaned.append(s.lower())

        if cleaned and random.random() < 0.55:
            base = random.choice(cleaned)
            # укоротить / слегка перефразировать
            words = base.split()
            if len(words) > 8:
                words = words[: random.randint(4, 8)]
            text = " ".join(words)
        else:
            text = random.choice(templates)

        text = text.strip().lower()
        text = text.rstrip(".,!?;:…")
        # без эмодзи и заглавной
        if text:
            text = text[0].lower() + text[1:]
        return text[:90]

    def _is_safe_comment(self, text: str) -> bool:
        if not text or len(text) < 5:
            return False
        if re.search(r"[A-ZА-Я]{4,}", text):
            return False
        if re.search(r"[!?]{2,}|@{2,}|#\w+", text):
            return False
        if any(ord(c) > 0x1F300 for c in text):
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
        )
        low = text.lower()
        return not any(b in low for b in banned)

    # ------------------------------------------------------------------
    # Main farm loop
    # ------------------------------------------------------------------

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

                    logger.info("MangaBuff open title %s", title.get("slug"))
                    start_url = await self._open_first_chapter(title["href"])
                    if not start_url or not re.search(
                        r"/manga/[^/]+/\d+/\d+", start_url
                    ):
                        logger.warning("MangaBuff cannot open %s", title.get("slug"))
                        continue
                    self.stats.titles_visited += 1
                    self.stats.touch(f"тайтл: {title.get('title','')[:40]}", start_url)
                    logger.info(
                        "MangaBuff reading %s from %s",
                        title.get("slug"),
                        start_url,
                    )
                    self._persist_stats()

                    chapters_this_title = 0
                    max_per_title = random.randint(40, 120)
                    while (
                        not self._stop_flag.is_set()
                        and chapters_this_title < max_per_title
                    ):
                        await self._await_night_break_if_needed()
                        if self._stop_flag.is_set():
                            break

                        url = page.url.split("?")[0]
                        if not re.search(r"/manga/[^/]+/\d+/\d+", url):
                            logger.warning(
                                "MangaBuff not on chapter page: %s — skip title",
                                url,
                            )
                            break
                        if url in self._read_urls:
                            if not await self._go_next_chapter(page):
                                break
                            continue
                        self._read_urls.add(url)
                        logger.info("MangaBuff scroll chapter %s", url)

                        await self._dismiss_overlays(page)
                        c, cards, _ = await self._click_reward_buttons(page)
                        self.stats.rewards_claimed += c
                        self.stats.cards_claimed += cards

                        steps = await self._smooth_read_chapter(page)
                        self.stats.chapters_read += 1
                        chapters_this_title += 1
                        self.stats.touch("глава прочитана", page.url)
                        self._persist_stats()
                        logger.info(
                            "MangaBuff chapter done steps=%s total_chapters=%s url=%s",
                            steps,
                            self.stats.chapters_read,
                            page.url,
                        )

                        await self._maybe_comment(page)

                        if on_progress is not None:
                            try:
                                await on_progress(self.stats)
                            except Exception:  # noqa: BLE001
                                pass

                        await self._human_pause(2.0, 6.0)
                        if not await self._go_next_chapter(page):
                            break

                    await self._human_pause(8.0, 20.0)

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
