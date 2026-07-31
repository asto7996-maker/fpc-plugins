# DwarBot — «Легенда: Наследие Драконов»

Асинхронный бот для браузерной игры **Легенда: Наследие Драконов** (серверы `w1` / `w2`).  
Работает через **Playwright + Chromium**, эмулирует человеческие клики (кривые Безье), управляется из **Telegram**, умеет восстанавливаться после сбоев и упакован в **Docker** для VPS.

> Это инструмент автоматизации клиентского интерфейса. Использование может нарушать правила игры. Автор не несёт ответственности за блокировки аккаунтов.

---

## Возможности

- Авто-бои (удары верх/сердце/низ) и эликсиры при низком HP  
- Квесты / переходы по локациям  
- Фарм профессий (сбор ресурсов)  
- Аукцион: скан лотов, выкуп по watch-list, выставление  
- Telegram remote: `/stats`, `/screenshot`, `/pause`, `/resume`, `/stop`  
- Антидетект: Безье-мышь, случайные задержки, реакция на капчу  
- CrashRecovery + SelfDiagnostics (скрин / HTML / console dump)  
- Analytics KPI + периодические отчёты  
- BackgroundScheduler: почта, daily, бафы, cleanup  
- Docker Compose для Ubuntu/Debian VPS  

---

## Архитектура

```text
.
├── main.py                      # Точка входа: python main.py
├── requirements.txt             # Python-зависимости
├── .env.example                 # Шаблон секретов / настроек
├── Dockerfile / docker-compose.yml
├── auth/
│   └── cookies.json             # Cookie Editor JSON (не в git)
├── config/
│   └── selectors.py             # CSS/XPath и фреймы (DevTools-настройка)
├── captchas/                    # Скриншоты капчи (том Docker)
├── marketing/
│   └── funpay_listing.txt       # Тексты для FunPay / TG-каталогов
└── dwar_bot/                    # Основной пакет бота
    ├── config.py                # BotConfig, задержки, URL серверов, env
    ├── logger.py                # Rotating log + Telegram notify
    ├── main.py                  # BotOrchestrator + main_loop
    ├── __main__.py              # python -m dwar_bot
    ├── auth/
    │   └── cookie_manager.py    # Загрузка / валидация / ротация cookies
    ├── core/
    │   ├── browser.py           # BrowserEngine (Playwright, human_click)
    │   ├── anti_bot.py          # HumanBehavior, CaptchaHandler, AntiBot
    │   ├── telegram_bot.py      # TelegramRemoteControl (aiohttp long-poll)
    │   ├── recovery.py          # CrashRecoveryManager (health / restart)
    │   └── self_diagnostics.py  # CrashDump, DOM-гипотезы, cleanup
    ├── modules/
    │   ├── stats_parser.py      # HP/MP/рюкзак/уведомления
    │   ├── combat_engine.py     # Бои и эликсиры
    │   ├── quest_tracker.py     # NPC / квесты / локации
    │   ├── profession_farm.py   # Сбор ресурсов
    │   ├── timers_manager.py    # Кулдауны и sleep
    │   ├── auction_trader.py    # Аукцион и торговый чат
    │   ├── analytics_reporter.py# KPI, SQLite, отчёты, CSV
    │   └── background_scheduler.py # Почта, daily, бафы, cleanup
    ├── data/                    # Логи, analytics.db, crash_dumps, screenshots
    └── tests/                   # pytest
```

### Кратко по ключевым файлам

| Путь | Назначение |
|------|------------|
| `main.py` | Запуск оркестратора из корня репозитория |
| `dwar_bot/config.py` | Конфиг, `DWAR_*` / алиасы `.env`, сервера w1/w2 |
| `config/selectors.py` | Точная настройка DOM-селекторов и имён фреймов |
| `dwar_bot/auth/` | Cookie-сессии (Cookie Editor / Netscape) |
| `dwar_bot/core/` | Браузер, антибот, Telegram, recovery, диагностика |
| `dwar_bot/modules/` | Игровая логика: бой, фарм, аукцион, аналитика, планировщик |

---

## Требования

