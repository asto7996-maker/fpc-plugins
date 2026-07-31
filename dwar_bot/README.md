# dwar_bot

Модульный, отказоустойчивый асинхронный бот для браузерной игры
**«Легенда: Наследие Драконов» (Dwar)**.

> ⚠️ Проект предназначен для образовательных целей и автоматизации собственного
> аккаунта. Использование ботов может нарушать правила игры — ответственность за
> применение лежит на пользователе.

## Архитектура

```
dwar_bot/
├── config.py              # Конфигурация, селекторы, задержки, креды
├── logger.py              # Логирование в файл (bot.log) и Telegram
├── auth/
│   └── cookie_manager.py  # Авторизация через Cookie Editor (JSON/Netscape), ротация сессий
├── core/
│   ├── browser.py         # Инициализация Playwright, перехват сетевых запросов
│   └── anti_bot.py        # Имитация человеческого поведения, задержки, движения мыши
├── modules/
│   ├── stats_parser.py    # Парсинг профиля, рюкзака, денег, статистики, уведомлений
│   ├── combat_engine.py   # Движок боёв: удары, эликсиры, касты, парсинг логов
│   ├── quest_tracker.py   # Прохождение сюжета, диалоги NPC, выбор веток
│   └── timers_manager.py  # Кулдауны, тайм-ауты профессий, восстановление энергии
└── main.py                # Главный асинхронный цикл и оркестратор модулей
```

## Статус реализации

| Модуль | Статус |
| --- | --- |
| `config.py` | ✅ Готов |
| `logger.py` | ✅ Готов |
| `auth/cookie_manager.py` | ✅ Готов |
| `core/browser.py` | ⏳ Далее |
| `core/anti_bot.py` | ⏳ Далее |
| `modules/*` | ⏳ Далее |
| `main.py` | ⏳ Далее |

## Установка

```bash
cd dwar_bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # только если используете браузерный слой
cp .env.example .env               # заполните своими значениями
```

## Cookie / сессии

Экспортируйте cookie игрового домена расширением **Cookie-Editor** (кнопка
*Export* → *JSON*) либо любым инструментом в формате **Netscape cookies.txt** и
положите файл(ы) в директорию `cookies/`. Каждый файл — отдельный аккаунт.

```python
from dwar_bot.auth import CookieManager

cm = CookieManager()
cm.load()                      # найдёт и провалидирует все профили
print(cm.summary())

# Для Playwright:
# await context.add_cookies(cm.playwright_cookies())

# При разлогине/бане — ротация на следующий валидный профиль:
cm.invalidate_current(reason="logout detected")
if not cm.rotate():
    print("Валидных сессий не осталось")
```

Поддерживаемые форматы: **Cookie-Editor JSON** и **Netscape cookies.txt**
(формат определяется автоматически). Валидация проверяет наличие обязательных
cookie (`DWAR_REQUIRED_COOKIES`), их срок годности и принадлежность домену.

## Конфигурация

Все параметры имеют разумные значения по умолчанию и переопределяются
переменными окружения / файлом `.env` (см. `.env.example`). Точка входа —
синглтон `settings` в `dwar_bot/config.py`.
