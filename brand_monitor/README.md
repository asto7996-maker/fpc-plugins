# Brand Monitor

Асинхронная система мониторинга упоминаний бренда и первичных ответов поддержки в корпоративных / партнёрских группах Telegram.

## Стек

- Python 3.10+
- Telethon — клиентские агенты поддержки
- aiogram 3.x — панель администратора
- aiosqlite — SQLite
- PySocks — SOCKS5 / HTTP прокси

## Структура

```
brand_monitor/
├── main.py                 # Точка входа
├── config.py               # Настройки из ENV
├── core/
│   └── userbot_manager.py  # Пул агентов, фильтры, backoff
├── database/
│   ├── models.py
│   └── repository.py
├── admin/
│   └── bot.py              # Admin bot (aiogram 3)
└── utils/
    ├── backoff.py
    └── templates.py
```

## Быстрый старт

```bash
cd /path/to/repo
python -m venv .venv
source .venv/bin/activate
pip install -r brand_monitor/requirements.txt

cp brand_monitor/.env.example .env
# заполните ADMIN_BOT_TOKEN и ADMIN_IDS

export $(grep -v '^#' .env | xargs)
PYTHONPATH=. python -m brand_monitor.main
```

## Admin-команды

| Команда | Назначение |
|---------|------------|
| `/auth_agent` | Авторизация нового агента (телефон → OTP → session) |
| `/agents` / `/status` | Список и статус пула |
| `/set_schedule <id> HH:MM-HH:MM` | Рабочее окно агента |
| `/keywords`, `/add_keyword`, `/del_keyword` | Ключевые слова |
| `/kb`, `/add_kb`, `/del_kb` | База знаний |

Шаблоны ответов поддерживают варианты: `{Здравствуйте|Добрый день}! Чем помочь?`

## Ядро (`userbot_manager.py`)

- Каждый агент — изолированная asyncio-задача
- Проверка `work_window` + атомарный claim в `interaction_log`
- Typing-имитация перед ответом
- `AuthKeyDuplicatedError` / `UserDeactivatedError` / исчерпание reconnect → `inactive` + алерт админу
- Exponential backoff при сетевых/прокси сбоях
