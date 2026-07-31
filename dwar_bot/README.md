# dwar_bot

Модульный асинхронный бот для браузерной игры «Легенда: Наследие Драконов»
(`dwar.ru`). Проект строится по слоистой архитектуре: конфиг → авторизация →
ядро (браузер / антибот) → прикладные модули (профиль, бои, квесты, таймеры)
→ оркестратор `main.py`.

## Структура

```
dwar_bot/
├── config.py               # константы, селекторы, задержки, ENV / .env / config.local.json
├── logger.py               # логирование в файл + Telegram (в разработке)
├── auth/
│   └── cookie_manager.py   # загрузка/валидация куков, ротация сессий
├── core/
│   ├── browser.py          # Playwright (в разработке)
│   └── anti_bot.py         # human-like поведение (в разработке)
├── modules/
│   ├── stats_parser.py
│   ├── combat_engine.py
│   ├── quest_tracker.py
│   └── timers_manager.py
├── main.py
├── requirements.txt
├── sessions/               # файлы куков (не коммитятся)
├── logs/                   # логи (не коммитятся)
├── snapshots/              # HTML/скриншоты при ошибках
└── state/                  # runtime-метрики ротации
```

Текущий коммит содержит реализованные модули `config.py` и
`auth/cookie_manager.py`.

## Конфигурация

Все параметры имеют разумные значения по умолчанию. Переопределить можно
тремя способами (приоритет в порядке убывания):

1. Переменные окружения (`DWAR_*`);
2. Файл `dwar_bot/.env` (или `.env` в корне репозитория);
3. Файл `dwar_bot/config.local.json` (структурный оверрайд секций).

Основные переменные окружения:

| Переменная                | Назначение                                     | Default          |
|---------------------------|------------------------------------------------|------------------|
| `DWAR_HEADLESS`           | Запуск Playwright в headless-режиме            | `true`           |
| `DWAR_USER_AGENT`         | User-Agent браузера                            | Chrome 125       |
| `DWAR_PROXY_SERVER`       | `http://host:port` для прокси                  | —                |
| `DWAR_PROXY_USER`/`_PASS` | Учётные данные прокси                          | —                |
| `DWAR_TIMEOUT_MS`         | Дефолтный timeout для селекторов               | `15000`          |
| `DWAR_TG_ENABLED`         | Включить Telegram-уведомления                  | `false`          |
| `DWAR_TG_TOKEN`           | Токен Telegram-бота                            | —                |
| `DWAR_TG_CHAT_ID`         | Chat ID для уведомлений                        | —                |
| `DWAR_ROTATION`           | Включить ротацию мультиаккаунтов               | `false`          |
| `DWAR_ROTATION_COOLDOWN`  | Кулдаун сессии после ошибки, сек               | `900`            |
| `DWAR_LOG_LEVEL`          | `DEBUG` / `INFO` / `WARNING` / …               | `INFO`           |

## Куки

Положите файлы сессий в `dwar_bot/sessions/`. Поддерживаются два формата:

* **Cookie-Editor JSON** — экспорт из расширения Cookie-Editor (Chrome/Firefox).
  Ожидается массив объектов с полями `name`, `value`, `domain`, `path`,
  `expirationDate`, `httpOnly`, `secure`, `sameSite`.
* **Netscape HTTP Cookie File** — текстовый формат `curl`/`wget`
  (7 полей, разделённых табом). Строки с префиксом `#HttpOnly_`
  распознаются автоматически.

Обязательные куки (регулируются `REQUIRED_COOKIE_NAMES` в `config.py`):
`PHPSESSID`. Отсутствие этих куков приводит к `CookieValidationError`.

Пример использования:

```python
from dwar_bot.auth import CookieManager

cm = CookieManager()
cm.load_from_directory()
session = cm.acquire()                    # выбирает сессию по стратегии
pw_cookies = cm.to_playwright_cookies(session)

# await context.add_cookies(pw_cookies)   # в модуле core.browser

try:
    ...  # действия в браузере
except AuthError:
    cm.mark_failure(session, reason="captcha")
else:
    cm.mark_success(session)
```

## Установка

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r dwar_bot/requirements.txt
python3 -m playwright install chromium
```

## Дисклеймер

Использование автоматизированных клиентов может нарушать пользовательское
соглашение игры. Проект публикуется в образовательных целях; ответственность
за использование лежит на пользователе.
