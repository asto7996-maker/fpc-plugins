# Content Bot — Telegram контент-машина

Автоматический бот для мониторинга каналов-доноров и публикации контента в целевой канал.

## Возможности

- **Мониторинг каналов**: Отслеживает 1–50 каналов-доноров в реальном времени
- **Автопубликация**: Публикует посты в целевой канал с настраиваемой задержкой
- **Фильтрация**: Blacklist/whitelist по ключевым словам, regex, типу медиа, просмотрам
- **Обработка медиа**: Очистка метаданных, перекодирование видео, водяные знаки, сжатие
- **Дедупликация**: Хеширование текста и медиа — никаких повторов
- **Расписание**: Публикация только в заданные часы
- **Антиспам**: Пропуск рекламных постов по маркерам
- **Шаблоны подписей**: Добавление своих ссылок и текста к каждому посту
- **Админ-панель**: Полное управление через Telegram-команды
- **Стабильность**: Автоперезапуск, FloodWait-обработка, экспоненциальный бэкофф
- **Статистика**: Подробная статистика по каждому донору и общая

## Требования

- Python 3.8+
- ffmpeg (для обработки видео)
- Telegram API ключи (api_id + api_hash)
- Bot Token от @BotFather

## Быстрая установка (Ubuntu/Debian)

```bash
# Клонируйте / скопируйте папку на сервер
cd telegram_content_bot

# Запустите установщик
chmod +x install.sh
./install.sh

# Отредактируйте конфигурацию
nano config.yaml

# Первый запуск (нужна авторизация)
source venv/bin/activate
python main.py

# После успешной авторизации — запуск как сервис
sudo systemctl start content-bot
sudo systemctl enable content-bot
```

## Ручная установка

```bash
# 1. Системные зависимости
sudo apt update && sudo apt install -y python3 python3-pip python3-venv ffmpeg

# 2. Виртуальное окружение
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Конфигурация
cp config.example.yaml config.yaml
nano config.yaml

# 4. Запуск
python main.py
```

## Получение API-ключей

1. Перейдите на https://my.telegram.org/apps
2. Авторизуйтесь с вашим номером телефона
3. Создайте приложение → получите `api_id` и `api_hash`
4. Создайте бота через @BotFather → получите `bot_token`
5. Узнайте свой user ID через @userinfobot

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие |
| `/help` | Список команд |
| `/status` | Статус бота |
| `/stats` | Подробная статистика |
| `/pause` | Приостановить публикацию |
| `/resume` | Возобновить публикацию |
| `/donors` | Список доноров |
| `/add_donor @channel` | Добавить донора |
| `/remove_donor ID` | Удалить донора |
| `/toggle_donor ID` | Вкл/выкл донора |
| `/queue` | Очередь публикации |
| `/queue_size` | Размер очереди |
| `/clear_queue` | Очистить очередь |
| `/skip` | Пропустить текущий пост |
| `/set_delay мин макс` | Задержка (в секундах) |
| `/set_caption шаблон` | Шаблон подписи |
| `/set_link ссылка` | Рекламная ссылка |
| `/set_schedule 8 23` | Расписание (часы) |
| `/filters` | Текущие фильтры |
| `/add_blackword слово` | Добавить в ЧС |
| `/remove_blackword слово` | Удалить из ЧС |
| `/set_min_views 100` | Мин. просмотры |
| `/errors` | Последние ошибки |
| `/cleanup` | Очистка БД |
| `/ping` | Проверка связи |

## Шаблоны подписей

Переменные для `caption_template`:
- `{caption}` — оригинальный текст поста
- `{donor}` — название канала-донора
- `{link}` — ваша рекламная ссылка
- `{newline}` — перенос строки

Пример:
```
{caption}{newline}{newline}👉 Подписывайся: {link}
```

## Управление сервисом

```bash
sudo systemctl start content-bot      # Запуск
sudo systemctl stop content-bot       # Остановка
sudo systemctl restart content-bot    # Перезапуск
sudo systemctl status content-bot     # Статус
sudo systemctl enable content-bot     # Автозапуск при boot
journalctl -u content-bot -f          # Логи в реальном времени
```

## Структура проекта

```
telegram_content_bot/
├── main.py              # Точка входа, оркестрация
├── config.py            # Менеджер конфигурации (YAML)
├── database.py          # SQLite: посты, доноры, хеши, статистика
├── monitor.py           # Мониторинг каналов через Telethon
├── publisher.py         # Очередь и публикация в целевой канал
├── filters.py           # Фильтрация: ключевые слова, regex, медиа
├── media_processor.py   # Обработка: метаданные, видео, водяные знаки
├── bot_commands.py      # Админ-команды бота
├── config.example.yaml  # Пример конфигурации
├── requirements.txt     # Python-зависимости
├── install.sh           # Скрипт установки
└── README.md            # Документация
```

## Переменные окружения

Можно переопределить настройки через env-переменные:

| Переменная | Описание |
|-----------|----------|
| `TG_API_ID` | Telegram API ID |
| `TG_API_HASH` | Telegram API Hash |
| `TG_BOT_TOKEN` | Bot Token |
| `TG_PHONE` | Номер телефона |
| `TG_ADMIN_IDS` | Admin IDs через запятую |
| `TG_TARGET_CHANNEL` | Username целевого канала |
