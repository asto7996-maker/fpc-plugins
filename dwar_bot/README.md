# dwar_bot

Модульный асинхронный бот для браузерной игры **«Легенда: Наследие Драконов»** (Dwar).

## Архитектура

```
dwar_bot/
├── config.py              # Конфигурация: URL, селекторы, задержки, креды, ENV
├── logger.py              # (WIP) логирование в bot.log + Telegram
├── auth/
│   └── cookie_manager.py  # Cookie Editor JSON / Netscape, валидация, ротация
├── core/
│   ├── browser.py         # (WIP) Playwright async + перехват XHR
│   └── anti_bot.py        # (WIP) human-like движения мыши, задержки
├── modules/
│   ├── stats_parser.py    # (WIP) профиль, рюкзак, деньги, статы
│   ├── combat_engine.py   # (WIP) эликсиры, касты, разбор лога боя
│   ├── quest_tracker.py   # (WIP) сюжет, диалоги NPC, ветки
│   └── timers_manager.py  # (WIP) кулдауны, профессии, регены
└── main.py                # (WIP) главный async-цикл, оркестратор
```

## Требования

* Python **3.11+**
* Playwright (async), либо `requests`/`beautifulsoup4` для HTTP-режима

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r dwar_bot/requirements.txt
python -m playwright install chromium
```

## Конфигурация

Всё берётся из ENV или `.env` (в корне проекта или в `dwar_bot/.env`).
Ключевые переменные:

| Переменная                | Назначение                                    |
| ------------------------- | --------------------------------------------- |
| `DWAR_LOGIN`              | Логин игрока (для авторизации без кук)        |
| `DWAR_PASSWORD`           | Пароль                                        |
| `DWAR_COOKIES_FILE`       | Абсолютный путь к файлу кук                   |
| `DWAR_COOKIES_DIR`        | Каталог с профилями кук (для ротации)         |
| `DWAR_HEADLESS`           | `true`/`false` — headless-режим Playwright    |
| `DWAR_PROXY_SERVER`       | Прокси (`http://host:port`)                   |
| `DWAR_TG_TOKEN`           | Токен Telegram-бота для уведомлений           |
| `DWAR_TG_CHAT_ID`         | Chat ID получателя уведомлений                |
| `DWAR_LOG_LEVEL`          | `DEBUG` / `INFO` / `WARNING` / `ERROR`        |

Полный список — в `dwar_bot/config.py`.

## Cookie Manager

Поддерживает два формата:

1. **Cookie Editor JSON** — экспорт из расширения [Cookie-Editor](https://cookie-editor.com/):
   массив объектов или `{"cookies": [...]}`. Поля: `name`, `value`, `domain`,
   `path`, `expirationDate`, `secure`, `httpOnly`, `sameSite`.
2. **Netscape `cookies.txt`** — стандартный формат curl/wget, включая префикс
   `#HttpOnly_` перед доменом.

Пример:

```python
from dwar_bot.auth.cookie_manager import CookieManager

cm = CookieManager()
cm.discover()                       # ищет в sessions/cookies/*.{json,txt,cookies}
profile = cm.acquire(strategy="lru")

# Playwright
await context.add_cookies(profile.to_playwright())

# Requests
session.cookies = profile.to_requests_jar()

# Обновить после сессии
refreshed = await context.cookies()
cm.release(profile, refreshed_cookies=refreshed, persist=True)
```

Каталоги `sessions/cookies/`, `sessions/storage_state/`, `logs/` создаются
автоматически при первом импорте `dwar_bot.config`.

## Roadmap

- [x] `config.py` — конфигурация
- [x] `auth/cookie_manager.py` — куки
- [ ] `logger.py` — логгер + Telegram sink
- [ ] `core/browser.py` — Playwright wrapper
- [ ] `core/anti_bot.py` — human-like поведение
- [ ] `modules/stats_parser.py`
- [ ] `modules/combat_engine.py`
- [ ] `modules/quest_tracker.py`
- [ ] `modules/timers_manager.py`
- [ ] `main.py` — оркестратор
