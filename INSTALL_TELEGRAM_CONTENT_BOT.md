# Установка Telegram Content Machine на сервер

Это безопасная версия контент-бота для Telegram:

- берет новые материалы из RSS/Atom и явно разрешенных Telegram-каналов;
- кладет новые посты в очередь модерации;
- публикует одобренные посты в ваш канал;
- добавляет подпись/рекламную ссылку;
- делает рассылки только пользователям, которые сами подписались через `/start` или `/subscribe`.

Бот **не** парсит участников чужих чатов, **не** инвайтит незнакомых людей и **не** отправляет спам в ЛС.

## 1. Подготовка в Telegram

1. Создайте бота через [@BotFather](https://t.me/BotFather) и получите `BOT_TOKEN`.
2. Добавьте бота администратором в целевой канал, куда он будет публиковать посты.
3. Узнайте свой numeric Telegram ID, например через `@userinfobot`, и укажите его в `ADMIN_IDS`.
4. Если хотите принимать посты из Telegram-каналов-источников через Bot API, добавьте бота в эти каналы и укажите их ID в `ALLOWED_SOURCE_CHANNEL_IDS`.

> Для открытых каналов, где бот не состоит/не имеет доступа, Bot API не отдает поток постов. Для стабильной и безопасной работы используйте RSS/Atom или каналы, где бот добавлен с разрешения владельца.

## 2. Установка на Ubuntu 22.04/24.04

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git

cd /opt
sudo git clone https://github.com/asto7996-maker/fpc-plugins.git telegram-content-machine
sudo chown -R "$USER":"$USER" /opt/telegram-content-machine

cd /opt/telegram-content-machine
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-content-bot.txt
```

Если репозиторий приватный, клонируйте его с Personal Access Token:

```bash
git clone https://<ВАШ_TOKEN>@github.com/asto7996-maker/fpc-plugins.git telegram-content-machine
```

## 3. Настройка `.env`

```bash
cd /opt/telegram-content-machine
cp .env.content-bot.example .env
nano .env
```

Минимальные обязательные параметры:

```env
BOT_TOKEN=123456789:replace_me
ADMIN_IDS=123456789
TARGET_CHANNEL_ID=@your_public_channel
AD_SIGNATURE="Подписывайтесь: https://t.me/your_public_channel"
```

Проверка:

```bash
source .venv/bin/activate
python telegram_content_machine.py --check-config
python telegram_content_machine.py --init-db
```

## 4. Первый ручной запуск

```bash
cd /opt/telegram-content-machine
source .venv/bin/activate
python telegram_content_machine.py
```

В Telegram администратору должно прийти сообщение:

```text
✅ Telegram Content Machine запущен.
```

Остановить ручной запуск: `Ctrl+C`.

## 5. Запуск через systemd

Создайте сервис:

```bash
sudo nano /etc/systemd/system/telegram-content-machine.service
```

Вставьте:

```ini
[Unit]
Description=Telegram Content Machine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/telegram-content-machine
EnvironmentFile=/opt/telegram-content-machine/.env
ExecStart=/opt/telegram-content-machine/.venv/bin/python /opt/telegram-content-machine/telegram_content_machine.py
Restart=always
RestartSec=5
User=ubuntu
Group=ubuntu

[Install]
WantedBy=multi-user.target
```

Если на сервере другой пользователь, замените `User=ubuntu` и `Group=ubuntu`.

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-content-machine
sudo systemctl status telegram-content-machine --no-pager
```

Логи:

```bash
journalctl -u telegram-content-machine -f
```

Перезапуск после обновления:

```bash
cd /opt/telegram-content-machine
git pull origin main
source .venv/bin/activate
pip install -r requirements-content-bot.txt
sudo systemctl restart telegram-content-machine
```

## 6. Админ-команды бота

| Команда | Описание |
|---|---|
| `/status` | статистика по базе, источникам, очереди |
| `/queue` | показать очередь модерации |
| `/approve <id>` | одобрить пост для планировщика |
| `/publish <id>` | опубликовать пост сразу |
| `/schedule <id> <unix_ts или ISO дата>` | запланировать публикацию |
| `/reject <id> [причина]` | отклонить пост |
| `/feeds` | список RSS/Atom источников |
| `/feed_add <url>` | добавить RSS/Atom |
| `/feed_remove <id>` | удалить RSS/Atom |
| `/feed_on <id>` / `/feed_off <id>` | включить или выключить источник |
| `/set_signature <текст>` | обновить рекламную подпись |
| `/broadcast CONFIRM <текст>` | рассылка только opt-in подписчикам |
| `/policy` | правила безопасной работы |

Обычные пользователи:

| Команда | Описание |
|---|---|
| `/start` или `/subscribe` | добровольно подписаться на обновления |
| `/unsubscribe` | отключить рассылку |
| `/help` | справка |

## 7. Как добавить контент

### RSS/Atom

```text
/feed_add https://example.com/feed.xml
```

Новые записи попадут в `/queue`. Нажмите inline-кнопку `Одобрить` или используйте:

```text
/approve 15
/publish 15
```

### Ручной пост

Администратор может отправить боту текст, фото, видео или пересланный пост. Бот добавит материал в очередь модерации.

### Разрешенные Telegram-каналы

Укажите ID канала в `.env`:

```env
ALLOWED_SOURCE_CHANNEL_IDS=-1001234567890
```

Бот будет принимать `channel_post` updates только из этих каналов, если Telegram Bot API их передает боту.

## 8. Резервное копирование

SQLite-база хранится в `DATABASE_PATH`, по умолчанию:

```text
data/content_machine.sqlite3
```

Бэкап:

```bash
cd /opt/telegram-content-machine
sqlite3 data/content_machine.sqlite3 ".backup data/content_machine.$(date +%F_%H-%M).sqlite3"
```

## 9. Частые ошибки

### `Configuration error: BOT_TOKEN is required`

Проверьте `.env` и `EnvironmentFile` в systemd.

### Бот не публикует в канал

Проверьте:

1. бот добавлен в канал администратором;
2. `TARGET_CHANNEL_ID` указан верно;
3. у бота есть право публиковать сообщения.

### RSS не добавляет посты

Проверьте URL:

```bash
curl -I https://example.com/feed.xml
```

Источник должен отдавать RSS/Atom XML.

### Рассылка не уходит пользователям

Бот отправляет сообщения только тем, кто сам написал `/start` или `/subscribe`. Это сделано специально, чтобы не нарушать правила Telegram и не ловить баны.
