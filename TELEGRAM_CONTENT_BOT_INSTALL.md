# Telegram Content Machine Bot

Безопасный Telegram-бот для автопостинга контента из разрешенных каналов и
легальных opt-in рассылок.

## Что реализовано

- Мониторинг 5-10+ каналов-источников через Telegram Bot API.
- Дедупликация постов в SQLite: один исходный пост не публикуется повторно.
- Очередь публикаций, rate-limit, retry/backoff при ошибках Telegram API.
- Репост текста, фото, видео, документов, анимаций, аудио, voice/video note.
- Опциональная очистка метаданных:
  - изображения через Pillow;
  - видео/аудио через `ffmpeg -map_metadata -1`.
- Добавление рекламного текста и inline-кнопки под постами.
- Админ-команды: `/status`, `/sources`, `/broadcast`, `/ping`.
- Рассылка только пользователям, которые сами подписались через `/start` или
  `/subscribe`.

## Что намеренно не реализовано

Бот не парсит участников чужих чатов, не инвайтит людей в каналы и не шлет
непрошеные личные сообщения. Такие сценарии нарушают правила Telegram, быстро
приводят к банам аккаунтов и создают юридические риски. Вместо этого есть
безопасная opt-in рассылка по пользователям, которые сами начали диалог с ботом.

## Важное ограничение Bot API

Обычный Telegram-бот не может читать любые открытые каналы “снаружи”. Чтобы
получать новые посты, добавьте бота в каждый канал-источник и дайте ему права,
достаточные для получения `channel_post`. В целевом канале бот тоже должен быть
администратором с правом публикации сообщений.

## Быстрая установка на Ubuntu/Debian

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/asto7996-maker/fpc-plugins.git
cd fpc-plugins
bash install_telegram_content_bot.sh
```

Если репозиторий приватный:

```bash
git clone https://<ВАШ_TOKEN>@github.com/asto7996-maker/fpc-plugins.git
cd fpc-plugins
bash install_telegram_content_bot.sh
```

Скрипт установит приложение в `/opt/telegram-content-machine`, создаст venv,
поставит зависимости и зарегистрирует systemd-сервис
`telegram-content-machine.service`.

## Ручная установка

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg git

sudo mkdir -p /opt/telegram-content-machine
sudo cp telegram_content_machine_bot.py requirements-telegram-bot.txt .env.telegram-content.example /opt/telegram-content-machine/
cd /opt/telegram-content-machine

sudo python3 -m venv venv
sudo ./venv/bin/python -m pip install --upgrade pip setuptools wheel
sudo ./venv/bin/pip install --upgrade -r requirements-telegram-bot.txt

sudo cp .env.telegram-content.example .env
sudo nano .env
```

Проверка:

```bash
/opt/telegram-content-machine/venv/bin/python \
  /opt/telegram-content-machine/telegram_content_machine_bot.py \
  --env-file /opt/telegram-content-machine/.env \
  --check-config
```

Запуск без systemd для теста:

```bash
cd /opt/telegram-content-machine
./venv/bin/python telegram_content_machine_bot.py --env-file .env
```

## Настройка `.env`

Минимальный пример:

```dotenv
BOT_TOKEN=123456:CHANGE_ME
TARGET_CHANNEL_ID=@your_target_channel
ADMIN_IDS=123456789
SOURCE_CHANNELS=@source_one,@source_two,-1001234567890

AD_TEXT=Подписывайся на наш канал: https://t.me/your_target_channel
CTA_TEXT=Открыть канал
CTA_URL=https://t.me/your_target_channel
```

Пояснения:

- `BOT_TOKEN` — токен от `@BotFather`.
- `TARGET_CHANNEL_ID` — целевой канал, куда публиковать контент.
- `ADMIN_IDS` — числовые Telegram ID администраторов бота.
- `SOURCE_CHANNELS` — список разрешенных источников через запятую.
- `AD_TEXT` — текст, который добавляется к постам.
- `CTA_TEXT` и `CTA_URL` — кнопка под постом.

Шаблоны:

```dotenv
TEXT_TEMPLATE={text}\n\n{ad_text}
CAPTION_TEMPLATE={caption}\n\n{ad_text}
```

Доступные плейсхолдеры:

- `{text}` — текст исходного сообщения;
- `{caption}` — подпись исходного медиа;
- `{ad_text}` — рекламный текст;
- `{source}` — ссылка на исходный пост, если включить
  `INCLUDE_ORIGINAL_SOURCE=true`;
- `{channel_title}` — название канала-источника;
- `{channel_username}` — username канала-источника.

## Systemd-команды

После установки через `install_telegram_content_bot.sh`:

```bash
sudo nano /opt/telegram-content-machine/.env

sudo -u tgbot /opt/telegram-content-machine/venv/bin/python \
  /opt/telegram-content-machine/telegram_content_machine_bot.py \
  --env-file /opt/telegram-content-machine/.env \
  --check-config

sudo systemctl restart telegram-content-machine.service
sudo systemctl status telegram-content-machine.service --no-pager
sudo journalctl -u telegram-content-machine.service -f
```

Остановка/перезапуск:

```bash
sudo systemctl stop telegram-content-machine.service
sudo systemctl restart telegram-content-machine.service
```

Автозапуск:

```bash
sudo systemctl enable telegram-content-machine.service
```

## Команды в Telegram

Для пользователей:

- `/start` или `/subscribe` — подписаться на opt-in рассылку.
- `/unsubscribe` — отписаться.
- `/help` — справка.

Для администраторов:

- `/status` — очередь, статистика публикаций, последние ошибки.
- `/sources` — список разрешенных источников.
- `/broadcast текст` — отправить сообщение всем opt-in подписчикам.
- Ответить `/broadcast` на сообщение — скопировать это сообщение всем
  opt-in подписчикам.
- `/ping` — проверка, что бот отвечает.

## Проверка автопостинга

1. Создайте бота через `@BotFather`.
2. Добавьте бота администратором в целевой канал.
3. Добавьте бота в каждый канал-источник.
4. Заполните `.env`.
5. Запустите сервис.
6. Опубликуйте тестовый пост в канале-источнике.
7. Проверьте целевой канал и логи:

```bash
sudo journalctl -u telegram-content-machine.service -f
```

## Частые ошибки

### Бот не видит посты источника

Проверьте:

- бот добавлен в канал-источник;
- канал указан в `SOURCE_CHANNELS` как `@username` или `-100...`;
- сервис перезапущен после изменения `.env`;
- у бота есть права на чтение новых постов канала.

### Бот не публикует в целевой канал

Проверьте:

- бот администратор целевого канала;
- у бота есть право публиковать сообщения;
- `TARGET_CHANNEL_ID` указан верно;
- в логах нет ошибок `403`, `chat not found`, `not enough rights`.

### Метаданные видео не очищаются

Установите `ffmpeg`:

```bash
sudo apt install -y ffmpeg
```

### Фото публикуются без очистки EXIF

Проверьте Pillow:

```bash
/opt/telegram-content-machine/venv/bin/pip show Pillow
```

## Обновление

```bash
cd fpc-plugins
git pull origin main
bash install_telegram_content_bot.sh
sudo systemctl restart telegram-content-machine.service
```

Скрипт не перезаписывает существующий `/opt/telegram-content-machine/.env`.
