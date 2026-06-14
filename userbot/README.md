# Telegram AI Userbot

Один файл — один юзербот. Встраивается в твой Telegram-аккаунт, общается как человек через Gemini.

## Запуск

```bash
cd userbot
pip install -r requirements.txt

export TELEGRAM_API_ID=123456
export TELEGRAM_API_HASH=your_hash
export TELEGRAM_PHONE=+70000000000

python userbot.py
```

После первого входа бот напишет в **Избранное** — туда отправь API key и прокси:

```
AIzaSy...|ip:port:user:pass
```

Или `/parse_proxy` — сам подтянет публичные socks5 и проверит.

## Команды (в Избранном)

| Команда | Описание |
|---------|----------|
| `/help` | Справка |
| `/logs` | Последние 50 ошибок |
| `/status` | Uptime, Gemini, прокси |
| `/stats` | Статистика |
| `/set_prompt` | Системный промпт |
| `/set_context` | Лимит истории (20–30) |
| `/pause` / `/resume` | Пауза автоответов |
| `/blacklist_add/rm` | Исключить чат |
| `/trusted_add/rm` | Доверенные (могут менять профиль) |
| `/parse_proxy` | Парсинг прокси |
| `/restore_profile` | Вернуть профиль |
| `/reset_key` | Сброс API key |

## Поведение

- Читает 20–30 последних сообщений, подстраивается под стиль чата
- Пишет строчными, со сленгом и матом по контексту
- Умная задержка «печатает...» перед ответом
- В группах не отвечает на каждое сообщение — не палится
- Доверенные могут просить сменить ник, био, аватар
- Ошибки → `userbot_errors.log` + SQLite

## Файлы

- `userbot.py` — весь код (~4700 строк)
- `data/userbot.db` — база
- `userbot_errors.log` — лог ошибок
