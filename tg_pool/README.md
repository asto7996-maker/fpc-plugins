# TG Account Pool

Каркас системы безопасного управления пулом Telegram Userbots через admin-бота (Bot API).

## Почему Telethon, а не Pyrogram

Для пула сессий Telethon удобнее: зрелый `StringSession`, явная таксономия `FloodWaitError` / ban-ошибок, стабильные kwargs отпечатка устройства (`device_model`, `system_version`, `app_version`, `lang_code`) и предсказуемое поведение при множественных concurrent-клиентах.

## Стек

| Слой | Технология |
|------|------------|
| Userbots | Telethon |
| Admin UI | aiogram 3.x |
| DB | PostgreSQL + SQLAlchemy 2 (AsyncIO) |
| Очередь | Redis (LPUSH/BRPOPLPUSH) + APScheduler |
| Event loop | asyncio (+ uvloop на Linux) |

> Celery намеренно не используется как основной рантайм: sync-воркеры плохо стыкуются с Telethon. Redis-очередь + APScheduler держат всё в asyncio; Celery можно добавить позже для тяжёлых offline-задач.

## Архитектура

```
Admin Bot (aiogram)
        │
        ▼
  Redis Task Broker  ◄── APScheduler (promote delayed)
        │
        ▼
   TaskRouter ──► pick Account (limits / flood / rotation)
        │
        ▼
 SessionWrapper (Telethon + sticky proxy + fingerprint + jitter)
        │
        ▼
   PostgreSQL (Account / Proxy)
```

### Сущности

- **Account** — session_string, sticky fingerprint, status (`active/banned/flood_wait/paused/spambot`), `flood_until`, `is_spambot_restricted`, `total_actions_today`
- **Proxy** — SOCKS5/HTTP, 1:1 привязка к аккаунту (`assigned_account_id`)

### Anti-spam primitives

1. Sticky device fingerprint + sticky proxy (не ротировать под живой сессией)
2. Jitter `random.uniform(min, max)` перед side-effect вызовами
3. `FloodWait` → `flood_wait` + `flood_until` + alert + ротация задачи
4. Проверка `@SpamBot` (`/start` + парсинг ответа)

### Ротация задач

`TaskRouter` выбирает аккаунт с наименьшим `total_actions_today`. При FloodWait / ban аккаунт исключается (`_exclude_account_ids`), задача уходит в delayed-очередь и подхватывается следующим свободным аккаунтом.

## UI панели и доступ

- Команды: `/start` `/menu` `/profile` `/admin` `/help` (регистрируются через `set_my_commands`)
- HTML-верстка + inline-кнопки «⬅️ Назад» / «🔄 Обновить»
- Суперадмин (creator): `7835556726` — полный доступ без инвайта
- Остальные — только по одноразовому коду `ALT-XXXX-YYYY`
- `/admin` → генерация / список инвайтов, уведомление creator при активации

### Главное меню
- 🚀 Мои Аккаунты
- ➕ Добавить аккаунт (TData / Session)
- 🌐 Прокси
- 📊 Статистика
- 🤖 Gemini / Черновики (HITL draft engine)
- 🔑 Доступ (только creator)

## Gemini Draft Engine (Вариант 2)

Human-in-the-loop ассистент: юзербот ловит триггеры в группах → Gemini готовит черновик → карточка в admin-боте → оператор одобряет.

1. **Мониторинг** — `ListenerManager` (Telethon `NewMessage`) на аккаунтах с `assistant_enabled`
2. **Генерация** — `GeminiDraftClient` + `PendingDraft` в PostgreSQL
3. **Карточка оператора** — Отправить / Изменить / Отклонить / Авто-режим
4. **Отправка** — delay + typing action, затем `send_message` через тот же userbot
5. **`auto_approve_enabled`** (по умолчанию `False`) — пропуск ручного подтверждения

Env: `GEMINI_API_KEY` (или ключ в настройках панели).

## Быстрый старт

```bash
cd tg_pool
docker compose up -d   # postgres + redis (или SQLITE в .env)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt aiosqlite
cp .env.example .env
# заполните ADMIN_BOT_TOKEN, ADMIN_IDS, TELEGRAM_API_ID/HASH

# запуск с автоперезапуском при зависании:
chmod +x scripts/run.sh
./scripts/run.sh
PYTHONPATH=. python -m tg_pool
```

В боте: `/menu` → Add proxy → Add account (StringSession) → Activate → SpamBot / Ping.

### Импорт TData (ZIP)

1. `/menu` → **📦 Import TData ZIP** (или `/import_tdata`)
2. Укажите sticky-прокси (`socks5://…` / id / `none`)
3. Passcode TData или `none`
4. Пришлите ZIP с папкой `tdata`

Бот через `opentele` конвертирует сессию в `StringSession`, сохраняет organic Desktop fingerprint (`device_model` / `system_version` / `app_version`), проверяет аккаунт через прокси и пишет в БД со статусом `active`. Временные файлы удаляются в `finally`.

## Модули

```
tg_pool/
├── db/models.py              # Account, Proxy
├── clients/session_wrapper.py# Telethon + FloodWait + jitter
├── clients/spambot.py        # @SpamBot probe
├── services/task_router.py   # лимиты + ротация
├── services/account_service.py
├── services/alerts.py
├── taskqueue/broker.py       # Redis
├── taskqueue/scheduler.py    # APScheduler
└── admin/bot.py              # aiogram panel
```
