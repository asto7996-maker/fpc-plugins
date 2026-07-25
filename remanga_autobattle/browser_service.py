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
import json
import logging
import random
import re
import time
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
        """Краткий человекочитаемый отчёт для чата (основа — текст кнопки)."""
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
        # Главный отчёт — текст с кнопки на сайте
        if self.raw_text and self.outcome not in (BattleOutcome.ERROR,):
            snippet = self.raw_text.strip()
            if len(snippet) > 400:
                snippet = snippet[:397] + "..."
            lines.append(f"📝 Кнопка: {snippet}")
        if self.rating_change:
            lines.append(f"📈 Рейтинг: {self.rating_change}")
        if self.rewards:
            lines.append(f"🎁 Награды: {self.rewards}")
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

    # Текстовые маркеры кнопки «В БОЙ» / «戰 В БОЙ» (как на murim-cards)
    BATTLE_BUTTON_PATTERNS = (
        re.compile(r"戰\s*в\s*бой", re.IGNORECASE),
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
                await self._navigate_battle_page(page)
            except Exception as exc:  # noqa: BLE001
                logger.error("Не удалось открыть страницу боёв: %s", exc)
                return BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message=f"Не удалось открыть страницу боёв: {exc}",
                )

            # Проверка Cloudflare / DDoS-Guard / неавторизован
            body_text = await self._safe_body_text(page)
            challenge = self._detect_challenge(body_text or "", page.url)
            if challenge:
                return BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message=challenge,
                    raw_text=(body_text or "")[:500],
                )

            # Короткая пауза: SPA (#/duel) дорисует кнопку
            await asyncio.sleep(random.uniform(1.5, 3.0))

            button = await self._find_battle_button(page)
            if button is None:
                body_text = await self._safe_body_text(page)
                if body_text and self.COOLDOWN_MARKERS.search(body_text):
                    return BattleResult(
                        outcome=BattleOutcome.SKIPPED,
                        message="Кнопка «В БОЙ» недоступна (энергия/кулдаун).",
                        raw_text=body_text[:500],
                    )
                # Сохраняем скриншот для диагностики на сервере
                try:
                    shot = self.config.user_data_dir.parent / "last_battle_error.png"
                    await page.screenshot(path=str(shot), full_page=True)
                    logger.warning("Скриншот ошибки: %s", shot)
                except Exception:  # noqa: BLE001
                    pass
                return BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message=(
                        "Кнопка «В БОЙ» не найдена. "
                        "Сделайте setup (вход в аккаунт): "
                        "systemctl stop remanga-autobattle && "
                        "cd /root/remanga_autobattle && "
                        "source .venv/bin/activate && python bot.py --setup"
                    ),
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

            # На murim-cards кнопка часто div/span: is_disabled() и class*disabled
            # дают ложные срабатывания. Считаем неактивной только явную блокировку.
            try:
                disabled = await button.is_disabled()
            except Exception:  # noqa: BLE001
                disabled = False
            aria_disabled = (await button.get_attribute("aria-disabled") or "").lower()
            looks_disabled = disabled or aria_disabled in {"true", "1"}
            if looks_disabled:
                body_text = await self._safe_body_text(page)
                return BattleResult(
                    outcome=BattleOutcome.SKIPPED,
                    message="Кнопка «В БОЙ» неактивна (восстановление энергии / кулдаун).",
                    raw_text=(body_text or "")[:500],
                )

            # Текст кнопки до клика — чтобы понять, когда он обновится
            try:
                text_before = ((await button.inner_text(timeout=3_000)) or "").strip()
            except Exception:  # noqa: BLE001
                text_before = ""

            # Короткая пауза перед кликом (антибан, но не блокируем интервал 30с)
            await asyncio.sleep(random.uniform(
                min(1.5, self.config.human_delay_min_sec),
                min(4.0, self.config.human_delay_max_sec),
            ))

            try:
                await button.scroll_into_view_if_needed()
                await button.click(timeout=self.config.selector_timeout_ms)
                logger.info("Клик по «В БОЙ» выполнен. Текст до клика: %r", text_before)
            except Exception as exc:  # noqa: BLE001
                logger.error("Не удалось нажать «В БОЙ»: %s", exc)
                return BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message=f"Клик по «В БОЙ» не удался: {exc}",
                )

            # Отчёт берём из текста кнопки после боя
            result = await self._wait_and_parse_result_from_button(
                page,
                button,
                text_before=text_before,
            )
            logger.info("Итог боя: %s — %s", result.outcome.value, result.message)
            return result

    async def _find_battle_button(self, page: Page):
        """
        Найти кнопку «戰 В БОЙ» на экране дуэли murim-cards.

        На UI текст часто разбит на строки: «戰» + «В БОЙ».
        """
        # 0. Самый надёжный вариант для текущего UI: regex по тексту
        try:
            loc = page.get_by_text(re.compile(r"戰\s*В\s*БОЙ|В\s*БОЙ", re.I))
            count = await loc.count()
            for i in range(min(count, 12)):
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                # Поднимаемся к кликабельному родителю (button / role=button / кликабельный div)
                handle = await el.evaluate_handle(
                    """(node) => {
                        let n = node;
                        for (let i = 0; i < 6 && n; i++) {
                            const tag = (n.tagName || '').toLowerCase();
                            const role = n.getAttribute && n.getAttribute('role');
                            const style = window.getComputedStyle(n);
                            const clickable = tag === 'button' || tag === 'a' || role === 'button'
                                || style.cursor === 'pointer';
                            const txt = (n.innerText || '').replace(/\\s+/g, ' ');
                            if (clickable && /в\\s*бой/i.test(txt)) return n;
                            n = n.parentElement;
                        }
                        return node;
                    }"""
                )
                element = handle.as_element()
                if element is not None:
                    return element
        except Exception:  # noqa: BLE001
            pass

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

        # 2. CSS-fallback
        css_candidates = [
            "button:has-text('В БОЙ')",
            "button:has-text('В бой')",
            "[role='button']:has-text('В БОЙ')",
            "[role='button']:has-text('戰')",
            "a:has-text('В БОЙ')",
            "[class*='duel'] button",
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

    async def _wait_and_parse_result_from_button(
        self,
        page: Page,
        button,
        text_before: str = "",
    ) -> BattleResult:
        """
        Дождаться обновления текста кнопки после боя и собрать отчёт из неё.

        На Remanga результат (победа/поражение/рейтинг) обычно появляется
        прямо в тексте боевой кнопки — именно его отправляем в Telegram.
        """
        timeout_ms = self.config.selector_timeout_ms  # по умолчанию 30 сек
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        button_text = text_before
        changed = False

        # Поллим текст кнопки, пока он не изменится или не появится маркер результата
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                # Кнопка могла перерисоваться — ищем заново, если старый locator «протух»
                current = button
                try:
                    current_text = ((await current.inner_text(timeout=1_500)) or "").strip()
                except Exception:  # noqa: BLE001
                    refreshed = await self._find_battle_button(page)
                    if refreshed is None:
                        continue
                    button = refreshed
                    current_text = ((await button.inner_text(timeout=1_500)) or "").strip()

                if not current_text:
                    continue

                button_text = current_text
                text_changed = bool(text_before) and current_text != text_before
                has_result = bool(
                    self.WIN_MARKERS.search(current_text)
                    or self.LOSE_MARKERS.search(current_text)
                    or self.DRAW_MARKERS.search(current_text)
                    or self.RATING_MARKERS.search(current_text)
                    or re.search(r"[+\-−–]\s*\d+", current_text)
                )
                # Больше не «В БОЙ» — тоже признак обновления
                no_longer_battle = not re.search(r"в\s*бой", current_text, re.I)

                if text_changed or has_result or (text_before and no_longer_battle):
                    changed = True
                    # Небольшая пауза: текст кнопки может дорисоваться (рейтинг/награда)
                    await asyncio.sleep(0.8)
                    try:
                        button_text = ((await button.inner_text(timeout=1_500)) or current_text).strip()
                    except Exception:  # noqa: BLE001
                        button_text = current_text
                    break
            except Exception:  # noqa: BLE001
                continue

        # Fallback: если кнопка не обновилась — пробуем модалку/диалог, но приоритет у кнопки
        if not changed or not button_text:
            for scope in ("[role='dialog']", "[class*='modal']", "[class*='result']"):
                try:
                    loc = page.locator(scope).first
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                    alt = ((await loc.inner_text(timeout=2_000)) or "").strip()
                    if alt:
                        button_text = alt
                        changed = True
                        break
                except Exception:  # noqa: BLE001
                    continue

        raw_text = button_text or text_before or ""
        outcome = BattleOutcome.UNKNOWN
        message = "Результат считан с кнопки."

        if self.WIN_MARKERS.search(raw_text):
            outcome = BattleOutcome.WIN
            message = "Бой выигран."
        elif self.LOSE_MARKERS.search(raw_text):
            outcome = BattleOutcome.LOSE
            message = "Бой проигран."
        elif self.DRAW_MARKERS.search(raw_text):
            outcome = BattleOutcome.DRAW
            message = "Ничья."
        elif self.COOLDOWN_MARKERS.search(raw_text):
            outcome = BattleOutcome.SKIPPED
            message = "Бой пропущен (энергия/кулдаун) — по тексту кнопки."
        elif changed:
            message = "Бой завершён (текст кнопки обновлён)."
        else:
            message = f"Текст кнопки не обновился за {timeout_ms // 1000} сек."
            outcome = BattleOutcome.UNKNOWN

        rating_change = None
        rating_match = self.RATING_MARKERS.search(raw_text)
        if rating_match:
            rating_change = rating_match.group(0).strip()
        else:
            plus_minus = re.search(r"([+\-−–]\s*\d+)", raw_text)
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

    async def _inject_saved_token(self, page: Page) -> None:
        """Если есть .remanga_token.json — записать access_token в localStorage/cookie."""
        token_path = Path(__file__).resolve().parent / ".remanga_token.json"
        if not token_path.exists():
            return
        try:
            data = json.loads(token_path.read_text(encoding="utf-8"))
            token = (data.get("access_token") or "").strip()
            user_id = int(data.get("id") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось прочитать .remanga_token.json: %s", exc)
            return
        if not token:
            return

        await page.evaluate(
            """([token, userId]) => {
                const keys = ['token', 'access_token', 'accessToken', 'auth_token', 'user_token'];
                for (const k of keys) {
                    try { localStorage.setItem(k, token); } catch (e) {}
                }
                try {
                    localStorage.setItem('user', JSON.stringify({ id: userId, access_token: token }));
                    localStorage.setItem('auth', JSON.stringify({ access_token: token, token }));
                } catch (e) {}
            }""",
            [token, user_id],
        )
        try:
            assert self._context is not None
            await self._context.add_cookies(
                [
                    {
                        "name": "token",
                        "value": token,
                        "domain": ".remanga.org",
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ]
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("cookie inject: %s", exc)
        logger.info("Токен Remanga инжектирован в профиль (user_id=%s)", user_id)

    async def _navigate_battle_page(self, page: Page) -> None:
        """
        Открыть страницу боёв.

        Важно:
        - НЕ ждём networkidle — у Remanga SPA/сокеты часто не затихают никогда.
        - URL с hash (#/duel) открываем как base + hash (иначе SPA-роутер может не сработать).
        """
        # Сначала домен + токен, потом боевая страница
        await page.goto("https://remanga.org/", wait_until="domcontentloaded", timeout=self.config.selector_timeout_ms)
        await self._inject_saved_token(page)

        url = self.config.battle_url.strip()
        # Прямой переход (в т.ч. с #/duel). Затем форсируем hash, если SPA ушла на /map.
        await page.goto(url, wait_until="domcontentloaded", timeout=self.config.selector_timeout_ms)
        await self._inject_saved_token(page)
        await asyncio.sleep(2.0)

        if "#" in url:
            _, _, fragment = url.partition("#")
            want = fragment if fragment.startswith("/") else f"/{fragment}"
            # Повторно выставляем hash (murim-cards иногда редиректит на #/map)
            for _ in range(3):
                current_hash = await page.evaluate("() => window.location.hash || ''")
                if want in current_hash or current_hash.lstrip("#") == want.lstrip("#"):
                    break
                await page.evaluate("(frag) => { window.location.hash = frag; }", want)
                await asyncio.sleep(1.5)

        # Ждём экран дуэли / кнопку «В БОЙ»
        try:
            await page.wait_for_selector(
                "text=/в\\s*бой|подготовка к дуэли|戰/i",
                timeout=min(self.config.selector_timeout_ms, 20_000),
                state="visible",
            )
        except Exception:  # noqa: BLE001
            logger.warning("Экран дуэли не появился сразу, продолжаю поиск кнопки...")

        try:
            await page.wait_for_load_state("load", timeout=8_000)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _detect_challenge(body_text: str, current_url: str) -> Optional[str]:
        """Вернуть текст ошибки, если страница — капча / блок / не логин."""
        low = (body_text or "").lower()
        markers = (
            "checking your browser",
            "just a moment",
            "ddos-guard",
            "cloudflare",
            "cf-browser-verification",
            "внимание: доступ ограничен",
            "подтвердите, что вы не робот",
            "are you a robot",
            "captcha",
        )
        if any(m in low for m in markers):
            return (
                "Сайт показал защиту (Cloudflare/DDoS-Guard). "
                "Нужен ручной setup с входом в аккаунт:\n"
                "systemctl stop remanga-autobattle && "
                "cd /root/remanga_autobattle && source .venv/bin/activate && "
                "python bot.py --setup"
            )
        if "вход" in low and "регистрац" in low and "в бой" not in low:
            return (
                "Похоже, вы не авторизованы на Remanga. "
                "Выполните setup и войдите в аккаунт."
            )
        return None

    async def _human_delay(self, reason: str = "") -> None:
        """Случайная пауза для снижения риска бана."""
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