- Python **3.11+** (рекомендуется)  
- Linux / macOS / Windows (для VPS удобнее Ubuntu/Debian + Docker)  
- Аккаунт в игре + экспорт cookies  
- (Опционально) Telegram-бот от [@BotFather](https://t.me/BotFather)  

---

## Быстрый старт (локально)

### 1. Клонирование и venv

```bash
git clone <URL_РЕПОЗИТОРИЯ> dwar-bot
cd dwar-bot

python3 -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate
```

### 2. Зависимости и Chromium

```bash
pip install -r requirements.txt
playwright install chromium
# На Linux при необходимости:
# playwright install-deps chromium
```

### 3. Cookies из браузера

1. Войдите в игру на нужном мире (`w1` или `w2`) в Chrome/Firefox.  
2. Установите расширение **Cookie-Editor** (или аналог).  
3. Export → JSON.  
4. Сохраните в файл:

```bash
mkdir -p auth
# вставьте экспорт в:
# auth/cookies.json
```

В наборе должен быть актуальный `PHPSESSID`. Просроченные куки = бот не войдёт.

### 4. Настройка `.env`

```bash
cp .env.example .env
nano .env   # или любой редактор
```

Минимум:

```env
DWAR_SERVER=w2
DWAR_HEADLESS=true
COOKIES_FILE_PATH=auth/cookies.json
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=ваш_числовой_id
```

Создайте бота у `@BotFather`, напишите ему `/start`, узнайте `chat_id` (например через `@userinfobot`).

### 5. Запуск

```bash
python main.py
# или:
python -m dwar_bot
```

После старта при включённом Telegram придёт сообщение вроде «DwarBot remote online».

---

## Docker (VPS Ubuntu/Debian)

```bash
cp .env.example .env
# заполните .env и auth/cookies.json
touch bot.log
mkdir -p auth captchas

docker compose up -d --build
docker compose logs -f dwar_bot
```

Тома:

- `./auth/cookies.json` → сессия на хосте  
- `./bot.log` → лог  
- `./captchas` → скрины капчи  

Политика: `restart: unless-stopped`.

---

## Команды Telegram

| Команда | Описание |
|---------|----------|
| `/start` / `/help` | Справка |
| `/stats` | HP, золото, бой, текущая задача, KPI, фарм |
| `/screenshot` | Скриншот текущего экрана игры |
| `/pause` | Пауза главного цикла |
| `/resume` | Снять паузу |
| `/stop` | Graceful shutdown (cookie + state) |

Управление принимает сообщения **только** от `TELEGRAM_CHAT_ID` / `DWAR_TELEGRAM_CHAT_ID`.

---

## Переменные окружения

Поддерживаются префикс `DWAR_*` и короткие алиасы из `.env.example`:

| Переменная | Описание |
|------------|----------|
| `DWAR_SERVER` / `DWAR_SERVER_URL` | `w1` / `w2` или URL мира |
| `DWAR_HEADLESS` / `HEADLESS_MODE` | Headless Chromium |
| `DWAR_COOKIES_FILE` / `COOKIES_FILE_PATH` | Путь к cookies.json |
| `DWAR_TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` | Токен бота |
| `DWAR_TELEGRAM_CHAT_ID` / `TELEGRAM_CHAT_ID` | Ваш chat id |
| `DWAR_DELAY_*` | Диапазоны human-like задержек |
| `DWAR_REPORT_INTERVAL_HOURS` | Интервал KPI-отчётов |
| `DWAR_DISCORD_WEBHOOK_URL` | Опционально Discord |

Полный список — в `dwar_bot/config.py` (`load_config`).

---

## Тесты

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest dwar_bot/tests/ -q
```

---

## Безопасность и антидетект — важно

1. **Нет 100% защиты от античита.** Бот снижает риски (Безье, джиттер задержек, паузы, реакция на капчу), но не гарантирует отсутствие санкций.  
2. **Не гоняйте 24/7** без перерывов на одном персонаже.  
3. **Не занижайте** `DWAR_DELAY_*` до «роботных» значений (клик 0.01с и т.п.).  
4. Храните `.env` и `auth/cookies.json` вне git; ротируйте сессию при подозрении на компрометацию.  
5. При капче бот **останавливается**, шлёт алерт в Telegram и ждёт ручного прохождения.  
6. Crash-дампы лежат в `dwar_bot/data/crash_dumps/` — не публикуйте их вместе с cookies/токенами.  

---

## Типовой FAQ

**Капча / «ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО»**  
Пройдите проверку в браузере вручную, при необходимости обновите cookies и отправьте `/resume`.

**Сессия невалидна**  
Снова экспортируйте cookies в `auth/cookies.json` и перезапустите (`docker compose restart` или `python main.py`).

**Где логи?**  
`bot.log` (корень / том Docker) и `dwar_bot/data/logs/`.

**Как обновить селекторы после патча игры?**  
Правьте `config/selectors.py` (инструкция в docstring файла, проверка через DevTools / `document.querySelector`).

---

## Лицензия / дисклеймер

Проект предоставляется «как есть», без гарантий. Используйте на свой страх и риск и соблюдайте правила игры и применимое законодательство.
