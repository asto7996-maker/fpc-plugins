# MangaBuff Autopilot

Telegram-бот (aiogram 3.x) для MangaBuff.ru:

- авточтение тайтлов (главы через `addHistory`)
- карты · сундуки · паки · эвенты
- уведомления о дропе карт

Сессия браузера: `user_data_mangabuff/`.

## Возможности

- Playwright persistent-профиль
- Фарм популярных тайтлов до ~90%
- Темп чтения пресетами (Турбо … Медленно)
- Сбор наград / сундуков / паков
- Детект карт из `/notifications` и ответа `addHistory` (с редкостью)
- Выставление лотов на `/market` (цена = 1 карта ранга выше)
- Ночная пауза 01:00–05:00 МСК

## Структура

```
remanga_autobattle/
├── bot.py
├── scheduler.py
├── config.py / settings_store.py / ui_theme.py
├── services/
│   └── mangabuff_service.py
└── user_data_mangabuff/
```

## Установка

```bash
curl -fsSL https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/remanga-autobattle-ba83/remanga_autobattle/install.sh | bash
```

Или вручную:

```bash
cd remanga_autobattle
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
cp .env.example .env   # BOT_TOKEN + опционально MANGABUFF_EMAIL/PASSWORD
python bot.py
```

## Setup сессии MangaBuff

```bash
systemctl stop remanga-autobattle
cd /root/remanga_autobattle && source .venv/bin/activate
python bot.py --setup-mangabuff
systemctl start remanga-autobattle
```

Логи: `journalctl -u remanga-autobattle -f`
