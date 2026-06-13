# Starvell Cardinal — руководство по установке

**Starvell Cardinal** — Telegram-бот для автоматизации продаж на маркетплейсе [Starvell](https://starvell.com). Аналог FunPay Cardinal, адаптированный под API и специфику Starvell.

## Возможности

| Функция | Описание |
|---------|----------|
| Автовыдача | Моментальная отправка товара покупателю после оплаты |
| Автобамп | Циклическое поднятие лотов по таймеру |
| Приветствие | Авто-сообщение при новом диалоге |
| Авто-отзывы | Благодарность покупателю после закрытия сделки |
| ИИ-ответы | OpenAI / Gemini в чатах Starvell |
| Уведомления | Логи действий прямо в Telegram |
| Плагины | Расширяемость через Python-модули |
| Профиль | Баланс, статус заказов, настройки сессии |

---

## Быстрая установка на Ubuntu 24.04

> **Важно:** не копируйте буквально `<repo>` — это был плейсхолдер. Ниже готовые команды.

### Вариант 1: Одна команда (рекомендуется)

Скопируйте и выполните **целиком** на сервере под root:

```bash
curl -fsSL https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/starvell-cardinal-bot-280c/install_starvell_cardinal.sh | bash
```

Скрипт сам установит Python, скачает проект, спросит токены и запустит systemd-сервис.

### Вариант 2: Через git clone

```bash
apt update && apt install -y git
git clone -b cursor/starvell-cardinal-bot-280c https://github.com/asto7996-maker/fpc-plugins.git /opt/starvell-cardinal
cd /opt/starvell-cardinal
bash install_starvell_cardinal.sh
```

### Вариант 3: Ручная установка (без systemd)

```bash
apt update
apt install -y python3 python3-venv python3-pip git sqlite3

git clone -b cursor/starvell-cardinal-bot-280c https://github.com/asto7996-maker/fpc-plugins.git ~/starvell-cardinal
cd ~/starvell-cardinal

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

mkdir -p config storage/plugins logs
cp config/settings.json.example config/settings.json
nano config/settings.json

python main.py
```

> На Ubuntu 24.04 используйте **`python3`**, не `python3.11` — его может не быть в системе.
> Команда `pip` работает только внутри venv после `source venv/bin/activate`.

---

## Настройка config/settings.json

| Поле | Описание |
|------|----------|
| `bot_token` | Токен Telegram-бота от [@BotFather](https://t.me/BotFather) |
| `bot_password_md5` | MD5-хеш пароля для входа в бота (или задайте `BOT_PASSWORD` в env) |
| `admin_ids` | Список Telegram user_id администраторов |
| `session_cookie` | Cookie `session` авторизованного аккаунта Starvell |
| `sid_cookie` | Cookie `sid` (опционально, подтягивается автоматически) |
| `gemini_api_key` | Ключ Google Gemini для ИИ-ответов |
| `openai_api_key` | Ключ OpenAI (альтернатива Gemini) |
| `ai_provider` | `gemini` или `openai` |
| `bump_interval` | Интервал автобампа в секундах (по умолчанию 3600) |
| `api_delay_seconds` | Задержка между запросами к Starvell (антифлуд) |

### Как получить SESSION_COOKIE

1. Войдите на [starvell.com](https://starvell.com) в браузере
2. Откройте DevTools (F12) → **Application** → **Cookies** → `starvell.com`
3. Скопируйте значение cookie **`session`**
4. Вставьте в бот: **Профиль** → **Обновить SESSION_COOKIE**

### Переменные окружения (перекрывают JSON)

```bash
export BOT_TOKEN="123456:ABC..."
export BOT_PASSWORD="мой_пароль"
export SESSION_COOKIE="ваш_session_cookie"
export GEMINI_API_KEY="AIza..."
export ADMIN_IDS="123456789"
```

---

## Структура проекта

```
starvell-cardinal/
├── main.py              # Точка входа
├── config.py            # Настройки, константы
├── starvell_api.py      # Клиент API Starvell
├── tg_bot.py            # Telegram-интерфейс (aiogram 3)
├── automation.py        # Фоновая автоматизация
├── database.py          # SQLite
├── ai_service.py        # OpenAI / Gemini
├── plugin_manager.py    # Система плагинов
├── requirements.txt
├── install_starvell_cardinal.sh
├── config/
│   └── settings.json
├── plugins/             # Плагины (.py)
├── storage/             # БД и состояние плагинов
└── logs/                # Логи
```

---

## Telegram-команды

| Команда | Действие |
|---------|----------|
| `/start` | Авторизация и главное меню |
| `/menu` | Открыть панель управления |
| `/status` | Баланс и активные заказы |
| `/restart` | Перезапуск процесса бота |

### Главное меню (инлайн-кнопки)

- **Автовыдача / Автобамп / Приветствие / Авто-отзывы / ИИ-ответы** — вкл/выкл
- **Уведомления** — переключатели по типам событий
- **Профиль** — сессия Starvell, аккаунты
- **Склад автовыдачи** — добавление товаров (кодов)
- **Настройки ИИ** — ключи API, провайдер
- **Плагины** — управление модулями

---

## Автовыдача

1. Откройте **Склад автовыдачи** → **Добавить товар**
2. Укажите название **точно как на Starvell** (briefDescription лота)
3. Введите коды/ключи (каждый с новой строки)

При оплате заказа бот автоматически отправит покупателю:

```
✅ Ваш заказ выполнен!
...
⚠️ ВАЖНО! Совершая покупку, вы автоматически соглашаетесь с правилами магазина.
❌ Возврат средств (рефанд) не предусмотрен ни при каких условиях.
```

---

## Плагины

Плагины — одиночные `.py` файлы в `plugins/` с классом `Plugin`:

```python
class Plugin:
    def __init__(self, cardinal, config): ...
    def setup(self): ...      # регистрация обработчиков
    def unload(self): ...     # отписка от событий
```

События: `on_message`, `on_order`, `on_order_completed`

Состояние (вкл/выкл): `storage/plugins/state.json`

Пример: `plugins/starvell_example.py`

---

## Несколько аккаунтов Starvell

В `settings.json` добавьте массив `accounts`:

```json
{
  "accounts": [
    {
      "name": "shop1",
      "session_cookie": "cookie1...",
      "enabled": true
    },
    {
      "name": "shop2",
      "session_cookie": "cookie2...",
      "enabled": true
    }
  ]
}
```

Бот запустит параллельные циклы мониторинга для каждого аккаунта.

---

## Systemd (фоновый режим)

```bash
sudo systemctl status starvell-cardinal
sudo systemctl restart starvell-cardinal
sudo journalctl -u starvell-cardinal -f
```

## Screen (альтернатива)

```bash
screen -S cardinal
cd /opt/starvell-cardinal
source venv/bin/activate
python main.py
# Ctrl+A, D — отключиться
screen -r cardinal  # вернуться
```

---

## Безопасность

- Храните `config/settings.json` с правами `600`
- Не публикуйте SESSION_COOKIE и API-ключи
- Бот защищён паролем (до 5 попыток, блокировка 24 ч)
- Встроенный rate-limiter: не более 40 запросов/мин к Starvell

---

## Устранение неполадок

| Проблема | Решение |
|----------|---------|
| «Сессия недействительна» | Обновите SESSION_COOKIE в профиле |
| Автовыдача не работает | Проверьте название товара на складе и в лоте |
| ИИ не отвечает | Задайте Gemini/OpenAI ключ в настройках ИИ |
| Бамп не срабатывает | Убедитесь, что прошёл кулдаун Starvell на поднятие |
| Ошибки в логах | `tail -f logs/cardinal.log` |

---

## Ответственность

Использование бота должно соответствовать правилам **Starvell** и **Telegram**. Автор не несёт ответственности за действия пользователей и возможные ограничения со стороны платформ.
