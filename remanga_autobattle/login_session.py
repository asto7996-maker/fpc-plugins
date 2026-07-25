"""
login_session.py — авторизация Remanga через API и запись сессии в user_data.

Использование (на сервере):
  REMANGA_USER='email' REMANGA_PASSWORD='pass' python login_session.py

НЕ храните пароль в git / .env репозитория. Передавайте только через env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

from config import load_config

logger = logging.getLogger(__name__)

LOGIN_URL = "https://api.remanga.org/api/users/login/"


def api_login(username: str, password: str) -> dict:
    """POST /api/users/login/ → content.access_token."""
    payload = json.dumps({"user": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        LOGIN_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://remanga.org/",
            "Origin": "https://remanga.org",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Login HTTP {exc.code}: {body[:500]}") from exc

    content = data.get("content") or {}
    token = content.get("access_token")
    if not token:
        raise RuntimeError(f"Нет access_token в ответе: {data!r}")
    return content


async def inject_session(token: str, user_id: int, battle_url: str, user_data_dir: Path) -> bool:
    """Записать токен в persistent profile и проверить страницу дуэли."""
    user_data_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
            ignore_default_args=["--enable-automation"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Сначала заходим на домен, чтобы localStorage был доступен
        await page.goto("https://remanga.org/", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(2)

        # Remanga хранит токен в localStorage под разными ключами в разных версиях UI
        await page.evaluate(
            """([token, userId]) => {
                const keys = [
                    'token', 'access_token', 'accessToken',
                    'auth_token', 'user_token', 'jwt'
                ];
                for (const k of keys) {
                    try { localStorage.setItem(k, token); } catch (e) {}
                }
                // Часто лежит объект пользователя / auth
                try {
                    localStorage.setItem('user', JSON.stringify({ id: userId, access_token: token }));
                } catch (e) {}
                try {
                    localStorage.setItem('auth', JSON.stringify({ access_token: token, token }));
                } catch (e) {}
                // cookie-fallback
                document.cookie = `token=${token}; path=/; max-age=31536000; SameSite=Lax`;
                document.cookie = `access_token=${token}; path=/; max-age=31536000; SameSite=Lax`;
            }""",
            [token, user_id],
        )

        # Cookie через Playwright API (надёжнее)
        await context.add_cookies(
            [
                {
                    "name": "token",
                    "value": token,
                    "domain": ".remanga.org",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "user_id",
                    "value": str(user_id),
                    "domain": ".remanga.org",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )

        # Перезагрузка с токеном
        await page.goto("https://remanga.org/", wait_until="domcontentloaded", timeout=60_000)
        await asyncio.sleep(2)
        # Повторная инъекция после reload (SPA иногда затирает storage)
        await page.evaluate(
            """([token, userId]) => {
                for (const k of ['token', 'access_token', 'accessToken']) {
                    try { localStorage.setItem(k, token); } catch (e) {}
                }
                try {
                    localStorage.setItem('user', JSON.stringify({ id: userId, access_token: token }));
                } catch (e) {}
            }""",
            [token, user_id],
        )

        # Страница дуэли
        if "#" in battle_url:
            base, _, frag = battle_url.partition("#")
            await page.goto(base, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(1.5)
            await page.evaluate(
                "(f) => { window.location.hash = f.startsWith('/') ? f : '/' + f; }",
                frag,
            )
            await asyncio.sleep(3)
        else:
            await page.goto(battle_url, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(3)

        body = ""
        try:
            body = await page.locator("body").inner_text(timeout=8_000)
        except Exception:  # noqa: BLE001
            pass

        shot = Path(__file__).resolve().parent / "login_check.png"
        try:
            await page.screenshot(path=str(shot), full_page=True)
            logger.info("Скриншот: %s", shot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("screenshot failed: %s", exc)

        ok_markers = ("в бой", "подготовка к дуэли", "твой отряд", "дуэль")
        fail_markers = ("вход/регистрация", "войти", "checking your browser", "ddos-guard")
        low = body.lower()
        has_battle = any(m in low for m in ok_markers)
        looks_logged_out = "вход/регистрация" in low or ("войти" in low and "в бой" not in low)

        logger.info("URL после логина: %s", page.url)
        logger.info("Фрагмент текста: %s", body[:400].replace("\n", " | "))

        await context.close()
        return has_battle and not looks_logged_out


async def amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    user = os.getenv("REMANGA_USER", "").strip()
    password = os.getenv("REMANGA_PASSWORD", "").strip()
    if not user or not password:
        print("Задайте REMANGA_USER и REMANGA_PASSWORD", file=sys.stderr)
        return 2

    config = load_config()
    logger.info("Логин через API как %s ...", user)
    content = api_login(user, password)
    token = content["access_token"]
    user_id = int(content.get("id") or 0)
    logger.info("OK: user_id=%s username=%s", user_id, content.get("username"))

    # Сохраним токен локально (не в git) — для повторной инъекции
    token_path = Path(__file__).resolve().parent / ".remanga_token.json"
    token_path.write_text(
        json.dumps({"access_token": token, "id": user_id, "username": content.get("username")}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(token_path, 0o600)

    ok = await inject_session(token, user_id, config.battle_url, config.user_data_dir)
    if ok:
        print("✅ Сессия сохранена в", config.user_data_dir)
        return 0
    print(
        "⚠️ Токен записан, но кнопка «В БОЙ» на странице не подтверждена.\n"
        "Проверьте login_check.png и при необходимости повторите.",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
